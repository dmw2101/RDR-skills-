from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rdr_engine import Accept, RDREngine, StalePredictionError
from rdr_engine.skill_cases import SkillConditionEvaluator, build_case
from rdr_engine.skill_compiler import (
    FeatureSchema, FragmentCatalog, SkillIR, SkillInput, SkillTreeCompiler,
)
from rdr_engine.skill_renderer import SkillRenderer


ROOT = Path(__file__).resolve().parents[1]


class SkillRendererTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = ROOT / "skills" / "alfworld"
        source = SkillInput.from_markdown(directory / "initial.md")
        ir = SkillIR.model_validate_json((directory / "compiled" / "ir.json").read_text())
        self.schema = FeatureSchema.from_file(ROOT / "skills" / "feature_schemas.json", "alfworld")
        self.catalog = FragmentCatalog.from_ir(source, ir)
        self.engine = SkillTreeCompiler().compile(ir, self.catalog, SkillConditionEvaluator(self.schema))

    def test_heat_case_renders_one_row_with_table_header_and_general_guidance(self) -> None:
        prediction = self.engine.predict({"task_type": ["heat_place"]})
        rendered = SkillRenderer().render(prediction, self.engine, self.catalog)
        self.assertIn("| Type | Goal | Key Steps |\n|------|------|-----------|\n| Heat & Place |", rendered.markdown)
        self.assertNotIn("| Cool & Place |", rendered.markdown)
        self.assertIn("**Only choose admissible actions**", rendered.markdown)
        self.assertLess(rendered.markdown.index("## Task Types"), rendered.markdown.index("## General Principles"))

    def test_unknown_case_renders_root_only(self) -> None:
        rendered = SkillRenderer().render(
            self.engine.predict({"task_type": []}), self.engine, self.catalog
        )
        self.assertNotIn("## Task Types", rendered.markdown)
        self.assertIn("## General Principles", rendered.markdown)

    def test_task_builder_is_conservative_and_overrideable(self) -> None:
        self.assertEqual(
            build_case("alfworld", "Heat the apple and put it in the bowl", None, self.schema),
            {"task_type": ["heat_place"]},
        )
        self.assertEqual(
            build_case("alfworld", "Find the thing", {"task_type": ["cool_place"]}, self.schema),
            {"task_type": ["cool_place"]},
        )
        self.assertEqual(
            build_case("alfworld", "Find the thing", None, self.schema),
            {"task_type": []},
        )

    def test_simultaneous_conditions_both_contribute(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source_file = Path(temp) / "initial.md"
            source_file.write_text("# Demo\nAlways do A.\nIf first, do B.\nIf second, do C.\n")
            source = SkillInput.from_markdown(source_file)
            ir = SkillIR.model_validate({
                "root": [{"start": 2, "end": 2}],
                "rules": [
                    {"id": "first", "when": [{"field": "first", "op": "eq", "value": "yes"}],
                     "source": [{"start": 3, "end": 3}]},
                    {"id": "second", "when": [{"field": "second", "op": "eq", "value": "yes"}],
                     "source": [{"start": 4, "end": 4}]},
                ],
                "excluded": [{"start": 1, "end": 1, "reason": "title"}],
            })
            schema = FeatureSchema.model_validate({"features": {
                "first": {"description": "First", "values": ["yes", "no"]},
                "second": {"description": "Second", "values": ["yes", "no"]},
            }})
            catalog = FragmentCatalog.from_ir(source, ir)
            tree = SkillTreeCompiler().compile(ir, catalog, SkillConditionEvaluator(schema))
            rendered = SkillRenderer().render(
                tree.predict({"first": "yes", "second": "yes"}), tree, catalog
            )
            self.assertEqual(rendered.markdown, "# Demo\nAlways do A.\nIf first, do B.\nIf second, do C.\n")

    def test_stale_prediction_and_missing_fragment_fail(self) -> None:
        prediction = self.engine.predict({"task_type": ["heat_place"]})
        self.engine.learn(prediction, Accept())
        with self.assertRaises(StalePredictionError):
            SkillRenderer().render(prediction, self.engine, self.catalog)

        snapshot = self.engine.to_dict()
        snapshot["nodes"][0]["conclusion"]["add"] = ["does_not_exist"]
        altered = RDREngine.from_dict(snapshot, evaluator=SkillConditionEvaluator(self.schema))
        with self.assertRaisesRegex(ValueError, "missing fragment"):
            SkillRenderer().render(altered.predict({"task_type": []}), altered, self.catalog)


if __name__ == "__main__":
    unittest.main()
