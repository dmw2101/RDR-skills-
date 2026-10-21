"""Scenario checks for stages 4–6, called by the existing acceptance runner."""

import copy
import io
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace

from ._json import ValidationError, canonical, read, write
from .acceptance import rejects, require
from .contracts import Expression, FragmentDelta, Truth
from .engine import RuleNode
from .evaluation import ModelAccessDisabled
from .insertion import NeedsReview, choose_parent, publish_patch, stage_patch
from .integration_example import (BASE_MARKDOWN, FORMULA_INSTRUCTION, condition, context, evaluation_recording,
                                  failure_fixture, integration_demo, patch_recording, scope)
from .patches import PatchProposal, PatchRequest, RecordedPatchProvider, create_patch, validate_patch_response
from .registry import ConditionDefinition, ConditionRegistry
from .skill_state import Fragment, SkillState, import_skill, markdown_blocks, materialize_skill, resource_bytes
from .trace2skill import ExceptionMapCreator, verify_source


def run_integration_scenarios(scenario, directory):
    seed = directory / "integration-seed"
    seed.mkdir()
    (seed / "SKILL.md").write_bytes(BASE_MARKDOWN.encode("utf-8"))
    (seed / "recalc.py").write_bytes(b"# frozen resource\r\n")
    (seed / "LICENSE.txt").write_bytes(b"offline fixture license\n")
    (seed / "reference.bin").write_bytes(bytes(range(256)))
    registry = ConditionRegistry("xlsx", (
        ConditionDefinition("working_with_workbook", "The request operates on an Excel workbook."),
        ConditionDefinition("aggregation_requested", "The request aggregates workbook data.")))
    state = import_skill(seed, registry)
    failure = failure_fixture(directory / "source-fixture", "formula-case", context(), BASE_MARKDOWN)
    request = PatchRequest(registry, failure)
    recording = patch_recording(state, failure)
    proposal = PatchProposal.from_dict(recording)
    extended = proposal.extended_registry()
    protected = [evaluation_recording(extended, formulas=False)]

    def changed(**changes):
        response = copy.deepcopy(recording["response"])
        response.update(changes)
        return validate_patch_response(request, response)

    def direct_map():
        payload = request.to_dict()
        require(set(payload) == {"registry", "failed_execution", "system_prompt", "response_schema"}, "Unexpected MAP inputs")
        require(not any(key in canonical(payload["registry"]) for key in ('"nodes"', '"parent_id"', '"fragment_ids"')), "Tree leaked")
        provider = RecordedPatchProvider(recording)
        result = create_patch(request, provider=provider)
        require(provider.invocations == 1 and result == proposal, "MAP did not return one direct proposal")
        rejects(lambda: create_patch(request), ModelAccessDisabled)
        broken = copy.deepcopy(recording)
        broken["response"]["parent_id"] = 0
        bad_provider = RecordedPatchProvider(broken)
        rejects(lambda: create_patch(request, provider=bad_provider))
        require(bad_provider.invocations == 1, "Unexpected format-repair retry")
    scenario("direct_tree_free_map_response_and_no_hidden_calls", direct_map)

    def condition_contract():
        rejects(lambda: changed(scope=condition("contains_formulas")))  # New IDs cannot define existing scope.
        rejects(lambda: changed(new_conditions=[registry.conditions[0].to_dict()]))
        rejects(lambda: changed(when={"all": []}))  # Unused new condition.
        bad = copy.deepcopy(recording["response"]["judgments"])
        bad[-1]["evidence_refs"] = ["analysis_report.md"]
        rejects(lambda: changed(judgments=bad))
        bad[-1]["evidence_refs"] = ["inspection"]
        bad.pop()
        rejects(lambda: changed(judgments=bad))
        reused_request = replace(request, registry=extended)
        response = copy.deepcopy(recording["response"])
        response["new_conditions"] = []
        reused = validate_patch_response(reused_request, response)
        require(reused.extended_registry() == extended, "Existing condition was needlessly recreated")
    scenario("map_reuses_conditions_and_rejects_invalid_rules_or_evidence", condition_contract)

    def verified_inputs():
        flag = directory / "source-fixture/formula-case/evaluate_passed.flag"
        original = flag.read_bytes()
        flag.unlink()
        try:
            rejects(lambda: verify_source(failure))
        finally:
            flag.write_bytes(original)
        data = failure.to_dict()
        for verdict in ({"id": "formula-case", "success": True},
                        {"id": "formula-case", "success": False, "test_cases": []},
                        {"id": "formula-case", "success": False, "error": "missing data", "test_cases": [{"passed": False}]}):
            broken = copy.deepcopy(data)
            broken["source"]["verdict"] = verdict
            from .patches import FailedExecution
            rejects(lambda broken=broken: FailedExecution.from_dict(broken))
        verify_source(failure)
    scenario("upstream_verdict_and_repair_flag_both_required", verified_inputs)

    def snapshot():
        statuses = {c.id: Truth.TRUE for c in registry.conditions}
        require(state.render(statuses).encode("utf-8") == (seed / "SKILL.md").read_bytes(), "Base Markdown changed")
        blocks, _ = markdown_blocks(BASE_MARKDOWN)
        require(any("```python" in b and 'print("inspect first")' in b for b in blocks), "Code fence split")
        require(any("| Check | Action |" in b and "| Output |" in b for b in blocks), "Table split")
        require("".join(markdown_blocks("\n\nA paragraph.\n\n")[0]) == "\n\nA paragraph.\n\n", "Leading whitespace lost")
        rejects(lambda: markdown_blocks("```python\nunclosed"))
        state.save(directory / "roundtrip-state.json")
        require(SkillState.load(directory / "roundtrip-state.json") == state, "State round trip changed data")
        require(resource_bytes(state.resources)["reference.bin"] == bytes(range(256)), "Binary resource changed")
    scenario("frozen_markdown_blocks_resources_and_state_round_trip", snapshot)

    def placement():
        ref = lambda name: Expression("condition", name)
        wider = replace(state, registry=registry.register(ConditionDefinition("incidental", "An unrelated fact is present.")), nodes=state.nodes + (
            RuleNode(1, 0, ref("working_with_workbook"), FragmentDelta()),
            RuleNode(2, 1, ref("aggregation_requested"), FragmentDelta()),
            RuleNode(3, 0, ref("incidental"), FragmentDelta())))
        statuses = {c.id: Truth.TRUE for c in wider.registry.conditions}
        parent = choose_parent(wider, Expression.from_dict(scope("working_with_workbook", "aggregation_requested")), statuses)
        require(parent == 2, "Placement followed incidental facts instead of procedure scope")
        parent = choose_parent(wider, ref("working_with_workbook"), statuses)
        require(parent == 1, "Scope was over-specialized to the failed case")
        ambiguous = replace(wider, nodes=wider.nodes + (RuleNode(4, 0, ref("working_with_workbook"), FragmentDelta()),))
        rejects(lambda: choose_parent(ambiguous, ref("working_with_workbook"), statuses), NeedsReview)
    scenario("placement_uses_scope_not_all_true_context_features", placement)

    def pending_edits():
        for edit in (
            {"file": "recalc.py", "old_text": "", "new_text": "rewrite script"},
            {"file": "references/help.md", "old_text": "", "new_text": "change reference"},
            {"file": "SKILL.md", "old_text": "unrelated sheets", "new_text": "partial rewrite"},
            {"file": "SKILL.md", "old_text": state.fragments[0].text, "new_text": "replace frontmatter"},
            {"file": "SKILL.md", "old_text": "", "new_text": "Preserve unrelated sheets and existing formatting."},
        ):
            candidate, report = stage_patch(state, changed(edits=[edit]), protected)
            require(candidate is None and report["status"] == "pending_review", f"Unsupported edit applied: {edit}")
    scenario("unsupported_ambiguous_and_duplicate_instruction_edits_held", pending_edits)

    def source_activation():
        for field in ("contains_formulas", "aggregation_requested"):
            for status in ("unknown", "false"):
                response = copy.deepcopy(recording["response"])
                next(j for j in response["judgments"] if j["condition_id"] == field)["status"] = status
                candidate, _ = stage_patch(state, validate_patch_response(request, response), protected)
                require(candidate is None, "An unestablished source condition activated learning")
    scenario("scope_and_exception_must_be_known_true_on_source", source_activation)

    def unknown_protection():
        uncertain = copy.deepcopy(protected)
        uncertain[0]["response"]["judgments"][-1]["status"] = "unknown"
        candidate, report = stage_patch(state, proposal, uncertain)
        require(candidate is None and not report["replay"][0]["excluded"], "Unknown proved exclusion")
        overlapping = [evaluation_recording(extended)]
        candidate, report = stage_patch(state, proposal, overlapping)
        require(candidate is None, "A protected case was changed")
        require(stage_patch(state, proposal, [])[0] is None, "Unprotected insertion published")
    scenario("protected_replay_requires_known_exclusion_and_unchanged_guidance", unknown_protection)

    def stale_inputs():
        wider = replace(state, registry=registry.register(ConditionDefinition("another", "Another input observation holds.")))
        require(stage_patch(wider, proposal, protected)[0] is None, "Different registry silently accepted")
        altered = replace(failure, skill_text=BASE_MARKDOWN + "\nDifferent instructions.")
        altered_request = replace(request, failure=altered)
        stale = validate_patch_response(altered_request, recording["response"])
        require(stage_patch(state, stale, protected)[0] is None, "Different executed skill silently accepted")
        rejects(lambda: create_patch(altered_request, provider=RecordedPatchProvider(recording)))
    scenario("changed_registry_or_executed_guidance_requires_new_proposal", stale_inputs)

    def whole_blocks():
        target = "Preserve unrelated sheets and existing formatting."
        replacement = changed(edits=[{"file": "SKILL.md", "old_text": target, "new_text": "Preserve every unrelated sheet and its formatting."}])
        candidate, report = stage_patch(state, replacement, protected)
        require(candidate is not None, str(report))
        statuses = {j.condition_id: j.status for j in proposal.judgments}
        require(target not in candidate.render(statuses) and "Preserve every unrelated" in candidate.render(statuses), "Replacement failed")
        removal = changed(edits=[{"file": "SKILL.md", "old_text": target, "new_text": ""}])
        candidate, report = stage_patch(state, removal, protected)
        require(candidate is not None and target not in candidate.render(statuses), str(report))
        require("# Workbook procedure" in candidate.render(statuses), "Parent guidance disappeared")
    scenario("whole_block_replacement_and_removal_preserve_other_parent_guidance", whole_blocks)

    def replacement_conflicts():
        root = RuleNode(0, None, Expression("all"), FragmentDelta(("base",)))
        first = RuleNode(1, 0, Expression("all"), FragmentDelta(("first",), ("base",)))
        second = RuleNode(2, 0, Expression("all"), FragmentDelta(("second",), ("base",)))
        conflicting = SkillState(registry, (root, first, second),
                                 (Fragment("base", "Base\n"), Fragment("first", "First\n", "base"), Fragment("second", "Second\n", "base")))
        rejects(lambda: conflicting.render({}))
        rejects(lambda: resource_bytes((("../outside", "YQ=="),)))
        rejects(lambda: resource_bytes((("SKILL.md/child", "YQ=="),)))
    scenario("independent_replacements_conflict_and_resource_paths_stay_local", replacement_conflicts)

    def persistence():
        original = canonical(state.to_dict())
        report = publish_patch(state, proposal, protected, directory / "publication", engine="reference")
        require(report["status"] == "ready_reference", str(report))
        candidate = SkillState.load(directory / "publication/skill-state.json")
        require(len(candidate.nodes) == 2 and len(candidate.registry.conditions) == 3, "Related state not published together")
        require(canonical(state.to_dict()) == original, "Input state was mutated")
        blocked = publish_patch(state, proposal, protected, directory / "production-blocked")
        require(blocked["status"] == "blocked_upstream" and not (directory / "production-blocked/skill-state.json").exists(), "Production engine was used")
        pending = publish_patch(state, proposal, [], directory / "pending", engine="reference")
        require(pending["status"] == "pending_review" and not (directory / "pending/skill-state.json").exists(), "Failed replay published state")
        rejects(lambda: publish_patch(state, proposal, protected, directory / "publication", engine="reference"))
    scenario("candidate_publication_is_atomic_and_original_state_is_untouched", persistence)

    def protected_history():
        candidate, report = stage_patch(state, proposal, protected)
        require(candidate is not None, str(report))
        from .integration_example import VALUES_RULE
        new_registry = candidate.registry.register(VALUES_RULE)
        extra_context = evaluation_recording(new_registry)["context"]
        from .contracts import ContextSnapshot
        with_extra = replace(candidate, protected_contexts=candidate.protected_contexts + (ContextSnapshot.from_dict(extra_context),))
        # No subsequent request can silently discard a protected context.
        next_failure = failure_fixture(directory / "next-source", "values-case", context(values=True),
                                       candidate.render({c.id: Truth.TRUE for c in candidate.registry.conditions}))
        next_proposal = PatchProposal.from_dict(patch_recording(candidate, next_failure, refinement=True))
        result, report = stage_patch(with_extra, next_proposal, [evaluation_recording(new_registry, formulas=False)])
        require(result is None and "previously protected" in report["reason"], str(report))
    scenario("later_insertions_cannot_skip_persisted_protected_contexts", protected_history)

    def export_boundary():
        exported = ExceptionMapCreator().run_map_phase(registry, [failure], [recording], directory / "map-export")
        require(exported == [proposal], "MAP changed proposal")
        require(read(directory / "map-export/summary.json")["reducer_called"] is False, "Reducer was invoked")
        bad = copy.deepcopy(recording)
        bad["response"]["edits"] = []
        rejects(lambda: ExceptionMapCreator().run_map_phase(registry, [failure, failure], [recording, bad], directory / "bad-map"))
        require(not (directory / "bad-map").exists(), "Invalid batch partially published proposals")
    scenario("map_exports_structured_records_without_reduction_or_partial_bad_batch", export_boundary)

    def rendering():
        candidate, report = stage_patch(state, proposal, protected)
        require(candidate is not None, str(report))
        # Resource copies come from the frozen state, not a changed source tree.
        (seed / "recalc.py").write_bytes(b"changed later")
        output = directory / "materialized"
        materialize_skill(candidate, context(), evaluation_recording(extended), output)
        require((output / "recalc.py").read_bytes() == b"# frozen resource\r\n", "Resource snapshot changed")
        require((output / "reference.bin").read_bytes() == bytes(range(256)), "Binary copy changed")
        require(FORMULA_INSTRUCTION in (output / "SKILL.md").read_text(), "Learned instruction missing")
        require((output / "SKILL.md").read_text().startswith("---\nname:"), "Frontmatter moved")
        rejects(lambda: materialize_skill(candidate, context(), evaluation_recording(extended), output))
    scenario("rendered_skill_keeps_metadata_resources_and_original_source", rendering)

    def nested_example():
        report = integration_demo(directory / "nested-example")
        require([s["parent_id"] for s in report["steps"]] == [0, 1], "Refinement was not placed below first exception")
        final = SkillState.load(directory / "nested-example/02-refinement/insert/skill-state.json")
        require(len(final.protected_contexts) == 2, "Protected cases missing")
        require(report["model_calls"] == 0 and report["task_execution"] == "not_run", "Misleading demo claim")
    scenario("end_to_end_exception_then_exception_to_exception", nested_example)

    def cli():
        from .__main__ import main
        registry.save(directory / "map-registry.json")
        write(directory / "failure.json", failure.to_dict())
        write(directory / "patch-recording.json", recording)
        write(directory / "protected.json", protected)
        state.save(directory / "cli-state.json")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            require(main(["prepare-patch", "--registry", str(directory / "map-registry.json"), "--failure", str(directory / "failure.json"),
                          "--output", str(directory / "map-request.json")]) == 0, "Patch request CLI failed")
            require(main(["export-map", "--registry", str(directory / "map-registry.json"), "--failure", str(directory / "failure.json"),
                          "--recording", str(directory / "patch-recording.json"), "--output-dir", str(directory / "cli-map")]) == 0, "MAP CLI failed")
            require(main(["insert", "--state", str(directory / "cli-state.json"), "--proposal", str(directory / "cli-map/proposal_0001.json"),
                          "--protected", str(directory / "protected.json"), "--engine", "reference", "--output-dir", str(directory / "cli-insert")]) == 0,
                    "Reference insert CLI failed")
    scenario("cli_prepares_exports_and_inserts_recorded_proposals", cli)
