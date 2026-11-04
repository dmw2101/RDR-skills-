# SkillOpt

Code: [microsoft/SkillOpt](https://github.com/microsoft/SkillOpt) at `79124b3` (MIT). It is
vendored, trimmed, in [`benchmarks/skillopt`](../../benchmarks/skillopt). The split manifests
live in `benchmarks/skillopt/data/` and are not duplicated here.

## Benchmarks

| Item id | Benchmark | Train / val / test | Status here |
|---|---|---:|---|
| `spreadsheetbench` | SpreadsheetBench-Verified 400 | 80 / 40 / 280 | Fetched; split built in `local-data/spreadsheetbench/split/` |
| `livemathematicianbench` | LiveMathematicianBench (Nov 2025 – Feb 2026) | 35 / 17 / 125 | Fetched; split built in `local-data/livemathematicianbench/split/` |
| `sealqa` | SealQA Seal-0 and Seal-Hard (appendix) | – | Fetched; needs a live web-search tool |
| `docvqa` | DocVQA validation subset | 107 / 53 / 374 | On demand (534 images) |
| `searchqa` | SearchQA | 400 / 200 / 1,400 | On demand (streamed) |
| `alfworld` | ALFWorld | 39 / 18 / 134 | On demand |
| `officeqa` | OfficeQA | 50 / 24 / 172 | On demand; gated on Hugging Face (needs `HF_TOKEN`) |

## Skills (all MIT, committed)

| Folder | What it is |
|---|---|
| `skills/initial/` | The starting skill for each benchmark: SkillOpt's `envs/*/skills/initial.md`. `searchqa.md` is an empty placeholder |
| `skills/learned-gpt5.5/` | The released skill SkillOpt learned for each benchmark with GPT-5.5 (`ckpt/*/gpt5.5_skill.md`) |

| Benchmark | Initial (words) | Learned (words) |
|---|---:|---:|
| SpreadsheetBench | 224 | 1,918 |
| OfficeQA | 145 | 916 |
| DocVQA | 81 | 306 |
| ALFWorld | 516 | 2,034 |
| LiveMath | 154 | 479 |
| SearchQA | 16 | 1,507 |

The paper's "human skill" baseline is described as an expert-written document per
benchmark (145–516 tokens). It does not say outright that this is `initial.md`, though
four of the files fall in that range. Its "one-shot LLM skill" baseline (written once
by GPT-5.5) is not released.
