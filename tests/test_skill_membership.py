from __future__ import annotations

import json
import runpy
import tempfile
import unittest
from pathlib import Path

from rdr_engine.skill_cases import SkillConditionEvaluator
from rdr_engine.skill_compiler import (
    FeatureSchema, Predicate, SkillIRValidator, SkillInput, SkillRuleExtractor,
)


ROOT = Path(__file__).resolve().parents[1]


class MembershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = FeatureSchema.model_validate({"features": {
            "operation": {"description": "Requested operations", "values": ["a", "b"], "multiple": True},
            "source_kind": {"description": "Input type", "values": ["text", "scan"]},
        }})
        self.evaluator = SkillConditionEvaluator(self.schema)

    def test_membership_validation_and_scalar_predicates(self) -> None:
        condition = {"field": "operation", "op": "contains", "value": "a"}
        for values, expected in (([], False), (["b"], False), (["a", "b"], True)):
            self.assertEqual(self.evaluator.evaluate(condition, {"operation": values}), expected)
        for invalid in ("a", ["a", "a"], ["other"], [True], {}, None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.evaluator.validate_case({"operation": invalid}, {"operation"})
        for op, value in (("eq", "a"), ("in", ["a", "b"])):
            with self.assertRaisesRegex(ValueError, "use contains"):
                self.evaluator.evaluate({"field": "operation", "op": op, "value": value}, {"operation": ["a"]})
        self.assertTrue(self.evaluator.evaluate(
            {"field": "source_kind", "op": "in", "value": ["text", "scan"]}, {"source_kind": "scan"}
        ))
        with self.assertRaises(ValueError):
            self.evaluator.evaluate(
                {"field": "source_kind", "op": "contains", "value": "scan"}, {"source_kind": "scan"}
            )
        with self.assertRaises(ValueError):
            Predicate.model_validate({"field": "operation", "op": "contains", "value": ["a"]})

    def test_extractor_accepts_two_memberships_on_the_same_feature(self) -> None:
        response = {
            "root": [{"start": 1, "end": 1}],
            "rules": [{"id": "both_operations", "when": [
                {"field": "operation", "op": "contains", "value": "a"},
                {"field": "operation", "op": "contains", "value": "b"},
            ], "source": [{"start": 2, "end": 2}]}],
            "excluded": [],
        }

        class Model:
            def generate(self, **kwargs: object) -> str:
                self.request = kwargs
                return json.dumps(response)

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "initial.md"
            path.write_text("Always do this.\nIf a and b apply, do both.\n", encoding="utf-8")
            source = SkillInput.from_markdown(path)
            model = Model()
            ir = SkillRuleExtractor(model).extract(source, self.schema)
            self.assertEqual(SkillIRValidator().validate(ir, source, self.schema), [])
            self.assertIn("contains", model.request["response_schema"]["$defs"]["Predicate"]["properties"]["op"]["enum"])
            for field, op, value in (("operation", "eq", "a"), ("source_kind", "contains", "scan")):
                invalid = ir.model_copy(update={"rules": [ir.rules[0].model_copy(update={
                    "when": [Predicate(field=field, op=op, value=value)],
                })]})
                issues = SkillIRValidator().validate(invalid, source, self.schema)
                self.assertIn("invalid_feature_operator", [issue.code for issue in issues])

    def test_viewer_distinguishes_compatible_labels_and_real_conflicts(self) -> None:
        viewer = runpy.run_path(str(ROOT / "skills" / "visualise_tree.py"))

        def node(node_id: int, condition: tuple | None, *children: int, stopping: bool = False) -> dict:
            conditions = [] if condition is None else [
                {"field": condition[0], "op": condition[1], "value": condition[2]},
            ]
            return {"id": node_id, "conditions": conditions, "conclusion": None if stopping else {"fragments": []},
                    "stopping": stopping, "evidence": [], "children": list(children)}

        nodes = {
            0: node(0, None, 1, 4, 5),
            1: node(1, ("operation", "contains", "a"), 2, 3),
            2: node(2, ("operation", "contains", "b")),          # a and b can coexist.
            3: node(3, ("operation", "contains", "a"), stopping=True),  # a already required.
            4: node(4, ("operation", "contains", "c")),          # not in the domain.
            5: node(5, ("source_kind", "eq", "text"), 6),
            6: node(6, ("source_kind", "eq", "scan"), 7),        # contradicts its parent.
            7: node(7, None),
        }
        analysis, warnings = viewer["analyse"](
            nodes, 0, {"operation": ["a", "b"], "source_kind": ["text", "scan"]}
        )
        self.assertEqual(warnings, [])
        self.assertTrue(analysis[2].can_match)
        self.assertEqual(analysis[2].path, [(0, True), (1, True)])
        self.assertFalse(analysis[3].can_fail)
        self.assertIn("Always true when reached", analysis[3].notes[0])
        self.assertFalse(analysis[4].can_match)
        self.assertEqual(analysis[6].status, "live")
        self.assertFalse(analysis[6].can_match)
        self.assertEqual(analysis[7].status, "dead")
        payload = viewer["build_payload"](
            Path("tree.json"), {"root_id": 0}, nodes, analysis, {}, {}, [], False, "t"
        )
        self.assertEqual(payload["stats"]["stops"], 1)
        # Node 1 always has a firing child when it fires, so it cannot report.
        self.assertEqual(payload["stats"]["terminals"], 3)


if __name__ == "__main__":
    unittest.main()
