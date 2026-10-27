"""Command line for verifier-driven MCRDR learning."""

import argparse
import asyncio
import json

from .pipeline import Learner, load_learner_config


def main(argv=None):
    parser = argparse.ArgumentParser(prog="trace_learner", description=(
        "Diagnose failed episodes of a collected run and add validated exceptions to the skill's MCRDR"))
    commands = parser.add_subparsers(dest="command", required=True)
    help_text = {
        "init": "Score the collected run and store passing views as cornerstones (no model calls)",
        "verify": "Run the agentic verifier on every failed episode (model calls)",
        "consolidate": "Merge validated proposals per target node (model calls if merge is configured)",
        "insert": "Insert proposals through the engine and validate them by re-running tasks (model calls)",
        "learn": "All stages in order; resumes from saved stage outputs",
        "status": "Summarize diagnoses, proposals and accepted versions (no model calls)",
    }
    for name, text in help_text.items():
        sub = commands.add_parser(name, help=text)
        sub.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    learner = Learner(load_learner_config(args.config))
    if args.command == "status":
        result = learner.status()
    else:
        stage = {"init": learner.initialize, "verify": learner.verify, "consolidate": learner.consolidate,
                 "insert": learner.insert, "learn": learner.insert}[args.command]
        asyncio.run(stage())
        result = learner.status()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
