"""Build and expand reviewed procedure guidance dependencies."""

import argparse
import json
import os
import shutil
import tempfile
from collections import deque
from pathlib import Path

from pydantic import Field

from .skill_compiler import (
    DEFAULT_MODEL, GeminiRuleModel, LineRange, SkillBundle, SkillInput,
    SkillIR, StrictModel, _response_schema, _sha256,
)


class Link(StrictModel):
    procedure: str
    prerequisite: str
    evidence: list[LineRange] = Field(min_length=1)
    reason: str = Field(min_length=1)


class DependencyReview(StrictModel):
    links: list[Link]
    deferred: list[Link]


def load_review(directory, review, engine):
    directory = Path(directory)
    source = SkillInput.from_markdown(directory / "initial.md")
    ir = SkillIR.model_validate_json((directory / "compiled/ir.json").read_bytes())
    rules = {rule.id: rule for rule in ir.rules}
    mapping = {}
    nodes = engine.nodes
    for index, rule in enumerate(ir.rules, start=2):
        if index >= len(nodes):
            raise ValueError(f"missing procedure node for IR rule {rule.id}")
        node = nodes[index]
        if node.parent != 0 or list(node.conditions) != [p.model_dump() for p in rule.when]:
            raise ValueError(f"procedure node does not match IR rule {rule.id}")
        mapping[rule.id] = index
    seen = set()
    for link in review.links + review.deferred:
        if link.procedure not in rules or link.prerequisite not in rules:
            raise ValueError(f"unknown dependency rule: {link.procedure} -> {link.prerequisite}")
        if any(span.end > len(source.lines) for span in link.evidence):
            raise ValueError("dependency evidence points outside the source")
    for link in review.links:
        pair = (link.procedure, link.prerequisite)
        if pair in seen or pair[0] == pair[1]:
            raise ValueError(f"duplicate or self dependency: {pair}")
        seen.add(pair)
        for rule_id in pair:
            predicates = rules[rule_id].when
            if len(predicates) != 1 or not (
                (predicates[0].field, predicates[0].op)
                in {("needed_actions", "contains"), ("operation", "contains"),
                    ("operation", "eq"), ("task_type", "eq"), ("task_type", "contains")}
            ):
                raise ValueError(f"{rule_id} needs a single procedure relevance predicate")
    return mapping


def expand(review, mapping, engine, results, condition_id):
    nodes = engine.nodes
    directly_selected = [
        rule_id for rule_id, node_id in mapping.items()
        if all(results[condition_id(predicate)] == "true"
               for predicate in nodes[node_id].conditions)
    ]
    reached = set(directly_selected)
    pending = deque(directly_selected)
    added = []
    while pending:
        procedure = pending.popleft()
        for link in review.links:
            if link.procedure == procedure and link.prerequisite not in reached:
                reached.add(link.prerequisite)
                pending.append(link.prerequisite)
                added.append(link.model_dump())
    return frozenset(mapping[link["prerequisite"]] for link in added), {
        "directly_selected": directly_selected,
        "added": added,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    propose = commands.add_parser("propose")
    propose.add_argument("skill", type=Path)
    propose.add_argument("output", type=Path)
    propose.add_argument("--model", default=DEFAULT_MODEL)
    build = commands.add_parser("build")
    build.add_argument("skill", type=Path)
    build.add_argument("review", type=Path)
    build.add_argument("destination", type=Path)
    args = parser.parse_args()
    if args.command == "propose":
        engine, _ = SkillBundle.load(args.skill)
        source = SkillInput.from_markdown(args.skill / "initial.md")
        ir = (args.skill / "compiled/ir.json").read_text(encoding="utf-8")
        model = GeminiRuleModel(os.environ.get("GEMINI_API_KEY", ""), args.model)
        raw = model.generate(
            system_instruction=(
                "Propose prerequisite guidance links between existing IR rule IDs. "
                "A link means reading the prerequisite procedure is useful whenever the "
                "dependent procedure is selected, not that a factual prerequisite is met. "
                "Use links only when the skill explicitly supports an unconditional dependency. "
                "Place conditional, alternative, and ambiguous relationships in deferred. "
                "Each relationship must cite numbered source ranges and give a short reason. "
                "Do not change the IR or invent rule IDs."
            ),
            user_input=f"Complete numbered skill:\n{source.numbered_markdown}\n\nExisting IR:\n{ir}",
            response_schema=_response_schema(DependencyReview),
        )
        review = DependencyReview.model_validate_json(raw)
        load_review(args.skill, review, engine)
        with args.output.open("x", encoding="utf-8") as output:
            output.write(review.model_dump_json(indent=2) + "\n")
        return
    review = DependencyReview.model_validate_json(args.review.read_bytes())
    build_bundle(args.skill, review, args.destination)


def build_bundle(skill: Path, review: DependencyReview, destination: Path):
    engine, _ = SkillBundle.load(skill)
    mapping = load_review(skill, review, engine)
    target = destination.resolve()
    if target.exists():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as staging:
        bundle = Path(staging) / target.name
        compiled = bundle / "compiled"
        compiled.mkdir(parents=True)
        shutil.copy2(skill / "initial.md", bundle)
        for name in ("manifest.json", "ir.json", "tree.json", "fragments.json"):
            shutil.copy2(skill / "compiled" / name, compiled)
        graph = compiled / "dependencies.json"
        graph.write_text(json.dumps({"rule_nodes": mapping, "review": review.model_dump()}, indent=2),
                         encoding="utf-8")
        manifest_path = compiled / "manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        manifest["dependencies_sha256"] = _sha256(graph.read_bytes())
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        SkillBundle.load(bundle)
        bundle.rename(target)


if __name__ == "__main__":
    main()
