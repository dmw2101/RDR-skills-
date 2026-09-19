# Base RDR engine

This package implements the single-conclusion algorithms in **RDR**, Ashwin Srinivasan, August 2026 (`rdr.md` in the parent Ashwin-SOP workspace). It owns traversal, evidence, consistency checking, and insertion. Domain components own the meaning of cases, conditions, and conclusions. No language model, network access, credentials, or benchmark data is required.

## Run it

From the repository root, using Python 3.10 or newer:

```sh
python -m pip install -e .
python -m rdr_engine.demo
python -m unittest discover -s tests -v
```

Use a virtual environment for installation. Alternatively, without installing anything, in PowerShell:

```powershell
$env:PYTHONPATH = 'src'
py -3.12 -m rdr_engine.demo
py -3.12 -m unittest discover -s tests -v
```

The delivery demo supplies all three deterministic components. Four training cases produce a default `standard` conclusion, a right-hand `express` exception, a left-hand `insured` alternative, and a nested `cold-chain` exception. It then predicts new cases and checks a save/load round trip. This is a mechanics demonstration, not a skill benchmark.

## Public API

```python
from rdr_engine import Accept, Correct, RDREngine
from rdr_engine.demo import DeliveryAuthor, DeliveryEvaluator, DeliveryOracle, package

evaluator = DeliveryEvaluator()
author = DeliveryAuthor()
oracle = DeliveryOracle()
engine = RDREngine(evaluator, author, default_conclusion="standard")

# Separate inference and feedback, for applications with an external verifier.
prediction = engine.predict(package("books"))
engine.learn(prediction, Accept())

prediction = engine.predict(package("fruit", perishable=True))
result = engine.learn(prediction, Correct("express"))
assert result.prediction.conclusion == "standard"  # original, not corrected
assert result.feedback.conclusion == "express"
assert result.status == "revised"

# Or run the complete loop through an oracle component.
result = engine.process(package("glass", fragile=True), oracle)

engine.save("delivery.json")
engine = RDREngine.load("delivery.json", evaluator=evaluator, author=author)
```

`predict(case)` is read-only. Its result contains:

| Field | Meaning |
| --- | --- |
| `case`, `conclusion` | Snapshots of the input and the last true node's conclusion |
| `last_visited_id` | Final node examined (`v1` in the document) |
| `last_true_id` | Winning node (`v2`); it need not be the final node |
| `path` | Ordered `EvaluationStep(node_id, matched)` entries, including false tests |
| `revision` | Tree/evidence revision used for the prediction |

Pass the original, unmodified prediction to `learn`. Predictions are tied to their engine instance and revision; they are not portable feedback tokens. After any intervening update or reload, predict again and obtain fresh feedback. `engine.nodes` returns defensive node snapshots for inspection, not a mutable editing API.

`learn` returns an `UpdateResult` with the original prediction, explicit feedback, current revision, and one of three statuses:

- `accepted`: store the case at the winning node; no new node.
- `revised`: insert one validated rule; `new_node_id` identifies it.
- `unresolved`: no valid conjunction within the attempt budget; `reason` explains the last failed consistency check. Nothing is added, including evidence.

`attempts` records the number of selection attempts (zero for acceptance). Never score `result.feedback.conclusion` as the engine's prediction: it is an oracle correction, not an independently predicted answer.

## Supply another domain

Implement these methods; subclassing the protocols is not required:

| Component | Methods and contract |
| --- | --- |
| `ConditionEvaluator` | `evaluate(condition, case) -> bool`: interpret one individual predicate. Return an actual Boolean, or raise. The engine handles AND and short-circuiting. |
| `RuleAuthor` | `generate_candidates(case, examples) -> Sequence[JSONValue]`: propose predicates distinguishing the new case from the winning node's stored cases. |
| `RuleAuthor` | `select_condition(candidates, case, examples, conclusion, attempt) -> Sequence[JSONValue]`: choose the final conjunction. May add predicates not in `candidates`. `conclusion` is the correction; `attempt` starts at 1. |
| `Oracle` | `review(prediction) -> Accept() or Correct(replacement)`: independently assess the prediction and explicitly accept or correct it. |

The author can be omitted for prediction and acceptance, but correction requires it. An oracle object is only needed for `process`; applications may obtain feedback themselves and call `learn`.

Cases, individual predicates, and conclusions can be any strict JSON value: null, Boolean, finite number, string, list, or string-keyed object. Datetimes, paths, tuples, executable callbacks, and arbitrary Python objects must first be encoded as JSON values. Components receive defensive copies.

For example, a future skill adapter could use:

```python
case = {"task": "extract a table", "file_type": "pdf", "scanned": True}
condition = {"field": "scanned", "equals": True}
default_conclusion = {"skill_ref": "skills/example/initial.md"}
```

The core treats `skill_ref` as ordinary data: it does not open files, interpret Markdown, apply patches, or execute skills. The adapter must implement those operations. Natural-language conditions also work if the evaluator knows how to judge them. The delivery oracle happens to compare expected labels, but the core does not use conclusion equality to decide correctness; a skill oracle could instead run an execution verifier.

## Learning and consistency

The root is node 0 with an empty conjunction (always true), configurable conclusion, and no examples. The default conclusion is Boolean `True`, matching the document's dummy conclusion.

On correction:

1. Generate candidate predicates using the new case and evidence at the previous winning node.
2. Retain only individual predicates true on the new case and false on **every** stored example at that node.
3. Ask the author to select a conjunction, including any additional predicates it needs.
4. Independently evaluate the entire conjunction. It must match the new case and exclude every stored example at that node.
5. If `last_visited_id == last_true_id`, attach the new node as the final node's true/right child. Otherwise, attach it as the final node's false/left child. Its evidence initially contains the corrected case.

The individual filter is deliberately strict, as in Algorithm 5. Sometimes no predicate passes it although a conjunction can distinguish the cases. Additional predicates from the selector allow this; the final consistency check is still mandatory.

Selection gets three attempts by default (`max_selection_attempts` is configurable). A logically unsuitable conjunction is retried. Malformed component outputs, exceptions, and non-Boolean evaluations fail immediately instead of being treated as false or retried silently. `Correct(None)` is a valid explicit correction. An empty conjunction is valid only when there is no old evidence to exclude.

Complete examples are retained. Acceptance is set-like: identical canonical JSON cases are not stored twice. The revision increments on each actual evidence change or node insertion, not on duplicate acceptance, prediction, failure, or unresolved revision.

With deterministic, unchanged evaluation and evidence built through the learning API, the insertion and consistency constraint preserve conclusions for stored cases. This does not guarantee generalization to unseen cases or oracle correctness. Inconsistent feedback on an already stored identical case cannot be reconciled under these constraints and returns unresolved if no valid proposal is found.

## Persistence and operational limits

`to_dict()` / `from_dict()` expose the same format as `save()` / `load()`. Schema version 1 stores the root ID, revision, attempt budget, and an ordered node table. Each node stores `id`, `conditions`, `conclusion`, `evidence`, `true_child`, and `false_child`. Child IDs use JSON null for no child; IDs are contiguous from zero.

Loading rejects unsupported schemas, malformed payloads, bad links, cycles, shared children, unreachable nodes, and a conditional root. It validates structure, not the domain semantics of conditions or evidence. It does not call evaluators while loading. Evaluator and author implementations are supplied again, not serialized; using a different evaluator can change predictions. Keep adapter/model configurations pinned alongside experiment artifacts.

Saving replaces the destination atomically after writing a temporary file in the same existing directory. A failed write does not deliberately truncate an existing snapshot. This is not a concurrent-writer store or a crash-durability guarantee.

Failures are explicit: `EvaluationError`, `RuleAuthorError`, `OracleError`, `InvalidPayloadError`, `InvalidTreeError`, or `InvalidPredictionError` (including `StalePredictionError`). Invalid feedback types raise `TypeError`; filesystem errors propagate. Failed authoring/validation does not mutate the tree. The engine is synchronous and single-writer: callbacks must not re-enter it or mutate it from another thread. Prediction ownership checks guard accidental misuse, not hostile Python code.

Tree traversal and structural validation are iterative. Payload JSON copying still has Python's nesting limits. All evidence is retained and reevaluated during learning; there is no summarization, pruning, caching, or LLM cost control yet.

## Correspondence to the document

| Document | Implementation |
| --- | --- |
| Algorithm 1: overall RDR loop | `process`, or separate `predict` / `learn` calls |
| Algorithm 2: Traverse | Iterative `predict`; true goes right, false goes left |
| Algorithm 3: EvaluateCondition | `_matches`; empty conjunction is true |
| Algorithm 4: Revise | Validated insertion in `learn` |
| Algorithm 5: FindDifferences | Author generation followed by individual candidate filtering |
| Algorithms 6 and 7: SelectCondition / ExistsSatisfied | Bounded selection loop and conjunction checks against stored examples |

The implementation resolves the source's inconsistent traversal arguments by always starting at root 0 and tracking the last true node separately from the next available node ID. The malformed new-node tuple in Algorithm 4 is interpreted according to the prose as `(C -> q', {x}, left=nil, right=nil)`, with no extra rule field. The `eft` typo in acceptance means the existing left link. `None` replaces the source's `-1` sentinel. Explicit feedback replaces Algorithm 1's conclusion-equality test; authoring is separated from oracle judgment; bounded attempts replace Algorithm 6's unbounded repetition.

Only the last true conclusion is returned. Ancestor conclusions are not accumulated. The path is available for a future, separate skill renderer, which must distinguish true and false evaluations rather than blindly concatenate ancestors. Markdown-to-rule compilation, live LLM adapters, skill rendering/execution, and benchmark experiments remain outside this engine.
