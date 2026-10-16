"""Shared payload types, results, component interfaces, and named errors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, Sequence, Union

JSONValue = Union[None, bool, int, float, str, list["JSONValue"], dict[str, "JSONValue"]]


class RDRError(Exception):
    """Base class for engine errors."""


class InvalidPayloadError(RDRError, ValueError):
    pass


class EvaluationError(RDRError):
    pass


class RuleAuthorError(RDRError):
    pass


class OracleError(RDRError):
    pass


class InvalidPredictionError(RDRError, ValueError):
    pass


class StalePredictionError(InvalidPredictionError):
    pass


class InvalidFeedbackError(RDRError, ValueError):
    pass


class InvalidTreeError(RDRError, ValueError):
    pass


@dataclass(frozen=True)
class Node:
    id: int
    conditions: tuple[JSONValue, ...]
    conclusion: JSONValue
    evidence: tuple[JSONValue, ...] = ()
    children: tuple[int, ...] = ()
    parent: int | None = None
    # A stopping rule suppresses its parent's conclusion and adds none.
    stopping: bool = False


@dataclass(frozen=True)
class EvaluationStep:
    node_id: int
    matched: bool
    depth: int


@dataclass(frozen=True)
class FiredConclusion:
    node_id: int
    conclusion: JSONValue


@dataclass(frozen=True)
class Prediction:
    case: JSONValue
    # Conclusions of terminal fired, non-stopping nodes, in depth-first preorder.
    conclusions: tuple[FiredConclusion, ...]
    fired: tuple[int, ...]
    trace: tuple[EvaluationStep, ...]
    revision: int
    _owner: object = field(repr=False, compare=False)
    _snapshot: str = field(repr=False, compare=False)

    @property
    def distinct_conclusions(self) -> tuple[JSONValue, ...]:
        """Conclusion values without repeats, in first-occurrence order."""
        from ._json import key

        seen: set[str] = set()
        values: list[JSONValue] = []
        for item in self.conclusions:
            identity = key(item.conclusion)
            if identity not in seen:
                seen.add(identity)
                values.append(item.conclusion)
        return tuple(values)


@dataclass(frozen=True)
class Accept:
    """The oracle ratifies every conclusion of the prediction."""


@dataclass(frozen=True)
class Stop:
    """The conclusion of terminal node node_id is wrong; nothing replaces it."""

    node_id: int


@dataclass(frozen=True)
class Replace:
    """The conclusion of terminal node node_id is wrong; use conclusion (even null)."""

    node_id: int
    conclusion: JSONValue


@dataclass(frozen=True)
class Add:
    """A conclusion is missing; add a rule under fired node parent_id."""

    conclusion: JSONValue
    parent_id: int = 0


Change = Union[Stop, Replace, Add]


@dataclass(frozen=True)
class Revise:
    """Explicit per-conclusion corrections. Unmentioned conclusions are accepted."""

    changes: tuple[Change, ...]


Feedback = Union[Accept, Revise]


@dataclass(frozen=True)
class RuleRequest:
    """What the author is writing a rule for. conclusion is None for a stop."""

    kind: Literal["stop", "replace", "add"]
    parent_id: int
    conclusion: JSONValue


@dataclass(frozen=True)
class UpdateResult:
    # The prediction was made BEFORE this feedback was applied.
    prediction: Prediction
    feedback: Feedback
    status: Literal["accepted", "revised", "unresolved"]
    revision: int
    new_node_ids: tuple[int, ...] = ()
    attempts: tuple[int, ...] = ()
    reason: str | None = None


class ConditionEvaluator(Protocol):
    def evaluate(self, condition: JSONValue, case: JSONValue) -> bool:
        """Evaluate one predicate; return a real bool or raise an exception."""
        ...


class RuleAuthor(Protocol):
    def generate_candidates(
        self, case: JSONValue, cornerstones: Sequence[JSONValue]
    ) -> Sequence[JSONValue]:
        """Propose individual difference conditions."""
        ...

    def select_condition(
        self,
        candidates: Sequence[JSONValue],
        case: JSONValue,
        cornerstones: Sequence[JSONValue],
        request: RuleRequest,
        attempt: int,
    ) -> Sequence[JSONValue]:
        """Return a conjunction, optionally including additional predicates.

        attempt is one-based. The engine validates this proposal independently.
        """
        ...


class Oracle(Protocol):
    def review(self, prediction: Prediction) -> Feedback:
        """Ratify the conclusions or provide per-conclusion corrections."""
        ...
