"""Render an RDR tree.json as a self-contained, interactive HTML page.

Usage:
    python scripts/visualise_tree.py skills/alfworld/compiled/tree.json
    python scripts/visualise_tree.py TREE [-o OUT.html] [--fragments FRAGMENTS.json]
                             [--schema SCHEMA.json] [--hide-unreachable]

A thin wrapper around ``rdr_engine.visualise`` (see its docstring), so the trace
learner can render learned trees too. Works from a checkout without installing.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rdr_engine.visualise import *  # noqa: E402,F401,F403  (runpy users read helpers from this namespace)
from rdr_engine.visualise import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
