"""Offline incremental learning: each document runs on the tree learned so far, with fake models."""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from rdr_service.backend import RDRBackend
from rdr_service.server import make_server
from skill_agent.experiments import episode_filename
from skill_agent.http import HTTPStatusError
from skill_agent.learned_features import JudgeError, judge_context
from skill_agent.models import ModelResponse
from skill_agent.storage import read_json, write_json
from trace_learner.incremental import IncrementalLearner
from trace_learner.pipeline import load_learner_config
from trace_learner.records import Interrupted, episode_key, was_interrupted
from trace_learner.report import incremental_report
from trace_learner.workspace import SkillWorkspace

ROOT = Path(__file__).resolve().parents[2]
MARKER = "Always list AcroForm fields with pypdf get_fields() first."
USAGE = {"input_tokens": 3, "output_tokens": 1, "cached_input_tokens": 0, "cost_usd": 0.0}
TASK = "Extract the requested information from this PDF"
# Learning order. As in VAREX, the task sentence is the same; only the PDFs (materials) differ.
ORDER = ["forms_a", "names", "forms_b", "totals"]
MATERIALS = {"forms_a": "PDF text layer: an application with form fields Name and Date",
             "names": "PDF text layer: a list of names", "forms_b": "PDF text layer: a claim with form fields",
             "totals": "PDF text layer: a totals table"}


def call(name, arguments):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}


class AnswerTool:
    definition = {"type": "function", "function": {"name": "answer", "parameters": {
        "type": "object", "properties": {"saw_guidance": {"type": "boolean"}}}}}

    def __init__(self, session):
        self.session = session

    async def execute(self, arguments):
        self.session.answer = arguments
        return {"saved": True}


class Session:
    def __init__(self, task):
        self.prompt, self.answer = task["task"], None
        self.tools = {"answer": AnswerTool(self)}

    async def collect(self):
        return self.answer

    async def close(self):
        pass


class Adapter:
    snapshot_fingerprint = "toy-pdf-v1"
    system_prompt = "Extract data from the PDF."

    async def prepare_runtime(self):
        return {}

    def tasks(self, split, limit=None):
        return [{"id": name, "task": TASK} for name in ORDER][:limit]

    async def open_session(self, task):
        return Session(task)

    def judge_materials(self, task):
        return MATERIALS[task["id"]]

    def score(self, records, output_dir):
        return {"summaries": [], "document_metrics": []}


class Hooks:
    """Form documents pass only if the worker saw the learned guidance; the others always pass."""

    benchmark = "toy PDF extraction"

    def __init__(self, adapter):
        self.adapter = adapter

    def outcomes(self, records, output_dir):
        return {episode_key(r): not r["task_id"].startswith("forms")
                or bool((r["prediction"] or {}).get("saw_guidance")) for r in records}

    def ground_truth(self, task):
        return {"saw_guidance": True}

    async def rescore(self, task, prediction):
        return {"passed": prediction.get("saw_guidance") is True}

    async def open_inspection(self, task):
        return Session(task)


class AgentModel:
    """The worker: reports whether the learned guidance was in its prompt, then finishes."""

    calls, refuse_at = 0, None  # Call number (counted across instances) that the provider refuses.

    async def complete(self, messages, tools):
        AgentModel.calls += 1
        if AgentModel.calls == AgentModel.refuse_at:
            raise HTTPStatusError(429, "openrouter.ai")
        if messages[-1]["role"] == "tool":
            return ModelResponse({"role": "assistant", "content": "done"}, dict(USAGE), {})
        return ModelResponse(call("answer", {"saw_guidance": MARKER in messages[0]["content"]}), dict(USAGE), {})


class VerifierModel:
    """The inspector: one instance per diagnosed episode."""

    episodes, refuse_next = 0, False
    script = [
        call("compare_with_ground_truth", {}),
        call("rescore", {"prediction": {"saw_guidance": True}}),
        call("submit_diagnosis", {"diagnosis": {
            "attribution": "skill_missing", "failing_turn": 0, "root_cause": "form fields were never read",
            "proposal": {"kind": "replace", "target_node_id": 2, "guidance_text": MARKER,
                         "conditions": [{"new_feature": {
                             "name": "lf_form_fields", "description": "The input document has form fields."}}],
                         "rationale": "Form PDFs need field extraction."}}}),
    ]

    def __init__(self):
        VerifierModel.episodes += 1
        self.turn = 0

    async def complete(self, messages, tools):
        if VerifierModel.refuse_next:
            VerifierModel.refuse_next = False
            raise HTTPStatusError(429, "openrouter.ai")
        self.turn += 1
        return ModelResponse(self.script[min(self.turn, len(self.script)) - 1], dict(USAGE), {})


class JudgeModel:
    cut_off_next = False  # Answer once as if it ran out of output tokens.

    async def complete(self, messages, tools):
        if JudgeModel.cut_off_next:
            JudgeModel.cut_off_next = False
            return ModelResponse({"role": "assistant", "content": "Let me think"}, dict(USAGE),
                                 {"finish_reason": "length"})
        names = tools[0]["function"]["parameters"]["required"]
        visible = messages[1]["content"].split("<visible_context>")[1]
        return ModelResponse(call("report_features", {
            name: "true" if "form fields" in visible else "false" for name in names}), dict(USAGE), {})


class IncrementalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.skill = self.root / "pdf"
        shutil.copytree(ROOT / "skills/pdf", self.skill)
        store = self.root / "store"
        server = make_server(RDRBackend(store), port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def close():
            server.shutdown()
            server.server_close()
            thread.join()
        self.addCleanup(close)
        url = f"http://127.0.0.1:{server.server_port}"
        write_json(self.root / "receipt.json", RDRBackend(store).prepare(self.skill))
        write_json(self.root / "run.json", {
            "version": 1, "model": {"factory": f"{__name__}:AgentModel"},
            "benchmark": {"factory": f"{__name__}:Adapter"},
            "skill": {"id": "pdf", "full_markdown": str(self.skill / "initial.md"),
                      "receipt": str(self.root / "receipt.json"),
                      "resolver": {"factory": "skill_agent.resolvers:HttpSkillResolver", "options": {"base_url": url}}},
            "run": {"output_dir": str(self.root / "unused"), "split": "dev", "limit": None, "replicates": 1,
                    "concurrency": 1, "max_calls": 4, "timeout_seconds": 30, "skill_loading": "rolling",
                    "conditions": ["reduced"]}})
        write_json(self.root / "learner.json", {
            "version": 1, "mode": "incremental", "run_config": str(self.root / "run.json"),
            "skill_dir": str(self.skill), "output_dir": str(self.root / "learning"),
            "hooks": {"factory": f"{__name__}:Hooks"},
            "verifier": {"model": {"factory": f"{__name__}:VerifierModel"}, "max_calls": 6},
            "judge": {"model": {"factory": f"{__name__}:JudgeModel"}},
            "validation": {"enabled": True, "service_url": url, "store": str(store), "replicates": 2}})
        VerifierModel.episodes, VerifierModel.refuse_next = 0, False
        AgentModel.calls, AgentModel.refuse_at = 0, None
        JudgeModel.cut_off_next = False

    def learner(self):
        return IncrementalLearner(load_learner_config(self.root / "learner.json"))

    async def test_a_later_document_is_passed_by_an_earlier_rule_without_the_inspector(self):
        learner = self.learner()
        state = await learner.learn()
        self.assertEqual((state["round"], state["batch_size"]), (4, 1))
        rounds = learner.rounds()
        self.assertEqual([rounds[str(i)]["tasks"] for i in range(4)], [[name] for name in ORDER])
        self.assertEqual([list(rounds[str(i)]["passed"].values()) for i in range(4)],
                         [[False], [True], [True], [True]])
        # forms_a: rule kept (v1); names: stored as worked fine (v2); forms_b: passes on v2 thanks
        # to the rule; totals: stored (v3).
        self.assertEqual([(rounds[str(i)]["version_before"], rounds[str(i)]["version_after"]) for i in range(4)],
                         [(0, 1), (1, 2), (2, 3), (3, 4)])
        self.assertEqual(rounds["0"]["accepted"], [0])
        self.assertEqual(VerifierModel.episodes, 1)  # Only forms_a ever reached the inspector.
        forms_b = read_json(learner.round_dir(2) / "run" / "episodes" / episode_filename("forms_b", "reduced"))
        judged = [e["response"]["metadata"]["feature_judge"]["values"] for e in forms_b["events"]
                  if e["type"] == "skill"]
        self.assertEqual(judged[0], {"lf_form_fields": "true"})  # From the materials, at the first step.

        status = learner.status()
        self.assertEqual((status["attempts"], status["passed"], status["diagnoses"], status["accepted"]),
                         (4, 3, 1, 1))
        final = SkillWorkspace(status["current_skill"])
        base = {"operation": ["extract_content"], "source_kind": "unknown"}
        self.assertIn(MARKER, final.render(final.case_for(
            base, judge_context(TASK, MATERIALS["forms_a"]))).markdown)
        self.assertNotIn(MARKER, final.render(final.case_for(
            base, judge_context(TASK, MATERIALS["names"]))).markdown)
        # The original skill is untouched; working folders of finished rounds are gone.
        self.assertFalse((self.skill / "compiled/learned.json").exists())
        self.assertFalse(any(learner.round_dir(i).joinpath("work").exists() for i in range(4)))
        self.assertFalse((learner.root / ".learning").exists())

        report = incremental_report(learner)
        self.assertEqual([(a["task_id"], a["passed"], a.get("rule")) for a in report["attempts"]],
                         [("forms_a", False, "1.1"), ("names", True, None), ("forms_b", True, None),
                          ("totals", True, None)])
        [rule] = report["rules"]
        self.assertEqual((rule["rule"], rule["decision"], rule["fixed"], rule["tree_after"]),
                         ("1.1", "accepted", {"forms_a": True}, 1))
        self.assertEqual((report["first_half"], report["second_half"]),
                         ({"passed": 1, "attempts": 2}, {"passed": 2, "attempts": 2}))
        self.assertEqual(report["costs"]["2 inspector"]["model_calls"], 3)
        self.assertIn("| 1 | forms_a | 0 | v0 | failed | skill_missing |",
                      (learner.root / "report.md").read_text(encoding="utf-8"))

        # Running again changes nothing and calls no model.
        again = self.learner()
        with patch.object(AgentModel, "complete", side_effect=AssertionError("worker called")):
            self.assertEqual(await again.learn(), state)

    async def test_an_interrupted_round_resumes_without_repeating_paid_steps(self):
        class Stop(Exception):
            pass

        async def interrupted(self, directory, outcomes):
            raise Stop

        with patch.object(IncrementalLearner, "_proposals", interrupted), self.assertRaises(Stop):
            await self.learner().learn()
        self.assertEqual(VerifierModel.episodes, 1)
        self.assertFalse((self.root / "learning" / ".learning").exists())  # The lock is released.
        state = await self.learner().learn()
        self.assertEqual(VerifierModel.episodes, 1)  # The saved diagnosis was reused.
        self.assertEqual(state["round"], 4)

    async def test_refused_inspector_and_checker_calls_are_retried_not_recorded(self):
        # 1. The inspector's call is refused: no verdict is saved and the round stays open.
        VerifierModel.refuse_next = True
        with self.assertRaisesRegex(Interrupted, "inspector"):
            await self.learner().learn()
        learner = self.learner()
        self.assertEqual(list((learner.round_dir(0) / "diagnoses").glob("*.json")), [])
        self.assertEqual(learner.state()["round"], 0)
        self.assertFalse((learner.root / ".learning").exists())
        # 2. The rerun diagnoses again; then the checker's first re-run (the worker's next call) is refused.
        AgentModel.refuse_at = AgentModel.calls + 1
        with self.assertRaisesRegex(Interrupted, "checker"):
            await self.learner().learn()
        self.assertEqual(VerifierModel.episodes, 2)
        self.assertFalse((learner.round_dir(0) / "ledger.json").exists())  # No decision was recorded.
        # 3. The rerun repeats only the refused re-run, keeps the rule and finishes every round.
        state = await self.learner().learn()
        self.assertEqual(VerifierModel.episodes, 2)
        self.assertEqual(state["round"], 4)
        rounds = learner.rounds()
        self.assertEqual(rounds["0"]["accepted"], [0])
        [diagnosis] = (learner.round_dir(0) / "diagnoses").glob("*.json")
        self.assertEqual(read_json(diagnosis)["diagnosis"]["attribution"], "skill_missing")
        [entry] = read_json(learner.round_dir(0) / "ledger.json")
        self.assertEqual((entry["decision"], entry["validation"]["fixed"]), ("accepted", {"forms_a": True}))

    async def test_a_refused_worker_attempt_is_repeated_not_counted_as_a_failure(self):
        AgentModel.refuse_at = 1  # The very first worker call (round 1).
        with self.assertRaisesRegex(Interrupted, "worker"):
            await self.learner().learn()
        learner = self.learner()
        self.assertEqual(list((learner.round_dir(0) / "run" / "episodes").glob("*.json")), [])
        self.assertFalse((learner.round_dir(0) / "outcomes.json").exists())
        self.assertEqual(VerifierModel.episodes, 0)  # The refusal never reached the inspector.
        state = await self.learner().learn()
        self.assertEqual(state["round"], 4)
        self.assertEqual([list(learner.rounds()[str(i)]["passed"].values()) for i in range(4)],
                         [[False], [True], [True], [True]])

    async def test_an_unusable_judge_answer_stops_learning_instead_of_rejecting_the_rule(self):
        # Round 1's tree has no learned features yet, so the first judge call is the rule's insertion.
        JudgeModel.cut_off_next = True
        with self.assertRaisesRegex(JudgeError, "ran out of output tokens"):
            await self.learner().learn()
        learner = self.learner()
        self.assertFalse((learner.round_dir(0) / "ledger.json").exists())  # No rejection was recorded.
        self.assertEqual(learner.state()["round"], 0)
        self.assertFalse((learner.root / ".learning").exists())
        state = await self.learner().learn()
        self.assertEqual((state["round"], learner.rounds()["0"]["accepted"]), (4, [0]))
        self.assertEqual(VerifierModel.episodes, 1)  # The saved diagnosis was reused.

    def test_what_counts_as_interrupted(self):
        for error in ("HTTPStatusError: HTTP 429 from openrouter.ai", "RuntimeError: HTTP 503 from x",
                      "JudgeError: judge response was cut off", "URLError: <urlopen error timed out>",
                      "ConnectionResetError: reset", "TimeoutError: read timed out"):
            self.assertTrue(was_interrupted({"status": "failed", "error": error}), error)
        for outcome in ({"status": "failed", "error": None},  # The model simply never finished.
                        {"status": "budget_exhausted", "error": "TimeoutError: "},  # The episode's own budget.
                        {"status": "failed", "error": "cleanup failed: ConnectionResetError: x"},
                        {"status": "failed", "error": "HTTPStatusError: HTTP 400 from openrouter.ai"},
                        {"status": "completed", "error": None}):
            self.assertFalse(was_interrupted(outcome), outcome)

    async def test_redo_last_round_rediagnoses_a_crashed_inspector_and_reuses_the_worker_attempt(self):
        with patch.object(sys.modules[__name__], "ORDER", ["names", "forms_a"]):
            # As earlier versions saved it: a refused inspector call stored as the verdict.
            VerifierModel.refuse_next = True
            with patch("trace_learner.incremental.was_interrupted", return_value=False):
                await self.learner().learn()
            learner = self.learner()
            self.assertEqual(learner.rounds()["1"]["accepted"], [])
            [episode] = (learner.round_dir(1) / "run" / "episodes").glob("*.json")
            attempt = episode.read_bytes()

            learner.redo_last_round()
            self.assertEqual((learner.state()["round"], learner.state()["current_version"]), (1, 1))
            self.assertEqual(list((learner.round_dir(1) / "diagnoses").glob("*.json")), [])
            state = await self.learner().learn()
            item = learner.rounds()["1"]
            self.assertEqual((state["round"], item["accepted"], item["version_before"], item["version_after"]),
                             (2, [0], 1, 2))
            self.assertEqual(VerifierModel.episodes, 2)
            self.assertEqual(episode.read_bytes(), attempt)  # The worker's attempt was reused, not repeated.
            self.assertEqual(len(list((learner.round_dir(1) / "run" / "episodes").glob("*.json"))), 1)

    async def test_redo_last_round_undoes_its_version_and_examples(self):
        learner = self.learner()
        await learner.learn()
        def mentions_totals():
            return any("totals|reduced|0" in sources
                       for sources in read_json(learner.root / "cornerstones.json").values())
        self.assertTrue(mentions_totals())
        learner.redo_last_round()
        self.assertEqual((learner.state()["round"], learner.state()["current_version"]), (3, 3))
        self.assertFalse(learner.version_dir(4).exists())
        self.assertFalse(mentions_totals())
        with patch.object(AgentModel, "complete", side_effect=AssertionError("worker called")):
            state = await self.learner().learn()
        self.assertEqual((state["round"], state["current_version"]), (4, 4))
        self.assertTrue(mentions_totals())

    def test_redo_needs_a_finished_round(self):
        with self.assertRaisesRegex(ValueError, "no finished round"):
            self.learner().redo_last_round()

    async def test_one_learner_per_output_folder(self):
        folder = self.root / "learning"
        folder.mkdir()
        (folder / ".learning").write_text(str(os.getpid()))  # A live process holds the lock.
        with self.assertRaisesRegex(RuntimeError, "another learner"):
            await self.learner().learn()

    def test_config_must_match_the_folder(self):
        config = read_json(self.root / "learner.json")
        config["batch_size"] = 0
        write_json(self.root / "bad.json", config)
        with self.assertRaisesRegex(ValueError, "batch_size"):
            load_learner_config(self.root / "bad.json")
        del config["run_config"]
        config["batch_size"] = 1
        write_json(self.root / "bad.json", config)
        with self.assertRaisesRegex(ValueError, "run_config"):
            load_learner_config(self.root / "bad.json")


if __name__ == "__main__":
    unittest.main()
