"""On-demand preparation of SearchQA and DocVQA without raw dataset archives.

The default `check` command only reads remote metadata; it does not download
benchmark examples. `searchqa` streams the source rows, and `docvqa` uses the
dataset viewer to select only the published IDs and their images. Output is
always written under the git-ignored benchmarks/local-data/ directory.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
SKILLOPT = ROOT / "skillopt"
LOCAL = ROOT / "local-data"
SPLITS = ("train", "val", "test")
_MANIFEST = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
SOURCES = {
    item["id"]: (item["source"].removeprefix("https://huggingface.co/datasets/"), item["revision"])
    for item in _MANIFEST["benchmarks"]
    if item["id"] in ("searchqa", "docvqa")
}
DOCVQA_FIELDS = (
    "questionId", "docId", "question", "answer", "image_path", "topic",
    "ucsf_document_id", "ucsf_document_page_no", "source_split",
)


def read_json(path: Path):
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def get_json(url: str) -> dict:
    request = Request(url, headers={"User-Agent": "rdr-skills-benchmark-prep/1.0"})
    for attempt in range(8):
        try:
            with urlopen(request, timeout=90) as response:
                return json.load(response)
        except HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == 7:
                raise
            if error.code == 429:
                retry_after = error.headers.get("Retry-After")
                try:
                    delay = max(5.0, min(float(retry_after), 300.0))
                except (TypeError, ValueError):
                    delay = min(30.0 * (attempt + 1), 300.0)
            else:
                delay = min(2.0 ** attempt, 20.0)
        except URLError:
            if attempt == 7:
                raise
            delay = min(2.0 ** attempt, 20.0)
        time.sleep(delay)
    raise RuntimeError("Unreachable retry state")


def check_revision(name: str) -> None:
    repository, expected = SOURCES[name]
    pinned_url = f"https://huggingface.co/api/datasets/{repository}/revision/{expected}"
    actual = get_json(pinned_url).get("sha")
    if actual != expected:
        raise RuntimeError(
            f"{repository}: expected pinned revision {expected}, got {actual}."
        )
    if name == "docvqa":
        # The viewer API serves HEAD rather than an explicit revision. Refuse
        # to silently use a different version if the repository moves again.
        head = get_json(f"https://huggingface.co/api/datasets/{repository}").get("sha")
        if head != expected:
            raise RuntimeError(
                f"DocVQA viewer serves HEAD {head}, not the pinned {expected}. "
                "Use a revision-aware data loader or update the recipe after review."
            )
    print(f"{name}: source revision verified ({expected})")


def manifest_ids(name: str) -> dict[str, list[str]]:
    manifest_dir = SKILLOPT / "data" / f"{name}_id_split"
    return {
        split: [str(item["id"]) for item in read_json(manifest_dir / split / "items.json")]
        for split in SPLITS
    }


def check() -> None:
    for name in SOURCES:
        check_revision(name)
        ids = manifest_ids(name)
        flat = [item_id for split in SPLITS for item_id in ids[split]]
        if len(flat) != len(set(flat)):
            raise ValueError(f"{name}: duplicate IDs across splits")
        print(f"  published split counts: { {split: len(ids[split]) for split in SPLITS} }")
    print("No benchmark payload was downloaded.")


def searchqa() -> None:
    try:
        from datasets import load_dataset
    except ImportError as error:
        raise SystemExit("Install on the experiment machine: python -m pip install datasets") from error

    sys.path.insert(0, str(SKILLOPT / "scripts"))
    from materialize_searchqa import materialize_searchqa_splits

    check_revision("searchqa")
    repository, revision = SOURCES["searchqa"]
    dataset = load_dataset(
        repository,
        revision=revision,
        streaming=True,
        cache_dir=str(LOCAL / "hf-cache"),
    )
    counts = materialize_searchqa_splits(
        SKILLOPT / "data" / "searchqa_id_split",
        LOCAL / "searchqa" / "split",
        dataset,
        dataset_name=f"{repository}@{revision} (streaming)",
    )
    print(f"SearchQA materialized only the selected examples: {counts}")


def docvqa_pages():
    repository, _ = SOURCES["docvqa"]
    base = "https://datasets-server.huggingface.co/rows"
    offset = 0
    while True:
        url = base + "?" + urlencode(
            {"dataset": repository, "config": "DocVQA", "split": "validation", "offset": offset, "length": 100}
        )
        page = get_json(url)
        rows = page["rows"]
        if not rows:
            break
        yield from (entry["row"] for entry in rows)
        offset += len(rows)
        if offset >= page["num_rows_total"]:
            break
        # The viewer API rate-limits bursts. Fifty-four paced metadata pages
        # are still far smaller than downloading six source Parquet shards.
        time.sleep(2.5)


def check_docvqa_ids() -> None:
    """Scan remote metadata only; do not fetch or store any images."""
    check_revision("docvqa")
    ids = manifest_ids("docvqa")
    wanted = {item_id for split in SPLITS for item_id in ids[split]}
    found = set()
    total = 0
    for row in docvqa_pages():
        total += 1
        question_id = str(row.get("questionId", ""))
        if question_id in wanted:
            found.add(question_id)
        if total % 1000 == 0:
            print(f"Scanned {total} DocVQA metadata rows", flush=True)
    missing = sorted(wanted - found)
    if missing:
        raise RuntimeError(f"DocVQA metadata is missing {len(missing)} selected IDs: {missing[:5]}")
    print(f"Found all {len(found)} selected IDs among {total} metadata rows; no images downloaded.")


def download_selected_image(url: str, path: Path) -> None:
    if path.exists() and path.stat().st_size > 0:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(path.suffix + ".part")
    request = Request(url, headers={"User-Agent": "rdr-skills-benchmark-prep/1.0"})
    for attempt in range(8):
        try:
            with urlopen(request, timeout=120) as response, part.open("wb") as target:
                while chunk := response.read(1024 * 1024):
                    target.write(chunk)
            part.replace(path)
            return
        except HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == 7:
                raise
            retry_after = error.headers.get("Retry-After")
            try:
                delay = max(5.0, min(float(retry_after), 300.0))
            except (TypeError, ValueError):
                delay = min(30.0 * (attempt + 1), 300.0)
        except URLError:
            if attempt == 7:
                raise
            delay = min(2.0 ** attempt, 20.0)
        time.sleep(delay)


def docvqa() -> None:
    check_revision("docvqa")
    ids = manifest_ids("docvqa")
    wanted = {item_id for split in SPLITS for item_id in ids[split]}
    if len(wanted) != 534:
        raise ValueError(f"Expected 534 selected DocVQA question IDs, got {len(wanted)}")

    output_root = LOCAL / "docvqa"
    selected: dict[str, dict[str, str]] = {}
    for row in docvqa_pages():
        question_id = str(row.get("questionId", ""))
        if question_id not in wanted:
            continue
        if question_id in selected:
            raise ValueError(f"Duplicate DocVQA questionId: {question_id}")
        image_url = row.get("image", {}).get("src")
        if not image_url:
            raise ValueError(f"DocVQA question {question_id} has no image URL")
        image_path = (output_root / "images" / f"q{question_id}_d{row['docId']}.jpg").resolve()
        download_selected_image(image_url, image_path)
        answers = row.get("answers") or []
        selected[question_id] = {
            "questionId": question_id,
            "docId": str(row["docId"]),
            "question": str(row["question"]),
            "answer": repr(answers),
            "image_path": str(image_path),
            "topic": "|".join(row.get("question_types") or []),
            "ucsf_document_id": str(row.get("ucsf_document_id") or ""),
            "ucsf_document_page_no": str(row.get("ucsf_document_page_no") or ""),
            "source_split": "validation",
        }
        if len(selected) % 25 == 0 or len(selected) == len(wanted):
            print(f"DocVQA selected images: {len(selected)}/{len(wanted)}", flush=True)

    missing = sorted(wanted - selected.keys())
    if missing:
        raise RuntimeError(f"DocVQA source is missing {len(missing)} published IDs: {missing[:5]}")
    for split in SPLITS:
        output = output_root / "splits" / split / "items.csv"
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=DOCVQA_FIELDS)
            writer.writeheader()
            writer.writerows(selected[item_id] for item_id in ids[split])
        print(f"DocVQA/{split}: {len(ids[split])} selected examples")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "check-docvqa-ids", "searchqa", "docvqa"))
    command = parser.parse_args().command
    {"check": check, "check-docvqa-ids": check_docvqa_ids, "searchqa": searchqa, "docvqa": docvqa}[command]()
