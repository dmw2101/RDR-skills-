# Verifier-driven MCRDR learning

`trace_learner` adapts Trace2Skill's agentic error analyst to MCRDR skills. Trace2Skill turns each verified failure into a text patch and merges the patches into SKILL.md. Here, each verified failure becomes an **exception or alternative rule in the skill's MCRDR tree**. The engine accepts a rule only when it separates the failing situation from every stored situation where the guidance already worked. A candidate tree is then kept only if re-running the tasks confirms it.

## Pipeline

```
collected run (reduced arm, official scores)
  └─ init         passing episodes' skill views become cornerstone cases            (no model calls)
  └─ verify       one agentic verifier per failed episode → validated diagnosis      (verifier model)
  └─ consolidate  diagnoses aimed at the same node are merged by an LLM              (optional merge model)
  └─ insert       proposal → learned fragment/feature → RDREngine.learn(Revise(...)) (feature judge)
                  → snapshot → re-run fixed + protected tasks → accept or reject     (agent + judge models)
```

### Verifier

The verifier is an agent run by the same loop as the benchmark agent (`skill_agent.runtime.run_episode`), with read-only tools:
- `read_trace` and `skill_views`: the guidance (tree case, rendered nodes and fragments) shown at each turn.
- `show_tree` and `show_fragment`.
- `ground_truth` and `compare_with_ground_truth`.
- `inspect_shell`: a fresh copy of the original inputs.
- `rescore`: the official scorer on a corrected prediction.

It follows the paper's workflow: find what is wrong, trace it to a decision, validate the cause with a minimal fix, then attribute it (`skill_missing`, `skill_misleading`, `not_skill`, `unexplained`).

**A diagnosis is eligible only when a `rescore` call the verifier made actually passed.** This is recorded by code, not taken from the model's own claim. Diagnoses not attributed to the skill are logged and dropped.

### What a proposal becomes

| kind | MCRDR change | Effect on guidance |
|---|---|---|
| `replace` | `Replace(node)`: an exception child | The node's fragments, minus any removals, plus the new learned fragment, only where the conditions hold |
| `add` | `Add(parent)`: an alternative rule | New guidance where the conditions hold (usually under the root) |
| `stop` | `Stop(node)` | That node's guidance is withheld where the conditions hold |

- **Conditions** reuse existing feature predicates or define **learned features** (`lf_<name>`). A learned feature is a yes/no definition judged true/false/unknown by a separate judge model from the same visible text the resolver sees: the task at first, then the last three assistant turns. Only `true` fires a rule.
- **Before inserting**, the learner judges the new feature on the failing context and on every cornerstone reaching the parent node. The judge must say `true` on the failing context.
- **The engine then requires** that the conjunction holds on the failing case, excludes every cornerstone, and leaves every stored case's output unchanged. Otherwise nothing changes.

### Validation

1. The candidate is snapshotted with `rdr_service prepare` into the configured store.
2. The original run configuration is re-run for the fixed tasks plus up to `protected_limit` passing tasks whose cornerstones reach the changed node. It uses the reduced arm only, for `replicates` replicates, with `LearnedFeatureResolver` judging learned features each refresh.
3. The candidate is accepted when every fixed task passes in a majority of replicates and no protected task stops passing.
4. Accepted candidates become the next immutable version.

## Learned bundles

A learned version is an ordinary compiled skill plus `compiled/learned.json`:
- **learned fragments**: new guidance text placed after an anchor fragment;
- **learned features** and their definitions;
- **contexts**: the visible text of every cornerstone;
- **judgments** per context.

The manifest records the hash of `learned.json` and the extended feature schema. `SkillBundle.load`, `rdr_service` and `skill_tool render` all load it.

The original `initial.md` and its compiled fragments are never changed. `skill_tool compile` refuses to rebuild a learned bundle (which would drop the learned rules) unless `--discard-learned` is given.

## Colab trial

[`notebooks/verifier_learning_colab.ipynb`](../notebooks/verifier_learning_colab.ipynb) learns from one batch of documents and tests the result on different documents. Open it in VS Code with the Google Colab extension (Select Kernel → Colab). It:
1. collects a reduced-arm run on a **learning batch**: the first `LEARN_DOCUMENTS` held-out documents (default 30), never the 30 dev documents;
2. runs each learner stage in its own cell, with every paid cell marked, and shows the `report` table;
3. runs the newest accepted version on the **30 dev documents** for `TEST_RUNS` runs (default 1), and optionally re-runs the original tree alongside (`RERUN_BASE`);
4. compares the learned tree with the pilot deck's numbers for the original tree on those same documents (3 runs, same worker settings; the MCRDR tree gives the same advice as the deck's single-classification tree, see [agent evaluation](agent-evaluation.md#mcrdr-runs)), plus a `skill_agent report` of the new runs;
5. downloads a zip.

The deck has only totals, so without `RERUN_BASE` the comparison is on totals, not document by document. One run is noisy: the deck's own runs ranged from 85.7% to 94.0% exact-match fields.

It uses [`scripts/setup_mcrdr_colab.py`](../scripts/setup_mcrdr_colab.py) (see [Colab setup](agent-commands.md#colab-in-repo-setup-script)):
- `--heldout-batch N` prepares and runs the first N held-out documents (`skill_agent prepare --heldout-limit N`).
- `--learn` writes `experiments/configs/<run-name>.learner.json` from the example config below, using the run's paths, the service on port 8766 and its snapshot store.
- `--skill-version <versions/vN/pdf>` snapshots a learned version under its own receipt and serves it through `LearnedFeatureResolver`, with the example config's judge model. The full arm stays the original skill.

Models, all on OpenRouter (provider `alibaba`):

| Role | Model |
|---|---|
| Worker | `qwen/qwen3.7-flash`, from the run config |
| Feature judge (checker) | `qwen/qwen3.7-flash` |
| Verifier (inspector) and merge | `qwen/qwen3.7-plus` |

The notebook has not yet been run on Colab, so its cost and pass rates are unmeasured.

## Commands (PowerShell 5.1, repository root)

**1. Collect a run.** Collect with the MCRDR reduced arm, as in the [agent evaluation commands](agent-commands.md). The learner needs a completed run directory. For example, run `examples/agent/varex.mcrdr.pilot.json` (skill service on `experiments/snapshots`, port 8765). The learner checks that `skill_dir` is the skill that run used.

**2. Configure the learner.** Copy [examples/learning/varex.learner.json](../examples/learning/varex.learner.json) and set:
- `collect_run`;
- a fresh `output_dir`.

Its models are the OpenRouter ones listed under [Colab trial](#colab-trial). Any OpenAI-compatible tool-calling endpoint works instead.

Keys come from environment variables, as for the agent:

```powershell
$env:MODEL_API_KEY = "YOUR_API_KEY"
```

**3. Run the stages.** Each stage saves its output and resumes after an interruption. `learn` runs all of them. **Every stage except `init`, `status` and `report` makes paid model calls.** Validation re-runs agent episodes, so keep the skill service running against the configured `store`.

```powershell
.\.venv\Scripts\python.exe -m trace_learner init --config examples/learning/varex.learner.json
.\.venv\Scripts\python.exe -m trace_learner verify --config examples/learning/varex.learner.json
.\.venv\Scripts\python.exe -m trace_learner consolidate --config examples/learning/varex.learner.json
.\.venv\Scripts\python.exe -m trace_learner insert --config examples/learning/varex.learner.json
.\.venv\Scripts\python.exe -m trace_learner status --config examples/learning/varex.learner.json
.\.venv\Scripts\python.exe -m trace_learner report --config examples/learning/varex.learner.json
```

`report` writes `report.md` and `report.json` to `output_dir`, with three tables:
- each failed episode: what the verifier blamed, whether its fix re-scored as passing, the rule it led to, and that rule's decision;
- each rule: its conditions, advice, decision, and the tasks it fixed or broke;
- calls and cost per stage. The judge's calls while rules are inserted are counted, but their cost is not recorded.

**4. Evaluate the newest version.** `status` prints the newest version as `current_skill`. Prepare its snapshot and point an evaluation config's reduced resolver at the feature judge:

```powershell
.\.venv\Scripts\python.exe -m rdr_service prepare experiments/learning/varex-pdf-mcrdr/versions/v1/pdf --output experiments/pdf-snapshot-learned-v1.json
```

```json
"resolver": {"factory": "skill_agent.learned_features:LearnedFeatureResolver",
             "options": {"base_url": "http://127.0.0.1:8765", "judge_model": {"factory": "...", "options": {}}}}
```

Use the held-out split and a new `output_dir`, then `summarize` as usual. The judge's calls and cost are included in the reduced arm's totals. Snapshots without learned features make no judge calls.

## Outputs (`output_dir`)

| Path | Contents |
|---|---|
| `outcomes.json` | Official pass/fail of every collected episode |
| `cornerstones.json` | Cornerstone context → source episodes (used to choose protected tasks) |
| `diagnoses/` | One verifier transcript, diagnosis and eligibility per failed episode (contains gold labels; keep private) |
| `proposals.json` | Merged proposals and their source episodes |
| `ledger.json` | Each proposal's insertion result, validation passes/regressions and decision |
| `report.md`, `report.json` | Tables of failures, rules, decisions and cost (`report`) |
| `versions/vN/<skill>/` | Immutable accepted versions; `v0` is the original skill plus cornerstones |
| `validation/cN/` | Snapshot receipts and validation runs |

## Limits

- Failed traces only. Trace2Skill's success analyst is not implemented. Cornerstones come only from passing reduced-arm episodes, at most `cornerstones_per_episode` evenly spaced views each.
- Learned features depend on the judge model. Noisy judgments can block valid rules, and an `unknown` never fires. Learned text is fixed guidance written by the verifier, not checked against the upstream skill.
- Validation majority over a few replicates is weak evidence. Only the reduced arm is re-run. The full arm stays the original skill.
- Proposals are inserted one at a time in order. Later proposals are checked against earlier accepted rules, and a proposal the earlier rules make redundant is usually rejected by the engine.
- VAREX is the first adapter (`trace_learner.benchmarks.varex:VarexHooks`). SpreadsheetBench, the benchmark Trace2Skill, SkillOpt and WikiSkill all use, still needs an agent adapter and verifier hooks. A new benchmark needs `outcomes`, `ground_truth`, `rescore` and `open_inspection` (see `trace_learner/hooks.py`).
