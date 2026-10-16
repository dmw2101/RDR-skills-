"""Run with python -m rdr_engine.demo. No network, credentials, or datasets."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Sequence

from . import (
    Accept, Add, Feedback, JSONValue, Prediction, RDREngine, Replace, Revise, RuleRequest, Stop,
)


class DeliveryEvaluator:
    def evaluate(self, condition: JSONValue, case: JSONValue) -> bool:
        if not isinstance(condition, dict) or not isinstance(case, dict):
            raise ValueError("The delivery demo expects object predicates and cases")
        return case[condition["field"]] == condition["equals"]


class DeliveryAuthor:
    def generate_candidates(
        self, case: JSONValue, cornerstones: Sequence[JSONValue]
    ) -> list[JSONValue]:
        return [
            {"field": field, "equals": case[field]}
            for field in ("perishable", "fragile", "refrigerated", "packing")
        ]

    def select_condition(
        self,
        candidates: Sequence[JSONValue],
        case: JSONValue,
        cornerstones: Sequence[JSONValue],
        request: RuleRequest,
        attempt: int,
    ) -> list[JSONValue]:
        return list(candidates[:1])


def expected_labels(case: dict[str, JSONValue]) -> set[str]:
    labels = set()
    if case["perishable"]:
        labels.add("cold-chain" if case["refrigerated"] else "express")
    if case["fragile"] and case["packing"] != "crate":
        labels.add("insured")
    return labels or {"standard"}


class DeliveryOracle:
    """Compare label sets and correct each wrong or missing label explicitly."""

    def review(self, prediction: Prediction) -> Feedback:
        expected = expected_labels(prediction.case)
        wrong = [item for item in prediction.conclusions if item.conclusion not in expected]
        given = {item.conclusion for item in prediction.conclusions}
        missing = sorted(expected - given)
        if not wrong and not missing:
            return Accept()
        if len(wrong) == 1 and len(missing) == 1:
            return Revise((Replace(wrong[0].node_id, missing[0]),))
        return Revise(tuple(Stop(item.node_id) for item in wrong) + tuple(Add(label) for label in missing))


def package(
    name: str, *, perishable=False, fragile=False, refrigerated=False, packing="box"
) -> dict[str, JSONValue]:
    return {
        "name": name,
        "perishable": perishable,
        "fragile": fragile,
        "refrigerated": refrigerated,
        "packing": packing,
    }


def labels(prediction: Prediction) -> list[JSONValue]:
    return list(prediction.distinct_conclusions)


def main() -> None:
    evaluator, author, oracle = DeliveryEvaluator(), DeliveryAuthor(), DeliveryOracle()
    engine = RDREngine(evaluator, author, default_conclusion="standard")
    cases = [
        package("books"),
        package("fruit", perishable=True),
        package("glass", fragile=True),
        package("ice cream", perishable=True, refrigerated=True),
        package("eggs", perishable=True, fragile=True),
        package("crated melons", perishable=True, fragile=True, packing="crate"),
    ]
    print("Learning from oracle feedback:")
    for case in cases:
        result = engine.process(case, oracle)
        print(f"  {case['name']}: predicted {labels(result.prediction)}; {result.status}")
        assert set(labels(engine.predict(case))) == expected_labels(case)

    print("\nTree (children fire only when their parent fires):")
    nodes = engine.nodes
    pending = [(0, 0)]
    while pending:
        node_id, depth = pending.pop()
        node = nodes[node_id]
        outcome = "STOP" if node.stopping else node.conclusion
        print(f"  {'  ' * depth}{node.id}: {list(node.conditions) or 'true'} -> {outcome}")
        pending.extend((child, depth + 1) for child in reversed(node.children))

    print("\nNew cases using the learned rules:")
    for case in cases:
        new_case = {**case, "name": f"another {case['name']} package"}
        prediction = engine.predict(new_case)
        assert set(labels(prediction)) == expected_labels(new_case)
        result = engine.learn(prediction, oracle.review(prediction))
        assert result.status == "accepted"
        print(f"  {new_case['name']}: {labels(prediction)}; terminals "
              f"{[item.node_id for item in prediction.conclusions]}; accepted")

    with TemporaryDirectory() as directory:
        path = Path(directory) / "delivery.json"
        engine.save(path)
        restored = RDREngine.load(path, evaluator=evaluator, author=author)
        for case in cases:
            assert restored.predict(case) == engine.predict(case)
        assert restored.to_dict() == engine.to_dict()
    print("\nJSON round trip preserved the tree, evidence, conclusions, and traces.")


if __name__ == "__main__":
    main()
