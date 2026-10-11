
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from ._json import clone, key
from .types import (
    Accept,
    ConditionEvaluator,
    Correct,
    EvaluationError,
    EvaluationStep,
    Feedback,
    InvalidPayloadError,
    InvalidPredictionError,
    InvalidTreeError,
    JSONValue,
    Node,
    Oracle,
    OracleError,
    Prediction,
    RuleAuthor,
    RuleAuthorError,
    StalePredictionError,
    UpdateResult,
)


def _copy_node(node: Node) -> Node:
    return replace(
        node,
        conditions=tuple(clone(c) for c in node.conditions),
        conclusion=clone(node.conclusion),
        evidence=tuple(clone(x) for x in node.evidence),
    )


def _copy_prediction(prediction: Prediction) -> Prediction:
    return replace(prediction, case=clone(prediction.case), conclusion=clone(prediction.conclusion))


def _prediction_key(prediction: Prediction) -> str:
    return key({
        "case": prediction.case,
        "conclusion": prediction.conclusion,
        "last_visited_id": prediction.last_visited_id,
        "last_true_id": prediction.last_true_id,
        "path": [[step.node_id, step.matched] for step in prediction.path],
        "revision": prediction.revision,
    })


class RDREngine:
    """Synchronous, single-writer RDR with injected domain components.

    Empty conjunctions are true, making node zero an unconditional root.
    Public nodes and component arguments are snapshots, not mutable tree state.
    """

    SCHEMA_VERSION = 1

    def __init__(
        self,
        evaluator: ConditionEvaluator,
        author: RuleAuthor | None = None,
        *,
        default_conclusion: JSONValue = True,
        max_selection_attempts: int = 3,
    ) -> None:
        if type(max_selection_attempts) is not int or max_selection_attempts < 1:
            raise ValueError("max_selection_attempts must be a positive integer")
        self._evaluator = evaluator
        self._author = author
        self._max_selection_attempts = max_selection_attempts
        self._nodes = [Node(0, (), clone(default_conclusion))]
        self._revision = 0
        self._owner = object()

    @property
    def nodes(self) -> tuple[Node, ...]:
        return tuple(_copy_node(node) for node in self._nodes)

    @property
    def revision(self) -> int:
        return self._revision

    def _matches(self, conditions: Sequence[JSONValue], case: JSONValue) -> bool:
        for condition in conditions:
            try:
                matched = self._evaluator.evaluate(clone(condition), clone(case))
            except Exception as error:
                raise EvaluationError("Condition evaluator raised an exception") from error
            if type(matched) is not bool:
                raise EvaluationError("Condition evaluator must return bool, not an uncertain value")
            if not matched:
                return False
        return True

    def predict(self, case: JSONValue) -> Prediction:
        """Return the last true conclusion and the path, without learning."""
        case = clone(case)
        revision = self._revision
        current = 0
        last_true = 0
        path: list[EvaluationStep] = []
        while True:
            node = self._nodes[current]
            matched = self._matches(node.conditions, case)
            path.append(EvaluationStep(current, matched))
            if matched:
                last_true = current
            child = node.true_child if matched else node.false_child
            if child is None:
                break
            current = child
        if self._revision != revision:
            raise StalePredictionError("The tree changed during condition evaluation")
        prediction = Prediction(
            case=case,
            conclusion=clone(self._nodes[last_true].conclusion),
            last_visited_id=current,
            last_true_id=last_true,
            path=tuple(path),
            revision=revision,
            _owner=self._owner,
            _snapshot="",
        )
        return replace(prediction, _snapshot=_prediction_key(prediction))

    def _check_prediction(self, prediction: Prediction) -> None:
        if not isinstance(prediction, Prediction) or prediction._owner is not self._owner:
            raise InvalidPredictionError("Prediction must come from this engine instance")
        if prediction.revision != self._revision:
            raise StalePredictionError("Predict again and obtain fresh feedback after a tree update")
        try:
            unchanged = prediction._snapshot == _prediction_key(prediction)
        except (InvalidPayloadError, AttributeError, TypeError) as error:
            raise InvalidPredictionError("Prediction was modified after traversal") from error
        if not unchanged:
            raise InvalidPredictionError("Prediction was modified after traversal")

    def learn(self, prediction: Prediction, feedback: Feedback) -> UpdateResult:
        """Apply explicit feedback to a prediction from the current tree revision.

        Exceptions and unresolved proposals leave the tree unchanged. Callbacks
        must not re-enter the engine or mutate it from another thread.
        """
        self._check_prediction(prediction)
        prediction = _copy_prediction(prediction)
        winner = self._nodes[prediction.last_true_id]
        if isinstance(feedback, Accept):
            if not any(key(example) == key(prediction.case) for example in winner.evidence):
                self._nodes[winner.id] = replace(
                    winner, evidence=winner.evidence + (clone(prediction.case),)
                )
                self._revision += 1
            return UpdateResult(prediction, Accept(), "accepted", self._revision)
        if not isinstance(feedback, Correct):
            raise TypeError("Feedback must be Accept() or Correct(replacement_conclusion)")
        correction = Correct(clone(feedback.conclusion))
        if self._author is None:
            raise RuleAuthorError("Refutation requires a rule author")

        candidates = self._call_author(
            "generate_candidates", clone(prediction.case), [clone(x) for x in winner.evidence]
        )
        # Algorithm 5 filters EACH candidate, not merely their conjunction.
        differences = [
            condition for condition in candidates
            if self._matches([condition], prediction.case)
            and not any(self._matches([condition], example) for example in winner.evidence)
        ]
        reason = "No valid condition was selected"
        for attempt in range(1, self._max_selection_attempts + 1):
            conditions = self._call_author(
                "select_condition",
                [clone(c) for c in differences],
                clone(prediction.case),
                [clone(x) for x in winner.evidence],
                clone(correction.conclusion),
                attempt,
            )
            if not self._matches(conditions, prediction.case):
                reason = "Proposed conjunction does not match the new case"
                continue
            if any(self._matches(conditions, example) for example in winner.evidence):
                reason = "Proposed conjunction matches stored evidence at the winning node"
                continue

            self._check_prediction(prediction)
            parent_id = prediction.last_visited_id
            parent = self._nodes[parent_id]
            branch = "true_child" if parent_id == winner.id else "false_child"
            if getattr(parent, branch) is not None:
                raise InvalidPredictionError("The insertion branch is no longer empty")
            new_id = len(self._nodes)
            new_node = Node(
                new_id,
                tuple(clone(c) for c in conditions),
                clone(correction.conclusion),
                (clone(prediction.case),),
            )
            # Commit only after all authoring and consistency checks succeed.
            updated = list(self._nodes)
            updated[parent_id] = replace(parent, **{branch: new_id})
            updated.append(new_node)
            self._nodes = updated
            self._revision += 1
            return UpdateResult(
                prediction, correction, "revised", self._revision,
                new_node_id=new_id, attempts=attempt,
            )
        return UpdateResult(
            prediction, correction, "unresolved", self._revision,
            attempts=self._max_selection_attempts, reason=reason,
        )       

    def _call_author(self, method: str, *args: object) -> list[JSONValue]:
        try:
            values = getattr(self._author, method)(*args)
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
                raise TypeError("Rule author must return a sequence of individual conditions")
            return [clone(value) for value in values]
        except Exception as error:
            raise RuleAuthorError(f"Rule author {method} failed") from error

    def process(self, case: JSONValue, oracle: Oracle) -> UpdateResult:
        """Convenience wrapper for predict -> oracle -> learn (Algorithm 1)."""
        prediction = self.predict(case)
        try:
            feedback = oracle.review(_copy_prediction(prediction))
        except Exception as error:
            raise OracleError("Oracle raised an exception") from error
        return self.learn(prediction, feedback)

    def to_dict(self) -> dict[str, JSONValue]:
        """Return a JSON-compatible snapshot; extension components are excluded."""
        return {
            "schema_version": self.SCHEMA_VERSION,
            "revision": self._revision,
            "max_selection_attempts": self._max_selection_attempts,
            "root_id": 0,
            "nodes": [
                {
                    "id": node.id,
                    "conditions": [clone(c) for c in node.conditions],
                    "conclusion": clone(node.conclusion),
                    "evidence": [clone(x) for x in node.evidence],
                    "true_child": node.true_child,
                    "false_child": node.false_child,
                }
                for node in self._nodes
            ],
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, JSONValue],
        *,
        evaluator: ConditionEvaluator,
        author: RuleAuthor | None = None,
    ) -> RDREngine:
        """Load and validate a tree, supplying its execution components anew."""
        try:
            data = clone(data)
            fields = {"schema_version", "revision", "max_selection_attempts", "root_id", "nodes"}
            if not isinstance(data, dict) or set(data) != fields:
                raise InvalidTreeError("Invalid snapshot fields")
            if type(data["schema_version"]) is not int or data["schema_version"] != cls.SCHEMA_VERSION:
                raise InvalidTreeError("Unsupported schema version")
            if type(data["root_id"]) is not int or data["root_id"] != 0:
                raise InvalidTreeError("The root ID must be zero")
            revision = data["revision"]
            attempts = data["max_selection_attempts"]
            if type(revision) is not int or revision < 0:
                raise InvalidTreeError("Revision must be a nonnegative integer")
            if type(attempts) is not int or attempts < 1:
                raise InvalidTreeError("Selection attempts must be a positive integer")
            raw_nodes = data["nodes"]
            if not isinstance(raw_nodes, list) or not raw_nodes:
                raise InvalidTreeError("A tree must contain a root node")
            nodes = []
            node_fields = {"id", "conditions", "conclusion", "evidence", "true_child", "false_child"}
            for index, raw in enumerate(raw_nodes):
                if not isinstance(raw, dict) or set(raw) != node_fields:
                    raise InvalidTreeError("Invalid node fields")
                if type(raw["id"]) is not int or raw["id"] != index:
                    raise InvalidTreeError("Node IDs must be contiguous and ordered from zero")
                if not isinstance(raw["conditions"], list) or not isinstance(raw["evidence"], list):
                    raise InvalidTreeError("Conditions and evidence must be arrays")
                for child in (raw["true_child"], raw["false_child"]):
                    if child is not None and (type(child) is not int or not 0 <= child < len(raw_nodes)):
                        raise InvalidTreeError("Invalid child ID")
                nodes.append(Node(
                    index, tuple(raw["conditions"]), raw["conclusion"], tuple(raw["evidence"]),
                    true_child=raw["true_child"], false_child=raw["false_child"],
                ))
            if nodes[0].conditions or nodes[0].false_child is not None:
                raise InvalidTreeError("The root must be unconditional with no false child")
            seen: set[int] = set()
            pending = [0]
            while pending:
                node_id = pending.pop()
                if node_id in seen:
                    raise InvalidTreeError("Cycles and shared children are not trees")
                seen.add(node_id)
                node = nodes[node_id]
                pending.extend(child for child in (node.true_child, node.false_child) if child is not None)
            if len(seen) != len(nodes):
                raise InvalidTreeError("Tree contains unreachable nodes")
        except InvalidPayloadError as error:
            raise InvalidTreeError("Snapshot contains invalid JSON payloads") from error
        engine = cls(evaluator, author, max_selection_attempts=attempts)
        engine._nodes = nodes
        engine._revision = revision
        return engine

    def save(self, path: str | Path) -> None:
        """Atomically write JSON to a file in an existing directory."""
        target = Path(path)
        serialized = json.dumps(self.to_dict(), indent=2, ensure_ascii=True, allow_nan=False) + "\n"
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", dir=target.parent,
                prefix=f".{target.name}.", suffix=".tmp", delete=False,
            ) as stream:
                temporary = stream.name
                stream.write(serialized)
            os.replace(temporary, target)
            temporary = None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    @classmethod
    def load(
        cls, path: str | Path, *, evaluator: ConditionEvaluator, author: RuleAuthor | None = None
    ) -> RDREngine:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise InvalidTreeError("Invalid JSON snapshot") from error
        return cls.from_dict(data, evaluator=evaluator, author=author)
