from __future__ import annotations

import shutil
import tempfile
import unittest
from itertools import product
from pathlib import Path

from rdr_engine.skill_cases import SkillConditionEvaluator
from rdr_engine.skill_compiler import (
    FeatureSchema, FragmentCatalog, SkillBundle, SkillIR, SkillInput,
    SkillTreeCompiler,
)


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "alfworld"


class SkillCompilerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = SkillInput.from_markdown(SKILL / "initial.md")
        self.ir = SkillIR.model_validate_json((SKILL / "compiled" / "ir.json").read_text())
        self.schema = FeatureSchema.from_file(ROOT / "skills" / "feature_schemas.json", "alfworld")

    def test_fragments_keep_exact_text_and_table_dependencies(self) -> None:
        catalog = FragmentCatalog.from_ir(self.source, self.ir)
        heat_id = catalog.ids_for(self.ir.rules[4].source)[0]
        heat = next(fragment for fragment in catalog.fragments if fragment.id == heat_id)
        self.assertEqual(heat.text, self.source.lines[20])
        required = [fragment for fragment in catalog.fragments if fragment.id in heat.requires]
        self.assertIn("| Type | Goal | Key Steps |\n|------|------|-----------|\n", [f.text for f in required])
        self.assertTrue(all(fragment.role == "non_instruction" for fragment in required))

    def test_compile_rules_as_flat_mcrdr_tree_and_predict(self) -> None:
        catalog = FragmentCatalog.from_ir(self.source, self.ir)
        tree = SkillTreeCompiler().compile(self.ir, catalog, SkillConditionEvaluator(self.schema))
        nodes = tree.nodes
        self.assertEqual(len(nodes), 2 + len(self.ir.rules))
        self.assertEqual(nodes[0].children, tuple(range(1, len(nodes))))
        self.assertEqual(nodes[0].conclusion, {"fragments": []})
        self.assertEqual(nodes[1].conditions, ())
        self.assertEqual(nodes[1].conclusion, {"fragments": catalog.ids_for(self.ir.root)})
        self.assertTrue(all(not node.children and not node.stopping for node in nodes[1:]))
        heat = tree.predict({"task_type": ["heat_place"]})
        self.assertEqual(heat.fired, (0, 1, 6))
        self.assertEqual([item.node_id for item in heat.conclusions], [1, 6])
        self.assertEqual(heat.conclusions[1].conclusion,
                         {"fragments": catalog.ids_for(self.ir.rules[4].source)})

    def test_bundle_round_trip_and_hash_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "alfworld"
            (directory / "compiled").mkdir(parents=True)
            shutil.copyfile(SKILL / "initial.md", directory / "initial.md")
            ir_path = directory / "compiled" / "ir.json"
            shutil.copyfile(SKILL / "compiled" / "ir.json", ir_path)
            built, _ = SkillBundle.build(directory, ir_path=ir_path, feature_schema=self.schema)
            loaded, _ = SkillBundle.load(directory)
            case = {"task_type": ["heat_place"]}
            self.assertEqual(built.predict(case).trace, loaded.predict(case).trace)
            with (directory / "compiled" / "fragments.json").open("a") as stream:
                stream.write(" ")
            with self.assertRaisesRegex(ValueError, "fragments.json changed"):
                SkillBundle.load(directory)

    def test_many_rules_compile_without_duplication(self) -> None:
        extra = [
            self.ir.rules[0].model_copy(update={"id": f"extra_rule_{index}"}) for index in range(7)
        ]
        many = self.ir.model_copy(update={"rules": self.ir.rules + extra})
        catalog = FragmentCatalog.from_ir(self.source, many)
        tree = SkillTreeCompiler().compile(many, catalog, SkillConditionEvaluator(self.schema))
        self.assertEqual(len(tree.nodes), 2 + 13)
        prediction = tree.predict({"task_type": ["pick_place"]})
        self.assertEqual([item.node_id for item in prediction.conclusions], [1, 2, *range(8, 15)])

    def test_catalog_must_match_the_compiled_rules(self) -> None:
        catalog = FragmentCatalog.from_ir(self.source, self.ir)
        fewer = self.ir.model_copy(update={"rules": self.ir.rules[:-1]})
        with self.assertRaisesRegex(ValueError, "differ"):
            SkillTreeCompiler().compile(fewer, catalog, SkillConditionEvaluator(self.schema))

    def test_all_membership_combinations_follow_the_expected_path(self) -> None:
        catalog = FragmentCatalog.from_ir(self.source, self.ir)
        tree = SkillTreeCompiler().compile(self.ir, catalog, SkillConditionEvaluator(self.schema))
        for matches in product((False, True), repeat=len(self.ir.rules)):
            labels = [rule.when[0].value for rule, matched in zip(self.ir.rules, matches) if matched]
            with self.subTest(labels=labels):
                prediction = tree.predict({"task_type": labels})
                self.assertEqual([step.matched for step in prediction.trace], [True, True, *matches])
                self.assertEqual(
                    [item.node_id for item in prediction.conclusions],
                    [1, *(index + 2 for index, matched in enumerate(matches) if matched)],
                )

    def test_searchqa_placeholder_is_metadata(self) -> None:
        source = SkillInput.from_markdown(ROOT / "skills" / "searchqa" / "initial.md")
        ir = SkillIR.model_validate({
            "root": [], "rules": [],
            "excluded": [
                {"start": 1, "end": 1, "reason": "title"},
                {"start": 3, "end": 3, "reason": "placeholder"},
            ],
        })
        catalog = FragmentCatalog.from_ir(source, ir)
        self.assertTrue(all(fragment.role == "non_instruction" for fragment in catalog.fragments))
        tree = SkillTreeCompiler().compile(ir, catalog, SkillConditionEvaluator(FeatureSchema(features={})))
        self.assertEqual(tree.predict({}).distinct_conclusions, ({"fragments": []},))


if __name__ == "__main__":
    unittest.main()
