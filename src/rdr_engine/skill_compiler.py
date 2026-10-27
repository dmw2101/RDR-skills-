"""Extract source-referenced rules and compile a Markdown skill into an RDR tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .engine import RDREngine
from .types import ConditionEvaluator


GEMINI_API_KEY = "REPLACE_WITH_YOUR_GEMINI_API_KEY"
DEFAULT_MODEL = "gemini-3.8-flash"
PROMPT_VERSION = "2"
ARTIFACT_VERSION = 2


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class LineRange(StrictModel):
    start: int = Field(ge=1, description="First source line, inclusive and one-based.")
    end: int = Field(ge=1, description="Last source line, inclusive and one-based.")

    @model_validator(mode="after")
    def check_order(self) -> LineRange:
        if self.end < self.start:
            raise ValueError("end must be at least start")
        return self


class Predicate(StrictModel):
    field: str = Field(min_length=1, description="Name from the supplied feature schema.")
    op: Literal["eq", "in", "contains"]
    value: str | list[str] = Field(
        description="One allowed value for eq/contains, or a nonempty list for in."
    )

    @model_validator(mode="after")
    def check_value_shape(self) -> Predicate:
        if self.op in ("eq", "contains") and (not isinstance(self.value, str) or not self.value):
            raise ValueError(f"{self.op} requires one nonempty string value")
        if self.op == "in":
            if not isinstance(self.value, list) or not self.value:
                raise ValueError("in requires a nonempty list of values")
            if any(not item for item in self.value) or len(set(self.value)) != len(self.value):
                raise ValueError("in values must be nonempty and unique")
        return self


class RuleSpec(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$", description="Unique snake_case rule ID.")
    when: list[Predicate] = Field(min_length=1, description="Conjunction of predicates.")
    source: list[LineRange] = Field(min_length=1, description="Exact source lines to show.")


class ExcludedRange(LineRange):
    reason: str = Field(min_length=1, description="Why these lines are not skill guidance.")


class SkillIR(StrictModel):
    """The complete JSON response requested from the model."""

    root: list[LineRange] = Field(description="General instructions for every task.")
    rules: list[RuleSpec] = Field(description="Conditional instructions in source order.")
    excluded: list[ExcludedRange] = Field(description="Non-instructional source lines.")


class FeatureSpec(StrictModel):
    description: str = Field(min_length=1)
    values: list[str] = Field(min_length=1)
    multiple: bool = Field(default=False, description="Whether the case supplies a list of selected values.")

    @model_validator(mode="after")
    def check_values(self) -> FeatureSpec:
        if any(not value for value in self.values) or len(set(self.values)) != len(self.values):
            raise ValueError("feature values must be nonempty and unique")
        return self


class FeatureSchema(StrictModel):
    features: dict[str, FeatureSpec]

    @model_validator(mode="after")
    def check_names(self) -> FeatureSchema:
        for name in self.features:
            if re.fullmatch(r"[a-z][a-z0-9_]*", name) is None:
                raise ValueError(f"invalid feature name: {name!r}")
        return self

    @classmethod
    def from_file(cls, path: str | Path, skill_id: str) -> FeatureSchema:
        catalog = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(catalog, dict) or skill_id not in catalog:
            raise ValueError(f"no feature schema for skill {skill_id!r} in {path}")
        return cls.model_validate({"features": catalog[skill_id]})


@dataclass(frozen=True)
class SkillInput:
    path: Path
    markdown: str
    lines: tuple[str, ...]
    sha256: str

    @classmethod
    def from_markdown(cls, path: str | Path) -> SkillInput:
        source = Path(path).resolve()
        raw = source.read_bytes()
        markdown = raw.decode("utf-8-sig")
        return cls(
            path=source,
            markdown=markdown,
            lines=tuple(markdown.splitlines(keepends=True)),
            sha256=hashlib.sha256(raw).hexdigest(),
        )

    @property
    def numbered_markdown(self) -> str:
        return "\n".join(
            f"{number:>4} | " + line.rstrip("\r\n")
            for number, line in enumerate(self.lines, start=1)
        )


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class RuleExtractionError(ValueError):
    def __init__(self, issues: Sequence[ValidationIssue], attempts: int) -> None:
        self.issues = tuple(issues)
        self.attempts = attempts
        details = "\n".join(f"- {issue}" for issue in self.issues)
        super().__init__(f"no valid rule list after {attempts} model call(s):\n{details}")


class SkillIRValidator:
    """Check source references and predicates that JSON Schema alone cannot check."""

    def validate(
        self, ir: SkillIR, skill_input: SkillInput, feature_schema: FeatureSchema
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        line_count = len(skill_input.lines)
        owners: list[tuple[str, str, LineRange]] = []

        for span in ir.root:
            owners.append(("root", "root", span))
        for rule in ir.rules:
            for span in rule.source:
                owners.append(("rule", rule.id, span))
        for index, span in enumerate(ir.excluded, start=1):
            owners.append(("excluded", f"excluded:{index}", span))

        valid_owners: list[tuple[str, str, LineRange]] = []
        for kind, owner, span in owners:
            if span.end > line_count:
                issues.append(ValidationIssue(
                    "line_out_of_range",
                    f"{owner} references lines {span.start}-{span.end}; the skill has {line_count} lines",
                ))
            else:
                valid_owners.append((kind, owner, span))

        for index, (left_kind, left_owner, left_span) in enumerate(valid_owners):
            for right_kind, right_owner, right_span in valid_owners[index + 1:]:
                overlap = left_span.start <= right_span.end and right_span.start <= left_span.end
                if not overlap:
                    continue
                shared_rule_span = (
                    left_kind == right_kind == "rule"
                    and left_owner != right_owner
                    and left_span == right_span
                )
                if not shared_rule_span:
                    issues.append(ValidationIssue(
                        "overlapping_ranges",
                        f"{left_owner} lines {left_span.start}-{left_span.end} overlap "
                        f"{right_owner} lines {right_span.start}-{right_span.end}",
                    ))

        covered: set[int] = set()
        owner_lines: dict[str, set[int]] = {}
        for _, owner, span in valid_owners:
            numbers = set(range(span.start, span.end + 1))
            covered.update(numbers)
            owner_lines.setdefault(owner, set()).update(numbers)

        missing = [
            number for number, line in enumerate(skill_input.lines, start=1)
            if line.strip() and number not in covered
        ]
        for start, end in _contiguous_ranges(missing):
            label = f"line {start}" if start == end else f"lines {start}-{end}"
            issues.append(ValidationIssue("unassigned_lines", f"{label} are not assigned or excluded"))

        for start, end in _fenced_blocks(skill_input.lines):
            block = set(range(start, end + 1))
            for owner, selected in owner_lines.items():
                if selected & block and not block <= selected:
                    issues.append(ValidationIssue(
                        "split_code_block",
                        f"{owner} selects only part of fenced code block at lines {start}-{end}",
                    ))

        known_ids: set[str] = set()
        previous_start = 0
        for rule in ir.rules:
            if rule.id in known_ids:
                issues.append(ValidationIssue("duplicate_rule_id", f"rule ID {rule.id!r} is repeated"))
            known_ids.add(rule.id)
            first_line = min(span.start for span in rule.source)
            if first_line < previous_start:
                issues.append(ValidationIssue(
                    "rule_order", f"rule {rule.id!r} appears before an earlier source rule"
                ))
            previous_start = first_line

            possible_by_field: dict[str, set[str]] = {}
            for predicate in rule.when:
                feature = feature_schema.features.get(predicate.field)
                if feature is None:
                    issues.append(ValidationIssue(
                        "unknown_feature", f"rule {rule.id!r} uses unknown feature {predicate.field!r}"
                    ))
                    continue
                if (predicate.op == "contains") != feature.multiple:
                    expected = "contains" if feature.multiple else "eq or in"
                    issues.append(ValidationIssue(
                        "invalid_feature_operator",
                        f"rule {rule.id!r} must use {expected} for {predicate.field!r}",
                    ))
                    continue
                values = {predicate.value} if isinstance(predicate.value, str) else set(predicate.value)
                invalid = values - set(feature.values)
                if invalid:
                    issues.append(ValidationIssue(
                        "unknown_feature_value",
                        f"rule {rule.id!r} uses {predicate.field} value(s) {sorted(invalid)!r}; "
                        f"allowed: {feature.values!r}",
                    ))
                if feature.multiple:
                    # Different membership requirements may all hold in one case.
                    continue
                if predicate.field in possible_by_field:
                    possible_by_field[predicate.field] &= values
                    if not possible_by_field[predicate.field]:
                        issues.append(ValidationIssue(
                            "contradictory_condition",
                            f"rule {rule.id!r} has incompatible predicates on {predicate.field!r}",
                        ))
                else:
                    possible_by_field[predicate.field] = set(values)
        return issues


def _contiguous_ranges(numbers: Sequence[int]) -> list[tuple[int, int]]:
    if not numbers:
        return []
    groups: list[tuple[int, int]] = []
    start = end = numbers[0]
    for number in numbers[1:]:
        if number == end + 1:
            end = number
        else:
            groups.append((start, end))
            start = end = number
    groups.append((start, end))
    return groups


def _fenced_blocks(lines: Sequence[str]) -> list[tuple[int, int]]:
    blocks: list[tuple[int, int]] = []
    opening: tuple[int, str, int] | None = None
    for number, line in enumerate(lines, start=1):
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if opening is None:
            if fence:
                marker = fence.group(1)
                opening = (number, marker[0], len(marker))
        elif fence and fence.group(1)[0] == opening[1] and len(fence.group(1)) >= opening[2]:
            if not line[fence.end():].strip():
                blocks.append((opening[0], number))
                opening = None
    if opening is not None:
        blocks.append((opening[0], len(lines)))
    return blocks


class Fragment(StrictModel):
    id: str
    text: str
    start: int = Field(ge=1)
    end: int = Field(ge=1)
    order: int = Field(ge=1)
    role: Literal["instruction", "non_instruction"]
    requires: list[str]


def _fragment_id(start: int, end: int, text: str) -> str:
    digest = hashlib.sha256(f"{start}:{end}:".encode() + text.encode()).hexdigest()[:16]
    return f"frag_{digest}"


def _is_heading(line: str) -> int | None:
    match = re.match(r"^ {0,3}(#{1,6})\s+", line)
    return len(match.group(1)) if match else None


def _table_rows(lines: Sequence[str]) -> dict[int, tuple[int, int]]:
    """Map each data-row line to its table header span."""
    rows: dict[int, tuple[int, int]] = {}
    index = 0
    while index + 1 < len(lines):
        header = lines[index].strip()
        delimiter = lines[index + 1].strip()
        cells = [cell.strip() for cell in delimiter.strip("|").split("|")]
        is_table = (
            header.startswith("|") and header.endswith("|")
            and delimiter.startswith("|") and delimiter.endswith("|")
            and len(cells) >= 2
            and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)
        )
        if not is_table:
            index += 1
            continue
        start, end = index + 1, index + 2
        index += 2
        while index < len(lines) and lines[index].strip().startswith("|"):
            rows[index + 1] = (start, end)
            index += 1
    return rows


@dataclass(frozen=True)
class FragmentCatalog:
    """Exact source slices plus structural dependencies for rendering."""

    fragments: tuple[Fragment, ...]
    skill_input: SkillInput
    ir: SkillIR

    @classmethod
    def from_ir(cls, skill_input: SkillInput, ir: SkillIR) -> FragmentCatalog:
        all_ranges = [*ir.root, *(span for rule in ir.rules for span in rule.source), *ir.excluded]
        for span in all_ranges:
            if span.start < 1 or span.end > len(skill_input.lines) or span.end < span.start:
                raise ValueError(f"invalid fragment source range {span.start}-{span.end}")
        spans: dict[tuple[int, int], str] = {}
        rule_spans: set[tuple[int, int]] = set()

        def add_span(start: int, end: int, role: str, *, shared_rule: bool = False) -> None:
            for (other_start, other_end), other_role in spans.items():
                if (start, end) == (other_start, other_end) and shared_rule and (start, end) in rule_spans:
                    return  # Two rules may share exactly one source fragment.
                if start <= other_end and other_start <= end:
                    raise ValueError(
                        f"fragment range {start}-{end} overlaps {other_start}-{other_end}"
                    )
            spans[(start, end)] = role

        for span in ir.root:
            add_span(span.start, span.end, "instruction")
        for rule in ir.rules:
            for span in rule.source:
                add_span(span.start, span.end, "instruction", shared_rule=True)
                rule_spans.add((span.start, span.end))

        # Split excluded material into structural units. A Markdown table's
        # header and delimiter must stay together when a selected row uses them.
        table_rows = _table_rows(skill_input.lines)
        headers = set(table_rows.values())
        for span in ir.excluded:
            line = span.start
            while line <= span.end:
                header = next((pair for pair in headers if pair[0] == line), None)
                if header is not None and header[1] <= span.end:
                    add_span(*header, "non_instruction")
                    line = header[1] + 1
                else:
                    if skill_input.lines[line - 1].strip():
                        add_span(line, line, "non_instruction")
                    line += 1

        fragments: list[Fragment] = []
        for (start, end), role in sorted(spans.items()):
            if start < 1 or end > len(skill_input.lines) or end < start:
                raise ValueError(f"invalid fragment source range {start}-{end}")
            text = "".join(skill_input.lines[start - 1:end])
            fragments.append(Fragment(
                id=_fragment_id(start, end, text), text=text,
                start=start, end=end, order=start, role=role, requires=[],
            ))
        by_line = {
            number: fragment
            for fragment in fragments
            for number in range(fragment.start, fragment.end + 1)
        }
        if len({fragment.id for fragment in fragments}) != len(fragments):
            raise ValueError("fragment ID collision")

        completed: list[Fragment] = []
        for fragment in fragments:
            if fragment.role != "instruction":
                completed.append(fragment)
                continue
            headings: list[tuple[int, int]] = []
            for number, line in enumerate(skill_input.lines[:fragment.start - 1], start=1):
                level = _is_heading(line)
                if level is not None:
                    while headings and headings[-1][0] >= level:
                        headings.pop()
                    headings.append((level, number))
            required: list[str] = []
            for _, number in headings:
                dependency = by_line.get(number)
                if dependency is not None and dependency.role == "non_instruction":
                    required.append(dependency.id)
            for number in range(fragment.start, fragment.end + 1):
                header = table_rows.get(number)
                if header is None:
                    continue
                dependency = by_line.get(header[0])
                if dependency is None or dependency.end < header[1] or dependency.role != "non_instruction":
                    raise ValueError(
                        f"table row at line {number} needs its header at lines "
                        f"{header[0]}-{header[1]} classified as excluded metadata"
                    )
                required.append(dependency.id)
            completed.append(fragment.model_copy(update={"requires": list(dict.fromkeys(required))}))

        catalog = cls(tuple(completed), skill_input, ir)
        catalog.validate_coverage()
        return catalog

    def ids_for(self, spans: Sequence[LineRange]) -> list[str]:
        lookup = {(f.start, f.end): f.id for f in self.fragments if f.role == "instruction"}
        try:
            return list(dict.fromkeys(lookup[(span.start, span.end)] for span in spans))
        except KeyError as error:
            raise ValueError(f"missing instructional fragment for range {error.args[0]}") from error

    def validate_coverage(self) -> None:
        by_id = {fragment.id: fragment for fragment in self.fragments}
        if len(by_id) != len(self.fragments):
            raise ValueError("duplicate fragment ID")
        assigned: set[int] = set()
        for fragment in self.fragments:
            if fragment.end > len(self.skill_input.lines) or fragment.end < fragment.start:
                raise ValueError(f"invalid fragment range for {fragment.id}")
            text = "".join(self.skill_input.lines[fragment.start - 1:fragment.end])
            if fragment.text != text or fragment.id != _fragment_id(fragment.start, fragment.end, text):
                raise ValueError(f"fragment {fragment.id} differs from the original skill")
            if fragment.order != fragment.start:
                raise ValueError(f"fragment {fragment.id} has invalid source order")
            for required in fragment.requires:
                dependency = by_id.get(required)
                if dependency is None or dependency.role != "non_instruction":
                    raise ValueError(f"fragment {fragment.id} has invalid dependency {required}")
            assigned.update(range(fragment.start, fragment.end + 1))
        unassigned = [
            number for number, line in enumerate(self.skill_input.lines, start=1)
            if line.strip() and number not in assigned
        ]
        if unassigned:
            raise ValueError(f"uncovered nonblank skill line(s): {unassigned}")
        for spans in [self.ir.root, *(rule.source for rule in self.ir.rules)]:
            self.ids_for(spans)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": ARTIFACT_VERSION,
            "source_sha256": self.skill_input.sha256,
            "fragments": [fragment.model_dump() for fragment in self.fragments],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], skill_input: SkillInput, ir: SkillIR) -> FragmentCatalog:
        if set(data) != {"schema_version", "source_sha256", "fragments"}:
            raise ValueError("invalid fragment catalog fields")
        if data["schema_version"] != ARTIFACT_VERSION or data["source_sha256"] != skill_input.sha256:
            raise ValueError("fragment catalog version or source hash does not match")
        loaded = tuple(Fragment.model_validate(item) for item in data["fragments"])
        expected = cls.from_ir(skill_input, ir)
        if loaded != expected.fragments:
            raise ValueError("fragment catalog does not match the accepted IR and source")
        return cls(loaded, skill_input, ir)


class SkillTreeCompiler:
    """Compile rule predicates into a flat MCRDR tree.

    Node 0 is the root default, reported only when no child fires, so it holds
    no fragments. Node 1 is an unconditional child carrying general guidance.
    Each conditional rule is another child of the root, in source order.
    """

    def compile(
        self, ir: SkillIR, catalog: FragmentCatalog, evaluator: ConditionEvaluator
    ) -> RDREngine:
        if ir != catalog.ir:
            raise ValueError("rule list and fragment catalog differ")
        catalog.validate_coverage()

        def rule_node(node_id: int, conditions: list[Any], fragments: list[str]) -> dict[str, Any]:
            return {
                "id": node_id, "conditions": conditions,
                "conclusion": {"fragments": fragments},
                "stopping": False, "evidence": [], "children": [],
            }

        nodes = [rule_node(0, [], []), rule_node(1, [], catalog.ids_for(ir.root))]
        for rule in ir.rules:
            nodes.append(rule_node(
                len(nodes),
                [predicate.model_dump() for predicate in rule.when],
                catalog.ids_for(rule.source),
            ))
        nodes[0]["children"] = list(range(1, len(nodes)))
        return RDREngine.from_dict({
            "schema_version": RDREngine.SCHEMA_VERSION,
            "revision": 0,
            "max_selection_attempts": 3,
            "root_id": 0,
            "nodes": nodes,
        }, evaluator=evaluator)


def _json_bytes(data: Any) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class SkillBundle:
    """Write and load a checked set of skill source, IR, fragments, and tree."""

    @staticmethod
    def build(
        skill_dir: str | Path, *, ir_path: str | Path, feature_schema: FeatureSchema,
        model_id: str = "unspecified", force: bool = False,
    ) -> tuple[RDREngine, FragmentCatalog]:
        from .skill_cases import SkillConditionEvaluator

        directory = Path(skill_dir).resolve()
        source = SkillInput.from_markdown(directory / "initial.md")
        ir = SkillIR.model_validate_json(Path(ir_path).read_text(encoding="utf-8"), strict=True)
        issues = SkillIRValidator().validate(ir, source, feature_schema)
        if issues:
            raise ValueError("invalid rule list:\n" + "\n".join(str(issue) for issue in issues))
        catalog = FragmentCatalog.from_ir(source, ir)
        tree = SkillTreeCompiler().compile(ir, catalog, SkillConditionEvaluator(feature_schema))
        target = directory / "compiled"
        ir_target = target / "ir.json"
        files_to_write = {
            "ir.json": _json_bytes(ir.model_dump()),
            "fragments.json": _json_bytes(catalog.to_dict()),
            "tree.json": _json_bytes(tree.to_dict()),
        }
        source_ir = Path(ir_path).resolve()
        conflicts = [
            target / name for name in files_to_write
            if (target / name).exists() and not force and (target / name).resolve() != source_ir
        ]
        if (target / "manifest.json").exists() and not force:
            conflicts.append(target / "manifest.json")
        if conflicts:
            raise ValueError(f"compiled artifact already exists: {conflicts[0]}; use --force to rebuild")
        target.mkdir(parents=True, exist_ok=True)
        for name, data in files_to_write.items():
            destination = target / name
            if destination.resolve() != source_ir or force:
                destination.write_bytes(data)
        manifest = {
            "schema_version": ARTIFACT_VERSION,
            "engine_schema_version": RDREngine.SCHEMA_VERSION,
            "prompt_version": PROMPT_VERSION,
            "model_id": model_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "feature_schema": feature_schema.model_dump(),
            "feature_schema_sha256": _sha256(_json_bytes(feature_schema.model_dump())),
            "source_sha256": source.sha256,
            "ir_sha256": _sha256(ir_target.read_bytes()),
            "fragments_sha256": _sha256((target / "fragments.json").read_bytes()),
            "tree_sha256": _sha256((target / "tree.json").read_bytes()),
        }
        (target / "manifest.json").write_bytes(_json_bytes(manifest))
        return tree, catalog

    @staticmethod
    def load(
        skill_dir: str | Path, evaluator: ConditionEvaluator | None = None
    ) -> tuple[RDREngine, FragmentCatalog]:
        from .skill_cases import SkillConditionEvaluator

        directory = Path(skill_dir).resolve()
        compiled = directory / "compiled"
        manifest = json.loads((compiled / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("schema_version") != ARTIFACT_VERSION:
            raise ValueError(
                f"skill bundle version {manifest.get('schema_version')!r} is not {ARTIFACT_VERSION}; "
                "recompile with `skill_tool compile --force`"
            )
        source = SkillInput.from_markdown(directory / "initial.md")
        if source.sha256 != manifest.get("source_sha256"):
            raise ValueError("skill source changed since compilation")
        for name in ("ir", "fragments", "tree"):
            if _sha256((compiled / f"{name}.json").read_bytes()) != manifest.get(f"{name}_sha256"):
                raise ValueError(f"{name}.json changed since compilation")
        features = manifest.get("feature_schema")
        if _sha256(_json_bytes(features)) != manifest.get("feature_schema_sha256"):
            raise ValueError("feature schema hash does not match")
        schema = FeatureSchema.model_validate(features)
        ir = SkillIR.model_validate_json((compiled / "ir.json").read_text(encoding="utf-8"), strict=True)
        issues = SkillIRValidator().validate(ir, source, schema)
        if issues:
            raise ValueError("stored rule list is invalid:\n" + "\n".join(str(issue) for issue in issues))
        catalog = FragmentCatalog.from_dict(
            json.loads((compiled / "fragments.json").read_text(encoding="utf-8")), source, ir
        )
        tree = RDREngine.load(compiled / "tree.json", evaluator=evaluator or SkillConditionEvaluator(schema))
        known = {fragment.id for fragment in catalog.fragments}
        for node in tree.nodes:
            if node.stopping:
                continue  # The engine already requires a null conclusion.
            operation = node.conclusion
            if not isinstance(operation, dict) or set(operation) != {"fragments"}:
                raise ValueError(f"node {node.id} has an invalid fragment conclusion")
            if not isinstance(operation["fragments"], list) or any(
                not isinstance(item, str) or item not in known for item in operation["fragments"]
            ):
                raise ValueError(f"node {node.id} refers to an unknown fragment")
        return tree, catalog


class RuleModel(Protocol):
    def generate(
        self, *, system_instruction: str, user_input: str, response_schema: dict[str, Any]
    ) -> str: ...


def _response_schema() -> dict[str, Any]:
    """Keep API constraints to Gemini's supported JSON Schema subset.

    Pydantic still checks string lengths and rule-ID patterns after generation.
    """

    def strip_unsupported(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: strip_unsupported(item)
                for key, item in value.items()
                if key not in {"minLength", "maxLength", "pattern"}
            }
        if isinstance(value, list):
            return [strip_unsupported(item) for item in value]
        return value

    return strip_unsupported(SkillIR.model_json_schema())


class GeminiRuleModel:
    """Small Gemini Interactions API adapter; the key remains a placeholder here."""

    def __init__(
        self, api_key: str, model: str = DEFAULT_MODEL, *,
        http_options: dict[str, Any] | None = None,
    ) -> None:
        if not api_key or api_key == "REPLACE_WITH_YOUR_GEMINI_API_KEY":
            raise ValueError("Set GEMINI_API_KEY or replace its placeholder near the top of this file")
        self.api_key = api_key
        self.model = model
        self.http_options = http_options

    def generate(
        self, *, system_instruction: str, user_input: str, response_schema: dict[str, Any]
    ) -> str:
        from google import genai

        client = genai.Client(api_key=self.api_key, http_options=self.http_options)
        try:
            interaction = client.interactions.create(
                model=self.model,
                system_instruction=system_instruction,
                input=user_input,
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": response_schema,
                },
                store=False,
            )
            if not interaction.output_text:
                raise RuntimeError("Gemini returned no text response")
            return interaction.output_text
        finally:
            client.close()


class SkillRuleExtractor:
    def __init__(
        self, model: RuleModel, *, system_prompt: str | None = None, max_repairs: int = 2
    ) -> None:
        if max_repairs < 0:
            raise ValueError("max_repairs must be nonnegative")
        self.model = model
        self.system_prompt = (
            system_prompt if system_prompt is not None
            else files("rdr_engine").joinpath(
                "prompts", "skill_to_rules_system.md"
            ).read_text(encoding="utf-8")
        )
        self.max_repairs = max_repairs
        self.validator = SkillIRValidator()

    def extract(self, skill_input: SkillInput, feature_schema: FeatureSchema) -> SkillIR:
        prompt = self._source_prompt(skill_input, feature_schema)
        raw = self.model.generate(
            system_instruction=self.system_prompt,
            user_input=prompt,
            response_schema=_response_schema(),
        )
        for attempt in range(self.max_repairs + 1):
            ir, issues = self._parse_and_validate(raw, skill_input, feature_schema)
            if ir is not None and not issues:
                return ir
            if attempt == self.max_repairs:
                raise RuleExtractionError(issues, attempt + 1)
            raw = self.repair(skill_input, raw, issues, feature_schema)
        raise AssertionError("unreachable")

    def repair(
        self,
        skill_input: SkillInput,
        previous_output: str,
        issues: Sequence[ValidationIssue],
        feature_schema: FeatureSchema,
    ) -> str:
        feedback = "\n".join(f"- {issue}" for issue in issues)
        user_input = (
            self._source_prompt(skill_input, feature_schema)
            + "\n\nYour previous JSON response was:\n"
            + previous_output
            + "\n\nCorrect every validation error below and return the full JSON object again:\n"
            + feedback
        )
        return self.model.generate(
            system_instruction=self.system_prompt,
            user_input=user_input,
            response_schema=_response_schema(),
        )

    @staticmethod
    def _source_prompt(skill_input: SkillInput, feature_schema: FeatureSchema) -> str:
        features = feature_schema.model_dump_json(indent=2)
        return (
            f"Skill: {skill_input.path.name}\n"
            f"SHA-256: {skill_input.sha256}\n\n"
            f"Allowed task features:\n{features}\n\n"
            f"Complete numbered Markdown skill:\n{skill_input.numbered_markdown}\n"
        )

    def _parse_and_validate(
        self, raw: str, skill_input: SkillInput, feature_schema: FeatureSchema
    ) -> tuple[SkillIR | None, list[ValidationIssue]]:
        try:
            ir = SkillIR.model_validate_json(raw, strict=True)
        except ValidationError as error:
            issues = [
                ValidationIssue(
                    "invalid_response",
                    f"{'.'.join(map(str, detail['loc'])) or 'root'}: {detail['msg']}",
                )
                for detail in error.errors(include_url=False)[:20]
            ]
            return None, issues
        return ir, self.validator.validate(ir, skill_input, feature_schema)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract and validate rules from one full Markdown skill")
    parser.add_argument("skill", type=Path, help="Path to a skill's initial.md")
    parser.add_argument("--features", type=Path, help="Feature schema catalog JSON")
    parser.add_argument("--prompt", type=Path, help="Override the packaged system prompt")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--output", type=Path, help="Save accepted IR here; otherwise print it")
    args = parser.parse_args(argv)

    try:
        skill_input = SkillInput.from_markdown(args.skill)
        output = args.output.resolve() if args.output else None
        if output is not None and output.exists():
            raise ValueError(f"output already exists: {output}; choose a new path")
        features_path = args.features or skill_input.path.parent.parent / "feature_schemas.json"
        feature_schema = FeatureSchema.from_file(features_path, skill_input.path.parent.name)
        system_prompt = args.prompt.read_text(encoding="utf-8") if args.prompt else None
        api_key = os.environ.get("GEMINI_API_KEY") or GEMINI_API_KEY
        extractor = SkillRuleExtractor(
            GeminiRuleModel(api_key, model=args.model), system_prompt=system_prompt
        )
        ir = extractor.extract(skill_input, feature_schema)
        payload = ir.model_dump_json(indent=2) + "\n"
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(payload, encoding="utf-8")
            print(f"Accepted rule list saved to {output}; review its source ranges and conditions.")
        else:
            print(payload, end="")
        return 0
    except (OSError, UnicodeError, ValueError) as error:
        parser.exit(1, f"error: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
