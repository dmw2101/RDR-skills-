"""The root-only base tree: the whole skill at a lone root, every child added by learning."""

import json
import tempfile
import unittest
from pathlib import Path

from rdr_engine.skill_compiler import SkillBundle, SkillTreeCompiler
from trace_learner.pipeline import load_learner_config
from trace_learner.proposals import Proposal
from trace_learner.workspace import SkillWorkspace, copy_bundle

ROOT = Path(__file__).resolve().parents[2]
EXTRACT = {"operation": ["extract_content"], "source_kind": "unknown"}
SCANNED = {"operation": [], "source_kind": "scanned_image"}


class KeywordJudge:
    """A learned feature is true exactly when its keyword appears in the visible context."""

    def __init__(self, keywords):
        self.keywords = keywords

    async def judge(self, features, text):
        return {name: "true" if self.keywords[name] in text else "false" for name in features}, None


def proposal(**overrides):
    data = {"kind": "replace", "target_node_id": 0,
            "conditions": [{"new_feature": {"name": "lf_form_fields", "description": "The PDF has form fields."}}],
            "guidance_text": "Read form values with pypdf get_fields() before page text.", "rationale": "test"}
    data.update(overrides)
    return Proposal.model_validate_json(json.dumps(data))


def instruction_ids(catalog):
    return [f.id for f in sorted(catalog.fragments, key=lambda f: f.order) if f.role == "instruction"]


class RootOnlyBundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = copy_bundle(ROOT / "skills/pdf", Path(self.temporary.name) / "pdf")

    def test_reshape_puts_the_whole_skill_at_a_lone_root(self):
        tree = SkillBundle.reshape(self.directory, "root_only")
        [root] = tree.nodes
        self.assertEqual((root.id, list(root.conditions), list(root.children)), (0, [], []))
        manifest = json.loads((self.directory / "compiled/manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["tree_shape"], "root_only")
        engine, catalog = SkillBundle.load(self.directory)  # Hashes still check out.
        self.assertEqual(root.conclusion["fragments"], instruction_ids(catalog))
        # Every situation gets the same, complete guidance.
        workspace = SkillWorkspace(self.directory)
        texts = {workspace.render(workspace.case_for(case, "x")).markdown for case in (EXTRACT, SCANNED)}
        self.assertEqual(len(texts), 1)
        whole = texts.pop()
        for fragment_id in instruction_ids(catalog):
            text = next(f.text for f in catalog.fragments if f.id == fragment_id).strip().splitlines()[0]
            self.assertIn(text, whole)

    def test_flat_is_still_the_default_shape(self):
        engine, _ = SkillBundle.load(self.directory)
        self.assertEqual(len(engine.nodes), 8)
        with self.assertRaises(ValueError):
            SkillTreeCompiler("bushy")

    def test_a_learned_bundle_cannot_be_reshaped(self):
        (self.directory / "compiled/learned.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "trace-learned"):
            SkillBundle.reshape(self.directory, "root_only")


class RootOnlyWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        directory = copy_bundle(ROOT / "skills/pdf", Path(self.temporary.name) / "pdf")
        SkillBundle.reshape(directory, "root_only")
        self.workspace = SkillWorkspace(directory)
        self.whole = list(self.workspace.engine.nodes[0].conclusion["fragments"])
        self.judge = KeywordJudge({"lf_form_fields": "form fields", "lf_scanned": "scanned",
                                   "lf_rotated": "rotated"})
        for index in range(3):
            await self.workspace.accept(EXTRACT, f"Extract totals from invoice {index}")

    def text(self, context):
        return self.workspace.render(self.workspace.case_for(EXTRACT, context)).markdown

    def feature(self, name):
        return [{"new_feature": {"name": name, "description": f"The input shows {name}."}}]

    async def test_add_at_the_root_keeps_the_whole_skill(self):
        result = await self.workspace.apply(proposal(kind="add", target_node_id=0),
                                            EXTRACT, "The tool listed 12 form fields", self.judge, {})
        self.assertEqual(result.status, "revised", result.reason)
        self.assertIn("applied as replace", result.note)
        [child] = list(self.workspace.engine.nodes[0].children)
        self.assertEqual(self.workspace.engine.nodes[child].conclusion["fragments"],
                         self.whole + [result.fragment_id])
        with_rule, without = self.text("The tool listed 12 form fields"), self.text("Extract totals from invoice 0")
        self.assertIn("get_fields()", with_rule)
        self.assertNotIn("get_fields()", without)
        self.assertTrue(with_rule.startswith(without.rstrip()[:200]))  # The skill itself is unchanged.

    async def test_the_tree_grows_in_breadth_and_in_depth(self):
        # Breadth: two unrelated failures become two siblings under the root.
        first = await self.workspace.apply(proposal(target_node_id=0, conditions=self.feature("lf_form_fields")),
                                           EXTRACT, "12 form fields", self.judge, {})
        second = await self.workspace.apply(proposal(
            target_node_id=0, conditions=self.feature("lf_scanned"), guidance_text="OCR scanned pages first."),
            EXTRACT, "a scanned page", self.judge, {})
        self.assertEqual((first.status, second.status), ("revised", "revised"), (first.reason, second.reason))
        engine = self.workspace.engine
        self.assertEqual(list(engine.nodes[0].children), first.new_node_ids + second.new_node_ids)
        # Depth: a failure where the first exception fired refines that exception. The run's
        # judge said lf_form_fields was true at that step, so the exception fired there.
        [form_node] = first.new_node_ids
        deeper = await self.workspace.apply(proposal(
            target_node_id=form_node, conditions=self.feature("lf_rotated"),
            guidance_text="Rotate pages upright before reading fields."),
            {**EXTRACT, "lf_form_fields": "true"}, "12 form fields on rotated pages", self.judge, {})
        self.assertEqual(deeper.status, "revised", deeper.reason)
        self.assertEqual(list(self.workspace.engine.nodes[form_node].children), deeper.new_node_ids)
        deepest = self.text("12 form fields on rotated pages")
        self.assertIn("get_fields()", deepest)
        self.assertIn("Rotate pages upright", deepest)
        self.assertNotIn("Rotate pages upright", self.text("12 form fields"))
        self.workspace.save()
        SkillBundle.load(self.workspace.directory)

    async def test_stop_at_the_root_withholds_the_skill_only_where_it_holds(self):
        result = await self.workspace.apply(proposal(kind="stop", target_node_id=0, guidance_text=None),
                                            EXTRACT, "form fields everywhere", self.judge, {})
        self.assertEqual(result.status, "revised", result.reason)
        self.assertEqual(self.text("form fields everywhere"), "")
        self.assertNotEqual(self.text("Extract totals from invoice 1"), "")


class BaseTreeConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "learner.json"

    def load(self, **overrides):
        config = {"version": 1, "skill_dir": "skills/pdf", "output_dir": "out", "hooks": {}, "verifier": {},
                  "judge": {"model": {}}, "mode": "incremental", "run_config": "run.json",
                  "validation": {"service_url": "http://x", "store": "s"}, **overrides}
        self.path.write_text(json.dumps(config), encoding="utf-8")
        return load_learner_config(self.path)

    def test_base_tree_defaults_to_the_compiled_tree(self):
        self.assertEqual(self.load()["base_tree"], "compiled")
        self.assertEqual(self.load(base_tree="root_only")["base_tree"], "root_only")
        with self.assertRaisesRegex(ValueError, "base_tree"):
            self.load(base_tree="bushy")

    def test_root_only_needs_incremental_learning(self):
        with self.assertRaisesRegex(ValueError, "incremental"):
            self.load(base_tree="root_only", mode="batch", collect_run="run")


if __name__ == "__main__":
    unittest.main()
