"""Fetch only the ALFWorld text-game files needed by the SkillOpt split.

This is intentionally on demand. It does not fetch the ALFWorld visual
detector, pretrained agents, or Seq2Seq payload downloaded by the official
`alfworld-download` command.
"""

from __future__ import annotations

import argparse
import json
import shutil
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
_SOURCES = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))["benchmarks"]
ARCHIVES = next(item["source_files"] for item in _SOURCES if item["id"] == "alfworld")


def fetch(url: str, destination: Path) -> None:
    if destination.exists() and zipfile.is_zipfile(destination):
        print(f"Using cached {destination.name}")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_suffix(destination.suffix + ".part")
    request = Request(url, headers={"User-Agent": "rdr-skills-benchmark-prep/1.0"})
    with urlopen(request, timeout=180) as response, part.open("wb") as target:
        shutil.copyfileobj(response, target)
    if not zipfile.is_zipfile(part):
        raise ValueError(f"Downloaded file is not a ZIP archive: {url}")
    part.replace(destination)


def extract_safely(archive: Path, destination: Path) -> None:
    root = destination.resolve()
    with zipfile.ZipFile(archive) as source:
        for member in source.infolist():
            relative = Path(member.filename.replace("\\", "/"))
            target = (root / relative).resolve()
            if not target.is_relative_to(root):
                raise ValueError(f"Unsafe ZIP path in {archive.name}: {member.filename}")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if target.exists() and target.stat().st_size == member.file_size:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(member) as input_stream, target.open("wb") as output_stream:
                shutil.copyfileobj(input_stream, output_stream)


def copy_logic(data_root: Path) -> None:
    try:
        from alfworld.info import ALFRED_PDDL_PATH, ALFRED_TWL2_PATH
    except ImportError as error:
        raise SystemExit("Install the ALFWorld runtime first: python -m pip install alfworld") from error
    logic = data_root / "logic"
    logic.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ALFRED_PDDL_PATH, logic / "alfred.pddl")
    shutil.copyfile(ALFRED_TWL2_PATH, logic / "alfred.twl2")


def validate_paths(data_root: Path) -> None:
    for name in ("alfred.pddl", "alfred.twl2"):
        path = data_root / "logic" / name
        if not path.is_file():
            raise FileNotFoundError(f"Missing ALFWorld logic file: {path}")
    split_root = ROOT / "skillopt" / "data" / "alfworld_path_split"
    missing = []
    total = 0
    for split in ("train", "val", "test"):
        with (split_root / split / "items.json").open(encoding="utf-8") as stream:
            items = json.load(stream)
        for item in items:
            total += 1
            path = data_root / item["gamefile"]
            if not path.is_file():
                missing.append(str(path))
    if missing:
        raise FileNotFoundError(f"Missing {len(missing)} of {total} SkillOpt game files; first: {missing[0]}")
    print(f"All {total} SkillOpt ALFWorld game paths are present under {data_root.resolve()}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root", type=Path, default=ROOT / "local-data" / "alfworld",
        help="Location for ignored game data (can be on a separate disk).",
    )
    parser.add_argument("--check-only", action="store_true", help="Validate an existing data root without downloading.")
    args = parser.parse_args()
    data_root = args.data_root.resolve()
    if not args.check_only:
        # Fail before downloading if the runtime, which supplies logic files,
        # is not installed.
        try:
            import alfworld.info  # noqa: F401
        except ImportError as error:
            raise SystemExit("Install the ALFWorld runtime first: python -m pip install alfworld") from error
        archive_root = data_root / "archives"
        for url in ARCHIVES:
            archive = archive_root / url.rsplit("/", 1)[-1]
            fetch(url, archive)
            extract_safely(archive, data_root)
        copy_logic(data_root)
    validate_paths(data_root)
    print(f"Set ALFWORLD_DATA={data_root} when running SkillOpt.")


if __name__ == "__main__":
    main()
