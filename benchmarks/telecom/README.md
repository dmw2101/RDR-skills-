# Telecom benchmark: prepared assets, not yet a three-arm experiment

Official source: [Sierra tau2-bench](https://github.com/sierra-research/tau2-bench),
release **v1.0.1**, commit `fc0055dc4e0a316c3f83133267fbd6faaa770992` (MIT).
Use the text, dual-control `telecom` domain and its **base** split. This is a
fixed 1.x release, not the newer 2.0 replacement tasks or necessarily the exact
inputs used for the original [tau2-bench paper](https://arxiv.org/abs/2506.07982).

## Prepare or check — PowerShell 5.1, from this repository root

Download the pinned code and Telecom data, and materialize the original skill:

```powershell
py -3.12 benchmarks/prepare_telecom.py
```

Install the official text runtime in a separate Python venv and check it offline:

```powershell
py -3.12 benchmarks/prepare_telecom.py --install
```

Recheck existing files and load the installed runtime without installing anything:

```powershell
py -3.12 benchmarks/prepare_telecom.py --check-only --check-runtime
```

These commands make **no model calls**, need no API key, and run no scored episodes.
No Docker, GPU, voice dependencies, or PDF utilities are required.
The download/install commands can be rerun, but refuse to overwrite altered assets.
The runtime check has passed locally: 114 base tasks, 13 agent tools, 30 customer
tools, an importable official evaluator, and an exact policy-to-skill match.
This is an installation check, not a benchmark score or a live model check.

## What to inspect

| Location | Contents |
|---|---|
| `skills/telecom/initial.md` | Exact full agent policy: main policy + technical-support manual |
| `skills/telecom/source.json`, `LICENSE.txt` | Source revision, file hashes, and license |
| `benchmarks/local-data/tau2-bench/data/tau2/domains/telecom/` | Official tasks, splits, agent database, user/device database, and alternative policies |
| `benchmarks/local-data/tau2-bench/src/tau2/domains/telecom/` | Executable support-agent and customer/device tools |
| `benchmarks/local-data/tau2-bench/src/tau2/evaluator/` | Official evaluation implementation |
| `benchmarks/local-data/telecom/provenance.json` | Prepared-source hashes and, after installation, resolved Python dependencies |
| `benchmarks/local-data/tau2-venv/` | Isolated benchmark Python environment |

The downloaded checkout, environment, and provenance are ignored by Git. The
preparation script, pin, documentation, and source skill are portable project files.
No external Git checkout is added as a submodule or embedded into the parent repo.

## Tasks and scoring

- **114 base evaluation tasks**; `small` has 20, `train` 74, `test` 40.
- The base tasks cover **29 cellular-service**, **36 mobile-data**, and **49 MMS**
  cases, often with several simultaneous faults.
- The complete generated pool has **2,285 tasks**. These splits overlap: do not
  add their counts or call the generated pool the standard base evaluation.
- A task is a support conversation, not a PDF. The agent uses account tools;
  a separate simulated customer can inspect and change their phone settings.
- Preserve the official initial state, customer simulator, termination rules,
  and evaluator. Success concerns the required final state and task assertions,
  not JSON field exact match. Account for customer-model usage separately.
- Task files include private simulator instructions, setup, and expected actions.
  **Task IDs themselves reveal faults.** Neither IDs, hidden state, reference
  actions, nor evaluation criteria may be used as agent/resolver input.

## Change needed for full / reduced / none

This is a **new domain integration, not a rewrite**, but not a config-only swap.

1. Define Telecom features from visible conversation/tool observations; extract
   and review the skill IR, compile fragments/tree, and add Telecom case building.
   Current natural-language case building only recognizes the existing domains.
2. Add a Telecom agent wrapper through the upstream agent factory/registry.
   Keep the official orchestrator in control of both speakers and environment.
   Before each acting-model call, reuse the skill resolver and rolling-selection
   policy: full policy, current selected policy, or no policy. Do not leak the
   original full domain policy through another prompt field in reduced/none.
3. Bridge official trajectories/rewards into our experiment records and reports.
   Log agent, customer, and resolver tokens/costs separately, along with turns,
   status, success, and selected-view hashes. The PDF-specific loop currently
   treats an assistant reply without tools as completion; Telecom must instead
   continue the conversation under the official orchestrator.
4. Add a Telecom setup/config path. Keep agent model, customer model, task IDs,
   seeds, budgets, tools, and scorer fixed across arms. Retain neutral tool schemas
   in all arms; remove procedural policy/catalog/request tool in the blind arm.
   Use only agent-visible context for selection; include required prerequisites.

Reusable unchanged: RDR engine, compiler/renderer mechanics, versioned skill
service contract, HTTP resolver, static full-skill resolver, and most usage/event
accounting. PDF/VAREX commands and the current Colab setup remain unchanged.

Remaining: implement those four integration pieces, verify skill blindness and
per-turn selection offline, then run a small paid pilot before a full comparison.
