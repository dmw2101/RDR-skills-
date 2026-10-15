"""Generic tool loop. Skill policies, models, tools, and sessions are replaceable."""

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Protocol

from .contracts import ResolveSkillRequest, fingerprint, source_fingerprint


class Tool(Protocol):
    definition: dict
    async def execute(self, arguments: dict) -> dict: ...


class SkillRequestPolicy(Protocol):
    async def before_model(self, session: "SessionContext", turn: int) -> None: ...


class InitialSkillPolicy:
    async def before_model(self, session, turn):
        if turn == 0:
            await session.load_skill()


@dataclass
class SessionContext:
    skill_id: str
    artifact_id: str
    task: str
    resolver: object
    expected_source: str
    expected_revision: str | None = None
    observed: dict = field(default_factory=dict)
    active_skills: dict = field(default_factory=dict)
    events: list = field(default_factory=list)

    async def load_skill(self, context=None):
        if context:
            self.observed.update(context)
        request = ResolveSkillRequest(self.skill_id, self.task, dict(self.observed), self.artifact_id)
        started = time.monotonic()
        self.events.append({"type": "skill_request", "request": request.to_dict()})
        try:
            result = await self.resolver.resolve(request)
        except Exception as exc:
            self.events.append({"type": "skill_error", "error": type(exc).__name__,
                                "seconds": time.monotonic() - started})
            raise
        if result.artifact_id != self.artifact_id:
            raise ValueError("resolver returned a different artifact")
        if self.expected_revision is not None and result.resolver_revision != self.expected_revision:
            raise ValueError("resolver implementation changed during experiment")
        if result.source_fingerprint != self.expected_source:
            raise ValueError("full and reduced skills are not from the same source")
        self.active_skills[self.skill_id] = result.markdown
        self.events.append({"type": "skill", "request": request.to_dict(), "response": result.to_dict(),
                            "markdown_sha256": source_fingerprint(result.markdown),
                            "seconds": time.monotonic() - started})
        # A receipt rather than duplicated guidance in tool history.
        return {"skill_id": self.skill_id, "markdown_sha256": source_fingerprint(result.markdown)}


@dataclass(frozen=True)
class EpisodeResult:
    status: str
    prediction: dict | None
    error: str | None
    events: list
    seconds: float


async def run_episode(*, model, session, skill_context, system_prompt, policy=None,
                      max_calls=20, timeout_seconds=600):
    """Session exposes prompt, tools, collect(), close(); inputs exclude gold labels."""
    policy = policy or InitialSkillPolicy()
    started = time.monotonic()
    history = [{"role": "user", "content": session.prompt}]
    events = skill_context.events
    prediction, status, error = None, "failed", None

    async def loop():
        nonlocal status, prediction
        for turn in range(max_calls):
            await policy.before_model(skill_context, turn)
            skill_text = "\n\n".join(text for text in skill_context.active_skills.values() if text)
            # This is the only part of the model input that differs across arms.
            active = "\n\n<active_skill>\n" + skill_text + "\n</active_skill>" if skill_text else ""
            messages = [{"role": "system", "content": system_prompt + active}, *history]
            definitions = [tool.definition for tool in session.tools.values()]
            events.append({"type": "model_request", "turn": turn, "messages": messages,
                           "tools": definitions, "request_sha256": fingerprint([messages, definitions])})
            response = await model.complete(messages, definitions)
            events.append({"type": "model_response", "turn": turn, "message": response.message,
                           "usage": response.usage, "metadata": response.metadata})
            history.append(response.message)
            calls = response.message.get("tool_calls") or []
            if not calls:
                prediction = await session.collect()
                status = "completed"
                return
            for call in calls:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"]["arguments"])
                    if not isinstance(args, dict):
                        raise ValueError("tool arguments must be an object")
                    if name not in session.tools:
                        raise ValueError("unknown tool")
                    output = await session.tools[name].execute(args)
                except (ValueError, KeyError) as exc:
                    output = {"error": str(exc)}
                events.append({"type": "tool", "name": name, "call": call, "result": output})
                history.append({"role": "tool", "tool_call_id": call["id"],
                                "content": json.dumps(output, ensure_ascii=False)})
        raise TimeoutError("model-call budget exhausted")

    try:
        await asyncio.wait_for(loop(), timeout=timeout_seconds)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        status = "budget_exhausted" if isinstance(exc, TimeoutError) else "failed"
    finally:
        try:
            await session.close()
        except Exception as exc:
            status, error = "failed", f"cleanup failed: {type(exc).__name__}: {exc}"
    return EpisodeResult(status, prediction, error, events, time.monotonic() - started)
