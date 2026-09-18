"""Offline tests for benchmark preparation; no dataset payload is fetched."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import materialize_remote
import materialize_small
import prepare_alfworld


class PreparationTests(unittest.TestCase):
    def test_source_manifest_matches_published_split_counts(self) -> None:
        root = Path(__file__).resolve().parent
        sources = json.loads((root / "sources.json").read_text(encoding="utf-8"))
        self.assertEqual(len(sources["benchmarks"]), 5)
        for benchmark in sources["benchmarks"]:
            self.assertTrue((root.parent / benchmark["skill"]).is_file())
            manifest_root = root.parent / benchmark["manifest"]
            for split, count in benchmark["counts"].items():
                items = json.loads((manifest_root / split / "items.json").read_text(encoding="utf-8"))
                self.assertEqual(len(items), count)

    def test_write_split_uses_published_id_order(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest_root = root / "skillopt" / "data" / "fixture"
            for split, ids in {"train": ["b", "a"], "val": ["c"], "test": ["d"]}.items():
                path = manifest_root / split / "items.json"
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps([{"id": item_id} for item_id in ids]), encoding="utf-8")
            lookup = {item_id: {"id": item_id, "question": item_id} for item_id in "abcd"}
            with patch.object(materialize_small, "SKILLOPT", root / "skillopt"), patch.object(
                materialize_small, "LOCAL", root / "local"
            ):
                materialize_small.write_split("sample", lookup, "fixture")
            train = json.loads((root / "local" / "sample" / "split" / "train" / "items.json").read_text())
            self.assertEqual([item["id"] for item in train], ["b", "a"])

    def test_docvqa_selects_only_manifest_images(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ids = {
                "train": [str(n) for n in range(107)],
                "val": [str(n) for n in range(107, 160)],
                "test": [str(n) for n in range(160, 534)],
            }
            rows = [
                {
                    "questionId": str(n), "docId": n, "question": f"Question {n}?",
                    "answers": [str(n)], "question_types": ["form"],
                    "image": {"src": "https://example.invalid/image.jpg"},
                }
                for n in range(535)
            ]

            def fake_download(_url: str, path: Path) -> None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fake image")

            with patch.object(materialize_remote, "LOCAL", root), patch.object(
                materialize_remote, "check_revision"
            ), patch.object(materialize_remote, "manifest_ids", return_value=ids), patch.object(
                materialize_remote, "docvqa_pages", return_value=iter(rows)
            ), patch.object(materialize_remote, "download_selected_image", side_effect=fake_download):
                materialize_remote.docvqa()

            for split, expected_ids in ids.items():
                path = root / "docvqa" / "splits" / split / "items.csv"
                with path.open(encoding="utf-8", newline="") as stream:
                    actual = list(csv.DictReader(stream))
                self.assertEqual([row["questionId"] for row in actual], expected_ids)
            self.assertEqual(len(list((root / "docvqa" / "images").glob("*.jpg"))), 534)

    def test_alfworld_rejects_unsafe_zip_paths(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as stream:
                stream.writestr("../outside.txt", "bad")
            with self.assertRaises(ValueError):
                prepare_alfworld.extract_safely(archive, root / "games")
            self.assertFalse((root / "outside.txt").exists())


if __name__ == "__main__":
    unittest.main()
