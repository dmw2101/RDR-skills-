# SOPBench Bank evaluation

This experiment compares `none`, `full` and `reduced` on the 34 SOPBench Bank cases that share the [compiled policy](../skills/sopbench_bank_fixed_policy/README.md). The pilot uses two cases from different action types (six episodes), including an eligible and an ineligible case; the full configuration uses all 34 (102 episodes). These are a project-selected subset, not an official benchmark split.

Each episode receives a fresh upstream Bank environment, the same public tools and customer request, and the upstream non-LLM customer response when the assistant asks for information. The assistant ends the conversation with `exit_conversation`. The original SOPBench directed-graph evaluator scores the action sequence and final database. The surrounding agent loop, model settings and 20-call budget are our evaluation protocol, not an exact reproduction of the paper's full experiment.

The no-skill arm receives a neutral role prompt and the Bank tools. The full arm receives the complete policy on each model turn. The reduced arm automatically selects relevant fragments on each turn. A separate model selects action names from the **visible customer request and recent conversation/tool results**, including helpers when needed; the MCRDR service renders the matching fragments. It receives no private initial database, hidden target action, reference answer, or oracle trajectory. The same selector is not used for full or no-skill. Its model calls, input/output tokens and reported costs are included in reduced-arm totals. Selection errors or missed procedures can affect results.

`run.conditions` chooses arms, `run.replicates` chooses repetitions, `run.limit` chooses cases, and `run.task_ids` can choose exact subset IDs. Both supplied configs use one repetition and a sequential run. The configured Qwen and provider settings match the earlier PDF experiment; availability and account limits still apply. Change `model.options` and `skill.resolver.options.selector_model` together if using another provider/model. Store the key in an environment variable, not a config file.

## PowerShell 5.1: prepare once

Run from the repository root. Install the compiler dependency, fetch the pinned source and freeze the existing compiled skill:

```powershell
py -3.12 -m pip install -e ".[skill-compiler]"
py -3.12 scripts/prepare_sopbench_bank.py
py -3.12 -m rdr_service prepare skills/sopbench_bank_fixed_policy --store experiments/bank-snapshots --output experiments/bank-snapshot-mcrdr.json
```

Start the Bank skill service in this PowerShell window and leave it running. It uses port **8766**, leaving the PDF service's port available:

```powershell
py -3.12 -m rdr_service serve --store experiments/bank-snapshots --port 8766
```

## Run the two-case pilot

Open another PowerShell window at the repository root. Set your OpenRouter key in this window, check setup without making an inference call, run the pilot, then score it:

```powershell
$env:MODEL_API_KEY = "YOUR_OPENROUTER_API_KEY"
py -3.12 -m skill_agent doctor --config examples/agent/sopbench.bank.pilot.json
py -3.12 -m skill_agent run --config examples/agent/sopbench.bank.pilot.json
py -3.12 -m skill_agent summarize experiments/sopbench-bank-pilot
py -3.12 -m skill_agent inspect-skills experiments/sopbench-bank-pilot
```

`doctor` verifies the snapshot and service without calling the selection model; it does not prove the hosted API or selector works. `inspect-skills` shows selected actions, request context and view hashes. Episode JSON files contain the conversation, tool calls, selection metadata and per-call usage. Run the pilot before the full subset and inspect any failed episodes.

## Run all 34 cases

After the pilot, use the separate output directory in the 34-case config:

```powershell
py -3.12 -m skill_agent run --config examples/agent/sopbench.bank.dev34.json
py -3.12 -m skill_agent summarize experiments/sopbench-bank-dev34
py -3.12 -m skill_agent inspect-skills experiments/sopbench-bank-dev34
```

The summary reports SOPBench success, completion counts, mean and median input tokens per episode, elapsed time, reported cost and paired comparisons. The individual official component checks are in `scores/scoring-result.json`. Incomplete episodes count as failures; their partial official scores, when available, remain in that file. Missing provider usage is reported as incomplete rather than zero. The existing `report` command is specific to VAREX field metrics and does not support Bank scores.

Runs never overwrite an existing output directory. To resume an interrupted run with the same unchanged config and source, add `--resume`. For a new run or changed config, choose a new `run.output_dir`. A code or snapshot revision change prevents resuming an older run; already saved PDF results remain readable, and PDF experiments retain their existing configs.

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
