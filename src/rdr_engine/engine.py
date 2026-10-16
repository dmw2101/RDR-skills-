
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
    Add,
    ConditionEvaluator,
    EvaluationError,
    EvaluationStep,
    Feedback,
    FiredConclusion,
    InvalidFeedbackError,
    InvalidPayloadError,
    InvalidPredictionError,
    InvalidTreeError,
    JSONValue,
    Node,
    Oracle,
    OracleError,
    Prediction,
    Replace,
    Revise,
    RuleAuthor,
    RuleAuthorError,
    RuleRequest,
    StalePredictionError,
    Stop,
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
    return replace(
        prediction,
        case=clone(prediction.case),
        conclusions=tuple(
            FiredConclusion(item.node_id, clone(item.conclusion)) for item in prediction.conclusions
        ),
    )


def _prediction_key(prediction: Prediction) -> str:
    return key({
        "case": prediction.case,
        "conclusions": [[item.node_id, item.conclusion] for item in prediction.conclusions],
        "fired": list(prediction.fired),
        "trace": [[step.node_id, step.matched, step.depth] for step in prediction.trace],
        "revision": prediction.revision,
    })


def _signature(conclusions: Sequence[FiredConclusion]) -> list[tuple[int, str]]:
    return sorted((item.node_id, key(item.conclusion)) for item in conclusions)


class RDREngine:
    """Synchronous, single-writer multiple-classification RDR (MCRDR).

    Node zero is an unconditional root whose conclusion is the default: it is
    reported only when none of its children fire. All children of a fired node
    are evaluated; the output is the conclusion of every terminal fired node.
    Public nodes and component arguments are snapshots, not mutable tree state.
    """

    SCHEMA_VERSION = 2

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

    def _infer(
        self, nodes: Sequence[Node], case: JSONValue
    ) -> tuple[tuple[FiredConclusion, ...], tuple[int, ...], tuple[EvaluationStep, ...]]:
        """Iterative depth-first MCRDR inference over a node table."""
        trace: list[EvaluationStep] = []
        fired: list[int] = []
        conclusions: list[FiredConclusion] = []
        child_fired: dict[int, bool] = {}
        # (node, depth, exiting): the exit marker runs after all children.
        stack: list[tuple[int, int, bool]] = [(0, 0, False)]
        while stack:
            node_id, depth, exiting = stack.pop()
            node = nodes[node_id]
            if exiting:
                if not child_fired[node_id] and not node.stopping:
                    conclusions.append(FiredConclusion(node_id, clone(node.conclusion)))
                continue
            matched = self._matches(node.conditions, case)
            trace.append(EvaluationStep(node_id, matched, depth))
            if not matched:
                continue
            fired.append(node_id)
            if node.parent is not None:
                child_fired[node.parent] = True
            child_fired[node_id] = False
            stack.append((node_id, depth, True))
            stack.extend((child, depth + 1, False) for child in reversed(node.children))
        return tuple(conclusions), tuple(fired), tuple(trace)

    def predict(self, case: JSONValue) -> Prediction:
        """Return every terminal conclusion and the evaluation trace, without learning."""
        case = clone(case)
        revision = self._revision
        conclusions, fired, trace = self._infer(self._nodes, case)
        if self._revision != revision:
            raise StalePredictionError("The tree changed during condition evaluation")
        prediction = Prediction(
            case=case,
            conclusions=conclusions,
            fired=fired,
            trace=trace,
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

    def _terminals(self, fired: Sequence[int]) -> list[int]:
        """Fired nodes with no fired child, including silent stopping nodes."""
        active = set(fired)
        return [
            node_id for node_id in fired
            if not any(child in active for child in self._nodes[node_id].children)
        ]

    @staticmethod
    def _with_evidence(nodes: list[Node], node_ids: Sequence[int], case: JSONValue) -> bool:
        changed = False
        identity = key(case)
        for node_id in node_ids:
            node = nodes[node_id]
            if not any(key(example) == identity for example in node.evidence):
                nodes[node_id] = replace(node, evidence=node.evidence + (clone(case),))
                changed = True
        return changed

    def learn(self, prediction: Prediction, feedback: Feedback) -> UpdateResult:
        """Apply explicit feedback to a prediction from the current tree revision.

        Exceptions and unresolved proposals leave the tree unchanged. Callbacks
        must not re-enter the engine or mutate it from another thread.
        """
        self._check_prediction(prediction)
        prediction = _copy_prediction(prediction)
        terminals = self._terminals(prediction.fired)
        if isinstance(feedback, Accept):
            updated = list(self._nodes)
            if self._with_evidence(updated, terminals, prediction.case):
                self._nodes = updated
                self._revision += 1
            return UpdateResult(prediction, Accept(), "accepted", self._revision)
        if not isinstance(feedback, Revise):
            raise TypeError("Feedback must be Accept() or Revise(changes)")
        revise = self._validated_revise(prediction, feedback, terminals)
        if self._author is None:
            raise RuleAuthorError("Revision requires a rule author")

        # Every distinct stored case, with its current output. Cornerstones for
        # a new child of P are the stored cases that fire P, wherever stored.
        stored: dict[str, tuple[JSONValue, frozenset[int], list[tuple[int, str]]]] = {}
        for node in self._nodes:
            for example in node.evidence:
                identity = key(example)
                if identity not in stored:
                    conclusions, fired, _ = self._infer(self._nodes, example)
                    stored[identity] = (example, frozenset(fired), _signature(conclusions))
        case_key = key(prediction.case)

        resolved: list[tuple[Stop | Replace | Add, list[JSONValue]]] = []
        attempts: list[int] = []
        for index, change in enumerate(revise.changes, start=1):
            if isinstance(change, Add):
                request = RuleRequest("add", change.parent_id, clone(change.conclusion))
            elif isinstance(change, Replace):
                request = RuleRequest("replace", change.node_id, clone(change.conclusion))
            else:
                request = RuleRequest("stop", change.node_id, None)
            cornerstones = [
                example for identity, (example, fired, _) in stored.items()
                if request.parent_id in fired and identity != case_key
            ]
            if case_key in stored and request.parent_id in stored[case_key][1]:
                attempts.append(0)
                return UpdateResult(
                    prediction, revise, "unresolved", self._revision, attempts=tuple(attempts),
                    reason=f"Change {index}: the case is already stored with a conflicting accepted output",
                )
            conditions, used, reason = self._author_rule(prediction.case, cornerstones, request)
            attempts.append(used)
            if conditions is None:
                return UpdateResult(
                    prediction, revise, "unresolved", self._revision, attempts=tuple(attempts),
                    reason=f"Change {index}: {reason}",
                )
            resolved.append((change, conditions))

        self._check_prediction(prediction)
        # Stage every new node; commit only after the regression checks pass.
        staged = list(self._nodes)
        new_ids: list[int] = []
        for change, conditions in resolved:
            parent_id = change.parent_id if isinstance(change, Add) else change.node_id
            new_id = len(staged)
            stopping = isinstance(change, Stop)
            staged.append(Node(
                new_id,
                tuple(clone(c) for c in conditions),
                None if stopping else clone(change.conclusion),
                (clone(prediction.case),),
                parent=parent_id,
                stopping=stopping,
            ))
            parent = staged[parent_id]
            staged[parent_id] = replace(parent, children=parent.children + (new_id,))
            new_ids.append(new_id)
        targeted = {change.node_id for change, _ in resolved if not isinstance(change, Add)}
        parents = {staged[node_id].parent for node_id in new_ids}
        self._with_evidence(
            staged, [node_id for node_id in terminals if node_id not in targeted], prediction.case
        )

        # A new child of a terminal node suppresses that node's conclusion.
        expected = [
            item for item in prediction.conclusions
            if item.node_id not in targeted and item.node_id not in parents
        ] + [
            FiredConclusion(node_id, staged[node_id].conclusion)
            for node_id in new_ids if not staged[node_id].stopping
        ]
        new_conclusions, _, _ = self._infer(staged, prediction.case)
        if _signature(new_conclusions) != _signature(expected):
            return UpdateResult(
                prediction, revise, "unresolved", self._revision, attempts=tuple(attempts),
                reason="The revised tree does not give the requested conclusions for this case",
            )
        for identity, (example, _, signature) in stored.items():
            if identity == case_key:
                continue
            conclusions, _, _ = self._infer(staged, example)
            if _signature(conclusions) != signature:
                return UpdateResult(
                    prediction, revise, "unresolved", self._revision, attempts=tuple(attempts),
                    reason="The revision would change the output for a stored case",
                )
        self._nodes = staged
        self._revision += 1
        return UpdateResult(
            prediction, revise, "revised", self._revision,
            new_node_ids=tuple(new_ids), attempts=tuple(attempts),
        )

    def _validated_revise(
        self, prediction: Prediction, feedback: Revise, terminals: Sequence[int]
    ) -> Revise:
        changes = feedback.changes
        if not isinstance(changes, Sequence) or isinstance(changes, (str, bytes, bytearray)):
            raise InvalidFeedbackError("Revise.changes must be a sequence of changes")
        if not changes:
            raise InvalidFeedbackError("Revise needs at least one change; use Accept() otherwise")
        concluding = {item.node_id for item in prediction.conclusions}
        terminal = set(terminals)
        fired = set(prediction.fired)
        targets: set[int] = set()
        copied: list[Stop | Replace | Add] = []
        for change in changes:
            if not isinstance(change, (Stop, Replace, Add)):
                raise TypeError("Each change must be Stop, Replace, or Add")
            try:
                if isinstance(change, Add):
                    parent = change.parent_id
                    if type(parent) is not int or parent not in fired:
                        raise InvalidFeedbackError("Add.parent_id must be a node that fired for this case")
                    node = self._nodes[parent]
                    if parent != 0 and parent in terminal and not node.stopping:
                        raise InvalidFeedbackError(
                            "Adding under a terminal node would replace its conclusion; use Replace"
                        )
                    copied.append(Add(clone(change.conclusion), parent))
                    continue
                if type(change.node_id) is not int or change.node_id not in concluding:
                    raise InvalidFeedbackError("Stop and Replace must target a reported conclusion")
                if change.node_id in targets:
                    raise InvalidFeedbackError("A conclusion can be corrected only once per revision")
                targets.add(change.node_id)
                copied.append(
                    Stop(change.node_id) if isinstance(change, Stop)
                    else Replace(change.node_id, clone(change.conclusion))
                )
            except InvalidPayloadError as error:
                raise InvalidFeedbackError("Corrected conclusions must be JSON values") from error
        return Revise(tuple(copied))

    def _author_rule(
        self, case: JSONValue, cornerstones: list[JSONValue], request: RuleRequest
    ) -> tuple[list[JSONValue] | None, int, str]:
        candidates = self._call_author(
            "generate_candidates", clone(case), [clone(x) for x in cornerstones]
        )
        # Algorithm 5 filters EACH candidate, not merely their conjunction.
        differences = [
            condition for condition in candidates
            if self._matches([condition], case)
            and not any(self._matches([condition], example) for example in cornerstones)
        ]
        reason = "No valid condition was selected"
        for attempt in range(1, self._max_selection_attempts + 1):
            conditions = self._call_author(
                "select_condition",
                [clone(c) for c in differences],
                clone(case),
                [clone(x) for x in cornerstones],
                replace(request, conclusion=clone(request.conclusion)),
                attempt,
            )
            if not self._matches(conditions, case):
                reason = "Proposed conjunction does not match the new case"
                continue
            if any(self._matches(conditions, example) for example in cornerstones):
                reason = "Proposed conjunction matches a cornerstone case"
                continue
            return conditions, attempt, ""
        return None, self._max_selection_attempts, reason

    def _call_author(self, method: str, *args: object) -> list[JSONValue]:
        try:
            values = getattr(self._author, method)(*args)
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
                raise TypeError("Rule author must return a sequence of individual conditions")
            return [clone(value) for value in values]
        except Exception as error:
            raise RuleAuthorError(f"Rule author {method} failed") from error

    def process(self, case: JSONValue, oracle: Oracle) -> UpdateResult:
        """Convenience wrapper for predict -> oracle -> learn."""
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
                    "stopping": node.stopping,
                    "evidence": [clone(x) for x in node.evidence],
                    "children": list(node.children),
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
            if isinstance(data, dict) and data.get("schema_version") == 1:
                raise InvalidTreeError(
                    "Schema version 1 is a single-classification snapshot; rebuild it as schema 2"
                )
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
            node_fields = {"id", "conditions", "conclusion", "stopping", "evidence", "children"}
            parents: dict[int, int] = {}
            for index, raw in enumerate(raw_nodes):
                if not isinstance(raw, dict) or set(raw) != node_fields:
                    raise InvalidTreeError("Invalid node fields")
                if type(raw["id"]) is not int or raw["id"] != index:
                    raise InvalidTreeError("Node IDs must be contiguous and ordered from zero")
                if not isinstance(raw["conditions"], list) or not isinstance(raw["evidence"], list):
                    raise InvalidTreeError("Conditions and evidence must be arrays")
                if type(raw["stopping"]) is not bool:
                    raise InvalidTreeError("stopping must be a Boolean")
                if raw["stopping"] and raw["conclusion"] is not None:
                    raise InvalidTreeError("A stopping node must have a null conclusion")
                children = raw["children"]
                if not isinstance(children, list):
                    raise InvalidTreeError("children must be an array")
                if len(set(c for c in children if type(c) is int)) != len(children):
                    raise InvalidTreeError("Invalid or duplicate child ID")
                for child in children:
                    if not 0 <= child < len(raw_nodes):
                        raise InvalidTreeError("Invalid child ID")
                    if child == 0 or child == index or child in parents:
                        raise InvalidTreeError("Cycles and shared children are not trees")
                    parents[child] = index
            nodes = [
                Node(
                    index, tuple(raw["conditions"]), raw["conclusion"], tuple(raw["evidence"]),
                    children=tuple(raw["children"]), parent=parents.get(index),
                    stopping=raw["stopping"],
                )
                for index, raw in enumerate(raw_nodes)
            ]
            if nodes[0].conditions or nodes[0].stopping:
                raise InvalidTreeError("The root must be unconditional and not a stopping rule")
            seen: set[int] = set()
            pending = [0]
            while pending:
                node_id = pending.pop()
                if node_id in seen:
                    raise InvalidTreeError("Cycles and shared children are not trees")
                seen.add(node_id)
                pending.extend(nodes[node_id].children)
            if len(seen) != len(nodes):
                raise InvalidTreeError("Tree contains unreachable nodes or cycles")
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
