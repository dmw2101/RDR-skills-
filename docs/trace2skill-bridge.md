# Offline Trace2Skill bridge

The current flow is:

**Current context + condition registry → evaluate conditions → traverse → compose guidance**

Each refresh evaluates again, including when the input is identical. There is no
cache, registry history, definition-revision API, or evaluator-version tracking.
Saved inputs and outputs are experiment logs and are never searched for results.

## Registry and context

A registry is an append-only collection of named conditions for one skill:

```json
{
  "skill_id": "xlsx",
  "conditions": [
    {"id": "working_with_workbook", "definition": "The request operates on an Excel workbook."},
    {"id": "contains_formulas", "definition": "At least one input cell contains an Excel formula."}
  ]
}
```

Definitions are immutable. Registration can reuse an existing condition or append
a new one. Changing the meaning of an existing ID is rejected; use a different
ID for a different meaning. Optional examples help clarify a condition. Exact
duplicate definitions are rejected; there is no heuristic semantic deduplicator.

The context contains the original task and relevant observations. When the agent
learns something new, the caller supplies the updated context and requests another
evaluation. Optional inspected facts must be respected. Existing eq/in/contains
predicates can also be imported through the typed compatibility helper.

The evaluator returns true, false, or unknown for every registered condition,
with evidence references. Unknown does not activate a node, and NOT unknown is
still unknown. This avoids treating an uninspected workbook as formula-free.

The tree combines conditions with all/any/not and defines fragments to add or
remove. Matching children retain parent guidance unless they explicitly remove
it. Conflicting independent branches remain unresolved.

## Run offline

From PowerShell in RDR-skills:

```powershell
.\scripts\trace2skill_bridge.ps1 setup
.\scripts\trace2skill_bridge.ps1 demo
.\scripts\trace2skill_bridge.ps1 acceptance
```

From WSL, use `bash scripts/trace2skill_bridge.sh` with the same arguments.
Setup uses WSL Ubuntu's Python 3.12 and the dedicated `.venv-trace2skill`
environment. The bridge uses the standard library; no downloads or model SDKs
are required.

The demo writes to `experiments/trace2skill-bridge/demo-simple/`. It
shows two contexts: before inspection, formula presence is unknown; after a
recorded formula observation, it is true and different guidance is selected.

All example judgments are hand-authored. Live inference is disabled. For offline
execution, explicitly supply a recording containing the registry, context, and
response that belong together. A recording for another input is rejected.

```powershell
.\scripts\trace2skill_bridge.ps1 evaluate --registry experiments/trace2skill-bridge/demo-simple/registry.json --context experiments/trace2skill-bridge/demo-simple/before-inspection/context.json --recording experiments/trace2skill-bridge/demo-simple/before-inspection/recording.json --engine reference --tree experiments/trace2skill-bridge/demo-simple/fixture-tree.json --output experiments/trace2skill-bridge/result.json
```

The result logs the full input, output judgments, traversal, and selected fragment
IDs. These logs are not used to skip later evaluations. Without an explicit
recording, evaluation stops. No automatic retry or model-based repair occurs.
Earlier experiment files are retained but the simplified workflow does not read
their old metadata format.

## Python entry points

```python
from trace2skill_bridge import ConditionDefinition, ConditionRegistry
from trace2skill_bridge.evaluation import RecordedResponseProvider, evaluate_context

registry = ConditionRegistry("xlsx").register(
    ConditionDefinition("contains_formulas", "At least one input cell contains a formula.")
)
result = evaluate_context(
    context, registry,
    provider=RecordedResponseProvider.from_file("recording.json"),
)
statuses = result.statuses()
```

`prepare_request(registry, context)` prepares the prompt input. The prompt
and descriptive JSON Schemas are under `src/trace2skill_bridge/`. Runtime
checks require complete condition coverage, valid evidence references, and
consistency with inspected facts. They cannot establish whether a model's
interpretation of natural language is correct.

## Stages 4–6: direct conditional patches

The learning flow is now:

**Verified failure → structured MAP proposal → code chooses placement → stage → replay → save a reference candidate**

The patch creator receives only the registry and the failed execution (visible
context, the guidance actually used, verified analysis, and source links). It
does **not** receive the tree, node IDs, or fragment IDs. There is no second
model call to convert a prose patch into a rule.

Its single response contains:

- `scope`: the existing procedure, such as workbook aggregation.
- `when`: the distinguishing situation, such as an input containing formulas.
- `new_conditions`: definitions that cannot reuse an existing registry ID.
- `judgments`: true/false/unknown for the full extended registry on this context.
- `edits`: minimal instruction additions or exact replacements/removals.
- `reason`: the reusable correction.

The new prompt is `prompts/create_exception.md`; the contract is
`schemas/patch_response.json`. The same condition-evaluation checks apply to
the returned judgments. Analyst text and gold answers cannot be cited as
evidence for runtime conditions. Whether a model actually follows this rule
requires real inference evaluation; the offline checks cannot prove it.

### Stage 4 — import and propose

`trace2skill.py` reads `eval_official_results.json` and
`parsed_error_records.json`. It requires one matching failed task comparison,
the task's `analysis_report.md`, and `evaluate_passed.flag`. Parsed analyst text
alone is not sufficient. A passing repair verifies the analyst's correction,
**not the effectiveness of the proposed skill instruction**.

`ExceptionMapCreator.run_map_phase()` replaces the upstream MAP prompt/response
boundary. It exports one structured proposal per failure, with its complete
input. It never invokes `run_reduce_phase()` or the upstream full `run()` method.
It uses recorded responses only and rejects malformed output without repair or
retry. Trace2Skill's execution, scoring, and analysis commands remain unchanged.
There are no edits or monkey patches in the pinned Trace2Skill repository.

### Stage 5 — skill storage and rendering

`skill_state.py` snapshots the base skill into ordered Markdown blocks. Tables
and fenced code blocks remain whole. Base text round-trips byte-for-byte, and
resources (including licensing and scripts) are frozen unchanged in the state.
Binary resources use base64; there are no revision counters or caches.

For now, automatic edits support `SKILL.md` only. An addition has empty
`old_text`; a replacement/removal must match one whole active block. Partial,
ambiguous, metadata, reference, and script edits are held for review. Children
add guidance by default. Explicit replacements remove only the targeted
inherited fragment, not all parent guidance. Conflicting independent operations
or competing replacements remain unresolved.

`init-skill` creates a reference state with the complete base skill on its root.
It does not reinterpret the repository's existing compiled tree bundles or
overwrite them. Those bundles and the production renderer remain unchanged.

### Stage 6 — placement, replay, and publication

`insertion.py` traverses using the proposal's source judgments, then selects
one most-specific ancestor whose path is logically implied by `scope`.
Incidental true facts do not select placement. The new child is guarded by
both `scope` and `when`. Ambiguous independent anchors require review. Logical
implication is a small CPU-only truth-table check, capped at 12 distinct
conditions per comparison; larger scopes are held rather than guessed.

Insertion first happens in memory. The source must activate the proposed rule
and use exactly the frozen guidance recorded in the failure. At least one
protected context must then replay with fresh, explicit judgments covering the
extended registry. Every previously protected context must also be included.
The new rule must be **known to exclude** each protected case, and its rendered
guidance must remain unchanged. Unknown exclusion is rejected.

A passing reference candidate saves registry, nodes, fragments, frozen resources,
protected contexts, and source links together in one atomic `skill-state.json`.
The original state is never overwritten. Unsupported or failed proposals produce
a diagnostic without publishing candidate state. There is no automatic merge
across independently created proposals: after a registry or relevant guidance
change, recreate stale proposals against the current inputs.

Protected-context replay checks guidance preservation, not answer accuracy or
successful execution. It cannot detect semantic conflicts between differently
worded instructions. Failed source cases are not automatically labelled as
successes or protected successful cornerstones. Fresh execution remains necessary.

## Run the full offline example

```powershell
.\scripts\trace2skill_bridge.ps1 integration-demo
.\scripts\trace2skill_bridge.ps1 acceptance
```

The integration example requires a new output directory. To run it again:

```powershell
.\scripts\trace2skill_bridge.ps1 integration-demo --output-dir experiments/trace2skill-bridge/my-next-demo
```

It imports authored upstream-shaped fixtures, exports a proposal, inserts the
formula-handling exception, and then inserts a values-only exception **under that
exception**. It preserves general guidance and replays protected cases. Inspect
`01-exception/` and `02-refinement/` for requests, recordings, proposals, reports,
updated state, and materialized skill directories. These are deliberately
authored examples, not model traces or benchmark results.

The same steps are individually available through `--help`:

```text
init-skill       Freeze a base skill with the current registry
import-failure   Read upstream verdict + parsed analysis + repair flag
prepare-patch    Write the tree-free prompt input and response contract
export-map       Use an explicit recorded response; export without reduction
insert           Stage, replay, and save (--engine reference is required)
render-skill     Evaluate a recorded context and write a new runnable skill copy
```

For example, after running the demo, replay its first insertion into a new
directory (PowerShell continuation characters are backticks):

```powershell
.\scripts\trace2skill_bridge.ps1 insert `
  --state experiments/trace2skill-bridge/integration-demo/initial-state.json `
  --proposal experiments/trace2skill-bridge/integration-demo/01-exception/export/proposal_0001.json `
  --protected experiments/trace2skill-bridge/integration-demo/01-exception/protected-recordings.json `
  --engine reference `
  --output-dir experiments/trace2skill-bridge/replayed-insertion
```

Use relative paths or WSL paths for launcher arguments, not Windows drive paths.
Source links are rechecked against local upstream artifacts when proposals are
exported or staged; keep the run folders. A recording is explicitly selected input,
not a cache lookup. `render-skill` always requires a new destination and does not
execute tasks. Initial XLSX skill path: `../Trace2Skill/spreadsheet_agent/skills/xlsx`.

## Engine and validation status

The reference implementation supports the agreed inherited fragment behavior. The
actual upstream engine still suppresses parent conclusions and is deliberately
blocked by its compatibility check. Its correction and production integration
remain separate work. Trace2Skill source and existing compiled skills are unchanged.

The acceptance runner checks the simplified flow with sockets blocked. See
[verification](trace2skill-bridge-verification.md) for results.

## When GPU-backed inference is available

1. Add explicitly enabled live backends for condition evaluation and structured
   MAP creation. Check condition accuracy, unknown handling, evidence quality,
   rule reuse versus unnecessary new conditions, prompt injection, repeatability,
   latency, and registry-size effects. Do not infer model quality from recordings.
2. Connect the separately corrected upstream MCRDR engine only after inheritance,
   additions/removals, and independent-branch conflict checks pass. Existing
   compiled-bundle import and production publication need that adapter.
3. Run **three evolution-set tasks with one worker**, saving original execution,
   scoring, verified analysis, and patch outputs. Supply only pre-decision visible
   observations to condition evaluation, not answers or hindsight from the verifier.
4. Re-execute repaired source tasks and protected successful tasks using the
   materialized candidate. Publish a production update only after actual target
   improvement and regression checks pass. Add genuinely successful cases to the
   protected set; the offline demo's cases are only guidance-replay fixtures.
5. Compare the unchanged skill, original Trace2Skill aggregation, and conditional
   exceptions. Measure task accuracy, transfer, regressions, update cost, and
   adaptation. Keep held-out tasks out of authoring and selection.
