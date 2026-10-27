"""Read skill views and visible contexts back out of saved episode records."""

import json
import re

from skill_agent.learned_features import judge_context

# Provider refusals and network failures that outlasted the model client's retries, and judge
# answers that could not be used. Such an attempt says nothing about the worker, the inspector or
# the skill, so it is retried, never kept.
INTERRUPTION = re.compile(r"HTTP (429|5\d\d) from |URLError|Connection\w*Error|RemoteDisconnected|"
                          r"IncompleteRead|TimeoutError|JudgeError")


class Interrupted(RuntimeError):
    """A model call was refused or lost, or a judge answer was unusable.

    Rerunning the same command continues where it stopped.
    """


def was_interrupted(outcome):
    """True for an episode that failed on a refused or lost call, not on its own behaviour or budget."""
    error = outcome.get("error") or ""
    return (outcome.get("status") == "failed" and not error.startswith("cleanup failed")
            and INTERRUPTION.search(error) is not None)


def skill_views(events, materials=None):
    """Every resolved view with the model turn it was first shown on.

    context is the text the judge reads: the worker's starting materials (when the
    benchmark provides them) and the resolver query of that view.

    A view applies to the next model request, both for rolling refreshes (made
    before the request) and for model-initiated requests (made during a turn).
    """
    views, turn = [], 0
    for event in events:
        if event["type"] == "model_request":
            turn += 1
        elif event["type"] == "skill":
            metadata = event["response"].get("metadata", {})
            views.append({
                "turn": turn,
                "context": judge_context(event["request"]["task"], materials),
                "case": metadata.get("case"),
                "fragment_ids": metadata.get("fragment_ids", []),
                "terminal_ids": metadata.get("terminal_ids", []),
                "markdown_sha256": event.get("markdown_sha256"),
            })
    return views


def view_at(events, turn, materials=None):
    """The view active when the model produced ``turn`` (zero-based)."""
    active = [view for view in skill_views(events, materials) if view["turn"] <= turn]
    return active[-1] if active else None


def _clip(value, limit):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + f"... [{len(text) - limit} more characters]"


def trace_steps(events, from_turn=0, to_turn=None, *, limit=4000):
    """Readable transcript slice for the verifier, with long outputs truncated."""
    steps, turn = [], -1
    for event in events:
        kind = event["type"]
        if kind == "model_request":
            turn += 1
            continue
        if turn < from_turn or (to_turn is not None and turn > to_turn):
            continue
        if kind == "model_response":
            message = event["message"]
            steps.append({"turn": turn, "role": "assistant",
                          "reasoning": _clip(message.get("reasoning") or message.get("reasoning_content") or "", limit),
                          "content": _clip(message.get("content") or "", limit),
                          "tool_calls": [{"name": call["function"]["name"],
                                          "arguments": _clip(call["function"]["arguments"], limit)}
                                         for call in message.get("tool_calls") or []],
                          "finish_reason": event.get("metadata", {}).get("finish_reason")})
        elif kind == "tool":
            steps.append({"turn": turn, "role": "tool", "name": event["name"],
                          "result": _clip(event["result"], limit)})
        elif kind == "user_message":
            steps.append({"turn": turn, "role": "user", "content": _clip(event["content"], limit)})
    return steps


def turn_count(events):
    return sum(event["type"] == "model_request" for event in events)


def episode_key(record):
    return f"{record['task_id']}|{record['condition']}|{record['replicate']}"
