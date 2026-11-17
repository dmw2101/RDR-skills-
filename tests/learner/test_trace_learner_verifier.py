import json
import tempfile
import unittest
from pathlib import Path

from skill_agent.contracts import ResolveSkillRequest, ResolvedSkill
from skill_agent.learned_features import (MAX_JUDGE_CHARS, MAX_MATERIAL_CHARS, FeatureJudge, JudgeError,
                                          LearnedFeatureResolver, judge_context)
from skill_agent.models import ModelResponse
from trace_learner.records import skill_views, trace_steps, view_at
from trace_learner.verifier import MAX_TOOL_CHARS, verify_episode
from trace_learner.workspace import SkillWorkspace, copy_bundle

ROOT = Path(__file__).resolve().parents[2]
USAGE = {"input_tokens": 5, "output_tokens": 1, "cached_input_tokens": 0, "cost_usd": 0.001}


def call(name, arguments, index=0):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"c{index}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}


class ScriptedModel:
    def __init__(self, replies):
        self.replies, self.requests = replies, []

    async def complete(self, messages, tools):
        self.requests.append((messages, tools))
        return ModelResponse(self.replies[min(len(self.requests), len(self.replies)) - 1], dict(USAGE), {})


class Hooks:
    benchmark = "toy"

    def __init__(self):
        self.rescored = []

    def ground_truth(self, task):
        return {"total": "12"}

    async def rescore(self, task, prediction):
        self.rescored.append(prediction)
        return {"passed": prediction == {"total": "12"}, "fields": []}

    async def open_inspection(self, task):
        raise RuntimeError("no inputs in this test")


def episode(view_case=None):
    case = view_case or {"operation": ["extract_content"], "source_kind": "unknown"}
    skill = {"type": "skill", "request": {"task": "visible text"}, "markdown_sha256": "h",
             "response": {"metadata": {"case": case, "fragment_ids": ["f"], "terminal_ids": [1, 2]}}}
    response = {"type": "model_response", "message": {"content": "reading the PDF", "tool_calls": [
        {"function": {"name": "shell", "arguments": "{\"command\": \"pdftotext\"}"}}]}, "metadata": {}}
    events = [skill, {"type": "model_request"}, response, {"type": "tool", "name": "shell", "result": "x" * 5000},
              {**skill, "request": {"task": "later text"}}, {"type": "model_request"},
              {"type": "model_response", "message": {"content": "done"}, "metadata": {}}]
    return {"task_id": "doc1", "condition": "reduced", "replicate": 0, "status": "completed",
            "prediction": {"total": "13"}, "error": None, "events": events}


DIAGNOSIS = {"attribution": "skill_missing", "failing_turn": 0, "root_cause": "missed the totals table",
             "evidence": ["turn 0"], "proposal": {
                 "kind": "replace", "target_node_id": 2, "guidance_text": "Check every table.",
                 "conditions": [{"new_feature": {"name": "lf_tables", "description": "The PDF has tables."}}],
                 "rationale": "general"}}


class VerifierTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = SkillWorkspace(copy_bundle(ROOT / "skills/pdf", Path(self.temporary.name) / "pdf"))
        self.task = {"id": "doc1", "task": "Extract the fields"}

    async def verify(self, replies):
        return await verify_episode(model=ScriptedModel(replies), task=self.task, record=episode(),
                                    hooks=Hooks(), workspace=self.workspace, max_calls=10)

    async def test_validated_skill_diagnosis_is_eligible(self):
        outcome = await self.verify([
            call("compare_with_ground_truth", {}), call("skill_views", {}), call("read_trace", {"from_turn": 0, "to_turn": 1}),
            call("show_tree", {}), call("inspect_shell", {"command": "ls"}),
            call("rescore", {"prediction": {"total": "12"}}), call("submit_diagnosis", {"diagnosis": DIAGNOSIS})])
        self.assertEqual(outcome["status"], "completed", outcome["error"])
        self.assertTrue(outcome["fix_validated"])
        self.assertTrue(outcome["eligible"])
        self.assertEqual(outcome["validated_prediction"], {"total": "12"})
        results = [e["result"] for e in outcome["events"] if e["type"] == "tool"]
        self.assertEqual(results[1]["views"][1]["turn"], 1)
        self.assertIn("RuntimeError", results[4]["error"])  # Tool failures become observations.

    async def test_unvalidated_or_non_skill_diagnoses_are_not_eligible(self):
        outcome = await self.verify([call("rescore", {"prediction": {"total": "14"}}),
                                     call("submit_diagnosis", {"diagnosis": DIAGNOSIS})])
        self.assertFalse(outcome["eligible"])
        self.assertIn("no passing rescore", outcome["ineligible_reason"])
        outcome = await self.verify([call("submit_diagnosis", {"diagnosis": {
            "attribution": "not_skill", "root_cause": "OCR noise"}})])
        self.assertFalse(outcome["eligible"])
        self.assertEqual(outcome["ineligible_reason"], "attributed to not_skill")
        # A wrong answer key ends the inspection with no rule, even after a passing rescore.
        outcome = await self.verify([call("rescore", {"prediction": {"total": "12"}}),
                                     call("submit_diagnosis", {"diagnosis": {
                                         "attribution": "answer_key_error", "root_cause": "gold value is cut off"}})])
        self.assertEqual(outcome["status"], "completed")
        self.assertFalse(outcome["eligible"])
        self.assertEqual(outcome["ineligible_reason"], "attributed to answer_key_error")
        outcome = await self.verify([call("submit_diagnosis", {"diagnosis": {**DIAGNOSIS, "attribution": "answer_key_error"}}),
                                     call("submit_diagnosis", {"diagnosis": {
                                         "attribution": "answer_key_error", "root_cause": "gold value is cut off"}})])
        errors = [e["result"]["error"] for e in outcome["events"] if e["type"] == "tool" and "error" in e["result"]]
        self.assertIn("only skill-attributed diagnoses may propose a change", errors[0])

    async def test_invalid_diagnosis_is_returned_for_correction(self):
        outcome = await self.verify([
            call("submit_diagnosis", {"diagnosis": {**DIAGNOSIS, "failing_turn": 9}}),
            call("submit_diagnosis", {"diagnosis": {**DIAGNOSIS, "proposal": None}}),
            call("rescore", {"prediction": {"total": "12"}}), call("submit_diagnosis", {"diagnosis": DIAGNOSIS})])
        errors = [e["result"]["error"] for e in outcome["events"] if e["type"] == "tool" and "error" in e["result"]]
        self.assertEqual(len(errors), 2)
        self.assertTrue(outcome["eligible"])

    async def test_a_diagnosis_sent_as_json_text_is_accepted(self):
        # The 27B sometimes encodes a long diagnosis as a string; refusing it made it submit filler.
        outcome = await self.verify([call("rescore", {"prediction": {"total": "12"}}),
                                     call("submit_diagnosis", {"diagnosis": json.dumps(DIAGNOSIS)})])
        self.assertTrue(outcome["eligible"])
        self.assertEqual(outcome["diagnosis"]["root_cause"], DIAGNOSIS["root_cause"])
        outcome = await self.verify([call("submit_diagnosis", {"diagnosis": "not json"}),
                                     call("submit_diagnosis", {"diagnosis": {"attribution": "not_skill", "root_cause": "x"}})])
        errors = [e["result"]["error"] for e in outcome["events"] if e["type"] == "tool" and "error" in e["result"]]
        self.assertIn("invalid diagnosis, fix and resubmit", errors[0])

    async def test_first_prompt_carries_the_standard_results(self):
        model = ScriptedModel([call("rescore", {"prediction": {"total": "12"}}),
                               call("submit_diagnosis", {"diagnosis": DIAGNOSIS})])
        outcome = await verify_episode(model=model, task=self.task, record=episode(), hooks=Hooks(),
                                       workspace=self.workspace, max_calls=10)
        self.assertTrue(outcome["eligible"])
        first = model.requests[0][0][1]["content"]
        self.assertIn("ALREADY RUN FOR YOU", first)
        for title in ("compare_with_ground_truth", "ground_truth:\n{\"ground_truth\": {\"total\": \"12\"}}",
                      "skill_views", "show_tree"):
            self.assertIn(title, first)
        self.assertIn('"fragment_ids": "same as turn 0"', first)  # Unchanged guidance is not listed twice.

    async def test_first_prompt_carries_the_judge_text_and_the_base_skill(self):
        # Decisions I10 and I12: the judge's exact text and the original skill, up front.
        materials = "Task: x\n\nFilled PDF form fields:\nName: Pope\nPDF text layer:\nTotals table"
        model = ScriptedModel([call("submit_diagnosis", {"diagnosis": {"attribution": "not_skill", "root_cause": "x"}})])
        await verify_episode(model=model, task=self.task, record=episode(), hooks=Hooks(),
                             workspace=self.workspace, max_calls=10, materials=materials)
        first = model.requests[0][0][1]["content"]
        judged = first.split("starting_materials (EXACTLY")[1]
        self.assertIn("<starting_materials>\n" + materials + "\n</starting_materials>", judged)
        self.assertIn("base_skill (the original skill text", first)
        self.assertIn(self.workspace.catalog.fragments[1].text.strip()[:40], first)
        tools = [t["function"]["name"] for t in model.requests[0][1]]
        self.assertNotIn("test_conditions", tools)  # No judge given: no condition test.

    async def test_draft_conditions_are_tested_on_the_judge(self):
        # Decision I11: the inspector asks the judge before submitting, at most five times.
        class Judge:
            def __init__(self):
                self.texts = []

            async def judge(self, features, text):
                self.texts.append(text)
                return {name: "true" if "Pope" in text and "form" in spec["description"] else "false"
                        for name, spec in features.items()}, None

            async def explain(self, features, text):
                return "lf_forms: true - 'Name: Pope' is a filled field."
        judge = Judge()
        draft = {"conditions": [{"name": "lf_forms", "description": "The PDF has filled form fields."},
                                {"name": "lf_tables", "description": "The PDF has a totals table."}]}
        replies = [call("test_conditions", draft, index) for index in range(6)]
        # The list sent as JSON text, and a single bare object, are read as the same request.
        replies[2] = call("test_conditions", {"conditions": json.dumps(draft["conditions"])}, 2)
        replies[3] = call("test_conditions", {"conditions": draft["conditions"][0]}, 3)
        # An invalid draft is returned for correction and does not use up a test.
        replies.insert(1, call("test_conditions", {"conditions": [{"name": "bad name", "description": "x"}]}, 6))
        replies.append(call("submit_diagnosis", {"diagnosis": {"attribution": "not_skill", "root_cause": "x"}}, 7))
        materials = "Filled PDF form fields:\nName: Pope"
        outcome = await verify_episode(model=ScriptedModel(replies), task=self.task, record=episode(), hooks=Hooks(),
                                       workspace=self.workspace, max_calls=10, materials=materials, judge=judge)
        results = [e["result"] for e in outcome["events"] if e["type"] == "tool"]
        self.assertEqual(results[0]["verdicts"], {"lf_forms": "true", "lf_tables": "false"})
        self.assertEqual(results[3]["verdicts"], {"lf_forms": "true", "lf_tables": "false"})  # JSON text.
        self.assertEqual(results[4]["verdicts"], {"lf_forms": "true"})  # One bare object.
        self.assertIn("Name: Pope", results[0]["reasons"])
        self.assertIn("invalid condition", results[1]["error"])
        self.assertEqual(results[5]["tests_left"], 0)
        self.assertIn("test limit reached", results[6]["error"])
        self.assertEqual(len(judge.texts), 5)
        self.assertTrue(all(text.startswith("<starting_materials>\n") and text.endswith("</starting_materials>")
                            for text in judge.texts))  # The same text a document condition is judged on.

    async def test_inspector_tools_ask_for_schema_constrained_arguments(self):
        # Decision I15: every inspector tool is strict, so vLLM constrains its arguments to the schema.
        model = ScriptedModel([call("submit_diagnosis", {"diagnosis": {"attribution": "not_skill", "root_cause": "x"}})])
        await verify_episode(model=model, task=self.task, record=episode(), hooks=Hooks(), workspace=self.workspace,
                             max_calls=10, materials="x", judge=object())
        definitions = model.requests[0][1]
        self.assertIn("test_conditions", [d["function"]["name"] for d in definitions])
        self.assertTrue(all(d["function"]["strict"] is True for d in definitions))
        self.assertTrue(all(d["function"]["parameters"]["additionalProperties"] is False for d in definitions))

    async def test_long_advice_is_returned_for_shortening(self):
        # Decision I14: advice is at most 600 characters, checked when the inspector submits.
        long = {**DIAGNOSIS, "proposal": {**DIAGNOSIS["proposal"], "guidance_text": "Check every table. " * 40}}
        outcome = await self.verify([call("rescore", {"prediction": {"total": "12"}}),
                                     call("submit_diagnosis", {"diagnosis": long}, 1),
                                     call("submit_diagnosis", {"diagnosis": DIAGNOSIS}, 2)])
        errors = [e["result"]["error"] for e in outcome["events"] if e["type"] == "tool" and "error" in e["result"]]
        self.assertIn("keep it to 600 or fewer", errors[0])
        self.assertTrue(outcome["eligible"])

    async def test_a_retry_continues_the_first_inspection_when_it_fits(self):
        # Decision I13: same conversation plus the feedback; the passing rescore still counts.
        first = await self.verify([call("rescore", {"prediction": {"total": "12"}}),
                                   call("submit_diagnosis", {"diagnosis": DIAGNOSIS}, 1)])
        model = ScriptedModel([call("submit_diagnosis", {"diagnosis": DIAGNOSIS}, 2)])
        retry = await verify_episode(model=model, task=self.task, record=episode(), hooks=Hooks(),
                                     workspace=self.workspace, max_calls=30, feedback="too broad", previous=first)
        self.assertTrue(retry["continued"])
        self.assertTrue(retry["eligible"])  # Without a new rescore.
        messages = model.requests[0][0]
        self.assertNotIn("PREVIOUS ATTEMPT", messages[0]["content"])  # The system prompt is unchanged.
        self.assertIn("ALREADY RUN FOR YOU", messages[1]["content"])
        self.assertEqual([m["role"] for m in messages[-3:]], ["assistant", "tool", "user"])
        self.assertIn("PREVIOUS ATTEMPT (rejected)\ntoo broad", messages[-1]["content"])
        # Too long a first conversation, or none, starts a fresh inspection with the feedback in its prompt.
        for event in first["events"]:
            if event["type"] == "model_response":
                event["usage"] = {**event["usage"], "input_tokens": 70_000}  # Over RESUME_MAX_INPUT_TOKENS.
        for previous in (first, None):
            model = ScriptedModel([call("rescore", {"prediction": {"total": "12"}}),
                                   call("submit_diagnosis", {"diagnosis": DIAGNOSIS}, 1)])
            retry = await verify_episode(model=model, task=self.task, record=episode(), hooks=Hooks(),
                                         workspace=self.workspace, max_calls=30, feedback="too broad",
                                         previous=previous)
            self.assertFalse(retry["continued"])
            self.assertIn("PREVIOUS ATTEMPT (rejected)\ntoo broad", model.requests[0][0][0]["content"])

    async def test_long_tool_outputs_are_cut_with_a_note(self):
        record = episode()
        record["events"][3]["result"] = "x" * 50_000
        record["events"][2]["message"]["reasoning"] = "r" * 50_000
        for _ in range(8):  # Ten long turns in one read_trace call.
            record["events"] += record["events"][1:4]
        model = ScriptedModel([call("read_trace", {"from_turn": 0, "to_turn": 9}),
                               call("submit_diagnosis", {"diagnosis": {"attribution": "not_skill", "root_cause": "x"}})])
        outcome = await verify_episode(model=model, task=self.task, record=record, hooks=Hooks(),
                                       workspace=self.workspace, max_calls=10)
        result = next(e["result"] for e in outcome["events"] if e["type"] == "tool")
        self.assertEqual(len(result["output_cut"]), MAX_TOOL_CHARS)
        self.assertIn("more characters were not shown", result["note"])

    async def test_a_proposal_the_engine_would_refuse_is_returned_for_correction(self):
        def with_proposal(**changes):
            return {**DIAGNOSIS, "proposal": {**DIAGNOSIS["proposal"], **changes}}

        bad = [with_proposal(conditions=[{"field": "operation", "op": "in", "value": ["extract_content"]}]),
               with_proposal(conditions=[{"field": "page_count", "op": "eq", "value": "1"}]),
               with_proposal(target_node_id=99),
               with_proposal(target_node_id=3),  # Guidance of node 3 was not rendered at the failing turn.
               with_proposal(remove_fragment_ids=["no_such_fragment"]),
               with_proposal(anchor_fragment_id="no_such_fragment")]
        outcome = await self.verify([*(call("submit_diagnosis", {"diagnosis": d}) for d in bad),
                                     call("rescore", {"prediction": {"total": "12"}}),
                                     call("submit_diagnosis", {"diagnosis": with_proposal(conditions=[
                                         {"field": "operation", "op": "contains", "value": "extract_content"}])})])
        errors = [e["result"]["error"] for e in outcome["events"] if e["type"] == "tool" and "error" in e["result"]]
        self.assertEqual(len(errors), len(bad))
        self.assertTrue(all(error.startswith("invalid proposal, fix and resubmit") for error in errors))
        self.assertIn("use contains for a multiple-value feature", errors[0])
        self.assertIn("unknown feature 'page_count'", errors[1])
        self.assertIn("does not exist", errors[2])
        self.assertIn("rendered at the failing turn; those nodes are [1, 2]", errors[3])
        self.assertTrue(outcome["eligible"])  # The corrected proposal goes through in the same session.

    async def test_a_root_only_tree_hides_and_refuses_the_original_features(self):
        # Decision T5: every condition of a rule on a root-only tree is a learned, judged one.
        base = {**DIAGNOSIS, "proposal": {**DIAGNOSIS["proposal"], "conditions": [
            {"field": "operation", "op": "contains", "value": "extract_content"},
            DIAGNOSIS["proposal"]["conditions"][0]]}}
        model = ScriptedModel([call("show_tree", {}), call("skill_views", {}),
                               call("submit_diagnosis", {"diagnosis": base}),
                               call("rescore", {"prediction": {"total": "12"}}),
                               call("submit_diagnosis", {"diagnosis": DIAGNOSIS})])
        outcome = await verify_episode(model=model, task=self.task, record=episode(), hooks=Hooks(),
                                       workspace=self.workspace, max_calls=10, learned_only=True)
        system, first = model.requests[0][0][0]["content"], model.requests[0][0][1]["content"]
        self.assertIn("no predefined features", system)
        self.assertIn('"features": {}', first)  # (This test's tree is the compiled one, so its nodes still show theirs.)
        self.assertIn('"case": {}', first)
        results = [e["result"] for e in outcome["events"] if e["type"] == "tool"]
        self.assertEqual(results[0]["features"], {})
        self.assertEqual([view["case"] for view in results[1]["views"]], [{}, {}])
        self.assertIn("feature 'operation' cannot be used on this tree", results[2]["error"])
        self.assertTrue(outcome["eligible"])
        # Without the switch the same proposal is accepted and the features are shown.
        outcome = await self.verify([call("show_tree", {}), call("rescore", {"prediction": {"total": "12"}}),
                                     call("submit_diagnosis", {"diagnosis": base})])
        self.assertIn("operation", next(e["result"] for e in outcome["events"] if e["type"] == "tool")["features"])
        self.assertTrue(outcome["eligible"])

    async def test_the_inspector_is_warned_once_before_its_budget_runs_out(self):
        model = ScriptedModel([call("read_trace", {"from_turn": 0, "to_turn": 0})] * 3
                              + [call("submit_diagnosis", {"diagnosis": {"attribution": "not_skill", "root_cause": "x"}})])
        outcome = await verify_episode(model=model, task=self.task, record=episode(), hooks=Hooks(),
                                       workspace=self.workspace, max_calls=5)
        warnings = [e for e in outcome["events"] if e["type"] == "user_message"]
        self.assertEqual([w["turn"] for w in warnings], [2])  # Three calls left, said once.
        self.assertIn("BUDGET WARNING", warnings[0]["content"])
        self.assertEqual(model.requests[2][0][-1], {"role": "user", "content": warnings[0]["content"]})
        self.assertEqual(outcome["status"], "completed")

    async def test_the_inspector_is_warned_when_its_conversation_nears_the_context_window(self):
        # Decision I7 (17 Nov): the last request's input plus output passes WARN_CONTEXT_TOKENS.
        from trace_learner.verifier import WARN_CONTEXT_TOKENS

        class Growing(ScriptedModel):
            async def complete(self, messages, tools):
                response = await super().complete(messages, tools)
                response.usage["input_tokens"] = WARN_CONTEXT_TOKENS * len(self.requests) // 2
                return response
        model = Growing([call("read_trace", {"from_turn": 0, "to_turn": 0})] * 3
                        + [call("submit_diagnosis", {"diagnosis": {"attribution": "not_skill", "root_cause": "x"}})])
        outcome = await verify_episode(model=model, task=self.task, record=episode(), hooks=Hooks(),
                                       workspace=self.workspace, max_calls=30)
        warnings = [e for e in outcome["events"] if e["type"] == "user_message"]
        self.assertEqual([w["turn"] for w in warnings], [2])  # After the second call reached the limit.
        self.assertIn("tokens of conversation remain", warnings[0]["content"])

    async def test_missing_submission_fails_the_verifier_episode(self):
        outcome = await self.verify([{"role": "assistant", "content": "I think it is fine."}])
        self.assertEqual(outcome["status"], "failed")
        self.assertFalse(outcome["eligible"])
        # It was asked to act twice before the inspection was given up.
        self.assertEqual(sum(e["type"] == "user_message" for e in outcome["events"]), 2)

    async def test_an_answer_without_a_tool_call_is_asked_again(self):
        outcome = await self.verify([{"role": "assistant", "content": None, "reasoning": "thinking " * 4000},
                                     call("rescore", {"prediction": {"total": "12"}}),
                                     call("submit_diagnosis", {"diagnosis": DIAGNOSIS})])
        self.assertEqual(outcome["status"], "completed", outcome["error"])
        self.assertTrue(outcome["eligible"])
        nudge = next(e for e in outcome["events"] if e["type"] == "user_message")
        self.assertIn("called no tool", nudge["content"])

    def test_views_follow_rolling_and_model_initiated_turns(self):
        events = episode()["events"]
        self.assertEqual([view["turn"] for view in skill_views(events)], [0, 1])
        self.assertEqual(view_at(events, 0)["context"], "visible text")
        self.assertEqual(view_at(events, 5)["context"], "later text")
        steps = trace_steps(events, 0, 0, limit=100)
        self.assertEqual([step["role"] for step in steps], ["assistant", "tool"])
        self.assertIn("more characters", steps[1]["result"])


class Inner:
    def __init__(self, learned):
        self.learned, self.requests = learned, []

    async def resolve(self, request):
        self.requests.append(request)
        metadata = {"learned_features": self.learned} if self.learned else {}
        flag = request.context.get("lf_tables")
        return ResolvedSkill(f"view lf_tables={flag}", request.artifact_id, "src", "rev", metadata)


class JudgeModel:
    def __init__(self, reply=None):
        self.reply, self.calls = reply, 0

    async def complete(self, messages, tools):
        self.calls += 1
        self.seen = messages[1]["content"]
        assert "has tables" in messages[1]["content"]
        return ModelResponse(self.reply or call("report_features", {"lf_tables": "true"}), dict(USAGE), {"model": "j"})


async def _message(message):
    return ModelResponse(message, dict(USAGE), {"finish_reason": "tool_calls"})


async def _response(finish_reason):
    return ModelResponse({"role": "assistant", "content": "thinking"}, dict(USAGE), {"finish_reason": finish_reason})


class ResolverTests(unittest.IsolatedAsyncioTestCase):
    def resolver(self, learned, judge):
        resolver = LearnedFeatureResolver(base_url="http://127.0.0.1:1", judge_model={
            "base_url": "http://127.0.0.1:1/v1", "model": "local", "api_key_env": None})
        resolver.inner, resolver.judge.model = Inner(learned), judge
        return resolver

    async def test_base_snapshots_never_call_the_judge(self):
        judge = JudgeModel()
        resolver = self.resolver({}, judge)
        for _ in range(2):
            result = await resolver.resolve(ResolveSkillRequest("pdf", "task", {}, "artifact"))
        self.assertEqual(judge.calls, 0)
        self.assertEqual(result.usage["model_calls"], 0)
        self.assertEqual(len(resolver.inner.requests), 2)

    async def test_learned_features_are_judged_and_billed(self):
        judge = JudgeModel()
        resolver = self.resolver({"lf_tables": {"description": "The PDF has tables."}}, judge)
        result = await resolver.resolve(ResolveSkillRequest("pdf", "a table appears", {}, "artifact"))
        self.assertEqual(result.markdown, "view lf_tables=true")
        self.assertEqual(result.usage, {"model_calls": 1, "input_tokens": 5, "output_tokens": 1, "cost_usd": 0.001})
        self.assertEqual(result.metadata["feature_judge"]["values"], {"lf_tables": "true"})
        await resolver.resolve(ResolveSkillRequest("pdf", "again", {}, "artifact"))
        self.assertEqual(judge.calls, 2)

    async def test_starting_materials_reach_the_judge(self):
        judge = JudgeModel()
        resolver = self.resolver({"lf_tables": {"description": "The PDF has tables."}}, judge)
        self.assertTrue(resolver.uses_materials)
        result = await resolver.resolve(ResolveSkillRequest("pdf", "worker: reading page 1", {}, "artifact"),
                                        materials="Task: extract\nPDF text layer:\nTotals table")
        self.assertIn("<starting_materials>\nTask: extract\nPDF text layer:\nTotals table\n</starting_materials>",
                      judge.seen)
        self.assertIn("<recent_activity>\nworker: reading page 1\n</recent_activity>", judge.seen)
        self.assertIsNotNone(result.metadata["feature_judge"]["materials_sha256"])

    async def test_document_scope_features_are_judged_once_and_reused(self):
        judge = JudgeModel()
        resolver = self.resolver({"lf_tables": {"description": "The PDF has tables.", "scope": "document"}}, judge)
        for step in ("worker: step 1", "worker: step 2"):
            result = await resolver.resolve(ResolveSkillRequest("pdf", step, {}, "artifact"),
                                            materials="PDF text layer:\nhas tables")
            self.assertEqual(result.markdown, "view lf_tables=true")
        self.assertEqual(judge.calls, 1)
        self.assertNotIn("recent_activity", judge.seen)  # Judged on the materials alone.
        self.assertEqual(result.usage["model_calls"], 0)
        self.assertEqual(result.metadata["feature_judge"]["reused"], ["lf_tables"])
        self.assertEqual(result.metadata["feature_judge"]["latency_seconds"], 0)
        # Another document is judged on its own materials.
        await resolver.resolve(ResolveSkillRequest("pdf", "worker: step 1", {}, "artifact"),
                               materials="PDF text layer:\nalso has tables")
        self.assertEqual(judge.calls, 2)

    def test_judge_context_keeps_the_start_of_materials_and_the_end_of_recent_text(self):
        self.assertEqual(judge_context("recent"), "recent")  # Without materials: unchanged from before.
        text = judge_context("r" * (MAX_JUDGE_CHARS + 5) + "END", "BEGIN" + "m" * MAX_MATERIAL_CHARS)
        self.assertIn("<starting_materials>\nBEGIN", text)
        self.assertIn("[... 5 more characters not shown]", text)
        self.assertTrue(text.endswith("END\n</recent_activity>"))
        self.assertLess(len(text), MAX_JUDGE_CHARS + MAX_MATERIAL_CHARS + 200)

    async def test_an_answer_split_over_several_tool_calls_is_merged(self):
        features = {name: {"description": f"{name} holds."} for name in ("lf_a", "lf_b")}

        def calls(*arguments, name="report_features"):
            return {"role": "assistant", "content": None, "tool_calls": [
                {"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(a)}}
                for i, a in enumerate(arguments)]}

        async def judged(message):
            judge = JudgeModel()
            judge.complete = lambda messages, tools: _message(message)
            return await FeatureJudge(judge).judge(features, "has tables")

        values, _ = await judged(calls({"lf_a": "true"}, {"lf_b": "false"}))  # One call per condition.
        self.assertEqual(values, {"lf_a": "true", "lf_b": "false"})
        values, _ = await judged(calls({"lf_a": "true", "lf_b": "unknown"}, {"lf_a": "true"}))  # A repeat.
        self.assertEqual(values, {"lf_a": "true", "lf_b": "unknown"})
        # Decision J6: an unusable answer is not asked again; every condition counts as unknown.
        for message, error in ((calls({"lf_a": "true"}, {"lf_a": "false", "lf_b": "true"}), "conflicting"),
                               (calls({"lf_a": "true"}, name="answer"), r"called \['answer'\]"),
                               (calls({"lf_a": "true"}), r"missing \['lf_b'\]"),
                               (calls({"lf_a": "true", "lf_b": "maybe"}), "invalid")):
            with self.subTest(error=error):
                values, response = await judged(message)
                self.assertEqual(values, {"lf_a": "unknown", "lf_b": "unknown"})
                self.assertRegex(response.metadata["judge_unusable"], error)

    async def test_cut_off_or_blocked_judge_answers_count_as_unknown_with_evidence(self):
        for reason, message in (("length", "ran out of output tokens"), ("content_filter", "content filter")):
            judge = JudgeModel()
            judge.complete = lambda messages, tools, reason=reason: _response(reason)
            resolver = self.resolver({"lf_tables": {"description": "The PDF has tables."}}, judge)
            with self.subTest(reason=reason):
                result = await resolver.resolve(ResolveSkillRequest("pdf", "x", {}, "artifact"))
                judged = result.metadata["feature_judge"]
                self.assertEqual(judged["values"], {"lf_tables": "unknown"})
                self.assertIn(message, judged["unusable"][0])
                self.assertEqual(judged["evidence"][0]["finish_reason"], reason)
                self.assertEqual(result.usage["model_calls"], 1)  # Asked once, never again.

    async def test_the_judge_is_asked_once_with_a_forced_strict_call_and_documented(self):
        # Decision J6: the call is constrained; unknown and unusable answers leave their full input and output.
        class Recorder:
            def __init__(self, answer):
                self.answer, self.seen = answer, []

            async def complete(self, messages, tools, seed=None, tool_choice=None):
                self.seen.append((tools, tool_choice))
                return ModelResponse(self.answer, dict(USAGE), {"finish_reason": "stop"})

        features = {"lf_tables": {"description": "Has tables."}, "lf_forms": {"description": "Has forms."}}
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "judge_calls.jsonl"
            model = Recorder(call("report_features", {"lf_tables": "true", "lf_forms": "unknown"}))
            values, _ = await FeatureJudge(model, log=log).judge(features, "visible text")
            self.assertEqual(values, {"lf_tables": "true", "lf_forms": "unknown"})
            [(tools, choice)] = model.seen
            self.assertEqual(choice, "required")
            self.assertTrue(tools[0]["function"]["strict"])
            model = Recorder({"role": "assistant", "content": "thinking..."})
            values, response = await FeatureJudge(model, log=log).judge(features, "visible text")
            self.assertEqual(values, {"lf_tables": "unknown", "lf_forms": "unknown"})
            self.assertEqual(len(model.seen), 1)
            lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([line["usable"] for line in lines], [True, False])
            self.assertEqual(lines[0]["values"], {"lf_tables": "true", "lf_forms": "unknown"})
            evidence = sorted((Path(temporary) / "judge_evidence").glob("*.json"))
            self.assertEqual(sorted(path.name.split("-")[-1] for path in evidence), ["unknown.json", "unusable.json"])
            record = json.loads(next(p for p in evidence if p.name.endswith("unusable.json")).read_text(encoding="utf-8"))
            self.assertEqual(record["visible_text"], "visible text")
            self.assertEqual(record["output"], {"role": "assistant", "content": "thinking..."})
            self.assertIn("lf_forms", record["conditions"])

    def test_a_judge_call_can_force_one_tool_call(self):
        from skill_agent.models import OpenAICompatibleClient
        import asyncio
        client = OpenAICompatibleClient(base_url="http://127.0.0.1:8000/v1", model="m", api_key_env=None)
        sent = []

        async def post(payload):
            sent.append(payload)
            return ({"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                     "usage": {}}, 0, {})
        client._post = post
        tool = [{"type": "function", "function": {"name": "t", "parameters": {"type": "object"}}}]
        asyncio.run(client.complete([{"role": "user", "content": "x"}], tool))
        asyncio.run(client.complete([{"role": "user", "content": "x"}], tool, tool_choice="required"))
        self.assertEqual([p["tool_choice"] for p in sent], ["auto", "required"])
        self.assertNotIn("parallel_tool_calls", sent[0])
        self.assertIs(sent[1]["parallel_tool_calls"], False)


if __name__ == "__main__":
    unittest.main()
