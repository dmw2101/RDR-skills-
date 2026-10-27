import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from skill_agent.benchmarks.varex import VarexAdapter, download_scorer
from skill_agent.storage import write_json
from trace_learner.benchmarks.varex import VarexHooks


@unittest.skipUnless(os.environ.get("SKILL_AGENT_NETWORK_TESTS") == "1", "opt-in network/scorer dependencies")
class VarexHooksTests(unittest.TestCase):
    def test_rescore_and_outcomes_use_the_official_perfect_document_verdict(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            download_scorer(root / "scorer")
            write_json(root / "private/a.json", {"x": "HELLO", "number": "123"})
            adapter = VarexAdapter.__new__(VarexAdapter)
            adapter.root = root
            adapter.provenance = {"scorer_revision": "fixture uses pinned source"}
            hooks = VarexHooks(adapter)
            task = {"id": "a"}
            self.assertEqual(hooks.ground_truth(task), {"x": "HELLO", "number": "123"})
            wrong = asyncio.run(hooks.rescore(task, {"x": "hello", "number": "124"}))
            self.assertFalse(wrong["passed"])
            self.assertTrue(wrong["fields"])  # Official per-field rows for the verifier.
            self.assertTrue(asyncio.run(hooks.rescore(task, {"x": " hello ", "number": "123.0"}))["passed"])
            records = [{"task_id": "a", "condition": "reduced", "replicate": r, "status": status,
                        "prediction": prediction}
                       for r, status, prediction in ((0, "completed", {"x": "HELLO", "number": "123"}),
                                                     (1, "failed", None))]
            self.assertEqual(hooks.outcomes(records, root / "scores"),
                             {"a|reduced|0": True, "a|reduced|1": False})


if __name__ == "__main__":
    unittest.main()
