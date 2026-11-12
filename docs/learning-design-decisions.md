# Learning design decisions

A record of every design decision about how the learner proposes, checks and keeps rules: the
**inspector** (called the *verifier* in the code), the **judge** and the **checker** (validation). Use it to
see why the pipeline works the way it does and to set up ablation studies: each decision names the
config switch that turns it off, or says it has none.

**Keep this file current.** A change to the inspector, judge or checker updates its entry here in the
same commit. A new decision gets the next number in its section. A decision that is replaced keeps its
entry, with *Status: replaced by …*, so earlier results stay explainable.

## Roles

| Role | In the code | Job |
|---|---|---|
| Worker | the run config's `model` | Does the task, with guidance from the current tree |
| Inspector | `verifier` (`trace_learner/verifier.py`) | Reads a failed trace and proposes one rule: conditions, advice, target node |
| Merge | `merge` (`trace_learner/consolidate.py`) | Combines proposals that target the same node in one round |
| Judge | `judge` (`FeatureJudge`, `skill_agent/learned_features.py`) | Answers each learned condition true / false / unknown for one context. Never decides whether a rule is kept |
| Checker | `validation` (`trace_learner/validate.py`) | Reruns the worker on the tasks a rule affects; code keeps the rule only if they pass |

## How to read an entry

- **Decision**: what the pipeline does.
- **Alternatives**: what else was considered.
- **Why**: the reason for the choice.
- **Where**: the code and the config switch.
- **Ablation**: the config change that runs the alternative, or "none" when there is no switch.

## Decisions about the inspector

### I1. The inspector proposes; the engine and the checker decide
- **Decision:** the inspector only drafts a rule. Whether it is kept is decided by code: the engine's checks, then the checker's reruns scored by the benchmark's own scorer. No model votes on a rule.
- **Alternatives:** an LLM reviewer that approves or rejects rules.
- **Why:** acceptance rests on measured task outcomes, not on a model's opinion.
- **Where:** `workspace.SkillWorkspace.apply`, `validate.validate_candidate`, `incremental._insert`.
- **Ablation:** none.
- **Status:** in use (since 2026-10-28, commit `4272ccf`).

### I2. A diagnosis counts only after its fix is scored as passing
- **Decision:** the inspector must call `rescore` with a corrected answer, and the scorer must pass it. Code records this; the inspector's own claim is not used.
- **Alternatives:** trust the inspector's diagnosis.
- **Why:** a rule should only be tried when fixing the identified mistake really fixes the task.
- **Where:** `verifier.eligibility` (`fix_validated`).
- **Ablation:** none.
- **Status:** in use (since `4272ccf`).

### I3. Success examples for the inspector, at every level
- **Decision:** the inspector can call `passing_examples` for its target node. It gets up to **6** verified successes in which that node supplied guidance: the most recent, excluding the failing task. This applies at every level, including the root. Its prompt tells it to use them to narrow its condition.
- **Alternatives:** no examples; examples only for rules below level 1 (at the root, any success counts, so the examples are not specific to the failure); a preview tool that shows which successes a draft fires on before submitting. The preview was **rejected** because the judge (4B) and inspector (27B) cannot share the GPU, so each preview would need two model switches.
- **Why:** the professors asked for past successes to guide drafting. At deeper levels the examples are exactly the cases a refinement must keep working. At the root they still show what normal cases look like.
- **Where:** `passing.PassingTraces.examples`, `verifier.VerifierSession` (`passing_examples` tool), config `verifier.passing_examples` (default `true`).
- **Ablation:** `verifier.passing_examples: false`.
- **Status:** in use since 2026-11-13 (examples from the merge of `a71deb4`; the switch was added the same day).

### I4. One retry, always on, for every rejection
- **Decision:** when a rule is rejected, the inspector gets **one** more run with feedback, and this cannot be switched off. The triggers are:
  - the engine rejects it as too broad (its condition is not true for the failing case, or it matches a stored success);
  - the checker's rerun shows the failing task still fails;
  - the checker's rerun shows a changed success now fails.

  The feedback names each document involved, with an excerpt. A retry that fails again is abandoned.
- **Alternatives:** no retry; several retries; retry only for engine rejections.
- **Why:** many rejections are fixable by narrowing. The professors asked for a smarter retrial.
- **Where:** `incremental._insert` (`retryable`), `incremental._feedback`, `incremental._checker_feedback`.
- **Ablation:** none (always on, by decision).
- **Status:** in use. The engine-rejection retry dates from 2026-10-28 (`ea82ec0`); the checker-rejection retry was added 2026-11-13. A retry records `retry_of` (the proposal it revises) in the ledger.

## Decisions about the judge

### J1. The judge reads the worker's starting materials and recent activity
- **Decision:** the judge sees the task, the requested output and the input document's text (first 12,000 characters), plus the resolver query: the task at first, then the worker's last three messages. It does not see later tool outputs. The same text is built during learning and at run time.
- **Alternatives:** the resolver query only.
- **Why:** conditions such as "the PDF has no text layer" need the document. Building the text the same way keeps learning and test-time judgments identical.
- **Where:** `learned_features.judge_context`, the adapter's `judge_materials`.
- **Ablation:** none.
- **Status:** in use (since 2026-10-28, `37f20a1`).

### J2. Only `true` fires a rule; `unknown` never does
- **Decision:** the judge answers true, false or unknown, and a learned condition holds only when it is judged true. The judge must say true on the failing case, or the rule is rejected.
- **Alternatives:** treat unknown as true.
- **Why:** a rule should fire only when the visible evidence establishes its condition.
- **Where:** `proposals.Proposal.predicates`, `workspace._apply`.
- **Ablation:** none.
- **Status:** in use (since `4272ccf`).

### J3. A judge failure is not a verdict
- **Decision:** a judge answer that is cut off, blocked or malformed stops learning, which resumes on rerun. It is never recorded as a rule rejection. The same applies to any refused model call.
- **Alternatives:** count it as a rejection.
- **Why:** a broken call says nothing about the rule.
- **Where:** `JudgeError` handling in `workspace.apply` and `incremental`.
- **Ablation:** none.
- **Status:** in use (since 2026-10-28, `3178ec5`, `7c577e8`).

## Decisions about the tree and the engine

### T1. Learning starts from a root-only tree
- **Decision:** the whole skill sits at a lone root, and every child is learned.
- **Alternatives:** the compiled flat tree (an unconditional general node plus one node per compiled rule).
- **Why:** tests whether learning needs the skill split into conditional rules up front.
- **Where:** config `base_tree` (`"root_only"` or `"compiled"`); `SkillBundle.reshape`.
- **Ablation:** `base_tree: "compiled"`.
- **Status:** in use on this branch (since 2026-11-04, `983184f`).

### T2. An add under a guidance-giving terminal node becomes a replace that keeps its guidance
- **Decision:** a new child of a node that rendered guidance (with no fired child) is applied as a `replace` keeping all that node's fragments plus the new advice. In a root-only tree, this covers adds under the root.
- **Alternatives:** the engine's plain add, which would hide the parent's guidance where the child fires.
- **Why:** the inspector's intent is "the node's advice **plus** the new advice where the conditions hold".
- **Where:** `workspace._apply`.
- **Ablation:** none.
- **Status:** in use (since 2026-10-28, `a6a2c17`).

### T3. Each document is learned from once, on the tree learned so far
- **Decision:** incremental mode with **batch size 1**. Each document runs on the newest tree, and its failure is diagnosed once and not revisited.
- **Alternatives:** larger batches, which let the merge model combine similar failures and need fewer model switches.
- **Why:** classic case-by-case RDR. A failure an earlier rule already fixes never reaches the inspector.
- **Where:** config `mode: "incremental"`, `batch_size`.
- **Ablation:** `batch_size: N`.
- **Status:** in use (since 2026-10-28, `e82bf22`); batch size 1 confirmed 2026-11-13.

### T4. Six snapshots of each success are stored as reference cases
- **Decision:** each passing task stores up to **6** snapshots (evenly spaced turns, always including the first) as cornerstone cases for the engine.
- **Alternatives:** every turn; only the first turn.
- **Why:** this bounds judge cost and storage while covering a whole task.
- **Where:** config `cornerstones_per_episode`; `pipeline.sample_views`.
- **Ablation:** change `cornerstones_per_episode`.
- **Status:** in use (since `4272ccf`).

## Decisions about the checker

### C1. Changed successes are rerun, not rejected
- **Decision:** when a draft rule would change the guidance of stored successes, the engine **allows** it, and the checker **reruns those successes**, with no cap. The rule is kept only if the failing task now passes and every rerun success still passes.
- **Alternatives:** classic RDR, where the engine rejects any rule that changes a stored case and the inspector narrows it on its retry; a capped number of reruns.
- **Why:** our rules give advice, not labels. Different advice can still give a correct answer, so rejecting every rule that touches a success throws away broad, useful rules such as "if the PDF is rotated, unrotate it". A cap may be added later if rerun costs are too high.
- **Where:** `RDREngine.learn(..., changeable=...)`, `workspace._apply`, `incremental._insert`; config `validation.changed_successes` (`"rerun"` default, `"reject"`).
- **Ablation:** `validation.changed_successes: "reject"` restores engine rejection, together with C2's fallback.
- **Detail:** only stored cases from learning qualify. They carry a context, so their task can be re-run; any other stored case keeps classic protection. Batch mode (`pipeline.Learner`) always uses classic rejection.
- **Status:** in use since 2026-11-13 (the default).

### C2. Only affected tasks are rerun
- **Decision:** the checker reruns the failing task plus every success whose guidance would change. It finds these from the stored cases the rule fires on, and by replaying every saved success view under the old and new tree. Successes whose guidance is unchanged are not rerun, since they see identical guidance. The old fallback, which reran the 3 earliest successes when nothing changed, is **dropped**.
- **Alternatives:** rerun up to `protected_limit` successes reaching the changed node (the original behaviour); keep the fallback.
- **Why:** a rerun of unchanged guidance cannot test the rule.
- **Where:** `passing.affected_tasks`, `incremental._insert`.
- **Ablation:** `validation.changed_successes: "reject"` restores the fallback.
- **Status:** in use since 2026-11-13. Each candidate's `validation/cN/selection.json` lists the `protected` tasks, the `changed_documents` and the `mode`.

### C3. Stored references follow the rerun
- **Decision:** after a rule is kept, each changed success's stored snapshots are **replaced** by snapshots from its passing rerun. The inspector's success examples and the impact replay also switch to that newest passing trace.
- **Alternatives:** keep the original snapshots.
- **Why:** later rules must be checked against the guidance and behaviour a task actually has now.
- **Where:** `workspace.replace_cornerstones`, `incremental._refresh_cornerstones` (the ledger entry's `refresh` and `origins`), and `incremental._passing_index`, which derives each task's newest passing record from the rounds and their kept rules. `rounds/rNNN/cornerstones_before.json` lets `redo-last-round` undo the swap.
- **Ablation:** none (part of C1).
- **Status:** in use since 2026-11-13.

### C4. One run per rerun task
- **Decision:** the checker runs each affected task **once**.
- **Alternatives:** 2 runs (the old default) with a majority, meaning both must pass; 3 runs with a majority.
- **Why:** the local models run at temperature 0, so repeat runs are nearly identical. One run halves the checker's cost. Revisit if decisions look inconsistent.
- **Where:** config `validation.replicates`.
- **Ablation:** `validation.replicates: 2` (or more).
- **Status:** in use since 2026-11-13, set in `examples/learning/varex.learner.json`, which the Colab setup copies. The code's own default is still 2 for configs that omit it.

## Decisions about serving and measurement

### G1. The worker/judge model and the inspector model take turns on one GPU
- **Decision:** the 4B (worker, checker, judge) and the 27B (inspector, merge) run as two local vLLM servers that cannot both fit in GPU memory. Before each call, `SwitchingClient` wakes the call's server and puts the other to sleep (vLLM sleep mode, level 1: weights move to CPU memory). A switch waits until no call to the other server is in flight.
- **Alternatives:** both models loaded at once (forces a smaller inspector); the inspector on a hosted API (cost in two units).
- **Why:** a strong 27B inspector with a capable worker on a 24 GB GPU. Test-time evaluation uses only the worker and judge, so no switching happens there.
- **Where:** `skill_agent.models.SwitchingClient` (option `peers`); the learner's model configs.
- **Ablation:** none (a serving choice, not a method choice).
- **Status:** in use since 2026-11-13.

### M1. Latency is measured per model call, apart from waits and switches
- **Decision:** each model call records `latency_seconds`, the wall time of its successful HTTP request. Retry back-off (`retry_wait_seconds`) and model switches (`switch_seconds`) are recorded separately and never counted as latency. The judge's calls carry the same fields. `skill_agent summarize` reports per arm:
  - `model_seconds` and `model_seconds_per_call`;
  - `judge_seconds`;
  - `model_switch_seconds` and `retry_wait_seconds`;
  - `other_seconds`: tools, skill selection and overhead.
- **Alternatives:** whole-episode time only, which mixes model time with tool time.
- **Why:** the skill method changes what the model reads, so per-call model time isolates its effect on latency.
- **Where:** `skill_agent.models.OpenAICompatibleClient._post`, `skill_agent.experiments.timing_summary`.
- **Ablation:** none (measurement only).
- **Status:** in use since 2026-11-13. Records from earlier runs report null model time.

## Decisions about visibility

### V1. Every proposed rule is logged in full, after every round
- **Decision:** after each round, the learner writes `rounds/rNNN/rules.md` and regenerates the running `rules.md`. Each lists every proposed rule, accepted and rejected, including retry drafts, untruncated:
  - target node, level and kind;
  - each condition with its definition and examples;
  - the advice;
  - the inspector's reasoning and evidence;
  - the judge's answers;
  - the successes the rule fired on;
  - each rerun and its result;
  - the decision and its reason.

  The tree diagram (`versions/vN/tree.html`) shows learned conditions and advice in plain English. The log is read-only: learning never waits for a person.
- **Alternatives:** a human approval step before each rule is inserted (classic RDR with an expert in the loop).
- **Why:** the professors asked for proposed rules to be readable. Approval would need someone watching every run.
- **Where:** `trace_learner/rules_log.py`; `rdr_engine/visualise.py` (`write_tree_page`; `scripts/visualise_tree.py` is now a thin wrapper around it). The ledger entry carries `level`, `retry_of`, `changed_documents`, `reruns`, the insert's `judged` and `changed`, and `refresh`.
- **Ablation:** none (always on).
- **Status:** in use since 2026-11-13.
