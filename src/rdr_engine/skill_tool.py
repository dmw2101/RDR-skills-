"""Offline commands for compiling and previewing a skill-backed RDR tree."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .skill_cases import SkillConditionEvaluator, build_case
from .skill_compiler import FeatureSchema, SkillBundle
from .skill_renderer import SkillRenderer


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compile or preview a skill-backed RDR tree")
    commands = parser.add_subparsers(dest="command", required=True)
    compile_parser = commands.add_parser("compile", help="Build fragments and a tree from reviewed IR")
    compile_parser.add_argument("skill_dir", type=Path)
    compile_parser.add_argument("--ir", type=Path, help="Accepted rule-list JSON (default: compiled/ir.json)")
    compile_parser.add_argument("--features", type=Path, help="Feature schema catalog JSON")
    compile_parser.add_argument("--model-id", default="unspecified")
    compile_parser.add_argument("--force", action="store_true", help="Replace existing compiled artifacts")
    compile_parser.add_argument("--discard-learned", action="store_true",
                                help="Rebuild even though compiled/learned.json holds trace-learned rules")

    render_parser = commands.add_parser("render", help="Predict and print selected Markdown")
    render_parser.add_argument("skill_dir", type=Path)
    context = render_parser.add_mutually_exclusive_group(required=True)
    context.add_argument("--case", help="Inline JSON feature object; prefer --case-file in PowerShell")
    context.add_argument("--case-file", type=Path, help="UTF-8 JSON file containing the feature object")
    context.add_argument("--task", help="Visible natural-language task; uses conservative heuristics")
    render_parser.add_argument("--visible", help="JSON object of inspected pre-execution feature values")
    render_parser.add_argument(
        "--show-path", action="store_true",
        help="Print the case, evaluation trace, terminal nodes, and fragment IDs to stderr",
    )
    args = parser.parse_args(argv)

    try:
        directory = args.skill_dir.resolve()
        if args.command == "compile":
            feature_path = args.features or directory.parent / "feature_schemas.json"
            schema = FeatureSchema.from_file(feature_path, directory.name)
            tree, catalog = SkillBundle.build(
                directory,
                ir_path=args.ir or directory / "compiled" / "ir.json",
                feature_schema=schema,
                model_id=args.model_id,
                force=args.force,
                discard_learned=args.discard_learned,
            )
            print(f"Compiled {len(catalog.fragments)} fragments and {len(tree.nodes)} tree nodes in {directory / 'compiled'}")
            return 0

        manifest = json.loads((directory / "compiled" / "manifest.json").read_text(encoding="utf-8"))
        schema = FeatureSchema.model_validate(manifest["feature_schema"])
        evaluator = SkillConditionEvaluator(schema)
        tree, catalog = SkillBundle.load(directory, evaluator)
        if args.case is not None or args.case_file is not None:
            case_text = (
                args.case_file.read_text(encoding="utf-8-sig")
                if args.case_file is not None else args.case
            )
            case = json.loads(case_text)
            required = {
                condition["field"]
                for node in tree.nodes for condition in node.conditions
            }
            evaluator.validate_case(case, required)
        else:
            visible = json.loads(args.visible) if args.visible else None
            if visible is not None and not isinstance(visible, dict):
                raise ValueError("--visible must be a JSON object")
            case = build_case(directory.name, args.task, visible, schema)
        prediction = tree.predict(case)
        rendered = SkillRenderer().render(prediction, tree, catalog)
        if args.show_path:
            print(f"case={json.dumps(case, sort_keys=True)}", file=sys.stderr)
            print("trace=", file=sys.stderr)
            for step in rendered.trace:
                outcome = "true" if step.matched else "false"
                print(f"  {'  ' * step.depth}{step.node_id}:{outcome}", file=sys.stderr)
            print(f"terminals={list(rendered.terminal_ids)}", file=sys.stderr)
            print(f"fragments={list(rendered.selected_ids)}", file=sys.stderr)
        print(rendered.markdown, end="")
        return 0
    except (OSError, UnicodeError, ValueError, KeyError) as error:
        parser.exit(1, f"error: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
