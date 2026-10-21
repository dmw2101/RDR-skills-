"""Evaluate the registry against the current context, once on every refresh."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from typing import Protocol

from ._json import ValidationError, canonical, fields, loads, read
from .contracts import ContextSnapshot, Judgment, Truth
from .registry import ConditionRegistry, inspected_judgments


class ModelAccessDisabled(RuntimeError):
    pass


@dataclass(frozen=True)
class EvaluationRequest:
    registry: ConditionRegistry
    context: ContextSnapshot

    def __post_init__(self):
        if not isinstance(self.registry, ConditionRegistry) or not isinstance(self.context, ContextSnapshot):
            raise ValidationError("Expected a registry and context")
        if self.registry.skill_id != self.context.skill_id:
            raise ValidationError("Context and registry belong to different skills")
        inspected_judgments(self.registry, self.context)

    def to_dict(self):
        resources = files("trace2skill_bridge")
        facts = inspected_judgments(self.registry, self.context)
        return {"registry": self.registry.to_dict(), "context": self.context.to_dict(),
                "system_prompt": resources.joinpath("prompts/evaluate_context.md").read_text(encoding="utf-8"),
                "response_schema": loads(resources.joinpath("schemas/condition_response.json").read_text(encoding="utf-8")),
                "authoritative_judgments": [facts[k].to_dict() for k in sorted(facts)]}


def prepare_request(registry, context):
    return EvaluationRequest(registry, context)


def validate_response(request, response):
    fields(response, ("judgments",))
    if type(response["judgments"]) is not list:
        raise ValidationError("judgments must be a list")
    judgments = tuple(Judgment.from_dict(j) for j in response["judgments"])
    ids = [j.condition_id for j in judgments]
    if len(ids) != len(set(ids)) or set(ids) != {c.id for c in request.registry.conditions}:
        raise ValidationError("Return every registry condition exactly once, without invented IDs")
    authority = inspected_judgments(request.registry, request.context)
    for judgment in judgments:
        if set(judgment.evidence_refs) - request.context.evidence_ids:
            raise ValidationError(f"Invalid evidence reference for {judgment.condition_id}")
        fact = authority.get(judgment.condition_id)
        if fact is not None and fact.status is not Truth.UNKNOWN:
            if judgment.status is not fact.status or not set(fact.evidence_refs) <= set(judgment.evidence_refs):
                raise ValidationError(f"Judgment must respect and cite the inspected fact: {judgment.condition_id}")
    by_id = {j.condition_id: j for j in judgments}
    return tuple(by_id[c.id] for c in request.registry.conditions)


class JudgmentProvider(Protocol):
    def evaluate(self, request: EvaluationRequest) -> dict: ...


class DisabledProvider:
    def evaluate(self, request):
        raise ModelAccessDisabled("Live inference is disabled. Supply an explicit input/output recording")


class RecordedResponseProvider:
    """Explicit offline substitute for a model; no automatic response reuse."""

    def __init__(self, recording):
        fields(recording, ("registry", "context", "response"))
        self._recording = canonical(recording)
        self.invocations = 0

    @classmethod
    def from_file(cls, path):
        return cls(read(path))

    def evaluate(self, request):
        self.invocations += 1
        recording = loads(self._recording)
        # Keep an offline example paired with the input it describes.
        if recording["registry"] != request.registry.to_dict() or recording["context"] != request.context.to_dict():
            raise ValidationError("This recording describes a different registry or context")
        return recording["response"]


@dataclass(frozen=True)
class EvaluationResult:
    request: EvaluationRequest
    judgments: tuple[Judgment, ...]

    def statuses(self):
        return {item.condition_id: item.status for item in self.judgments}

    def to_dict(self):
        return {"input": self.request.to_dict(), "output": {"judgments": [j.to_dict() for j in self.judgments]},
                "provider_invocations": 1, "model_calls": 0, "semantic_quality": "not_measured"}


def evaluate_context(context, registry, *, provider=None):
    """Every call evaluates once. Logs are never consulted for future results."""
    provider = DisabledProvider() if provider is None else provider
    if type(provider) not in (DisabledProvider, RecordedResponseProvider):
        raise ModelAccessDisabled("This offline phase accepts recorded providers only")
    request = prepare_request(registry, context)
    return EvaluationResult(request, validate_response(request, provider.evaluate(request)))
