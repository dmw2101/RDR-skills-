"""Convert skills to base MCRDR trees: Gemini rule extraction, offline compile, tree view.

Run from the repository root (PowerShell 5.1 or any shell):

    $env:GEMINI_API_KEY = "YOUR_GEMINI_API_KEY"
    py -3.12 scripts/convert_skills.py

By default this converts the six SkillOpt GPT-5.5 optimized skills. For each one it

1. asks Gemini for a rule list (`rdr_engine.skill_compiler`), saved as a new
   `compiled/ir.<timestamp>.json` so earlier extractions are never overwritten;
2. compiles that rule list offline into fragments, tree and manifest
   (`rdr_engine.skill_tool compile`);
3. writes the interactive tree view `compiled/tree_visualisation.html`.

A skill that already has `compiled/manifest.json` is skipped unless --force is given.
`--compile-only` rebuilds from the newest saved `compiled/ir.<timestamp>.json`
without calling Gemini (for example after reviewing or repairing a rule list).
One failing skill does not stop the others; the exit code is nonzero if any failed.
The key is read by the extractor from GEMINI_API_KEY and is never printed.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DEFAULT_MODEL = "gemini-3.8-flash"  # the model recorded in the existing alfworld and pdf manifests
OPTIMIZED_SKILLS = ("alfworld_gpt55", "docvqa_gpt55", "livemath_gpt55",
                    "officeqa_gpt55", "searchqa_gpt55", "spreadsheetbench_gpt55")


def saved_irs(skill_dir: Path) -> list[Path]:
    """Timestamped rule lists from earlier extractions, oldest first."""
    return sorted(path for path in (skill_dir / "compiled").glob("ir.*.json")
                  if path.name.split(".")[1][:1].isdigit())


def plan(skill_dir: Path, features: Path, model: str, *, compile_only: bool, stamp: str) -> list[tuple[str, list[str]]]:
    """The steps for one skill, as (label, argv) pairs."""
    if compile_only:
        previous = saved_irs(skill_dir)
        ir = previous[-1] if previous else skill_dir / "compiled" / "ir.<newest saved>.json"
    else:
        ir = skill_dir / "compiled" / f"ir.{stamp}.json"
    steps = []
    if not compile_only:
        steps.append(("extract", [str(skill_dir / "initial.md"), "--features", str(features),
                                  "--model", model, "--output", str(ir)]))
    steps.append(("compile", ["compile", str(skill_dir), "--ir", str(ir), "--features", str(features),
                              "--model-id", model, "--force"]))
    steps.append(("visualise", [str(skill_dir / "compiled" / "tree.json")]))
    return steps


def load_visualiser():
    spec = importlib.util.spec_from_file_location("visualise_tree", ROOT / "skills" / "visualise_tree.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def call(function, argv: list[str]) -> str | None:
    """Run a CLI entry point in-process; return an error message or None."""
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr):
            code = function(argv)
    except SystemExit as exit:  # argparse and parser.exit report failures this way
        code = exit.code
    message = stderr.getvalue().strip()
    if message:
        print(message, file=sys.stderr)
    if code not in (0, None):
        return message.splitlines()[-1] if message else f"exit code {code}"
    return None


def counts(skill_dir: Path) -> dict[str, int | None]:
    compiled = skill_dir / "compiled"
    try:
        return {
            "rules": len(json.loads((compiled / "ir.json").read_text(encoding="utf-8"))["rules"]),
            "nodes": len(json.loads((compiled / "tree.json").read_text(encoding="utf-8"))["nodes"]),
            "fragments": len(json.loads((compiled / "fragments.json").read_text(encoding="utf-8"))["fragments"]),
        }
    except (OSError, ValueError, KeyError):
        return {"rules": None, "nodes": None, "fragments": None}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Convert skills to base MCRDR trees")
    parser.add_argument("--skills", nargs="+", default=list(OPTIMIZED_SKILLS),
                        help="Skill folder names under skills/ (default: the six GPT-5.5 optimized skills)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Gemini model for rule extraction")
    parser.add_argument("--force", action="store_true", help="Rebuild skills that are already compiled")
    parser.add_argument("--compile-only", action="store_true",
                        help="Rebuild from the newest saved compiled/ir.<timestamp>.json; no Gemini call")
    parser.add_argument("--dry-run", action="store_true", help="Print the steps without running them")
    parser.add_argument("--skills-dir", type=Path, default=ROOT / "skills", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    skills_dir = args.skills_dir.resolve()
    features = skills_dir / "feature_schemas.json"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S%f")[:-3]
    needs_gemini = not args.compile_only and not args.dry_run
    if needs_gemini and not os.environ.get("GEMINI_API_KEY"):
        parser.exit(1, 'error: set the key first, e.g. $env:GEMINI_API_KEY = "YOUR_GEMINI_API_KEY"\n')

    if not args.dry_run:
        from rdr_engine import skill_compiler, skill_tool
        entry = {"extract": skill_compiler.main, "compile": skill_tool.main, "visualise": load_visualiser().main}

    results = []
    for name in args.skills:
        skill_dir = skills_dir / name
        if not (skill_dir / "initial.md").is_file():
            results.append((name, "failed: no initial.md", counts(skill_dir)))
            continue
        if (skill_dir / "compiled" / "manifest.json").exists() and not args.force:
            results.append((name, "skipped (already compiled; --force rebuilds)", counts(skill_dir)))
            continue
        if args.compile_only and not saved_irs(skill_dir):
            results.append((name, "failed: no saved compiled/ir.<timestamp>.json to compile", counts(skill_dir)))
            continue
        print(f"\n== {name}", flush=True)
        status = "converted"
        for label, step in plan(skill_dir, features, args.model, compile_only=args.compile_only, stamp=stamp):
            module = {"extract": "rdr_engine.skill_compiler", "compile": "rdr_engine.skill_tool",
                      "visualise": "skills/visualise_tree.py"}[label]
            print(f"   {label}: {module} {' '.join(step)}", flush=True)
            if args.dry_run:
                continue
            error = call(entry[label], step)
            if error:
                status = f"failed at {label}: {error}"
                break
        results.append((name, "planned (dry run)" if args.dry_run else status, counts(skill_dir)))

    print("\n| Skill | Rules | Nodes | Fragments | Status |")
    print("|---|---|---|---|---|")
    for name, status, numbers in results:
        cells = ["-" if numbers[key] is None else str(numbers[key]) for key in ("rules", "nodes", "fragments")]
        print(f"| {name} | {' | '.join(cells)} | {status} |")
    return int(any(status.startswith("failed") for _, status, _ in results))


if __name__ == "__main__":
    raise SystemExit(main())
