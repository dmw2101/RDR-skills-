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


class InvalidTreeError(RDRError, ValueError):
    pass


@dataclass(frozen=True)
class Node:
    id: int
    conditions: tuple[JSONValue, ...]
    conclusion: JSONValue
    evidence: tuple[JSONValue, ...] = ()
    true_child: int | None = None
    false_child: int | None = None


@dataclass(frozen=True)
class EvaluationStep:
    node_id: int
    matched: bool


@dataclass(frozen=True)
class Prediction:
    case: JSONValue
    conclusion: JSONValue
    last_visited_id: int
    last_true_id: int
    path: tuple[EvaluationStep, ...]
    revision: int
    _owner: object = field(repr=False, compare=False)
    _snapshot: str = field(repr=False, compare=False)


@dataclass(frozen=True)
class Accept:
    """The oracle ratifies the prediction."""


@dataclass(frozen=True)
class Correct:
    """The oracle refutes the prediction and supplies a replacement (even null)."""

    conclusion: JSONValue


Feedback = Union[Accept, Correct]


@dataclass(frozen=True)
class UpdateResult:
    # The prediction was made BEFORE this feedback was applied.
    prediction: Prediction
    feedback: Feedback
    status: Literal["accepted", "revised", "unresolved"]
    revision: int
    new_node_id: int | None = None
    attempts: int = 0
    reason: str | None = None


class ConditionEvaluator(Protocol):
    def evaluate(self, condition: JSONValue, case: JSONValue) -> bool:
        """Evaluate one predicate; return a real bool or raise an exception."""
        ...


class RuleAuthor(Protocol):
    def generate_candidates(
        self, case: JSONValue, examples: Sequence[JSONValue]
    ) -> Sequence[JSONValue]:
        """Propose individual difference conditions."""
        ...

    def select_condition(
        self,
        candidates: Sequence[JSONValue],
        case: JSONValue,
        examples: Sequence[JSONValue],
        conclusion: JSONValue,
        attempt: int,
    ) -> Sequence[JSONValue]:
        """Return a conjunction, optionally including additional predicates.

        attempt is one-based. The engine validates this proposal independently.
        """
        ...


class Oracle(Protocol):
    def review(self, prediction: Prediction) -> Feedback:
        """Ratify the conclusion or provide a correction."""
        ...
