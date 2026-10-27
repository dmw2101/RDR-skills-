"""Structured verifier output: a diagnosis and its proposed MCRDR change."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from rdr_engine.learned import FEATURE_PREFIX
from rdr_engine.skill_compiler import Predicate, StrictModel


class NewFeature(StrictModel):
    name: str = Field(pattern=FEATURE_PREFIX + r"[a-z0-9_]+$",
                      description="New observable yes/no condition, named lf_<snake_case>.")
    description: str = Field(min_length=1, description="Precise definition, observable before the decision.")
    examples: list[str] = Field(default_factory=list, description="Visible situations where it is true.")
    counterexamples: list[str] = Field(default_factory=list, description="Visible situations where it is false.")


class NewFeatureCondition(StrictModel):
    new_feature: NewFeature


class Proposal(StrictModel):
    """One MCRDR edit. The engine, not the verifier, decides whether it is safe."""

    kind: Literal["add", "replace", "stop"]
    target_node_id: int = Field(ge=0, description=(
        "add: a fired node to add an alternative under (0 = root). "
        "replace/stop: a node whose conclusion was rendered at the failing turn."))
    conditions: list[Predicate | NewFeatureCondition] = Field(min_length=1, description=(
        "Conjunction that distinguishes the failing situation. Existing feature predicates "
        "or new learned features; never the gold answer or the outcome."))
    guidance_text: str | None = Field(default=None, description="New general procedure (Markdown).")
    anchor_fragment_id: str | None = Field(default=None, description="Show guidance after this fragment.")
    remove_fragment_ids: list[str] = Field(default_factory=list,
                                           description="replace only: inherited fragments to drop.")
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def check_kind(self) -> Proposal:
        if self.kind == "add" and not (self.guidance_text or "").strip():
            raise ValueError("add needs guidance_text")
        if self.kind == "replace" and not ((self.guidance_text or "").strip() or self.remove_fragment_ids):
            raise ValueError("replace needs guidance_text or remove_fragment_ids")
        if self.kind == "stop" and (self.guidance_text or self.remove_fragment_ids):
            raise ValueError("stop suppresses a conclusion; it takes no guidance or removals")
        if self.kind != "replace" and self.remove_fragment_ids:
            raise ValueError("remove_fragment_ids applies only to replace")
        return self

    @property
    def new_features(self) -> list[NewFeature]:
        return [c.new_feature for c in self.conditions if isinstance(c, NewFeatureCondition)]

    def predicates(self) -> list[dict]:
        """Engine conditions: a new feature fires only when judged true."""
        return [
            {"field": c.new_feature.name, "op": "eq", "value": "true"}
            if isinstance(c, NewFeatureCondition) else c.model_dump()
            for c in self.conditions
        ]


class Diagnosis(StrictModel):
    attribution: Literal["skill_missing", "skill_misleading", "not_skill", "unexplained"]
    failing_turn: int | None = Field(default=None, ge=0)
    root_cause: str = Field(min_length=1)
    evidence: list[str] = Field(default_factory=list)
    proposal: Proposal | None = None

    @model_validator(mode="after")
    def check_proposal(self) -> Diagnosis:
        skill = self.attribution in ("skill_missing", "skill_misleading")
        if skill and (self.proposal is None or self.failing_turn is None):
            raise ValueError("skill-attributed diagnoses need failing_turn and proposal")
        if not skill and self.proposal is not None:
            raise ValueError("only skill-attributed diagnoses may propose a change")
        return self
