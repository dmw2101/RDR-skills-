"""Command line for verifier-driven MCRDR learning."""

import argparse
import asyncio
import json

from .incremental import IncrementalLearner
from .pipeline import Learner, load_learner_config
from .records import Interrupted
from .report import incremental_report, learning_report

BATCH_STAGES = ("init", "verify", "consolidate", "insert")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="trace_learner", description=(
        "Diagnose failed episodes and add validated exceptions to the skill's MCRDR"))
    commands = parser.add_subparsers(dest="command", required=True)
    help_text = {
        "init": "batch mode: score the collected run and store passing views as cornerstones (no model calls)",
        "verify": "batch mode: run the agentic verifier on every failed episode (model calls)",
        "consolidate": "batch mode: merge validated proposals per target node (model calls if merge is configured)",
        "insert": "batch mode: insert proposals and validate them by re-running tasks (model calls)",
        "learn": "All stages; incremental mode runs the worker batch by batch on the tree learned so far",
        "status": "Summarize diagnoses, proposals and accepted versions (no model calls)",
        "report": "Write report.md/report.json: failures, rules, decisions and cost (no model calls)",
        "redo-last-round": "incremental mode: reopen the last finished round so learn repeats it (no model calls)",
    }
    for name, text in help_text.items():
        sub = commands.add_parser(name, help=text)
        sub.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        run(parser, args)
    except Interrupted as exc:
        print(f"\nINTERRUPTED: {exc}\nNothing unfinished was saved; rerunning the same command continues "
              "where it stopped.", flush=True)
        raise SystemExit(3) from None


def run(parser, args):
    config = load_learner_config(args.config)
    if config["mode"] == "incremental":
        if args.command in BATCH_STAGES:
            parser.error(f"'{args.command}' is a batch-mode stage; incremental mode uses 'learn'")
        learner = IncrementalLearner(config)
        if args.command == "learn":
            asyncio.run(learner.learn())
        elif args.command == "redo-last-round":
            learner.redo_last_round()
    else:
        if args.command == "redo-last-round":
            parser.error("'redo-last-round' is for incremental mode")
        learner = Learner(config)
        if args.command in (*BATCH_STAGES, "learn"):
            stage = {"init": learner.initialize, "verify": learner.verify, "consolidate": learner.consolidate,
                     "insert": learner.insert, "learn": learner.insert}[args.command]
            asyncio.run(stage())
    if args.command == "report":
        (incremental_report if config["mode"] == "incremental" else learning_report)(learner)
        print((learner.root / "report.md").read_text(encoding="utf-8"))
        return
    print(json.dumps(learner.status(), indent=2))


if __name__ == "__main__":
    main()
