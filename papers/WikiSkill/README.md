# WikiSkill

Paper: [arXiv 2608.27454](https://arxiv.org/abs/2608.27454) (Google Research). **The authors
have released no code, splits or skills.** Third-party reimplementations exist on GitHub
(for example `ashutoshsinghpr7/wikiskill`), but they are unofficial and not used here.

## Benchmarks

| Item id | Benchmark | Train / val / test in the paper | Status here |
|---|---|---:|---|
| `spreadsheetbench` | SpreadsheetBench-Verified 400 | 80 / 40 / 280 | Fetched; built with SkillOpt's manifest, which has the same counts |
| `livemathematicianbench` | LiveMathematicianBench | 35 / 18 / 124 | Fetched. WikiSkill's split is unreleased, so SkillOpt's 35/17/125 is built as the nearest match |
| `sealqa` | SealQA Seal-0 (111 questions) | 16 / 10 / 85 | Fetched. Split unreleased; needs a live web-search tool |
| `officeqa` | OfficeQA | 50 / 24 / 172 | On demand; same counts as SkillOpt's `officeqa_id_split`; gated (needs `HF_TOKEN`) |
| `alfworld` | ALFWorld | 39 / 18 / 134 | On demand; same counts as SkillOpt's path split |

Matching counts suggest WikiSkill reused SkillOpt's splits for SpreadsheetBench, OfficeQA
and ALFWorld. The paper does not say so, so treat that as likely, not confirmed.

## Skills

WikiSkill starts every method from an **empty skill set and an empty wiki**, including its
reruns of Trace2Skill, EvoSkill and SkillOpt. There is no starting skill to include. The
only fixed text is a short per-benchmark system prompt, given in the paper's appendix.
See [skills/README.md](skills/README.md).
