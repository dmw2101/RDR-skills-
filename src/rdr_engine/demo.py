"""Run with python -m rdr_engine.demo. No network, credentials, or datasets."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Sequence

from . import Accept, Correct, Feedback, JSONValue, Prediction, RDREngine


class DeliveryEvaluator:
    def evaluate(self, condition: JSONValue, case: JSONValue) -> bool:
        if not isinstance(condition, dict) or not isinstance(case, dict):
            raise ValueError("The delivery demo expects object predicates and cases")
        return case[condition["field"]] == condition["equals"]


class DeliveryAuthor:
    def generate_candidates(
        self, case: JSONValue, examples: Sequence[JSONValue]
    ) -> list[JSONValue]:
        return [
            {"field": field, "equals": case[field]}
            for field in ("perishable", "fragile", "refrigerated")
        ]

    def select_condition(
        self,
        candidates: Sequence[JSONValue],
        case: JSONValue,
        examples: Sequence[JSONValue],
        conclusion: JSONValue,
        attempt: int,
    ) -> list[JSONValue]:
        return list(candidates[:1])


class DeliveryOracle:
    def review(self, prediction: Prediction) -> Feedback:
        case = prediction.case
        if case["perishable"] and case["refrigerated"]:
            expected = "cold-chain"
        elif case["perishable"]:
            expected = "express"
        elif case["fragile"]:
            expected = "insured"
        else:
            expected = "standard"
        return Accept() if prediction.conclusion == expected else Correct(expected)


def package(name: str, *, perishable=False, fragile=False, refrigerated=False) -> dict[str, JSONValue]:
    return {
        "name": name,
        "perishable": perishable,
        "fragile": fragile,
        "refrigerated": refrigerated,
    }


def main() -> None:
    evaluator, author, oracle = DeliveryEvaluator(), DeliveryAuthor(), DeliveryOracle()
    engine = RDREngine(evaluator, author, default_conclusion="standard")
    cases = [
        (package("books"), "standard"),
        (package("fruit", perishable=True), "express"),
        (package("glass", fragile=True), "insured"),
        (package("ice cream", perishable=True, refrigerated=True), "cold-chain"),
    ]
    print("Learning from oracle feedback:")
    for case, expected in cases:
        result = engine.process(case, oracle)
        print(f"  {case['name']}: predicted {result.prediction.conclusion}; {result.status}")
        assert engine.predict(case).conclusion == expected

    print("\nTree (true = right, false = left):")   
    for node in engine.nodes:
        print(
            f"  {node.id}: {list(node.conditions) or 'true'} -> {node.conclusion}; "
            f"right={node.true_child}, left={node.false_child}"
        )

    print("\nNew cases using the learned rules:")
    for case, expected in cases:
        new_case = {**case, "name": f"another {case['name']} package"}
        prediction = engine.predict(new_case)
        assert prediction.conclusion == expected
        route = " -> ".join(f"{step.node_id}:{step.matched}" for step in prediction.path)
        result = engine.learn(prediction, oracle.review(prediction))
        assert result.status == "accepted"
        print(f"  {new_case['name']}: {prediction.conclusion}; path {route}; accepted")

    with TemporaryDirectory() as directory:
        path = Path(directory) / "delivery.json"
        engine.save(path)
        restored = RDREngine.load(path, evaluator=evaluator, author=author)
        for case, expected in cases:
            assert restored.predict(case).conclusion == expected
            assert restored.predict(case).path == engine.predict(case).path
        assert restored.to_dict() == engine.to_dict()
    print("\nJSON round trip preserved the tree, evidence, predictions, and paths.")


if __name__ == "__main__":
    main()
