"""Export the largest fixed Bank policy group, then extract and compile with Gemini.

Only the policy and its action-name schema go to Gemini, never task databases,
customer requests, benchmark answers, or oracle trajectories.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "benchmarks/local-data/sopbench-li"
SKILL = ROOT / "skills/sopbench_bank_fixed_policy"
REVISION = "8790ed7406acbd22270f90bd1c4038de3fc5db8f"
POLICY_SHA = "f6d3da734ea07f8e403a47895c1ec7c89dc5fa126449680f035678ec2a44d43f"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save(path: Path, data: bytes) -> None:
    """Do not silently replace a previously exported source or selection."""
    if path.exists() and path.read_bytes() != data:
        raise ValueError(f"Existing export differs; inspect it before replacing: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def save_json(path: Path, value: object) -> None:
    save(path, (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode())


def action_blocks(markdown: str) -> dict[str, tuple[int, int]]:
    """Read explicit upstream action labels; do not invent task categories."""
    lines = markdown.splitlines()
    start = lines.index("### Actions with Constraints:") + 1
    end = lines.index("### Internal Verification Functions:")
    headings = []
    for index in range(start, end):
        match = re.fullmatch(r"\* ([a-z][a-z0-9_]*):", lines[index])
        if match:
            headings.append((match.group(1), index))
    blocks = {}
    for position, (name, first) in enumerate(headings):
        last = headings[position + 1][1] - 1 if position + 1 < len(headings) else end - 1
        while not lines[last].strip():
            last -= 1
        blocks[name] = (first + 1, last + 1)
    if len(blocks) != 23:
        raise ValueError(f"Expected 23 complete action blocks, found {len(blocks)}")
    return blocks


def prepare() -> tuple[str, dict[str, tuple[int, int]]]:
    if not (SOURCE / "env/task.py").is_file():
        raise ValueError(
            "Missing upstream checkout. Run: py -3.12 scripts/import_skill_candidates.py; "
            "then git -C benchmarks/local-data/sopbench-li sparse-checkout add env data"
        )
    revision = subprocess.check_output(
        ["git", "-C", str(SOURCE), "rev-parse", "HEAD"], text=True
    ).strip()
    if revision != REVISION:
        raise ValueError("Upstream revision differs from the pinned Bank source")
    subprocess.run(["git", "-C", str(SOURCE), "diff", "--exit-code", "HEAD", "--", "env", "data"], check=True)
    sys.path.insert(0, str(SOURCE))
    from env.task import task_default_dep_full, task_initializer

    data_path = SOURCE / "data/bank_tasks.json"
    tasks = json.loads(data_path.read_text(encoding="utf-8"))
    defaults = task_default_dep_full("bank", "full", "structured", dependency_verb_dep_orig=True)
    groups = {}
    policies = {}
    for action, entries in tasks.items():
        for index, original in enumerate(entries):
            task = dict(original, user_goal=action)
            _, _, assistant, _ = task_initializer(
                "bank", task, *defaults, None, "prompt", False, "structured"
            )
            policy = (assistant["instructions"].rstrip() + "\n").encode("utf-8")
            sha = digest(policy)
            policies[sha] = policy
            groups.setdefault(sha, []).append({
                "action": action, "index": index,
                "task_sha256": digest(json.dumps(original, sort_keys=True).encode()),
            })
    selected = max(groups, key=lambda key: (len(groups[key]), key))
    if selected != POLICY_SHA or len(groups[selected]) != 34:
        raise ValueError("The expected 34-case shared policy was not reproduced")
    policy = policies[selected]
    markdown = policy.decode("utf-8")
    blocks = action_blocks(markdown)
    save(SKILL / "initial.md", policy)
    for name in ("LICENSE", "DATA_LICENSE"):
        save(SKILL / f"{name}.txt", (SOURCE / name).read_bytes())
    save_json(SKILL / "source.json", {
        "source_repository": "https://github.com/Leezekun/SOPBench",
        "source_commit": REVISION, "paper": "https://arxiv.org/abs/2503.08669",
        "kind": "Official generated SOP instructions, not an upstream SKILL.md",
        "export": "env.task.task_initializer: assistant.instructions.rstrip() + newline",
        "settings": {"domain": "bank", "default_constraint_option": "full",
                     "constraint_descr_format": "structured", "env_mode": "prompt",
                     "included_functions": None, "shuffle_func": False,
                     "dependency_verb_dep_orig": True},
        "source_data_sha256": digest(data_path.read_bytes()),
        "initial_sha256": selected, "words": len(markdown.split()),
        "total_cases": sum(map(len, tasks.values())), "distinct_policies": len(groups),
        "selected_cases": len(groups[selected]),
        "selection": "Largest group with byte-identical complete policy; no model scores used",
        "license": "CC BY 4.0 data; MIT code/documentation",
    })
    save_json(SKILL / "policy_cases.json", {
        "policy_sha256": selected, "indexing": "Zero-based index within each action's upstream task list",
        "counts_by_action": dict(sorted(Counter(case["action"] for case in groups[selected]).items())),
        "cases": groups[selected],
    })
    save_json(SKILL / "feature_schema.json", {SKILL.name: {
        "needed_actions": {
            "description": "Bank action procedures needed at this step, including any helper actions "
                           "the caller intends to execute. Select actions, not eligibility outcomes. "
                           "Multiple actions may be needed; [] selects only general guidance.",
            "multiple": True, "values": list(blocks),
        }
    }})
    for name, actions in {
        "general": [], "transfer_funds": ["transfer_funds"],
        "get_loan": ["get_loan"], "transfer_and_loan": ["transfer_funds", "get_loan"],
        "transfer_with_helpers": ["login_user", "authenticate_admin_password", "get_account_balance", "transfer_funds"],
    }.items():
        save_json(SKILL / "cases" / f"{name}.json", {"needed_actions": actions})
    print(f"Exported exact shared policy: 34/134 cases, 14 target actions, {len(blocks)} procedure blocks.", flush=True)
    return markdown, blocks


def check_complete_rules(ir, blocks, markdown):
    """Reject dropped eligibility checks or invented eligibility-based routing."""
    lines = markdown.splitlines()
    seen = set()
    for rule in ir.rules:
        if len(rule.when) != 1:
            raise ValueError(f"Expected one action-membership predicate: {rule.id}")
        predicate = rule.when[0]
        if predicate.field != "needed_actions" or predicate.op != "contains" or predicate.value not in blocks:
            raise ValueError(f"Invalid procedure selector: {rule.id}")
        action = predicate.value
        if action in seen:
            raise ValueError(f"Duplicate action rule: {action}")
        seen.add(action)
        first, last = blocks[action]
        expected = {i for i in range(first, last + 1) if lines[i - 1].strip()}
        actual = {i for span in rule.source for i in range(span.start, span.end + 1) if lines[i - 1].strip()}
        if actual != expected:
            raise ValueError(f"Rule {action} must contain its entire action block, and no other procedure")
    if seen != set(blocks):
        raise ValueError(f"Missing action rules: {sorted(set(blocks) - seen)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-only", action="store_true", help="Export policy and schema without a model call")
    parser.add_argument("--model", default="gemini-3.1-flash-lite", help="Gemini model used for extraction")
    parser.add_argument("--timeout", type=int, default=60, help="Seconds per Gemini request; one transport attempt")
    parser.add_argument("--ir", type=Path, help="Reuse an existing Gemini IR; no API call")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    markdown, blocks = prepare()
    if args.prepare_only:
        return 0
    from rdr_engine.skill_compiler import (
        FeatureSchema, GeminiRuleModel, SkillBundle, SkillIR, SkillInput, SkillRuleExtractor,
    )

    schema = FeatureSchema.from_file(SKILL / "feature_schema.json", SKILL.name)
    if args.ir:
        ir_path = args.ir.resolve()
        ir = SkillIR.model_validate_json(ir_path.read_text(encoding="utf-8"))
    else:
        ir_path = SKILL / "compiled/ir.generated.json"
        if ir_path.exists():
            raise ValueError("Gemini IR already exists; use --ir skills/sopbench_bank_fixed_policy/compiled/ir.generated.json")
        key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not key:
            raise ValueError("Set GEMINI_API_KEY in this PowerShell session before extracting rules")
        model = GeminiRuleModel(key, args.model, http_options={
            "timeout": args.timeout * 1000, "retry_options": {"attempts": 1},
        })
        extractor = SkillRuleExtractor(model, max_repairs=1)
        extractor.system_prompt += "\n\n" + (SKILL / "extraction_prompt.md").read_text(encoding="utf-8")
        print(f"Extracting complete procedures with {args.model} (at most two calls)...", flush=True)
        ir = extractor.extract(SkillInput.from_markdown(SKILL / "initial.md"), schema)
    check_complete_rules(ir, blocks, markdown)
    if not args.ir:
        save_json(ir_path, ir.model_dump())
    tree, catalog = SkillBundle.build(SKILL, ir_path=ir_path, feature_schema=schema, model_id=args.model, force=True)
    print(f"Compiled {len(ir.rules)} rules, {len(catalog.fragments)} fragments, {len(tree.nodes)} MCRDR nodes.", flush=True)
    subprocess.run([
        sys.executable, str(ROOT / "scripts/visualise_tree.py"), str(SKILL / "compiled/tree.json"),
        "--title", "SOPBench Bank: shared 34-case policy (MCRDR)",
    ], check=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        raise SystemExit(f"error: {error}") from None
