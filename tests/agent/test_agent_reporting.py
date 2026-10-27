import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path

from rdr_service.server import make_server
from skill_agent.contracts import ResolvedSkill, fingerprint, source_fingerprint
from skill_agent.experiments import episode_filename, run_experiment, summarize
from skill_agent.models import ModelResponse
from skill_agent.reporting import build_report, kilo, render_markdown, tokens, write_report
from skill_agent.storage import read_json, write_json

TASK = "Extract the requested information from the PDF into JSON matching the supplied schema."


def make_run(root, conditions, episodes, *, model="qwen", engine="scrdr", view="view-1", revision="rev-a"):
    """Write a complete, summarized run folder.

    episodes maps (document, condition) to (status, correct fields of 10, input tokens, cost).
    """
    root = Path(root)
    documents = sorted({doc for doc, _ in episodes})
    skill = {} if conditions == ["none"] else {"id": "pdf"}
    config = {"version": 1, "model": {"factory": "x:Model", "options": {"model": model, "temperature": 0}},
              "benchmark": {"factory": "x:Varex", "options": {}}, "skill": skill,
              "run": {"output_dir": str(root), "split": "dev", "limit": len(documents), "replicates": 1,
                      "concurrency": 1, "max_calls": 20, "timeout_seconds": 600,
                      "skill_loading": "rolling", "conditions": conditions}}
    manifest = {"config": config, "git_revision": "abc", "benchmark_fingerprint": "data-v1",
                "skill_receipt": None if not skill else {"resolver_revision": revision},
                "tasks": [{"id": doc, "task": TASK} for doc in documents], "system_prompt": "system"}
    run_id = fingerprint(manifest)
    write_json(root / "manifest.json", {"run_id": run_id, **manifest, "preflight_skill": []})
    records, metrics = [], []
    for (doc, condition), (status, correct, input_tokens, cost) in sorted(episodes.items()):
        events = [{"type": "model_request", "turn": 0},
                  {"type": "model_response", "turn": 0, "message": {},
                   "usage": {"input_tokens": input_tokens, "output_tokens": 10, "cost_usd": cost}}]
        if condition != "none":
            metadata = ({"condition": "full"} if condition == "full" else
                        {"engine": "mcrdr", "terminal_ids": [1]} if engine == "mcrdr" else {"path": []})
            request = {"skill_id": "pdf", "task": TASK, "context": {}, "artifact_id": "a"}
            events = [{"type": "skill_request", "request": request},
                      {"type": "skill", "request": request, "markdown_sha256": view if condition == "reduced" else "full",
                       "response": {"markdown": "x", "metadata": metadata,
                                    "usage": {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0}}}] + events
        record = {"run_id": run_id, "task_id": doc, "replicate": 0, "condition": condition, "status": status,
                  "prediction": {}, "error": None if status == "completed" else "stopped", "events": events,
                  "seconds": 1.0}
        record["record_sha256"] = fingerprint(record)
        write_json(root / "episodes" / episode_filename(doc, condition), record)
        records.append(record)
        for metric, numerator, denominator in (("normalized_field_em", correct, 10),
                                               ("perfect_document", int(correct == 10), 1)):
            metrics.append({"task_id": doc, "condition": condition, "replicate": 0, "metric": metric,
                            "numerator": numerator, "denominator": denominator})
    write_json(root / "scores" / "scoring-request.json", {"records": records})
    write_json(root / "scores" / "scoring-result.json", {"summaries": [], "document_metrics": metrics})
    return root


def arms(values, condition):
    return {(doc, condition): value for doc, value in zip(("d1", "d2", "d3"), values)}


class ReportFormulaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        ok = "completed"
        # SCRDR: two runs, each a full/reduced folder plus a separate skill-blind folder.
        make_run(self.root / "s1fr", ["full", "reduced"], {
            **arms([(ok, 10, 100, .01), (ok, 8, 200, .01), (ok, 7, 300, .01)], "full"),
            **arms([(ok, 10, 50, .01), (ok, 9, 60, None), (ok, 9, 1000, .01)], "reduced")})
        make_run(self.root / "s1n", ["none"], arms([(ok, 6, 40, .01), (ok, 5, 45, .01),
                                                     ("budget_exhausted", 0, 30, .01)], "none"))
        make_run(self.root / "s2fr", ["full", "reduced"], {
            **arms([(ok, 9, 110, .01), (ok, 9, 210, .01), (ok, 9, 310, .01)], "full"),
            **arms([(ok, 8, 55, .01), (ok, 9, 65, .01), (ok, 7, 70, .01)], "reduced")})
        make_run(self.root / "s2n", ["none"], arms([(ok, 7, 41, .01), (ok, 7, 46, .01), (ok, 7, 31, .01)], "none"))
        # MCRDR: one run with the same documents.
        make_run(self.root / "m1fr", ["full", "reduced"], {
            **arms([(ok, 10, 100, .01), (ok, 8, 200, .01), (ok, 7, 300, .01)], "full"),
            **arms([(ok, 10, 50, .01), (ok, 10, 60, .01), (ok, 9, 900, .01)], "reduced")}, engine="mcrdr",
            revision="rev-m")
        self.spec = {"series": {
            "SCRDR": {"Run 1": ["s1fr", "s1n"], "Run 2": ["s2fr", "s2n"]},
            "MCRDR": {"Run 1": ["m1fr"]}}, "outliers": 1, "top_documents": 1, "bootstrap_samples": 200}

    def report(self, spec=None):
        path = self.root / "spec.json"
        write_json(path, spec or self.spec)
        return build_report(path)

    def test_accuracy_completion_and_perfect_documents(self):
        scrdr = self.report()["series"]["SCRDR"]
        self.assertEqual(scrdr["conditions"], ["full", "reduced", "none"])
        pooled = scrdr["pooled"]["conditions"]
        self.assertEqual((pooled["full"]["fields_correct"], pooled["full"]["fields_total"]), (52, 60))
        self.assertEqual((pooled["reduced"]["fields_correct"], pooled["none"]["fields_correct"]), (52, 32))
        self.assertAlmostEqual(scrdr["runs"]["Run 1"]["conditions"]["reduced"]["field_exact_match"], 28 / 30)
        self.assertAlmostEqual(scrdr["runs"]["Run 1"]["differences"]["reduced_minus_full"], 3 / 30)
        self.assertAlmostEqual(scrdr["runs"]["Run 2"]["differences"]["reduced_minus_full"], -3 / 30)
        self.assertAlmostEqual(scrdr["pooled"]["differences"]["reduced_minus_full"], 0)
        self.assertAlmostEqual(scrdr["pooled"]["differences"]["reduced_minus_none"], 20 / 60)
        self.assertEqual([pooled[c]["completed"] for c in ("full", "reduced", "none")], [6, 6, 5])
        self.assertEqual([pooled[c]["perfect_documents"] for c in ("full", "reduced", "none")], [1, 1, 0])
        self.assertEqual(scrdr["incomplete_episodes"], [{"condition": "none", "run": "Run 1", "task_id": "d3",
                                                        "replicate": 0, "status": "budget_exhausted",
                                                        "error": "stopped"}])
        ci = scrdr["pooled"]["paired_bootstrap"]["normalized_field_em:reduced_minus_full"]
        self.assertEqual(ci["documents"], 3)

    def test_reduced_only_series_compare_original_and_learned_trees(self):
        # The learning notebook's comparison: reduced-arm runs only, original tree first.
        ok = "completed"
        make_run(self.root / "base", ["reduced"], arms([(ok, 9, 50, .01), (ok, 8, 60, .01), (ok, 10, 70, .01)],
                                                       "reduced"), engine="mcrdr", revision="rev-m")
        make_run(self.root / "learned", ["reduced"], arms([(ok, 10, 55, .01), (ok, 9, 60, .01), (ok, 10, 75, .01)],
                                                          "reduced"), engine="mcrdr", view="view-2", revision="rev-l")
        report = self.report({"series": {"Original": {"Run 1": ["base"]}, "Learned": {"Run 1": ["learned"]}}})
        learned = report["series"]["Learned"]
        self.assertEqual(learned["conditions"], ["reduced"])
        self.assertAlmostEqual(learned["pooled"]["conditions"]["reduced"]["field_exact_match"], 29 / 30)
        self.assertEqual(learned["runs"]["Run 1"]["conditions"]["reduced"]["perfect_documents"], 2)
        change = report["series_comparison"]["Learned_vs_Original"]["reduced"]
        self.assertAlmostEqual(change["field_exact_match"], 2 / 30)
        self.assertEqual(change["perfect_documents"], 1)
        self.assertIn("Learned", render_markdown(report))

    def test_usage_percentiles_cost_and_outliers(self):
        scrdr = self.report()["series"]["SCRDR"]
        full, reduced = (scrdr["pooled"]["conditions"][c] for c in ("full", "reduced"))
        self.assertEqual(full["input_tokens"], {"median": 205, "mean": 205, "p90": 305, "total": 1230})
        self.assertEqual(reduced["input_tokens"]["median"], 62.5)
        self.assertAlmostEqual(scrdr["pooled"]["median_input_reduction_vs_full"], (205 - 62.5) / 205)
        self.assertAlmostEqual(full["cost_usd"]["total"], .06)
        self.assertIsNone(reduced["cost_usd"]["total"])
        self.assertEqual(reduced["cost_usd"]["episodes_with_unreported_cost"], 1)
        self.assertEqual(scrdr["top_document_input_share"]["reduced"], {"documents": ["d3"], "share": 1070 / 1300})
        self.assertEqual(scrdr["outlier_episodes"]["reduced"], [{
            "run": "Run 1", "task_id": "d3", "replicate": 0, "status": "completed", "input_tokens": 1000,
            "paired_input_tokens": {"full": 300, "reduced": 1000, "none": 30}}])

    def test_engine_comparison_and_guidance_identity(self):
        report = self.report()
        comparison = report["series_comparison"]["MCRDR_vs_SCRDR"]
        self.assertAlmostEqual(comparison["reduced"]["field_exact_match"], 29 / 30 - 52 / 60)
        self.assertNotIn("none", comparison)
        self.assertEqual(report["guidance_identity"]["between_series"]["MCRDR_vs_SCRDR"],
                         {"shared_requests": 1, "identical": 1, "differing": 0})
        engines = {f["folder"].rsplit("\\", 1)[-1].rsplit("/", 1)[-1]: f["engine"] for f in report["provenance"]}
        self.assertEqual((engines["s1fr"], engines["m1fr"], engines["s1n"]), (["scrdr"], ["mcrdr"], []))
        make_run(self.root / "m2fr", ["full", "reduced"], {**arms([("completed", 10, 1, .0)] * 3, "full"),
                                                          **arms([("completed", 10, 1, .0)] * 3, "reduced")},
                 engine="mcrdr", view="view-2")
        self.spec["series"]["MCRDR"] = {"Run 1": ["m2fr"]}
        self.assertEqual(self.report()["guidance_identity"]["between_series"]["MCRDR_vs_SCRDR"]["differing"], 1)

    def test_markdown_matches_deck_layout(self):
        text = render_markdown(self.report())
        for expected in ("| Pooled (60 fields) | 86.67% | 86.67% | 53.33% |",
                         "Correct fields: Full 52 / 60     Reduced 52 / 60     Blind 32 / 60",
                         "| Completed | 6 / 6 | 6 / 6 | 5 / 6 |",
                         "| Run 1 | +10.00 pp | +56.67 pp |", "| Run 2 | −10.00 pp |",
                         "| Median (p50) input tokens | 205 | 63 | 41 |",
                         "Reduced uses 69.5% fewer median input tokens than full skill.",
                         "| Reported cost (total) | $0.0600 | not reported | $0.0600 |",
                         "## Series comparison", "1 of 1 shared requests identical"):
            self.assertIn(expected, text)
        self.assertEqual((tokens(51093.5), kilo(76949)), ("51,094", "76.9k"))

    def test_write_report_creates_json_and_markdown(self):
        path = self.root / "spec.json"
        write_json(path, self.spec)
        result = write_report(path)
        self.assertEqual(result["series"], ["SCRDR", "MCRDR"])
        self.assertTrue(Path(result["report_markdown"]).read_text(encoding="utf-8").startswith("# VAREX"))
        self.assertIn("definitions", read_json(result["report_json"]))

    def test_invalid_inputs_are_rejected(self):
        cases = {
            "appears in both": {"series": {"S": {"Run 1": ["s1fr", "s2fr"]}}},
            "not paired": {"series": {"S": {"Run 1": ["s1fr", "short"]}}},
            "mixes engines": {"series": {"S": {"Run 1": ["s1fr"], "Run 2": ["m1fr"]}}},
            "nonempty 'series'": {"runs": {}},
        }
        make_run(self.root / "short", ["none"], {("d1", "none"): ("completed", 5, 1, .0)})
        for message, spec in cases.items():
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                self.report(spec)
        (self.root / "s1n" / "scores" / "scoring-result.json").unlink()
        with self.assertRaisesRegex(ValueError, "summarize"):
            self.report()

    def test_scores_from_other_episodes_and_changed_settings(self):
        request = self.root / "s2n" / "scores" / "scoring-request.json"
        data = read_json(request)
        data["records"][0]["record_sha256"] = "other"
        write_json(request, data)
        with self.assertRaisesRegex(ValueError, "different episodes"):
            self.report()
        data["records"][0]["record_sha256"] = read_json(next((self.root / "s2n" / "episodes").glob("d1_*")))["record_sha256"]
        write_json(request, data)
        make_run(self.root / "m1fr", ["full", "reduced"], {**arms([("completed", 10, 1, .0)] * 3, "full"),
                                                          **arms([("completed", 10, 1, .0)] * 3, "reduced")},
                 engine="mcrdr", model="other-model")
        self.assertIn("setting 'model' differs between folders; see provenance", self.report()["warnings"])


class ReportTemplateTests(unittest.TestCase):
    def test_template_spec_is_valid_and_mirrors_the_pilot(self):
        from skill_agent.reporting import load_spec

        series, options = load_spec(Path(__file__).resolve().parents[2] / "examples/agent/report.template.json")
        self.assertEqual(list(series), ["SCRDR", "MCRDR"])
        self.assertTrue(all(len(runs) == 3 and all(len(f) == 2 for f in runs.values()) for runs in series.values()))
        self.assertEqual(options, {"outliers": 5, "top_documents": 5, "bootstrap_samples": 2000})


class TinyModel:
    async def complete(self, messages, tools):
        return ModelResponse({"role": "assistant", "content": "done"},
                             {"input_tokens": 11, "output_tokens": 2, "cost_usd": 0.001}, {})


class TinySession:
    tools, prompt = {}, "tiny"

    async def collect(self):
        return {"done": True}

    async def close(self):
        pass


class TinyVarex:
    """Writes scores exactly where and how VarexAdapter does."""
    snapshot_fingerprint = "tiny-data"
    system_prompt = "Tiny extraction."

    async def prepare_runtime(self):
        return {}

    def tasks(self, split, limit=None):
        return [{"id": doc, "task": TASK} for doc in ("a", "b")][:limit]

    async def open_session(self, task):
        return TinySession()

    def score(self, records, output_dir):
        metrics = [{"task_id": r["task_id"], "condition": r["condition"], "replicate": r["replicate"],
                    "metric": metric, "numerator": value, "denominator": denominator}
                   for r in records
                   for metric, value, denominator in (("normalized_field_em", 3 if r["condition"] == "reduced" else 2, 4),
                                                      ("perfect_document", 0, 1))]
        result = {"summaries": [], "document_metrics": metrics}
        write_json(Path(output_dir) / "scoring-request.json", {"records": records})
        write_json(Path(output_dir) / "scoring-result.json", result)
        return result


class ReportFromHarnessRunTests(unittest.TestCase):
    def test_report_reads_real_harness_folders(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            text = "full skill"

            class Backend:
                def resolve(self, request):
                    return ResolvedSkill("reduced", request.artifact_id, source_fingerprint(text), "rev",
                                         {"engine": "mcrdr", "terminal_ids": [1]})

            server = make_server(Backend(), port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                (root / "full.md").write_text(text, encoding="utf-8")
                write_json(root / "receipt.json", {"artifact_id": "opaque", "resolver_revision": "rev",
                                                   "source_fingerprint": source_fingerprint(text)})
                config = {"version": 1, "model": {"factory": f"{__name__}:TinyModel"},
                          "benchmark": {"factory": f"{__name__}:TinyVarex"},
                          "skill": {"id": "pdf", "full_markdown": str(root / "full.md"),
                                    "receipt": str(root / "receipt.json"),
                                    "resolver": {"factory": "skill_agent.resolvers:HttpSkillResolver",
                                                 "options": {"base_url": f"http://127.0.0.1:{server.server_port}"}}},
                          "run": {"output_dir": str(root / "run"), "split": "dev", "limit": 2, "replicates": 1,
                                  "concurrency": 1, "max_calls": 3, "timeout_seconds": 10, "skill_loading": "rolling"}}
                asyncio.run(run_experiment(config))
                summarize(root / "run")
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
            write_json(root / "spec.json", {"series": {"MCRDR": {"Run 1": ["run"]}}})
            report = build_report(root / "spec.json")
            pooled = report["series"]["MCRDR"]["pooled"]
            self.assertEqual(pooled["conditions"]["reduced"]["fields_correct"], 6)
            self.assertAlmostEqual(pooled["differences"]["reduced_minus_full"], .25)
            self.assertEqual(pooled["conditions"]["none"]["input_tokens"]["median"], 11)
            self.assertAlmostEqual(pooled["conditions"]["full"]["cost_usd"]["total"], .002)
            self.assertEqual(report["provenance"][0]["engine"], ["mcrdr"])
            self.assertEqual(report["provenance"][0]["resolver_revision"], "rev")


if __name__ == "__main__":
    unittest.main()
