import json
import tempfile
import unittest
from pathlib import Path

from skill_agent.contracts import ResolveSkillRequest, ResolvedSkill
from skill_agent.learned_features import JudgeError, LearnedFeatureResolver
from skill_agent.models import ModelResponse
from trace_learner.records import skill_views, trace_steps, view_at
from trace_learner.verifier import verify_episode
from trace_learner.workspace import SkillWorkspace, copy_bundle

ROOT = Path(__file__).resolve().parents[1]
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

    async def test_invalid_diagnosis_is_returned_for_correction(self):
        outcome = await self.verify([
            call("submit_diagnosis", {"diagnosis": {**DIAGNOSIS, "failing_turn": 9}}),
            call("submit_diagnosis", {"diagnosis": {**DIAGNOSIS, "proposal": None}}),
            call("rescore", {"prediction": {"total": "12"}}), call("submit_diagnosis", {"diagnosis": DIAGNOSIS})])
        errors = [e["result"]["error"] for e in outcome["events"] if e["type"] == "tool" and "error" in e["result"]]
        self.assertEqual(len(errors), 2)
        self.assertTrue(outcome["eligible"])

    async def test_missing_submission_fails_the_verifier_episode(self):
        outcome = await self.verify([{"role": "assistant", "content": "I think it is fine."}])
        self.assertEqual(outcome["status"], "failed")
        self.assertFalse(outcome["eligible"])

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
        assert "has tables" in messages[1]["content"]
        return ModelResponse(self.reply or call("report_features", {"lf_tables": "true"}), dict(USAGE), {"model": "j"})


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

    async def test_unusable_judgment_keeps_its_cost(self):
        resolver = self.resolver({"lf_tables": {"description": "The PDF has tables."}},
                                 JudgeModel(call("report_features", {"lf_tables": "maybe"})))
        with self.assertRaises(JudgeError) as caught:
            await resolver.resolve(ResolveSkillRequest("pdf", "x", {}, "artifact"))
        self.assertEqual(caught.exception.usage["model_calls"], 1)


if __name__ == "__main__":
    unittest.main()
