from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rdr_engine import Accept, RDREngine, Replace, Revise, StalePredictionError, Stop
from rdr_engine.skill_cases import SkillConditionEvaluator, build_case
from rdr_engine.skill_compiler import (
    FeatureSchema, FragmentCatalog, SkillIR, SkillInput, SkillTreeCompiler,
)
from rdr_engine.skill_renderer import SkillRenderer


ROOT = Path(__file__).resolve().parents[1]


class MembershipAuthor:
    """Propose one task-type membership the case has and the cornerstones lack."""

    def generate_candidates(self, case, cornerstones):
        return [{"field": "task_type", "op": "contains", "value": v} for v in case["task_type"]]

    def select_condition(self, candidates, case, cornerstones, request, attempt):
        return list(candidates[:1])


class SkillRendererTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = ROOT / "skills" / "alfworld"
        source = SkillInput.from_markdown(directory / "initial.md")
        ir = SkillIR.model_validate_json((directory / "compiled" / "ir.json").read_text())
        self.schema = FeatureSchema.from_file(ROOT / "skills" / "feature_schemas.json", "alfworld")
        self.catalog = FragmentCatalog.from_ir(source, ir)
        self.engine = SkillTreeCompiler().compile(ir, self.catalog, SkillConditionEvaluator(self.schema))

    def learner(self) -> RDREngine:
        return RDREngine.from_dict(
            self.engine.to_dict(), evaluator=SkillConditionEvaluator(self.schema),
            author=MembershipAuthor(),
        )

    def render(self, engine: RDREngine, labels: list[str]) -> str:
        return SkillRenderer().render(
            engine.predict({"task_type": labels}), engine, self.catalog
        ).markdown

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
        snapshot["nodes"][1]["conclusion"]["fragments"] = ["does_not_exist"]
        altered = RDREngine.from_dict(snapshot, evaluator=SkillConditionEvaluator(self.schema))
        with self.assertRaisesRegex(ValueError, "missing fragment"):
            SkillRenderer().render(altered.predict({"task_type": []}), altered, self.catalog)

    def test_replace_on_general_guidance_drops_a_fragment_only_where_it_applies(self) -> None:
        engine = self.learner()
        general = engine.nodes[1].conclusion["fragments"]
        by_id = {fragment.id: fragment for fragment in self.catalog.fragments}
        dropped = next(f for f in general if "Only choose admissible actions" in by_id[f].text)
        prediction = engine.predict({"task_type": ["heat_place"]})
        result = engine.learn(prediction, Revise((
            Replace(1, {"fragments": [f for f in general if f != dropped]}),
        )))
        self.assertEqual(result.status, "revised")
        self.assertNotIn("**Only choose admissible actions**", self.render(engine, ["heat_place"]))
        self.assertIn("| Heat & Place |", self.render(engine, ["heat_place"]))
        self.assertIn("**Only choose admissible actions**", self.render(engine, ["cool_place"]))

    def test_stop_on_a_rule_removes_its_row_only_for_matching_cases(self) -> None:
        engine = self.learner()
        prediction = engine.predict({"task_type": ["clean_place", "heat_place"]})
        heat = next(item.node_id for item in prediction.conclusions
                    if item.node_id != 1 and "heat_place" in str(engine.nodes[item.node_id].conditions))
        result = engine.learn(prediction, Revise((Stop(heat),)))
        self.assertEqual(result.status, "revised")
        self.assertTrue(engine.nodes[result.new_node_ids[0]].stopping)
        both = self.render(engine, ["clean_place", "heat_place"])
        self.assertIn("| Clean & Place |", both)
        self.assertNotIn("| Heat & Place |", both)
        self.assertIn("| Heat & Place |", self.render(engine, ["heat_place"]))


if __name__ == "__main__":
    unittest.main()
