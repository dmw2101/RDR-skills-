"""Generic tool loop. Skill policies, models, tools, and sessions are replaceable."""

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Protocol

from .contracts import ResolveSkillRequest, fingerprint, source_fingerprint


SKILL_REQUEST_INSTRUCTIONS = (
    "Use request_skill when the active guidance does not cover the operation you need next. "
    "Describe that operation briefly; do not repeat the whole task unless all of it is needed. "
    "Include only relevant observed facts in context. Each request replaces the active skill "
    "section for subsequent model calls; previous feature values are not carried over. "
    "The tool returns a receipt; read the updated <active_skill> section on your next turn. "
    "If no guidance is available, continue with the other tools."
)


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

    async def load_skill(self, context=None, *, task=None):
        """Resolve a focused task independently, or reuse the initial task for a policy load."""
        if context is not None and not isinstance(context, dict):
            raise ValueError("skill context must be an object")
        observed = dict(self.observed) if task is None else {}
        observed.update(context or {})
        request = ResolveSkillRequest(
            self.skill_id, self.task if task is None else task, observed, self.artifact_id)
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
        self.observed = observed
        self.active_skills[self.skill_id] = result.markdown
        self.events.append({"type": "skill", "request": request.to_dict(), "response": result.to_dict(),
                            "markdown_sha256": source_fingerprint(result.markdown),
                            "seconds": time.monotonic() - started})
        # A receipt rather than duplicated guidance in tool history.
        return {"skill_id": self.skill_id, "markdown_sha256": source_fingerprint(result.markdown),
                "guidance_available": bool(result.markdown)}


class SkillRequestTool:
    """Model-initiated guidance refresh through the backend-independent resolver."""

    definition = {"type": "function", "function": {
        "name": "request_skill",
        "description": "Replace active guidance for the assigned skill with guidance for your next operation.",
        "parameters": {"type": "object", "properties": {
            "request": {"type": "string", "description": "The specific operation you need guidance for now."},
            "context": {"type": "object", "additionalProperties": True,
                        "description": "Optional observed facts relevant now; previous context is not inherited."},
        }, "required": ["request"], "additionalProperties": False},
    }}

    def __init__(self, session: SessionContext):
        self.session = session

    async def execute(self, arguments):
        request = arguments.get("request")
        context = arguments.get("context", {})
        if (set(arguments) - {"request", "context"} or not isinstance(request, str)
                or not request.strip() or not isinstance(context, dict)):
            raise ValueError("request_skill expects nonempty request text and optional context object")
        fingerprint(context)
        try:
            return await self.session.load_skill(context, task=request.strip())
        except Exception as exc:
            # Resolver/provenance failures abort the attempt rather than reusing old guidance.
            raise RuntimeError(f"skill refresh failed: {exc}") from exc


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
        tools = dict(session.tools)
        if "request_skill" in tools:
            raise ValueError("request_skill is reserved for the agent's skill resolver")
        tools["request_skill"] = SkillRequestTool(skill_context)
        instructions = system_prompt + "\n\n" + SKILL_REQUEST_INSTRUCTIONS
        for turn in range(max_calls):
            await policy.before_model(skill_context, turn)
            skill_text = "\n\n".join(text for text in skill_context.active_skills.values() if text)
            # This is the only part of the model input that differs across arms.
            active = "\n\n<active_skill>\n" + skill_text + "\n</active_skill>" if skill_text else ""
            messages = [{"role": "system", "content": instructions + active}, *history]
            definitions = [tool.definition for tool in tools.values()]
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
                    if name not in tools:
                        raise ValueError("unknown tool")
                    output = await tools[name].execute(args)
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
