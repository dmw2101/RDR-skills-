import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from skill_agent.experiments import load_config

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("setup_mcrdr_colab", ROOT / "scripts" / "setup_mcrdr_colab.py")
colab = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = colab  # dataclasses resolve annotations through sys.modules
SPEC.loader.exec_module(colab)


def options(*extra):
    return colab.parse_args(["--run-name", "mcrdr-test", *extra])


def base(experiment):
    name = colab.EXPERIMENTS[experiment]["config"]
    return json.loads((ROOT / "examples" / "agent" / name).read_text(encoding="utf-8"))


class ColabSetupConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = colab.Paths(Path(self.temp.name) / "work")

    def build(self, *extra):
        opts = options(*extra)
        return colab.build_config(base(opts.experiment), opts, self.paths)

    def assertLoads(self, config):
        path = Path(self.temp.name) / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        load_config(path)

    def test_every_experiment_and_backend_gives_a_loadable_config(self):
        for experiment in colab.EXPERIMENTS:
            for backend in ("api", "local"):
                with self.subTest(experiment=experiment, backend=backend):
                    self.assertLoads(self.build("--experiment", experiment, "--backend", backend))

    def test_defaults_reproduce_the_mcrdr_config_with_absolute_paths(self):
        work = self.paths.workdir
        for experiment in colab.EXPERIMENTS:
            with self.subTest(experiment=experiment):
                config = self.build("--experiment", experiment)
                original = base(experiment)
                data_dir, _ = colab.data_selection(experiment, original["run"].get("limit"))
                self.assertEqual(config["benchmark"]["options"]["data_dir"], str(work / data_dir))
                self.assertEqual(config["run"]["output_dir"], str(self.paths.results / "mcrdr-test"))
                self.assertEqual(config["skill"]["full_markdown"], str(work / "skills/pdf/initial.md"))
                self.assertEqual(config["skill"]["receipt"], str(self.paths.receipt))
                self.assertEqual(config["skill"]["resolver"]["options"]["base_url"], "http://127.0.0.1:8766")
                for value in (config, original):
                    del value["benchmark"]["options"]["data_dir"], value["run"]["output_dir"]
                    del value["skill"]["full_markdown"], value["skill"]["receipt"]
                    del value["skill"]["resolver"]["options"]["base_url"]
                self.assertEqual(config, original)

    def test_overrides_map_to_config_keys(self):
        config = self.build(
            "--experiment", "deepseek-dev30", "--model", "qwen/qwen3.7-flash",
            "--base-url", "https://example.test/v1", "--provider", "alibaba",
            "--max-input-price", "0.23", "--max-output-price", "0.92", "--max-tokens", "4096",
            "--limit", "12", "--replicates", "3", "--concurrency", "2",
            "--conditions", "full", "reduced", "--skill-loading", "model_initiated",
            "--service-port", "9000",
        )
        model = config["model"]["options"]
        self.assertEqual((model["model"], model["base_url"], model["max_tokens"]),
                         ("qwen/qwen3.7-flash", "https://example.test/v1", 4096))
        self.assertEqual(model["provider"], {"only": ["alibaba"], "allow_fallbacks": False,
                                             "require_parameters": True,
                                             "max_price": {"prompt": 0.23, "completion": 0.92}})
        run = config["run"]
        self.assertEqual((run["limit"], run["replicates"], run["concurrency"]), (12, 3, 2))
        self.assertEqual(run["conditions"], ["full", "reduced"])
        self.assertEqual(run["skill_loading"], "model_initiated")
        self.assertTrue(config["benchmark"]["options"]["data_dir"].endswith("varex-dev-12"))
        self.assertEqual(config["skill"]["resolver"]["options"]["base_url"], "http://127.0.0.1:9000")
        self.assertLoads(config)

    def test_skill_blind_run_needs_no_skill_section_or_service(self):
        config = self.build("--conditions", "none")
        self.assertEqual(config["skill"], {})
        self.assertFalse(colab.needs_service(config))
        self.assertLoads(config)

    def test_local_backend_uses_the_pinned_qwen_server(self):
        from skill_agent.local_models import QWEN_MODEL

        config = self.build("--backend", "local")
        self.assertEqual(config["model"]["factory"], "skill_agent.local_models:LocalTransformersClient")
        self.assertEqual(config["model"]["options"]["model"], QWEN_MODEL)
        self.assertEqual((config["run"]["concurrency"], config["run"]["timeout_seconds"]), (1, 1800))
        with self.assertRaisesRegex(colab.SetupError, "--provider"):
            self.build("--backend", "local", "--provider", "alibaba")

    def test_data_selection_matches_documented_prepare_arguments(self):
        self.assertEqual(colab.data_selection("pilot", 5),
                         ("benchmarks/local-data/varex-dev-5", ["--dev-limit", "5"]))
        self.assertEqual(colab.data_selection("qwen-dev30", 30),
                         ("benchmarks/local-data/varex-dev-30", ["--dev-limit", "30"]))
        self.assertEqual(colab.data_selection("skill-requests", 2),
                         ("benchmarks/local-data/varex-skill-requests",
                          ["--task-ids", "FS5441-3E", "LS-275si"]))
        for limit in (0, 31, None):
            with self.subTest(limit=limit), self.assertRaises(colab.SetupError):
                colab.data_selection("pilot", limit)

    def test_learner_config_uses_the_example_models_with_this_run_and_service(self):
        from trace_learner.pipeline import load_learner_config

        opts = options("--limit", "10", "--conditions", "reduced", "--learn")
        run_config = colab.build_config(base(opts.experiment), opts, self.paths)
        example = json.loads((ROOT / colab.LEARNER_BASE).read_text(encoding="utf-8"))
        learner = colab.build_learner_config(example, opts, self.paths, run_config)
        # Incremental by default: one document at a time, the worker run by the learner itself.
        self.assertEqual((learner["mode"], learner["batch_size"]), ("incremental", 1))
        self.assertEqual(learner["run_config"], str(self.paths.configs / "mcrdr-test.json"))
        self.assertNotIn("collect_run", learner)
        self.assertEqual(colab.build_learner_config(example, options("--learn", "--batch-size", "5"), self.paths,
                                                    run_config)["batch_size"], 5)
        with self.assertRaisesRegex(colab.SetupError, "batch-size"):
            colab.build_learner_config(example, options("--learn", "--batch-size", "0"), self.paths, run_config)
        self.assertEqual(learner["skill_dir"], str(self.paths.workdir / "skills" / "pdf"))
        self.assertEqual(learner["output_dir"], str(self.paths.learning / "mcrdr-test"))
        self.assertEqual(learner["validation"]["service_url"], "http://127.0.0.1:8766")
        self.assertEqual(learner["validation"]["store"], str(self.paths.snapshots))
        self.assertEqual(learner["verifier"]["model"]["options"]["model"], "qwen/qwen3.7-plus")
        self.assertEqual(learner["merge"]["model"]["options"]["model"], "qwen/qwen3.7-plus")
        self.assertEqual(learner["judge"]["model"]["options"]["model"],
                         run_config["model"]["options"]["model"])  # checker = worker model
        path = Path(self.temp.name) / "learner.json"
        path.write_text(json.dumps(learner), encoding="utf-8")
        load_learner_config(path)
        with self.assertRaisesRegex(colab.SetupError, "reduced"):
            blind = options("--conditions", "full", "--learn")
            colab.build_learner_config(example, blind, self.paths,
                                       colab.build_config(base(blind.experiment), blind, self.paths))

    def test_heldout_batch_runs_only_held_out_documents(self):
        config = self.build("--heldout-batch", "30", "--conditions", "reduced")
        self.assertEqual((config["run"]["split"], config["run"]["limit"]), ("heldout", None))
        self.assertEqual(config["benchmark"]["options"]["data_dir"],
                         str(self.paths.workdir / "benchmarks/local-data/varex-heldout-30"))
        self.assertEqual(colab.data_selection("qwen-dev30", 30, 30),
                         ("benchmarks/local-data/varex-heldout-30", ["--heldout-limit", "30"]))
        self.assertLoads(config)
        with self.assertRaisesRegex(colab.SetupError, "remove --limit"):
            self.build("--heldout-batch", "30", "--limit", "10")
        with self.assertRaisesRegex(colab.SetupError, "skill-requests"):
            self.build("--experiment", "skill-requests", "--heldout-batch", "5")
        with self.assertRaisesRegex(colab.SetupError, "positive"):
            colab.data_selection("qwen-dev30", None, 0)

    def test_skill_version_serves_the_learned_tree_with_the_judge(self):
        version = "experiments/learning/learn/versions/v1/pdf"
        judge = {"factory": "skill_agent.models:OpenAICompatibleClient", "options": {"model": "judge"}}
        opts = options("--conditions", "reduced", "--skill-version", version)
        config = colab.build_config(base(opts.experiment), opts, self.paths, judge)
        skill = config["skill"]
        self.assertEqual(skill["receipt"], str(self.paths.experiments / "pdf-snapshot-mcrdr-test.json"))
        self.assertEqual(skill["full_markdown"], str(self.paths.workdir / "skills/pdf/initial.md"))
        self.assertEqual(skill["resolver"], {
            "factory": "skill_agent.learned_features:LearnedFeatureResolver",
            "options": {"base_url": "http://127.0.0.1:8766", "judge_model": judge}})
        self.assertEqual(colab.skill_source(opts, self.paths), self.paths.workdir / version)
        self.assertLoads(config)
        with self.assertRaisesRegex(colab.SetupError, "judge"):
            colab.build_config(base(opts.experiment), opts, self.paths)
        with self.assertRaisesRegex(colab.SetupError, "versions/vN/pdf"):
            wrong = options("--skill-version", "experiments/learning/learn/versions/v1")
            colab.build_config(base(wrong.experiment), wrong, self.paths, judge)
        with self.assertRaisesRegex(colab.SetupError, "skill condition"):
            blind = options("--conditions", "none", "--skill-version", version)
            colab.build_config(base(blind.experiment), blind, self.paths, judge)
        default = options()
        self.assertEqual(colab.receipt_path(default, self.paths), self.paths.receipt)

    def test_run_names_cannot_be_paths(self):
        for name in ("../escape", "a/b", ""):
            with self.subTest(name=name), self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                colab.parse_args(["--run-name", name])


class ColabSetupRunnerTests(unittest.TestCase):
    def test_command_output_is_streamed_and_failures_show_the_tail(self):
        lines = []
        runner = colab.Runner(out=lines.append)
        runner.run([sys.executable, "-c", "print('first'); print('second')"])
        self.assertIn("first", lines)
        self.assertIn("second", lines)
        with self.assertRaisesRegex(colab.SetupError, "boom"):
            runner.run([sys.executable, "-c", "import sys; print('boom'); sys.exit(3)"])


class ColabSetupDryRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workdir = Path(self.temp.name) / "work"

    def dry_run(self, *extra):
        opts = options("--workdir", str(self.workdir), "--dry-run", *extra)
        runner = colab.Runner(dry_run=True, out=lambda *args: None)
        with patch.object(colab, "missing_tools", return_value=["qpdf"]):
            config = colab.setup(opts, runner)
        return runner.records, config

    def test_dry_run_lists_steps_in_order_and_changes_nothing(self):
        records, config = self.dry_run("--experiment", "qwen-dev30")
        commands = [text for kind, text in records if kind in ("run", "start")]
        expected = ["apt-get update", "git clone --branch verifier-mcrdr", "-m venv", "pip install",
                    "skill_agent prepare", "rdr_service prepare", "rdr_service serve", "skill_agent doctor"]
        positions = [next(i for i, text in enumerate(commands) if fragment in text) for fragment in expected]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("--dev-limit 30", next(text for text in commands if "skill_agent prepare" in text))
        self.assertIn("--port 8766", next(text for text in commands if "rdr_service serve" in text))
        self.assertIn(("write", str(self.workdir.resolve() / "experiments/configs/mcrdr-test.json")), records)
        self.assertFalse(any("skill_agent run" in text for text in commands))
        self.assertFalse(self.workdir.exists())
        self.assertEqual(config["run"]["output_dir"], str(self.workdir.resolve() / "experiments/results/mcrdr-test"))

    def test_placeholder_model_settings_stop_setup(self):
        for experiment in ("pilot", "skill-requests"):
            with self.subTest(experiment=experiment):
                with self.assertRaisesRegex(colab.SetupError, "--base-url and --model"):
                    self.dry_run("--experiment", experiment)
                records, config = self.dry_run("--experiment", experiment, "--base-url",
                                               "https://openrouter.ai/api/v1", "--model", "qwen/qwen3.7-flash")
                self.assertEqual(config["model"]["options"]["model"], "qwen/qwen3.7-flash")

    def test_dry_run_with_run_flag_executes_run_inspect_and_summarize(self):
        records, _ = self.dry_run("--experiment", "pilot", "--run", "--resume",
                                  "--base-url", "https://openrouter.ai/api/v1", "--model", "qwen/qwen3.7-flash")
        commands = [text for kind, text in records if kind == "run"]
        self.assertTrue(any("skill_agent run" in text and text.endswith("--resume") for text in commands))
        self.assertTrue(any("inspect-skills" in text for text in commands))
        self.assertTrue(any("skill_agent summarize" in text for text in commands))

    def test_learn_dry_run_writes_the_learner_config_and_prints_its_stages(self):
        lines = []
        opts = options("--workdir", str(self.workdir), "--dry-run", "--limit", "10", "--conditions", "reduced",
                       "--learn")
        runner = colab.Runner(dry_run=True, out=lines.append)
        with patch.object(colab, "missing_tools", return_value=[]):
            colab.setup(opts, runner)
        learner_path = self.workdir.resolve() / "experiments/configs/mcrdr-test.learner.json"
        self.assertIn(("write", str(learner_path)), runner.records)
        self.assertIn("--dev-limit 10", next(text for kind, text in runner.records
                                             if kind == "run" and "skill_agent prepare" in text))
        printed = [line for line in lines if isinstance(line, str) and "-m trace_learner" in line]
        self.assertEqual([line.split("-m trace_learner ")[1].split()[0] for line in printed], ["learn", "report"])
        self.assertTrue(all(str(learner_path) in line for line in printed))
        # The learner runs the worker itself, so setup neither offers nor runs a whole-batch run.
        self.assertFalse(any("skill_agent run" in str(line) for line in lines))
        with self.assertRaisesRegex(colab.SetupError, "remove --run"):
            self.dry_run("--limit", "10", "--conditions", "reduced", "--learn", "--run")
        records, _ = self.dry_run()
        self.assertFalse(any("learner.json" in text for _, text in records))

    def test_learned_version_dry_run_snapshots_that_version_with_the_example_judge(self):
        version = self.workdir.resolve() / "experiments/learning/learn/versions/v1/pdf"
        records, config = self.dry_run("--conditions", "reduced", "--skill-version", str(version))
        prepare = next(text for kind, text in records if kind == "run" and "rdr_service prepare" in text)
        self.assertIn(str(version), prepare)
        self.assertIn("pdf-snapshot-mcrdr-test.json", prepare)
        example = json.loads((ROOT / colab.LEARNER_BASE).read_text(encoding="utf-8"))
        self.assertEqual(config["skill"]["resolver"]["options"]["judge_model"], example["judge"]["model"])
        records, _ = self.dry_run("--heldout-batch", "30", "--conditions", "reduced", "--learn")
        prepare = next(text for kind, text in records if kind == "run" and "skill_agent prepare" in text)
        self.assertIn("--heldout-limit 30", prepare)

    def test_skill_blind_dry_run_starts_no_service(self):
        records, _ = self.dry_run("--conditions", "none")
        self.assertFalse(any("rdr_service" in text for _, text in records))

    def test_local_backend_dry_run_starts_and_checks_the_model_server(self):
        records, _ = self.dry_run("--backend", "local")
        commands = [text for kind, text in records if kind in ("run", "start")]
        self.assertTrue(any("download.pytorch.org/whl/cu126" in text for text in commands))
        self.assertTrue(any("local-model" in text for text in commands))
        self.assertTrue(any("local_models serve" in text for text in commands))
        self.assertTrue(any("local_models check" in text for text in commands))

    def test_existing_results_require_resume(self):
        (self.workdir / ".git").mkdir(parents=True)  # an existing checkout from an earlier setup
        results = self.workdir / "experiments" / "results" / "mcrdr-test"
        results.mkdir(parents=True)
        with self.assertRaisesRegex(colab.SetupError, "--resume"):
            self.dry_run()
        records, _ = self.dry_run("--resume")
        self.assertTrue(records)


if __name__ == "__main__":
    unittest.main()
