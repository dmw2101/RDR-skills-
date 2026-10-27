# Benchmarks and data policy

This folder pairs the five skills in `../skills/` with the published SkillOpt
benchmark implementations and splits. `skillopt/` is a pinned snapshot of
[Microsoft SkillOpt](https://github.com/microsoft/SkillOpt) at commit
`79124b37e9a6371e13b753f8bcd7adb1e493ade1`. It contains the evaluation
code, configurations, and **lightweight ID/path split manifests**, not the raw
benchmark payloads. Exact source locations and revisions are in `sources.json`.
The snapshot is trimmed to the research package (`skillopt/`), `scripts/`,
`configs/`, `data/`, `ckpt/` and packaging files. Its website, docs, plugins,
web UI, Sleep tool and own tests are removed (listed in `sources.json`).

| Skill | Benchmark | Train / val / test | Published manifest |
|---|---|---:|---|
| `spreadsheetbench` | SpreadsheetBench Verified 400 | 80 / 40 / 280 | `skillopt/data/spreadsheetbench_id_split/` |
| `alfworld` | ALFWorld | 39 / 18 / 134 | `skillopt/data/alfworld_path_split/` |
| `docvqa` | DocVQA validation subset | 107 / 53 / 374 | `skillopt/data/docvqa_id_split/` |
| `searchqa` | SearchQA | 400 / 200 / 1400 | `skillopt/data/searchqa_id_split/` |
| `livemathematicianbench` | LiveMathematicianBench (four months) | 35 / 17 / 125 | `skillopt/data/livemathematicianbench_id_split/` |

## What goes to GitHub

Commit the skills, this documentation and preparation code, `sources.json`,
and the pinned SkillOpt code/manifests. **Never commit**
`benchmarks/local-data/`: it is ignored by the root `.gitignore` and holds raw
downloads, selected images/game files, materialized examples, and caches.
The manifests alone are not runnable examples. A fresh clone prepares data
only on an experiment machine, immediately before evaluation. Dataset terms
remain with the original hosts; this repository does not redistribute data.

The preparation commands below are optional until you run experiments. No
large benchmark payload needs to be downloaded on this computer or pushed to
GitHub. If a runner has a separate data disk, use it as the data root where
supported, or move the ignored `local-data/` directory there.

## Preparation on an experiment machine

Run these from the repository root. Python 3.10+ is required. Install the
vendored harness only when you need to run it:

```bash
python -m pip install -e "benchmarks/skillopt[searchqa,alfworld]"
```

Small sources (SpreadsheetBench Verified 400 and four LiveMath JSON files,
about 16 MB compressed in total):

```bash
python benchmarks/fetch_small.py
python benchmarks/materialize_small.py
```

This verifies SHA-256 checksums and reconstructs the exact published splits
under `benchmarks/local-data/{spreadsheetbench,livemathematicianbench}/split/`.
Use `python benchmarks/fetch_small.py --check-only` to verify an existing cache
without network access.

Large sources are **not fetched by default**:

```bash
python benchmarks/materialize_remote.py check
# Optional deeper check: scans metadata only; no images or raw Parquet files
python benchmarks/materialize_remote.py check-docvqa-ids
python benchmarks/materialize_remote.py searchqa
python benchmarks/materialize_remote.py docvqa
python benchmarks/prepare_alfworld.py
```

- `check` reads only source metadata and verifies the pinned revisions and
  manifest counts; it downloads no task payload.
- `check-docvqa-ids` scans the remote DocVQA viewer metadata to confirm that
  every published question ID is available, without fetching any images.
- `searchqa` streams source rows and keeps only the 2,000 selected examples
  under `local-data/searchqa/split/`. It does not retain the full Parquet files,
  although the full source may still be read **over the network**.
- `docvqa` scans lightweight viewer metadata and fetches only the 534 selected
  images, writing CSV splits under `local-data/docvqa/splits/`. It does not
  download the roughly 1 GB validation Parquet collection. These selected
  images still require local storage on the machine running evaluation.
- `prepare_alfworld.py` downloads only the three text-game archives and checks
  all 191 published game paths. It omits the visual detector and pretrained
  agents downloaded by the official `alfworld-download` command. Run this on
  the experiment machine, not on a laptop where the game data is unwanted.
  Use `--data-root /path/to/data` for a separate disk and set `ALFWORLD_DATA`
  to that path when evaluating. `--check-only` verifies an existing data root.

The on-demand large-payload commands have not been run in this repository;
their outputs are not currently present. The small sources and split
materialization were verified locally. A metadata-only scan confirmed all
534 selected DocVQA IDs in the 5,349-row source validation split, but did not
download images. Remote APIs can change, so pin checks fail rather than
silently switching to a different dataset revision.

## Connecting prepared data to SkillOpt

Run the vendored harness from `benchmarks/skillopt/` and override its default
paths to point to the ignored prepared data. For example:

```bash
cd benchmarks/skillopt
python scripts/eval_only.py --config configs/searchqa/default.yaml \
  --skill ../../skills/searchqa/initial.md \
  --split_dir ../local-data/searchqa/split
```

Likewise use `--split_dir ../local-data/livemathematicianbench/split`,
`--split_dir ../local-data/docvqa/splits`, or
`--split_dir ../local-data/spreadsheetbench/split` for the other materialized
benchmarks. SpreadsheetBench additionally needs
`--data_root ../local-data/spreadsheetbench/spreadsheetbench_verified_400`.
ALFWorld uses its already committed `data/alfworld_path_split` directly, but
requires `ALFWORLD_DATA` to point at the game-data root.

These commands prepare comparable task sets; they do not run or claim any
benchmark result. Model credentials and runtime dependencies are separate.
