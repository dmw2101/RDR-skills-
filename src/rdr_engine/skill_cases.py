"""Evaluate typed skill predicates and derive conservative pre-execution cases."""

from __future__ import annotations

import re
from typing import Any, Mapping

from .skill_compiler import FeatureSchema


FeatureValue = str | list[str]


class SkillConditionEvaluator:
    def __init__(self, feature_schema: FeatureSchema) -> None:
        self.feature_schema = feature_schema

    def validate_case(self, case: Any, required_fields: set[str] | None = None) -> None:
        if not isinstance(case, dict):
            raise ValueError("a skill case must be a JSON object")
        required = required_fields if required_fields is not None else set(self.feature_schema.features)
        for field in required:
            spec = self.feature_schema.features.get(field)
            if spec is None:
                raise ValueError(f"unknown case feature {field!r}")
            if field not in case:
                raise ValueError(f"missing required case feature {field!r}")
            value = case[field]
            if spec.multiple:
                if not isinstance(value, list) or any(
                    not isinstance(item, str) or item not in spec.values for item in value
                ):
                    raise ValueError(f"invalid value for {field!r}; expected a list from {spec.values}")
                if len(value) != len(set(value)):
                    raise ValueError(f"duplicate values for {field!r}")
            elif not isinstance(value, str) or value not in spec.values:
                raise ValueError(f"invalid value for {field!r}; allowed: {spec.values}")

    def evaluate(self, condition: Any, case: Any) -> bool:
        if not isinstance(condition, dict) or set(condition) != {"field", "op", "value"}:
            raise ValueError("invalid skill predicate")
        field, op, value = condition["field"], condition["op"], condition["value"]
        if not isinstance(field, str) or field not in self.feature_schema.features:
            raise ValueError(f"unknown predicate feature {field!r}")
        self.validate_case(case, {field})
        spec = self.feature_schema.features[field]
        allowed = spec.values
        if spec.multiple:
            if op == "contains" and isinstance(value, str) and value in allowed:
                return value in case[field]
            raise ValueError(f"invalid predicate for {field!r}; use contains for a multiple-value feature")
        if op == "eq" and isinstance(value, str) and value in allowed:
            return case[field] == value
        if op == "in" and isinstance(value, list) and value and all(
            isinstance(item, str) and item in allowed for item in value
        ):
            return case[field] in value
        raise ValueError(f"invalid predicate for {field!r}")


def _alfworld_task_types(task: str) -> list[str]:
    cues = {
        "examine_in_light": r"\b(examine|inspect)\b",
        "clean_place": r"\b(clean|wash|washed)\b",
        "heat_place": r"\b(heat|hot|warm)\b",
        "cool_place": r"\b(cool|cold|chill)\b",
        "pick_two_place": r"\b(two|2)\b",
    }
    matched = [name for name, pattern in cues.items() if re.search(pattern, task, re.I)]
    if matched:
        return matched
    return ["pick_place"] if re.search(r"\b(put|place|move)\b", task, re.I) else []


def build_case(
    skill_id: str, task: str, visible_inputs: Mapping[str, FeatureValue] | None,
    feature_schema: FeatureSchema,
) -> dict[str, FeatureValue]:
    """Use only the task and explicitly visible pre-execution feature values.

    Heuristics provide a convenient demo case. Pass an explicit feature value in
    visible_inputs when the task wording is ambiguous or inputs need inspection.
    """
    if not isinstance(task, str):
        raise ValueError("task must be text")
    case: dict[str, FeatureValue] = {
        field: [] if spec.multiple else "unknown"
        for field, spec in feature_schema.features.items()
        if spec.multiple or "unknown" in spec.values
    }
    if skill_id == "alfworld":
        case["task_type"] = _alfworld_task_types(task)
    elif skill_id == "livemathematicianbench":
        case["has_quantifier"] = (
            "yes" if re.search(r"\b(all|every|each|exists|there is|some|any)\b", task, re.I)
            else "no"
        )
    elif skill_id == "spreadsheetbench":
        if re.search(r"\b(aggregate|aggregation|sort|group|filter|merge|transform|bulk)\b", task, re.I):
            case["operation"] = "bulk_transform"
        elif re.search(r"\b(cell|range|value)\b", task, re.I):
            case["operation"] = "simple_cell_edit"
        else:
            case["operation"] = "other" if task.strip() else "unknown"
        if re.search(r"\bpreserve\b.{0,25}\bformatting\b", task, re.I):
            case["preserve_formatting"] = "yes"

    if skill_id == "pdf":
        case.update(_pdf_case(task))

    # Older bundles omitted `multiple` and still expect one scalar value.
    for field, value in list(case.items()):
        spec = feature_schema.features[field]
        if spec.multiple and isinstance(value, str):
            case[field] = [] if value == "unknown" else [value]
        elif not spec.multiple and isinstance(value, list):
            case[field] = value[0] if len(value) == 1 else "unknown"

    for field, value in (visible_inputs or {}).items():
        if field not in feature_schema.features:
            raise ValueError(f"unknown visible feature {field!r}")
        case[field] = value
    SkillConditionEvaluator(feature_schema).validate_case(case)
    return case


def _pdf_case(task: str) -> dict[str, FeatureValue]:
    """Infer all matching PDF operations and explicit source-type cues."""
    cues = {
        "extract_content": (
            r"\b(extract(?:ing|ion)?|read(?:ing)?)\b"
            r"|\bmake\b.{0,40}\bsearchable\b"
        ),
        "combine_or_split": r"\b(merg(?:e|ing)|combin(?:e|ing)|split(?:ting)?|separat(?:e|ing))\b",
        "rotate_pages": r"\b(rotat(?:e|ing|ion)|reorient)\b",
        "create_or_watermark": r"\b(creat(?:e|ing)|generat(?:e|ing)|watermark(?:s|ing)?)\b",
        "fill_forms": r"\b(fill(?:ing)?|complete)\b.{0,40}\b(forms?|fields?)\b",
        "password_security": (
            r"\b(encrypt(?:ing|ion)?|decrypt(?:ing|ion)?|unlock)\b"
            r"|\bpassword[- ]protect\b"
            r"|\b(add|remove|set|change)\b.{0,40}\bpassword\b"
        ),
    }
    matched = [name for name, pattern in cues.items() if re.search(pattern, task, re.I)]

    scanned = bool(re.search(r"\b(scanned|image[- ](?:only|based)|ocr)\b", task, re.I))
    text_based = bool(re.search(
        r"\b(text[- ]based|born[- ]digital|digitally[- ]generated|selectable text)\b", task, re.I
    ))
    if re.search(r"\bmixed\b", task, re.I) or (scanned and text_based):
        source_kind = "mixed"
    elif scanned:
        source_kind = "scanned_image"
    elif text_based:
        source_kind = "text_based"
    else:
        source_kind = "unknown"
    return {"operation": matched, "source_kind": source_kind}
