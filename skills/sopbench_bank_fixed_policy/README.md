# SOPBench Bank: shared 34-case policy

`initial.md` is the exact official agent policy shared by **34 of the pinned Bank domain's 134 cases**, covering **14 target actions**. It is selected by identical policy text, not model performance. There are 23 action procedure blocks including support actions. This is an adaptation of benchmark-provided SOP instructions, not an upstream `SKILL.md` package or the complete benchmark.

The earlier representative export remains unchanged in `../sopbench_bank/`. `source.json` records settings, revision and checksums; `policy_cases.json` records only source case references (zero-based indices), not databases, customer requests or answers. The policy alone is sent to Gemini.

Status (2026-10-27): **Gemini 3.1 Flash-Lite successfully extracted the rules** through the existing Interactions adapter. The bundle contains 23 action rules, 34 fragments and 25 MCRDR nodes; [open the visualization](compiled/tree_visualisation.html). All nodes are reachable. Checked empty selection, all 23 individual actions, all 253 action pairs, bundle reload and reconstruction of all original nonblank lines when selecting every action. These are mechanical checks, not benchmark performance results. No deterministic fallback was used; API keys are not saved here.

## Build and view (PowerShell 5.1, repository root)

Install the compiler once:

```powershell
py -3.12 -m pip install -e ".[skill-compiler]"
```

Set your key for this terminal session, then export the policy, extract rules with Gemini, compile fragments/MCRDR, and generate HTML:

```powershell
$env:GEMINI_API_KEY = "YOUR_GEMINI_API_KEY"
py -3.12 scripts/build_sopbench_bank.py --model gemini-3.1-flash-lite
```

Use a model ID available to your account; `gemini-3.1-flash-lite` is the verified working choice. The script uses the existing `GeminiRuleModel` Interactions adapter, extractor/JSON validation and compiler prompt plus `extraction_prompt.md`, with at most one formatting repair. Requests use `--timeout 60` seconds by default and one transport attempt, avoiding hidden retry waits. There is no separate Bank API adapter. The schema's action vocabulary is read from explicit policy labels, not a manually invented taxonomy. Gemini returns the rules and exact source ranges. Code checks that all 23 complete procedures are present before saving accepted IR, then uses the existing fragment catalog and MCRDR compiler unchanged. If an IR is already saved, use the offline rebuild command below instead of extracting again.

### Original pipeline commands, without the wrapper

After exporting the policy, these are the same extraction, offline compilation and visualization commands used for the earlier skills. The extraction command uses the original packaged prompt (not the Bank-specific supplement); review the accepted rules before compiling. Use a fresh output filename if `ir.generated.json` already exists.

```powershell
py -3.12 -m rdr_engine.skill_compiler skills/sopbench_bank_fixed_policy/initial.md --features skills/sopbench_bank_fixed_policy/feature_schema.json --model gemini-3.1-flash-lite --output skills/sopbench_bank_fixed_policy/compiled/ir.generated.json
py -3.12 -m rdr_engine.skill_tool compile skills/sopbench_bank_fixed_policy --ir skills/sopbench_bank_fixed_policy/compiled/ir.generated.json --features skills/sopbench_bank_fixed_policy/feature_schema.json --model-id gemini-3.1-flash-lite --force
py -3.12 skills/visualise_tree.py skills/sopbench_bank_fixed_policy/compiled/tree.json
```

Open the interactive diagram; click a node to inspect its conditions and actual fragments:

```powershell
Start-Process skills/sopbench_bank_fixed_policy/compiled/tree_visualisation.html
```

The diagram is a flat MCRDR: an empty default root, one unconditional general-guidance child, and 23 independent action children. All matching siblings can report conclusions; there is no binary-tree duplication.

## Query an existing tree

Select the loan procedure, or several procedures together:

```powershell
py -3.12 -m rdr_engine.skill_tool render skills/sopbench_bank_fixed_policy --case-file skills/sopbench_bank_fixed_policy/cases/get_loan.json --show-path
py -3.12 -m rdr_engine.skill_tool render skills/sopbench_bank_fixed_policy --case-file skills/sopbench_bank_fixed_policy/cases/transfer_and_loan.json --show-path
```

Cases use `{"needed_actions": ["get_loan"]}`. The `transfer_with_helpers.json` example explicitly includes login, admin authentication and balance-check procedures. The caller must select helper procedures too when it needs them; automatic semantic dependency expansion and natural-language Bank case resolution are **not implemented**. Empty actions select only general guidance. Rendering is offline.

Rebuild from saved Gemini output without another API call:

```powershell
py -3.12 scripts/build_sopbench_bank.py --ir skills/sopbench_bank_fixed_policy/compiled/ir.generated.json --model gemini-3.1-flash-lite
```

Export only, without Gemini or compilation:

```powershell
py -3.12 scripts/build_sopbench_bank.py --prepare-only
```

For a fresh checkout, fetch the pinned Bank source first with `py -3.12 scripts/prepare_sopbench_bank.py`.

## Scope

The fixed policy, Gemini extraction, compiled MCRDR and HTML inspection view are ready. For example, selecting `get_loan` renders 399 words versus 1,699 in the original (word counts, not model tokens). The [Bank evaluation guide](../../docs/sopbench-bank-evaluation.md) describes the three-arm adapter, selector and commands; no hosted-model benchmark results are included yet.

Source: [SOPBench by Li et al.](https://github.com/Leezekun/SOPBench/tree/8790ed7406acbd22270f90bd1c4038de3fc5db8f), [paper](https://arxiv.org/abs/2503.08669). Preserve `LICENSE.txt` and `DATA_LICENSE.txt` attribution.
