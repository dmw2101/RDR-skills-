"""Behavioural check of a candidate tree: re-run the fixed task and protected passing tasks."""

import copy
from pathlib import Path

from rdr_service.backend import RDRBackend
from skill_agent.experiments import load_run, run_experiment
from skill_agent.storage import write_json

from .records import episode_key


def majority(values):
    return sum(values) * 2 > len(values)


async def validate_candidate(*, skill_dir, base_config, hooks, fixed_tasks, protected_tasks, settings,
                             workdir):
    """Accept when every fixed task passes in most replicates and no protected task stops passing.

    The skill service at settings["service_url"] must serve settings["store"]; snapshots
    prepared here become visible to it without a restart.
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    receipt = RDRBackend(settings["store"]).prepare(skill_dir)
    write_json(workdir / "receipt.json", receipt)
    config = copy.deepcopy(base_config)
    config["skill"]["receipt"] = str(workdir / "receipt.json")
    config["skill"]["resolver"] = {
        "factory": "skill_agent.learned_features:LearnedFeatureResolver",
        "options": {"base_url": settings["service_url"], "judge_model": settings["judge_model"]},
    }
    config["run"].update({
        "output_dir": str(workdir / "run"), "task_ids": list(dict.fromkeys(fixed_tasks + protected_tasks)),
        "conditions": ["reduced"], "replicates": settings.get("replicates", 2), "limit": None,
        "max_calls": settings.get("max_calls", config["run"]["max_calls"]),
        "timeout_seconds": settings.get("timeout_seconds", config["run"]["timeout_seconds"]),
    })
    # An interrupted validation resumes its finished episodes instead of refusing the folder.
    await run_experiment(config, resume=(workdir / "run" / "manifest.json").exists())
    _, records = load_run(workdir / "run")
    outcomes = hooks.outcomes(records, workdir / "scores")
    passes = {}
    for record in records:
        passes.setdefault(record["task_id"], []).append(outcomes[episode_key(record)])
    fixed = {task: majority(passes[task]) for task in fixed_tasks}
    regressions = [task for task in protected_tasks if not majority(passes[task])]
    accepted = all(fixed.values()) and not regressions
    report = {"accepted": accepted, "receipt": receipt, "fixed": fixed, "regressions": regressions,
              "passes": passes, "replicates": config["run"]["replicates"]}
    write_json(workdir / "validation.json", report)
    return report
