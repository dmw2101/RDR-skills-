from __future__ import annotations

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from rdr_engine import (
    Accept,
    Correct,
    EvaluationError,
    InvalidPayloadError,
    InvalidPredictionError,
    InvalidTreeError,
    OracleError,
    RDREngine,
    RuleAuthorError,
    StalePredictionError,
)
from rdr_engine.demo import DeliveryAuthor, DeliveryEvaluator, DeliveryOracle, package


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.evaluator = DeliveryEvaluator()
        self.author = DeliveryAuthor()
        self.oracle = DeliveryOracle()
        self.engine = RDREngine(self.evaluator, self.author, default_conclusion="standard")
        self.books = package("books")
        self.fruit = package("fruit", perishable=True)
        self.glass = package("glass", fragile=True)
        self.ice = package("ice cream", perishable=True, refrigerated=True)

    def trained(self):
        for case in (self.books, self.fruit, self.glass, self.ice):
            self.engine.process(case, self.oracle)
        return self.engine

    def assertUnchanged(self, before):
        self.assertEqual(self.engine.to_dict(), before)

    def test_dummy_root_is_true_without_calling_evaluator(self):
        evaluator = Mock()
        evaluator.evaluate.side_effect = AssertionError("Root should be unconditional")
        engine = RDREngine(evaluator)
        before = engine.to_dict()
        result = engine.predict({"any": [1, None]})
        self.assertIs(result.conclusion, True)
        self.assertEqual((result.last_visited_id, result.last_true_id), (0, 0))
        self.assertEqual(engine.to_dict(), before)
        evaluator.evaluate.assert_not_called()

    def test_refutations_create_right_left_and_nested_branches(self):
        engine = self.trained()
        nodes = engine.nodes
        self.assertEqual(len(nodes), 4)
        self.assertEqual(nodes[0].true_child, 1)
        self.assertEqual(nodes[1].false_child, 2)
        self.assertEqual(nodes[1].true_child, 3)
        for case, expected in [(self.books, "standard"), (self.fruit, "express"),
                               (self.glass, "insured"), (self.ice, "cold-chain")]:
            self.assertEqual(engine.predict(case).conclusion, expected)

    def test_terminal_false_returns_last_true_not_terminal_conclusion(self):
        engine = self.trained()
        result = engine.predict(self.books)
        self.assertEqual(result.conclusion, "standard")
        self.assertEqual((result.last_visited_id, result.last_true_id), (2, 0))
        self.assertEqual([(step.node_id, step.matched) for step in result.path],
                         [(0, True), (1, False), (2, False)])
        result = engine.predict(self.fruit)
        self.assertEqual(result.conclusion, "express")
        self.assertEqual((result.last_visited_id, result.last_true_id), (3, 1))

    def test_acceptance_stores_at_winner_not_last_visited(self):
        engine = self.trained()
        case = package("more fruit", perishable=True)
        count = len(engine.nodes)
        engine.learn(engine.predict(case), Accept())
        self.assertIn(case, engine.nodes[1].evidence)
        self.assertNotIn(case, engine.nodes[3].evidence)
        self.assertEqual(len(engine.nodes), count)

    def test_acceptance_is_set_like_for_identical_json_examples(self):
        self.engine.process(self.books, self.oracle)
        revision = self.engine.revision
        self.engine.process(dict(reversed(list(self.books.items()))), self.oracle)
        self.assertEqual(self.engine.revision, revision)
        self.assertEqual(len(self.engine.nodes[0].evidence), 1)

    def test_prediction_result_keeps_pre_correction_conclusion(self):
        result = self.engine.process(self.fruit, self.oracle)
        self.assertEqual(result.prediction.conclusion, "standard")
        self.assertEqual(result.feedback.conclusion, "express")
        self.assertEqual(self.engine.predict(self.fruit).conclusion, "express")

    def test_stored_cases_keep_conclusions_after_every_insertion(self):
        seen = []
        for case, expected in [(self.books, "standard"), (self.fruit, "express"),
                               (self.glass, "insured"), (self.ice, "cold-chain")]:
            self.engine.process(case, self.oracle)
            seen.append((case, expected))
            for old_case, old_conclusion in seen:
                self.assertEqual(self.engine.predict(old_case).conclusion, old_conclusion)

    def test_only_individually_distinguishing_candidates_reach_selector(self):
        author = Mock()
        author.generate_candidates.return_value = [
            {"field": "perishable", "equals": True},   # true new, false old
            {"field": "fragile", "equals": False},      # true both
            {"field": "refrigerated", "equals": True},  # false new
        ]
        author.select_condition.side_effect = lambda candidates, *args: candidates
        engine = RDREngine(self.evaluator, author, default_conclusion="standard")
        engine.learn(engine.predict(self.books), Accept())
        engine.learn(engine.predict(self.fruit), Correct("express"))
        self.assertEqual(author.select_condition.call_args.args[0],
                         [{"field": "perishable", "equals": True}])

    def test_additional_conditions_can_form_a_discriminating_conjunction(self):
        # Neither a nor b individually excludes every old case. Their AND does.
        author = Mock()
        author.generate_candidates.return_value = [
            {"field": "a", "equals": True}, {"field": "b", "equals": True}
        ]
        author.select_condition.return_value = author.generate_candidates.return_value
        engine = RDREngine(self.evaluator, author, default_conclusion="old")
        engine.learn(engine.predict({"a": True, "b": False}), Accept())
        engine.learn(engine.predict({"a": False, "b": True}), Accept())
        result = engine.learn(engine.predict({"a": True, "b": True}), Correct("new"))
        self.assertEqual(author.select_condition.call_args.args[0], [])
        self.assertEqual(result.status, "revised")
        self.assertEqual(engine.predict({"a": True, "b": True}).conclusion, "new")
        self.assertEqual(engine.predict({"a": True, "b": False}).conclusion, "old")
        self.assertEqual(engine.predict({"a": False, "b": True}).conclusion, "old")

    def test_invalid_proposals_retry_then_accept(self):
        author = Mock()
        author.generate_candidates.return_value = []
        author.select_condition.side_effect = [
            [{"field": "perishable", "equals": False}],  # misses new case
            [],                                            # matches old case
            [{"field": "perishable", "equals": True}],
        ]
        engine = RDREngine(self.evaluator, author)
        engine.learn(engine.predict(self.books), Accept())
        result = engine.learn(engine.predict(self.fruit), Correct("express"))
        self.assertEqual(result.attempts, 3)
        self.assertEqual(result.status, "revised")
        self.assertEqual([call.args[-1] for call in author.select_condition.call_args_list], [1, 2, 3])

    def test_conflicting_identical_case_exhausts_attempts_without_mutation(self):
        self.engine.learn(self.engine.predict(self.books), Accept())
        before = self.engine.to_dict()
        result = self.engine.learn(self.engine.predict(self.books), Correct("contradiction"))
        self.assertEqual(result.status, "unresolved")
        self.assertEqual(result.attempts, 3)
        self.assertIn("stored evidence", result.reason)
        self.assertUnchanged(before)

    def test_empty_conjunction_allowed_when_no_evidence_to_protect(self):
        author = Mock()
        author.generate_candidates.return_value = []
        author.select_condition.return_value = []
        engine = RDREngine(self.evaluator, author)
        result = engine.learn(engine.predict(self.books), Correct(None))
        self.assertEqual(result.status, "revised")
        self.assertIsNone(engine.predict(self.fruit).conclusion)

    def test_explicit_feedback_does_not_depend_on_conclusion_equality(self):
        result = self.engine.learn(self.engine.predict(self.fruit), Correct("standard"))
        self.assertEqual(result.status, "revised")
        self.assertEqual(len(self.engine.nodes), 2)

    def test_stale_feedback_rejected_after_evidence_change_or_insertion(self):
        for other_case in (self.books, self.fruit):
            with self.subTest(other_case=other_case):
                engine = RDREngine(self.evaluator, self.author, default_conclusion="standard")
                old = engine.predict(self.glass)
                engine.process(other_case, self.oracle)
                before = engine.to_dict()
                with self.assertRaises(StalePredictionError):
                    engine.learn(old, Correct("insured"))
                self.assertEqual(engine.to_dict(), before)

    def test_foreign_and_modified_predictions_rejected(self):
        foreign = RDREngine(self.evaluator).predict(self.books)
        with self.assertRaises(InvalidPredictionError):
            self.engine.learn(foreign, Accept())
        prediction = self.engine.predict(self.books)
        prediction.case["fragile"] = True
        with self.assertRaises(InvalidPredictionError):
            self.engine.learn(prediction, Accept())
        prediction = self.engine.predict(self.books)
        with self.assertRaises(InvalidPredictionError):
            self.engine.learn(replace(prediction, last_true_id=123), Accept())

    def test_external_payload_mutations_cannot_change_tree(self):
        conclusion = {"skill": "original", "patches": []}
        engine = RDREngine(self.evaluator, self.author, default_conclusion=conclusion)
        conclusion["skill"] = "changed"
        case = {"value": [1]}
        prediction = engine.predict(case)
        case["value"].append(2)
        engine.learn(prediction, Accept())
        prediction.case["value"].append(3)
        nodes = engine.nodes
        nodes[0].conclusion["patches"].append("unexpected")
        nodes[0].evidence[0]["value"].append(4)
        self.assertEqual(engine.nodes[0].conclusion, {"skill": "original", "patches": []})
        self.assertEqual(engine.nodes[0].evidence, ({"value": [1]},))

    def test_evaluator_errors_and_non_booleans_fail_without_mutation(self):
        engine = self.trained()
        for outcome in (None, "true", 1, RuntimeError("offline")):
            with self.subTest(outcome=outcome):
                evaluator = Mock()
                if isinstance(outcome, Exception):
                    evaluator.evaluate.side_effect = outcome
                else:
                    evaluator.evaluate.return_value = outcome
                other = RDREngine.from_dict(engine.to_dict(), evaluator=evaluator, author=self.author)
                before = other.to_dict()
                with self.assertRaises(EvaluationError):
                    other.predict(self.fruit)
                self.assertEqual(other.to_dict(), before)

    def test_revision_evaluation_error_leaves_tree_unchanged(self):
        evaluator = Mock()
        evaluator.evaluate.return_value = None
        engine = RDREngine(evaluator, self.author)
        prediction = engine.predict(self.books)
        before = engine.to_dict()
        with self.assertRaises(EvaluationError):
            engine.learn(prediction, Correct("new"))
        self.assertEqual(engine.to_dict(), before)

    def test_final_validation_evaluator_failure_leaves_tree_unchanged(self):
        author = Mock()
        author.generate_candidates.return_value = []
        author.select_condition.return_value = ["proposed condition"]
        for outcome in (None, RuntimeError("cannot evaluate")):
            with self.subTest(outcome=outcome):
                evaluator = Mock()
                if isinstance(outcome, Exception):
                    evaluator.evaluate.side_effect = outcome
                else:
                    evaluator.evaluate.return_value = outcome
                engine = RDREngine(evaluator, author)
                before = engine.to_dict()
                with self.assertRaises(EvaluationError):
                    engine.learn(engine.predict({}), Correct("new"))
                self.assertEqual(engine.to_dict(), before)

    def test_author_errors_and_malformed_outputs_are_explicit(self):
        for method, output in [("generate_candidates", "not a list"),
                               ("select_condition", {"not": "a list"}),
                               ("select_condition", [float("nan")])]:
            with self.subTest(method=method, output=output):
                author = Mock()
                author.generate_candidates.return_value = []
                getattr(author, method).return_value = output
                engine = RDREngine(self.evaluator, author)
                before = engine.to_dict()
                with self.assertRaises(RuleAuthorError):
                    engine.learn(engine.predict(self.books), Correct("new"))
                self.assertEqual(engine.to_dict(), before)
        author = Mock()
        author.generate_candidates.side_effect = RuntimeError("generation failed")
        engine = RDREngine(self.evaluator, author)
        before = engine.to_dict()
        with self.assertRaises(RuleAuthorError):
            engine.learn(engine.predict(self.books), Correct("new"))
        self.assertEqual(engine.to_dict(), before)

    def test_invalid_payloads_and_missing_author_rejected(self):
        for case in (float("nan"), {1: "invalid key"}, (1, 2)):
            with self.assertRaises(InvalidPayloadError):
                self.engine.predict(case)
        cyclic = []
        cyclic.append(cyclic)
        with self.assertRaises(InvalidPayloadError):
            self.engine.predict(cyclic)
        engine = RDREngine(self.evaluator)
        with self.assertRaises(RuleAuthorError):
            engine.learn(engine.predict(self.books), Correct("new"))
        with self.assertRaises(TypeError):
            engine.learn(engine.predict(self.books), True)

    def test_oracle_failure_leaves_tree_unchanged(self):
        oracle = Mock()
        oracle.review.side_effect = RuntimeError("No verdict")
        before = self.engine.to_dict()
        with self.assertRaises(OracleError):
            self.engine.process(self.books, oracle)
        self.assertUnchanged(before)

    def test_conjunction_short_circuits(self):
        evaluator = Mock()
        evaluator.evaluate.return_value = False
        data = self.engine.to_dict()
        data["nodes"][0]["true_child"] = 1
        data["nodes"].append({
            "id": 1, "conditions": ["false predicate", "must not evaluate"],
            "conclusion": "other", "evidence": [], "true_child": None, "false_child": None,
        })
        engine = RDREngine.from_dict(data, evaluator=evaluator)
        self.assertEqual(engine.predict({}).conclusion, "standard")
        evaluator.evaluate.assert_called_once_with("false predicate", {})

    def test_deep_tree_loading_and_prediction_are_iterative(self):
        evaluator = Mock()
        evaluator.evaluate.return_value = False
        data = self.engine.to_dict()
        data["nodes"][0]["true_child"] = 1
        last_id = 1500
        for node_id in range(1, last_id + 1):
            data["nodes"].append({
                "id": node_id, "conditions": ["false"], "conclusion": node_id,
                "evidence": [], "true_child": None,
                "false_child": node_id + 1 if node_id < last_id else None,
            })
        engine = RDREngine.from_dict(data, evaluator=evaluator)
        prediction = engine.predict({})
        self.assertEqual(prediction.conclusion, "standard")
        self.assertEqual(prediction.last_visited_id, last_id)
        self.assertEqual(prediction.last_true_id, 0)
        self.assertEqual(len(prediction.path), last_id + 1)

    def test_json_round_trip_preserves_results_paths_evidence_and_revision(self):
        engine = self.trained()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tree.json"
            engine.save(path)
            restored = RDREngine.load(path, evaluator=self.evaluator, author=self.author)
            self.assertEqual(restored.to_dict(), engine.to_dict())
            for case in (self.books, self.fruit, self.glass, self.ice):
                left, right = engine.predict(case), restored.predict(case)
                self.assertEqual(left, right)
            with self.assertRaises(InvalidPredictionError):
                restored.learn(engine.predict(self.books), Accept())
            result = restored.process(package("new books"), self.oracle)
            self.assertEqual(result.status, "accepted")
            self.assertEqual(restored.revision, engine.revision + 1)
            restored.save(path)  # replacing an existing snapshot also works
            self.assertEqual(json.loads(path.read_text()), restored.to_dict())

    def test_non_default_attempt_budget_survives_reload(self):
        engine = RDREngine(self.evaluator, self.author, max_selection_attempts=1)
        engine.learn(engine.predict(self.books), Accept())
        restored = RDREngine.from_dict(engine.to_dict(), evaluator=self.evaluator, author=self.author)
        result = restored.learn(restored.predict(self.books), Correct("impossible"))
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.status, "unresolved")

    def test_failed_snapshot_replacement_preserves_original_file(self):
        engine = self.trained()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tree.json"
            engine.save(path)
            original = path.read_bytes()
            engine.process(package("new books"), self.oracle)
            with patch("rdr_engine.engine.os.replace", side_effect=OSError("write failed")):
                with self.assertRaises(OSError):
                    engine.save(path)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_invalid_json_files_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tree.json"
            for payload in ("{broken", "null", "NaN"):
                with self.subTest(payload=payload):
                    path.write_text(payload, encoding="utf-8")
                    with self.assertRaises(InvalidTreeError):
                        RDREngine.load(path, evaluator=self.evaluator)

    def test_invalid_snapshots_rejected(self):
        good = self.trained().to_dict()
        mutations = [
            lambda data: data.update(schema_version=2),
            lambda data: data.update(revision=-1),
            lambda data: data.update(max_selection_attempts=0),
            lambda data: data.update(root_id=1),
            lambda data: data.update(nodes=[]),
            lambda data: data["nodes"][0].update(conditions=["not unconditional"]),
            lambda data: data["nodes"][0].update(false_child=1),
            lambda data: data["nodes"][1].update(id=12),
            lambda data: data["nodes"][1].update(true_child=99),
            lambda data: data["nodes"][1].update(true_child=-1),
            lambda data: data["nodes"][1].update(true_child=True),
            lambda data: data["nodes"][1].update(true_child=0),  # cycle
            lambda data: data["nodes"][1].update(true_child=2),  # shared child
            lambda data: data["nodes"][1].update(true_child=None),  # unreachable node
            lambda data: data["nodes"][1].update(evidence="not an array"),
        ]
        for change in mutations:
            data = copy.deepcopy(good)
            change(data)
            with self.subTest(data=data):
                with self.assertRaises(InvalidTreeError):
                    RDREngine.from_dict(data, evaluator=self.evaluator)


if __name__ == "__main__":
    unittest.main()
