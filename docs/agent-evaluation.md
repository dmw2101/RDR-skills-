# Agent evaluation v1

## Separation from RDR

`skill_agent` never imports `rdr_engine`, reads a compiled tree, or calls Gemini. It sends a task and observed context to `POST /v1/resolve` and receives Markdown. `rdr_service` alone owns bundle loading, feature inference, traversal, and rendering. Changing those internals requires changing the service adapter, not the runner. Keep the public v1 contract stable; breaking public changes need a new version.

Request: `skill_id`, `task` (text), `context` (observed JSON facts), `artifact_id` (opaque snapshot ID).

Response: `markdown`, `artifact_id`, `source_fingerprint`, `resolver_revision`, opaque `metadata`, and `usage` (`model_calls`, `input_tokens`, `output_tokens`, `cost_usd`; unknown values are null). Metadata is logged, never interpreted by the agent. Usage must include future LLM-based selection costs.

Snapshots freeze source + compiled bundle + implementation revision. The agent only sees their public receipt. Modifying live source files does not alter an existing snapshot. Changed backend code requires a new snapshot and experiment; old runs refuse to silently resume with changed code. Snapshot preparation does not call Gemini.

## What runs

Each document/replicate has fresh attempts with **none**, **full**, and **reduced** skill views. Only the active skill prompt section changes. Full and reduced must share the same source fingerprint. No fallback to full guidance is allowed if resolution fails.

`run_episode` performs policy → skill section → model → tools → observations until completion/budget. `SessionContext.load_skill(context, task=...)` is the shared refresh operation. A focused task uses only that request and its supplied context: the original task is not appended and prior feature values are not inherited. A refresh replaces the managed prompt section on the next call, not the audit record. The original objective, prior model-generated text and tool observations remain in history; this does not erase knowledge already seen.

The initial policy loads guidance once before the first call. Every condition then exposes the same built-in `request_skill` tool: `{"request":"OCR this PDF","context":{"source_kind":"scanned_image"}}`. `request` is required; `context` is optional and contains only relevant observed facts for this request. The tool resolves the already-assigned skill and returns a short receipt, not another copy of the Markdown in tool history. Full returns the full source, none returns no guidance, and reduced resolves a fresh view. No extra model call is made by the tool itself; refreshes remain inside the existing episode/model-call budget. Backend failures abort the attempt without falling back to old guidance. Automatic skill discovery and tree updates remain separate work.

## PDF benchmark protocol

- Pinned [VAREX dataset](https://huggingface.co/datasets/ibm-research/VAREX) revision `2dfc3386a4567c7d56bf1abf4d12ff42afed27b6` and [official scorer](https://github.com/udibarzi/varex-bench/tree/3cce9c18530748cfc843f38c02e2f43e0694d167/evaluation). The scorer and field exclusions are downloaded unchanged with provenance hashes.
- Flat documents: sort IDs, shuffle with seed 42, first 30 dev, remainder held out. This is **our split**, not an official train/validation/test split. The dataset's `Flat/Nested/Table` labels describe structure.
- Preparation streams only metadata columns and supports `--dev-limit N` for downloading just the first N dev PDFs. Pilot snapshots expose only those dev tasks and no held-out tasks; provenance records the selection and complete Flat population fingerprint. Full preparation retains the original split.
- Each attempt gets a fresh temporary folder with the original PDF at `inputs/document.pdf` and public schema at `inputs/schema.json`. Shell commands run there using the configured venv (runner Python by default), through PowerShell on Windows or `/bin/sh` on Linux/Colab. The agent writes `answer.json`; cleanup removes the temporary folder after collection.
- Labels are excluded from prompts and public input folders; the scorer reads them after inference. A venv isolates dependencies, not filesystem/network access: arbitrary shell commands retain host user permissions and could find labels elsewhere on disk. This is a controlled, non-adversarial evaluation setup, not enforced label isolation. Credential-like environment variables are removed from tool subprocesses; this is not a security boundary. Failures produce explicit `_error` predictions and remain in the denominator. No answer repair or silent output truncation.
- Reports include normalized field exact match, perfect-document rate, per-document paired bootstrap intervals, model/selector usage, timings, and failure coverage. Unknown usage/cost is null, not zero. Setup selection is logged separately under `preflight_skill`; include it when calculating total experimental cost. Provider-neutral dollar estimates are not invented.
- This is **our raw-PDF agent protocol**, not exact direct-inference VAREX or SkillOpt reproduction. The scorer is official; the agent protocol is ours. A confidence interval alone does not establish equivalence: choose a non-inferiority margin before final held-out evaluation.

Default pilot: 5 dev PDFs × 1 replicate × 3 conditions. Main run: all held-out PDFs × 3 replicates. Defaults: temperature 0, concurrency 1, 20 model calls/10 minutes per attempt; each shell command has 60 seconds and 128 KB output. Commands are terminated on timeout/output overflow, including their active descendants. The venv adds no CPU/RAM/disk quotas. The `pdf-runtime` extra pins PDF packages; manifests capture the venv's installed package versions, Python/platform, shell, and available PDF tool paths/versions. Missing optional system tools are recorded as null. Keep the same host tools and venv for all three conditions.

## Main interfaces

| File | Interface / responsibility |
|---|---|
| `skill_agent/contracts.py` | `ResolveSkillRequest`, `ResolvedSkill`, `SkillResolver` |
| `skill_agent/resolvers.py` | `HttpSkillResolver`, `StaticSkillResolver` (full/empty) |
| `skill_agent/models.py` | `ModelClient.complete`, `OpenAICompatibleClient` |
| `skill_agent/local_models.py` | Optional `LocalTransformersClient`, FP16 Qwen server startup, two-turn tool smoke check |
| `skill_agent/runtime.py` | `Tool`, `SkillRequestPolicy`, `InitialSkillPolicy`, `SessionContext`, `SkillRequestTool`, `run_episode` |
| `skill_agent/benchmarks/` | `BenchmarkAdapter`, `VarexAdapter`, pinned preparation and scoring |
| `skill_agent/pdf_session.py` | `VenvPDFSession`, bounded local shell execution and venv provenance |
| `skill_agent/experiments.py` | factories, paired runs, atomic episode records, resume, reports |
| `rdr_service/backend.py` | `RDRBackend.prepare` and `.resolve`; sole RDR integration point |

Configuration factories use `module:Class` + options. Configuration files are trusted code configuration, not model-controlled input. To add a model, implement `complete(messages, tools)` returning `ModelResponse`. To add a benchmark, implement `prepare_runtime`, `tasks`, `open_session`, `score`, `system_prompt`, and `snapshot_fingerprint`. A session exposes `prompt`, `tools`, `collect`, and `close`. A scorer returns `summaries` and `document_metrics`: rows with `task_id`, `condition`, `replicate`, `metric`, `numerator`, `denominator`. This supports success-rate benchmarks as well as field-level metrics without changing the loop or paired analysis.

## Reproducibility and limits

Run manifests record resolved configuration, task IDs, package/code/Git revisions, host and venv dependency versions, dataset fingerprints, source fingerprint, and service receipt. Episode records contain exact requests/responses, tool calls, skill views, provenance and usage. Resume is at completed-episode boundaries and refuses changed configuration/artifacts; it does not retry already-recorded failures. Use a new output directory for a new experiment, including when switching from the previous Docker runtime. A crash may leave `.running`: first ensure no runner is active, then remove that exact run's lock before resuming. Local service binds only to loopback; expose it remotely only behind proper authentication/TLS.

The current PDF source is an existing supplied local snapshot with restrictive upstream provenance; this work does not clear those terms or fetch missing supporting guides. `FORMS.md`, `REFERENCE.md`, and upstream scripts are absent. Treat it as an incomplete skill snapshot, not the full upstream package. The filename is normalized to `initial.md` for Linux compatibility, without content changes.

Current PDF selection still uses deterministic heuristics and the existing six-rule compiler. Identical initial extraction tasks yield the same initial view; model-initiated requests can select different guidance later. An OCR-only request selects the OCR branch without forcing general extraction fragments; explicitly asking to extract text and use OCR selects both. Root instructions and structural dependencies still apply. The harness does not fix selection quality or remove the exponential-tree limit.

Implemented: service boundary/snapshots, generic loop, model-initiated focused refresh, OpenAI-compatible model adapter, three arms, local venv PDF/data/scorer adapters, accounting, resume, and reports. Hosted initial-only experiments have run on Colab, including 30 dev PDFs with Qwen3.7 Flash. Focused refresh is verified offline through the actual HTTP service and compiled PDF tree; live model verification of refresh and held-out experiments remain pending. No trace-based update integration is included.
