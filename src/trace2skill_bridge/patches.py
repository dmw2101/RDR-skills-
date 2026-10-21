"""One direct MAP response: conditions and a correction, never tree placement."""

from dataclasses import dataclass
from importlib.resources import files

from ._json import ValidationError, canonical, fields, loads, read, text
from .contracts import ContextSnapshot, Expression, Judgment
from .evaluation import ModelAccessDisabled, prepare_request, validate_response
from .registry import ConditionDefinition, ConditionRegistry


@dataclass(frozen=True)
class FailedExecution:
    task_id: str
    context: ContextSnapshot
    skill_text: str
    analysis: dict
    source: dict

    def __post_init__(self):
        text(self.task_id, "task_id")
        text(self.skill_text, "skill_text")
        if not isinstance(self.context, ContextSnapshot):
            raise ValidationError("Expected the context visible at the decision being corrected")
        if (type(self.analysis) is not dict or type(self.analysis.get("items")) is not list
                or not self.analysis["items"] or any(type(item) is not dict for item in self.analysis["items"])):
            raise ValidationError("Parsed failure analysis must contain items")
        if str(self.analysis.get("instance_id")) != self.task_id:
            raise ValidationError("Analysis task ID does not match the failed execution")
        fields(self.source, ("evaluation_file", "analysis_file", "repair_flag", "verdict"))
        for name in ("evaluation_file", "analysis_file", "repair_flag"):
            text(self.source[name], name)
        verdict = self.source["verdict"]
        if type(verdict) is not dict or str(verdict.get("id")) != self.task_id or verdict.get("success") is not False:
            raise ValidationError("A proposal requires a failed task verdict, not just analyst prose")
        cases = verdict.get("test_cases")
        if (verdict.get("error") or type(cases) is not list or not cases
                or any(type(c) is not dict or type(c.get("passed")) is not bool for c in cases)
                or not any(c["passed"] is False for c in cases)):
            raise ValidationError("Missing comparisons or setup errors are not verified task failures")
        canonical(self.to_dict())

    def to_dict(self):
        return {"task_id": self.task_id, "context": self.context.to_dict(), "skill_text": self.skill_text,
                "analysis": self.analysis, "source": self.source}

    @classmethod
    def from_dict(cls, data):
        fields(data, ("task_id", "context", "skill_text", "analysis", "source"))
        return cls(data["task_id"], ContextSnapshot.from_dict(data["context"]), data["skill_text"],
                   data["analysis"], data["source"])


@dataclass(frozen=True)
class InstructionEdit:
    file: str
    old_text: str
    new_text: str

    def __post_init__(self):
        text(self.file, "edit file")
        if type(self.old_text) is not str or type(self.new_text) is not str:
            raise ValidationError("Edit text must be strings")
        if self.old_text.strip() == self.new_text.strip():
            raise ValidationError("An edit must change an instruction")

    def to_dict(self):
        return vars(self).copy()

    @classmethod
    def from_dict(cls, data):
        fields(data, ("file", "old_text", "new_text"))
        return cls(**data)


@dataclass(frozen=True)
class PatchRequest:
    registry: ConditionRegistry
    failure: FailedExecution

    def __post_init__(self):
        prepare_request(self.registry, self.failure.context)

    def to_dict(self):
        # Deliberately no tree, node IDs, parent IDs, or fragment IDs here.
        return {"registry": self.registry.to_dict(), "failed_execution": self.failure.to_dict(),
                "system_prompt": files("trace2skill_bridge").joinpath("prompts/create_exception.md").read_text(encoding="utf-8"),
                "response_schema": loads(files("trace2skill_bridge").joinpath("schemas/patch_response.json").read_text(encoding="utf-8"))}


@dataclass(frozen=True)
class PatchProposal:
    request: PatchRequest
    scope: Expression
    when: Expression
    new_conditions: tuple[ConditionDefinition, ...]
    judgments: tuple[Judgment, ...]
    edits: tuple[InstructionEdit, ...]
    reason: str

    def extended_registry(self):
        registry = self.request.registry
        for condition in self.new_conditions:
            registry = registry.register(condition)
        return registry

    def response(self):
        return {"scope": self.scope.to_dict(), "when": self.when.to_dict(),
                "new_conditions": [c.to_dict() for c in self.new_conditions],
                "judgments": [j.to_dict() for j in self.judgments],
                "edits": [e.to_dict() for e in self.edits], "reason": self.reason}

    def to_dict(self):
        return {"registry": self.request.registry.to_dict(), "failed_execution": self.request.failure.to_dict(),
                "response": self.response()}

    @classmethod
    def from_dict(cls, data):
        fields(data, ("registry", "failed_execution", "response"))
        request = PatchRequest(ConditionRegistry.from_dict(data["registry"]), FailedExecution.from_dict(data["failed_execution"]))
        return validate_patch_response(request, data["response"])


def validate_patch_response(request, response):
    fields(response, ("scope", "when", "new_conditions", "judgments", "edits", "reason"))
    for key in ("new_conditions", "judgments", "edits"):
        if type(response[key]) is not list:
            raise ValidationError(f"{key} must be a list")
    scope, when = Expression.from_dict(response["scope"]), Expression.from_dict(response["when"])
    request.registry.validate_expression(scope)
    new = tuple(ConditionDefinition.from_dict(c) for c in response["new_conditions"])
    existing = {c.id for c in request.registry.conditions}
    if len({c.id for c in new}) != len(new) or any(c.id in existing for c in new):
        raise ValidationError("Reuse existing conditions by ID; new_conditions contains only new IDs")
    registry = request.registry
    for condition in new:
        registry = registry.register(condition)
    registry.validate_expression(when)
    if {c.id for c in new} - when.condition_ids:
        raise ValidationError("Do not create unused registry conditions")
    judgments = validate_response(prepare_request(registry, request.failure.context), {"judgments": response["judgments"]})
    edits = tuple(InstructionEdit.from_dict(e) for e in response["edits"])
    if not edits:
        raise ValidationError("A correction needs at least one edit")
    return PatchProposal(request, scope, when, new, judgments, edits, text(response["reason"], "reason"))


class RecordedPatchProvider:
    def __init__(self, recording):
        fields(recording, ("registry", "failed_execution", "response"))
        self._recording = canonical(recording)
        self.invocations = 0

    @classmethod
    def from_file(cls, path):
        return cls(read(path))

    def create(self, request):
        self.invocations += 1
        recording = loads(self._recording)
        if recording["registry"] != request.registry.to_dict() or recording["failed_execution"] != request.failure.to_dict():
            raise ValidationError("Patch recording belongs to a different registry or failed execution")
        return recording["response"]


def create_patch(request, *, provider=None):
    """Replacement for MAP proposal creation. No reducer or second model conversion."""
    if type(provider) is not RecordedPatchProvider:
        raise ModelAccessDisabled("Patch inference is disabled; supply an explicit recorded MAP response")
    return validate_patch_response(request, provider.create(request))
