"""Fetch pinned small benchmark sources on demand (about 16 MB compressed).

The raw files and extracted data stay under git-ignored benchmarks/local-data/.
Use `--check-only` to verify an existing cache without making network calls.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tarfile
from pathlib import Path
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
LOCAL = ROOT / "local-data"
SOURCES = {item["id"]: item for item in json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))["benchmarks"]}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_and_verify(url: str, destination: Path, expected: str, check_only: bool) -> None:
    if not destination.exists():
        if check_only:
            raise FileNotFoundError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        part = destination.with_suffix(destination.suffix + ".part")
        request = Request(url, headers={"User-Agent": "rdr-skills-benchmark-prep/1.0"})
        with urlopen(request, timeout=180) as response, part.open("wb") as target:
            shutil.copyfileobj(response, target)
        part.replace(destination)
    actual = sha256(destination)
    if actual != expected:
        raise ValueError(f"Checksum mismatch for {destination}: expected {expected}, got {actual}")
    print(f"Verified {destination.relative_to(ROOT)}")


def extract_spreadsheetbench(archive: Path) -> None:
    root = (LOCAL / "spreadsheetbench").resolve()
    with tarfile.open(archive, "r:gz") as source:
        for member in source:
            relative = Path(member.name.replace("\\", "/"))
            target = (root / relative).resolve()
            if not target.is_relative_to(root):
                raise ValueError(f"Unsafe TAR path: {member.name}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                if target.exists() and target.stat().st_size == member.size:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as input_stream, target.open("wb") as output_stream:
                    shutil.copyfileobj(input_stream, output_stream)
            else:
                raise ValueError(f"Unsupported TAR entry type: {member.name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=("spreadsheetbench", "livemathematicianbench"))
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    if args.only in (None, "spreadsheetbench"):
        item = SOURCES["spreadsheetbench"]
        name = item["source_file"]
        archive = LOCAL / "spreadsheetbench" / name
        url = f"{item['source']}/resolve/{item['revision']}/{name}"
        fetch_and_verify(url, archive, item["sha256"], args.check_only)
        if not args.check_only:
            extract_spreadsheetbench(archive)
            print("Extracted SpreadsheetBench Verified 400")

    if args.only in (None, "livemathematicianbench"):
        item = SOURCES["livemathematicianbench"]
        for source_file, checksum in item["source_files"].items():
            destination = LOCAL / "livemathematicianbench" / Path(source_file).name
            url = f"{item['source']}/resolve/{item['revision']}/{source_file}"
            fetch_and_verify(url, destination, checksum, args.check_only)


if __name__ == "__main__":
    main()
