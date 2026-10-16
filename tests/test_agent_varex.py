import hashlib
import importlib.util
import json
import os
import random
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from skill_agent.benchmarks.varex import VarexAdapter, download_scorer, prepare_varex
from skill_agent.storage import read_json, write_json


class VarexPreparationTests(unittest.TestCase):
    def test_prepare_is_pinned_splits_deterministically_and_separates_gold(self):
        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / "fixture.pdf"
            pdf.write_bytes(b"%PDF-FAKE")
            rows = [{"doc_id": str(i), "split": "Flat", "schema": '{"type":"object"}',
                     "ground_truth": '{"secret":"gold"}'} for i in range(33)]
            rows[-1]["doc_id"] = "DIC-23_(9-01)"  # Actual upstream filename rejected by the old check.
            class Dataset:
                def select_columns(self, columns):
                    self.columns = columns
                    return self
                def __iter__(self):
                    return iter(rows)
            ds = types.ModuleType("datasets")
            hf = types.ModuleType("huggingface_hub")
            ds.load_dataset = lambda *a, **kw: Dataset()
            hf.hf_hub_download = lambda *a, **kw: str(pdf)
            def fake_scorer(directory):
                write_json(directory / "field_exclusions.json", {"exclusions": {}})
                (directory / "score.py").write_text("# mock fixture, not an official scorer\n")
            target = Path(temporary) / "dataset"
            with patch.dict("sys.modules", {"datasets": ds, "huggingface_hub": hf}), patch(
                "skill_agent.benchmarks.varex.download_scorer", side_effect=fake_scorer):
                receipt = prepare_varex(target)
                with self.assertRaisesRegex(ValueError, "already exists"):
                    prepare_varex(target)
            self.assertEqual(receipt["heldout"], 3)
            ids = sorted(row["doc_id"] for row in rows)
            random.Random(42).shuffle(ids)
            self.assertEqual(read_json(target / "splits.json")["dev"], ids[:30])
            self.assertEqual(sorted(p.name for p in (target / "public/0").iterdir()), ["document.pdf", "schema.json"])
            self.assertNotIn("secret", (target / "tasks.json").read_text())
            adapter = VarexAdapter(data_dir=target)
            self.assertEqual(len(adapter.tasks("dev")), 30)
            self.assertEqual(len(adapter.tasks("heldout")), 3)
            (target / "private/0.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "snapshot changed"):
                VarexAdapter(data_dir=target)


@unittest.skipUnless(os.environ.get("SKILL_AGENT_NETWORK_TESTS") == "1", "opt-in network/scorer dependencies")
class OfficialScorerParityTests(unittest.TestCase):
    def test_wrapper_matches_official_scorer_including_failures_and_exclusions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            download_scorer(root / "scorer")
            # Keep downloaded source byte-for-byte; fixture exclusions are separately selected.
            original_checksum = hashlib.sha256((root / "scorer/score.py").read_bytes()).hexdigest()
            write_json(root / "private/a.json", {"x": "HELLO", "number": "123", "ignored": "value"})
            write_json(root / "private/b.json", {"x": "missing"})
            adapter = VarexAdapter.__new__(VarexAdapter)
            adapter.root = root
            adapter.provenance = {"scorer_revision": "fixture uses pinned source"}
            # The public official exclusion list is loaded as-is (our synthetic IDs have none).
            records = [{"task_id": "a", "condition": "reduced", "replicate": 0,
                        "status": "completed", "prediction": {"x": " hello ", "number": "123.0"}},
                       {"task_id": "b", "condition": "reduced", "replicate": 0,
                        "status": "failed", "prediction": None}]
            output = root / "scores"
            wrapped = adapter.score(records, output)
            spec = importlib.util.spec_from_file_location("official_direct", root / "scorer/score.py")
            scorer = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(scorer)
            direct = scorer.score_model_mode("agent", "reduced_0", output / "reduced_0",
                {key: read_json(root / "private" / f"{key}.json") for key in ("a", "b")},
                {"a": "Flat", "b": "Flat"}, scorer.load_field_exclusions(root / "scorer/field_exclusions.json"), output)
            self.assertEqual(wrapped["summaries"][0]["exact_match_normalized"], direct["agg"]["exact_match_normalized"])
            self.assertEqual(direct["agg"]["docs"], 2)
            self.assertEqual(direct["agg"]["exact_match_normalized"], .5)
            self.assertIn("_error", read_json(output / "reduced_0/b.pred.json"))
            self.assertEqual(hashlib.sha256((root / "scorer/score.py").read_bytes()).hexdigest(), original_checksum)
