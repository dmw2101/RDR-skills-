"""PowerShell-friendly file-based experiment commands."""

import argparse
import asyncio
import json
import sys

from .benchmarks.varex import prepare_varex
from .experiments import (factory, inspect_skill_requests, load_config, make_resolvers,
                          rename_episode_files, run_experiment, selected_conditions, select_tasks, summarize)
from .contracts import ResolveSkillRequest


async def doctor(config):
    results = {}
    try:
        factory(config["model"])
        results["model_configuration"] = "ok (no inference call made)"
    except Exception as exc:
        results["model_configuration"] = f"ERROR: {exc}"
    tasks = []
    try:
        adapter = factory(config["benchmark"])
        tasks = select_tasks(adapter, config["run"])
        results["benchmark"] = f"ok ({len(tasks)} selected documents)"
        try:
            results["benchmark_runtime"] = await adapter.prepare_runtime()
        except Exception as exc:
            results["benchmark_runtime"] = f"ERROR: {exc}"
    except Exception as exc:
        results["benchmark"] = f"ERROR: {exc}"
    if selected_conditions(config["run"]) == ("none",):
        results["skill_loading"] = "skill-blind-v1: task and execution tools only"
        results["skill_service"] = "not required for a no-skill-only run"
        return results
    try:
        receipt, resolvers = make_resolvers(config)
        resolved = await resolvers["reduced"].resolve(ResolveSkillRequest(
            config["skill"]["id"], tasks[0]["task"] if tasks else "Resolve this task", {}, receipt["artifact_id"]))
        if resolved.source_fingerprint != receipt["source_fingerprint"] or resolved.resolver_revision != receipt["resolver_revision"]:
            raise ValueError("service provenance differs from receipt")
        results["skill_service"] = "ok"
        results["skill_capabilities"] = resolved.capabilities
        results["skill_loading"] = (
            "automatic before every model call; task first, then last three assistant turns; "
            "request_skill only acknowledges requests"
            if config["run"].get("skill_loading") == "rolling" else
            "model-initiated only; no skill is injected before the first model call")
    except Exception as exc:
        results["skill_service"] = f"ERROR: {exc}"
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description="Controlled skill-agent experiments")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="Download pinned Flat VAREX PDFs and scorer")
    prepare.add_argument("--output", default="benchmarks/local-data/varex")
    selection = prepare.add_mutually_exclusive_group()
    selection.add_argument("--dev-limit", type=int, help="Prepare only the first N deterministic dev PDFs (1-30)")
    selection.add_argument("--task-ids", nargs="+", help="Download only these exact document IDs from the dev split")
    for command in ("doctor", "run"):
        sub = commands.add_parser(command)
        sub.add_argument("--config", required=True)
        if command == "run":
            sub.add_argument("--resume", action="store_true")
    summary = commands.add_parser("summarize", help="Run official scoring and paired analysis")
    summary.add_argument("run_dir")
    inspect = commands.add_parser("inspect-skills", help="Show actual model skill calls and selected views, even during a run")
    inspect.add_argument("run_dir")
    rename = commands.add_parser("rename-episodes", help="Give existing episode files document/condition names")
    rename.add_argument("run_dir")
    report = commands.add_parser("report", help="Pilot-deck metrics across summarized runs and engine series")
    report.add_argument("--spec", required=True, help="JSON file mapping series and runs to run folders")
    report.add_argument("--output", help="Output folder (default: report/ beside the spec)")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare_varex(args.output, dev_limit=args.dev_limit, task_ids=args.task_ids)
        elif args.command == "summarize":
            result = summarize(args.run_dir)
        elif args.command == "rename-episodes":
            result = rename_episode_files(args.run_dir)
        elif args.command == "report":
            from .reporting import write_report
            result = write_report(args.spec, args.output)
        elif args.command == "inspect-skills":
            result = inspect_skill_requests(args.run_dir)
        else:
            config = load_config(args.config)
            if args.command == "doctor":
                result = asyncio.run(doctor(config))
            else:
                result = asyncio.run(run_experiment(config, resume=args.resume))
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return int(args.command == "doctor" and any(str(v).startswith("ERROR:") for v in result.values()))
    except (ValueError, OSError, RuntimeError, ImportError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
