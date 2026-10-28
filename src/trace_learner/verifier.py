"""Agentic error analysis of one failed episode, run through skill_agent's own tool loop."""

import json
from importlib.resources import files

from pydantic import ValidationError

from skill_agent.runtime import NoSkillPolicy, run_episode

from .proposals import Diagnosis
from .records import skill_views, trace_steps, turn_count


def verifier_prompt(benchmark):
    return files("trace_learner").joinpath("prompts/verifier.md").read_text(encoding="utf-8").replace(
        "{benchmark}", benchmark)


def _clip(value, limit=12_000):
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + f"... [{len(text) - limit} more characters]"


class _Tool:
    def __init__(self, name, description, properties, required, run):
        self.definition = {"type": "function", "function": {
            "name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required,
                           "additionalProperties": False}}}
        self.run = run

    async def execute(self, arguments):
        try:
            return await self.run(arguments)
        except (ValueError, KeyError):
            raise
        except Exception as exc:  # Report tool failures to the verifier instead of aborting.
            raise ValueError(f"{type(exc).__name__}: {exc}") from exc


class VerifierSession:
    """Read-only access to one episode plus gold-label tools; ends on submit_diagnosis."""

    def __init__(self, *, task, record, hooks, workspace, passed=False):
        self.task, self.record, self.hooks, self.workspace = task, record, hooks, workspace
        self.events = record.get("events", [])
        self.rescores = []
        self.diagnosis = None
        self.finished = False
        self.inspection = None
        self.prompt = (
            f"Task ID: {task['id']}\nCondition: {record['condition']}, replicate {record['replicate']}\n"
            f"Episode status: {record['status']}" + (f" ({record['error']})" if record.get("error") else "")
            + f"\nOfficial verdict: {'passed' if passed else 'FAILED'}\n"
            f"Model turns: {turn_count(self.events)} (zero-based turn numbers)\n\n"
            f"Task given to the agent:\n{task['task']}\n\n"
            "Final prediction submitted by the agent:\n" + _clip(record.get("prediction"), 6000)
            + "\n\nFollow the mandatory workflow and finish with submit_diagnosis.")
        object_schema = {"type": "object", "additionalProperties": True}
        tools = [
            _Tool("read_trace", "Read the agent's transcript for an inclusive range of zero-based turns.",
                  {"from_turn": {"type": "integer", "minimum": 0}, "to_turn": {"type": "integer", "minimum": 0}},
                  ["from_turn", "to_turn"], self._read_trace),
            _Tool("skill_views", "List the guidance shown at each turn: tree case, rendered nodes and fragments.",
                  {}, [], self._skill_views),
            _Tool("show_fragment", "Show the text of one skill fragment by ID.",
                  {"fragment_id": {"type": "string"}}, ["fragment_id"], self._show_fragment),
            _Tool("show_tree", "Show the MCRDR tree: node conditions, fragments, and features in use.",
                  {}, [], self._show_tree),
            _Tool("ground_truth", "Show the gold answer for this task (never visible to the agent).",
                  {}, [], self._ground_truth),
            _Tool("compare_with_ground_truth", "Officially score the agent's actual prediction, field by field.",
                  {}, [], self._compare),
            _Tool("inspect_shell", "Run a shell command in a fresh copy of the task's original inputs.",
                  {"command": {"type": "string"}}, ["command"], self._shell),
            _Tool("rescore", "Officially score a corrected prediction that your diagnosis implies.",
                  {"prediction": object_schema}, ["prediction"], self._rescore),
            _Tool("submit_diagnosis", "Submit the final diagnosis (once).",
                  {"diagnosis": {**object_schema, "description": json.dumps(
                      Diagnosis.model_json_schema(), ensure_ascii=False)}},
                  ["diagnosis"], self._submit),
        ]
        self.tools = {tool.definition["function"]["name"]: tool for tool in tools}

    async def _read_trace(self, args):
        start, end = int(args["from_turn"]), int(args["to_turn"])
        if end < start or end - start > 9:
            raise ValueError("read at most 10 turns at a time, with to_turn >= from_turn")
        return {"steps": trace_steps(self.events, start, end)}

    async def _skill_views(self, args):
        return {"views": [{**view, "context": view["context"][-1500:]} for view in skill_views(self.events)]}

    async def _show_fragment(self, args):
        catalog = self.workspace.catalog
        for fragment in (*catalog.fragments, *catalog.learned):
            if fragment.id == args["fragment_id"]:
                return {"id": fragment.id, "text": fragment.text}
        raise ValueError("unknown fragment ID")

    async def _show_tree(self, args):
        nodes = [{"id": n.id, "parent": n.parent, "conditions": list(n.conditions), "stopping": n.stopping,
                  "fragments": (n.conclusion or {}).get("fragments", []), "cornerstones": len(n.evidence)}
                 for n in self.workspace.engine.nodes]
        features = {name: spec.model_dump() for name, spec in self.workspace.schema.features.items()}
        return {"nodes": nodes, "features": features}

    async def _ground_truth(self, args):
        return {"ground_truth": self.hooks.ground_truth(self.task)}

    async def _compare(self, args):
        prediction = self.record.get("prediction")
        if not isinstance(prediction, dict):
            return {"passed": False, "note": f"no prediction was collected (status {self.record['status']})"}
        return await self.hooks.rescore(self.task, prediction)

    async def _shell(self, args):
        if self.inspection is None:
            self.inspection = await self.hooks.open_inspection(self.task)
        return await self.inspection.tools["shell"].execute({"command": args["command"]})

    async def _rescore(self, args):
        result = await self.hooks.rescore(self.task, args["prediction"])
        self.rescores.append({"prediction": args["prediction"], "passed": bool(result["passed"])})
        return result

    async def _submit(self, args):
        if self.diagnosis is not None:
            raise ValueError("diagnosis already submitted")
        try:
            diagnosis = Diagnosis.model_validate_json(json.dumps(args["diagnosis"]))
        except ValidationError as exc:
            raise ValueError(f"invalid diagnosis, fix and resubmit: {exc}") from exc
        if diagnosis.failing_turn is not None and diagnosis.failing_turn >= max(turn_count(self.events), 1):
            raise ValueError("failing_turn is outside the transcript")
        self.diagnosis, self.finished = diagnosis, True
        return {"received": True}

    async def collect(self):
        if self.diagnosis is None:
            raise ValueError("the verifier finished without submit_diagnosis")
        return {"diagnosis": self.diagnosis.model_dump(),
                "fix_validated": any(item["passed"] for item in self.rescores),
                "validated_prediction": next((i["prediction"] for i in self.rescores if i["passed"]), None),
                "rescores": len(self.rescores)}

    async def close(self):
        if self.inspection is not None:
            await self.inspection.close()
            self.inspection = None


def eligibility(outcome):
    """Only validated, skill-attributed diagnoses may change the tree."""
    if outcome is None:
        return False, "verifier did not produce a diagnosis"
    attribution = outcome["diagnosis"]["attribution"]
    if attribution not in ("skill_missing", "skill_misleading"):
        return False, f"attributed to {attribution}"
    if not outcome["fix_validated"]:
        return False, "no passing rescore validated the diagnosis"
    return True, None


async def verify_episode(*, model, task, record, hooks, workspace, max_calls=30, timeout_seconds=900,
                         feedback=None):
    """feedback: why an earlier proposal for this episode was rejected, for one retry."""
    session = VerifierSession(task=task, record=record, hooks=hooks, workspace=workspace)
    prompt = verifier_prompt(hooks.benchmark)
    if feedback:
        prompt += "\n\nPREVIOUS ATTEMPT (rejected)\n" + feedback
    result = await run_episode(model=model, session=session, skill_context=None,
                               system_prompt=prompt, policy=NoSkillPolicy(),
                               max_calls=max_calls, timeout_seconds=timeout_seconds)
    eligible, reason = eligibility(result.prediction)
    return {"task_id": record["task_id"], "condition": record["condition"], "replicate": record["replicate"],
            "status": result.status, "error": result.error, **(result.prediction or {}),
            "eligible": eligible, "ineligible_reason": reason, "events": result.events,
            "seconds": result.seconds}
