# RDR Skills

This research prototype represents procedural skills as Multiple Classification Ripple-Down Rule (MCRDR) trees. A tree selects every relevant part of a Markdown skill for a task, while the underlying engine also supports per-conclusion corrections from feedback: refinements, stopping rules, and new rules, each checked against cornerstone cases.

## What is here

```
src/                  the code (four Python packages)
  rdr_engine/           MCRDR engine, skill compiler and renderer, learned-rule layer
  rdr_service/          HTTP service that serves frozen skill trees to the agent
  skill_agent/          agent runner, experiments, scoring and benchmark adapters (VAREX, SOPBench Bank)
  trace_learner/        verifier-driven learning: failed traces -> new MCRDR rules
skills/               one folder per skill: initial.md (the skill) and, once compiled, compiled/ (its tree)
examples/
  agent/                run settings for agent experiments
  learning/             settings for the learner (models, validation)
notebooks/            Colab notebooks: MCRDR pilot runs, learning trial
scripts/              Colab setup, SOPBench Bank build, tree visualiser
benchmarks/           benchmark preparation; skillopt/ is a trimmed copy of Microsoft SkillOpt
tests/                one folder per package: engine, service, agent, learner, colab, benchmark_prep
docs/                 documentation; see docs/README.md for an index
```

Large datasets and run outputs are not stored here: `benchmarks/local-data/` and `experiments/` are ignored by Git. Run all tests with `python -m unittest discover -s tests -p "test_*.py"`.

The current skill pipeline extracts rules from a Markdown skill, compiles them into fragments and a flat MCRDR tree (one unconditional general-guidance rule plus one rule per condition, with no rule limit), then renders the fragments of every terminal conclusion for a task. Multiple task categories in one natural-language query are supported; membership conditions select all matching guidance. The agent harness compares no skill, full skill, and selected skill through a stable HTTP boundary. PDF/VAREX runs use a Python venv on Windows or Linux/Colab. Current pilot configurations automatically refresh guidance before every model call using the task initially and the last three assistant turns thereafter (`run.skill_loading="rolling"`); the original model-initiated mode remains selectable. The `varex.mcrdr.*` configurations rerun the same experiments with the MCRDR engine serving the reduced arm. See [agent evaluation](docs/agent-evaluation.md) for accounting and limitations. [Verifier-driven learning](docs/verifier-learning.md) diagnoses failed episodes with an agentic verifier (after Trace2Skill), adds validated exceptions and alternatives to the skill's MCRDR, and keeps a candidate tree only after re-running the affected tasks; it has been tested offline and against the pinned VAREX scorer, not yet in a live model run. A [Colab notebook](notebooks/verifier_learning_colab.ipynb) learns from a batch of held-out VAREX documents on OpenRouter models, then tests the learned tree on the 30 dev documents of the earlier pilot.

## Run commands

Use the [PowerShell 5.1 command guide](docs/commands.md) for copy-paste instructions to:

1. Build a new tree from a skill.
2. Query an existing tree with a task type.
3. Query an existing tree with a natural-language task.

The command guide assumes PowerShell is already open at this repository's root. It avoids inline JSON arguments, which PowerShell 5.1 can pass incorrectly to Python.

For model-based evaluation, use the [agent evaluation commands](docs/agent-commands.md): create a venv, prepare VAREX, configure your provider, run, and score. Full/reduced also use a frozen skill snapshot and local service. The current rolling mode supplies guidance automatically each turn; optional model-initiated mode exposes a catalog and `request_skill`. The [skill-blind baseline](docs/agent-commands.md#skill-blind-baseline-only) uses `run.conditions=["none"]` and receives only neutral task instructions and execution tools, with no skill catalog, tool, guidance, or refreshes. Its setup needs no skill service. Docker is not required; `inspect-skills` shows actual calls and selected-view hashes.

For a Colab T4 without provider rate limits, use the optional [local Qwen3.5-4B backend](docs/local-qwen.md). Hosted API mode remains available. Real T4 inference/benchmark verification is pending.

## Further documentation

- [Documentation index](docs/README.md)
- [RDR engine overview](docs/rdr-engine.md)
- [Base-skill-to-RDR implementation plan (archived)](docs/archive/base-skill-to-rdr-plan.md)
- [Skills and benchmark inventory](benchmarks/README.md)
- [Agent evaluation architecture, interfaces, and limits](docs/agent-evaluation.md)
- [Verifier-driven MCRDR learning](docs/verifier-learning.md)
