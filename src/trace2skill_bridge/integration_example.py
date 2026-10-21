"""Two authored corrections, including an exception to an exception. No inference."""

from pathlib import Path

from ._json import ValidationError, write
from .contracts import ContextSnapshot, Evidence, Truth
from .engine import inspect_upstream
from .insertion import publish_patch
from .patches import PatchRequest
from .registry import ConditionDefinition, ConditionRegistry
from .skill_state import SkillState, import_skill, materialize_skill
from .trace2skill import ExceptionMapCreator, load_failure


BASE_MARKDOWN = """---
name: xlsx-example
description: Authored offline fixture, not a benchmark result.
---

# Workbook procedure

Preserve unrelated sheets and existing formatting.

| Check | Action |
| --- | --- |
| Output | Validate requested cells |

```python
# A fenced example stays in one fragment, even with blank lines.

print("inspect first")
```
"""

FORMULA_RULE = ConditionDefinition("contains_formulas", "At least one input workbook cell contains an Excel formula.")
VALUES_RULE = ConditionDefinition("values_only_output", "The user explicitly requests final values rather than formulas in result cells.")
FORMULA_INSTRUCTION = "When aggregating a workbook with formulas, preserve existing formulas and write totals as formulas."
VALUES_INSTRUCTION = "For final-values-only output, write computed values in result cells while preserving unrelated input formulas."


def condition(name):
    return {"condition": name}


def scope(*names):
    return {"all": [condition(n) for n in names]}


def context(*, formulas=True, values=False):
    task = "Group revenue in this Excel workbook by month."
    if values:
        task += " Return final values, not formulas, in the result cells."
    else:
        task += " Keep formula totals in the result cells."
    observed = "Sheet1!B2 contains =SUM(A1:A2)." if formulas else "Inspection of every input cell found constants only; no formulas."
    return ContextSnapshot("xlsx", task, (Evidence("inspection", observed, "authored_inspection"),))


def judgments(registry, *, formulas=True, values=False):
    results = []
    for definition in registry.conditions:
        status, evidence = Truth.TRUE, "task"
        if definition.id == "contains_formulas":
            status, evidence = Truth.TRUE if formulas else Truth.FALSE, "inspection"
        if definition.id == "values_only_output":
            status = Truth.TRUE if values else Truth.FALSE
        results.append({"condition_id": definition.id, "status": status.value, "evidence_refs": [evidence]})
    return results


def evaluation_recording(registry, *, formulas=True, values=False):
    return {"registry": registry.to_dict(), "context": context(formulas=formulas, values=values).to_dict(),
            "response": {"judgments": judgments(registry, formulas=formulas, values=values)}}


def failure_fixture(directory, task_id, snapshot, skill_text):
    """Use the real upstream file shapes; clearly label them as authored fixtures."""
    directory = Path(directory)
    analysis_dir = directory / task_id
    analysis_dir.mkdir(parents=True)
    (analysis_dir / "evaluate_passed.flag").write_text("authored fixture; no task executed\n", encoding="utf-8")
    (analysis_dir / "analysis_report.md").write_text("# Authored analysis fixture\nPreserve input formulas.\n", encoding="utf-8")
    evaluation = {"summary": {"fixture": True}, "results": [{"id": task_id, "success": False,
                    "test_cases": [{"passed": False, "message": "authored formula mismatch"}]}]}
    errors = [{"instance_id": task_id, "source_file": "analysis_report.md", "items": [
        {"type": "failure_cause", "title": "Formula writeback", "content": "The procedure needs a contextual writeback correction."}]}]
    write(directory / "eval_official_results.json", evaluation)
    write(directory / "parsed_error_records.json", errors)
    return load_failure(task_id=task_id, context=snapshot, skill_text=skill_text,
                        evaluation_file=directory / "eval_official_results.json",
                        parsed_errors_file=directory / "parsed_error_records.json", analysis_dir=analysis_dir)


def patch_recording(state, failure, *, refinement=False):
    new = VALUES_RULE if refinement else FORMULA_RULE
    extended = state.registry.register(new)
    names = ("working_with_workbook", "aggregation_requested") + (("contains_formulas",) if refinement else ())
    response = {"scope": scope(*names), "when": condition(new.id), "new_conditions": [new.to_dict()],
                "judgments": judgments(extended, values=refinement),
                "edits": [{"file": "SKILL.md", "old_text": FORMULA_INSTRUCTION if refinement else "",
                           "new_text": VALUES_INSTRUCTION if refinement else FORMULA_INSTRUCTION}],
                "reason": "Use an observable input condition to localize the writeback procedure."}
    return {"registry": state.registry.to_dict(), "failed_execution": failure.to_dict(), "response": response}


def integration_demo(output_dir):
    directory = Path(output_dir)
    if directory.exists():
        raise ValidationError("Integration demo requires a new output directory")
    seed = directory / "base-skill"
    seed.mkdir(parents=True)
    (seed / "SKILL.md").write_text(BASE_MARKDOWN, encoding="utf-8", newline="")
    (seed / "recalc.py").write_text("# Offline placeholder resource, not executed\n", encoding="utf-8")
    registry = ConditionRegistry("xlsx", (
        ConditionDefinition("working_with_workbook", "The current request operates on an Excel workbook."),
        ConditionDefinition("aggregation_requested", "The request groups or aggregates workbook data.")))
    state = import_skill(seed, registry)
    registry.save(directory / "initial-registry.json")
    state.save(directory / "initial-state.json")
    reports = []
    for refinement in (False, True):
        step = directory / ("02-refinement" if refinement else "01-exception")
        statuses = {c.id: Truth.TRUE for c in state.registry.conditions}
        failure = failure_fixture(step / "upstream-fixture", "values-case" if refinement else "formula-case",
                                  context(values=refinement), state.render(statuses))
        recording = patch_recording(state, failure, refinement=refinement)
        write(step / "failure.json", failure.to_dict())
        write(step / "map-recording.json", recording)
        write(step / "patch-request.json", PatchRequest(state.registry, failure).to_dict())
        proposals = ExceptionMapCreator().run_map_phase(state.registry, [failure], [recording], step / "export")
        extended = proposals[0].extended_registry()
        protected = [evaluation_recording(extended, formulas=False)]
        if refinement:
            protected.append(evaluation_recording(extended))
        write(step / "protected-recordings.json", protected)
        report = publish_patch(state, proposals[0], protected, step / "insert", engine="reference")
        if report["status"] != "ready_reference":
            raise ValidationError(f"Demo failed: {report}")
        state = SkillState.load(step / "insert/skill-state.json")
        state.registry.save(step / "registry.json")
        write(step / "source-context.json", failure.context.to_dict())
        source_recording = evaluation_recording(state.registry, values=refinement)
        write(step / "source-recording.json", source_recording)
        rendering = materialize_skill(state, failure.context, source_recording, step / "rendered-skill")
        write(step / "render-report.json", rendering)
        reports.append({"step": step.name, "node_id": report["node_id"], "parent_id": report["parent_id"],
                        "status": report["status"], "protected_cases": len(report["replay"])})
    summary = {"kind": "hand_authored_integration_example", "steps": reports, "model_calls": 0,
               "task_execution": "not_run", "semantic_quality": "not_measured", "production_ready": False,
               "upstream": inspect_upstream()}
    write(directory / "summary.json", summary)
    return summary
