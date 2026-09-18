"""Materialize the pinned SkillOpt SpreadsheetBench and LiveMath ID splits.

Run from the repository root after downloading the upstream raw files. Outputs
go under ignored benchmarks/local-data/, not into the vendored SkillOpt tree.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SKILLOPT = ROOT / "skillopt"
LOCAL = ROOT / "local-data"
SPLITS = ("train", "val", "test")


def read_json(path: Path):
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def write_split(name: str, lookup: dict[str, dict], manifest_name: str) -> None:
    manifest_root = SKILLOPT / "data" / manifest_name
    output_root = LOCAL / name / "split"
    for split in SPLITS:
        ids = [str(row["id"]) for row in read_json(manifest_root / split / "items.json")]
        missing = sorted(set(ids) - lookup.keys())
        if missing:
            raise ValueError(f"{name}/{split}: missing source IDs: {missing[:5]}")
        output = output_root / split / "items.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8") as stream:
            json.dump([lookup[item_id] for item_id in ids], stream, ensure_ascii=False, indent=2)
        print(f"{name}/{split}: {len(ids)} records")


def spreadsheetbench() -> None:
    source_root = LOCAL / "spreadsheetbench" / "spreadsheetbench_verified_400"
    rows = read_json(source_root / "dataset.json")
    lookup = {str(row["id"]): row for row in rows}
    if len(lookup) != 400:
        raise ValueError(f"Expected 400 unique SpreadsheetBench IDs; got {len(lookup)}")
    for row in rows:
        if not (source_root / row["spreadsheet_path"]).is_dir():
            raise FileNotFoundError(source_root / row["spreadsheet_path"])
    write_split("spreadsheetbench", lookup, "spreadsheetbench_id_split")


def livemathematicianbench() -> None:
    # Reuse the benchmark's own normalization logic, so the resulting rows have
    # exactly the shape consumed by its loader. Read JSON explicitly as UTF-8
    # because the upstream loader relies on the platform default encoding.
    sys.path.insert(0, str(SKILLOPT))
    from skillopt.envs.livemathematicianbench.dataloader import _normalize_item

    source_root = LOCAL / "livemathematicianbench"
    rows = []
    for path in sorted(source_root.glob("qa_*_final.json")):
        for index, row in enumerate(read_json(path)):
            item = _normalize_item(row, row_idx=index, source_path=str(path))
            if item["question"] and item["choices"] and item["correct_choice"]["label"]:
                rows.append(item)
    lookup = {str(row["id"]): row for row in rows}
    if len(lookup) != 177:
        raise ValueError(f"Expected 177 unique LiveMath IDs; got {len(lookup)}")
    write_split("livemathematicianbench", lookup, "livemathematicianbench_id_split")


if __name__ == "__main__":
    spreadsheetbench()
    livemathematicianbench()
