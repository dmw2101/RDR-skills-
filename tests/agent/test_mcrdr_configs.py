import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples" / "agent"


class MCRDRConfigTests(unittest.TestCase):
    """The MCRDR configurations must repeat the original experiments exactly."""

    def test_every_original_config_has_an_mcrdr_twin(self):
        originals = sorted(p for p in EXAMPLES.glob("varex.*.json") if not p.name.startswith("varex.mcrdr."))
        self.assertTrue(originals)
        for original in originals:
            twin = EXAMPLES / original.name.replace("varex.", "varex.mcrdr.", 1)
            with self.subTest(config=original.name):
                self.assertTrue(twin.is_file(), f"missing {twin.name}")

    def test_mcrdr_configs_differ_only_in_receipt_and_output_dir(self):
        twins = sorted(EXAMPLES.glob("varex.mcrdr.*.json"))
        self.assertEqual(len(twins), 4)
        output_dirs = set()
        for twin in twins:
            original = EXAMPLES / twin.name.replace("varex.mcrdr.", "varex.", 1)
            with self.subTest(config=twin.name):
                new = json.loads(twin.read_text(encoding="utf-8"))
                old = json.loads(original.read_text(encoding="utf-8"))
                self.assertEqual(new["skill"]["receipt"], "experiments/pdf-snapshot-mcrdr.json")
                old_dir = Path(old["run"]["output_dir"])
                self.assertEqual(new["run"]["output_dir"], str(old_dir.parent / ("mcrdr-" + old_dir.name)).replace("\\", "/"))
                output_dirs.add(new["run"]["output_dir"])
                for config in (new, old):
                    del config["skill"]["receipt"]
                    del config["run"]["output_dir"]
                self.assertEqual(new, old)
        self.assertEqual(len(output_dirs), len(twins))


if __name__ == "__main__":
    unittest.main()
