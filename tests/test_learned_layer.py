import json
import shutil
import tempfile
import unittest
from pathlib import Path

from rdr_engine import Replace, Revise
from rdr_engine.learned import LearnedLayer, extend_schema, save_learned_bundle
from rdr_engine.skill_cases import SkillConditionEvaluator
from rdr_engine.skill_compiler import FeatureSchema, SkillBundle
from rdr_engine.skill_renderer import SkillRenderer
from rdr_service.backend import RDRBackend
from skill_agent.contracts import ResolveSkillRequest

ROOT = Path(__file__).resolve().parents[1]


class FlagAuthor:
    def generate_candidates(self, case, cornerstones):
        return [{"field": "lf_flag", "op": "eq", "value": "true"}]

    def select_condition(self, candidates, case, cornerstones, request, attempt):
        return list(candidates)


class LearnedLayerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.skill = Path(self.temporary.name) / "pdf"
        shutil.copytree(ROOT / "skills/pdf", self.skill)

    def learn(self, anchor="node"):
        """Replace node 2 with its fragments plus learned guidance when lf_flag is true."""
        manifest = json.loads((self.skill / "compiled/manifest.json").read_text())
        engine, catalog = SkillBundle.load(self.skill)
        layer = LearnedLayer(features={"lf_flag": {"description": "A visible flag."}})
        schema = extend_schema(FeatureSchema.model_validate(manifest["feature_schema"]), layer)
        node_fragments = engine.nodes[2].conclusion["fragments"]
        fragment = layer.add_fragment("LEARNED GUIDANCE", node_fragments[-1] if anchor == "node" else None,
                                      catalog, {"test": True})
        from rdr_engine import RDREngine
        engine = RDREngine.from_dict(engine.to_dict(), evaluator=SkillConditionEvaluator(schema),
                                     author=FlagAuthor())
        case = {"operation": ["extract_content"], "source_kind": "unknown", "lf_flag": "true"}
        result = engine.learn(engine.predict(case), Revise((Replace(2, {"fragments": node_fragments + [fragment.id]}),)))
        self.assertEqual(result.status, "revised")
        save_learned_bundle(self.skill, engine.to_dict(), layer, schema)
        return fragment, node_fragments

    def render(self, flag):
        engine, catalog = SkillBundle.load(self.skill)
        case = {"operation": ["extract_content"], "source_kind": "unknown", "lf_flag": flag}
        return SkillRenderer().render(engine.predict(case), engine, catalog)

    def test_learned_fragment_renders_after_its_anchor_only_when_the_rule_fires(self):
        fragment, node_fragments = self.learn()
        rendered = self.render("true")
        self.assertIn("\n\nLEARNED GUIDANCE\n", rendered.markdown)
        ids = list(rendered.selected_ids)
        self.assertEqual(ids.index(fragment.id), ids.index(node_fragments[-1]) + 1)
        for value in ("false", "unknown"):
            self.assertNotIn("LEARNED GUIDANCE", self.render(value).markdown)

    def test_unanchored_learned_fragment_renders_last(self):
        self.learn(anchor=None)
        self.assertTrue(self.render("true").markdown.endswith("LEARNED GUIDANCE\n"))

    def test_tampering_and_missing_layer_are_rejected(self):
        self.learn()
        learned = self.skill / "compiled/learned.json"
        original = learned.read_text()
        learned.write_text(original.replace("LEARNED GUIDANCE", "OTHER GUIDANCE"))
        with self.assertRaisesRegex(ValueError, "learned.json changed"):
            SkillBundle.load(self.skill)
        learned.unlink()
        with self.assertRaisesRegex(ValueError, "disagree"):
            SkillBundle.load(self.skill)

    def test_layer_validates_fragment_ids_and_feature_names(self):
        with self.assertRaises(ValueError):
            LearnedLayer(features={"flag": {"description": "missing lf_ prefix"}})
        with self.assertRaises(ValueError):
            LearnedLayer.model_validate({"fragments": [{"id": "learned_0000000000000000", "text": "x\n",
                                                       "anchor": None, "requires": [], "provenance": {}}]})

    def test_rebuild_refuses_to_drop_learned_rules_unless_asked(self):
        self.learn()
        schema = FeatureSchema.from_file(ROOT / "skills/feature_schemas.json", "pdf")
        ir = self.skill / "compiled/ir.json"
        with self.assertRaisesRegex(ValueError, "discard-learned"):
            SkillBundle.build(self.skill, ir_path=ir, feature_schema=schema, force=True)
        engine, catalog = SkillBundle.build(self.skill, ir_path=ir, feature_schema=schema, force=True,
                                            discard_learned=True)
        self.assertFalse((self.skill / "compiled/learned.json").exists())
        self.assertEqual(catalog.learned, ())
        SkillBundle.load(self.skill)

    def test_service_snapshots_learned_layer_and_reads_judged_values_from_context(self):
        self.learn()
        backend = RDRBackend(Path(self.temporary.name) / "store")
        receipt = backend.prepare(self.skill)
        (self.skill / "compiled/learned.json").unlink()  # Snapshots are independent of the source.
        def resolve(context):
            return backend.resolve(ResolveSkillRequest("pdf", "Extract the text", context, receipt["artifact_id"]))
        flagged = resolve({"lf_flag": "true"})
        self.assertIn("LEARNED GUIDANCE", flagged.markdown)
        self.assertEqual(flagged.metadata["learned_features"]["lf_flag"]["description"], "A visible flag.")
        self.assertNotIn("LEARNED GUIDANCE", resolve({}).markdown)


if __name__ == "__main__":
    unittest.main()
