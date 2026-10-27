"""VAREX verification: official scorer verdicts, per-field comparison, and the original PDF."""

import asyncio
import csv
import tempfile
from pathlib import Path

from skill_agent.storage import read_json

from ..records import episode_key


class VarexHooks:
    """A document passes when every scored field matches (the official perfect_document)."""

    benchmark = "VAREX PDF field extraction"

    def __init__(self, adapter):
        self.adapter = adapter

    def outcomes(self, records, output_dir):
        scores = self.adapter.score(records, output_dir)
        perfect = {(row["task_id"], row["condition"], row["replicate"]): row["numerator"] == row["denominator"]
                   for row in scores["document_metrics"] if row["metric"] == "perfect_document"}
        return {episode_key(r): perfect.get((r["task_id"], r["condition"], r["replicate"]), False)
                for r in records}

    def ground_truth(self, task):
        return read_json(self.adapter.root / "private" / f"{task['id']}.json")

    async def rescore(self, task, prediction):
        if not isinstance(prediction, dict) or "_error" in prediction:
            raise ValueError("prediction must be a JSON object of extracted fields")
        record = {"task_id": task["id"], "condition": "reduced", "replicate": 0,
                  "status": "completed", "prediction": prediction}
        with tempfile.TemporaryDirectory(prefix="varex-rescore-") as temporary:
            scores = await asyncio.to_thread(self.adapter.score, [record], temporary)
            fields = []
            table = Path(temporary) / "agent_reduced_0.csv"
            if table.exists():
                with table.open(newline="", encoding="utf-8") as stream:
                    fields = [row for row in csv.DictReader(stream) if row.get("doc_id") == task["id"]]
        perfect = [row for row in scores["document_metrics"] if row["metric"] == "perfect_document"]
        passed = bool(perfect) and perfect[0]["numerator"] == perfect[0]["denominator"]
        return {"passed": passed, "fields": fields}

    async def open_inspection(self, task):
        return await self.adapter.open_session(task)
