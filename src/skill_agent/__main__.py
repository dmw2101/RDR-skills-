"""PowerShell-friendly file-based experiment commands."""

import argparse
import asyncio
import json
import sys

from .benchmarks.varex import prepare_varex
from .experiments import factory, load_config, make_resolvers, run_experiment, summarize
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
        tasks = adapter.tasks(config["run"]["split"], config["run"]["limit"])
        results["benchmark"] = f"ok ({len(tasks)} selected documents)"
        try:
            results["benchmark_runtime"] = await adapter.prepare_runtime()
        except Exception as exc:
            results["benchmark_runtime"] = f"ERROR: {exc}"
    except Exception as exc:
        results["benchmark"] = f"ERROR: {exc}"
    try:
        receipt, resolvers = make_resolvers(config)
        resolved = await resolvers["reduced"].resolve(ResolveSkillRequest(
            config["skill"]["id"], tasks[0]["task"] if tasks else "Resolve this task", {}, receipt["artifact_id"]))
        if resolved.source_fingerprint != receipt["source_fingerprint"] or resolved.resolver_revision != receipt["resolver_revision"]:
            raise ValueError("service provenance differs from receipt")
        results["skill_service"] = "ok"
    except Exception as exc:
        results["skill_service"] = f"ERROR: {exc}"
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description="Controlled skill-agent experiments")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="Download pinned Flat VAREX PDFs and scorer")
    prepare.add_argument("--output", default="benchmarks/local-data/varex")
    for command in ("doctor", "run"):
        sub = commands.add_parser(command)
        sub.add_argument("--config", required=True)
        if command == "run":
            sub.add_argument("--resume", action="store_true")
    summary = commands.add_parser("summarize", help="Run official scoring and paired analysis")
    summary.add_argument("run_dir")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare_varex(args.output)
        elif args.command == "summarize":
            result = summarize(args.run_dir)
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
