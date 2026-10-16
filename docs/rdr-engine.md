# Base RDR engine (MCRDR)

This package implements **Multiple Classification Ripple-Down Rules** (MCRDR; Kang, Compton & Preston). It builds on the single-conclusion algorithms in **RDR**, Ashwin Srinivasan, August 2026 (`rdr.md` in the parent Ashwin-SOP workspace). The engine owns traversal, evidence, cornerstone checking, and insertion. Domain components own the meaning of cases, conditions, and conclusions. No language model, network access, credentials, or benchmark data is required.

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

The skill tests also need `pydantic` (`pip install -e ".[skill-compiler]"`).

The delivery demo supplies all three deterministic components. Training cases produce:

- a default `standard` conclusion;
- independent `express` and `insured` rules, which fire together for eggs;
- a `cold-chain` refinement that replaces `express`;
- a stopping rule that withdraws `insured` from crated melons.

It then predicts new cases and checks a save/load round trip. This is a mechanics demonstration, not a skill benchmark.

## Inference

The tree is n-ary. Node 0 is an unconditional root.

1. The root always fires.
2. For every node that fires, **all** of its children are evaluated, in insertion order. A child of a node that did not fire is never evaluated.
3. A *terminal* node is a fired node none of whose children fired. Every terminal reports its conclusion, so a case can receive several conclusions.
4. A child that fires therefore *refines* its parent: the parent's conclusion is replaced by the child's.
5. A **stopping rule** (`stopping=True`, conclusion `null`) is a child that suppresses its parent's conclusion and reports nothing itself.
6. The root's conclusion is the **default**. It is reported only when none of the root's children fire. If only stopping rules fire under the root, the output is empty.

Conditions are conjunctions: an empty conjunction is always true, and evaluation stops at the first false predicate. Siblings are never short-circuited.

## Public API

```python
from rdr_engine import Accept, Add, RDREngine, Replace, Revise, Stop
from rdr_engine.demo import DeliveryAuthor, DeliveryEvaluator, DeliveryOracle, package

engine = RDREngine(DeliveryEvaluator(), DeliveryAuthor(), default_conclusion="standard")

prediction = engine.predict(package("books"))
engine.learn(prediction, Accept())

prediction = engine.predict(package("fruit", perishable=True))
result = engine.learn(prediction, Revise((Replace(0, "express"),)))
assert result.prediction.distinct_conclusions == ("standard",)  # original, not corrected
assert result.status == "revised"

# Or run the complete loop through an oracle component.
result = engine.process(package("glass", fragile=True), DeliveryOracle())

engine.save("delivery.json")
engine = RDREngine.load("delivery.json", evaluator=DeliveryEvaluator(), author=DeliveryAuthor())
```

`predict(case)` is read-only. Its result contains:

| Field | Meaning |
| --- | --- |
| `case` | Snapshot of the input |
| `conclusions` | `FiredConclusion(node_id, conclusion)` for each terminal, non-stopping node, in depth-first preorder |
| `distinct_conclusions` | Conclusion values without repeats (by canonical JSON), first occurrence first |
| `fired` | IDs of every node that fired, in preorder |
| `trace` | `EvaluationStep(node_id, matched, depth)` for every evaluated node, in preorder, including false tests |
| `revision` | Tree/evidence revision used for the prediction |

Pass the original, unmodified prediction to `learn`. Predictions are tied to their engine instance and revision; they are not portable feedback tokens. After any intervening update or reload, predict again and obtain fresh feedback. `engine.nodes` returns defensive node snapshots (`id`, `conditions`, `conclusion`, `evidence`, `children`, `parent`, `stopping`) for inspection, not a mutable editing API.

## Feedback

Feedback is explicit and per conclusion:

| Feedback | Meaning | Tree change |
| --- | --- | --- |
| `Accept()` | Every reported conclusion is right. | Store the case at every terminal node, including silent stopping terminals. |
| `Stop(node_id)` | That terminal's conclusion is wrong and nothing replaces it. | Add a stopping child under `node_id`. |
| `Replace(node_id, conclusion)` | That terminal's conclusion is wrong; use `conclusion` (even `None`). | Add a refinement child under `node_id`. |
| `Add(conclusion, parent_id=0)` | A conclusion is missing. | Add a child under a node that fired, the root by default. |

`Stop`, `Replace` and `Add` are combined in `Revise((change, ...))`. Conclusions that a `Revise` does not mention are implicitly accepted, so the case is stored at those terminals too.

There is no single-conclusion `Correct` any more. To replace a whole output, send a `Stop` or `Replace` for each terminal. Two details follow from the inference rules:

- Adding under the root when the root's default is the only output also withdraws the default.
- Adding under any other terminal node would silently replace its conclusion, so it is rejected; use `Replace` instead. Adding under a fired *stopping* node is allowed and reinstates a conclusion for the matching cases.

`learn` returns an `UpdateResult` with:

- the original prediction;
- the normalized feedback;
- the current revision;
- `new_node_ids`;
- `attempts` (selection attempts per change);
- one of three statuses:
  - `accepted`: evidence stored; no new node.
  - `revised`: every change produced a validated node.
  - `unresolved`: at least one change had no valid conjunction within the attempt budget, or the final regression check failed. `reason` names the change. **Nothing** is added, not even evidence.

Never score a correction as the engine's prediction: it is an oracle correction, not an independently predicted answer. The engine also never compares conclusions to decide correctness. Replacing a conclusion with an equal value, or adding one that is already present, is allowed.

These raise `InvalidFeedbackError` and leave the tree unchanged:

- an empty `Revise`;
- a target that is not a reported conclusion of this prediction;
- the same conclusion corrected twice;
- an `Add` parent that did not fire;
- a non-JSON conclusion.

Non-feedback objects or changes raise `TypeError`.

## Supply another domain

Implement these methods; subclassing the protocols is not required:

| Component | Methods and contract |
| --- | --- |
| `ConditionEvaluator` | `evaluate(condition, case) -> bool`: interpret one individual predicate. Return an actual Boolean, or raise. The engine handles AND and short-circuiting. |
| `RuleAuthor` | `generate_candidates(case, cornerstones) -> Sequence[JSONValue]`: propose predicates distinguishing the new case from the cornerstone cases. |
| `RuleAuthor` | `select_condition(candidates, case, cornerstones, request, attempt) -> Sequence[JSONValue]`: choose the final conjunction. May add predicates not in `candidates`. `request` is a `RuleRequest(kind, parent_id, conclusion)` with `kind` in `stop`/`replace`/`add` (`conclusion` is `None` for a stop). `attempt` starts at 1. |
| `Oracle` | `review(prediction) -> Accept() or Revise(changes)`: independently assess each conclusion. |

The author can be omitted for prediction and acceptance, but revision requires it. An oracle object is only needed for `process`; applications may obtain feedback themselves and call `learn`.

Cases, individual predicates, and conclusions can be any strict JSON value: null, Boolean, finite number, string, list, or string-keyed object. Datetimes, paths, tuples, executable callbacks, and arbitrary Python objects must first be encoded as JSON values. Components receive defensive copies.

The core treats conclusions as ordinary data: it does not open files, interpret Markdown, or execute skills. The skill pipeline (`skill_compiler`, `skill_renderer`) is one such adapter, with `{"fragments": [...]}` conclusions.

## Learning and cornerstones

A new node under parent `P` fires for a stored case exactly when `P` fires for that case and the new conjunction holds. So the **cornerstones** for `P` are all distinct stored cases, *wherever they are stored*, whose current inference fires `P`. This is broader than the evidence in `P`'s subtree, because one case can fire several branches.

For each change in a `Revise`, in order:

1. Collect the cornerstones for the change's parent.
2. If the current case itself is stored and fires the parent, the change cannot be validated, and the revision is `unresolved`. Inconsistent feedback on an already stored identical case cannot be reconciled.
3. Generate candidate predicates. Keep only those true on the new case and false on **every** cornerstone (Algorithm 5's strict individual filter).
4. Ask the author for a conjunction, which may include extra predicates. It must match the new case and exclude every cornerstone, or it is retried up to `max_selection_attempts` (default 3). An empty conjunction is valid only when there are no cornerstones.

When every change is resolved, the new nodes are staged on a copy of the tree. They are appended in change order with the next contiguous IDs, and each stores the case as its first evidence. The staged tree must then pass a final check:

- the new case must produce exactly the corrected output;
- every stored case must keep its previous output.

Only then is the copy committed, with one revision increment. With deterministic, unchanged evaluation and evidence built through this API, stored cases keep their accepted conclusions. This does not guarantee generalization to unseen cases or oracle correctness.

Malformed component outputs, exceptions, and non-Boolean evaluations fail immediately (as `EvaluationError` or `RuleAuthorError`) instead of being treated as false or retried silently.

Acceptance is set-like: identical canonical JSON cases are not stored twice at one node. The revision increments once per `learn` call that changes evidence or nodes, never on duplicate acceptance, prediction, failure, or unresolved revision.

## Persistence and operational limits

`to_dict()` / `from_dict()` expose the same format as `save()` / `load()`. Schema version 2 stores the root ID, revision, attempt budget, and an ordered node table. Each node stores:

- `id`
- `conditions`
- `conclusion`
- `stopping`
- `evidence`
- `children`: an ordered list of IDs

Parents are derived from `children`, not stored. IDs are contiguous from zero.

Loading rejects:

- unsupported schemas (schema 1 single-classification snapshots get a specific error, since SCRDR false branches cannot be converted without negating opaque conditions);
- malformed payloads;
- bad or duplicate child IDs;
- cycles, shared children, and unreachable nodes;
- a conditional or stopping root;
- a stopping node with a conclusion.

Loading validates structure, not the domain semantics of conditions or evidence, and does not call evaluators. Evaluator and author implementations are supplied again, not serialized; using a different evaluator can change predictions. Keep adapter/model configurations pinned alongside experiment artifacts.

Saving replaces the destination atomically after writing a temporary file in the same existing directory. A failed write does not deliberately truncate an existing snapshot. This is not a concurrent-writer store or a crash-durability guarantee.

Failures are explicit:

- `EvaluationError`
- `RuleAuthorError`
- `OracleError`
- `InvalidPayloadError`
- `InvalidFeedbackError`
- `InvalidTreeError`
- `InvalidPredictionError`, including `StalePredictionError`

Invalid feedback types raise `TypeError`; filesystem errors propagate. The engine is synchronous and single-writer: callbacks must not re-enter it or mutate it from another thread. Prediction ownership checks guard accidental misuse, not hostile Python code.

Inference and structural validation are iterative, so deep chains and wide fan-outs do not hit Python's recursion limit. Payload JSON copying still has Python's nesting limits. Every revision re-runs inference for all stored cases, so cost grows as (stored cases × tree size). There is no summarization, pruning, caching, or LLM cost control yet.

## Correspondence to the sources

| Source | Implementation |
| --- | --- |
| RDR Algorithm 1: overall loop | `process`, or separate `predict` / `learn` calls |
| MCRDR inference | Iterative `predict`; all children of a fired node are tested; terminal fired nodes report |
| RDR Algorithm 3: EvaluateCondition | `_matches`; empty conjunction is true |
| MCRDR refinement / stopping rule / new rule | `Replace`, `Stop`, `Add` in `Revise` |
| MCRDR cornerstone cases | Stored cases that fire the new rule's parent, from anywhere in the tree |
| RDR Algorithm 5: FindDifferences | Author generation followed by individual candidate filtering against cornerstones |
| RDR Algorithms 6 and 7: SelectCondition / ExistsSatisfied | Bounded selection loop and conjunction checks against cornerstones |

Explicit feedback replaces conclusion-equality tests, authoring is separated from oracle judgment, and bounded attempts replace unbounded repetition. Markdown-to-rule compilation lives in `skill_compiler.py`. Live LLM adapters, skill execution, and benchmark experiments remain outside this engine.
