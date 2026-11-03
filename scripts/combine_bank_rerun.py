"""Combine verified Bank runs while preserving both original result directories."""

import argparse
from pathlib import Path

from skill_agent.benchmarks.sopbench_bank import SOPBenchBankAdapter
from skill_agent.experiments import load_run, paired_bootstrap, usage_summary
from skill_agent.storage import write_json


AFFECTED = {
    "cancel_credit_card-014", "cancel_credit_card-015", "cancel_credit_card-016",
    "pay_bill_with_credit_card-002", "pay_bill_with_credit_card-003", "pay_bill_with_credit_card-004",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("original", type=Path)
    parser.add_argument("rerun", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--retries", type=Path, help="Optional individual 429 retry run directory")
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("combined output already exists; choose a new directory")
    old, old_records = load_run(args.original)
    new, new_records = load_run(args.rerun)
    old_ids = {task["id"] for task in old["tasks"]}
    new_ids = {task["id"] for task in new["tasks"]}
    if len(old_ids) != 34 or new_ids != AFFECTED or not new_ids <= old_ids:
        raise ValueError("expected the original 34 cases and exactly the six affected cases")
    if old["config"]["model"] != new["config"]["model"] or old["skill_receipt"] != new["skill_receipt"]:
        raise ValueError("model settings or skill snapshot differ")
    old_run, new_run = old["config"]["run"], new["config"]["run"]
    for field in ("conditions", "replicates", "concurrency", "max_calls", "timeout_seconds", "skill_loading", "split"):
        if old_run.get(field) != new_run.get(field):
            raise ValueError(f"run option differs: {field}")
    if old["config"]["skill"] != new["config"]["skill"]:
        raise ValueError("skill configurations differ")
    old_options = old["config"]["benchmark"]["options"]
    new_options = new["config"]["benchmark"]["options"]
    if {**old_options, "restore_missing_tools": True} != new_options:
        raise ValueError("unexpected benchmark configuration difference")
    old_adapter = SOPBenchBankAdapter(**old_options)
    new_adapter = SOPBenchBankAdapter(**new_options)
    if (old_adapter.snapshot_fingerprint != old["benchmark_fingerprint"] or
            new_adapter.snapshot_fingerprint != new["benchmark_fingerprint"]):
        raise ValueError("benchmark fingerprint does not match the saved run")
    case_sources = {(r["task_id"], r["condition"], r["replicate"]): "original"
                    for r in old_records if r["task_id"] not in AFFECTED}
    selected = {(r["task_id"], r["condition"], r["replicate"]): r for r in new_records}
    case_sources.update({key: "rerun" for key in selected})
    retry_ids = []
    if args.retries is not None:
        for directory in sorted(args.retries.glob("*/results")):
            retry_manifest, retry_records = load_run(directory)
            if len(retry_records) != 1 or retry_manifest["benchmark_fingerprint"] != new["benchmark_fingerprint"]:
                raise ValueError("retry must contain one episode with the restored tool catalog")
            retry = retry_records[0]
            key = (retry["task_id"], retry["condition"], retry["replicate"])
            if key not in selected or "HTTP 429 from " not in (selected[key].get("error") or ""):
                raise ValueError("retry does not replace an original 429 episode")
            options = dict(retry_manifest["config"]["model"]["options"])
            retries = options.pop("rate_limit_retries", None)
            delay = options.pop("rate_limit_delay_seconds", None)
            expected_receipt = None if retry["condition"] == "none" else new["skill_receipt"]
            if (options != new["config"]["model"]["options"] or retries != 2 or delay != 60
                    or retry_manifest["skill_receipt"] != expected_receipt):
                raise ValueError("retry settings differ beyond the rate-limit handling")
            selected[key] = retry
            case_sources[key] = "429-retry"
            retry_ids.append(retry_manifest["run_id"])
    records = [r for r in old_records if r["task_id"] not in AFFECTED] + list(selected.values())
    if len(records) != 102:
        raise ValueError("expected 102 selected episodes")
    args.output.mkdir(parents=True)
    scores = new_adapter.score(records, args.output / "scores")
    report = {
        "kind": "mixed-protocol Bank comparison: 28 original cases plus 6 rerun cases",
        "warning": "The 28 original cases had a different tool catalog. This is not a uniform 34-case benchmark run.",
        "original_run_id": old["run_id"], "rerun_id": new["run_id"],
        "retry_run_ids": retry_ids,
        "original_results": str(args.original.resolve()), "rerun_results": str(args.rerun.resolve()),
        "rerun_cases": sorted(AFFECTED),
        "episode_sources": [{"task_id": t, "condition": c, "replicate": n, "source": source}
                            for (t, c, n), source in sorted(case_sources.items())],
        "usage": usage_summary(records), "benchmark_summaries": scores["summaries"],
        "paired_comparisons": paired_bootstrap(scores["document_metrics"]),
        "protocol": "Original scores and episodes are preserved; six cases were rerun with two additional tool schemas.",
    }
    write_json(args.output / "summary.json", report)
    print(f"Saved mixed-protocol summary and scores to {args.output}")
    for summary in scores["summaries"]:
        print(f"{summary['condition']}: {summary['successes']}/{summary['cases']} passed; "
              f"median input tokens: {summary['median_input_tokens']}")


if __name__ == "__main__":
    main()
