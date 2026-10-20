# Local Qwen3.5-4B on a Colab T4

The agent and RDR service are unchanged. A Hugging Face Transformers server runs the model locally and returns ordinary Chat Completions tool calls. `LocalTransformersClient` uses the same HTTP client as hosted APIs, with a tokenizer-based context guard. API dependencies remain optional; no provider key is needed for local mode.

## Colab setup script

Upload the updated **external** `setup_rdr_colab.py` from the project workspace (outside this repository), select a **T4 GPU** runtime, and run:

```python
%run /content/setup_rdr_colab.py --backend local --run-name varex-qwen
```

Setup pulls `testing`, reuses the venv/data, starts one resident model server, checks a structured tool call and its follow-up, and prints the run/scoring commands. Add `--run` to execute them automatically. No automatic results deletion: clear the old run directory yourself or choose another name. Use `--backend api` for the previous hosted-provider workflow; local mode ignores provider environment settings.

For MCRDR runs, the in-repo `scripts/setup_mcrdr_colab.py --backend local` does the same with the `MCRDR-testing` branch; see [MCRDR runs](agent-commands.md#colab-in-repo-setup-script).

## Local serving commands (Linux, from the repository root)

Use an existing `.venv` containing the benchmark/service/PDF dependencies. Install the local dependencies; select CUDA 12.6 wheels explicitly for T4 compatibility:

```bash
.venv/bin/python -m pip install torch==2.9.1 torchvision==0.24.1 --index-url https://download.pytorch.org/whl/cu126
.venv/bin/python -m pip install -e '.[local-model]'
.venv/bin/python -m skill_agent.local_models serve --port 8000
```

Leave the server running. In another terminal/cell, verify tool calling (two local inference calls):

```bash
.venv/bin/python -m skill_agent.local_models check --port 8000
```

In your experiment config, replace only the `model` section:

```json
"model": {
  "factory": "skill_agent.local_models:LocalTransformersClient",
  "options": {
    "base_url": "http://127.0.0.1:8000/v1",
    "model": "Qwen/Qwen3.5-4B@851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a",
    "context_window": 8192,
    "max_tokens": 2048,
    "temperature": 0,
    "seed": 42,
    "timeout": 300
  }
}
```

Set `run.concurrency` to `1`, `run.timeout_seconds` to `1800`, and choose an unused `run.output_dir`. Use the existing `skill_agent doctor`, `run`, and `summarize` commands. `doctor` checks configuration without loading the model; the separate `check` command tests inference/tool compatibility.

## Fixed settings and limits

- Server: unquantized FP16, CUDA device 0, SDPA attention, thinking off, no continuous batching or CPU offloading. Transformers 5.20.0 supplies the Qwen3.5 tool parser. Checkpoint and tokenizer use the same pinned revision.
- Context: the complete prompt (including tool definitions/history) plus the output allowance must fit 8192 tokens. Oversized requests fail explicitly; no history truncation. Raising `--context-window` is not a promise that the larger allocation fits the T4.
- All three skill conditions use the same model/settings; prompts, actual server token counts and timings are recorded as before. The manifest also records Transformers/PyTorch/tokenizer package versions. Weights/tokenizer caching is not answer or skill-view caching.
- PDF handling remains shell/Python extraction and OCR; image inputs are not added. Local GPU time is not zero compute cost, and Colab still has usage/disconnection limits.
- Offline integration checks do not establish real T4 throughput, memory fit, or VAREX accuracy. Confirm these with the pilot before expanding evaluation. This is not an exact reproduction of another paper's serving setup.

References: [Qwen model card](https://huggingface.co/Qwen/Qwen3.5-4B), [Transformers serving/tool calls](https://huggingface.co/docs/transformers/main/serve-cli/serving), [PyTorch CUDA wheels](https://pytorch.org/get-started/previous-versions/).
