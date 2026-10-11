# RDR Skills

This research prototype represents procedural skills as Ripple-Down Rule (RDR) trees. A tree selects relevant parts of a Markdown skill for a task, while the underlying RDR engine also supports adding localized exceptions from feedback.

## What is here

- `src/rdr_engine/`: standalone RDR engine, skill compiler, and skill renderer.
- `skills/`: seed skills, source metadata, feature schemas, and compiled examples.
- `benchmarks/`: benchmark setup and data-preparation notes. Large datasets are not stored here.
- `tests/`: automated tests for the engine and skill pipeline.

The current skill pipeline extracts rules from a Markdown skill, compiles them into fragments and an RDR tree, then renders selected Markdown for a task. The ALFWorld demo can identify multiple task categories in one natural-language query; membership conditions select all matching guidance. The compiler still duplicates decision nodes on both branches and caps the tree at six conditional rules. Trace-driven updates, an oracle/verifier integration, and benchmark evaluation are not yet connected.

## Run commands

Use the [PowerShell 5.1 command guide](docs/commands.md) for copy-paste instructions to:

1. Build a new tree from a skill.
2. Query an existing tree with a task type.
3. Query an existing tree with a natural-language task.

The command guide assumes PowerShell is already open at this repository's root. It avoids inline JSON arguments, which PowerShell 5.1 can pass incorrectly to Python.

## Further documentation

- [RDR engine overview](docs/rdr-engine.md)
- [Base-skill-to-RDR implementation plan](docs/base-skill-to-rdr-plan.md)
- [Skills and benchmark inventory](benchmarks/README.md)
