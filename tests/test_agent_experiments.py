import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path

from rdr_service.server import make_server
from skill_agent.contracts import ResolvedSkill, fingerprint, source_fingerprint
from skill_agent.experiments import (episode_filename, inspect_skill_requests, load_config, paired_bootstrap, rename_episode_files,
                                     run_experiment, summarize, usage_summary)
from skill_agent.models import ModelResponse
from skill_agent.storage import read_json, write_json


class TinySession:
    tools = {}
    prompt = "tiny task (no labels)"
    active = 0
    peak = 0
    closed = 0

    async def collect(self):
        await asyncio.sleep(.01)
        return {"done": True}

    async def close(self):
        TinySession.active -= 1
        TinySession.closed += 1


class TinyModel:
    calls = 0

    async def complete(self, messages, tools):
        TinyModel.calls += 1
        return ModelResponse({"role": "assistant", "content": "done"},
                             {"input_tokens": 11, "output_tokens": 2, "cost_usd": None,
                              "cached_input_tokens": 0}, {})


class TinyAdapter:
    snapshot_fingerprint = "test-data-v1"
    system_prompt = "Complete the tiny benchmark task."

    async def prepare_runtime(self):
        return {"fake_environment": "v1"}

    def tasks(self, split, limit=None):
        return [{"id": name, "task": "do the task"} for name in ("a", "b")][:limit]

    async def open_session(self, task):
        TinySession.active += 1
        TinySession.peak = max(TinySession.peak, TinySession.active)
        return TinySession()

    def score(self, records, output_dir):
        return {"summaries": [{"benchmark": "tiny"}], "document_metrics": [
            {"task_id": r["task_id"], "condition": r["condition"], "replicate": r["replicate"],
             "metric": "success", "numerator": int(r["status"] == "completed"), "denominator": 1}
            for r in records]}


class ExperimentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        text = "full skill text"
        class Backend:
            def resolve(self, request):
                return ResolvedSkill("reduced", request.artifact_id, source_fingerprint(text), "v1")
        server = make_server(Backend(), port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join()
        self.addCleanup(close)
        (self.root / "full.md").write_bytes(text.encode())
        write_json(self.root / "receipt.json", {"artifact_id": "opaque", "resolver_revision": "v1",
                                              "source_fingerprint": source_fingerprint(text)})
        self.config = {"version": 1, "model": {"factory": f"{__name__}:TinyModel"},
            "benchmark": {"factory": f"{__name__}:TinyAdapter"},
            "skill": {"id": "example", "full_markdown": str(self.root / "full.md"),
                      "receipt": str(self.root / "receipt.json"), "resolver": {
                          "factory": "skill_agent.resolvers:HttpSkillResolver",
                          "options": {"base_url": f"http://127.0.0.1:{server.server_port}"}}},
            "run": {"output_dir": str(self.root / "run"), "split": "dev", "limit": 2,
                    "replicates": 1, "concurrency": 2, "max_calls": 3, "timeout_seconds": 10}}
        TinyModel.calls = TinySession.active = TinySession.peak = TinySession.closed = 0

    async def test_three_arm_run_concurrency_resume_and_second_benchmark(self):
        result = await run_experiment(self.config)
        self.assertEqual(result["episodes"], 6)
        self.assertEqual(TinyModel.calls, 6)
        self.assertEqual(TinySession.closed, 6)
        self.assertEqual(TinySession.active, 0)
        self.assertLessEqual(TinySession.peak, 2)
        directory = self.root / "run/episodes"
        self.assertEqual({p.name for p in directory.glob("*.json")}, {
            f"{doc}_{condition}_{fingerprint([doc, 0, condition])}.json"
            for doc in ("a", "b") for condition in ("none", "full", "reduced")})
        self.assertEqual(episode_filename("a", "full", 1), f"a_full_{fingerprint(['a', 1, 'full'])}.json")
        self.assertEqual(episode_filename("folder/a", "none"),
                         f"folder%2Fa_none_{fingerprint(['folder/a', 0, 'none'])}.json")
        readable = directory / episode_filename("a", "none")
        original = readable.read_bytes()
        legacy = directory / (fingerprint(["a", 0, "none"]) + ".json")
        readable.rename(legacy)
        await run_experiment(self.config, resume=True)
        self.assertEqual(TinyModel.calls, 6)
        self.assertEqual(rename_episode_files(self.root / "run")["renamed"], 1)
        self.assertEqual(readable.read_bytes(), original)
        self.assertEqual(rename_episode_files(self.root / "run")["renamed"], 0)
        report = summarize(self.root / "run")
        inspection = inspect_skill_requests(self.root / "run")
        self.assertEqual(inspection["saved_episodes"], 6)
        self.assertEqual(inspection["model_skill_calls"], 0)
        self.assertTrue(all(not episode["requests"] for episode in inspection["episodes"]))
        self.assertEqual(report["usage"]["reduced"]["skill_requests"], 0)
        self.assertEqual(report["benchmark_summaries"], [{"benchmark": "tiny"}])
        self.assertEqual(report["usage"]["reduced"]["input_tokens"], 22)
        self.assertIsNone(report["usage"]["reduced"]["cost_usd"])
        self.assertEqual(report["paired_comparisons"]["success:reduced_minus_full"]["delta"], 0)
        self.assertEqual(read_json(self.root / "run/summary.json")["run_id"], result["run_id"])

    async def test_changed_config_rejected_on_resume(self):
        await run_experiment(self.config)
        self.config["run"]["concurrency"] = 1
        with self.assertRaisesRegex(ValueError, "differs"):
            await run_experiment(self.config, resume=True)
        self.assertEqual(TinyModel.calls, 6)

    async def test_tampered_record_and_incomplete_run_rejected(self):
        await run_experiment(self.config)
        path = next((self.root / "run/episodes").glob("*.json"))
        record = read_json(path)
        record["prediction"] = {"tampered": True}
        write_json(path, record)
        with self.assertRaisesRegex(ValueError, "record changed"):
            summarize(self.root / "run")
        path.unlink()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            summarize(self.root / "run")
        await run_experiment(self.config, resume=True)
        self.assertEqual(TinyModel.calls, 7)

    async def test_no_overwrite_without_resume(self):
        await run_experiment(self.config)
        with self.assertRaises(ValueError):
            await run_experiment(self.config)


class AccountingTests(unittest.TestCase):
    def test_missing_usage_is_null_not_zero(self):
        record = {"condition": "reduced", "status": "failed", "seconds": 1,
                  "events": [{"type": "model_request"}, {"type": "skill_request"}]}
        usage = usage_summary([record])["reduced"]
        self.assertIsNone(usage["input_tokens"])
        self.assertIsNone(usage["resolver_model_calls"])
        self.assertEqual(usage["agent_model_calls"], 1)

    def test_bootstrap_keeps_pairs_and_replicates(self):
        rows = [{"task_id": d, "condition": c, "replicate": r, "metric": "accuracy",
                 "numerator": int(c != "none"), "denominator": 1}
                for d in ("a", "b") for c in ("none", "full", "reduced") for r in range(3)]
        result = paired_bootstrap(rows, samples=100)
        self.assertEqual(result["accuracy:reduced_minus_full"]["paired_document_bootstrap_95pct"], [0, 0])
        self.assertEqual(result["accuracy:reduced_minus_none"]["delta"], 1)

    def test_config_file_defaults_and_rejects_typos(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            config = {"version": 1, "model": {}, "benchmark": {}, "skill": {}, "run": {"output_dir": "run"}}
            write_json(path, config)
            self.assertEqual(load_config(path)["run"]["concurrency"], 1)
            config["run"]["concurency"] = 2
            write_json(path, config)
            with self.assertRaises(ValueError):
                load_config(path)
