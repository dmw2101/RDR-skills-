"""Offline incremental learning: each document runs on the tree learned so far, with fake models."""

import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from rdr_service.backend import RDRBackend
from rdr_service.server import make_server
from skill_agent.experiments import episode_filename
from skill_agent.learned_features import judge_context
from skill_agent.models import ModelResponse
from skill_agent.storage import read_json, write_json
from trace_learner.incremental import IncrementalLearner
from trace_learner.pipeline import load_learner_config
from trace_learner.records import episode_key
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

    async def complete(self, messages, tools):
        if messages[-1]["role"] == "tool":
            return ModelResponse({"role": "assistant", "content": "done"}, dict(USAGE), {})
        return ModelResponse(call("answer", {"saw_guidance": MARKER in messages[0]["content"]}), dict(USAGE), {})


class VerifierModel:
    """The inspector: one instance per diagnosed episode."""

    episodes = 0
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
        self.turn += 1
        return ModelResponse(self.script[min(self.turn, len(self.script)) - 1], dict(USAGE), {})


class JudgeModel:
    async def complete(self, messages, tools):
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
        VerifierModel.episodes = 0

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
