"""Agentic error analysis of one failed episode, run through skill_agent's own tool loop."""

import json
from importlib.resources import files

from pydantic import ValidationError

from skill_agent.runtime import NoSkillPolicy, run_episode

from rdr_engine.learned import FEATURE_PREFIX
from rdr_engine.skill_cases import SkillConditionEvaluator

from .proposals import Diagnosis, NewFeatureCondition
from .records import skill_views, trace_steps, turn_count, view_at

# Decision I5: every tool output is bounded, so 30 calls cannot outgrow the inspector's context.
MAX_TOOL_CHARS = 12_000
TRACE_FIELD_CHARS = 2000
# Decision I7: when this little is left, the inspector is told once to submit. A 27B call takes
# about a minute and its longest ones several, so the warning leaves room for a final answer.
WARN_CALLS_LEFT = 3
WARN_SECONDS_LEFT = 360


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
            result = await self.run(arguments)
        except (ValueError, KeyError):
            raise
        except Exception as exc:  # Report tool failures to the verifier instead of aborting.
            raise ValueError(f"{type(exc).__name__}: {exc}") from exc
        text = json.dumps(result, ensure_ascii=False, default=str)
        if len(text) <= MAX_TOOL_CHARS:
            return result
        return {"output_cut": text[:MAX_TOOL_CHARS],
                "note": f"{len(text) - MAX_TOOL_CHARS} more characters were not shown. Ask for less: a "
                        "narrower turn range, or head/grep/a page range in the shell."}


def proposal_problems(proposal, workspace, view):
    """Why a proposal cannot be inserted, found by code alone (decision I6); [] when none.

    These are the mistakes the engine would reject without ever asking the judge: a wrong
    target node, an unknown feature or operator, a fragment the node does not have.
    """
    problems, nodes, schema = [], workspace.engine.nodes, workspace.schema
    target = proposal.target_node_id
    if target >= len(nodes):
        return [f"target_node_id {target} does not exist; the tree has nodes 0 to {len(nodes) - 1}"]
    rendered, fired = (view or {}).get("terminal_ids") or [], (view or {}).get("fired_ids") or []
    if proposal.kind in ("replace", "stop") and rendered and target not in rendered:
        problems.append(f"{proposal.kind} must target a node whose guidance was rendered at the failing "
                        f"turn; those nodes are {rendered}")
    if proposal.kind == "add" and fired and target not in fired:
        problems.append(f"add must target a node that fired at the failing turn; those nodes are {fired}")
    shown = (nodes[target].conclusion or {}).get("fragments", [])
    missing = sorted(set(proposal.remove_fragment_ids) - set(shown))
    if missing:
        problems.append(f"node {target} does not render {missing}; it renders {shown}")
    anchors = {fragment.id for fragment in workspace.catalog.fragments}
    if proposal.anchor_fragment_id is not None and proposal.anchor_fragment_id not in anchors:
        problems.append(f"anchor_fragment_id {proposal.anchor_fragment_id!r} is not a fragment of the original "
                        "skill; omit it or use one of the fragment IDs shown for the target node")
    evaluator = SkillConditionEvaluator(schema)
    for condition in proposal.conditions:
        if isinstance(condition, NewFeatureCondition):
            feature = condition.new_feature
            existing = workspace.layer.features.get(feature.name)
            if feature.name in schema.features and (existing is None or (
                    existing.description, existing.scope) != (feature.description, feature.scope)):
                problems.append(f"feature {feature.name} already exists with another definition; reuse it as "
                                f'{{"field": "{feature.name}", "op": "eq", "value": "true"}} or pick a new name')
            continue
        predicate = condition.model_dump()
        spec = schema.features.get(predicate["field"])
        if spec is None:
            known = sorted(name for name in schema.features if not name.startswith(FEATURE_PREFIX))
            problems.append(f"unknown feature {predicate['field']!r}; existing features are {known} plus the "
                            "learned ones in the tree. Define anything else as a new_feature")
            continue
        try:  # The evaluator's own check, on a placeholder case.
            evaluator.evaluate(predicate, {predicate["field"]: [] if spec.multiple else spec.values[0]})
        except ValueError as exc:
            usage = ('op "contains" with one value' if spec.multiple
                     else 'op "eq" with one value, or "in" with a list')
            problems.append(f"{exc}: {predicate['field']} takes {usage}, from {spec.values}")
    return problems


class VerifierSession:
    """Read-only access to one episode plus gold-label tools; ends on submit_diagnosis."""

    def __init__(self, *, task, record, hooks, workspace, passed=False, passing_traces=None):
        self.task, self.record, self.hooks, self.workspace = task, record, hooks, workspace
        self.events = record.get("events", [])
        self.rescores = []
        self.diagnosis = None
        self.finished = False
        self.warned = False
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
        if passing_traces is not None:
            async def passing_examples(args):
                return {"examples": passing_traces.examples(args["node_id"], task["id"])}
            tools.append(_Tool(
                "passing_examples", "Read up to six verified passing traces for the given node.",
                {"node_id": {"type": "integer", "minimum": 0}}, ["node_id"], passing_examples))
        self.tools = {tool.definition["function"]["name"]: tool for tool in tools}

    async def _read_trace(self, args):
        start, end = int(args["from_turn"]), int(args["to_turn"])
        if end < start or end - start > 9:
            raise ValueError("read at most 10 turns at a time, with to_turn >= from_turn")
        return {"steps": trace_steps(self.events, start, end, limit=TRACE_FIELD_CHARS)}

    async def _skill_views(self, args):
        """One entry per view; fragment IDs are listed only when they differ from the turn before."""
        views, last = [], None
        for view in skill_views(self.events):
            item = {"turn": view["turn"], "context": view["context"][-600:], "case": view["case"],
                    "terminal_ids": view["terminal_ids"], "fragment_ids": view["fragment_ids"]}
            if last is not None and view["fragment_ids"] == last["fragment_ids"]:
                item["fragment_ids"] = f"same as turn {last['turn']}"
            else:
                last = view
            views.append(item)
        return {"views": views}

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

    def progress_note(self, calls_left, seconds_left):
        """One warning when the inspection is about to run out of calls or time (decision I7)."""
        if self.warned or (calls_left > WARN_CALLS_LEFT and seconds_left > WARN_SECONDS_LEFT):
            return None
        self.warned = True
        return (f"BUDGET WARNING: about {calls_left} model call(s) and {max(0, int(seconds_left // 60))} "
                "minute(s) remain; after that this inspection is lost. Stop investigating. If a rescore "
                "of yours already passed, call submit_diagnosis now. Otherwise make at most one more "
                "rescore, then call submit_diagnosis with the best-supported attribution (not_skill or "
                "unexplained when you could not establish a skill cause).")

    async def briefing(self):
        """What every inspection starts by asking for, computed up front so no model call is spent on it."""
        async def comparison(args):
            result = await self._compare(args)
            summarise = getattr(self.hooks, "failure_summary", None)
            return summarise(result) if summarise else result

        parts = []
        for title, tool, limit in (
                ("compare_with_ground_truth (the agent's prediction, officially scored)", comparison, 4000),
                ("ground_truth", self._ground_truth, 4000),
                ("skill_views (guidance shown at each turn)", self._skill_views, 6000),
                ("show_tree", self._show_tree, 4000)):
            try:
                parts.append(f"{title}:\n{_clip(await tool({}), limit)}")
            except Exception:  # The tool is still there to call; its error shows up then.
                continue
        return "\n\n".join(parts)

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
        if diagnosis.proposal is not None:
            problems = proposal_problems(diagnosis.proposal, self.workspace,
                                         view_at(self.events, diagnosis.failing_turn))
            if problems:
                raise ValueError("invalid proposal, fix and resubmit: " + "; ".join(problems))
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
                         feedback=None, passing_traces=None):
    """feedback: why an earlier proposal for this episode was rejected, for one retry."""
    session = VerifierSession(task=task, record=record, hooks=hooks, workspace=workspace,
                              passing_traces=passing_traces)
    prompt = verifier_prompt(hooks.benchmark)
    if passing_traces is not None:
        prompt += ("\nBefore proposing a skill change, call passing_examples for the target node. "
                   "Use the successful traces to narrow the condition to the distinguishing case. "
                   "If the tool returns no examples, proceed with the failed trace.")
    if feedback:
        prompt += "\n\nPREVIOUS ATTEMPT (rejected)\n" + feedback
    briefing = await session.briefing()
    if briefing:
        session.prompt += "\n\nALREADY RUN FOR YOU (results of the first tool calls)\n" + briefing
    result = await run_episode(model=model, session=session, skill_context=None,
                               system_prompt=prompt, policy=NoSkillPolicy(),
                               max_calls=max_calls, timeout_seconds=timeout_seconds)
    eligible, reason = eligibility(result.prediction)
    return {"task_id": record["task_id"], "condition": record["condition"], "replicate": record["replicate"],
            "status": result.status, "error": result.error, **(result.prediction or {}),
            "eligible": eligible, "ineligible_reason": reason, "events": result.events,
            "seconds": result.seconds}
