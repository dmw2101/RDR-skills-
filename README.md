# RDR Skills

This research prototype represents procedural skills as Multiple Classification Ripple-Down Rule (MCRDR) trees. A tree selects every relevant part of a Markdown skill for a task, while the underlying engine also supports per-conclusion corrections from feedback: refinements, stopping rules, and new rules, each checked against cornerstone cases.

## What is here

- `src/rdr_engine/`: standalone MCRDR engine, skill compiler, and skill renderer.
- `src/rdr_service/`: versioned HTTP skill-resolution service, isolating RDR internals.
- `src/skill_agent/`: independent model/tool runner and three-arm PDF evaluation pipeline.
- `skills/`: seed skills, source metadata, feature schemas, and compiled examples.
- `benchmarks/`: benchmark setup and data-preparation notes. Large datasets are not stored here.
- `tests/`: automated tests for the engine and skill pipeline.

The current skill pipeline extracts rules from a Markdown skill, compiles them into fragments and a flat MCRDR tree (one unconditional general-guidance rule plus one rule per condition, with no rule limit), then renders the fragments of every terminal conclusion for a task. Multiple task categories in one natural-language query are supported; membership conditions select all matching guidance. The agent harness compares no skill, full skill, and selected skill through a stable HTTP boundary. PDF/VAREX runs use a Python venv on Windows or Linux/Colab. Current pilot configurations automatically refresh guidance before every model call using the task initially and the last three assistant turns thereafter (`run.skill_loading="rolling"`); the original model-initiated mode remains selectable. The `varex.mcrdr.*` configurations rerun the same experiments with the MCRDR engine serving the reduced arm. See [agent evaluation](docs/agent-evaluation.md) for accounting and limitations. Trace-driven updates and oracle integration remain separate future work.

## Run commands

Use the [PowerShell 5.1 command guide](docs/commands.md) for copy-paste instructions to:

1. Build a new tree from a skill.
2. Query an existing tree with a task type.
3. Query an existing tree with a natural-language task.

The command guide assumes PowerShell is already open at this repository's root. It avoids inline JSON arguments, which PowerShell 5.1 can pass incorrectly to Python.

For model-based evaluation, use the [agent evaluation commands](docs/agent-commands.md): create a venv, prepare VAREX, configure your provider, run, and score. Full/reduced also use a frozen skill snapshot and local service. The current rolling mode supplies guidance automatically each turn; optional model-initiated mode exposes a catalog and `request_skill`. The [skill-blind baseline](docs/agent-commands.md#skill-blind-baseline-only) uses `run.conditions=["none"]` and receives only neutral task instructions and execution tools, with no skill catalog, tool, guidance, or refreshes. Its setup needs no skill service. Docker is not required; `inspect-skills` shows actual calls and selected-view hashes.

For a Colab T4 without provider rate limits, use the optional [local Qwen3.5-4B backend](docs/local-qwen.md). Hosted API mode remains available. Real T4 inference/benchmark verification is pending.

## Further documentation

- [RDR engine overview](docs/rdr-engine.md)
- [Base-skill-to-RDR implementation plan](docs/base-skill-to-rdr-plan.md)
- [Skills and benchmark inventory](benchmarks/README.md)
- [Agent evaluation architecture, interfaces, and limits](docs/agent-evaluation.md)
