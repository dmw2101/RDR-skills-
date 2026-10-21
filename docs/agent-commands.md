# Agent evaluation commands — PowerShell 5.1

Run these from the repository root. The commands use the venv's Python directly, so PowerShell activation/execution-policy changes are unnecessary. Docker is not needed.

## 1. Install dependencies

Creates a venv and installs the skill service, benchmark preparation/scoring tools, and pinned PDF libraries. No Gemini dependency is needed for an existing compiled tree. If `.venv` already exists, use it and skip its creation.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[skill-service,agent-benchmarks,pdf-runtime]"
```

## 2. Freeze the existing PDF skill

Creates a service-owned immutable snapshot and a small public receipt for the runner. This uses the already-compiled PDF tree; it makes no model call.

```powershell
.\.venv\Scripts\python.exe -m rdr_service prepare skills/pdf --output experiments/pdf-snapshot.json
```

## 3. Start the skill service in a second PowerShell window

Leave this running. It returns selected Markdown when the agent sends a task. Both windows must already be at the repository root.

```powershell
.\.venv\Scripts\python.exe -m rdr_service serve
```

## 4. Prepare the benchmark

Downloads the pinned VAREX dataset/PDFs and official scorer into the ignored local-data folder. This may be a substantial download; it does not call a model. Metadata reads exclude image/text payloads; document IDs containing parentheses are supported.

```powershell
.\.venv\Scripts\python.exe -m skill_agent prepare
```

For a smaller first setup, download only the first five deterministic dev PDFs into a separate folder:

```powershell
.\.venv\Scripts\python.exe -m skill_agent prepare --output benchmarks/local-data/varex-dev-5 --dev-limit 5
```

Set `benchmark.options.data_dir` in your config to `benchmarks/local-data/varex-dev-5`. This preserves the same dev ordering as full preparation but includes no held-out PDFs. For a full evaluation, use the original command and full dataset directory.

Python text extraction works with the installed libraries. For command-line PDF tools and OCR, install Poppler (`pdftotext`, `pdfinfo`, `pdftoppm`), Tesseract, and qpdf on the host and add their executable folders to `PATH`. `doctor` reports which tools are available; missing tools do not prevent Python-only tasks. Windows installations of Tesseract also need `TESSDATA_PREFIX` configured when required by that installation.

## 5. Configure your model

Open `examples/agent/varex.pilot.json` and replace `base_url` and `model` with your provider's OpenAI-compatible endpoint/model. The endpoint must support Chat Completions **function/tool calling**. Use the `/v1` base URL, not `/chat/completions`. The runner does not require an OpenAI-hosted model.

Set the key in the current PowerShell session (not in a tracked file):

```powershell
$env:MODEL_API_KEY = "YOUR_API_KEY"
```

The configuration stores only the environment variable's name. If the provider supports a seed, optionally add `"seed": 42` under model options. Unsupported model parameters are not silently removed; adjust the configuration for your endpoint. Do not put keys in JSON. The PDF tools use the same venv as the runner by default. To use another venv, set `benchmark.options.python_executable` to its Python path (use forward slashes in JSON). Remove any old `image` option from custom configurations.

## 6. Check prerequisites without spending inference tokens

Checks model configuration/key presence, the PDF venv and libraries, available system tools, prepared data, and a skill-service resolution. It does **not** test whether the provider accepts your key or supports tool calling.

```powershell
.\.venv\Scripts\python.exe -m skill_agent doctor --config examples/agent/varex.pilot.json
```

## 7. Run the pilot: no skill vs full skill vs reduced skill

Runs 5 dev documents under all three conditions. **This command makes paid/limited model calls using your configured provider.**

The current pilot uses `run.skill_loading="rolling"`: full/reduced receive guidance before every model call, using the task initially and the last three assistant turns thereafter. Full always has the whole skill and reduced gets a freshly selected view. `request_skill` is available in those arms but only acknowledges requests in this mode. None uses `skill-blind-v1`: no catalog, skill instructions, skill tool, or refreshes. `automatic_skill_refreshes` counts actual refreshes. Set `skill_loading` to `model_initiated` to restore optional tool-driven behaviour for full/reduced.

```powershell
.\.venv\Scripts\python.exe -m skill_agent run --config examples/agent/varex.pilot.json
```

After upgrading, rerun snapshot preparation and restart the service, then use a new run output directory. For the Colab pilot, upload the updated external `setup_rdr_colab.py` and run it with `--skill-pilot`; it prepares/restarts the service automatically.

### Two-document skill-request check

Prepare only FS5441-3E and LS-275si, chosen because their earlier traces needed substantial PDF inspection. No document guarantees skill use; this pilot checks actual behavior. It runs **2 documents × 3 conditions × 1 repetition = 6 episodes**.

```powershell
.\.venv\Scripts\python.exe -m skill_agent prepare --output benchmarks/local-data/varex-skill-requests --task-ids FS5441-3E LS-275si
```

Configure your provider in `examples/agent/varex.skill-requests.json`, then run:

```powershell
.\.venv\Scripts\python.exe -m skill_agent doctor --config examples/agent/varex.skill-requests.json
.\.venv\Scripts\python.exe -m skill_agent run --config examples/agent/varex.skill-requests.json
.\.venv\Scripts\python.exe -m skill_agent inspect-skills experiments/skill-requests-pilot
.\.venv\Scripts\python.exe -m skill_agent summarize experiments/skill-requests-pilot
```

`inspect-skills` makes no model/scorer calls and also works while a run is ongoing. Check `model_skill_calls`, `model_followup_skill_calls`, each focused request, and its Markdown hash/length. Tool definitions alone are not invocations. Two extraction requests can legitimately return the same view. Summaries also report `episodes_with_model_skill_calls`; the older `skill_refresh_calls` field counts completed tool executions including the first model request.

If the full 30-document dataset is already prepared, point the pilot config's `benchmark.options.data_dir` at it and skip the download. `run.task_ids` selects exactly the named IDs and overrides `limit`. To check only one document, leave just its ID in that list. Keep changed pilot settings in a new output directory.

In Colab, upload the updated setup script and run:

```python
%run /content/setup_rdr_colab.py --skill-pilot --base-url https://openrouter.ai/api/v1 --model qwen/qwen3.7-flash
```

Then run the three printed commands: run, inspect-skills, summarize. Existing prepared data containing both IDs is reused; otherwise only these two PDFs are downloaded. Use `--run-name another-request-pilot` for a fresh attempt. The selected `testing` checkout must contain this update before setup clones it.

### Qwen API: 30 documents with automatic guidance

Upload the updated external setup script (v10). This uses the earlier **Qwen3.7 Flash API**, not local Qwen3.5-4B. All 90 episodes use the same model/settings: 30 documents × none/full/reduced, one attempt each.

```python
%run /content/setup_rdr_colab.py --model qwen/qwen3.7-flash --provider alibaba --max-input-price 0.23 --max-output-price 0.92 --max-tokens 8192 --limit 30 --replicates 1 --concurrency 1 --run-name qwen-rolling-dev30
```

```python
!/content/rdr-skills-evaluation/.venv/bin/python -m skill_agent run --config /content/rdr-skills-evaluation/qwen-rolling-dev30.json
!/content/rdr-skills-evaluation/.venv/bin/python -m skill_agent inspect-skills /content/rdr-skills-evaluation/results/qwen-rolling-dev30
!/content/rdr-skills-evaluation/.venv/bin/python -m skill_agent summarize /content/rdr-skills-evaluation/results/qwen-rolling-dev30
```

OpenRouter is the setup's default API endpoint; other APIs remain selectable with `--base-url` and `--model`. Price ceilings are USD per million tokens, not whole-run caps. Use a fresh run name; do not resume earlier optional-loading results. For local PowerShell runs, the matching template is `examples/agent/varex.qwen-dev30.json` (prepare its data directory first).

### Third run: full vs reduced only

With setup v9, select only these two conditions and a fresh results directory. This runs **60 episodes**, retaining the existing full/reduced prompts, model settings, tools, budgets, rolling guidance, and relative execution order.

```python
%run /content/setup_rdr_colab.py --model qwen/qwen3.7-flash --provider alibaba --max-input-price 0.23 --max-output-price 0.92 --max-tokens 8192 --skill-loading rolling --limit 30 --replicates 1 --concurrency 1 --conditions full reduced --run-name qwen-rolling-dev30-full-reduced-run3
```

```python
!/content/rdr-skills-evaluation/.venv/bin/python -m skill_agent run --config /content/rdr-skills-evaluation/qwen-rolling-dev30-full-reduced-run3.json
!/content/rdr-skills-evaluation/.venv/bin/python -m skill_agent inspect-skills /content/rdr-skills-evaluation/results/qwen-rolling-dev30-full-reduced-run3
!/content/rdr-skills-evaluation/.venv/bin/python -m skill_agent summarize /content/rdr-skills-evaluation/results/qwen-rolling-dev30-full-reduced-run3
```

Custom JSON configs can set `run.conditions` to `["full", "reduced"]`. Omitted means all three arms. Reporting/scoring expect only selected arms. Full/reduced retain the same prompts and tool interface as earlier runs; new none attempts use the skill-blind protocol below.

### Skill-blind baseline only

Upload setup v10, then prepare 30 dev documents with one no-skill attempt each. Setup skips the skill snapshot and service. The model receives only the neutral benchmark instructions, task/schema, and ordinary execution tools. Model settings and budgets match the Qwen full/reduced comparison.

```python
%run /content/setup_rdr_colab.py --model qwen/qwen3.7-flash --provider alibaba --max-input-price 0.23 --max-output-price 0.92 --max-tokens 8192 --limit 30 --replicates 1 --concurrency 1 --conditions none --run-name qwen-skill-blind-dev30-run1
```

```python
!/content/rdr-skills-evaluation/.venv/bin/python -m skill_agent run --config /content/rdr-skills-evaluation/qwen-skill-blind-dev30-run1.json
!/content/rdr-skills-evaluation/.venv/bin/python -m skill_agent inspect-skills /content/rdr-skills-evaluation/results/qwen-skill-blind-dev30-run1
!/content/rdr-skills-evaluation/.venv/bin/python -m skill_agent summarize /content/rdr-skills-evaluation/results/qwen-skill-blind-dev30-run1
```

For three separate runs, repeat with fresh names ending in `run2` and `run3`. Manifests/summaries mark `condition_protocols.none` as `skill-blind-v1`; skill requests, automatic refreshes, and supplied skill characters must all be zero. Historical none runs included a catalog/tool and remain a different protocol. Custom none-only configs can use `"skill": {}`.

Resume an interrupted run with the same configuration and unchanged artifacts. Completed episodes, including failures, are not rerun:

```powershell
.\.venv\Scripts\python.exe -m skill_agent run --config examples/agent/varex.pilot.json --resume
```

## MCRDR runs (`MCRDR-testing` branch)

These repeat the experiments above with the MCRDR engine serving the reduced arm. Use the `MCRDR-testing` branch, a fresh snapshot receipt, and the `varex.mcrdr.*` configurations, which differ from the originals only in their receipt path and `experiments/mcrdr-*` output directory. See [MCRDR runs](agent-evaluation.md#mcrdr-runs) for what is and is not expected to change.

### Local PowerShell

From the repository root, after steps 1 and 4 above (the venv and prepared data can be reused):

```powershell
git switch MCRDR-testing
.\.venv\Scripts\python.exe -m pip install -e ".[skill-service,agent-benchmarks,pdf-runtime]"
.\.venv\Scripts\python.exe -m rdr_service prepare skills/pdf --output experiments/pdf-snapshot-mcrdr.json
```

Stop any service started from another branch, then start it again in a second PowerShell window so it runs the MCRDR code:

```powershell
.\.venv\Scripts\python.exe -m rdr_service serve
```

Configure the provider in the chosen MCRDR config exactly as in step 5, then check and run. Replace `qwen-dev30` with `pilot`, `skill-requests` or `deepseek-dev30` for the other experiments:

```powershell
.\.venv\Scripts\python.exe -m skill_agent doctor --config examples/agent/varex.mcrdr.qwen-dev30.json
.\.venv\Scripts\python.exe -m skill_agent run --config examples/agent/varex.mcrdr.qwen-dev30.json
.\.venv\Scripts\python.exe -m skill_agent inspect-skills experiments/mcrdr-qwen-rolling-dev30
.\.venv\Scripts\python.exe -m skill_agent summarize experiments/mcrdr-qwen-rolling-dev30
```

| Config | Output directory |
|---|---|
| `varex.mcrdr.pilot.json` | `experiments/mcrdr-varex-pilot` |
| `varex.mcrdr.skill-requests.json` | `experiments/mcrdr-skill-requests-pilot` |
| `varex.mcrdr.qwen-dev30.json` | `experiments/mcrdr-qwen-rolling-dev30` |
| `varex.mcrdr.deepseek-dev30.json` | `experiments/mcrdr-deepseek-dev30` |

The full-vs-reduced and skill-blind variants work unchanged: set `run.conditions` in a copy of an MCRDR config. A none-only run never contacts the service, so it gives the same protocol on both branches.

### Colab (in-repo setup script)

[`scripts/setup_mcrdr_colab.py`](../scripts/setup_mcrdr_colab.py) sets up and optionally runs any MCRDR experiment from the `MCRDR-testing` branch. It is a new script written for this branch, **not** a copy of the external `setup_rdr_colab.py` (which still pulls `testing` and the single-classification engine), and it has not yet been run on Colab. It uses its own folder (`/content/rdr-skills-mcrdr`) and service port (8766), so it can share a runtime with the external script without touching its checkout, service or results.

Set the key first (not in a tracked file), then download and run the script. It needs only the standard library, and it clones the public repository itself:

```python
import os
os.environ["MODEL_API_KEY"] = "YOUR_API_KEY"
!wget -q -O /content/setup_mcrdr_colab.py https://raw.githubusercontent.com/dmw2101/RDR-skills-/MCRDR-testing/scripts/setup_mcrdr_colab.py
%run /content/setup_mcrdr_colab.py --experiment qwen-dev30 --run-name mcrdr-qwen-rolling-dev30
```

In order, it:

1. installs Poppler, Tesseract, qpdf and venv support if any is missing;
2. clones or fast-forwards the branch. It stops if the checkout has local changes or is on another branch.
3. creates `.venv` and installs the extras, skipped when nothing changed;
4. prepares the experiment's VAREX documents, reused when already prepared;
5. freezes the PDF skill snapshot and (re)starts the MCRDR service it owns;
6. writes an absolute-path run config;
7. runs `doctor`, and stops on any error;
8. prints the run, inspect-skills and summarize commands.

Add `--run` to execute them as well. `--dry-run` prints every step and the generated config without changing anything. Generated configs, snapshot, logs and results are kept under `/content/rdr-skills-mcrdr/experiments/` (ignored by Git). An existing results directory is only reused with `--resume`, and nothing is deleted.

| Experiment | Base config | Prepared documents | Example |
|---|---|---|---|
| `qwen-dev30` (default) | `varex.mcrdr.qwen-dev30.json` | first 30 dev | `--experiment qwen-dev30 --run-name mcrdr-qwen-rolling-dev30` |
| `deepseek-dev30` | `varex.mcrdr.deepseek-dev30.json` | first 30 dev | `--experiment deepseek-dev30 --run-name mcrdr-deepseek-dev30` |
| `pilot` | `varex.mcrdr.pilot.json` | first 5 dev | `--experiment pilot --run-name mcrdr-pilot --base-url https://openrouter.ai/api/v1 --model qwen/qwen3.7-flash` |
| `skill-requests` | `varex.mcrdr.skill-requests.json` | FS5441-3E, LS-275si | `--experiment skill-requests --run-name mcrdr-skill-requests --base-url https://openrouter.ai/api/v1 --model qwen/qwen3.7-flash` |

The pilot and skill-requests configs contain provider placeholders, so the script requires `--base-url` and `--model` for them. With no overrides, a run uses exactly the settings of its `varex.mcrdr.*` config. `--limit N` selects the first N dev documents (1–30) and prepares a matching `varex-dev-N` folder.

Overrides use the same flags as the external script:
- `--model`, `--base-url`
- `--provider`, `--max-input-price`, `--max-output-price` (USD per million tokens)
- `--max-tokens`, `--limit`, `--replicates`, `--concurrency`
- `--conditions`, `--skill-loading`
- `--backend local`: the pinned local Qwen3.5-4B server on a T4 GPU, with concurrency 1 and a 30-minute attempt timeout. The api-only flags are rejected.

The external script's variants map as follows:

```python
# Full vs reduced only (60 episodes)
%run /content/setup_mcrdr_colab.py --experiment qwen-dev30 --conditions full reduced --run-name mcrdr-qwen-rolling-dev30-full-reduced-run1
# Skill-blind baseline (no snapshot or service is prepared; identical protocol on both branches)
%run /content/setup_mcrdr_colab.py --experiment qwen-dev30 --conditions none --run-name mcrdr-qwen-skill-blind-dev30-run1
# Local Qwen on a T4 GPU, no provider key
%run /content/setup_mcrdr_colab.py --experiment pilot --backend local --run-name mcrdr-varex-qwen-local
```

Rerunning the script is safe: it reuses everything already in place and restarts only its own service. Check that `doctor` reports `skill_service: ok` and that the printed resolver revision differs from the single-classification runs before spending tokens.

## 8. Score and inspect results

Runs official VAREX scoring, records failures, and produces paired comparisons. No model calls are made.

```powershell
.\.venv\Scripts\python.exe -m skill_agent summarize experiments/varex-pilot
```

Read `experiments/varex-pilot/summary.json`. Exact prompts, tool traces, selected skill text and usage are in `episodes/`; `manifest.json` pins the run. Official per-field CSVs and exported predictions are in `scores/`. These files contain document content/labels after scoring: keep them private as appropriate. Data, snapshots, test dependencies and results are ignored by Git.

Episode filenames use `<document>_<condition>_<hash>.json`, e.g. `fha2226_reduced_<hash>.json`. The full original hash of document ID, condition and repetition prevents filename collisions and distinguishes repetitions. Filename-unsafe characters in IDs are percent-encoded. Existing hash-named episodes remain readable. To rename an existing stopped run without changing its contents or scores:

```powershell
.\.venv\Scripts\python.exe -m skill_agent rename-episodes experiments/varex-pilot
```

For the final run, copy the pilot configuration to a new file, change `split` to `heldout`, `limit` to `null`, `replicates` to `3`, and choose a **new** `output_dir`. Pass that file to `run` and its output directory to `summarize`. Freeze the configuration and non-inferiority margin before inspecting held-out results.

## 9. Report across runs and engines

`skill_agent report` reproduces every number in the VAREX pilot slides from completed, summarized run folders. It makes no model calls and does not rescore. It also compares engines side by side (for example SCRDR against MCRDR). Run `summarize` on each folder first; the report reads the official per-document scores that `summarize` writes to `scores/`.

A spec file groups folders into **series** (one per engine) and **runs**. One run may combine folders, as the pilot did: a full/reduced run plus a separate skill-blind run over the same documents. Folder paths are relative to the spec file. Start from [`examples/agent/report.template.json`](../examples/agent/report.template.json): copy it into the `experiments` folder that holds the runs, and edit the folder names to match yours. On Colab with the MCRDR setup script that is `/content/rdr-skills-mcrdr/experiments/`, with your partner's single-classification run folders downloaded into its `scrdr/` subfolder.

```powershell
.\.venv\Scripts\python.exe -m skill_agent report --spec experiments/report-spec.json
```

Linux / Colab: `.venv/bin/python -m skill_agent report --spec experiments/report-spec.json`.

It writes `report.json` (every value, unrounded) and `report.md` (deck-style tables) to `report/` beside the spec, or to `--output`.

| Slide | Reported per series |
|---|---|
| Field-level exact match by run | Normalized field exact match per run and condition; pooled row with the field count; correct fields per condition |
| Completion and perfect documents | Completed episodes and perfect documents per condition; incomplete episodes are listed and scored as zero |
| Reduced vs full by run | Reduced − full and reduced − skill-blind, per run and pooled, with paired document-bootstrap 95% intervals |
| Usage | Median (p50), mean and p90 input tokens per episode; mean output tokens; reported cost total and per-episode mean (`not reported` when a provider omitted it); median reduction vs full |
| Variability | Share of input tokens from the five highest-usage documents; highest-input episodes with the paired episodes' input tokens |

With two or more series the report adds:
- **A series comparison:** per-condition differences in exact match, completion, perfect documents, and median and mean input.
- **A guidance-identity check:** reduced-arm requests that are identical across series (for example the turn-0 request, which uses the shared task text) must return identical guidance. Because the MCRDR bundles render byte-identically to the single-classification ones, a differing view signals a setup problem.

The report refuses to mix data silently:
- folder or score integrity changes;
- unsummarized folders;
- a condition repeated within a run;
- unpaired documents within a run;
- a series mixing engines.

Differences in model settings, budgets, prompt or data fingerprint between folders are listed as warnings and in the provenance table.

## Linux / Colab venv

Local Qwen3.5-4B is also supported without a provider account; see [local Qwen setup](local-qwen.md). The API commands above remain supported.

From the repository root, install host tools and create the venv (in Colab, run this in a `%%bash` cell):

```bash
apt-get update -qq
apt-get install -y poppler-utils tesseract-ocr qpdf fonts-dejavu-core python3-venv
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[skill-service,agent-benchmarks,pdf-runtime]'
```

Use `.venv/bin/python` in place of `.\.venv\Scripts\python.exe` for all commands above. On Linux outside Colab, `apt-get` may need `sudo`. Start the skill service in a separate terminal/background notebook process; run all steps in the same runtime. Set `MODEL_API_KEY` in the notebook environment before starting the runner. Venv activation does not persist between Colab shell cells; always use the explicit Python path.

Each attempt gets a fresh temporary folder containing only the PDF and public schema. Reference answers are excluded from prompts/tool inputs and read by the scorer afterward. Commands execute with local user permissions; the venv does not block access to other host files. See [evaluation limits](agent-evaluation.md).

## Tests (offline; no key needed)

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

Optional official-scorer parity check (downloads the pinned scorer, needs benchmark dependencies):

```powershell
$env:SKILL_AGENT_NETWORK_TESTS = "1"
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_agent_varex.py"
Remove-Item Env:SKILL_AGENT_NETWORK_TESTS
```

See [architecture and current limits](agent-evaluation.md) before interpreting results.
