"""Agentic error analysis of one failed episode, run through skill_agent's own tool loop."""

import json
from importlib.resources import files

from pydantic import ValidationError

from skill_agent.learned_features import document_context, judge_context
from skill_agent.runtime import NoSkillPolicy, run_episode

from rdr_engine.learned import FEATURE_PREFIX
from rdr_engine.skill_cases import SkillConditionEvaluator

from .proposals import Diagnosis, NewFeature, NewFeatureCondition
from .records import skill_views, trace_steps, turn_count, view_at

# Decision I5: every tool output is bounded, so 30 calls cannot outgrow the inspector's context.
MAX_TOOL_CHARS = 12_000
TRACE_FIELD_CHARS = 2000
# Decision I12: the first message also carries the base skill and the root's past successes.
BRIEFING_SKILL_CHARS = 12_000
BRIEFING_EXAMPLES_CHARS = 8000
MAX_CONDITION_TESTS = 5  # Decision I11: judge tests of draft conditions per inspection.
MAX_GUIDANCE_CHARS = 600  # Decision I14: a rule's advice is a few short instructions.
# Decision I13: a retry continues the first inspection only while its conversation leaves room in the
# 27B's window (98,304 tokens since 17 Nov, G1) for the feedback and a few more turns; otherwise it
# starts afresh.
RESUME_MAX_INPUT_TOKENS = 60_000
RESUME_MAX_CALLS = 12
# Decision I7: when this little is left, the inspector is told once to submit. A 27B call takes
# about a minute and its longest ones several, so the warning leaves room for a final answer.
WARN_CALLS_LEFT = 3
WARN_SECONDS_LEFT = 360
# Decision I7 (17 Nov): also when its conversation nears the 27B's 98,304-token window: one full output
# (8,192 tokens) and about one more turn (10,000) must still fit after the warning.
WARN_CONTEXT_TOKENS = 80_000
MAX_NUDGES = 2  # Decision I8: answers without a tool call that are asked again before giving up.


def verifier_prompt(benchmark):
    return files("trace_learner").joinpath("prompts/verifier.md").read_text(encoding="utf-8").replace(
        "{benchmark}", benchmark)


def _clip(value, limit=12_000):
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + f"... [{len(text) - limit} more characters]"


class _Tool:
    def __init__(self, name, description, properties, required, run):
        # Decision I15: strict, so a serving engine that enforces tool schemas (vLLM with xgrammar) makes
        # every call's arguments valid JSON of this shape; the inspector cannot unbalance brackets or send
        # an object as a string. Providers that ignore the flag behave as before.
        self.definition = {"type": "function", "function": {
            "name": name, "description": description, "strict": True,
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


LEARNED_ONLY_INSTRUCTIONS = (
    "\nThis tree started as the whole skill at a lone root, so it has no predefined features. Write "
    "every condition as a learned feature: reuse one that show_tree lists, as "
    '{"field": "lf_<name>", "op": "eq", "value": "true"}, or define a new_feature. If the kind of '
    "task matters to your rule, say so in a new_feature's definition.")


def proposal_problems(proposal, workspace, view, *, learned_only=False):
    """Why a proposal cannot be inserted, found by code alone (decision I6); [] when none.

    These are the mistakes the engine would reject without ever asking the judge: a wrong
    target node, an unknown feature or operator, a fragment the node does not have.
    learned_only (decision T5): the original skill's features may not be used either.
    """
    problems, nodes, schema = [], workspace.engine.nodes, workspace.schema
    target = proposal.target_node_id
    guidance = (proposal.guidance_text or "").strip()
    if len(guidance) > MAX_GUIDANCE_CHARS:  # Decision I14: light advice, checked here so saved proposals still load.
        problems.append(f"guidance_text has {len(guidance)} characters; keep it to {MAX_GUIDANCE_CHARS} or fewer: "
                        "at most three short, checkable instructions")
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
        if learned_only and not predicate["field"].startswith(FEATURE_PREFIX):
            learned = sorted(name for name in schema.features if name.startswith(FEATURE_PREFIX))
            problems.append(f"feature {predicate['field']!r} cannot be used on this tree; only learned features "
                            f"can (existing ones: {learned}). Define what you need as a new_feature")
            continue
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

    def __init__(self, *, task, record, hooks, workspace, passed=False, passing_traces=None,
                 learned_only=False, judge=None, materials=None, rescores=None):
        self.task, self.record, self.hooks, self.workspace = task, record, hooks, workspace
        # Decision T5: on a root-only tree the inspector neither sees nor uses the original features.
        self.learned_only = learned_only
        self.passing_traces = passing_traces
        self.events = record.get("events", [])
        self.rescores = list(rescores or [])  # A continued retry keeps its first passing rescore (I13).
        # Decision I10: exactly what the judge reads for this document's conditions (the starting
        # materials, as at run time); None when the benchmark gives the judge no materials.
        self.judge_text = document_context(judge_context(task["task"], materials)) if materials else None
        self.judge = judge
        self.tests = 0
        self.diagnosis = None
        self.finished = False
        self.warned = False
        self.nudges = 0
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
        if judge is not None and self.judge_text is not None:
            feature = {"type": "object", "properties": {
                "name": {"type": "string"}, "description": {"type": "string"},
                "examples": {"type": "array", "items": {"type": "string"}},
                "counterexamples": {"type": "array", "items": {"type": "string"}}},
                "required": ["name", "description"]}
            tools.append(_Tool(
                "test_conditions",
                f"Ask the judge, as it will be asked when your rule is checked, to answer draft new_feature "
                f"conditions on THIS failing document's starting materials. Returns each verdict "
                f"(true/false/unknown) and a short reason from a separate call. At most "
                f"{MAX_CONDITION_TESTS} tests per inspection.",
                {"conditions": {"type": "array", "items": feature, "minItems": 1, "maxItems": 4}},
                ["conditions"], self._test_conditions))
        self.tools = {tool.definition["function"]["name"]: tool for tool in tools}

    async def _test_conditions(self, args):
        """Decision I11: the same judge call that decides the rule, before the inspector submits."""
        if self.tests >= MAX_CONDITION_TESTS:
            raise ValueError(f"test limit reached ({MAX_CONDITION_TESTS}); submit your diagnosis")
        conditions = args.get("conditions")
        # The 27B often sends the list as JSON text (as with submit_diagnosis, I6), or one bare object.
        if isinstance(conditions, str):
            try:
                conditions = json.loads(conditions)
            except ValueError:
                pass
        if isinstance(conditions, dict):
            conditions = [conditions]
        if not isinstance(conditions, list) or not conditions:
            raise ValueError("conditions must be a nonempty JSON array of new_feature definitions "
                             '(objects with name and description), for example [{"name": "lf_x", ...}]')
        features = {}
        for raw in conditions:
            try:
                feature = NewFeature.model_validate(raw)
            except ValidationError as exc:
                raise ValueError(f"invalid condition, fix and retry: {exc}") from exc
            features[feature.name] = {"description": feature.description, "examples": feature.examples,
                                      "counterexamples": feature.counterexamples}
        self.tests += 1
        values, response = await self.judge.judge(features, self.judge_text)
        unusable = (getattr(response, "metadata", None) or {}).get("judge_unusable")
        if unusable:  # Decision J6: counted as unknown; the inspector should simplify the condition.
            return {"verdicts": values, "judge_unusable": unusable, "tests_left": MAX_CONDITION_TESTS - self.tests,
                    "note": "The judge could not give a usable answer (for example it ran out of tokens while "
                            "thinking), so every condition counts as unknown and the rule would be rejected. "
                            "Make the condition simpler and plainly visible in the starting materials."}
        try:
            reasons = await self.judge.explain(features, self.judge_text)
        except Exception as exc:  # The verdicts stand without the reasons.
            reasons = f"(no reasons: {type(exc).__name__})"
        return {"verdicts": values, "reasons": reasons, "tests_left": MAX_CONDITION_TESTS - self.tests,
                "note": "The verdicts are what count: a rule is rejected unless each new condition is true "
                        "here. The judge is greedy: the rule check asks this same question on this same text "
                        "and almost always gets the same verdict. Reasons come from a separate call and may disagree."}

    async def _read_trace(self, args):
        start, end = int(args["from_turn"]), int(args["to_turn"])
        if end < start or end - start > 9:
            raise ValueError("read at most 10 turns at a time, with to_turn >= from_turn")
        return {"steps": trace_steps(self.events, start, end, limit=TRACE_FIELD_CHARS)}

    async def _skill_views(self, args):
        """One entry per view; fragment IDs are listed only when they differ from the turn before."""
        views, last = [], None
        for view in skill_views(self.events):
            item = {"turn": view["turn"], "context": view["context"][-600:], "case": self._visible(view["case"]),
                    "terminal_ids": view["terminal_ids"], "fragment_ids": view["fragment_ids"]}
            if last is not None and view["fragment_ids"] == last["fragment_ids"]:
                item["fragment_ids"] = f"same as turn {last['turn']}"
            else:
                last = view
            views.append(item)
        return {"views": views}

    def _visible(self, by_feature):
        """A mapping keyed by feature name, without the original features when they are hidden."""
        if not self.learned_only:
            return by_feature
        return {name: value for name, value in by_feature.items() if name.startswith(FEATURE_PREFIX)}

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
        return {"nodes": nodes, "features": self._visible(features)}

    async def _ground_truth(self, args):
        return {"ground_truth": self.hooks.ground_truth(self.task)}

    async def _compare(self, args):
        prediction = self.record.get("prediction")
        if not isinstance(prediction, dict):
            return {"passed": False, "note": f"no prediction was collected (status {self.record['status']})"}
        return await self.hooks.rescore(self.task, prediction)

    def progress_note(self, calls_left, seconds_left, context_tokens=0):
        """One warning when the inspection is about to run out of calls, time or context (decision I7).

        context_tokens: the last request's input plus output tokens, i.e. the conversation's size.
        """
        if self.warned or (calls_left > WARN_CALLS_LEFT and seconds_left > WARN_SECONDS_LEFT
                           and context_tokens < WARN_CONTEXT_TOKENS):
            return None
        self.warned = True
        return (f"BUDGET WARNING: about {calls_left} model call(s), {max(0, int(seconds_left // 60))} "
                f"minute(s) and room for about {max(0, 98_304 - context_tokens):,} tokens of conversation "
                "remain; after that this inspection is lost. Stop investigating. If a rescore "
                "of yours already passed, call submit_diagnosis now. Otherwise make at most one more "
                "rescore, then call submit_diagnosis with the best-supported attribution (not_skill or "
                "unexplained when you could not establish a skill cause).")

    async def customer_response(self, message):
        """The inspector answered without calling a tool, usually cut off mid-thought at its output
        limit: ask it to act, twice at most, instead of ending the inspection (decision I8)."""
        if self.diagnosis is not None or self.nudges >= MAX_NUDGES:
            return None
        self.nudges += 1
        return ("Your last message called no tool (it may have been cut off at the output limit), so "
                "nothing was recorded. Do not reason at length. Call one tool now: submit_diagnosis if "
                "a rescore of yours passed or you can attribute the failure, otherwise the single "
                "next check you need.")

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
        if self.judge_text is not None:  # Decision I10: plain text, so it reads exactly as the judge sees it.
            parts.append("starting_materials (EXACTLY the text the judge reads when it answers your conditions "
                         "for this document; write conditions that are visibly true in it):\n" + self.judge_text)
        skill = "\n".join(fragment.text.rstrip("\n") for fragment in self.workspace.catalog.fragments)
        if skill:  # Decision I12: the base skill, so show_fragment is needed only for learned advice.
            parts.append("base_skill (the original skill text, all fragments in order):\n"
                         + (skill if len(skill) <= BRIEFING_SKILL_CHARS else skill[:BRIEFING_SKILL_CHARS]
                            + f"\n... [{len(skill) - BRIEFING_SKILL_CHARS} more characters; use show_fragment]"))
        if self.passing_traces is not None and self.learned_only:
            # Decision I12: on a root-only tree rules hang under the root, so its successes come up front.
            examples = self.passing_traces.examples(0, self.task["id"])
            parts.append(f"passing_examples for node 0 (the root):\n{_clip({'examples': examples}, BRIEFING_EXAMPLES_CHARS)}")
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
        submitted = args["diagnosis"]
        # A long diagnosis sometimes arrives as JSON text instead of an object; it is the same answer.
        encoded = submitted if isinstance(submitted, str) else json.dumps(submitted)
        try:
            diagnosis = Diagnosis.model_validate_json(encoded)
        except ValidationError as exc:
            raise ValueError(f"invalid diagnosis, fix and resubmit: {exc}") from exc
        if diagnosis.failing_turn is not None and diagnosis.failing_turn >= max(turn_count(self.events), 1):
            raise ValueError("failing_turn is outside the transcript")
        if diagnosis.proposal is not None:
            problems = proposal_problems(diagnosis.proposal, self.workspace,
                                         view_at(self.events, diagnosis.failing_turn),
                                         learned_only=self.learned_only)
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


def resumable_history(previous):
    """The first inspection's conversation, ready to continue, or None when it cannot be (I13).

    It must have ended with an accepted submit_diagnosis, and its last request must leave room
    in the inspector's context for the feedback and a few more turns.
    """
    events = (previous or {}).get("events") or []
    requests = [i for i, event in enumerate(events) if event["type"] == "model_request"]
    responses = [i for i, event in enumerate(events) if event["type"] == "model_response"]
    if not requests or not responses or previous.get("status") != "completed" or not previous.get("eligible"):
        return None
    if ((events[responses[-1]].get("usage") or {}).get("input_tokens") or 0) > RESUME_MAX_INPUT_TOKENS:
        return None
    history = list(events[requests[-1]]["messages"][1:]) + [events[responses[-1]]["message"]]
    for event in events[responses[-1] + 1:]:
        if event["type"] == "tool":
            history.append({"role": "tool", "tool_call_id": event["call"]["id"],
                            "content": json.dumps(event["result"], ensure_ascii=False)})
    return history


async def verify_episode(*, model, task, record, hooks, workspace, max_calls=30, timeout_seconds=900,
                         feedback=None, passing_traces=None, learned_only=False, judge=None, materials=None,
                         previous=None):
    """feedback: why an earlier proposal for this episode was rejected, for one retry.

    learned_only: hide the original skill's features and refuse proposals that use them (decision T5).
    judge, materials: the feature judge and the document's starting materials, for the judge's text
    up front (I10) and the condition test (I11). previous: the first inspection's outcome; a retry
    with feedback continues its conversation when it can (I13), else starts afresh.
    """
    history = resumable_history(previous) if feedback else None
    rescores = ([{"prediction": previous["validated_prediction"], "passed": True}]
                if history is not None and previous.get("validated_prediction") is not None else None)
    session = VerifierSession(task=task, record=record, hooks=hooks, workspace=workspace,
                              passing_traces=passing_traces, learned_only=learned_only,
                              judge=judge, materials=materials, rescores=rescores)
    prompt = verifier_prompt(hooks.benchmark)
    if learned_only:
        prompt += LEARNED_ONLY_INSTRUCTIONS
    if passing_traces is not None:
        prompt += ("\nBefore proposing a skill change, call passing_examples for the target node (unless "
                   "your first message already has them for that node). "
                   "Use the successful traces to narrow the condition to the distinguishing case. "
                   "If the tool returns no examples, proceed with the failed trace.")
    if history is not None:
        # Same system prompt as the first inspection, so the server reuses its cached prefix.
        history.append({"role": "user", "content": (
            "PREVIOUS ATTEMPT (rejected)\n" + feedback + "\n\nYou keep your earlier analysis and your passing "
            "rescore, which still counts: rescore again only if your corrected answer changes. Revise the "
            "rule and call submit_diagnosis once more.")})
        max_calls = min(max_calls, RESUME_MAX_CALLS)
    else:
        if feedback:
            prompt += ("\n\nPREVIOUS ATTEMPT (rejected)\n" + feedback + "\nThis is a new inspection: call rescore "
                       "again with your corrected prediction, then submit_diagnosis.")
        briefing = await session.briefing()
        if briefing:
            session.prompt += "\n\nALREADY RUN FOR YOU (results of the first tool calls)\n" + briefing
    result = await run_episode(model=model, session=session, skill_context=None,
                               system_prompt=prompt, policy=NoSkillPolicy(),
                               max_calls=max_calls, timeout_seconds=timeout_seconds, history=history)
    eligible, reason = eligibility(result.prediction)
    return {"task_id": record["task_id"], "condition": record["condition"], "replicate": record["replicate"],
            "status": result.status, "error": result.error, **(result.prediction or {}),
            "eligible": eligible, "ineligible_reason": reason, "events": result.events,
            "seconds": result.seconds, "continued": history is not None}
