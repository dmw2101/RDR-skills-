from __future__ import annotations

import copy
import itertools
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from rdr_engine import (
    Accept,
    Add,
    EvaluationError,
    InvalidFeedbackError,
    InvalidPayloadError,
    InvalidPredictionError,
    InvalidTreeError,
    OracleError,
    RDREngine,
    Replace,
    Revise,
    RuleAuthorError,
    StalePredictionError,
    Stop,
)
from rdr_engine.demo import (
    DeliveryAuthor, DeliveryEvaluator, DeliveryOracle, expected_labels, package,
)


def labels(prediction):
    return set(prediction.distinct_conclusions)


def node(node_id, conditions=(), conclusion=None, children=(), stopping=False, evidence=()):
    return {
        "id": node_id, "conditions": list(conditions), "conclusion": conclusion,
        "stopping": stopping, "evidence": list(evidence), "children": list(children),
    }


def snapshot(*nodes):
    return {
        "schema_version": 2, "revision": 0, "max_selection_attempts": 3, "root_id": 0,
        "nodes": list(nodes),
    }


def flag(field):
    return {"field": field, "equals": True}


class FlagEvaluator:
    def evaluate(self, condition, case):
        return case.get(condition["field"], False) == condition["equals"]


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
        self.eggs = package("eggs", perishable=True, fragile=True)
        self.melons = package("melons", perishable=True, fragile=True, packing="crate")
        self.training = [self.books, self.fruit, self.glass, self.ice, self.eggs, self.melons]

    def trained(self):
        for case in self.training:
            self.engine.process(case, self.oracle)
        return self.engine

    def assertUnchanged(self, before):
        self.assertEqual(self.engine.to_dict(), before)

    def flag_engine(self, *nodes, author=None):
        return RDREngine.from_dict(snapshot(*nodes), evaluator=FlagEvaluator(), author=author)

    # Inference -----------------------------------------------------------

    def test_dummy_root_is_true_without_calling_evaluator(self):
        evaluator = Mock()
        evaluator.evaluate.side_effect = AssertionError("Root should be unconditional")
        engine = RDREngine(evaluator)
        before = engine.to_dict()
        result = engine.predict({"any": [1, None]})
        self.assertEqual(result.distinct_conclusions, (True,))
        self.assertEqual([item.node_id for item in result.conclusions], [0])
        self.assertEqual(result.fired, (0,))
        self.assertEqual(engine.to_dict(), before)
        evaluator.evaluate.assert_not_called()

    def test_learned_tree_has_refinements_stops_and_independent_rules(self):
        engine = self.trained()
        nodes = engine.nodes
        self.assertEqual(len(nodes), 5)
        self.assertEqual(nodes[0].children, (1, 2))
        self.assertEqual(nodes[1].children, (3,))
        self.assertEqual(nodes[2].children, (4,))
        self.assertTrue(nodes[4].stopping)
        self.assertIsNone(nodes[4].conclusion)
        self.assertEqual([n.parent for n in nodes], [None, 0, 0, 1, 2])
        for case in self.training:
            self.assertEqual(labels(engine.predict(case)), expected_labels(case))

    def test_root_default_only_when_no_child_fires(self):
        engine = self.trained()
        self.assertEqual(labels(engine.predict(self.books)), {"standard"})
        self.assertNotIn("standard", labels(engine.predict(self.fruit)))

    def test_all_siblings_are_evaluated_and_terminals_are_in_preorder(self):
        engine = self.trained()
        result = engine.predict(self.eggs)
        self.assertEqual([item.node_id for item in result.conclusions], [1, 2])
        self.assertEqual(result.fired, (0, 1, 2))
        self.assertEqual(
            [(s.node_id, s.matched, s.depth) for s in result.trace],
            [(0, True, 0), (1, True, 1), (3, False, 2), (2, True, 1), (4, False, 2)],
        )

    def test_children_of_unfired_nodes_are_not_evaluated(self):
        engine = self.trained()
        result = engine.predict(self.books)
        self.assertEqual([s.node_id for s in result.trace], [0, 1, 2])

    def test_refinement_replaces_and_stop_suppresses_parent_conclusion(self):
        engine = self.trained()
        self.assertEqual([i.node_id for i in engine.predict(self.ice).conclusions], [3])
        melons = engine.predict(self.melons)
        self.assertEqual(labels(melons), {"express"})
        self.assertIn(4, melons.fired)

    def test_stop_under_root_gives_empty_output(self):
        engine = self.flag_engine(
            node(0, conclusion="default", children=[1]), node(1, [flag("a")], stopping=True)
        )
        result = engine.predict({"a": True})
        self.assertEqual(result.conclusions, ())
        self.assertEqual(engine.predict({}).distinct_conclusions, ("default",))

    def test_refinement_under_a_stop_restores_a_conclusion(self):
        engine = self.flag_engine(
            node(0, conclusion="d", children=[1]),
            node(1, [flag("a")], "x", children=[2]),
            node(2, [flag("b")], stopping=True, children=[3]),
            node(3, [flag("c")], "y"),
        )
        self.assertEqual(engine.predict({"a": True, "b": True}).distinct_conclusions, ())
        self.assertEqual(
            engine.predict({"a": True, "b": True, "c": True}).distinct_conclusions, ("y",)
        )

    def test_distinct_conclusions_remove_repeats_across_nodes(self):
        engine = self.flag_engine(
            node(0, conclusion="d", children=[1, 2]),
            node(1, [flag("a")], {"x": [1]}), node(2, [flag("b")], {"x": [1]}),
        )
        result = engine.predict({"a": True, "b": True})
        self.assertEqual(len(result.conclusions), 2)
        self.assertEqual(result.distinct_conclusions, ({"x": [1]},))

    def test_conjunction_short_circuits_but_siblings_do_not(self):
        evaluator = Mock()
        evaluator.evaluate.return_value = False
        engine = RDREngine.from_dict(snapshot(
            node(0, conclusion="standard", children=[1, 2]),
            node(1, ["false predicate", "must not evaluate"], "other"),
            node(2, ["sibling"], "other"),
        ), evaluator=evaluator)
        self.assertEqual(engine.predict({}).distinct_conclusions, ("standard",))
        self.assertEqual(
            [call.args[0] for call in evaluator.evaluate.call_args_list],
            ["false predicate", "sibling"],
        )

    def test_deep_and_wide_trees_load_and_predict_iteratively(self):
        last_id = 1500
        deep = [node(0, conclusion="root", children=[1])] + [
            node(i, [flag("on")], i, children=[i + 1] if i < last_id else []) for i in range(1, last_id + 1)
        ]
        engine = self.flag_engine(*deep)
        prediction = engine.predict({"on": True})
        self.assertEqual(prediction.distinct_conclusions, (last_id,))
        self.assertEqual(len(prediction.trace), last_id + 1)
        self.assertEqual(prediction.trace[-1].depth, last_id)
        wide = [node(0, conclusion="root", children=list(range(1, last_id + 1)))] + [
            node(i, [flag("on")], i) for i in range(1, last_id + 1)
        ]
        engine = self.flag_engine(*wide)
        self.assertEqual(len(engine.predict({"on": True}).conclusions), last_id)

    # Acceptance ----------------------------------------------------------

    def test_acceptance_stores_case_at_every_terminal(self):
        engine = self.trained()
        case = package("more eggs", perishable=True, fragile=True)
        count = len(engine.nodes)
        engine.learn(engine.predict(case), Accept())
        nodes = engine.nodes
        self.assertIn(case, nodes[1].evidence)
        self.assertIn(case, nodes[2].evidence)
        self.assertNotIn(case, nodes[0].evidence)
        self.assertEqual(len(nodes), count)

    def test_acceptance_stores_at_silent_stopping_terminals(self):
        engine = self.flag_engine(
            node(0, conclusion="d", children=[1]), node(1, [flag("a")], stopping=True)
        )
        engine.learn(engine.predict({"a": True}), Accept())
        self.assertEqual(engine.nodes[1].evidence, ({"a": True},))

    def test_acceptance_is_set_like_for_identical_json_examples(self):
        self.engine.process(self.books, self.oracle)
        revision = self.engine.revision
        self.engine.process(dict(reversed(list(self.books.items()))), self.oracle)
        self.assertEqual(self.engine.revision, revision)
        self.assertEqual(len(self.engine.nodes[0].evidence), 1)

    # Revision ------------------------------------------------------------

    def test_prediction_result_keeps_pre_correction_conclusions(self):
        result = self.engine.process(self.fruit, self.oracle)
        self.assertEqual(labels(result.prediction), {"standard"})
        self.assertEqual(result.feedback, Revise((Replace(0, "express"),)))
        self.assertEqual(result.status, "revised")
        self.assertEqual(result.new_node_ids, (1,))
        self.assertEqual(labels(self.engine.predict(self.fruit)), {"express"})

    def test_stored_cases_keep_outputs_after_every_update(self):
        seen = []
        for case in self.training:
            self.engine.process(case, self.oracle)
            seen.append(case)
            for old in seen:
                self.assertEqual(labels(self.engine.predict(old)), expected_labels(old))

    def test_regression_property_over_all_flag_combinations(self):
        for order in itertools.islice(itertools.permutations(range(8)), 0, 40, 7):
            engine = RDREngine(self.evaluator, self.author, default_conclusion="standard")
            cases = []
            for index in order:
                p, f, r = (bool(index & 1), bool(index & 2), bool(index & 4))
                cases.append(package(f"case {index}", perishable=p, fragile=f, refrigerated=r))
            accepted = []
            for case in cases:
                engine.process(case, self.oracle)
                result = engine.process(case, self.oracle)
                if result.status == "accepted":
                    accepted.append(case)
                for old in accepted:
                    with self.subTest(order=order, case=old["name"]):
                        self.assertEqual(labels(engine.predict(old)), expected_labels(old))

    def test_multiple_changes_in_one_revision(self):
        author = Mock()
        author.generate_candidates.side_effect = lambda case, cornerstones: [
            flag(name) for name in ("a", "b", "c") if case.get(name)
        ]
        author.select_condition.side_effect = lambda candidates, *args: candidates[:1]
        engine = self.flag_engine(
            node(0, conclusion="d", children=[1, 2]),
            node(1, [flag("a")], "x"), node(2, [flag("b")], "y"),
            author=author,
        )
        case = {"a": True, "b": True, "c": True}
        result = engine.learn(engine.predict(case), Revise((
            Stop(1), Replace(2, "z"), Add("w"),
        )))
        self.assertEqual(result.status, "revised")
        self.assertEqual(result.new_node_ids, (3, 4, 5))
        self.assertEqual(result.attempts, (1, 1, 1))
        self.assertEqual(engine.revision, 1)
        self.assertEqual(labels(engine.predict(case)), {"z", "w"})
        kinds = [call.args[3].kind for call in author.select_condition.call_args_list]
        self.assertEqual(kinds, ["stop", "replace", "add"])
        self.assertEqual(author.select_condition.call_args_list[0].args[3].conclusion, None)

    def test_add_under_terminal_root_drops_the_default(self):
        author = Mock()
        author.generate_candidates.return_value = []
        author.select_condition.return_value = []
        engine = RDREngine(FlagEvaluator(), author, default_conclusion="d")
        result = engine.learn(engine.predict({}), Revise((Add("extra"),)))
        self.assertEqual(result.status, "revised")
        self.assertEqual(engine.predict({}).distinct_conclusions, ("extra",))

    def test_add_under_a_fired_non_terminal_node(self):
        author = Mock()
        author.generate_candidates.return_value = [flag("c")]
        author.select_condition.side_effect = lambda candidates, *args: candidates
        engine = self.flag_engine(
            node(0, conclusion="d", children=[1]),
            node(1, [flag("a")], "x", children=[2]), node(2, [flag("b")], "y"),
            author=author,
        )
        case = {"a": True, "b": True, "c": True}
        engine.learn(engine.predict(case), Revise((Add("z", parent_id=1),)))
        self.assertEqual(labels(engine.predict(case)), {"y", "z"})
        self.assertEqual(engine.nodes[1].children, (2, 3))

    def test_cornerstones_include_cases_stored_outside_the_parents_subtree(self):
        author = Mock()
        author.generate_candidates.return_value = [flag("c"), flag("a")]
        author.select_condition.side_effect = lambda candidates, *args: candidates
        engine = self.flag_engine(
            node(0, conclusion="d", children=[1, 2]),
            node(1, [flag("a")], "x"), node(2, [flag("b")], "y"),
            author=author,
        )
        # Stored only at node 2, but it also fires node 1.
        engine.learn(engine.predict({"a": True, "b": True}), Accept())
        engine.learn(engine.predict({"a": True, "c": True}), Revise((Replace(1, "z"),)))
        self.assertEqual(author.generate_candidates.call_args.args[1], [{"a": True, "b": True}])
        self.assertEqual(author.select_condition.call_args.args[0], [flag("c")])
        self.assertEqual(labels(engine.predict({"a": True, "b": True})), {"x", "y"})

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
        engine.learn(engine.predict(self.fruit), Revise((Replace(0, "express"),)))
        self.assertEqual(author.select_condition.call_args.args[0],
                         [{"field": "perishable", "equals": True}])

    def test_additional_conditions_can_form_a_discriminating_conjunction(self):
        author = Mock()
        author.generate_candidates.return_value = [flag("a"), flag("b")]
        author.select_condition.return_value = author.generate_candidates.return_value
        engine = RDREngine(FlagEvaluator(), author, default_conclusion="old")
        engine.learn(engine.predict({"a": True, "b": False}), Accept())
        engine.learn(engine.predict({"a": False, "b": True}), Accept())
        result = engine.learn(engine.predict({"a": True, "b": True}), Revise((Replace(0, "new"),)))
        self.assertEqual(author.select_condition.call_args.args[0], [])
        self.assertEqual(result.status, "revised")
        self.assertEqual(labels(engine.predict({"a": True, "b": True})), {"new"})
        self.assertEqual(labels(engine.predict({"a": True, "b": False})), {"old"})
        self.assertEqual(labels(engine.predict({"a": False, "b": True})), {"old"})

    def test_invalid_proposals_retry_then_accept(self):
        author = Mock()
        author.generate_candidates.return_value = []
        author.select_condition.side_effect = [
            [{"field": "perishable", "equals": False}],  # misses new case
            [],                                            # matches cornerstone
            [{"field": "perishable", "equals": True}],
        ]
        engine = RDREngine(self.evaluator, author)
        engine.learn(engine.predict(self.books), Accept())
        result = engine.learn(engine.predict(self.fruit), Revise((Replace(0, "express"),)))
        self.assertEqual(result.attempts, (3,))
        self.assertEqual(result.status, "revised")
        self.assertEqual([call.args[-1] for call in author.select_condition.call_args_list], [1, 2, 3])

    def test_one_unresolved_change_leaves_everything_unchanged(self):
        author = Mock()
        author.generate_candidates.return_value = []
        author.select_condition.side_effect = [[flag("a")], [flag("x")], [flag("x")], [flag("x")]]
        engine = self.flag_engine(
            node(0, conclusion="d", children=[1, 2]),
            node(1, [flag("a")], "x"), node(2, [flag("b")], "y"),
            author=author,
        )
        before = engine.to_dict()
        result = engine.learn(
            engine.predict({"a": True, "b": True}), Revise((Replace(1, "z"), Replace(2, "w")))
        )
        self.assertEqual(result.status, "unresolved")
        self.assertEqual(result.attempts, (1, 3))
        self.assertIn("Change 2", result.reason)
        self.assertEqual(engine.to_dict(), before)

    def test_conflicting_identical_case_is_unresolved_without_mutation(self):
        self.engine.learn(self.engine.predict(self.books), Accept())
        before = self.engine.to_dict()
        result = self.engine.learn(
            self.engine.predict(self.books), Revise((Replace(0, "contradiction"),))
        )
        self.assertEqual(result.status, "unresolved")
        self.assertIn("already stored", result.reason)
        self.assertUnchanged(before)

    def test_empty_conjunction_allowed_only_without_cornerstones(self):
        author = Mock()
        author.generate_candidates.return_value = []
        author.select_condition.return_value = []
        engine = RDREngine(self.evaluator, author)
        result = engine.learn(engine.predict(self.books), Revise((Replace(0, None),)))
        self.assertEqual(result.status, "revised")
        self.assertEqual(engine.predict(self.fruit).distinct_conclusions, (None,))
        result = engine.learn(engine.predict(self.fruit), Revise((Replace(1, "express"),)))
        self.assertEqual(result.status, "unresolved")

    def test_explicit_feedback_does_not_depend_on_conclusion_equality(self):
        result = self.engine.learn(self.engine.predict(self.fruit), Revise((Replace(0, "standard"),)))
        self.assertEqual(result.status, "revised")
        self.assertEqual(len(self.engine.nodes), 2)

    def test_revision_also_accepts_unmentioned_terminals(self):
        engine = self.trained()
        case = package("big eggs", perishable=True, fragile=True, refrigerated=True)
        engine.process(case, self.oracle)
        self.assertIn(case, engine.nodes[2].evidence)

    # Invalid feedback and stale predictions --------------------------------

    def test_structurally_invalid_feedback_raises_without_mutation(self):
        engine = self.trained()
        prediction = engine.predict(self.eggs)
        before = engine.to_dict()
        for feedback in (
            Revise(()),
            Revise((Stop(3),)),              # not a terminal here
            Revise((Stop(99),)),             # unknown node
            Revise((Stop(True),)),           # not a real ID
            Revise((Stop(1), Replace(1, "z"))),
            Revise((Add("z", parent_id=3),)),  # parent did not fire
            Revise((Add("z", parent_id=1),)),  # terminal: use Replace
            Revise((Replace(1, float("nan")),)),
        ):
            with self.subTest(feedback=feedback), self.assertRaises(InvalidFeedbackError):
                engine.learn(prediction, feedback)
            self.assertEqual(engine.to_dict(), before)
        with self.assertRaises(TypeError):
            engine.learn(prediction, Revise(("not a change",)))
        with self.assertRaises(TypeError):
            engine.learn(prediction, True)
        self.assertEqual(engine.to_dict(), before)

    def test_stale_feedback_rejected_after_evidence_change_or_insertion(self):
        for other_case in (self.books, self.fruit):
            with self.subTest(other_case=other_case):
                engine = RDREngine(self.evaluator, self.author, default_conclusion="standard")
                old = engine.predict(self.glass)
                engine.process(other_case, self.oracle)
                before = engine.to_dict()
                with self.assertRaises(StalePredictionError):
                    engine.learn(old, Revise((Replace(0, "insured"),)))
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
            self.engine.learn(replace(prediction, fired=(0, 5)), Accept())
        with self.assertRaises(InvalidPredictionError):
            self.engine.learn(replace(prediction, conclusions=()), Accept())

    def test_external_payload_mutations_cannot_change_tree(self):
        conclusion = {"skill": "original", "patches": []}
        engine = RDREngine(self.evaluator, self.author, default_conclusion=conclusion)
        conclusion["skill"] = "changed"
        case = {"value": [1]}
        prediction = engine.predict(case)
        case["value"].append(2)
        prediction.conclusions[0].conclusion["patches"].append("leak")
        with self.assertRaises(InvalidPredictionError):
            engine.learn(prediction, Accept())
        prediction = engine.predict({"value": [1]})
        engine.learn(prediction, Accept())
        prediction.case["value"].append(3)
        nodes = engine.nodes
        nodes[0].conclusion["patches"].append("unexpected")
        nodes[0].evidence[0]["value"].append(4)
        self.assertEqual(engine.nodes[0].conclusion, {"skill": "original", "patches": []})
        self.assertEqual(engine.nodes[0].evidence, ({"value": [1]},))

    # Component failures ----------------------------------------------------

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
                    engine.learn(engine.predict({}), Revise((Replace(0, "new"),)))
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
                    engine.learn(engine.predict(self.books), Revise((Replace(0, "new"),)))
                self.assertEqual(engine.to_dict(), before)
        engine = RDREngine(self.evaluator)
        with self.assertRaises(RuleAuthorError):
            engine.learn(engine.predict(self.books), Revise((Replace(0, "new"),)))

    def test_invalid_payloads_rejected(self):
        for case in (float("nan"), {1: "invalid key"}, (1, 2)):
            with self.assertRaises(InvalidPayloadError):
                self.engine.predict(case)
        cyclic = []
        cyclic.append(cyclic)
        with self.assertRaises(InvalidPayloadError):
            self.engine.predict(cyclic)

    def test_oracle_failure_leaves_tree_unchanged(self):
        oracle = Mock()
        oracle.review.side_effect = RuntimeError("No verdict")
        before = self.engine.to_dict()
        with self.assertRaises(OracleError):
            self.engine.process(self.books, oracle)
        self.assertUnchanged(before)

    # Persistence -----------------------------------------------------------

    def test_json_round_trip_preserves_results_traces_evidence_and_revision(self):
        engine = self.trained()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tree.json"
            engine.save(path)
            restored = RDREngine.load(path, evaluator=self.evaluator, author=self.author)
            self.assertEqual(restored.to_dict(), engine.to_dict())
            self.assertEqual(restored.nodes, engine.nodes)
            for case in self.training:
                self.assertEqual(engine.predict(case), restored.predict(case))
            with self.assertRaises(InvalidPredictionError):
                restored.learn(engine.predict(self.books), Accept())
            result = restored.process(package("new books"), self.oracle)
            self.assertEqual(result.status, "accepted")
            self.assertEqual(restored.revision, engine.revision + 1)
            restored.save(path)
            self.assertEqual(json.loads(path.read_text()), restored.to_dict())

    def test_non_default_attempt_budget_survives_reload(self):
        engine = RDREngine(self.evaluator, self.author, max_selection_attempts=1)
        engine.learn(engine.predict(self.books), Accept())
        engine.learn(engine.predict(self.fruit), Accept())
        restored = RDREngine.from_dict(engine.to_dict(), evaluator=self.evaluator, author=self.author)
        result = restored.learn(restored.predict(package("x")), Revise((Replace(0, "impossible"),)))
        self.assertEqual(result.attempts, (1,))
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

    def test_single_classification_snapshot_has_a_specific_error(self):
        legacy = {"schema_version": 1, "revision": 0, "max_selection_attempts": 3, "root_id": 0,
                  "nodes": [{"id": 0, "conditions": [], "conclusion": True, "evidence": [],
                             "true_child": None, "false_child": None}]}
        with self.assertRaisesRegex(InvalidTreeError, "single-classification"):
            RDREngine.from_dict(legacy, evaluator=self.evaluator)

    def test_invalid_snapshots_rejected(self):
        good = self.trained().to_dict()
        mutations = [
            lambda data: data.update(schema_version=3),
            lambda data: data.update(revision=-1),
            lambda data: data.update(max_selection_attempts=0),
            lambda data: data.update(root_id=1),
            lambda data: data.update(nodes=[]),
            lambda data: data["nodes"][0].update(conditions=["not unconditional"]),
            lambda data: data["nodes"][0].update(stopping=True, conclusion=None),
            lambda data: data["nodes"][4].update(conclusion="stops conclude nothing"),
            lambda data: data["nodes"][4].update(stopping="yes"),
            lambda data: data["nodes"][1].update(id=12),
            lambda data: data["nodes"][1].update(children=[99]),
            lambda data: data["nodes"][1].update(children=[-1]),
            lambda data: data["nodes"][1].update(children=[True]),
            lambda data: data["nodes"][1].update(children=[3, 3]),
            lambda data: data["nodes"][1].update(children=[0]),      # cycle to root
            lambda data: data["nodes"][3].update(children=[3]),      # self cycle
            lambda data: data["nodes"][1].update(children=[3, 4]),   # shared child
            lambda data: data["nodes"][1].update(children=[]),       # unreachable node
            lambda data: data["nodes"][1].update(children="3"),
            lambda data: data["nodes"][1].update(evidence="not an array"),
            lambda data: data["nodes"][1].pop("stopping"),
            lambda data: data["nodes"][1].update(true_child=None),
        ]
        for change in mutations:
            data = copy.deepcopy(good)
            change(data)
            with self.subTest(data=data):
                with self.assertRaises(InvalidTreeError):
                    RDREngine.from_dict(data, evaluator=self.evaluator)

    def test_cycle_between_non_root_nodes_rejected(self):
        data = snapshot(
            node(0, conclusion="d", children=[1]), node(1, [flag("a")], "x"),
            node(2, [flag("b")], "y", children=[3]), node(3, [flag("c")], "z", children=[2]),
        )
        with self.assertRaises(InvalidTreeError):
            RDREngine.from_dict(data, evaluator=FlagEvaluator())


if __name__ == "__main__":
    unittest.main()
