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

ROOT = Path(__file__).resolve().parents[1]
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

    def test_run_names_cannot_be_paths(self):
        for name in ("../escape", "a/b", ""):
            with self.subTest(name=name), self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                colab.parse_args(["--run-name", name])


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
        expected = ["apt-get update", "git clone --branch MCRDR-testing", "-m venv", "pip install",
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
