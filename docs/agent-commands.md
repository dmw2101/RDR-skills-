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

All conditions start with the same capability catalog and **no skill text**. The model chooses whether/when to call `request_skill`; no initial call is forced. Each request replaces active guidance using only its current request/context. Full returns all guidance, reduced returns a selected view, and none returns empty guidance. Requests share the existing 20-model-call budget.

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

Resume an interrupted run with the same configuration and unchanged artifacts. Completed episodes, including failures, are not rerun:

```powershell
.\.venv\Scripts\python.exe -m skill_agent run --config examples/agent/varex.pilot.json --resume
```

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
