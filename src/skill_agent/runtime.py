"""Generic tool loop. Skill policies, models, tools, and sessions are replaceable."""

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Protocol

from .contracts import ResolveSkillRequest, fingerprint, source_fingerprint


SKILL_REQUEST_INSTRUCTIONS = (
    "Use the available procedural skill library to help complete this task. "
    "<skill_catalog> lists available topics, not their instructions. No skill is loaded initially.\n"
    "Workflow:\n"
    "1. Inspect the inputs when needed to identify the operation you are about to perform.\n"
    "2. Consult request_skill for the matching catalog topic before implementing a covered "
    "operation. Choose the topic and timing yourself; inspection can come first. If active "
    "guidance already covers this operation, use it without requesting it again.\n"
    "3. Read the resulting <active_skill> instructions on your next turn and execute the "
    "procedure with the other tools.\n"
    "4. Before moving to a different operation not covered by active guidance, consult "
    "request_skill again for that specific operation.\n"
    "5. If two consecutive attempts fail or produce no useful progress, stop and consult "
    "skill_catalog before trying another technique. Failure includes command errors, missing "
    "tools or libraries, empty or unusable output, and repeating an approach without recovering "
    "the needed information. If a relevant topic is available, call request_skill with a focused "
    "description of the difficulty and relevant observations, even if guidance was loaded earlier. "
    "Use the returned procedure before continuing; do not keep cycling through libraries or "
    "repeating unsuccessful actions without checking the available guidance.\n"
    "All topics belong to the same assigned skill. Describe only the operation you need now "
    "and include relevant observed facts in context; do not repeat the original task unless "
    "needed. Each request replaces active guidance; previous feature values are not inherited. "
    "The tool returns a receipt, while the guidance appears in the next prompt. "
    "If no guidance is returned or no topic is relevant, continue using the other tools. "
    "Do not repeat an identical unavailable request."
)


AUTOMATIC_SKILL_INSTRUCTIONS = (
    "Procedural guidance is supplied automatically in <active_skill> when available. "
    "The initial view uses the task; subsequent views use the last three assistant turns' "
    "available reasoning/planning text, with tool arguments as fallback. Each refresh replaces "
    "the active view, rather than appending another skill document. Use the supplied guidance "
    "to complete the task; if no guidance is available, continue with the other tools.\n"
    "<skill_catalog> lists the available topics. request_skill remains callable for compatibility, "
    "but in this automatic mode it only acknowledges your request: it does not resolve guidance "
    "or override the automatically managed view. You do not need to call it to receive guidance."
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


class ModelInitiatedSkillPolicy:
    """Leave selection to the model; no automatic or mandatory initial request."""

    async def before_model(self, session, turn):
        pass


class NoSkillPolicy:
    """Run with task instructions and execution tools only."""

    skill_enabled = False

    async def before_model(self, session, turn):
        pass


def planning_text(message):
    """Read provider-exposed planning only; never infer or generate hidden reasoning."""
    reasoning = message.get("reasoning") or message.get("reasoning_content")
    if not isinstance(reasoning, str) or not reasoning.strip():
        reasoning = "\n".join(
            detail["text"] for detail in (message.get("reasoning_details") or [])
            if isinstance(detail, dict) and isinstance(detail.get("text"), str)
        )
    content = message.get("content")
    parts = [text.strip() for text in (reasoning, content)
             if isinstance(text, str) and text.strip()]
    if parts:
        return "\n".join(dict.fromkeys(parts))
    return "\n".join(
        f"{call.get('function', {}).get('name', '')}: "
        f"{call.get('function', {}).get('arguments', '')}"
        for call in (message.get("tool_calls") or [])
        if call.get("function", {}).get("name") != "request_skill"
    )


class RollingSkillPolicy:
    """Refresh automatically from a fixed three-response window, without an LLM selector."""

    automatic_guidance = True
    instructions = AUTOMATIC_SKILL_INSTRUCTIONS

    async def before_model(self, session, turn):
        recent = [event for event in session.events if event["type"] == "model_response"][-3:]
        texts = [planning_text(event["message"]) for event in recent]
        query = "\n\n".join(text for text in texts if text)
        selection = {"policy": "rolling", "turn": turn,
                     "source_turns": [event["turn"] for event in recent],
                     "input_source": "recent_model_turns" if query else "task"}
        # A focused task avoids inheriting old feature values or appending the original task.
        await session.load_skill(task=query or session.task, selection=selection)


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
    capabilities: list[dict[str, str]] = field(default_factory=list)

    def catalog_text(self):
        lines = [f"Available skill: {self.skill_id}"]
        lines.extend(f"- {topic['name']}: {topic['description']}" for topic in self.capabilities)
        if not self.capabilities:
            lines.append("Request procedural guidance for your current operation when useful.")
        return "<skill_catalog>\n" + "\n".join(lines) + "\n</skill_catalog>"

    async def load_skill(self, context=None, *, task=None, selection=None):
        """Resolve a focused task independently, or reuse the initial task for a policy load."""
        if context is not None and not isinstance(context, dict):
            raise ValueError("skill context must be an object")
        observed = dict(self.observed) if task is None else {}
        observed.update(context or {})
        request = ResolveSkillRequest(
            self.skill_id, self.task if task is None else task, observed, self.artifact_id)
        started = time.monotonic()
        audit = {"selection": selection} if selection is not None else {}
        self.events.append({"type": "skill_request", "request": request.to_dict(), **audit})
        try:
            result = await self.resolver.resolve(request)
        except Exception as exc:
            event = {"type": "skill_error", "error": type(exc).__name__,
                     "seconds": time.monotonic() - started}
            if isinstance(getattr(exc, "usage", None), dict):
                event["usage"] = exc.usage
            self.events.append(event)
            raise
        if result.artifact_id != self.artifact_id:
            raise ValueError("resolver returned a different artifact")
        if self.expected_revision is not None and result.resolver_revision != self.expected_revision:
            raise ValueError("resolver implementation changed during experiment")
        if result.source_fingerprint != self.expected_source:
            raise ValueError("full and reduced skills are not from the same source")
        self.observed = observed
        if result.capabilities:
            self.capabilities = result.capabilities
        self.active_skills[self.skill_id] = result.markdown
        self.events.append({"type": "skill", "request": request.to_dict(), "response": result.to_dict(),
                            "markdown_sha256": source_fingerprint(result.markdown),
                            "seconds": time.monotonic() - started, **audit})
        # A receipt rather than duplicated guidance in tool history.
        return {"skill_id": self.skill_id, "markdown_sha256": source_fingerprint(result.markdown),
                "guidance_available": bool(result.markdown)}


class SkillRequestTool:
    """Model-initiated guidance refresh through the backend-independent resolver."""

    definition = {"type": "function", "function": {
        "name": "request_skill",
        "description": (
            "Look up procedural guidance from any topic in skill_catalog for your current need. "
            "You may call this after inspecting inputs and again for a different operation or "
            "when current guidance is insufficient. Each call replaces the active skill view."
        ),
        "parameters": {"type": "object", "properties": {
            "request": {"type": "string", "description": (
                "Describe the specific operation or troubleshooting help you need now, using "
                "the catalog topic where relevant. Request only the guidance needed at this point."
            )},
            "context": {"type": "object", "additionalProperties": True,
                        "description": "Optional observed facts relevant now; previous context is not inherited."},
        }, "required": ["request"], "additionalProperties": False},
    }}

    def __init__(self, session: SessionContext, *, acknowledge_only=False):
        self.session = session
        self.acknowledge_only = acknowledge_only

    async def execute(self, arguments):
        request = arguments.get("request")
        context = arguments.get("context", {})
        if (set(arguments) - {"request", "context"} or not isinstance(request, str)
                or not request.strip() or not isinstance(context, dict)):
            raise ValueError("request_skill expects nonempty request text and optional context object")
        fingerprint(context)
        if self.acknowledge_only:
            text = self.session.active_skills.get(self.session.skill_id, "")
            return {"skill_id": self.session.skill_id, "selection_mode": "rolling",
                    "request_applied": False, "guidance_available": bool(text),
                    "markdown_sha256": source_fingerprint(text)}
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
    policy = policy or ModelInitiatedSkillPolicy()
    skill_enabled = getattr(policy, "skill_enabled", True)
    started = time.monotonic()
    history = [{"role": "user", "content": session.prompt}]
    events = skill_context.events if skill_context is not None else []
    prediction, status, error = None, "failed", None

    async def loop():
        nonlocal status, prediction
        if "request_skill" in session.tools:
            raise ValueError("request_skill is reserved for the agent's skill resolver")
        tools = dict(session.tools)
        instructions = system_prompt
        if skill_enabled:
            if skill_context is None:
                raise ValueError("skill-enabled episodes require a skill context")
            tools = {"request_skill": SkillRequestTool(
                skill_context, acknowledge_only=getattr(policy, "automatic_guidance", False)), **tools}
            instructions += ("\n\n" + getattr(policy, "instructions", SKILL_REQUEST_INSTRUCTIONS)
                             + "\n\n" + skill_context.catalog_text())
        for turn in range(max_calls):
            skill_text = ""
            if skill_enabled:
                await policy.before_model(skill_context, turn)
                skill_text = "\n\n".join(text for text in skill_context.active_skills.values() if text)
            # Full/reduced compose the active view; no-skill omits the entire skill interface.
            active = "\n\n<active_skill>\n" + skill_text + "\n</active_skill>" if skill_text else ""
            messages = [{"role": "system", "content": instructions + active}, *history]
            definitions = [tool.definition for tool in tools.values()]
            events.append({"type": "model_request", "turn": turn, "messages": messages,
                           "tools": definitions, "request_sha256": fingerprint([messages, definitions])})
            response = await model.complete(messages, definitions)
            events.append({"type": "model_response", "turn": turn, "message": response.message,
                           "usage": response.usage, "metadata": response.metadata})
            history.append(response.message)
            if (getattr(session, "require_complete_responses", False)
                    and response.metadata.get("finish_reason") in ("length", "content_filter")):
                raise ValueError("model response was truncated or blocked")
            calls = response.message.get("tool_calls") or []
            if not calls:
                if hasattr(session, "customer_response"):
                    reply = await session.customer_response(response.message)
                    if reply is not None:
                        events.append({"type": "user_message", "turn": turn, "content": reply})
                        history.append({"role": "user", "content": reply})
                        continue
                prediction = await session.collect()
                status = "completed"
                return
            for call in calls:
                name = call["function"]["name"]
                args = {}
                try:
                    args = json.loads(call["function"]["arguments"])
                    if not isinstance(args, dict):
                        raise ValueError("tool arguments must be an object")
                    if name not in tools:
                        raise ValueError("unknown tool")
                    output = await tools[name].execute(args)
                except (ValueError, KeyError) as exc:
                    output = {"error": str(exc)}
                    if name != "request_skill" and hasattr(session, "record_invalid_tool_call"):
                        session.record_invalid_tool_call(name, args if isinstance(args, dict) else {}, output)
                event = {"type": "tool", "name": name, "call": call, "result": output}
                if hasattr(session, "customer_response"):
                    event["turn"] = turn
                events.append(event)
                history.append({"role": "tool", "tool_call_id": call["id"],
                                "content": json.dumps(output, ensure_ascii=False)})
                if getattr(session, "finished", False):
                    prediction = await session.collect()
                    status = "completed"
                    return
        raise TimeoutError("model-call budget exhausted")

    try:
        await asyncio.wait_for(loop(), timeout=timeout_seconds)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        status = "budget_exhausted" if isinstance(exc, TimeoutError) else "failed"
        if getattr(session, "collect_on_failure", False):
            try:
                prediction = await session.collect()
            except Exception:
                pass  # Keep the original error and mark the episode incomplete.
    finally:
        try:
            await session.close()
        except Exception as exc:
            status, error = "failed", f"cleanup failed: {type(exc).__name__}: {exc}"
    return EpisodeResult(status, prediction, error, events, time.monotonic() - started)
