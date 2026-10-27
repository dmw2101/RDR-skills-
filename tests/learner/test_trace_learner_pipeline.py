"""Offline end-to-end: collect -> verify -> consolidate -> insert -> validate, with fake models."""

import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from rdr_service.backend import RDRBackend
from rdr_service.server import make_server
from skill_agent.experiments import run_experiment
from skill_agent.learned_features import judge_context
from skill_agent.models import ModelResponse
from skill_agent.storage import read_json, write_json
from trace_learner.pipeline import Learner, load_learner_config
from trace_learner.records import episode_key
from trace_learner.report import learning_report
from trace_learner.workspace import SkillWorkspace

ROOT = Path(__file__).resolve().parents[2]
MARKER = "Always list AcroForm fields with pypdf get_fields() first."
USAGE = {"input_tokens": 3, "output_tokens": 1, "cached_input_tokens": 0, "cost_usd": 0.0}
# As in VAREX, every task has the same sentence; only the starting materials (the PDF) differ,
# so the learned rule can be judged true for the form only through the materials.
TASKS = {name: "Extract the requested information from this PDF" for name in ("forms", "totals", "names")}
MATERIALS = {"forms": "PDF text layer: an application with form fields Name and Date",
             "totals": "PDF text layer: a totals table", "names": "PDF text layer: a list of names"}


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
        return [{"id": name, "task": text} for name, text in TASKS.items()][:limit]

    async def open_session(self, task):
        return Session(task)

    def judge_materials(self, task):
        return MATERIALS[task["id"]]

    def score(self, records, output_dir):
        return {"summaries": [], "document_metrics": []}


class Hooks:
    """The form task passes only if the agent saw the learned guidance; others always pass."""

    benchmark = "toy PDF extraction"

    def __init__(self, adapter):
        self.adapter = adapter

    def outcomes(self, records, output_dir):
        return {episode_key(r): r["task_id"] != "forms" or bool((r["prediction"] or {}).get("saw_guidance"))
                for r in records}

    def ground_truth(self, task):
        return {"saw_guidance": True}

    async def rescore(self, task, prediction):
        return {"passed": prediction.get("saw_guidance") is True}

    async def open_inspection(self, task):
        return Session(task)


class AgentModel:
    """Turn 0 answers whether the learned guidance was in the prompt; turn 1 finishes."""

    async def complete(self, messages, tools):
        if messages[-1]["role"] == "tool":
            return ModelResponse({"role": "assistant", "content": "done"}, dict(USAGE), {})
        return ModelResponse(call("answer", {"saw_guidance": MARKER in messages[0]["content"]}), dict(USAGE), {})


class VerifierModel:
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
        self.turn = 0

    async def complete(self, messages, tools):
        self.turn += 1
        return ModelResponse(self.script[min(self.turn, len(self.script)) - 1], dict(USAGE), {})


class JudgeModel:
    calls = 0

    async def complete(self, messages, tools):
        JudgeModel.calls += 1
        names = tools[0]["function"]["parameters"]["required"]
        visible = messages[1]["content"].split("<visible_context>")[1]
        return ModelResponse(call("report_features", {
            name: "true" if "form fields" in visible else "false" for name in names}), dict(USAGE), {})


class PipelineTests(unittest.IsolatedAsyncioTestCase):
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
        collect = {"version": 1, "model": {"factory": f"{__name__}:AgentModel"},
                   "benchmark": {"factory": f"{__name__}:Adapter"},
                   "skill": {"id": "pdf", "full_markdown": str(self.skill / "initial.md"),
                             "receipt": str(self.root / "receipt.json"),
                             "resolver": {"factory": "skill_agent.resolvers:HttpSkillResolver",
                                          "options": {"base_url": url}}},
                   "run": {"output_dir": str(self.root / "collect"), "split": "dev", "limit": None,
                           "replicates": 1, "concurrency": 1, "max_calls": 4, "timeout_seconds": 30,
                           "skill_loading": "rolling", "conditions": ["reduced"]}}
        await run_experiment(collect)
        write_json(self.root / "learner.json", {
            "version": 1, "collect_run": str(self.root / "collect"), "skill_dir": str(self.skill),
            "output_dir": str(self.root / "learning"), "hooks": {"factory": f"{__name__}:Hooks"},
            "verifier": {"model": {"factory": f"{__name__}:VerifierModel"}, "max_calls": 6},
            "judge": {"model": {"factory": f"{__name__}:JudgeModel"}},
            "validation": {"enabled": True, "service_url": url, "store": str(store), "replicates": 2}})
        JudgeModel.calls = 0

    async def test_failed_trace_becomes_a_validated_mcrdr_exception(self):
        learner = Learner(load_learner_config(self.root / "learner.json"))
        ledger = await learner.insert()
        self.assertEqual(len(ledger), 1)
        entry = ledger[0]
        self.assertEqual(entry["decision"], "accepted", entry.get("reason"))
        self.assertEqual(entry["version"], 1)
        self.assertEqual(entry["validation"]["fixed"], {"forms": True})
        self.assertEqual(entry["validation"]["regressions"], [])
        self.assertEqual(sorted(entry["validation"]["passes"]), ["forms", "names", "totals"])
        status = learner.status()
        self.assertEqual((status["diagnoses"], status["eligible"], status["accepted"]), (1, 1, 1))
        self.assertEqual(status["attributions"], {"skill_missing": 1})

        learned = SkillWorkspace(status["current_skill"])
        base = {"operation": ["extract_content"], "source_kind": "unknown"}
        forms, totals = (judge_context(TASKS[name], MATERIALS[name]) for name in ("forms", "totals"))
        self.assertIn(MARKER, learned.render(learned.case_for(base, forms)).markdown)
        self.assertNotIn(MARKER, learned.render(learned.case_for(base, totals)).markdown)
        # The same task sentence alone was never judged, so it cannot fire the rule.
        self.assertNotIn(MARKER, learned.render(learned.case_for(base, TASKS["forms"])).markdown)
        # The original skill is untouched; versions are separate copies.
        self.assertFalse((self.skill / "compiled/learned.json").exists())

        report = learning_report(learner)
        self.assertEqual((report["episodes"], report["passed"]), (3, 2))
        [failure] = report["failures"]
        self.assertEqual((failure["task_id"], failure["attribution"], failure["eligible"], failure["proposal"],
                          failure["decision"]), ("forms", "skill_missing", True, 0, "accepted"))
        [rule] = report["rules"]
        self.assertEqual((rule["kind"], rule["node"], rule["decision"], rule["version"], rule["fixed"]),
                         ("replace", 2, "accepted", 1, {"forms": True}))
        self.assertIn("lf_form_fields (new)", rule["conditions"])
        self.assertEqual(report["costs"]["1 collect (worker)"]["model_calls"], 6)
        self.assertEqual(report["costs"]["2 verify (inspector)"]["model_calls"], 3)
        self.assertEqual(report["costs"]["5 checker re-runs (worker + judge)"]["model_calls"], 12)
        self.assertEqual(report["total_known_cost_usd"], 0.0)
        markdown = (self.root / "learning/report.md").read_text(encoding="utf-8")
        self.assertIn("| forms | 0 | completed | skill_missing |", markdown)
        self.assertIn("accepted (v1)", markdown)

        # Re-running every stage reuses saved outputs and makes no further model calls.
        calls = JudgeModel.calls
        again = Learner(load_learner_config(self.root / "learner.json"))
        self.assertEqual(await again.insert(), ledger)
        self.assertEqual(JudgeModel.calls, calls)

    async def test_learner_refuses_a_different_skill(self):
        config = read_json(self.root / "learner.json")
        config["skill_dir"] = str(ROOT / "skills/alfworld")
        write_json(self.root / "other.json", config)
        with self.assertRaisesRegex(ValueError, "not the skill the collected run used"):
            Learner(load_learner_config(self.root / "other.json"))


if __name__ == "__main__":
    unittest.main()
