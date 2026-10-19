# RDR Skills

This research prototype represents procedural skills as Ripple-Down Rule (RDR) trees. A tree selects relevant parts of a Markdown skill for a task, while the underlying RDR engine also supports adding localized exceptions from feedback.

## What is here

- `src/rdr_engine/`: standalone RDR engine, skill compiler, and skill renderer.
- `src/rdr_service/`: versioned HTTP skill-resolution service, isolating RDR internals.
- `src/skill_agent/`: independent model/tool runner and three-arm PDF evaluation pipeline.
- `skills/`: seed skills, source metadata, feature schemas, and compiled examples.
- `benchmarks/`: benchmark setup and data-preparation notes. Large datasets are not stored here.
- `tests/`: automated tests for the engine and skill pipeline.

The current skill pipeline extracts rules from a Markdown skill, compiles them into fragments and an RDR tree, then renders selected Markdown for a task. Multiple task categories are supported. The compiler still duplicates decision nodes and caps the tree at six conditional rules. The agent harness compares no skill, full skill, and selected skill through a stable HTTP boundary. PDF/VAREX runs use a Python venv on Windows or Linux/Colab. Current pilot configurations automatically refresh guidance before every model call using the task initially and the last three assistant turns thereafter (`run.skill_loading="rolling"`); the original model-initiated mode remains selectable. See [agent evaluation](docs/agent-evaluation.md) for accounting and limitations. Trace-driven updates and oracle integration remain separate future work.

## Run commands

Use the [PowerShell 5.1 command guide](docs/commands.md) for copy-paste instructions to:

1. Build a new tree from a skill.
2. Query an existing tree with a task type.
3. Query an existing tree with a natural-language task.

The command guide assumes PowerShell is already open at this repository's root. It avoids inline JSON arguments, which PowerShell 5.1 can pass incorrectly to Python.

For model-based evaluation, use the [agent evaluation commands](docs/agent-commands.md): create a venv, freeze a compiled skill, start the service, prepare VAREX, configure your provider, run, and score. Docker is not required. Episodes start with a capability catalog and no skill text; the model chooses when to call `request_skill`. Each request replaces active guidance using only its current request/context. Start with the [two-document request pilot](docs/agent-commands.md#two-document-skill-request-check) before another 30-document run; `inspect-skills` shows actual calls and selected-view hashes.

For a Colab T4 without provider rate limits, use the optional [local Qwen3.5-4B backend](docs/local-qwen.md). Hosted API mode remains available. Real T4 inference/benchmark verification is pending.

## Further documentation

- [RDR engine overview](docs/rdr-engine.md)
- [Base-skill-to-RDR implementation plan](docs/base-skill-to-rdr-plan.md)
- [Skills and benchmark inventory](benchmarks/README.md)
- [Agent evaluation architecture, interfaces, and limits](docs/agent-evaluation.md)
