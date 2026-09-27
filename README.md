# RDR skills and benchmarks

This repository holds base skills and their paired evaluation benchmarks. The `skills/` directory currently contains five unmodified initial skills from [Microsoft SkillOpt](https://github.com/microsoft/SkillOpt): SpreadsheetBench, ALFWorld, DocVQA, SearchQA, and LiveMathematicianBench. These are starting points for later RDR conversion, not optimized checkpoints. SearchQA is an intentionally near-empty seed suitable for an empty-start control.

`skills/sources.json` records the pinned upstream commit, original paths, and SHA-256 checksums. The copied files retain their original `initial.md` names so the experimental baseline stays unchanged. Their license is in `LICENSES/SkillOpt-MIT.txt`.

The `benchmarks/` directory now contains a pinned SkillOpt evaluation-code snapshot, lightweight published split manifests, source provenance, and on-demand preparation scripts. Large raw data is deliberately excluded from Git and is not downloaded until an experiment needs it. See `benchmarks/README.md`. No results in this repository should be interpreted as benchmark runs.

Anthropic's [official xlsx skill](https://github.com/anthropics/skills/tree/main/skills/xlsx) is an external comparison source only. It is not copied here because its [license](https://github.com/anthropics/skills/blob/main/skills/xlsx/LICENSE.txt) restricts copying and distribution. The spreadsheet file in this repository is SkillOpt's MIT-licensed seed skill, not Anthropic's skill.

## Base RDR engine

`src/rdr_engine/` is a standalone, domain-independent Python package implementing single-conclusion RDR traversal, oracle-driven learning, consistency checks, and versioned JSON persistence. It has no runtime dependencies and does not import or modify the skills or benchmarks.

With Python 3.10 or newer, run from the repository root (preferably in a virtual environment):

```sh
python -m pip install -e .
python -m rdr_engine.demo
python -m unittest discover -s tests -v
python -m unittest discover -s benchmarks -p test_preparation.py -v
```

On Windows, `py -3.12` can replace `python`. The demo and tests are offline; benchmark preparation tests use fixtures, not downloaded datasets.

See [the engine guide](docs/rdr-engine.md) for the three replaceable components, API, persistence format, and correspondence to `rdr.md`.

## Compile and preview a base skill

[The conversion plan](docs/base-skill-to-rdr-plan.md) is implemented through extraction, compilation, and rendering. Model extraction reads the whole skill and proposes source-referenced rules; inspect that JSON before compiling. Compilation and rendering are offline and do **not** call an oracle or Gemini. ALFWorld's `compiled/` directory contains the rule list shown in the first conversion example and its generated tree, so it can be previewed immediately.

These are **PowerShell commands** (not Command Prompt commands). Run them from the repository root:

```powershell
Set-Location "C:\Users\user\Desktop\Ashwin-SOP\RDR-skills"
py -3.12 -m pip install -e ".[skill-compiler]"
py -3.12 -m rdr_engine.skill_tool render skills/alfworld --% --case {"task_type":"heat_place"} --show-path
py -3.12 -m rdr_engine.skill_tool render skills/alfworld --task "Heat an apple and put it in the bowl"
```

In PowerShell, `--%` passes the JSON after it to Python literally. Without it, PowerShell may strip the required double quotes and cause a JSON parsing error. The `--task` form needs no JSON quoting.

To regenerate the existing ALFWorld tree from its saved rule list, run `py -3.12 -m rdr_engine.skill_tool compile skills/alfworld --model-id gemini-3.8-flash --force`. For another skill, first set `GEMINI_API_KEY` (or replace the placeholder in `src/rdr_engine/skill_compiler.py`), then run:

```powershell
py -3.12 -m rdr_engine.skill_compiler skills/docvqa/initial.md --output skills/docvqa/compiled/ir.json
py -3.12 -m rdr_engine.skill_tool compile skills/docvqa
py -3.12 -m rdr_engine.skill_tool render skills/docvqa --% --case {}
```

`skill_compiler` prints JSON if `--output` is omitted and refuses to overwrite an existing file. The prompt is in `src/rdr_engine/prompts/skill_to_rules_system.md`; allowed task features are in `skills/feature_schemas.json`. `skill_tool compile` writes `fragments.json`, `tree.json`, and `manifest.json` alongside the reviewed `ir.json`; it refuses to replace existing artifacts without `--force`. `render --case` uses explicit observable feature values. `render --task` derives a conservative demo case from visible text; ambiguous cases should use `--case` instead. The engine still returns the last true node's local conclusion; the renderer composes all matched fragments into the Markdown shown to an agent. Benchmark execution and trace-driven updates are not connected yet.
