# Paper folders

One folder per paper we compare against. Each holds that paper's benchmarks and
skills, pinned to exact upstream versions.

| Folder | Paper | Official code |
|---|---|---|
| [Trace2Skill/](Trace2Skill/README.md) | Trace2Skill: Distill Trajectory-Local Lessons into Transferable Agent Skills | [Qwen-Applications/Trace2Skill](https://github.com/Qwen-Applications/Trace2Skill) |
| [SkillOpt/](SkillOpt/README.md) | SkillOpt | [microsoft/SkillOpt](https://github.com/microsoft/SkillOpt), vendored in `benchmarks/skillopt` |
| [WikiSkill/](WikiSkill/README.md) | WikiSkill: Compiling Agent Experience into Persistent Knowledge for Skill Evolution | none released |
| [Combined/](Combined/README.md) | All compatible benchmarks from the three papers and outside work, with every result to match or beat | – |

## Layout of each folder

```text
<Paper>/
|-- README.md          benchmarks, splits, starting skills and gaps
|-- sources.json       every dataset and skill: source, pinned revision, licence, size, checksums
|-- skills/            skills we may redistribute (committed)
|   `-- licensed/      Anthropic-licensed skills (git-ignored, local use only)
`-- local-data/        downloaded datasets (git-ignored)
    `-- fetch-log.json what was fetched, with checksums
```

## Getting the data

Raw data is never committed. Each `sources.json` marks items `now` (small, fetched
by default) or `on-demand` (large or gated):

```bash
python papers/fetch.py Trace2Skill --list
python papers/fetch.py Trace2Skill
python papers/fetch.py SkillOpt --only docvqa alfworld
python papers/fetch.py WikiSkill --check-only
```

`--check-only` re-verifies checksums without any network calls. OfficeQA is gated
on Hugging Face: accept its terms on the dataset page, then set `HF_TOKEN`.

The SpreadsheetBench and LiveMath items rebuild SkillOpt's exact published splits
(`local-data/<benchmark>/split/{train,val,test}/items.json`) with
`benchmarks/materialize_small.py`. DocVQA, SearchQA and ALFWorld reuse the existing
`benchmarks/` preparation scripts, with their output sent to the paper folder.

## Licences

- Anthropic's `xlsx`, `pdf`, `docx`, `pptx` and `skill-creator` skills, and the two
  Trace2Skill skills built on `xlsx`, forbid redistribution and derivative works. They
  live only in the ignored `skills/licensed/` folders. Never commit them.
- Dataset licences are listed per item in `sources.json`. They stay with the original hosts.
