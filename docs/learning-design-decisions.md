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
- **Status:** in use. The engine-rejection retry dates from 2026-10-28 (`ea82ec0`); the checker-rejection retry was added 2026-11-13. A retry records `retry_of` (the proposal it revises) in the ledger. Since I13 (2026-11-17) the retry continues the first inspection when it can.

### I5. The inspector's inputs are bounded and the standard ones are given up front
- **Decision:** every inspector tool output is cut after **12,000 characters**, with a note asking for a narrower request. `read_trace` clips each field to 2,000 characters; `skill_views` and `passing_examples` list repeated guidance once. The results of `compare_with_ground_truth` (wrong fields only), `ground_truth`, `skill_views` and `show_tree` are placed in the first prompt, so no model call is spent on them. The example config's time limit is 1,800 s (was 900 s).
- **Alternatives:** no cut (the behaviour until now: only the prediction was clipped); a larger context limit for the 27B.
- **Why:** in the first 150-document run, single outputs reached 46,000 characters (`read_trace`), 44,000 (`skill_views`) and 37,000 (`passing_examples`). Prompts grew to 65,536 tokens: 2 inspections were cut off, 1 got HTTP 400 and 3 hit the 900 s limit after 10 to 14 calls. About 68 of 151 inspector minutes produced nothing.
- **Where:** `verifier._Tool.execute` (`MAX_TOOL_CHARS`), `VerifierSession.briefing`, `passing.PassingTraces.examples`, the hooks' optional `failure_summary`; config `verifier.timeout_seconds`.
- **Ablation:** none.
- **Status:** written 2026-11-14, not yet run on the GPU. I10 and I12 (2026-11-17) add the judge's text, the base skill and the root's successes to the first message.

### I6. A proposal is checked by code when it is submitted
- **Decision:** `submit_diagnosis` refuses a proposal the engine would reject without asking the judge: a target node that does not exist or was not rendered (replace, stop) or did not fire (add) at the failing turn, an unknown feature, a wrong operator or value, a fragment the node does not render, an unknown anchor. The inspector gets the reason and resubmits in the same session.
- **Alternatives:** reject after the session and spend the one retry (I4) on it; no retry at all (the behaviour until now, as for rule 23.1's `in` on a multi-value feature).
- **Why:** these mistakes need no model to find, and a retry costs 7 to 15 minutes.
- **Where:** `verifier.proposal_problems`, `VerifierSession._submit`.
- **Detail (2026-11-16):** a diagnosis sent as JSON text instead of an object is read as the same answer. In round 2 of `learn-4b-rootonly150-v2` the 27B sent its (correct, 6,000-character) diagnosis as text five times and was refused each time. It then "tested" the tool with a filler object ("medium length description padding …"), which was accepted as its diagnosis: rule 2.1 was filler, the 20-minute inspection was lost and the one retry (I4) was spent.
- **Detail (2026-11-17, `learn-4b-rootonly3160`):** on opm1638 the inspector had a passing rescore, then sent its diagnosis as JSON text 7 times with a bracket missing deep in the rule (`..."]}}]` written as `..."}}]`, or a `}` missing before `]`), was refused each time with only a column number, and ran out of its 30 minutes. The text was complete each time, not cut off. A repair of such brackets was written and then dropped in favour of I15, which stops the mistake at the source.
- **Ablation:** none.
- **Status:** written 2026-11-14, not yet run on the GPU.

### I7. The inspector is warned once before its budget runs out
- **Decision:** when 3 model calls or 6 minutes remain, the inspector gets one message telling it to stop investigating and submit: now if a rescore already passed, otherwise after at most one more rescore.
- **Alternatives:** no warning (the behaviour until now).
- **Why:** the inspector could not see its time or call budget. In the first 150-document run, 4 of 13 inspections ran out without submitting anything.
- **Where:** `VerifierSession.progress_note` (`WARN_CALLS_LEFT`, `WARN_SECONDS_LEFT`); `runtime.run_episode` asks any session that has a `progress_note` before each model call and records the message as a `user_message` event.
- **Ablation:** none.
- **Status:** written 2026-11-15, not yet run on the GPU.

### I8. An inspector answer without a tool call is asked again
- **Decision:** when the inspector answers without calling a tool, it gets a short message telling it to act (submit, or make the one check it still needs). This happens at most **twice** per inspection; a third such answer ends the inspection as before.
- **Alternatives:** end the inspection at the first such answer (the behaviour until now).
- **Why:** in the 5-document smoke run of 2026-11-16, the retry inspection of 50164 spent all 8,192 output tokens thinking, called no tool, and the whole 672 s inspection was lost. It was not a context overflow (20,180 input tokens).
- **Where:** `VerifierSession.customer_response` (`MAX_NUDGES`), through the existing follow-up hook in `runtime.run_episode`.
- **Ablation:** none.
- **Status:** written 2026-11-16, not yet run on the GPU.

### I9. A wrong answer key ends the inspection with no rule
- **Decision:** the inspector has a fifth attribution, `answer_key_error`: the gold answer itself is wrong (cut off, not in the input, or contradicting it), so no faithful output can pass. It submits that with its evidence and no proposal, and is told to stop as soon as it establishes this. Such a diagnosis is never eligible, so no rule is tried, no checker run is made and no retry is spent. The prompt also says a rescore must not be made to pass by copying gold values the input does not contain.
- **Alternatives:** leave it to `skill_missing` with a caveat (the behaviour until now).
- **Why:** on document 50164 the gold value for one field is cut off mid-sentence. The inspector noticed ("even an ideal agent caps at 7/8") but still proposed a rule, having "proved" its fix by rescoring the gold values. Under P1 no rule can pass there, and both learning runs spent an inspection, a checker run and a retry inspection on it (about 30 minutes in the smoke run).
- **Where:** `proposals.Diagnosis.attribution`, `verifier.eligibility`, `prompts/verifier.md` (step 3).
- **Ablation:** none.
- **Status:** written 2026-11-16, not yet run on the GPU.

### I10. The inspector sees exactly the text the judge reads
- **Decision:** the inspector's first message includes `starting_materials`: the failing document's text exactly as the judge reads it for a condition (the task, the requested schema, the PDF's filled form fields, then its text layer, cut as the judge's is). Its prompt says to write each condition so it is plainly true in that text, and that `inspect_shell`'s `pdftotext` output differs.
- **Alternatives:** leave the inspector to read the PDF through the shell (the behaviour until now).
- **Why:** of the first 10 rule rejections in `learn-4b-rootonly150-v2` (17 Nov), 5 came from the inspector and the judge reading different text: the inspector read `pdftotext` (form values inline), the judge read the form-field list and then the bare text layer, so conditions true in one were false in the other. Since K4 the judge reads one fixed text per document, so it can be shown once.
- **Where:** `VerifierSession.judge_text`, `VerifierSession.briefing`; `verify_episode(materials=...)`, passed by `incremental._diagnose` and `_retry` (`IncrementalLearner.materials`).
- **Ablation:** none as a switch; a benchmark without `judge_materials` gives no text.
- **Status:** written 2026-11-17, not yet run on the GPU.

### I11. The inspector can test a draft condition on the judge
- **Decision:** a tool, `test_conditions`, sends up to 4 draft `new_feature` conditions to the judge on the failing document's starting materials, the same call that later decides whether the rule is true there (J2). It returns each verdict and a one-line reason per condition from a second, separate judge call. At most **5** tests per inspection; an invalid draft is returned without using one up. The prompt tells the inspector to test its draft and rewrite it until the judge says true.
- **Alternatives:** the judge writes the conditions (rejected: the inspector stays the author, the judge only answers); a test that also checks stored successes (not needed for a rejection: since C1 a rule that fires on a success is rerun, not refused).
- **Why:** the rejection "not true for the failing document" was the most common one (9 of 19 proposals in the first 30 documents), and each costs a full retry inspection (I4). Model switches are short (median 1.5 s, at most 8 s in that run), so a test costs about one judge call.
- **Where:** `VerifierSession._test_conditions` (`MAX_CONDITION_TESTS`), `FeatureJudge.explain`; `verify_episode(judge=...)`, passed by `incremental` (`IncrementalLearner.judge`).
- **Ablation:** none as a switch; without a judge or materials the tool is not offered.
- **Caveat:** the judge samples (S1), so a verdict may differ when the rule is checked. The tool's note says to make conditions plainly true. The reason comes from a separate call and may disagree with the verdict.
- **Detail (2026-11-17, smoke test `learn-4b-rootonly-smoke5-v3`):** on document 40158 the inspector sent the conditions as JSON text in 3 of its 5 calls, and each was refused. The list as JSON text, and a single bare condition object, are now read as the same request. In that inspection the two accepted tests both came back true, and the rule was kept on its first try (11 min, against 22 min plus a 14-minute retry for the same document on 16 Nov). Each reason call took 28 to 36 s and about 1,200 to 1,600 output tokens, against 7 to 11 s for a verdict.
- **Status:** written 2026-11-17, not yet run on the GPU.

### I12. More of the standard inputs are in the first message
- **Decision:** besides I5's four results and I10's text, the first message carries the base skill (all original fragments in order, up to 12,000 characters) and, on a root-only tree, the root's past successes (`passing_examples` for node 0, up to 8,000 characters). The tools stay available for other nodes and learned advice.
- **Alternatives:** leave them as tool calls (the behaviour until now).
- **Why:** each call costs a full 27B turn, and nearly every inspection asked for these. On a root-only tree almost every rule targets the root, so its successes are the ones the inspector needs.
- **Where:** `VerifierSession.briefing` (`BRIEFING_SKILL_CHARS`, `BRIEFING_EXAMPLES_CHARS`).
- **Ablation:** none.
- **Status:** written 2026-11-17, not yet run on the GPU.

### I13. A retry continues the first inspection
- **Decision:** a retry (I4) continues the first inspection's conversation: the same system prompt and messages, followed by the rejection feedback as a new message. The first inspection's passing rescore still counts, so it need not rescore again unless its corrected answer changes. The continued inspection gets at most **12** more calls. It starts afresh instead (as before, with the feedback in its system prompt) when the first one's last request already used more than **40,000** input tokens, or when no usable first conversation is saved.
- **Alternatives:** a fresh inspection with the feedback (the behaviour until now).
- **Why:** a fresh retry re-reads and re-diagnoses everything, so a rejection doubled the cost of a failed document (20 to 45 minutes each). The diagnosis is usually right and only the rule needs changing. Keeping the system prompt identical lets the server reuse its cached prefix. The 40,000-token limit leaves room in the 27B's 65,536-token window (G1).
- **Where:** `verifier.resumable_history`, `verify_episode(previous=...)` (`RESUME_MAX_INPUT_TOKENS`, `RESUME_MAX_CALLS`), `runtime.run_episode(history=...)`; `incremental._retry` reads the first diagnosis file. The retry's saved outcome records `continued`.
- **Ablation:** none as a switch.
- **Status:** written 2026-11-17, not yet run on the GPU.

### I14. A rule's advice is short and does not repeat
- **Decision:** `guidance_text` may have at most **600** characters: at most three short, checkable instructions. A longer one is refused at submit time (I6) with the count, and the inspector shortens it. The prompt tells it to read the learned rules already in the tree first, not to repeat their advice, and, if a rule with the same advice fired and the worker still failed, to say why that advice was not enough and add only what was missing, or refine that rule.
- **Alternatives:** no limit (the behaviour until now); a limit in the proposal schema, which would also refuse saved proposals of earlier runs on resume.
- **Why:** in the first 30 documents of `learn-4b-rootonly150-v2`, nearly every rule said "copy values verbatim" or "for filled forms, read the form fields", and kept rules ran from 300 to 900 characters. K2 showed that the worker follows short, direct advice better.
- **Where:** `verifier.proposal_problems` (`MAX_GUIDANCE_CHARS`), `prompts/verifier.md`.
- **Ablation:** none as a switch.
- **Status:** written 2026-11-17, not yet run on the GPU.

### I15. The inspector's tool calls are constrained to their schemas
- **Decision:** every inspector tool is declared `"strict": true`. With vLLM (0.33) serving the 27B with the `qwen3_xml` tool parser, a strict tool makes xgrammar constrain each call's arguments to the tool's JSON schema as they are generated: the brackets always balance, and a field declared as an object cannot arrive as a quoted string. The inspector's thinking is not constrained; the constraint starts after it. Tool choice stays `auto`.
- **Alternatives:** repair mismatched brackets in code before refusing (written on 17 Nov and dropped: it treats the symptom); split the submission into several simpler calls; leave the arguments unconstrained (the behaviour until now).
- **Why:** unconstrained, the 27B sometimes broke the nesting of long submissions (I6 detail of 17 Nov): 3,000 to 5,000 characters, five levels deep, often sent as one escaped string, sampled at temperature 1.0. On opm1638 that cost a whole inspection with a passing fix, and it caused most refused `test_conditions` calls. Checked on the server's own vLLM and xgrammar, without the GPU: with `strict` the constraint for the 11 inspector tools is built and compiles; without it, vLLM applies no constraint at all in `auto` mode.
- **Where:** `trace_learner.verifier._Tool` (the `strict` flag on every definition).
- **Ablation:** none as a switch; removing the flag restores free generation. Providers that ignore the flag behave as before.
- **Caveat:** the `diagnosis` argument is declared as a free JSON object, so the constraint guarantees valid JSON of the right outer shape; its fields are still checked by `Diagnosis` validation (I6). Not yet seen on a live inspection.
- **Status:** written 2026-11-17, not yet run on the GPU.

## Decisions about the judge

### J1. The judge reads the worker's starting materials and recent activity
- **Decision:** the judge sees the task, the requested output and the input document's text (first 12,000 characters), plus the resolver query: the task at first, then the task followed by the worker's last three messages (K3; before 2026-11-16 the later queries held the three messages alone). It does not see later tool outputs. The same text is built during learning and at run time.
- **Alternatives:** the resolver query only.
- **Why:** conditions such as "the PDF has no text layer" need the document. Building the text the same way keeps learning and test-time judgments identical.
- **Where:** `learned_features.judge_context`, the adapter's `judge_materials`.
- **Ablation:** none.
- **Status:** in use (since 2026-10-28, `37f20a1`). With K4's `initial` loading, the query is the task alone, judged once.

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
- **Detail (2026-11-15):** before it counts as a failure, the judge call is repeated, up to **3** attempts in all, each retry with a different seed (seed + attempt). With one fixed seed (S1) the same request would return the same bad answer, so a restart alone could fail again. Each unusable answer is printed in the log, since it is saved nowhere else. In the first 150-document run a single malformed answer stopped learning and left the GPU idle for about 10 hours.
- **Ablation:** none.
- **Status:** replaced by J6 on 2026-11-17 for the judge's own answers: they are no longer retried and no longer stop learning; an unusable answer counts as `unknown` and is documented. A failed skill lookup and any refused model call still stop learning, which resumes on rerun. In use since 2026-10-28 (`3178ec5`, `7c577e8`); the in-place retry (15 Nov) never ran into an unusable answer.

### J4. A condition about the document is judged once per document
- **Decision:** each learned condition has a **scope**, chosen by the inspector when it writes the condition.
  - `document`: a property of the starting materials alone. The judge reads only those materials, is asked **once per document**, and the answer is reused at every worker step. During learning, every stored snapshot of one document shares that one answer.
  - `situation`: depends on the worker's recent messages. It is judged before every step on the materials plus the resolver query (the task and those messages, see K3), as before. This is the default, so earlier trees behave as they did.
- **Alternatives:** judge every condition before every step (the behaviour until now); judge once and let the judge also read the worker's messages.
- **Why:** in the first 150-document run, one learned rule raised a worker run from 45 s to 206 s, of which 136 s was the judge (about 10.6 calls of 12.8 s). Its answers also changed from step to step on the same document (1 to 10 changes per document), and it said `true` once in 20 first runs. A condition about the document has one right answer, so it is asked once. Checking a candidate rule also needs one call per stored document instead of one per stored snapshot (94 calls for rule 26.1).
- **Where:** `LearnedFeature.scope` (`rdr_engine/learned.py`), `NewFeature.scope` (`trace_learner/proposals.py`), `learned_features.document_context` / `judged_text`, `LearnedFeatureResolver.resolve` (`document_values`), `workspace._judge_contexts`; the inspector's prompt (`prompts/verifier.md`).
- **Ablation:** none as a switch. Conditions written with `scope: "situation"` run the earlier behaviour.
- **Caveat:** one judge answer now decides the rule for the whole document, and the judge samples (S1). A single wrong `unknown` hides the rule for that run.
- **Status:** written 2026-11-14, not yet run on the GPU. Since K4 (2026-11-17) the inspector no longer chooses the scope: every new condition is `document`. Stored `situation` conditions keep working.

### J5. The judge sees a PDF's filled form-field values
- **Decision:** for VAREX, the judge's starting materials list the PDF's filled form fields (`name: value`, at most 3,000 characters) before the text layer. A PDF with no filled fields gets the same materials as before.
- **Alternatives:** the text layer alone (the behaviour until now).
- **Why:** pypdf's text layer leaves out filled AcroForm values. On document 4742 it held none of the 6 filled values, and on opm1637 only the labels. The inspector, which reads the PDF through the shell, wrote conditions about values the judge could not see, so rules 3.1 and 16.1 were rejected as "not true for the failing document".
- **Where:** `skill_agent/benchmarks/varex.py` (`pdf_contents`, `form_values_text`, `VarexAdapter.judge_materials`).
- **Ablation:** none.
- **Caveat:** this changes the judged text for form PDFs, so stored snapshots from earlier runs no longer match new ones. Start a new learning run.
- **Status:** written 2026-11-14, not yet run on the GPU.

### J6. The judge answers greedily, once, in a constrained form; unusable and unknown answers are documented
- **Decision:**
  - **Greedy:** the judge runs at temperature 0 everywhere it is used: the learner's own judge (stored cases, rule checks, the inspector's `test_conditions`), the judge inside learning attempts and the checker's reruns, and the judge in test runs. The worker and the inspector keep the model card's sampling (S1).
  - **Constrained:** `report_features` is a strict tool and the call is forced (`tool_choice: "required"`, one call). The server then only lets the judge answer with exactly the listed conditions, each `true`, `false` or `unknown`. Its thinking and which value it picks are not constrained.
  - **Asked once:** a greedy judge would repeat any answer, so it is never asked again. An answer that is still unusable (in practice: cut off at `max_tokens` while thinking) counts as `unknown` for every condition of that call, which never fires a rule (J2): a rule being checked is rejected, a stored success or a test document does not get it. Learning and test runs go on.
  - **Documented:** every unusable answer, and every answer with `unknown` for any condition, is kept in full (the conditions, the judged text, the judge's whole output and why it stopped): in `judge_evidence/` beside the learner's `judge_calls.jsonl`, and in the run record's `feature_judge.evidence` for worker, checker and test runs. The reports count `true`/`false`/`unknown` (and unusable) answers per learned condition, so frequent `unknown`s can be traced to their cause.
- **Alternatives:** the model card's sampling for the judge too (S1, the behaviour until now); a majority of 3 sampled answers; re-asking an unusable answer, sampled (written on 17 Nov in `de43b83` and dropped: an unusable answer points to a cause in the input or the condition, which a retry hides); stopping learning on an unusable answer (J3).
- **Why:** the judge decides a fact about fixed text, so the same question should get the same answer. Sampled, it did not: on SF17-22a (17 Nov, `learn-4b-rootonly3160`) the inspector's test came back `true` and the rule check, asked the same question on the same text, came back `unknown`, so the rule was rejected. The same flip can hide a learned rule at test time. S1's reason for sampling (the 4B looped under greedy decoding) was seen in the worker's long runs, not in the judge's short answers. Over about 420 judge calls up to 17 Nov the judge used at most 1,284 of its 4,096 output tokens and gave no unusable answer. The 4B is served with the same tool format as the 27B (`qwen3_coder`, the same parser class as I15), and the forced strict call was checked to build and compile on the server's vLLM.
- **Where:** the judge model's `temperature` option in the learner config (`judge`, `validation.judge_model`) and in the test configs' resolver (`judge_model`); `FeatureJudge.judge` and `_evidence`; `LearnedFeatureResolver.resolve` (`evidence`, `unusable`); `OpenAICompatibleClient.complete(tool_choice=...)`; `experiments.judge_value_counts`; `trace_learner.report` (`answers_table`).
- **Ablation:** set the judge model's `temperature` back to 1.0.
- **Caveat:** greedy thinking can loop until `max_tokens`; such an answer is unusable and counts as `unknown`, so the evidence folder should be checked after each run. Runs before this used a sampling judge.
- **Status:** written 2026-11-17, not yet run on the GPU.

## Decisions about the tree and the engine

### T1. Learning starts from a root-only tree
- **Decision:** the whole skill sits at a lone root, and every child is learned. Learned rules use learned conditions only (T5).
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

### T5. On a root-only tree, every rule condition is a learned, judged one
- **Decision:** when `base_tree` is `"root_only"`, the inspector is not shown the original skill's conditions (`operation`, `source_kind`): they are left out of the tree's feature list and of the per-turn condition values it reads. Its instructions say to write every condition as a learned condition, and a proposal that uses an original condition is refused at submit time with a message (the I6 check), so the inspector corrects it in the same session. The root has no conditions and always fires, so the judge alone decides which learned rules fire.
- **Alternatives:** letting the inspector reuse the original conditions (the behaviour until now); having the judge answer the original conditions in place of the keyword match, which needs a list-valued judge answer and about one more judge call per worker run.
- **Why:** the root-only tree tests whether learning needs the skill split into conditional rules up front (T1). The compile step that cuts the skill into fragments also carries a hand-made list of conditions and a hand-written keyword matcher. With those still on offer, a "learned" rule could lean on them: the rule kept in the root-only smoke run of 2026-11-16 was `operation contains "extract_content"` plus one judged condition. On VAREX that condition is always true, so earlier results are unaffected, but the tree was not learned from the skill text alone.
- **Where:** `trace_learner.verifier` (`learned_only`, `LEARNED_ONLY_INSTRUCTIONS`, `proposal_problems`), set from `base_tree` in `trace_learner.incremental`.
- **Ablation:** none as a separate switch; it follows `base_tree`.
- **Detail:** the service still computes the original conditions by keyword match for every request, and stored cases still record them; no rule on a root-only tree reads them. Not covered: the merge step used with batch sizes above 1.
- **Status:** written 2026-11-16, not yet run on the GPU.

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
- **Detail:** the replay asks the judge for every saved view in one batch, 4 calls at a time (`workspace.replay_cases`). Until 2026-11-14 it asked one view after another, which took 12.7 of the 19 minutes spent checking rule 26.1.
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
- **Why:** one run halves the checker's cost. It was chosen when the models ran at temperature 0, so repeat runs were nearly identical. Since S1 they sample, which makes one run noisier; revisit if decisions look inconsistent.
- **Where:** config `validation.replicates`.
- **Ablation:** `validation.replicates: 2` (or more).
- **Status:** in use since 2026-11-13, set in `examples/learning/varex.learner.json`, which the Colab setup copies. The code's own default is still 2 for configs that omit it.

## Decisions about the data

### D1. VAREX Flat is split about 50/50: learn on 150, test on 149
- **Decision:** of the 299 VAREX Flat documents (sorted, shuffled with seed 42), learning uses the **first 150 held-out documents**. The final test uses the other **149**: the 30 dev documents plus held-out documents 151–269. No document is in both sets.
- **Alternatives:**
  - learn on the first 30 held-out documents and test on dev-30 (the earlier set-up, a leftover from the pilot's 30-document evaluation split);
  - match Trace2Skill: learn on VRDU Registration Forms and test on all 299 VAREX Flat. This would need a VRDU adapter first, and it tests transfer between datasets rather than learning within one.
- **Why:** 30 documents gave too few failures to learn from (the 10-document run had one learnable failure). Neither the VAREX paper nor Trace2Skill defines a VAREX training split; VAREX is test-only in both. Dev-30 goes in the test set because the learner never sees it. Our harness was tuned on it during the pilot, but that tuning is shared by every arm.
- **Where:** `skill_agent prepare --heldout-limit 150` (`benchmarks/local-data/varex-heldout-150`). The test set needs a prepare option for "dev plus held-out from position 151", not yet written.
- **Ablation:** none (a data choice).
- **Status:** decided 2026-11-14. The first learning run on 150 documents started the same day.

### D2. The next learning run uses held-out documents 31–60, and dev-30 is the test set
- **Decision:** the learning run of 17 Nov (`learn-4b-rootonly3160`) learns from scratch, on the root-only tree, from held-out documents 31–60 (positions in the prepared 150, taken in order). Every arm (no skill, full skill, root-only base, learned tree) is tested on the same 30 dev documents, one run each.
- **Alternatives:** learn again from held-out documents 1–30; test on held-out 151–269 (D1).
- **Why:** the inspector fixes I10 to I14 were designed from what happened on held-out documents 1–30 in `learn-4b-rootonly150-v2`, and the smoke test used 5 of them. Learning on them again would flatter the learning-phase numbers (rejections, rules kept). Documents 31–60 were chosen by position, not by content. They overlap neither documents 1–30, nor dev-30, nor held-out 151–269. Dev-30 is the test set because results go to the supervisors next to the earlier dev-30 pilot.
- **Where:** `run.task_ids` in the server config `learn-4b-rootonly3160.json`; the dev-30 configs `dev30-v3-*.json`.
- **Ablation:** none (a data choice).
- **Caveat:** the harness fixes H1 to H3 came from crashes and lost answers in the dev-30 pilot, so dev-30 influenced the harness. They apply to every arm alike and follow Trace2Skill's and SkillOpt's set-up, but a report on dev-30 should say so.
- **Status:** decided 2026-11-17; the learning run starts that evening after the dev-30 baselines.

## Decisions about the skill

### K1. The skill arms get the whole PDF skill package, not only SKILL.md
- **Decision:** every arm except `none` finds Anthropic's `pdf` skill files in its working folder: `FORMS.md` (forms.md), `REFERENCE.md` (reference.md) and `scripts/` (8 scripts), from the pinned commit `3337550`. The skill text the tree serves is unchanged: `SKILL.md`, identical to `skills/pdf/initial.md`, 1,007 words. The worker reads the extra files only when it chooses to. This covers learning runs, the checker's reruns and every evaluation arm.
- **Alternatives:** `SKILL.md` alone, as in the pilot, where its pointers ("read FORMS.md", "see REFERENCE.md") led to missing files; compiling the extra files into the tree, which would quadruple the skill text at the root.
- **Why:** this is the skill as Anthropic ships it, and the package Trace2Skill evolved. `forms.md` and `extract_form_field_info.py` cover fillable form fields, the cause of the first learning run's only learnable failure (ri20-064). The upstream files are lower-case, but `SKILL.md` names them in capitals, so they are copied under the capitalised names it uses (Linux file names are case-sensitive).
- **Where:** config `skill.files`; `skill_agent.experiments.skill_files`, `VenvPDFSession.add_files`. The files stay in the git-ignored `papers/Trace2Skill/skills/licensed/` folder (Anthropic's licence forbids redistribution) and are copied into each worker folder at run time.
- **Ablation:** omit `skill.files`.
- **Status:** in use from 2026-11-14. Results from earlier runs, including the pilot, used `SKILL.md` alone.

### K2. Learned advice is shown first, in its own block, and the worker is told to follow it
- **Decision:** when learned rules fire, their advice is rendered **first**, inside `<learned_rules>` with a one-line header saying the rules come from earlier mistakes, apply to this task and win over the general guidance. Only the advice of rules whose conditions were judged true is in the block; with none, there is no block. The skill's own fragments follow in their original order, unchanged. The worker's skill instructions (both loading modes, so every skill arm reads the same text) say these rules are requirements, to read them before the first action, and to check the answer against each rule before finishing. The inspector is told its advice is shown this way and to write short, checkable instructions.
- **Alternatives:** the advice placed inside the skill text after an anchor fragment, or at the end without one (the behaviour until now); the advice alone, hiding the skill when a rule fires; the advice repeated as a late reminder message.
- **Why:** in the 5-document smoke run of 2026-11-16, all 3 proposed rules were rejected although each rule fired and its advice was in the worker's prompt on every turn. The advice was 300 to 900 characters inside about 8,400 characters of general PDF guidance (at 22% and 89% of the way through), and the worker's behaviour did not change: it merged blocks the advice said to keep apart, and stripped `$` signs the advice said to keep.
- **Where:** `rdr_engine.skill_renderer.SkillRenderer.render` (`LEARNED_RULES_HEADER`), `skill_agent.runtime.LEARNED_RULES_INSTRUCTIONS`, `prompts/verifier.md`. A proposal's `anchor_fragment_id` is still accepted but no longer decides the position.
- **Ablation:** none as a switch.
- **Detail:** the instruction text changes the system prompt of every skill arm, so results are not comparable with runs before this commit.
- **Status:** written 2026-11-16; first used in the smoke runs of the same day (1 rule proposed and kept on each tree).

### K3. The task text stays in every guidance refresh
- **Decision:** in rolling loading, the text that picks the worker's guidance before each step is the task followed by the worker's last three responses (the first step uses the task alone, as before). The original skill's conditions (for PDF: `operation`, `source_kind`) are keyword matches on that text, so whatever the task sets holds for the whole attempt; the recent responses can only add operations. The judge's "recent activity" for conditions that are not about the document now starts with the task too.
- **Alternatives:** the last three responses alone (the behaviour until now); working out the conditions once at the first step and freezing them, which would stop the guidance following a task that moves to another operation.
- **Why:** a learned rule's base condition could turn false mid-task. In the expanded-tree smoke run of 2026-11-16 the kept rule fired on turns 0 to 6 and not on turns 7 and 8, because the worker's last three responses no longer contained a word like "extract" or "read". The advice block then leaves the prompt, possibly just before the answer is written, and a checker rerun can fail for that reason alone.
- **Where:** `skill_agent.runtime.RollingSkillPolicy` (`keep_task`). Each refresh records `input_source`: `task`, `task_and_recent_model_turns`, or `recent_model_turns`.
- **Ablation:** none as a config switch; `RollingSkillPolicy(keep_task=False)` in code.
- **Detail:** one sentence of the automatic-loading instructions changed to say this, so the system prompt of rolling arms differs from earlier runs. On the expanded tree, every operation the task names now stays rendered for the whole attempt.
- **Status:** written 2026-11-16; first used in `learn-4b-rootonly150-v2`. Applies only to rolling loading; with K4's `initial` loading there are no refreshes.

### K4. The guidance is built once, before the worker's first step
- **Decision:** a new loading mode, `skill_loading: "initial"`. The guidance is built once, before the first worker step, from the task and the starting materials (the same view a rolling run starts with), and stays the same for the whole attempt. Learned conditions are therefore judged once per attempt. The worker's skill instructions say the guidance is fixed. The inspector no longer chooses a condition's scope: every new condition is stored as `document` (J4), and its prompt says the judge answers once, before the first step, from the starting materials alone, so a mid-task mistake must be described by what in the input makes it likely ("a filled form field holds a shortened name", not "the worker is expanding a shortened name").
- **Alternatives:** rolling loading (K3), rebuilt before every step; rolling with a refresh every N steps, or only when the worker asks.
- **Why:** in the dev-30 pilot of 2026-11-17, the learned arm took 96 min against 31 to 36 min for the other arms; 61 min of it was the judge (328 calls), almost all from rule 30.1's `situation` condition being judged before every step. The guidance is in the worker's prompt at every step in both modes, so building it once loses only advice for mistakes the starting materials cannot predict (for example a tool output that is too large). Of the 4 rules kept in `learn-4b-rootonly150-v2`, only 30.1 had a `situation` condition, and it describes a property of the document.
- **Where:** `skill_agent.runtime.OnceSkillPolicy` (its wrapping since H5 is the full skill's; until then `ONCE_SKILL_INSTRUCTIONS`); `skill_agent.experiments` (config check, policy choice); `trace_learner.proposals.NewFeature.scope` (now always `document`, no longer a field the inspector sends); `prompts/verifier.md`.
- **Ablation:** `skill_loading: "rolling"`. Conditions already stored as `situation` (rule 30.1 in tree v21) are still judged before every step under rolling and once under `initial`; new conditions are `document` in both modes.
- **Detail:** each attempt records one skill event with `selection.policy: "initial"`, so a passing task stores one snapshot (T4's 6 become 1). The system prompt of skill arms differs from rolling runs. Not supported on SOPBench Bank, which has its own rolling policy.
- **Status:** written 2026-11-17, not yet run on the GPU.

## Decisions about the worker's harness and the baselines

These apply to every arm alike (no skill, full skill, preloaded skill, tree arms), to learning and to the checker's reruns. They follow the released code of Trace2Skill (`spreadsheet_agent/`, `src/react_agent/`) and SkillOpt's SpreadsheetBench agent, so our baselines are run the way theirs were. Results from before 2026-11-17 used none of them and are not directly comparable.

### H1. The worker sees at most 6,000 characters of each command's output
- **Decision:** a shell command's output is shown to the worker as its first 3,000 and last 3,000 characters, with a note saying how much was left out and to save long output to a file and read it with head, tail or grep. A command that prints more than the 128,000-byte hard limit is stopped and returns its first 6,000 characters with a note, as an ordinary tool result; the attempt goes on.
- **Alternatives:** whole outputs up to 128,000 bytes, and an attempt that ends when a command passes it (the behaviour until now); SkillOpt's cut at about 4,000 characters.
- **Why:** in the dev-30 pilot of 2026-11-17, 4 runs crashed: 3 when a command printed raw PDF bytes or character-level detail past 128,000 bytes (`OutputLimitError`, which the tool loop did not catch), and 1 when a 65,000-character dump of the PDF pushed the conversation past the 4B's 32k context (HTTP 400). Each of those documents passed in other arms, and the no-skill worker ran such commands most often; the harness, not the arm, was at fault. Trace2Skill cuts every observation to the same 3,000 + 3,000 characters.
- **Where:** `skill_agent.pdf_session` (`OBSERVATION_CHARS`, `cut_output`, `ShellTool.execute`, `OutputLimitError.partial`); `VenvPDFSession(observation_chars=...)`; `VarexAdapter.open_session`.
- **Ablation:** none as a config switch; `observation_chars=None` in code shows whole outputs.
- **Detail:** the inspector's shell does not use this cut (`VarexHooks.open_inspection` passes `None`); its tools keep their own 12,000-character cut (I5). It does gain the non-fatal hard limit.
- **Status:** written 2026-11-17, not yet run on the GPU.

### H2. The worker gets 30 steps
- **Decision:** `run.max_calls` is 30 in the VAREX configs, for evaluation, learning and the checker's reruns.
- **Alternatives:** 20 (until now); Trace2Skill's 100.
- **Why:** in the dev-30 pilot, 12 runs ran out of 20 steps, almost all in skill arms, and they were still exploring (no command repeated more than twice; 11 of the 12 documents passed in another arm). Finished runs took a median of 8 to 10 steps. Each step adds about 500 tokens (700 to 950 for the heaviest runs) to a skill arm's start of about 5,500, so 30 steps fit the 4B's 32k context and 40 would not for heavy runs; Trace2Skill's 100 assumes larger models with longer contexts. SkillOpt uses 30.
- **Where:** `run.max_calls` and `validation.max_calls` in `examples/agent/varex*.json` and `examples/learning/varex.learner.json`; the server configs set it the same way. The code default stays 20 so older configs keep their run IDs.
- **Ablation:** `run.max_calls: 20`.
- **Status:** written 2026-11-17, not yet run on the GPU.

### H3. A cut-off attempt is scored on the answer it left
- **Decision:** when a VAREX attempt ends without finishing (out of steps, out of time, or an error), its `answer.json`, if present and valid, is still collected and scored. Its status still records how it ended (`budget_exhausted`, `failed`), so such passes can be counted and reported per arm.
- **Alternatives:** score only attempts that finish on their own (the behaviour until now), so a written answer counted as all wrong.
- **Why:** Trace2Skill scores the output folder in a separate step that never looks at how a run ended, and SkillOpt scores the output file whenever its loop stops; a missing file is wrong in both. Ours was stricter than both: in the pilot, about 4 of the 12 out-of-steps runs had already written `answer.json` and scored zero, 11 of the 12 in skill arms.
- **Where:** `VenvPDFSession.collect_on_failure` (`skill_agent/pdf_session.py`), which `run_episode` already honours; `VarexAdapter._score` now scores any record with a prediction. Learning's pass/fail (`VarexHooks.outcomes`) and the checker go through the same scoring.
- **Ablation:** none as a config switch.
- **Detail:** in learning, an attempt that ran out of steps with a fully right answer now counts as passed, so it becomes a stored success rather than going to the inspector.
- **Status:** written 2026-11-17, not yet run on the GPU.

### H4. The full-skill baseline is the preloaded skill
- **Decision:** a new condition, `preloaded`: the whole `SKILL.md` (without its YAML header) is in the system prompt once, under the "Relevant Skill" wording of Trace2Skill's preloaded agent ("What are Skills?", "Mandatory Skill Usage Protocol", `<skill_content>`), with a line naming the skill's other files in the working folder (K1). The worker gets no `request_skill` tool, no `<skill_catalog>`, and none of the tree arms' skill instructions or learned-rules text. The `full` arm stays, as a control with the tree arms' interface and the whole skill.
- **Alternatives:** the `full` arm alone as the skill baseline (until now).
- **Why:** the `full` arm shares the tree arms' interface: a `request_skill` tool that only acknowledges requests, a catalog, instructions about `<learned_rules>` blocks that never appear, and the skill re-sent before every step. That is the right control for "the tree against the same skill given whole", but it is not the skill as Trace2Skill and SkillOpt give it, and for a 4B model the extra text may matter. A reviewer could call it a weakened baseline.
- **Where:** `skill_agent.runtime` (`PreloadedSkillPolicy`, `PRELOADED_SKILL_SECTION`, `preloaded_skill_section`, `run_episode`); `skill_agent.experiments` (`CONDITIONS`, `make_resolvers`, `condition_protocols`: `preloaded-v1`); `skill_agent.reporting`.
- **Ablation:** leave `preloaded` out of `run.conditions`.
- **Detail:** configs that omit `run.conditions` still run none, full and reduced, in their original rotation.
- **Decision (2026-11-17, later):** the `full` arm is retired, not run again. The `preloaded` condition is **the** full-skill baseline and is labelled "Full skill" in every report; old `full` results are labelled "Full skill (tree interface, retired)". Token comparisons against the full skill use `preloaded` when a run has it. The condition keeps its internal name, so saved runs stay readable.
- **Detail (prompt comparison, dev-30 pilot):** the retired `full` arm showed `SKILL.md` with its YAML header (8,035 characters); the root-only base tree (v0) shows the same file without the header (7,512 characters), and so does `preloaded`. Root-only base and `preloaded` therefore give the worker the same skill text; they differ only in the wrapping: `<active_skill>` with the tree arms' loading and learned-rules instructions, a `request_skill` tool and a catalog, against Trace2Skill's "Relevant Skill" section with neither tool nor catalog. A version giving the tree arms the same wrapping was at first not adopted; it was adopted the same evening as H5.
- **Status:** written 2026-11-17, not yet run on the GPU.

### H5. The tree arms show their guidance exactly as the full skill is shown
- **Decision:** with build-once loading (`skill_loading: "initial"`, K4), the tree arms put their guidance in the same "Relevant Skill" section as the full-skill baseline (H4): the same wording, the same `<skill_content>` tags, the same line naming the skill files, no `request_skill` tool and no `<skill_catalog>`. When learned rules fire, their `<learned_rules>` block opens the skill content (K2) and one more line is added to the "Mandatory Skill Usage Protocol": the learned-rules instruction of K2. With no learned rules, the root-only base tree gives the worker a prompt identical, byte for byte, to the full skill's.
- **Alternatives:** the tree arms' own wrapping: an `<active_skill>` block, our loading and learned-rules instructions, an acknowledge-only `request_skill` tool and a catalog (the behaviour until now, still used by rolling loading).
- **Why:** the root-only base tree is meant to equal the full skill, so that learned MCRDR differs from the full skill only by what was learned. In the dev-30 runs of 17 Nov the two showed the same skill text but scored 14/30 and 17/30 (226 and 237 of 265 fields). The only systematic difference was the wrapping, notably the full skill's "you must follow it" protocol and the tree arms' mention of a tool and a learned-rules block that never appeared. One run each cannot say whether the gap was chance; with the same wrapping the question does not arise.
- **Where:** `skill_agent.runtime` (`OnceSkillPolicy` now `preloaded`, with `files`; `LEARNED_RULES_BULLET`; `preloaded_skill_section`); `skill_agent.experiments` passes the skill file names. `ONCE_SKILL_INSTRUCTIONS` is gone.
- **Ablation:** `skill_loading: "rolling"` keeps the old tree-arm wrapping (and refreshes every step).
- **Detail:** learning runs, the checker's reruns and test runs all go through the same policy, so rules are learned and tested under the wrapping they are shown with. The `dev30-v3-rootbase` result of 17 Nov used the old wrapping.
- **Status:** written 2026-11-17, not yet run on the GPU.

## Decisions about scoring

### P1. A VAREX document passes only when every scored field is right
- **Decision:** during learning, a VAREX document counts as passed only when the official scorer marks it a **perfect document**: every scored field correct. One wrong field makes it a failure. This one line decides:
  - which documents go to the inspector as failures;
  - whether the inspector's corrected answer counts as a fix (I2);
  - whether the checker's reruns pass (C1, C2);
  - which documents become stored successes (T4).
- **Alternatives:** a field-accuracy threshold, such as at least 90% of scored fields correct; treating each wrong field as its own failure.
- **Why:** a document with a wrong field is still a wrong extraction, and a rule cannot be kept while it leaves a field wrong. The strict line also gives the learner more failures to learn from. The cost is that the inspector may chase a single small mistake, including mistakes in the answer key itself: in the first learning run, one failure came from an answer key that was cut short.
- **Where:** `trace_learner/benchmarks/varex.py` (`VarexHooks.outcomes` and `VarexHooks.rescore`), reading the `perfect_document` metric that `VarexAdapter.score` takes from the official scorer. Each benchmark defines its pass line in its own hooks.
- **Ablation:** none. A threshold would need a new option in `VarexHooks`.
- **Detail:** this governs learning only. Evaluation still reports both field-level accuracy (`normalized_field_em`) and perfect documents.
- **Status:** in use since 2026-10-28 (`4272ccf`); recorded here 2026-11-14.

## Decisions about serving and measurement

### G1. The worker/judge model and the inspector model take turns on one GPU
- **Decision:** the 4B (worker, checker, judge) and the 27B (inspector, merge) run as two local vLLM servers that cannot both fit in GPU memory. Before each call, `SwitchingClient` wakes the call's server and puts the other to sleep (vLLM sleep mode, level 1: weights move to CPU memory). A switch waits until no call to the other server is in flight.
- **Alternatives:** both models loaded at once (forces a smaller inspector); the inspector on a hosted API (cost in two units).
- **Why:** a strong 27B inspector with a capable worker on a 24 GB GPU. Test-time evaluation uses only the worker and judge, so no switching happens there.
- **Context:** the 27B is served with a 65,536-token limit per request and 3 GiB of KV cache (84,650 tokens). With 32,768, the inspector's requests outgrew the limit: on document 50164 its prompt reached 42,786 tokens by turn 12, and the server refused the call with HTTP 400, so the failure produced no diagnosis. Until I5 (2026-11-14) inspector tool outputs were not clipped (only the prediction was), and over a 30-call budget they added up. The 4B keeps 32,768.
- **Where:** `skill_agent.models.SwitchingClient` (option `peers`); the learner's model configs; the GPU machine's `serve_27b.sh` (`--max-model-len`, `--kv-cache-memory-bytes`).
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
- **Detail (2026-11-15):** `trace_learner learn` prefixes every log line with the date and time, and every tool event records its own `seconds`. The first 150-document run's time breakdown had to be rebuilt from file modification times.
- **Where:** `skill_agent.models.OpenAICompatibleClient._post`, `skill_agent.experiments.timing_summary`; `trace_learner.__main__._Stamped`, `runtime.run_episode`.
- **Ablation:** none (measurement only).
- **Status:** in use since 2026-11-13. Records from earlier runs report null model time.

### M2. Calls, input/output tokens and latency are reported per LLM role
- **Decision:** every run reports, for each LLM role, its model calls, input tokens, output tokens and model time (the sum of per-call latency, M1):
  - **Test arms:** `skill_agent summarize` gives each arm's `worker_input_tokens`, `worker_output_tokens`, `judge_input_tokens` and `judge_output_tokens` beside the combined `input_tokens`/`output_tokens`, `model_seconds` and `judge_seconds`.
  - **Learning:** the learning report (`report.json`, `report.md`) has a "Model calls, tokens and latency" table with one row per role: worker (learning attempts), judge in those attempts, inspector (first inspections and retries), the learner's own judge calls, merge, and the checker's re-runs (worker and judge), plus a total.
  - The learner's own judge calls (stored cases, rule checks, the impact replay, `test_conditions` and its reasons) were saved nowhere before. Each now appends one line to `judge_calls.jsonl` in the learning folder: time, kind, whether usable, tokens, latency, switch time.
- **Alternatives:** cost only (the behaviour until now), which is not recorded for local models.
- **Why:** the results go to the supervisors with the latency and token cost of each method, and the learning run's cost must be broken down by role.
- **Where:** `skill_agent.learned_features.FeatureJudge(log=...)`, `IncrementalLearner.judge`; `skill_agent.experiments.role_tokens`; `trace_learner.report` (`_record_usage`, `_judge_log_usage`, `usage_table`, `incremental_report`).
- **Ablation:** none (measurement only).
- **Detail:** calls whose tokens or latency were not recorded are counted in a separate column and left out of the totals. The merge saves no latency. `judge_calls.jsonl` keeps the calls of rounds later redone, since they were made.
- **Status:** written 2026-11-17, not yet run on the GPU. Learning runs before this have no `judge_calls.jsonl`.

### S1. Sampling follows each model card, not greedy decoding
- **Decision:** every role samples with its model card's recommended thinking-mode settings and a fixed `seed: 42`:
  - **Qwen3.5-4B** (worker, checker, judge): temperature 1.0, top_p 0.95, top_k 20, min_p 0, presence_penalty 1.5 (the card's "thinking, general" preset).
  - **Qwen3.8-27B** (inspector, merge): temperature 1.0, top_p 0.95, top_k 20 (its `generation_config.json`).
- **Alternatives:** temperature 0 (greedy). It was the harness default since 2026-10-16 (`9cf9829`), set before any model was chosen, with no recorded reason. Qwen's cards advise against greedy decoding for thinking models, and the 4B looped in the smoke test under it. The 4B card's "precise coding" preset (temperature 0.6, no presence penalty) is the other candidate for the worker.
- **Why:** these are the model maker's recommendations. They match Trace2Skill's Qwen3.5 settings (`gen_config/qwen3.5_35B_122B_thinking_reasoning.json`), and the presence penalty works against repetition loops.
- **Where:** model options `temperature`, `top_p`, `top_k`, `min_p`, `presence_penalty` and `repetition_penalty` (`OpenAICompatibleClient`); the GPU's generated configs.
- **Detail (2026-11-15):** repeat *r* of a document runs its worker with `seed + r` (repeat 0 keeps 42). Until then every repeat used seed 42, so repeats of one document could be near-copies instead of independent samples. This matters for the checker when `validation.replicates` is above 1 and for the final test. The judge keeps its configured seed. Where: `skill_agent.experiments.run_experiment`.
- **Ablation:** set `temperature: 0` and drop the other options.
- **Status:** in use since 2026-11-13. It affects C4: single runs are now noisier, so revisit how many runs the checker and the final comparison use. Since J6 (2026-11-17) the judge is greedy; the worker and inspector still sample.

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
