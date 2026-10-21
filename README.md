# RDR Skills

This research prototype represents procedural skills as Multiple Classification Ripple-Down Rule (MCRDR) trees. A tree selects every relevant part of a Markdown skill for a task, while the underlying engine also supports per-conclusion corrections from feedback: refinements, stopping rules, and new rules, each checked against cornerstone cases.

## What is here

- `src/rdr_engine/`: standalone MCRDR engine, skill compiler, and skill renderer.
- `skills/`: seed skills, source metadata, feature schemas, and compiled examples.
- `benchmarks/`: benchmark setup and data-preparation notes. Large datasets are not stored here.
- `tests/`: automated tests for the engine and skill pipeline.

The current skill pipeline extracts rules from a Markdown skill, compiles them into fragments and a flat MCRDR tree (one unconditional general-guidance rule plus one rule per condition, with no rule limit), then renders the fragments of every terminal conclusion for a task. The ALFWorld and PDF demos can identify multiple task categories in one natural-language query; membership conditions select all matching guidance. The separate offline Trace2Skill bridge supports recorded conditional proposals and reference exception insertion. Production trace-driven updates and benchmark evaluation are not yet connected.

## Run commands

Use the [PowerShell 5.1 command guide](docs/commands.md) for copy-paste instructions to:

1. Build a new tree from a skill.
2. Query an existing tree with a task type.
3. Query an existing tree with a natural-language task.

The command guide assumes PowerShell is already open at this repository's root. It avoids inline JSON arguments, which PowerShell 5.1 can pass incorrectly to Python.

## Further documentation

- [Offline Trace2Skill bridge: conditions, structured proposals, and exception insertion](docs/trace2skill-bridge.md)
- [Bridge offline verification and pending GPU checks](docs/trace2skill-bridge-verification.md)
- [RDR engine overview](docs/rdr-engine.md)
- [Base-skill-to-RDR implementation plan](docs/base-skill-to-rdr-plan.md)
- [Skills and benchmark inventory](benchmarks/README.md)
