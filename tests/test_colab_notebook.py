import ast
import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = json.loads((ROOT / "notebooks" / "mcrdr_varex_colab.ipynb").read_text(encoding="utf-8"))
CODE = ["".join(cell["source"]) for cell in NOTEBOOK["cells"] if cell["cell_type"] == "code"]


def cell_defining(name):
    return next(source for source in CODE if f"{name} = " in source)


class ColabNotebookTests(unittest.TestCase):
    def test_notebook_is_clean_and_every_code_cell_parses(self):
        self.assertEqual(NOTEBOOK["nbformat"], 4)
        for cell in NOTEBOOK["cells"]:
            if cell["cell_type"] == "code":
                self.assertEqual((cell["outputs"], cell["execution_count"]), ([], None))
        for source in CODE:
            ast.parse(source)
        self.assertFalse(any("sk-or-" in source for source in CODE))  # no key ever stored

    def test_settings_match_the_setup_script_and_report_template(self):
        settings = {}
        exec(cell_defining("BRANCH"), settings)
        spec = importlib.util.spec_from_file_location("setup_mcrdr_colab", ROOT / "scripts" / "setup_mcrdr_colab.py")
        script = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = script
        spec.loader.exec_module(script)
        self.assertEqual(settings["BRANCH"], script.BRANCH)
        self.assertEqual(settings["WORKDIR"], script.WORKDIR)
        self.assertIn(settings["EXPERIMENT"], script.EXPERIMENTS)
        template = json.loads((ROOT / "examples" / "agent" / "report.template.json").read_text(encoding="utf-8"))
        for number in (1, 2, 3):
            self.assertEqual(template["series"]["MCRDR"][f"Run {number}"][0],
                             "results/" + settings["RUN_NAME"].format(number))

    def test_deck_numbers_are_internally_consistent(self):
        values = {}
        exec(cell_defining("DECK"), values)
        deck = values["DECK"]
        for condition, correct in deck["fields_correct"].items():
            pooled = deck["field_exact_match"]["Pooled"][condition]
            self.assertAlmostEqual(correct / deck["fields_total"] * 100, pooled, delta=.005)
            per_run = [round(deck["field_exact_match"][f"Run {n}"][condition] * 265 / 100) for n in (1, 2, 3)]
            self.assertEqual(sum(per_run), correct)
        for run in ("Run 1", "Run 2", "Run 3", "Pooled"):
            em = deck["field_exact_match"][run]
            self.assertAlmostEqual(em["reduced"] - em["full"], deck["reduced_minus_full_pp"][run], delta=.015)


class LearningNotebookTests(unittest.TestCase):
    notebook = json.loads((ROOT / "notebooks" / "verifier_learning_colab.ipynb").read_text(encoding="utf-8"))
    code = ["".join(cell["source"]) for cell in notebook["cells"] if cell["cell_type"] == "code"]

    def test_notebook_is_clean_and_every_code_cell_parses(self):
        self.assertEqual(self.notebook["nbformat"], 4)
        for cell in self.notebook["cells"]:
            self.assertTrue(cell["id"])
            if cell["cell_type"] == "code":
                self.assertEqual((cell["outputs"], cell["execution_count"]), ([], None))
        for source in self.code:
            ast.parse(source)
        self.assertFalse(any("sk-or-" in source for source in self.code))

    def test_settings_give_a_valid_learning_setup(self):
        settings = {}
        exec(next(source for source in self.code if "BRANCH = " in source), settings)
        spec = importlib.util.spec_from_file_location("setup_mcrdr_colab", ROOT / "scripts" / "setup_mcrdr_colab.py")
        script = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = script
        spec.loader.exec_module(script)
        self.assertEqual((settings["BRANCH"], settings["WORKDIR"]), (script.BRANCH, script.WORKDIR))
        options = script.parse_args(["--experiment", settings["EXPERIMENT"], "--limit", str(settings["DOCUMENTS"]),
                                     "--conditions", "reduced", "--run-name", settings["RUN_NAME"], "--learn"])
        self.assertEqual(script.data_selection(options.experiment, options.limit)[1],
                         ["--dev-limit", str(settings["DOCUMENTS"])])
        stages = [line for source in self.code for line in source.splitlines() if line.startswith("learn(")]
        self.assertEqual(stages, [f'learn("{stage}")' for stage in (*script.LEARNER_STAGES, "report")])


if __name__ == "__main__":
    unittest.main()
