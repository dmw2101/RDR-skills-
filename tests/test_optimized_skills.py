import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rdr_engine.skill_cases import CUE_FEATURES, SkillConditionEvaluator, build_case
from rdr_engine.skill_compiler import FeatureSchema

ROOT = Path(__file__).resolve().parents[1]
FEATURES = ROOT / "skills" / "feature_schemas.json"
CHECKPOINTS = {"alfworld_gpt55": "alfworld", "docvqa_gpt55": "docvqa", "livemath_gpt55": "livemath",
               "officeqa_gpt55": "officeqa", "searchqa_gpt55": "searchqa",
               "spreadsheetbench_gpt55": "spreadsheetbench"}

SPEC = importlib.util.spec_from_file_location("convert_skills", ROOT / "scripts" / "convert_skills.py")
convert = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = convert
SPEC.loader.exec_module(convert)


def schema(skill):
    return FeatureSchema.from_file(FEATURES, skill)


class OptimizedSkillSourceTests(unittest.TestCase):
    def test_sources_are_unedited_copies_with_recorded_hashes(self):
        catalog = {entry["id"]: entry for entry in json.loads((ROOT / "skills/sources.json").read_text(encoding="utf-8"))["skills"]}
        for skill, checkpoint in CHECKPOINTS.items():
            with self.subTest(skill=skill):
                source = (ROOT / "skills" / skill / "initial.md").read_bytes()
                vendored = (ROOT / "benchmarks/skillopt/ckpt" / checkpoint / "gpt5.5_skill.md").read_bytes()
                self.assertEqual(source, vendored.replace(b"\r\n", b"\n"))  # checkouts may use CRLF
                digest = hashlib.sha256(source).hexdigest()
                provenance = json.loads((ROOT / "skills" / skill / "source.json").read_text(encoding="utf-8"))
                self.assertEqual(provenance["sha256"], digest)
                self.assertEqual(provenance["vendored_path"], f"benchmarks/skillopt/ckpt/{checkpoint}/gpt5.5_skill.md")
                self.assertEqual(catalog[skill]["sha256"], digest)

    def test_schemas_load_and_heuristics_cover_exactly_the_schema_values(self):
        self.assertEqual(set(CUE_FEATURES) | {"alfworld_gpt55"}, set(CHECKPOINTS))
        for skill, (field, cues) in CUE_FEATURES.items():
            with self.subTest(skill=skill):
                spec = schema(skill).features[field]
                self.assertTrue(spec.multiple)
                self.assertEqual(list(cues), spec.values)
        alfworld = schema("alfworld_gpt55").features
        self.assertTrue(alfworld["task_type"].multiple)
        self.assertFalse(alfworld["target_object"].multiple)
        self.assertIn("unknown", alfworld["target_object"].values)

    def test_example_cases_validate(self):
        for skill in CHECKPOINTS:
            files = sorted((ROOT / "skills" / skill / "cases").glob("*.json"))
            self.assertGreaterEqual(len(files), 2, skill)
            evaluator = SkillConditionEvaluator(schema(skill))
            for path in files:
                with self.subTest(case=f"{skill}/{path.name}"):
                    evaluator.validate_case(json.loads(path.read_text(encoding="utf-8-sig")))


class OptimizedSkillHeuristicTests(unittest.TestCase):
    EXPECTED = [
        ("alfworld_gpt55", "put a clean pan in countertop", {"task_type": ["clean_place"], "target_object": "pan"}),
        ("alfworld_gpt55", "clean some kettle and put it in cabinet",
         {"task_type": ["clean_place"], "target_object": "kettle_or_teapot"}),
        ("alfworld_gpt55", "put two pencils in drawer",
         {"task_type": ["pick_two_place"], "target_object": "office_or_bedroom_item"}),
        ("alfworld_gpt55", "", {"task_type": [], "target_object": "unknown"}),
        ("docvqa_gpt55", "What is the total amount in the table?",
         {"question_cues": ["value_or_count", "table_lookup"]}),
        ("docvqa_gpt55", "Who signed the form?", {"question_cues": ["name_or_quote", "form_or_label"]}),
        ("docvqa_gpt55", "What is the page number for Introduction in the table of contents?",
         {"question_cues": ["value_or_count", "list_or_index"]}),
        ("livemath_gpt55", "B) One of the remaining options is correct, but a stronger result can be proven.",
         {"option_cues": ["stronger_result_option", "nested_strength"]}),
        ("livemath_gpt55", "The bound holds if and only if mu <= mu_0",
         {"option_cues": ["quantifier_or_equivalence", "threshold_or_inequality"]}),
        ("officeqa_gpt55", "What was the standard deviation of monthly receipts from 1950 to 1955?",
         {"question_cues": ["period_range", "statistical_calculation"]}),
        ("officeqa_gpt55", "How much new cash was raised in the March 1962 offering, in millions?",
         {"question_cues": ["unit_or_currency_conversion", "treasury_financing"]}),
        ("searchqa_gpt55", "Crossword: Lake in Florida (10)",
         {"clue_cues": ["crossword_or_letter_count", "geographic_feature"]}),
        ("searchqa_gpt55", 'This man wrote "The Raven"',
         {"clue_cues": ["this_type_clue", "quotation_or_lyric", "creative_work", "person_answer"]}),
        ("spreadsheetbench_gpt55", "Write a SUMIFS formula to total sales by month",
         {"operation": ["formula_or_macro", "date_or_time", "aggregation_or_summary"]}),
        ("spreadsheetbench_gpt55", 'Highlight blank cells that start with "X"',
         {"operation": ["formatting", "blank_handling", "text_matching"]}),
        ("spreadsheetbench_gpt55", "", {"operation": []}),
    ]

    def test_task_heuristics(self):
        for skill, task, expected in self.EXPECTED:
            with self.subTest(skill=skill, task=task):
                self.assertEqual(build_case(skill, task, None, schema(skill)), expected)

    def test_visible_inputs_override_heuristics(self):
        case = build_case("docvqa_gpt55", "What is the total?", {"question_cues": ["form_or_label"]},
                          schema("docvqa_gpt55"))
        self.assertEqual(case, {"question_cues": ["form_or_label"]})


class ConvertSkillsScriptTests(unittest.TestCase):
    def run_script(self, *argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            try:
                code = convert.main(list(argv))
            except SystemExit as exit:
                code = exit.code
        return code, output.getvalue()

    def test_dry_run_plans_every_optimized_skill_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            skills = Path(temporary)
            for skill in CHECKPOINTS:
                (skills / skill).mkdir()
                shutil.copyfile(ROOT / "skills" / skill / "initial.md", skills / skill / "initial.md")
            shutil.copyfile(FEATURES, skills / "feature_schemas.json")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("GEMINI_API_KEY", None)
                code, output = self.run_script("--skills-dir", str(skills), "--dry-run")
            self.assertEqual(code, 0)
            for skill in CHECKPOINTS:
                self.assertIn(f"== {skill}", output)
                self.assertIn(f"| {skill} | - | - | - | planned (dry run) |", output)
                self.assertFalse((skills / skill / "compiled").exists())
        self.assertEqual(output.count("extract: rdr_engine.skill_compiler"), 6)
        self.assertEqual(output.count("--model gemini-3.8-flash"), 6)
        self.assertEqual(convert.OPTIMIZED_SKILLS, tuple(CHECKPOINTS))

    def test_extraction_requires_a_key(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GEMINI_API_KEY", None)
            code, _ = self.run_script("--skills", "docvqa_gpt55")
        self.assertEqual(code, 1)

    def test_compile_only_builds_skips_and_reports(self):
        with tempfile.TemporaryDirectory() as temporary:
            skills = Path(temporary)
            (skills / "alfworld" / "compiled").mkdir(parents=True)
            shutil.copyfile(ROOT / "skills/alfworld/initial.md", skills / "alfworld/initial.md")
            shutil.copyfile(ROOT / "skills/alfworld/compiled/ir.json",
                            skills / "alfworld/compiled/ir.20260101-000000000.json")
            shutil.copyfile(FEATURES, skills / "feature_schemas.json")
            (skills / "empty").mkdir()
            (skills / "empty" / "initial.md").write_text("# Empty\n", encoding="utf-8")
            code, output = self.run_script("--skills-dir", str(skills), "--skills", "alfworld", "empty",
                                           "--compile-only")
            self.assertEqual(code, 1)  # "empty" has no saved rule list
            self.assertIn("| alfworld | 6 | 8 | 19 | converted |", output)
            self.assertIn("| empty | - | - | - | failed: no saved compiled/ir.<timestamp>.json to compile |", output)
            self.assertTrue((skills / "alfworld/compiled/tree_visualisation.html").exists())
            code, output = self.run_script("--skills-dir", str(skills), "--skills", "alfworld", "--compile-only")
            self.assertEqual(code, 0)
            self.assertIn("skipped (already compiled; --force rebuilds)", output)


if __name__ == "__main__":
    unittest.main()
