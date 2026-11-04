# Trace2Skill

Code: [Qwen-Applications/Trace2Skill](https://github.com/Qwen-Applications/Trace2Skill) at
`3d0b52a`. The README states Apache-2.0, but the repository has no LICENSE file.

## Benchmarks

| Item id | Benchmark | Role in the paper | Status here |
|---|---|---|---|
| `trace2skill-repo` | SpreadsheetBench-Verified 400 | Evolve on items 0–199, test on 200–399 ("Vrf") | Fetched, in `local-data/trace2skill-repo/data/spreadsheetbench_verified/` |
| `trace2skill-repo` | SpreadsheetBench 912 | "Soft" and "Hard" tests, 2,529 examples, evolve items removed | Fetched, in `local-data/trace2skill-repo/data/all_data_912_v0.1/` |
| `wikitq` | WikiTableQuestions 1.0.2 | OOD test, 2,810 examples converted to spreadsheet tasks | Raw data fetched. **The conversion is not released** |
| `hitab` | HiTab | OOD test, 1,585 examples converted to spreadsheet tasks | Raw data and tables fetched. **The conversion is not released** |
| `dapo-math` | DAPO-Math-17k | Evolve on 400 questions, test on 100 disjoint ones | Fetched (1.79M rows: 17k problems, repeated). **The 400/100 IDs are not released** |
| `aime-2026` | AIME 2026 | OOD math test, avg@8 over 30 problems | Fetched |
| `docvqa-full` | DocVQA validation | Evolve on 50, test on the other 5,299 | On demand (large) |
| `vrdu-registration` | VRDU Registration Forms | PDF source traces | On demand |
| `varex` | VAREX Flat | PDF held-out test (76.9% → 85.3%) | On demand, through our pinned VAREX preparation |
| `tsbench` | TSBench (Talk-to-Your-Slides) | PPTX source and deck-disjoint test (72.5% → 88.8%) | On demand (612 MB repo) |
| `officebench` | OfficeBench DOCX | DOCX holdout, 64 tasks (79.7% → 87.5%) | On demand |

The repo's own runner reproduces the spreadsheet results:
`local-data/trace2skill-repo/run_spreadsheetbench.py` (see its README).

## Starting skills

| Mode | Starting skill | Where it is here |
|---|---|---|
| Deepening (spreadsheet) | Anthropic's official `xlsx` skill | `skills/licensed/trace2skill-combined/xlsx/` (the copy Trace2Skill pinned) and `skills/licensed/anthropic-skills/skills/xlsx/` |
| Deepening (PDF / PPTX / DOCX) | Anthropic's `pdf`, `pptx`, `docx` skills | `skills/licensed/anthropic-skills/skills/` |
| Creation (spreadsheet) | `xlsx-basic`, written by Qwen3.5-122B from its own knowledge | **Not released** |
| Creation (math, DocVQA) | No skill | – |
| `skill-creator` baseline | Anthropic's `skill-creator` | `skills/licensed/anthropic-skills/skills/skill-creator/` |

## Released learned skills

| Skill | Setting | Where |
|---|---|---|
| `xlsx-122B`, `xlsx-35B` | Created from scratch on 122B / 35B error traces | `skills/released/` (committed) |
| `trace2skill-xlsx-122B-combined`, `trace2skill-xlsx-35B-combined` | Deepened from Anthropic's `xlsx` on combined traces | `skills/licensed/trace2skill-combined/` (Anthropic licence, git-ignored) |

## Gaps for exact reproduction

- The WikiTQ and HiTab conversions, the DAPO 400/100 IDs and the `xlsx-basic` seed are not released.
- Trace2Skill's SpreadsheetBench split (first 200 / last 200) is not SkillOpt's 80/40/280 split. Numbers from the two papers are not directly comparable.
