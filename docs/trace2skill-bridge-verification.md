# Offline bridge verification — stages 1–6

Verified on 2026-10-21. Stages 4–6 are implemented against the explicit reference
backend; production MCRDR integration and live inference remain blocked/pending.

Branch: trace2skill-integration, based on origin/MCRDR-Integration at
ac0cb74cfff46f10dca5c547a49b41bec18c2c7f.

| Check | Result |
| --- | --- |
| Offline acceptance | 37 scenarios passed (20 foundations + 17 integration) |
| Existing repository suite | 72 tests passed |
| Bridge model calls | 0 |
| Bridge network attempts | 0; sockets blocked during acceptance |
| Repeated identical input | Evaluator invoked again on each refresh |
| New formula observation | New judgments and traversal; parent guidance retained |
| Direct MAP response | Tree-free prompt; rules and edits in one recorded response |
| Source requirements | Failed comparison plus task-matched verified analyst repair required |
| Reference insertion | Scope-based placement, new condition registration, staged fragment delta |
| Nested correction | Second exception inserted under the first exception |
| Protected replay | Known exclusion and unchanged rendered guidance required |
| Publication | One atomic skill-state file; original state untouched |
| Real XLSX snapshot/render | SKILL.md, recalc.py, LICENSE.txt byte-identical |
| Real XLSX rendered format | Anthropic quick_validate.py passed |
| Current upstream engine | Unsupported, as expected |
| Trace2Skill source and compiled skill modifications | None |

Removed from the bridge: caching, condition/registry revision counters, hash
binding, evaluator/prompt version metadata, semantic-duplicate hints, and the
separate saved-judgment metadata schema. Existing condition IDs cannot be
redefined. Simple input/output logs remain.

Foundation acceptance covers parent retention, explicit removals, ancestor gating, sibling
conflicts, three-valued logic, append-only registration, JSON persistence, typed
predicates, complete condition coverage, evidence validation, known facts,
context refresh, fresh evaluation on every call, explicit recording consistency,
disabled inference, CLI use, and upstream compatibility reporting.

Integration acceptance adds direct structured MAP output without tree disclosure,
condition reuse/validation, source flag/verdict checks, ordered Markdown and frozen
resource round trips, scope placement without incidental facts, unsupported edit
review, source activation, unknown/overlapping protected cases, changed-input
rejection, whole-block replacement/removal, competing replacement conflicts,
safe resource paths, atomic candidate publication, protection-history coverage,
export without reduction or partial bad batches, new skill materialization,
nested exceptions, and CLI request/export/insertion.

Detailed current reports are in experiments/trace2skill-bridge/verification/.
The context demonstration is in experiments/trace2skill-bridge/demo-simple/.
The two-step learning demonstration is in
experiments/trace2skill-bridge/integration-demo/. Actual XLSX import/render
artifacts are in experiments/trace2skill-bridge/xlsx-smoke/.
Older generated examples and logs were preserved.

The dedicated WSL environment uses Python 3.12.3 and the standard library only.
The unchanged repository suite uses the already available dependencies in the
sibling Trace2Skill environment. Run the checks using the
[guide](trace2skill-bridge.md).

## Still pending

- Model interpretation quality: the current judgments are authored examples.
- Upstream inherited-fragment correction and production engine adapter.
- Live structured MAP responses and semantic condition authoring quality.
- Production compiled-bundle adapter and fresh task execution experiments.
- Actual repaired-task success and regression accuracy. Offline replay establishes
  guidance preservation only; its fixture repair flags are explicitly authored,
  not evidence of benchmark execution.

## Preserved user files

| File | SHA-256 |
| --- | --- |
| First_run_manifest.json | 6f4fcd70fb2c83dd18790e4f2ebd0191c31b639b3c0e8784221b8051f66c39bf |
| First_run_scoring.json | c0339703e7a240e9cde63590c6de8cae4e8a36fcaeb9cc58d4760ad77c5e0f53 |
| First_run_summary.json | 7e7805ce32d958d75552b0e168f6fa4f75fa3b7ace5687cf43514b3eb0dc974f |
