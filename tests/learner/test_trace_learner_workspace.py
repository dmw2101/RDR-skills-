import json
import tempfile
import unittest
from pathlib import Path

from rdr_engine.skill_compiler import SkillBundle
from trace_learner.proposals import Diagnosis, Proposal
from trace_learner.workspace import SkillWorkspace, copy_bundle

ROOT = Path(__file__).resolve().parents[2]
EXTRACT = {"operation": ["extract_content"], "source_kind": "unknown"}


class KeywordJudge:
    """A learned feature is true exactly when its keyword appears in the visible context."""

    def __init__(self, keywords):
        self.keywords, self.calls = keywords, []

    async def judge(self, features, text):
        self.calls.append((sorted(features), text))
        return {name: "true" if self.keywords[name] in text else "false" for name in features}, None


def proposal(**overrides):
    data = {"kind": "replace", "target_node_id": 2,
            "conditions": [{"new_feature": {"name": "lf_form_fields", "description": "The PDF has form fields."}}],
            "guidance_text": "Read form values with pypdf get_fields() before page text.", "rationale": "test"}
    data.update(overrides)
    return Proposal.model_validate_json(json.dumps(data))


class WorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = SkillWorkspace(copy_bundle(ROOT / "skills/pdf", Path(self.temporary.name) / "pdf"))
        self.judge = KeywordJudge({"lf_form_fields": "form fields", "lf_scanned": "scanned"})
        for index in range(3):
            await self.workspace.accept(EXTRACT, f"Extract totals from invoice {index}")

    def text(self, case, context):
        return self.workspace.render(self.workspace.case_for(case, context)).markdown

    async def test_replace_adds_an_exception_only_where_the_new_feature_holds(self):
        result = await self.workspace.apply(proposal(), EXTRACT, "The tool listed 12 form fields", self.judge, {})
        self.assertEqual(result.status, "revised", result.reason)
        self.assertEqual(result.parent_id, 2)
        # The failing context plus every cornerstone reaching node 2 was judged, once each.
        self.assertEqual(len(self.judge.calls), 4)
        self.assertIn("get_fields()", self.text(EXTRACT, "The tool listed 12 form fields"))
        self.assertNotIn("get_fields()", self.text(EXTRACT, "Extract totals from invoice 0"))
        # Replace keeps the parent's guidance alongside the exception.
        self.assertIn("pdfplumber", self.text(EXTRACT, "The tool listed 12 form fields"))
        self.workspace.save()
        reloaded = SkillWorkspace(self.workspace.directory)
        self.assertEqual(reloaded.summary(), self.workspace.summary())
        SkillBundle.load(self.workspace.directory)

    async def test_add_alternative_at_root_with_an_existing_feature(self):
        scanned = {"operation": [], "source_kind": "scanned_image"}
        result = await self.workspace.apply(proposal(
            kind="add", target_node_id=0,
            conditions=[{"field": "source_kind", "op": "eq", "value": "mixed"}],
            guidance_text="For mixed PDFs, OCR only the image pages."), {**scanned, "source_kind": "mixed"},
            "a mixed PDF", self.judge, {})
        self.assertEqual(result.status, "revised", result.reason)
        self.assertEqual(self.judge.calls, [])
        self.assertIn("OCR only the image pages", self.text({"operation": [], "source_kind": "mixed"}, "x"))
        self.assertNotIn("OCR only the image pages", self.text(scanned, "y"))

    async def test_stop_withholds_guidance_in_the_matching_situation(self):
        fragments = self.workspace.engine.nodes[2].conclusion["fragments"]
        catalog = {f.id: f.text for f in self.workspace.catalog.fragments}
        marker = catalog[fragments[-1]].strip().splitlines()[0]
        result = await self.workspace.apply(proposal(kind="stop", guidance_text=None),
                                            EXTRACT, "form fields everywhere", self.judge, {})
        self.assertEqual(result.status, "revised", result.reason)
        self.assertNotIn(marker, self.text(EXTRACT, "form fields everywhere"))
        self.assertIn(marker, self.text(EXTRACT, "Extract totals from invoice 1"))

    async def test_condition_that_also_matches_a_cornerstone_changes_nothing(self):
        before = (self.workspace.summary(), self.workspace.engine.to_dict(), self.workspace.layer.model_dump())
        broad = proposal(conditions=[{"new_feature": {"name": "lf_form_fields", "description": "Any PDF."}}])
        judge = KeywordJudge({"lf_form_fields": "Extract"})  # True for the cornerstones too.
        result = await self.workspace.apply(broad, EXTRACT, "Extract the form", judge, {})
        self.assertEqual(result.status, "unresolved")
        self.assertIn("cornerstone", result.reason)
        self.assertEqual((self.workspace.summary(), self.workspace.engine.to_dict(),
                          self.workspace.layer.model_dump()), before)

    async def test_rerun_mode_lets_a_rule_change_matching_successes_and_reports_them(self):
        judge = KeywordJudge({"lf_form_fields": "invoice 1"})  # True for one stored success too.
        broad = proposal(conditions=[{"new_feature": {"name": "lf_form_fields", "description": "Invoice 1."}}])
        failing = "Extract the form of invoice 1 again"
        result = await self.workspace.apply(broad, EXTRACT, failing, judge, {}, changed_successes="rerun")
        self.assertEqual(result.status, "revised", result.reason)
        changed = self.workspace.case_for(EXTRACT, "Extract totals from invoice 1")
        self.assertEqual(result.changed, [changed[self.context_key]])
        self.assertEqual(result.judged, {"lf_form_fields": "true"})
        # The matching success now gets the new guidance; the others do not.
        self.assertIn("get_fields()", self.text(EXTRACT, "Extract totals from invoice 1"))
        self.assertNotIn("get_fields()", self.text(EXTRACT, "Extract totals from invoice 0"))
        # The default stays classic RDR: the same rule is rejected.
        other = SkillWorkspace(copy_bundle(ROOT / "skills/pdf", Path(self.temporary.name) / "pdf2"))
        for index in range(3):
            await other.accept(EXTRACT, f"Extract totals from invoice {index}")
        result = await other.apply(broad, EXTRACT, failing, KeywordJudge({"lf_form_fields": "invoice 1"}), {})
        self.assertEqual(result.status, "unresolved")
        self.assertEqual(len(result.conflicts), 1)

    async def test_replace_cornerstones_swaps_a_tasks_stored_cases(self):
        old = self.workspace.case_for(EXTRACT, "Extract totals from invoice 2")
        stored, calls, contexts = await self.workspace.replace_cornerstones(
            [old[self.context_key]], [{"case": EXTRACT, "context": "Re-run: totals of invoice 2"},
                                      {"case": None, "context": "no skill view"}], self.judge)
        self.assertEqual((stored, calls, len(contexts)), (True, 0, 1))
        stored_contexts = {case[self.context_key] for case in self.workspace.stored_cases()}
        self.assertNotIn(old[self.context_key], stored_contexts)
        self.assertIn(contexts[0], stored_contexts)
        self.assertEqual(len(stored_contexts), 3)

    @property
    def context_key(self):
        from rdr_engine.learned import CONTEXT_KEY
        return CONTEXT_KEY

    async def test_condition_not_judged_true_on_the_failing_context_is_rejected(self):
        result = await self.workspace.apply(proposal(), EXTRACT, "nothing visible here", self.judge, {})
        self.assertEqual(result.status, "unresolved")
        self.assertIn("not true for the failing context", result.reason)
        self.assertEqual(self.workspace.layer.features, {})

    async def test_invalid_targets_are_rejected_without_changes(self):
        for bad in (proposal(target_node_id=5),  # Did not fire for an extraction case.
                    proposal(remove_fragment_ids=["frag_missing"])):
            result = await self.workspace.apply(bad, EXTRACT, "12 form fields", self.judge, {})
            self.assertEqual(result.status, "unresolved", bad)
        self.assertEqual(len(self.workspace.engine.nodes), 8)

    async def test_add_under_a_terminal_node_keeps_its_guidance_as_a_replace(self):
        # The engine refuses Add here (it would replace node 2's guidance); the intent is kept.
        result = await self.workspace.apply(proposal(kind="add", target_node_id=2), EXTRACT,
                                            "The tool listed 12 form fields", self.judge, {})
        self.assertEqual(result.status, "revised", result.reason)
        self.assertIn("applied as replace", result.note)
        with_rule = self.text(EXTRACT, "The tool listed 12 form fields")
        self.assertIn("get_fields()", with_rule)
        self.assertIn("pdfplumber", with_rule)  # Node 2's own guidance is still there.
        self.assertNotIn("get_fields()", self.text(EXTRACT, "Extract totals from invoice 0"))

    async def test_accept_judges_unjudged_learned_features(self):
        await self.workspace.apply(proposal(), EXTRACT, "12 form fields", self.judge, {})
        self.judge.calls.clear()
        stored, calls = await self.workspace.accept(EXTRACT, "a new form fields situation", self.judge)
        self.assertTrue(stored)
        self.assertEqual(calls, 1)
        stored, calls = await self.workspace.accept({**EXTRACT, "lf_form_fields": "false"}, "run-time judged", self.judge)
        self.assertEqual(calls, 0)  # Values judged at run time are recorded, not re-judged.

    async def test_document_scope_features_are_judged_once_per_document(self):
        from skill_agent.learned_features import judge_context

        workspace = SkillWorkspace(copy_bundle(ROOT / "skills/pdf", Path(self.temporary.name) / "scoped"))
        plain, form = "Task: extract\nPDF text layer:\nTotals", "Task: extract\nFilled PDF form fields\nName: Pope"
        for step in range(3):  # Three stored steps of one document without form fields.
            await workspace.accept(EXTRACT, judge_context(f"worker step {step}", plain))
        judge = KeywordJudge({"lf_form_fields": "form fields"})
        scoped = proposal(conditions=[{"new_feature": {
            "name": "lf_form_fields", "description": "The PDF has form fields.", "scope": "document"}}])
        # The worker's own words mention form fields; a document-scope feature must ignore them.
        result = await workspace.apply(scoped, EXTRACT, judge_context("I will look for form fields", plain), judge, {})
        self.assertEqual(result.status, "unresolved")
        self.assertEqual(result.judged, {"lf_form_fields": "false"})
        judge.calls.clear()
        result = await workspace.apply(scoped, EXTRACT, judge_context("worker step 9", form), judge, {})
        self.assertEqual(result.status, "revised", result.reason)
        # One call per document (the failing one and the stored one), not one per stored step.
        self.assertEqual(len(judge.calls), 2)
        self.assertTrue(all(text.endswith("</starting_materials>") for _, text in judge.calls))
        # Later steps of the same documents reuse those answers.
        cases = await workspace.replay_cases([(EXTRACT, judge_context("another step", form)),
                                              (EXTRACT, judge_context("another step", plain))], judge)
        self.assertEqual([case["lf_form_fields"] for case in cases], ["true", "false"])
        self.assertEqual(len(judge.calls), 2)

    def test_diagnosis_schema_ties_proposals_to_skill_attributions(self):
        with self.assertRaises(ValueError):
            Diagnosis.model_validate_json(json.dumps({"attribution": "not_skill", "root_cause": "x",
                                                      "proposal": proposal().model_dump()}))
        with self.assertRaises(ValueError):
            Diagnosis.model_validate_json(json.dumps({"attribution": "skill_missing", "root_cause": "x"}))
        with self.assertRaises(ValueError):
            proposal(kind="stop")  # Stop carries no guidance.
        self.assertEqual(proposal().predicates(), [{"field": "lf_form_fields", "op": "eq", "value": "true"}])


if __name__ == "__main__":
    unittest.main()
