"""Repeat chosen rounds of a finished incremental learning run, in a copy, from the same snapshot.

  prepare: copy the run's folder and configs under a new name (the original is never changed), roll
           the copy back to just before round --from-round with the learner's own redo-last-round
           (same tree version, same stored successes the checker had then), and delete that round and
           every later one entirely, so the worker attempts them again too. --seed sets the worker's,
           inspector's and merge model's seed (a new attempt rather than a near-copy, decision S1).
  run:     run the next --rounds rounds exactly as learning does (worker, inspector, retry, checker),
           then stop. Exit 3 when interrupted, like `trace_learner learn`; running it again continues.

    python scripts/fork_learning_run.py prepare --config SRC.learner.json --name NEW --from-round 14 --seed 43
    python scripts/fork_learning_run.py run --config NEW.learner.json --rounds 2
"""

import argparse
import asyncio
import contextlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from skill_agent.learned_features import JudgeError  # noqa: E402
from trace_learner.__main__ import _Stamped  # noqa: E402
from trace_learner.incremental import IncrementalLearner, _acquire  # noqa: E402
from trace_learner.pipeline import load_learner_config  # noqa: E402
from trace_learner.records import Interrupted  # noqa: E402


def _write_new(path, data):
    if path.exists():
        raise SystemExit(f"{path} exists; not overwriting")
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print("wrote", path)


def prepare(args):
    source_path = Path(args.config).resolve()
    source = json.loads(source_path.read_text(encoding="utf-8"))
    run = json.loads(Path(source["run_config"]).read_text(encoding="utf-8"))
    old_root, configs = Path(source["output_dir"]), source_path.parent
    new_root = old_root.parent / args.name
    if new_root.exists():
        raise SystemExit(f"{new_root} exists; choose another --name")
    if (old_root / ".learning").exists():
        raise SystemExit(f"{old_root} is being learned on right now")
    run["run"]["output_dir"] = str(Path(run["run"]["output_dir"]).parent / args.name)
    learner = {**source, "name": args.name, "output_dir": str(new_root),
               "run_config": str(configs / f"{args.name}.json")}
    if args.seed is not None:
        run["model"]["options"]["seed"] = args.seed  # The worker, in learning attempts and checker re-runs.
        for role in ("verifier", "merge"):
            if learner.get(role, {}).get("model"):
                learner[role]["model"]["options"]["seed"] = args.seed
    _write_new(configs / f"{args.name}.json", run)
    _write_new(configs / f"{args.name}.learner.json", learner)
    shutil.copytree(old_root, new_root)
    print("copied", old_root, "->", new_root)

    copy = IncrementalLearner(load_learner_config(configs / f"{args.name}.learner.json"))
    keep = args.from_round - 1  # Rounds 1..keep stay as they were.
    if not 0 <= keep < copy.state()["round"]:
        raise SystemExit(f"--from-round must be between 1 and {copy.state()['round']}")
    while copy.state()["round"] > keep:
        copy.redo_last_round()
    for directory in sorted((new_root / "rounds").glob("r*")):
        if int(directory.name[1:]) >= keep:  # The attempt itself is redone too, not reused.
            shutil.rmtree(directory)
    state = copy.state()
    print(f"ready: round {state['round'] + 1} next, on tree v{state['current_version']}; "
          f"{len(json.loads((new_root / 'cornerstones.json').read_text(encoding='utf-8')))} stored situations")


async def run_rounds(learner, count):
    lock = learner.root / ".learning"
    _acquire(lock)
    try:
        state = learner.state()
        last = min(state["round"] + count, len(learner.batches))
        for index in range(state["round"]):
            for name in ("work", "candidates"):  # As learn(): folders left by a crash after a commit.
                shutil.rmtree(learner.round_dir(index) / name, ignore_errors=True)
        while state["round"] < last:
            state = await learner._round(state["round"], state)
        return state
    finally:
        lock.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    first = commands.add_parser("prepare")
    first.add_argument("--config", required=True)
    first.add_argument("--name", required=True)
    first.add_argument("--from-round", type=int, required=True, help="1-based, as the learning log counts")
    first.add_argument("--seed", type=int)
    second = commands.add_parser("run")
    second.add_argument("--config", required=True)
    second.add_argument("--rounds", type=int, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args)
        return
    learner = IncrementalLearner(load_learner_config(args.config))
    # Rounds stop after --rounds; a restart after an interruption continues to the same end.
    target_file = learner.root / "fork_target.json"
    if not target_file.exists():
        target_file.write_text(json.dumps({"stop_after_round": learner.state()["round"] + args.rounds}),
                               encoding="utf-8")
    remaining = json.loads(target_file.read_text(encoding="utf-8"))["stop_after_round"] - learner.state()["round"]
    try:
        with contextlib.redirect_stdout(_Stamped(sys.stdout)):
            state = asyncio.run(run_rounds(learner, max(remaining, 0)))
    except (Interrupted, JudgeError) as exc:
        print(f"\nINTERRUPTED: {exc}\nRun the same command again to continue.", flush=True)
        raise SystemExit(3) from None
    print(json.dumps(learner.status(), indent=2))
    print(f"stopped after round {state['round']}")


if __name__ == "__main__":
    main()
