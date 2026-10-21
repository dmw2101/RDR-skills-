"""An append-only set of named conditions. Existing IDs never change meaning."""

from __future__ import annotations

from dataclasses import dataclass

from ._json import ValidationError, fields, read, strings, text, write
from .contracts import ContextSnapshot, Expression, Judgment, Truth


@dataclass(frozen=True)
class TypedPredicate:
    field: str
    op: str
    value: str | tuple[str, ...]

    def __post_init__(self):
        text(self.field, "predicate field")
        if self.op == "in":
            if type(self.value) is not tuple:
                raise ValidationError("in requires an immutable value list")
            strings(list(self.value), "predicate values", nonempty=True)
        elif self.op in ("eq", "contains"):
            text(self.value, "predicate value")
        else:
            raise ValidationError("Typed predicate op must be eq, in, or contains")

    @classmethod
    def from_dict(cls, data):
        fields(data, ("field", "op", "value"))
        value = strings(data["value"], "predicate values", nonempty=True) if type(data["value"]) is list else data["value"]
        return cls(data["field"], data["op"], value)

    def to_dict(self):
        return {"field": self.field, "op": self.op,
                "value": list(self.value) if type(self.value) is tuple else self.value}


@dataclass(frozen=True)
class ConditionDefinition:
    id: str
    definition: str
    examples: tuple[str, ...] = ()
    counterexamples: tuple[str, ...] = ()
    typed_predicate: TypedPredicate | None = None

    def __post_init__(self):
        text(self.id, "condition ID")
        text(self.definition, "definition")
        for name in ("examples", "counterexamples"):
            if type(getattr(self, name)) is not tuple:
                raise ValidationError(f"{name} must be immutable")
            strings(list(getattr(self, name)), name)
        if self.typed_predicate is not None and not isinstance(self.typed_predicate, TypedPredicate):
            raise ValidationError("Invalid typed predicate")

    def to_dict(self):
        data = {"id": self.id, "definition": self.definition}
        if self.examples:
            data["examples"] = list(self.examples)
        if self.counterexamples:
            data["counterexamples"] = list(self.counterexamples)
        if self.typed_predicate is not None:
            data["typed_predicate"] = self.typed_predicate.to_dict()
        return data

    @classmethod
    def from_dict(cls, data):
        fields(data, ("id", "definition"), ("examples", "counterexamples", "typed_predicate"))
        return cls(data["id"], data["definition"], strings(data.get("examples", []), "examples"),
                   strings(data.get("counterexamples", []), "counterexamples"),
                   None if "typed_predicate" not in data else TypedPredicate.from_dict(data["typed_predicate"]))


@dataclass(frozen=True)
class ConditionRegistry:
    skill_id: str
    conditions: tuple[ConditionDefinition, ...] = ()

    def __post_init__(self):
        text(self.skill_id, "skill_id")
        if type(self.conditions) is not tuple or any(not isinstance(c, ConditionDefinition) for c in self.conditions):
            raise ValidationError("Conditions must be immutable definitions")
        if len({c.id for c in self.conditions}) != len(self.conditions):
            raise ValidationError("Duplicate condition ID")
        definitions = [" ".join(c.definition.casefold().split()) for c in self.conditions]
        if len(definitions) != len(set(definitions)):
            raise ValidationError("Duplicate definition: reuse the existing condition ID")

    def get(self, condition_id):
        for condition in self.conditions:
            if condition.id == condition_id:
                return condition
        raise ValidationError(f"Unknown condition ID: {condition_id}")

    def register(self, condition):
        if not isinstance(condition, ConditionDefinition):
            raise ValidationError("Expected a condition definition")
        if any(c.id == condition.id for c in self.conditions):
            if self.get(condition.id) == condition:
                return self
            raise ValidationError("Condition IDs are immutable. Use a new ID for a different meaning")
        return ConditionRegistry(self.skill_id, self.conditions + (condition,))

    def validate_expression(self, expression: Expression):
        missing = expression.condition_ids - {c.id for c in self.conditions}
        if missing:
            raise ValidationError(f"Unknown expression conditions: {sorted(missing)}")

    def to_dict(self):
        return {"skill_id": self.skill_id, "conditions": [c.to_dict() for c in self.conditions]}

    @classmethod
    def from_dict(cls, data):
        fields(data, ("skill_id", "conditions"))
        if type(data["conditions"]) is not list:
            raise ValidationError("conditions must be a list")
        return cls(data["skill_id"], tuple(ConditionDefinition.from_dict(c) for c in data["conditions"]))

    def save(self, path):
        write(path, self.to_dict())

    @classmethod
    def load(cls, path):
        return cls.from_dict(read(path))


def import_typed_predicate(registry, *, condition_id, predicate, definition):
    """Reuse an existing eq/in/contains predicate without its LLM compiler."""
    return registry.register(ConditionDefinition(condition_id, definition, typed_predicate=TypedPredicate.from_dict(predicate)))


def inspected_judgments(registry: ConditionRegistry, context: ContextSnapshot):
    """Known inspected facts must be respected by the context evaluator."""
    result = {fact.condition_id: fact for fact in context.facts}
    for condition_id in result:
        registry.get(condition_id)
    attributes = {item.name: item for item in context.attributes}
    for condition in registry.conditions:
        predicate = condition.typed_predicate
        if predicate is None or predicate.field not in attributes:
            continue
        attribute = attributes[predicate.field]
        if attribute.value == "unknown" or (type(attribute.value) is tuple and "unknown" in attribute.value):
            continue
        if predicate.op == "contains":
            if type(attribute.value) is not tuple:
                raise ValidationError("contains requires a multiple-value inspected attribute")
            matched = predicate.value in attribute.value
        else:
            if type(attribute.value) is not str:
                raise ValidationError("eq/in require a scalar inspected attribute")
            matched = attribute.value == predicate.value if predicate.op == "eq" else attribute.value in predicate.value
        fact = Judgment(condition.id, Truth.TRUE if matched else Truth.FALSE, attribute.evidence_refs)
        if condition.id in result and result[condition.id].status not in (Truth.UNKNOWN, fact.status):
            raise ValidationError("Conflicting inspected facts")
        result[condition.id] = fact
    return result
