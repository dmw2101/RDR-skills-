# SOPBench Bank evaluation

## Linux GPU comparison

From a checkout of `MCRDR-with-dependencies`, activate your Python venv and run:

```bash
python -m pip install torch==2.9.1 torchvision==0.24.1 --index-url https://download.pytorch.org/whl/cu126
python -m pip install -r requirements.txt
python -m pip check
python scripts/run_bank_gpu.py setup
python scripts/run_bank_gpu.py pilot --name bank-gpu-pilot
```

`setup` fetches the pinned Bank data and Qwen3.5-4B weights. It can run before a GPU allocation. Set `HF_HOME` to persistent storage shared with the GPU job. The pilot checks CUDA, structured tool calls, and a Bank selector response before running. A single local model serves both the actor and selector. The setup uses Git and an NVIDIA driver compatible with the CUDA wheels.

The five arms are `none`, `full`, `initial`, `requests`, and `dependencies`. Each runs the same three pilot cases once. The [frozen Bank variants](../skills/variants/bank-defensive-v2) share the same policy, fragments and tree. The dependency bundle adds the 29-link graph, and the proposal is stored beside the variants. Neither runtime setup nor inference calls Gemini.

Use `python scripts/run_bank_gpu.py bank34 --name bank-gpu-34` after reviewing the pilot. Use `--results-root` to write to persistent storage outside the checkout. Each named run keeps per-arm configs, results, and skill inspections alongside server logs and smoke check results. Use `--resume` with the original suite, name, and settings. Incomplete episodes retain their diagnostic records and block scoring for that arm until resolved.

## Benchmark protocol

This experiment compares `none`, `full` and `reduced` on the 34 SOPBench Bank cases that share the [compiled policy](../skills/sopbench_bank_fixed_policy/README.md). The pilot uses two cases from different action types (six episodes), including an eligible and an ineligible case; the full configuration uses all 34 (102 episodes). These are a project-selected subset, not an official benchmark split.

Each episode receives a fresh upstream Bank environment, the same public tools and customer request, and the upstream non-LLM customer response when the assistant asks for information. The assistant ends the conversation with `exit_conversation`. The original SOPBench directed-graph evaluator scores the action sequence and final database. The surrounding agent loop, model settings and 20-call budget are our evaluation protocol, not an exact reproduction of the paper's full experiment.

The no-skill arm receives a neutral role prompt and the Bank tools. The full arm receives the complete policy on each model turn. The reduced arm refreshes its guidance as in the PDF rolling experiment: it sends the last three available assistant responses, or the original task on the first turn, to the MCRDR service. Code matches Bank tool names and common phrases to action names; the tree renders the matching fragments. This uses no selector model and receives no private database, hidden target action or reference answer. Matching can miss paraphrases or select irrelevant mentions, so inspect the cases and fragments in the pilot. No helper action is selected unless its name or a matching phrase appears in the context.

`run.conditions` chooses arms, `run.replicates` chooses repetitions, `run.limit` chooses cases, and `run.task_ids` can choose exact subset IDs. The configs use one repetition and a sequential run. A model request receiving HTTP 429 waits 60 seconds before retrying, up to twice; the episode's time limit still applies. Change `model.options` if using another provider/model. Store the key in an environment variable, not a config file.

## PowerShell 5.1

Run these commands from the repository root. The scripts use a dedicated environment and save results under `experiments/bank-windows/runs`. They use Python 3.12, Git and an OpenRouter API key.

Prepare the environment, pinned benchmark and skill snapshot once:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup_bank_powershell.ps1
```

Check the setup without making model calls:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_bank_powershell.ps1 -Experiment pilot -Name pilot2 -CheckOnly
```

Run the two-case pilot. The script asks for your API key without displaying it, starts and stops its own skill service, and prints the scores and selected skills:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_bank_powershell.ps1 -Experiment pilot -Name pilot2
```

If `MODEL_API_KEY` is already set in your PowerShell session, the script uses it without prompting. `-CheckOnly` does not prove that the hosted API works. Episode JSON files contain the conversation, tool calls, selected feature values and per-call usage. Run the pilot before the full subset and inspect any failed episodes. The service uses port 8766; the script reports an error if that port is already in use.

## Run all 34 cases

After the pilot, use the separate output directory in the 34-case config:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_bank_powershell.ps1 -Experiment dev34 -Name bank34-run1
```

The summary reports SOPBench-style success, completion counts, input tokens, elapsed time, reported cost and paired comparisons. The component checks are in `scores/scoring-result.json`. Only completed episodes are scored. If any episode remains incomplete, the run exits with an error and summarization is blocked; inspect `results/diagnostics/` and resume. Diagnostics record the error category and observed partial usage without saving an incomplete episode trace. Final-episode usage and prior-attempt usage are reported separately; provider costs for timed-out requests can be unknown.

## Bank evaluation adaptations

This is an **adapted SOPBench Bank evaluation**, not an unmodified official benchmark run. The in-memory dataset and scorer use matching credit-card dictionaries, and the scorer's failed-login state handling and Bank card-creation defects are repaired. Card numbers are generated deterministically so scoring is reproducible across processes. Model tool arguments are passed through unchanged: a driver's licence encoded as a string remains a string, as in the upstream tool. Bank tool crashes leave an episode incomplete instead of being scored as model mistakes. The pinned upstream files and historical results remain unchanged; the run provenance records the adaptation version. These changes need a fresh pilot and full rerun before reporting new results.

The model client retries transient HTTP and network errors with bounded backoff and respects `Retry-After`. If a network timeout persists, the runner retries the episode once in a fresh environment by default; configure `episode_retries` and `episode_retry_delay_seconds` in `run` if needed. A model-call budget exhaustion is recorded separately and is not silently retried or scored. Retry events are recorded, but lost responses can have unknown provider charges. A completed run still requires independent review of the benchmark, scorer, and experimental protocol before making a publication claim. In particular, `pay_loan-004` permits paying more than the balance when the balance covers the total debt; the upstream tool can then produce negative balances, so that case needs a validity review.

Use a new `-Name` for each new run. To resume an interrupted run, use the same name and add `-Resume`:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_bank_powershell.ps1 -Name bank34-run1 -Resume
```

Resume uses the saved configuration; a code or snapshot revision change can prevent resuming an older run. The script does not overwrite existing results.

## Historical six-case tool repair

The original 34-case run omitted two target tool schemas from the pinned upstream assistant definitions. The current Bank configs restore schemas for `cancel_credit_card` and `pay_bill_with_credit_card`. The commands in this section describe the previous mixed-protocol analysis; its incomplete episodes and older fingerprints are not compatible with the new completed-only scorer.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_bank_powershell.ps1 -Experiment affected6 -Name bank-affected6
```

Combine the rerun with the other 28 original cases without changing either run:

```powershell
experiments/bank-windows/.venv/Scripts/python.exe scripts/combine_bank_rerun.py experiments/bank-windows/runs/bank34-run1/results experiments/bank-windows/runs/bank-affected6/results experiments/bank-windows/combined-bank34
```

The combined summary is explicitly mixed: the original 28 cases saw a tool catalog without the two restored tools. Report this as a mixed-protocol diagnostic, not a uniform 34-case benchmark run.

To retry just the saved 429 failures from `bank-affected6`, set `MODEL_API_KEY` and run `python scripts/retry_bank_429.py` inside the Bank environment. The retries save separate episodes and wait 60 seconds after a 429, up to twice per model request. To include them in the mixed summary, append `--retries experiments/bank-windows/runs/bank-affected6-retry429` to the combine command. Original records and non-429 failures remain unchanged.

## Batch LLM selection: two initial-guidance modes

These modes leave the existing rolling/PDF experiments available. A separate selector model evaluates the snapshot's entire condition catalog in one call; the MCRDR engine then applies ancestry, conjunctions, stopping rules, and terminal conclusions. Policy prerequisites remain in the policy text. Selecting a procedure does not establish that its prerequisites are satisfied.

After the existing Windows setup, run either three-case pilot from the repository root in **PowerShell 5.1** (the script prompts for an OpenRouter key if needed):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_bank_powershell.ps1 -Experiment initial_only -Name initial-only-01
```

`initial_only` selects guidance once from the complete public customer task/question. That view stays active, and the model has no `request_skill` tool.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_bank_powershell.ps1 -Experiment initial_with_requests -Name initial-requests-01
```

`initial_with_requests` makes the same initial selection and exposes policy topics plus `request_skill`. Follow-ups use the focused request, observations, and ordered visible execution history; the original task is background. Previous policy deliveries and receipts are excluded. A successful refresh replaces the active view. A failed refresh returns an explicit failure receipt and retains the previous view.

The acting-model prompt requires a lookup before executing a catalogued procedure whose own policy is absent from the active view. Knowing a tool's arguments or seeing a procedure mentioned as a prerequisite is insufficient. Missing policy must not be interpreted as permission to act. This remains a model instruction, not an execution gate or automatic per-turn refresh.

Both pilots run `transfer_funds-021`, `pay_bill-004`, and `set_safety_box-032`, once each in the reduced arm, with identical acting-model settings, repaired Bank environment, restored tools, fixed policy, and budgets. The initial selector is Qwen Flash, configured independently under `skill.resolver.options.selector_model` in the two `examples/agent/sopbench.bank.initial_*.json` files. Existing regex configurations still use their original resolver.

For all 34 cases, run the two reduced-only experiments sequentially:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_bank_powershell.ps1 -Experiment initial_only34 -Name initial-only-34
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_bank_powershell.ps1 -Experiment initial_with_requests34 -Name initial-requests-34
```

Each uses a fresh batch-model selection for the initial task. The second also lets the acting model request another selection. Each saves its own results and records inference and selector usage separately.

Add `-CheckOnly` to check setup/catalog access without model calls. Use a different `-Name` for each new experiment; use `-Resume` with its original mode/name only to finish an unchanged run. The wrapper prepares a frozen snapshot and starts/stops its own local service.

Inspect results under `experiments/bank-windows/runs/<Name>/results`:

- `manifest.json`: snapshot identity and `preflight_skill[0].response.metadata.condition_catalog`, including stable condition IDs, readable predicates, referencing nodes, and catalog/tree fingerprints. The service exposes this as `POST /v1/catalog`; evaluated resolutions submit a fingerprint-bound `condition_evaluations` payload to `/v1/resolve`.
- `episodes/*.json`: `skill` events contain selector inputs/output/evidence and retries in `response.metadata.selector`, evaluated conditions, traversal, terminal/fragment IDs, view hashes, and usage. `model_response` and `tool` events show acting-model decisions and outcomes.
- `summary.json`: benchmark results and separate `usage.reduced.inference_usage` / `selector_usage`, alongside combined totals and latency. Selector transport retries are recorded per selection; provider usage for unsuccessful transport attempts may be unavailable.
- `incomplete/` and `diagnostics/`: partial traces and errors. `execution-summary.json` reports resource use and unresolved counts even if a run cannot yet be scored. A failed initial selection is `unresolved`, with no fallback or formatting-repair model calls. Incomplete episodes are retained for inspection and must be resolved before official scoring; do not report them as completed benchmark failures.

For a saved, completed run:

```powershell
experiments/bank-windows/.venv/Scripts/python.exe -m skill_agent inspect-skills experiments/bank-windows/runs/initial-requests-01/results
```

Pilot acceptance concerns correct inputs, mode behaviour, traversal, and different views on follow-up—not an accuracy advantage. Historical rolling results are descriptive comparisons; a controlled comparison requires a fresh matched rolling run.

Local validation: 34 checks passed. Of six live episode attempts, five completed and one initial Qwen selection was unresolved due to malformed output at the output-token budget. No completed episode requested follow-up guidance; a separate manual live probe correctly switched from transfer to account-balance guidance using a focused request and saved visible trace. The detailed local report is `experiments/bank-windows/batch-selector-pilot.md`. Selector formatting reliability and autonomous request behaviour remain to be established before expanding the run.

## Dependency pilot

From the repository root, the reusable build script runs the standard compiler and both
Gemini stages. Use a Python installation with `py -3.12 -m pip install -e '.[skill-compiler]'`
and set `GEMINI_API_KEY` in your shell.

```powershell
$python = py -3.12 -c "import sys; print(sys.executable)"
& scripts/build_skill_dependencies.ps1 -Skill skills/sopbench_bank_fixed_policy -Output experiments/bank-gemini-build -Python $python -AcceptGeminiProposal
Start-Process experiments/bank-gemini-build/base-tree.html
Start-Process experiments/bank-gemini-build/dependency-tree.html
```

For another skill, substitute its directory and a fresh output path. The input is
`initial.md` and a feature catalog, either the skill's `feature_schema.json` or the parent
directory's `feature_schemas.json`. Use `-Features` to point to another catalog and `-Model`
to choose the Gemini model. The script uses a skill-local `extraction_prompt.md` when present.
Use `-Prompt` to supply a different prompt.
Pass `-BaseBundle` with an existing compiled skill to rerun dependency extraction without
paying for IR extraction again. Omit `-AcceptGeminiProposal` to review the proposal first,
then pass the reviewed JSON with `-ReviewedDependencies` and reuse the compiled base.

The first stage produces the IR. The dependency stage reviews up to six source procedures
per Gemini call. Each call receives the compiled node IDs, conditions, and conclusion text
for the source procedures and candidate prerequisites. Gemini returns one review per source,
including an empty list when there are no candidates. The compiler maps node pairs to rule
IDs and records the source procedure's line ranges for review.

The prompt favors including plausible prerequisites with a concrete connection to the source.
Those links deliver guidance even if the requirement might already hold or only apply in
some cases. Links whose endpoints lack a supported procedure-relevance predicate are recorded
for review. The graph sits alongside an exact copy of the base tree.

The dependency arm uses the initial-only policy with a separate skill bundle. It expands
procedure relevance before MCRDR traversal, so the prerequisite's exception and stopping rules
still apply. The Bank review is manually sourced for the three pilot cases. It links transfer
and safety-box guidance to admin authentication, then login. The separate balance procedure
is deferred because it is needed when the balance still needs checking.

For the 34-task Bank run with the existing Gemini dependency bundle, from the repository root:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_bank_powershell.ps1 -Experiment initial_only34 -Name dependency-initial-34 -DependencyBundle experiments/bank-gemini-next/with-dependencies/sopbench_bank_fixed_policy
```

This runs the reduced-skill condition once per task, selects guidance only at the start, and
does not expose `request_skill`. It keeps the normal selector retry setting. Results are saved
under `experiments/bank-windows/runs/dependency-initial-34`. To resume incomplete episodes:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_bank_powershell.ps1 -Experiment initial_only34 -Name dependency-initial-34 -Resume
```

The Gemini dependency bundle is local experiment data. Its proposed links require review; in
particular it links credit-card bill payment to login, but not to credit-card information.

To propose a new review with Gemini, set `GEMINI_API_KEY` and run:

```powershell
$python = 'experiments/bank-windows/.venv/Scripts/python.exe'
& $python -m rdr_engine.dependencies propose skills/sopbench_bank_fixed_policy experiments/bank-dependencies-proposed.json
```

Review each reason against the source procedure before passing a proposal to `build`. To use the checked-in Bank review
and run all three matched arms after the Windows setup:

```powershell
$python = 'experiments/bank-windows/.venv/Scripts/python.exe'
$bundle = 'experiments/dependency-pilot/sopbench_bank_fixed_policy'
& $python -m rdr_engine.dependencies build skills/sopbench_bank_fixed_policy examples/agent/sopbench.bank.dependencies.review.json $bundle
& scripts/run_bank_powershell.ps1 -Experiment initial_only -Name dep-baseline-01 -SingleSelection
& scripts/run_bank_powershell.ps1 -Experiment initial_with_requests -Name dep-requests-01 -SingleSelection
& scripts/run_bank_powershell.ps1 -Experiment initial_only -Name dep-expanded-01 -DependencyBundle $bundle
```

The bundle destination must be new and retain the original skill directory name. All three
runs use one semantic selector attempt per resolution and separate result directories. The
dependency arm records direct selections and added links under `response.metadata.dependencies`
in skill events. Inspect the existing execution summary and incomplete traces if an episode
cannot be scored.

To view the dependency bundle in the existing HTML viewer:

```powershell
$compiled = 'experiments/dependency-pilot/sopbench_bank_fixed_policy/compiled'
py -3.12 skills/visualise_tree.py "$compiled/tree.json"
Start-Process "$compiled/tree_visualisation.html"
```

The viewer reads the adjacent `dependencies.json` automatically. Purple dashed arrows point
from procedures to prerequisites. Hover an arrow to read its reason, or use **Show dependencies**
to toggle the overlay. Only links approved for expansion are drawn.

To reproduce the telecom construction check without model calls:

```powershell
& $python scripts/check_telecom_dependencies.py experiments/dependency-pilot/telecom-demo
```

The check saves the review, resolution metadata and rendered Markdown. It selects MMS and
follows the mobile-data and cellular-service links. Line suspension and overdue bills are
deferred because their relevance depends on the observed cause.

## Colab / Linux

Once these repository changes are available in your clone, run the same setup inside the repository's venv. Start the service in the background, then run the pilot:

```bash
python -m pip install -e '.[skill-compiler]'
python scripts/prepare_sopbench_bank.py
python -m rdr_service prepare skills/sopbench_bank_fixed_policy --store experiments/bank-snapshots --output experiments/bank-snapshot-mcrdr.json
python -m rdr_service serve --store experiments/bank-snapshots --port 8766 > experiments/bank-service.log 2>&1 &
export MODEL_API_KEY='YOUR_OPENROUTER_API_KEY'
python -m skill_agent doctor --config examples/agent/sopbench.bank.pilot.json
python -m skill_agent run --config examples/agent/sopbench.bank.pilot.json
python -m skill_agent summarize experiments/sopbench-bank-pilot
```

For a notebook, run the commands inside a shell cell (`%%bash`) and use a secure notebook secret or interactive prompt for the key. A runtime reset removes local files unless you copy the results directory to persistent storage. These instructions have not yet been run on Colab.
