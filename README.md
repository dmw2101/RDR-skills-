# RDR skills and benchmarks

This repository is the planned home for base skills and their paired evaluation benchmarks. The `skills/` directory currently contains five unmodified initial skills from [Microsoft SkillOpt](https://github.com/microsoft/SkillOpt): SpreadsheetBench, ALFWorld, DocVQA, SearchQA, and LiveMathematicianBench. These are starting points for later RDR conversion, not optimized checkpoints. SearchQA is an intentionally near-empty seed suitable for an empty-start control.

`skills/sources.json` records the pinned upstream commit, original paths, and SHA-256 checksums. The copied files retain their original `initial.md` names so the experimental baseline stays unchanged. Their license is in `LICENSES/SkillOpt-MIT.txt`.

Benchmark code, task data, and evaluators have not been added yet. A `benchmarks/` directory will be added in a later phase; no results in this repository should be interpreted as benchmark runs.

Anthropic's [official xlsx skill](https://github.com/anthropics/skills/tree/main/skills/xlsx) is an external comparison source only. It is not copied here because its [license](https://github.com/anthropics/skills/blob/main/skills/xlsx/LICENSE.txt) restricts copying and distribution. The spreadsheet file in this repository is SkillOpt's MIT-licensed seed skill, not Anthropic's skill.
