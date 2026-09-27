# Plan: compile a base Markdown skill into an RDR-backed skill

## Status

Steps 1–6 are implemented. `skill_compiler.py` extracts and validates an IR; `FragmentCatalog`, `SkillTreeCompiler`, and `SkillBundle` create and load checked artifacts; `skill_cases.py` evaluates/builds cases; `skill_renderer.py` composes matched fragments. `skill_tool.py` provides offline `compile` and `render` commands. The ALFWorld example is compiled under `skills/alfworld/compiled/` and has focused mechanical tests. Step 7 remains: review conversions for the other four seeds, expand regression tests, and compare rendered versus original skills on benchmark tasks. Online verifier/oracle integration remains a later phase.

## Decision

Send each complete `skills/<id>/initial.md` to a model, with line numbers added for referencing. The model returns a **reviewable rule list**: which source lines are general instructions, which apply under a condition, and which are non-instructional. Validate and, if needed, ask the model to repair that list. Only then extract exact Markdown fragments from the referenced lines and compile the RDR tree. The model selects source text; it does not rewrite it.

Keep `RDREngine.predict()` and its classical **last-true-node** conclusion unchanged. A separate `SkillRenderer` will read `prediction.path`, apply the conclusions of **matched true nodes only**, and produce the Markdown the agent sees. A false node determines the route but contributes no text. Thus the *rendered skill* accumulates applicable conclusions; `prediction.conclusion` remains the last true node's local conclusion. Do not change the core engine's traversal or persistence format for this first integration.

The agent's existing skill-selection step still chooses the skill; this plan changes only how that selected skill's body is loaded.

## Data contract

For each skill, produce `skills/<id>/compiled/{ir.json,fragments.json,tree.json,manifest.json}` beside the original `initial.md`:

- `ir.json`: accepted model output: general line ranges, conditional rules with source line ranges, and explicitly excluded non-instructional ranges. Keep this intermediate form for inspection and repeatable compilation.
- `fragments.json`: fragment ID, exact Markdown text, source line span, source order, role (`instruction` or `non_instruction`), and any `requires` IDs (for a section heading or table header needed to render the fragment). An ID points to text in this one catalog.
- `tree.json`: existing `RDREngine` schema. Every node conclusion is `{"add": ["fragment_id"], "remove": []}`; the unconditional root adds general fragments. `remove` is reserved for later exception patches and is empty for base compilation.
- `manifest.json`: hashes of the source, IR, fragment catalog, and tree; intermediate-schema/prompt versions, model ID, feature schema, and generation date. Loading fails if these disagree.

Illustrative model output (before fragment extraction):

```json
{
  "root": [{"start": 1, "end": 12}],
  "rules": [{
    "id": "xlsx_bulk_transform",
    "when": [{"field": "operation", "op": "eq", "value": "bulk_transform"}],
    "source": [{"start": 20, "end": 20}]
  }],
  "excluded": []
}
```

Line ranges above are illustrative, not the real spreadsheet skill's line numbers. The `when` clause is a conjunction over **observable task features**. For version 1, allow only typed `eq` and `in` predicates on an approved feature schema. A fragment without a defensible condition stays in the root if generally applicable; otherwise flag it for human review. Never silently discard instructions.

## Implementation steps

1. **Read the whole skill.** Add `SkillInput.from_markdown(path)` in `src/rdr_engine/skill_compiler.py`. It reads the original file, records its hash, and numbers its lines for the prompt. It does not decide fragment boundaries.

2. **Ask the model for rules and line references.** Add a versioned prompt, `SkillIR`/`RuleSpec` data types, and `SkillRuleExtractor.extract(skill_input, feature_schema) -> SkillIR`. Send the full Markdown and approved task-feature names. Ask for general ranges, conditional ranges, and exclusions with reasons; the model must not generate replacement instruction text. Use a hosted instruction-following model that supports JSON Schema output and can fit one complete skill in context. The API endpoint remains configurable.

3. **Validate and repair.** Implement `SkillIRValidator.validate(ir, skill_input, feature_schema) -> list[ValidationIssue]`. Check JSON shape, valid line ranges, source coverage, intact code fences/table rows, allowed predicates/features, overlapping ranges, and explicit ordering for co-applicable rules. Treat an unparseable response as a validation issue. If issues occur, call `SkillRuleExtractor.repair(skill_input, previous_output, issues, feature_schema) -> str` with precise errors (for example, “line 18 is unassigned” or “`task_type` is not an allowed value”); parse and validate that raw response again. Allow at most two repair calls, then report failure without writing compiled artifacts. Structured output helps with JSON syntax; these checks and a human review of each seed's first accepted IR address omissions and wrongly inferred conditions.

4. **Extract exact fragments.** Implement `FragmentCatalog.from_ir(skill_input, ir)` and `FragmentCatalog.validate_coverage()` in `skill_compiler.py`. Slice the original lines referenced by the accepted IR, assign stable fragment IDs, and share one fragment when two rules reference the same range. Record heading and table-header dependencies so selected rows render with valid Markdown structure. Preserve fenced code as a unit. Record SearchQA's “No learned rules yet” placeholder as `non_instruction` metadata, not guidance to the agent. Reject unresolved overlaps or uncovered instructional lines.

5. **Compile to the existing tree format.** Implement `SkillTreeCompiler.compile(ir, catalog, evaluator) -> RDREngine` by constructing a node table and passing it to `RDREngine.from_dict(..., evaluator=evaluator)`. The root holds general fragment IDs. Evaluate conditional rules in source order. To let independent rules both contribute when both match, copy the remaining decision subtree onto both the true and false branches; fragment text stays shared by ID. The engine rejects shared-child graphs. Cap version 1 at six conditional decisions (at most 64 nodes including the root); report a compilation error above that bound. Start trees with empty evidence; add verified case evidence later.

6. **Load, build cases, and render Markdown.** Add `SkillBundle.load(skill_dir, evaluator) -> (RDREngine, FragmentCatalog)` in `skill_compiler.py`; it verifies hashes and tree fragment references, then calls `RDREngine.from_dict(...)`. Add `SkillConditionEvaluator.evaluate(condition, case)` and benchmark-specific `build_case(task, visible_inputs)` adapters in `skill_cases.py`. Use only information available before the agent acts; missing required features raise errors. Add `SkillRenderer.render(prediction, engine, catalog) -> RenderedSkill` in `skill_renderer.py`: check the tree revision, walk the path, apply each true node's fragment operations, add required heading/table-header fragments, deduplicate IDs, restore source order, and return Markdown plus selected IDs and path. Missing IDs are errors.

7. **Check the conversion.** Start with DocVQA (mostly general text) and SpreadsheetBench (conditional guidance), then ALFWorld, LiveMathematicianBench, and SearchQA. Add `tests/test_skill_compiler.py` for invalid/missing ranges, repair exhaustion, tree round trips, and matching direct IR evaluation. Add `tests/test_skill_renderer.py` for simultaneous rules, root-only cases, table/heading dependencies, source order, stale revisions, and missing IDs. Inspect rendered Markdown for representative tasks. Compare unchanged seed and rendered skills on the same benchmark split and agent settings after mechanical checks pass.

## Expected behavior on these five seeds

| Skill | Likely initial structure | Initial observable case feature |
| --- | --- | --- |
| SpreadsheetBench | General workflow/template/output rules at root; conditional library-selection guidance. Formula preservation remains general unless a narrower condition is justified. | `operation` from task wording; `has_formulas` only if the input workbook is available for inspection. |
| ALFWorld | Output format and general action principles at root; six task-type procedures as conditional fragments. | `task_type` parsed from the initial goal, with an explicit `unknown` value if ambiguous. |
| DocVQA | Most instructions at root; add a condition only where the document explicitly makes advice context dependent. | None initially; use an all-root tree unless a reliable pre-answer feature is identified. |
| LiveMathematicianBench | General option/hypothesis checks and final-answer format at root; optional quantifier-specific fragment if the task text exposes that feature. | `has_quantifier` from the visible question text. |
| SearchQA | Root with no actionable instructions; retain the placeholder only as source metadata. This is the empty-start control. | None. |

## Boundary for later trace learning

This phase compiles and renders base skills. Later, an adapter can run `predict(case) -> render(path) -> agent execution -> verifier feedback -> learn(prediction, feedback)`. The verifier must judge the *executed rendered skill*, because the engine's `prediction.conclusion` is only one node's local fragment operation. A correction would stage a new fragment and pass its `add`/`remove` operation as the new node conclusion; commit the fragment only when `learn` successfully inserts the node. Accepted cases become evidence through the existing engine. Regression checks should also compare the **rendered skill and execution outcomes**, since classical single-conclusion preservation by itself does not prove composed instructions remain effective.
