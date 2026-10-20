"""UTF-8 evaluator subprocess entry point; never loaded by the model/tool loop."""

import sys
from pathlib import Path

from .varex import VarexAdapter
from ..storage import read_json, write_json


if __name__ == "__main__":
    path = Path(sys.argv[1])
    request = read_json(path)
    adapter = VarexAdapter.__new__(VarexAdapter)
    adapter.root = Path(request["root"])
    adapter.provenance = request["provenance"]
    result = adapter._score(request["records"], path.parent)
    write_json(path.parent / "scoring-result.json", result)
