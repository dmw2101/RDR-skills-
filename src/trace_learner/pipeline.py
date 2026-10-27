"""Collected run -> verified diagnoses -> merged proposals -> engine-checked, validated tree versions."""

import asyncio
import json
import shutil
from pathlib import Path
from urllib.parse import quote

from rdr_engine.learned import CONTEXT_KEY
from skill_agent.contracts import source_fingerprint
from skill_agent.experiments import factory, load_run
from skill_agent.learned_features import FeatureJudge
from skill_agent.storage import read_json, write_json

from . import __version__
from .consolidate import consolidate
from .proposals import Proposal
from .records import Interrupted, episode_key, skill_views, view_at, was_interrupted
from .validate import validate_candidate
from .verifier import verify_episode
from .workspace import SkillWorkspace, copy_bundle

CONFIG_KEYS = {"version", "name", "mode", "collect_run", "run_config", "batch_size", "skill_dir", "output_dir",
               "condition", "hooks", "verifier", "judge", "merge", "cornerstones_per_episode", "validation"}
MODES = {
    # Learn once from a finished run of the original tree.
    "batch": {"collect_run"},
    # Run the worker in batches with the current tree and learn after each batch (see incremental.py).
    "incremental": {"run_config"},
}


def sample_views(views, limit):
    """At most ``limit`` views as cornerstones: evenly spaced turns, always including the first."""
    if limit and len(views) > limit:
        views = [views[round(i * (len(views) - 1) / (limit - 1))] if limit > 1 else views[0]
                 for i in range(limit)]
    return views


def load_learner_config(path):
    config = read_json(path)
    mode = config.setdefault("mode", "batch")
    if mode not in MODES:
        raise ValueError(f"mode must be one of {sorted(MODES)}")
    required = {"version", "skill_dir", "output_dir", "hooks", "verifier", "judge"} | MODES[mode]
    missing = required - set(config)
    if config.get("version") != 1 or missing or set(config) - CONFIG_KEYS:
        raise ValueError(f"expected a v1 learner config; missing {sorted(missing)}, "
                         f"unknown {sorted(set(config) - CONFIG_KEYS)}")
    config.setdefault("condition", "reduced")
    config.setdefault("merge", None)
    config.setdefault("cornerstones_per_episode", 6)
    config.setdefault("validation", {"enabled": False})
    if mode == "incremental":
        batch_size = config.setdefault("batch_size", 1)
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
    # Incremental learning runs the worker itself, so it always needs the service and store.
    if config["validation"].get("enabled") or mode == "incremental":
        for name in ("service_url", "store"):
            if name not in config["validation"]:
                raise ValueError(f"validation needs {name}")
        config["validation"].setdefault("judge_model", config["judge"]["model"])
    return config


class Learner:
    def __init__(self, config):
        self.config = config
        self.root = Path(config["output_dir"])
        self.manifest, self.records = load_run(config["collect_run"])
        adapter = factory(self.manifest["config"]["benchmark"])
        if adapter.snapshot_fingerprint != self.manifest["benchmark_fingerprint"]:
            raise ValueError("benchmark changed since the collected run")
        self.hooks = factory(config["hooks"], adapter=adapter)
        self.adapter, self._materials = adapter, {}
        self.tasks = {task["id"]: task for task in self.manifest["tasks"]}
        skill_dir = Path(config["skill_dir"]).resolve()
        self.skill_name = skill_dir.name
        receipt = self.manifest.get("skill_receipt") or {}
        if receipt.get("source_fingerprint") != source_fingerprint(
                (skill_dir / "initial.md").read_bytes().decode("utf-8")):
            raise ValueError("skill_dir is not the skill the collected run used")
        self.judge = FeatureJudge(factory(config["judge"]["model"]))

    # -- stage 1: outcomes and cornerstones ---------------------------------------------------
    def outcomes(self):
        path = self.root / "outcomes.json"
        if not path.exists():
            self.root.mkdir(parents=True, exist_ok=True)
            write_json(path, {"collect_run": self.manifest["run_id"],
                              "passed": self.hooks.outcomes(self.records, self.root / "collect-scores")})
        saved = read_json(path)
        if saved["collect_run"] != self.manifest["run_id"]:
            raise ValueError("output_dir belongs to a different collected run")
        return saved["passed"]

    def materials(self, task_id):
        """The worker's starting materials, built exactly as runs build them for the judge."""
        if not hasattr(self.adapter, "judge_materials"):
            return None
        if task_id not in self._materials:
            self._materials[task_id] = self.adapter.judge_materials(self.tasks[task_id])
        return self._materials[task_id]

    def condition_records(self):
        return [r for r in self.records if r["condition"] == self.config["condition"]]

    def state(self):
        path = self.root / "state.json"
        return read_json(path) if path.exists() else None

    def version_dir(self, number):
        return self.root / "versions" / f"v{number}" / self.skill_name

    async def initialize(self):
        """v0: the original skill plus every passing episode's views as cornerstones."""
        if self.state() is not None:
            return self.state()
        passed = self.outcomes()
        target = self.version_dir(0)
        if target.parent.exists():
            shutil.rmtree(target.parent)  # An interrupted initialization is rebuilt.
        workspace = SkillWorkspace(copy_bundle(self.config["skill_dir"], target))
        origins, accepted = {}, 0
        for record in self.condition_records():
            if not passed[episode_key(record)]:
                continue
            views = sample_views(skill_views(record["events"], self.materials(record["task_id"])),
                                 self.config["cornerstones_per_episode"])
            for view in views:
                if view["case"] is None:
                    continue
                stored, _ = await workspace.accept(view["case"], view["context"], self.judge)
                accepted += stored
                origins.setdefault(workspace.case_for(view["case"], view["context"])[CONTEXT_KEY], []).append(
                    episode_key(record))
        workspace.save()
        write_json(self.root / "cornerstones.json", origins)
        state = {"current_version": 0, "learner_version": __version__, "collect_run": self.manifest["run_id"],
                 "cornerstone_contexts": len(origins), "accepted_views": accepted}
        write_json(self.root / "state.json", state)
        return state

    # -- stage 2: verification ----------------------------------------------------------------
    async def verify(self):
        await self.initialize()
        passed = self.outcomes()
        failed = [r for r in self.condition_records() if not passed[episode_key(r)]]
        directory = self.root / "diagnoses"
        directory.mkdir(parents=True, exist_ok=True)
        settings = self.config["verifier"]
        semaphore = asyncio.Semaphore(settings.get("concurrency", 1))
        # Verifiers read the tree the failing episode actually saw.
        workspace = SkillWorkspace(self.config["skill_dir"])

        async def one(record):
            path = directory / (quote(episode_key(record), safe="") + ".json")
            if path.exists():
                return read_json(path)
            async with semaphore:
                outcome = await verify_episode(
                    model=factory(settings["model"]), task=self.tasks[record["task_id"]], record=record,
                    hooks=self.hooks, workspace=workspace, max_calls=settings.get("max_calls", 30),
                    timeout_seconds=settings.get("timeout_seconds", 900))
            if was_interrupted(outcome):  # Not a verdict: leave it unsaved so the rerun diagnoses again.
                print(f"verifying {record['task_id']} rep={record['replicate']} was interrupted "
                      f"({outcome['error']}); not saved", flush=True)
                return outcome
            write_json(path, outcome)
            print(f"verified {record['task_id']} rep={record['replicate']}: {outcome['status']}, "
                  f"{outcome.get('diagnosis', {}).get('attribution')}, eligible={outcome['eligible']}", flush=True)
            return outcome

        outcomes = await asyncio.gather(*(one(record) for record in failed))
        interrupted = [outcome for outcome in outcomes if was_interrupted(outcome)]
        if interrupted:
            raise Interrupted(f"the verifier was interrupted for "
                              f"{', '.join(sorted({o['task_id'] for o in interrupted}))} "
                              f"({interrupted[0]['error']}); run the same stage again to continue")
        return outcomes

    # -- stage 3: consolidation ----------------------------------------------------------------
    async def consolidate(self):
        path = self.root / "proposals.json"
        if path.exists():
            return read_json(path)["proposals"]
        outcomes = await self.verify()
        eligible = [{"source": f"{o['task_id']}|{o['condition']}|{o['replicate']}",
                     "proposal": o["diagnosis"]["proposal"], "root_cause": o["diagnosis"]["root_cause"]}
                    for o in outcomes if o["eligible"]]
        model = factory(self.config["merge"]["model"]) if self.config.get("merge") else None
        proposals, usage = await consolidate(eligible, model)
        write_json(path, {"proposals": proposals, "merge_usage": usage,
                          "diagnosed": len(outcomes), "eligible": len(eligible)})
        return proposals

    # -- stage 4/5: insertion and validation ---------------------------------------------------
    def _failing_view(self, source):
        record = next(r for r in self.records if episode_key(r) == source)
        diagnosis = read_json(self.root / "diagnoses" / (quote(source, safe="") + ".json"))["diagnosis"]
        return record, view_at(record["events"], diagnosis["failing_turn"], self.materials(record["task_id"]))

    def _protected(self, workspace, parent_id, exclude):
        origins = read_json(self.root / "cornerstones.json")
        tasks = []
        for case in workspace.stored_cases():
            if parent_id in workspace.engine.predict(case).fired:
                tasks.extend(source.split("|")[0] for source in origins.get(case.get(CONTEXT_KEY), []))
        tasks = [task for task in dict.fromkeys(tasks) if task not in exclude]
        return tasks[: self.config["validation"].get("protected_limit", 3)]

    async def insert(self):
        """Try each proposal in order; each accepted one becomes the next immutable version."""
        await self.initialize()
        proposals = await self.consolidate()
        ledger_path = self.root / "ledger.json"
        ledger = read_json(ledger_path) if ledger_path.exists() else []
        validation = self.config["validation"]
        for index, item in enumerate(proposals):
            if index < len(ledger):
                continue
            state = self.state()
            current = state["current_version"]
            candidate = self.root / "candidates" / f"c{index}" / self.skill_name
            if candidate.parent.exists():
                shutil.rmtree(candidate.parent)
            workspace = SkillWorkspace(copy_bundle(self.version_dir(current), candidate))
            proposal = Proposal.model_validate_json(json.dumps(item["proposal"]))
            record, view = self._failing_view(item["sources"][0])
            entry = {"index": index, "sources": item["sources"], "proposal": item["proposal"],
                     "base_version": current}
            if view is None or view["case"] is None:
                entry.update(decision="rejected", reason="no skill view at the failing turn")
            else:
                result = await workspace.apply(proposal, view["case"], view["context"], self.judge,
                                               {"sources": item["sources"], "collect_run": self.manifest["run_id"]})
                entry["insert"] = result.to_dict()
                if result.status != "revised":
                    entry.update(decision="rejected", reason=result.reason)
                else:
                    workspace.save()
                    entry["tree"] = workspace.summary()
                    if validation.get("enabled"):
                        fixed = list(dict.fromkeys(source.split("|")[0] for source in item["sources"]))
                        protected = self._protected(workspace, result.parent_id, set(fixed))
                        report = await validate_candidate(
                            skill_dir=candidate, base_config=self.manifest["config"], hooks=self.hooks,
                            fixed_tasks=fixed, protected_tasks=protected, settings=validation,
                            workdir=self.root / "validation" / f"c{index}")
                        entry["validation"] = {k: report[k] for k in ("fixed", "regressions", "passes")}
                        entry.update(decision="accepted" if report["accepted"] else "rejected",
                                     reason=None if report["accepted"] else "behavioural validation failed")
                    else:
                        entry.update(decision="accepted", reason="engine checks only (validation disabled)")
            if entry["decision"] == "accepted":
                target = self.version_dir(current + 1)
                target.parent.parent.mkdir(parents=True, exist_ok=True)
                candidate.parent.rename(target.parent)
                entry["version"] = current + 1
                write_json(self.root / "state.json", {**state, "current_version": current + 1})
            else:
                shutil.rmtree(candidate.parent, ignore_errors=True)
            ledger.append(entry)
            write_json(ledger_path, ledger)
            print(f"proposal {index} ({proposal.kind} at node {proposal.target_node_id}): "
                  f"{entry['decision']}" + (f" - {entry['reason']}" if entry.get("reason") else ""), flush=True)
        return ledger

    def status(self):
        ledger_path = self.root / "ledger.json"
        ledger = read_json(ledger_path) if ledger_path.exists() else []
        diagnoses = [read_json(p) for p in sorted((self.root / "diagnoses").glob("*.json"))] \
            if (self.root / "diagnoses").exists() else []
        attributions = {}
        for outcome in diagnoses:
            name = (outcome.get("diagnosis") or {}).get("attribution", outcome["status"])
            attributions[name] = attributions.get(name, 0) + 1
        state = self.state()
        return {"state": state,
                "current_skill": str(self.version_dir(state["current_version"])) if state else None,
                "diagnoses": len(diagnoses), "attributions": attributions,
                "eligible": sum(o.get("eligible", False) for o in diagnoses),
                "proposals_tried": len(ledger),
                "accepted": sum(e["decision"] == "accepted" for e in ledger),
                "rejected_reasons": [e["reason"] for e in ledger if e["decision"] == "rejected"]}
