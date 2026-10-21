"""Conditions, visible context, and true/false/unknown judgments."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from ._json import ValidationError, fields, strings, text


class Truth(str, Enum):
    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, value):
        if type(value) is not str:
            raise ValidationError("Truth must be the string true, false, or unknown")
        try:
            return cls(value)
        except ValueError as exc:
            raise ValidationError(f"Invalid truth status: {value!r}") from exc


@dataclass(frozen=True)
class Expression:
    kind: str
    condition_id: str | None = None
    children: tuple[Expression, ...] = ()

    def __post_init__(self):
        if type(self.children) is not tuple or any(not isinstance(c, Expression) for c in self.children):
            raise ValidationError("Expression children must be immutable expressions")
        if self.kind == "condition":
            text(self.condition_id, "condition ID")
            if self.children:
                raise ValidationError("A condition reference cannot have children")
        elif self.kind not in ("all", "any", "not") or self.condition_id is not None:
            raise ValidationError("Invalid expression kind or condition ID")
        elif self.kind == "not" and len(self.children) != 1:
            raise ValidationError("not needs exactly one expression")

    @classmethod
    def from_dict(cls, data, _depth=0):
        if _depth > 64 or type(data) is not dict or len(data) != 1:
            raise ValidationError("Expression must have one operator and depth <= 64")
        kind, value = next(iter(data.items()))
        if kind == "condition":
            return cls(kind, text(value, "condition ID"))
        if kind == "not":
            return cls(kind, children=(cls.from_dict(value, _depth + 1),))
        if kind in ("all", "any") and type(value) is list:
            return cls(kind, children=tuple(cls.from_dict(x, _depth + 1) for x in value))
        raise ValidationError("Expected condition, all, any, or not")

    def to_dict(self):
        if self.kind == "condition":
            return {"condition": self.condition_id}
        if self.kind == "not":
            return {"not": self.children[0].to_dict()}
        return {self.kind: [child.to_dict() for child in self.children]}

    @property
    def condition_ids(self):
        if self.kind == "condition":
            return frozenset((self.condition_id,))
        return frozenset().union(*(child.condition_ids for child in self.children))

    def evaluate(self, judgments: Mapping[str, Truth]):
        if self.kind == "condition":
            if self.condition_id not in judgments:
                raise ValidationError(f"Missing condition judgment: {self.condition_id}")
            result = judgments[self.condition_id]
            if not isinstance(result, Truth):
                raise ValidationError("Expression evaluation requires Truth values")
            return result
        values = [child.evaluate(judgments) for child in self.children]
        if self.kind == "not":
            return {Truth.TRUE: Truth.FALSE, Truth.FALSE: Truth.TRUE, Truth.UNKNOWN: Truth.UNKNOWN}[values[0]]
        if self.kind == "all":
            return Truth.FALSE if Truth.FALSE in values else Truth.UNKNOWN if Truth.UNKNOWN in values else Truth.TRUE
        return Truth.TRUE if Truth.TRUE in values else Truth.UNKNOWN if Truth.UNKNOWN in values else Truth.FALSE

    def proves_exclusion(self, judgments):
        """Unknown is never distinguishing negative evidence for future learning."""
        return self.evaluate(judgments) is Truth.FALSE


@dataclass(frozen=True)
class FragmentDelta:
    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()

    def __post_init__(self):
        if type(self.add) is not tuple or type(self.remove) is not tuple:
            raise ValidationError("Fragment deltas use immutable ID tuples")
        strings(list(self.add), "add")
        strings(list(self.remove), "remove")
        if set(self.add) & set(self.remove):
            raise ValidationError("One delta cannot both add and remove a fragment")

    @classmethod
    def from_dict(cls, data):
        fields(data, ("add", "remove"))
        return cls(strings(data["add"], "add"), strings(data["remove"], "remove"))

    def to_dict(self):
        return {"add": list(self.add), "remove": list(self.remove)}


@dataclass(frozen=True)
class Evidence:
    id: str
    text: str
    source: str

    def __post_init__(self):
        for field in ("id", "text", "source"):
            text(getattr(self, field), field)

    @classmethod
    def from_dict(cls, data):
        fields(data, ("id", "text", "source"))
        return cls(**data)

    def to_dict(self):
        return {"id": self.id, "text": self.text, "source": self.source}


@dataclass(frozen=True)
class Judgment:
    condition_id: str
    status: Truth
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self):
        text(self.condition_id, "condition_id")
        if not isinstance(self.status, Truth) or type(self.evidence_refs) is not tuple:
            raise ValidationError("Invalid immutable judgment")
        strings(list(self.evidence_refs), "evidence_refs")
        if self.status is not Truth.UNKNOWN and not self.evidence_refs:
            raise ValidationError("A true/false judgment requires evidence references")

    @classmethod
    def from_dict(cls, data):
        fields(data, ("condition_id", "status", "evidence_refs"))
        return cls(data["condition_id"], Truth.parse(data["status"]), strings(data["evidence_refs"], "evidence_refs"))

    def to_dict(self):
        return {"condition_id": self.condition_id, "status": self.status.value, "evidence_refs": list(self.evidence_refs)}


@dataclass(frozen=True)
class Attribute:
    name: str
    value: str | tuple[str, ...]
    evidence_refs: tuple[str, ...]

    def __post_init__(self):
        text(self.name, "attribute name")
        if type(self.value) is tuple:
            strings(list(self.value), "attribute values")
        else:
            text(self.value, "attribute value")
        if type(self.evidence_refs) is not tuple:
            raise ValidationError("Attribute evidence must be immutable")
        strings(list(self.evidence_refs), "attribute evidence", nonempty=True)

    @classmethod
    def from_dict(cls, data):
        fields(data, ("name", "value", "evidence_refs"))
        value = strings(data["value"], "attribute values") if type(data["value"]) is list else data["value"]
        return cls(data["name"], value, strings(data["evidence_refs"], "evidence_refs", nonempty=True))

    def to_dict(self):
        return {"name": self.name, "value": list(self.value) if type(self.value) is tuple else self.value,
                "evidence_refs": list(self.evidence_refs)}


@dataclass(frozen=True)
class ContextSnapshot:
    skill_id: str
    task: str
    observations: tuple[Evidence, ...] = ()
    facts: tuple[Judgment, ...] = ()
    attributes: tuple[Attribute, ...] = ()

    def __post_init__(self):
        text(self.skill_id, "skill_id")
        text(self.task, "task")
        for name, kind in (("observations", Evidence), ("facts", Judgment), ("attributes", Attribute)):
            values = getattr(self, name)
            if type(values) is not tuple or any(not isinstance(value, kind) for value in values):
                raise ValidationError(f"Invalid immutable {name}")
        ids = ["task", *(o.id for o in self.observations)]
        if len(ids) != len(set(ids)):
            raise ValidationError("Evidence IDs must be unique; task is reserved")
        for name, values in (("fact", [f.condition_id for f in self.facts]),
                             ("attribute", [a.name for a in self.attributes])):
            if len(values) != len(set(values)):
                raise ValidationError(f"Duplicate {name}")
        for item in (*self.facts, *self.attributes):
            if set(item.evidence_refs) - set(ids):
                raise ValidationError("Unknown evidence reference in context")

    @classmethod
    def from_dict(cls, data):
        fields(data, ("skill_id", "task"), ("observations", "facts", "attributes"))
        for name in ("observations", "facts", "attributes"):
            if type(data.get(name, [])) is not list:
                raise ValidationError(f"{name} must be a list")
        return cls(data["skill_id"], data["task"], tuple(Evidence.from_dict(o) for o in data.get("observations", [])),
                   tuple(Judgment.from_dict(f) for f in data.get("facts", [])), tuple(Attribute.from_dict(a) for a in data.get("attributes", [])))

    def to_dict(self):
        return {"skill_id": self.skill_id, "task": self.task,
                "observations": [x.to_dict() for x in self.observations],
                "facts": [x.to_dict() for x in self.facts], "attributes": [x.to_dict() for x in self.attributes]}

    @property
    def evidence_ids(self):
        return frozenset(("task", *(o.id for o in self.observations)))
