"""Learn one batch of documents at a time, each batch run on the tree learned so far.

For every batch (default: one document) the worker runs with the current tree; passing
attempts become cornerstones, failed attempts are diagnosed by the verifier, and each
validated proposal is inserted and checked by re-running the worker. The result is the
next tree version, which the following batch runs on. This is case-by-case RDR knowledge
acquisition: a failure the earlier rules already fix never reaches the verifier.

Every stage of a round saves its output, so an interrupted run resumes where it stopped
without paying for finished model calls again. A round's working folders are discarded
once its version is committed.
"""

import asyncio
import copy
import json
import os
import shutil
from pathlib import Path
from urllib.parse import quote

from rdr_engine.learned import CONTEXT_KEY
from rdr_engine.skill_compiler import SkillBundle
from rdr_service.backend import RDRBackend
from skill_agent.experiments import episode_filename, factory, load_config, load_run, run_experiment, select_tasks
from skill_agent.learned_features import FeatureJudge, JudgeError
from skill_agent.storage import read_json, write_json

from . import __version__
from .consolidate import consolidate
from .passing import PassingTraces, affected_tasks, load_passing_record
from .pipeline import sample_views
from .proposals import Proposal
from .records import Interrupted, episode_key, skill_views, trace_steps, view_at, was_interrupted
from .validate import validate_candidate
from .verifier import verify_episode
from .workspace import SkillWorkspace, copy_bundle

RESOLVER = "skill_agent.learned_features:LearnedFeatureResolver"


class IncrementalLearner:
    def __init__(self, config):
        if config["mode"] != "incremental":
            raise ValueError("IncrementalLearner needs mode 'incremental'")
        self.config = config
        self.root = Path(config["output_dir"])
        self.run_config = load_config(config["run_config"])
        self.adapter = factory(self.run_config["benchmark"])
        self.hooks = factory(config["hooks"], adapter=self.adapter)
        self.skill_name = Path(config["skill_dir"]).resolve().name
        self.base_tree = config.get("base_tree", "compiled")
        if self.run_config["skill"].get("id") != self.skill_name:
            raise ValueError(f"skill_dir {self.skill_name!r} is not the run config's skill "
                             f"{self.run_config['skill'].get('id')!r}")
        self.tasks = select_tasks(self.adapter, self.run_config["run"])
        size = config["batch_size"]
        self.batches = [self.tasks[i:i + size] for i in range(0, len(self.tasks), size)]
        self.validation = config["validation"]
        # Design switches; see docs/learning-design-decisions.md (I3, C1).
        self.passing_examples = config["verifier"].get("passing_examples", True)
        self.changed_successes = self.validation.get("changed_successes", "rerun")
        self.judge = FeatureJudge(factory(config["judge"]["model"]))
        self._materials = {}

    # -- paths and saved state ----------------------------------------------------------------
    def version_dir(self, number):
        return self.root / "versions" / f"v{number}" / self.skill_name

    def round_dir(self, index):
        return self.root / "rounds" / f"r{index:03d}"

    def state(self):
        path = self.root / "state.json"
        return read_json(path) if path.exists() else None

    def rounds(self):
        path = self.root / "rounds.json"
        return read_json(path) if path.exists() else {}

    def materials(self, task):
        if not hasattr(self.adapter, "judge_materials"):
            return None
        if task["id"] not in self._materials:
            self._materials[task["id"]] = self.adapter.judge_materials(task)
        return self._materials[task["id"]]

    def initialize(self):
        """v0 is a copy of the original skill; cornerstones arrive with the passing episodes."""
        order = [task["id"] for task in self.tasks]
        state = self.state()
        if state is not None:
            if state.get("mode") != "incremental" or state["task_order"] != order \
                    or state["batch_size"] != self.config["batch_size"] \
                    or state.get("base_tree", "compiled") != self.base_tree:
                raise ValueError("output_dir belongs to a different learning run (documents, order, "
                                 "batch size or base tree changed); use a new output_dir")
            return state
        target = self.version_dir(0)
        if target.parent.exists():
            shutil.rmtree(target.parent)  # An interrupted initialization is rebuilt.
        copy_bundle(self.config["skill_dir"], target)
        if self.base_tree == "root_only":
            SkillBundle.reshape(target, "root_only")  # The whole skill at the root, no children yet.
        write_json(self.root / "cornerstones.json", {})
        state = {"mode": "incremental", "learner_version": __version__, "task_order": order,
                 "batch_size": self.config["batch_size"], "base_tree": self.base_tree,
                 "round": 0, "current_version": 0}
        write_json(self.root / "state.json", state)
        return state

    # -- one round ----------------------------------------------------------------------------
    async def _run_worker(self, directory, tasks, version):
        """Run the worker on this batch with the current tree; resumes finished episodes."""
        receipt_path = directory / "receipt.json"
        if not receipt_path.exists():
            write_json(receipt_path, RDRBackend(self.validation["store"]).prepare(self.version_dir(version)))
        config = copy.deepcopy(self.run_config)
        config["skill"]["receipt"] = str(receipt_path)
        config["skill"]["resolver"] = {"factory": RESOLVER, "options": {
            "base_url": self.validation["service_url"], "judge_model": self.validation["judge_model"]}}
        config["run"].update({"output_dir": str(directory / "run"), "task_ids": [task["id"] for task in tasks],
                              "conditions": ["reduced"], "limit": None})
        outcomes_path = directory / "outcomes.json"
        if outcomes_path.exists():  # Finished and scored: reuse it, even if the code changed since.
            return load_run(directory / "run")[1], read_json(outcomes_path)
        await run_experiment(config, resume=(directory / "run" / "manifest.json").exists())
        _, records = load_run(directory / "run")
        interrupted = [record for record in records if was_interrupted(record)]
        if interrupted:  # Forget those attempts so the rerun repeats only them.
            for record in interrupted:
                (directory / "run" / "episodes" / episode_filename(
                    record["task_id"], record["condition"], record["replicate"])).unlink(missing_ok=True)
            raise Interrupted(f"the worker's run of {', '.join(sorted({r['task_id'] for r in interrupted}))} "
                              f"was interrupted ({interrupted[0]['error']}); run learn again to continue")
        write_json(outcomes_path, self.hooks.outcomes(records, directory / "scores"))
        return records, read_json(outcomes_path)

    async def _add_cornerstones(self, directory, records, passed, version):
        """Copy the current tree and store every passing episode's views as cornerstones."""
        work, marker = directory / "work" / self.skill_name, directory / "work.json"
        if marker.exists():
            return work, read_json(marker)
        if work.parent.exists():
            shutil.rmtree(work.parent)
        workspace = SkillWorkspace(copy_bundle(self.version_dir(version), work))
        origins, stored, calls = {}, 0, 0
        for record in records:
            if not passed[episode_key(record)]:
                continue
            task = next(task for task in self.tasks if task["id"] == record["task_id"])
            views = sample_views(skill_views(record["events"], self.materials(task)),
                                 self.config["cornerstones_per_episode"])
            for view in views:
                if view["case"] is None:
                    continue
                accepted, judged = await workspace.accept(view["case"], view["context"], self.judge)
                stored, calls = stored + accepted, calls + judged
                context = workspace.case_for(view["case"], view["context"])[CONTEXT_KEY]
                origins.setdefault(context, []).append(episode_key(record))
        workspace.save()
        summary = {"origins": origins, "stored_views": stored, "judge_calls": calls}
        write_json(marker, summary)
        return work, summary

    def _passing_index(self, before=None):
        """Each task's newest verified passing record from finished rounds (before ``before``).

        A round's own passing attempts come first; a success the checker re-ran for a kept rule
        then points at that re-run (its ledger entry's ``refresh``), whose guidance is current.
        Derived from rounds.json and the ledgers, so redo-last-round needs no extra bookkeeping.
        """
        index = {}
        for item in sorted(self.rounds().values(), key=lambda entry: entry["round"]):
            number = item["round"]
            if before is not None and number >= before:
                continue
            directory = self.round_dir(number)
            for source, ok in item["passed"].items():
                if ok is True:
                    task, condition, replicate = source.rsplit("|", 2)
                    index[task] = {"file": directory / "run" / "episodes" / episode_filename(
                        task, condition, int(replicate)), "key": source, "round": number,
                        "version": item["version_before"]}
            ledger_path = directory / "ledger.json"
            for entry in read_json(ledger_path) if ledger_path.exists() else []:
                if entry["decision"] != "accepted":
                    continue
                for task, ref in ((entry.get("refresh") or {}).get("records") or {}).items():
                    index[task] = {"file": self.root / ref["file"], "key": ref["key"], "round": number,
                                   "version": item["version_after"]}
        return index

    def _passing_traces(self, index, version, records, passed):
        traces = PassingTraces()
        tasks = {task["id"]: task for task in self.tasks}
        current = {record["task_id"] for record in records if passed.get(episode_key(record)) is True}
        newest = sorted(self._passing_index(before=index).items(), key=lambda item: (item[1]["round"], item[0]))
        for task_id, ref in newest:
            if task_id in current or task_id not in tasks:
                continue
            record = load_passing_record(ref["file"], ref["key"])
            if record is not None:
                traces.add_round(ref["round"], ref["version"], [record], {ref["key"]: True}, tasks, self.materials)
        traces.add_round(index, version, records, passed, tasks, self.materials)
        return traces

    async def _diagnose(self, directory, records, passed, version, passing_traces):
        failed = [record for record in records if not passed[episode_key(record)]]
        folder = directory / "diagnoses"
        folder.mkdir(parents=True, exist_ok=True)
        settings = self.config["verifier"]
        semaphore = asyncio.Semaphore(settings.get("concurrency", 1))
        workspace = SkillWorkspace(self.version_dir(version))  # The tree the failing episode saw.

        async def one(record):
            path = folder / (quote(episode_key(record), safe="") + ".json")
            if path.exists():
                return read_json(path)
            async with semaphore:
                task = next(task for task in self.tasks if task["id"] == record["task_id"])
                outcome = await verify_episode(
                    model=factory(settings["model"]), task=task, record=record, hooks=self.hooks,
                    workspace=workspace, max_calls=settings.get("max_calls", 30),
                    timeout_seconds=settings.get("timeout_seconds", 900),
                    passing_traces=passing_traces if self.passing_examples else None)
            if was_interrupted(outcome):  # Not a verdict: leave it unsaved so the rerun diagnoses again.
                print(f"  inspector on {record['task_id']} rep={record['replicate']}: interrupted "
                      f"({outcome['error']}); not saved", flush=True)
                return outcome
            write_json(path, outcome)
            print(f"  inspector on {record['task_id']} rep={record['replicate']}: {outcome['status']}, "
                  f"{(outcome.get('diagnosis') or {}).get('attribution')}, eligible={outcome['eligible']}",
                  flush=True)
            return outcome

        outcomes = await asyncio.gather(*(one(record) for record in failed))
        interrupted = [outcome for outcome in outcomes if was_interrupted(outcome)]
        if interrupted:
            raise Interrupted(f"the inspector was interrupted for "
                              f"{', '.join(sorted({o['task_id'] for o in interrupted}))} "
                              f"({interrupted[0]['error']}); run learn again to continue")
        return outcomes

    async def _proposals(self, directory, outcomes):
        path = directory / "proposals.json"
        if path.exists():
            return read_json(path)["proposals"]
        eligible = [{"source": f"{o['task_id']}|{o['condition']}|{o['replicate']}",
                     "proposal": o["diagnosis"]["proposal"], "root_cause": o["diagnosis"]["root_cause"]}
                    for o in outcomes if o["eligible"]]
        model = factory(self.config["merge"]["model"]) if self.config.get("merge") else None
        proposals, usage = await consolidate(eligible, model)
        write_json(path, {"proposals": proposals, "merge_usage": usage,
                          "diagnosed": len(outcomes), "eligible": len(eligible)})
        return proposals

    def _protected(self, workspace, parent_id, exclude, origins):
        """Passing documents whose cornerstones reach the changed node, earliest first."""
        tasks = []
        for case in workspace.stored_cases():
            if parent_id in workspace.engine.predict(case).fired:
                tasks.extend(source.split("|")[0] for source in origins.get(case.get(CONTEXT_KEY), []))
        tasks = [task for task in dict.fromkeys(tasks) if task not in exclude]
        return tasks[: self.validation.get("protected_limit", 3)]

    def _feedback(self, item, entry, origins, workspace):
        """Why a proposal was rejected, with the passing situations its conditions also matched."""
        lines = [f"Your proposal {json.dumps(item['proposal'], ensure_ascii=False)} was rejected by the tree "
                 f"engine: {entry['reason']}."]
        documents = {}
        for context in (entry.get("insert") or {}).get("conflicts") or []:
            for episode in origins.get(context, []):
                documents.setdefault(episode.split("|")[0], context)
        if documents:
            lines.append("Its conditions were also judged true in situations from documents the tree already "
                         "handles correctly, so the rule would change their advice:")
            for document, context in list(documents.items())[:3]:
                text = " ".join(workspace.layer.contexts.get(context, "")[:600].split())
                lines.append(f"- document {document}: {text}")
        lines.append("Propose a rule whose conditions are judged true for THIS failing document's visible text "
                     "but false for those situations: a more specific property this document has and they lack. "
                     "Call rescore again with your corrected prediction, then submit_diagnosis.")
        return "\n".join(lines)

    def _checker_feedback(self, item, entry, workdir):
        """Why the checker rejected an inserted rule: each re-run document that failed, with an excerpt."""
        validation = entry.get("validation") or {}
        records = load_run(workdir / "run")[1] if (workdir / "run" / "manifest.json").exists() else []
        first = {}
        for record in records:
            first.setdefault(record["task_id"], record)

        def excerpt(task_id):
            record = first.get(task_id)
            if record is None:
                return "(no re-run record)"
            steps = trace_steps(record.get("events", []), limit=300)[-4:]
            text = json.dumps({"prediction": record.get("prediction"), "last_steps": steps}, ensure_ascii=False)
            return text if len(text) <= 1500 else text[:1500] + " ..."

        lines = [f"Your proposal {json.dumps(item['proposal'], ensure_ascii=False)} was inserted, but the "
                 "checker rejected it after re-running the documents it affects:"]
        for task_id, fixed in (validation.get("fixed") or {}).items():
            if not fixed:
                lines.append(f"- document {task_id}, the failure this rule was meant to fix, still fails with the "
                             f"rule. Its re-run: {excerpt(task_id)}")
        for task_id in validation.get("regressions") or []:
            lines.append(f"- document {task_id} passed before; the rule changed its advice and it now fails. "
                         f"Its re-run: {excerpt(task_id)}")
        lines.append("Revise the rule so it fixes THIS failing document (its advice may need to change) and its "
                     "conditions are false for any document it broke, or its advice no longer harms them. Call "
                     "rescore again with your corrected prediction, then submit_diagnosis.")
        return "\n".join(lines)

    async def _retry(self, directory, version, record, task, item, feedback, passing_traces):
        """One more inspector run with the rejection as feedback; returns a new proposal item or None."""
        source = item["sources"][0]
        path = directory / "diagnoses" / (quote(source, safe="") + ".retry.json")
        if path.exists():
            outcome = read_json(path)
        else:
            settings = self.config["verifier"]
            print(f"  inspector retry on {record['task_id']}: asking for a revised rule", flush=True)
            outcome = await verify_episode(
                model=factory(settings["model"]), task=task, record=record, hooks=self.hooks,
                workspace=SkillWorkspace(self.version_dir(version)), max_calls=settings.get("max_calls", 30),
                timeout_seconds=settings.get("timeout_seconds", 900), feedback=feedback,
                passing_traces=passing_traces if self.passing_examples else None)
            if was_interrupted(outcome):
                raise Interrupted(f"the inspector's retry on {record['task_id']} was interrupted "
                                  f"({outcome['error']}); run learn again to continue")
            write_json(path, outcome)
            print(f"  inspector retry on {record['task_id']}: {outcome['status']}, "
                  f"{(outcome.get('diagnosis') or {}).get('attribution')}, eligible={outcome['eligible']}",
                  flush=True)
        if not outcome["eligible"]:
            return None
        return {"sources": item["sources"], "proposal": outcome["diagnosis"]["proposal"], "retry": 1,
                "diagnosis_file": path.name}

    async def _refresh_cornerstones(self, workspace, workdir, tasks, origins):
        """After a rule is kept: swap each re-run success's stored cases for its passing re-run's views.

        Updates ``origins`` in place and returns what changed, for the ledger and the passing index.
        """
        refresh = {"records": {}, "removed": [], "added": {}, "judge_calls": 0}
        if not tasks or not (workdir / "run" / "manifest.json").exists():
            return refresh
        for record in load_run(workdir / "run")[1]:
            task_id = record["task_id"]
            if task_id not in tasks or record["replicate"] != 0 or task_id in refresh["records"]:
                continue
            source = episode_key(record)
            task = next(task for task in self.tasks if task["id"] == task_id)
            remove = [context for context, sources in origins.items()
                      if sources and {s.split("|")[0] for s in sources} == {task_id}]
            views = sample_views(skill_views(record["events"], self.materials(task)),
                                 self.config["cornerstones_per_episode"])
            _, calls, contexts = await workspace.replace_cornerstones(remove, views, self.judge)
            for context in remove:
                origins.pop(context, None)
            for context, sources in list(origins.items()):  # Shared situations keep their other tasks.
                origins[context] = [s for s in sources if s.split("|")[0] != task_id]
                if not origins[context]:
                    del origins[context]
            for context in contexts:
                origins[context] = list(dict.fromkeys(origins.get(context, []) + [source]))
            path = workdir / "run" / "episodes" / episode_filename(task_id, record["condition"], record["replicate"])
            refresh["records"][task_id] = {"file": str(path.relative_to(self.root)), "key": source}
            refresh["removed"] += remove
            refresh["added"][task_id] = contexts
            refresh["judge_calls"] += calls
        return refresh

    @staticmethod
    def _level(workspace, node_id):
        """Depth of a new rule under ``node_id``: 1 for a child of the root."""
        depth = 1
        while node_id not in (None, 0):
            node_id = workspace.engine.nodes[node_id].parent
            depth += 1
        return depth

    async def _insert(self, directory, index, proposals, records, work, origins, version, passing_traces):
        """Insert and check each proposal in order; returns the folder of the resulting tree.

        Every rejection gets one inspector retry with feedback (decision I4): the engine's (the
        conditions are not true for the failing case, or match a protected stored case) or the
        checker's (a re-run document still fails). In "rerun" mode (decision C1) a rule may change
        stored successes; the checker re-runs them, and once the rule is kept their stored cases
        are swapped for their passing re-run's (C3).
        """
        ledger_path = directory / "ledger.json"
        ledger = read_json(ledger_path) if ledger_path.exists() else []
        current = work
        for entry in ledger:  # Resume: continue from the last accepted candidate.
            if entry["decision"] == "accepted":
                current = Path(entry["candidate"])
                if entry.get("origins") is not None:  # Its re-run successes' stored cases were swapped.
                    origins.clear()
                    origins.update(entry["origins"])
        number = -1
        while number + 1 < len(proposals):
            number += 1
            item = proposals[number]
            if number < len(ledger):
                continue
            candidate = directory / "candidates" / f"c{number}" / self.skill_name
            if candidate.parent.exists():
                shutil.rmtree(candidate.parent)
            workspace = SkillWorkspace(copy_bundle(current, candidate))
            proposal = Proposal.model_validate_json(json.dumps(item["proposal"]))
            source = item["sources"][0]
            record = next(r for r in records if episode_key(r) == source)
            task = next(task for task in self.tasks if task["id"] == record["task_id"])
            diagnosis_file = item.get("diagnosis_file") or quote(source, safe="") + ".json"
            diagnosis = read_json(directory / "diagnoses" / diagnosis_file)["diagnosis"]
            view = view_at(record["events"], diagnosis["failing_turn"], self.materials(task))
            entry = {"round": index, "proposal_index": number, "sources": item["sources"],
                     "proposal": item["proposal"], "level": None, "retry_of": item.get("retry_of"),
                     "changed_successes": self.changed_successes}
            workdir = directory / "validation" / f"c{number}"
            if proposal.target_node_id < len(workspace.engine.nodes):
                entry["level"] = self._level(workspace, proposal.target_node_id)
            if view is None or view["case"] is None:
                entry.update(decision="rejected", reason="no skill view at the failing turn")
            else:
                result = await workspace.apply(proposal, view["case"], view["context"], self.judge,
                                               {"sources": item["sources"], "round": index},
                                               changed_successes=self.changed_successes)
                entry["insert"] = result.to_dict()
                if result.status != "revised":
                    entry.update(decision="rejected", reason=result.reason)
                else:
                    workspace.save()
                    entry["tree"] = workspace.summary()
                    if self.validation.get("enabled"):
                        fixed = list(dict.fromkeys(s.split("|")[0] for s in item["sources"]))
                        selection_path = workdir / "selection.json"
                        if selection_path.exists():
                            selection = read_json(selection_path)
                        else:
                            try:
                                impacted = await affected_tasks(passing_traces, SkillWorkspace(current),
                                                                workspace, self.judge, set(fixed))
                            except JudgeError as exc:
                                raise Interrupted(f"candidate impact check interrupted: {exc}") from exc
                            # Stored successes the rule now fires on (allowed in "rerun" mode).
                            changed = [task_id for task_id in dict.fromkeys(
                                source.split("|")[0] for context in result.changed
                                for source in origins.get(context, [])) if task_id not in fixed]
                            if self.changed_successes == "rerun":
                                # Only tasks whose guidance changes; unchanged ones would see identical advice.
                                protected = [task_id for task_id in dict.fromkeys(changed + impacted)
                                             if task_id not in fixed]
                            else:
                                protected = impacted or self._protected(workspace, result.parent_id, set(fixed),
                                                                        origins)
                            selection = {"fixed": fixed, "protected": protected, "impact_based": bool(impacted),
                                         "changed_documents": changed, "mode": self.changed_successes}
                            selection_path.parent.mkdir(parents=True, exist_ok=True)
                            write_json(selection_path, selection)
                        protected = selection["protected"]
                        entry["changed_documents"] = selection.get("changed_documents", [])
                        (workdir / "run" / ".running").unlink(missing_ok=True)  # We hold the learner lock.
                        report = await validate_candidate(
                            skill_dir=candidate, base_config=self.run_config, hooks=self.hooks,
                            fixed_tasks=fixed, protected_tasks=protected, settings=self.validation,
                            workdir=workdir)
                        entry["validation"] = {k: report[k] for k in ("fixed", "regressions", "passes")}
                        entry["reruns"] = report["passes"]
                        entry.update(decision="accepted" if report["accepted"] else "rejected",
                                     reason=None if report["accepted"] else "the checker's re-runs failed")
                        if report["accepted"] and self.changed_successes == "rerun" and protected:
                            # Later rules are checked against the guidance these successes get now.
                            refresh = await self._refresh_cornerstones(workspace, workdir, set(protected), origins)
                            entry["refresh"] = refresh
                            if refresh["records"]:
                                workspace.save()
                                entry["tree"] = workspace.summary()
                                # Restored on resume (see the loop above).
                                entry["origins"] = {context: list(sources) for context, sources in origins.items()}
                                for task_id, ref in refresh["records"].items():
                                    rerun = load_passing_record(self.root / ref["file"], ref["key"])
                                    if rerun is not None:
                                        passing_traces.replace_task(
                                            index, version + 1, rerun,
                                            next(t for t in self.tasks if t["id"] == task_id), self.materials)
                    else:
                        entry.update(decision="accepted", reason="engine checks only (validation disabled)")
            if entry["decision"] == "accepted":
                entry["candidate"] = str(candidate)
                current = candidate
            else:
                shutil.rmtree(candidate.parent, ignore_errors=True)
                insert = entry.get("insert") or {}
                engine_retry = bool(insert.get("conflicts")) or "not true for the failing" in (insert.get("reason") or "")
                checker_retry = "validation" in entry  # Inserted, then rejected by the checker's re-runs.
                already = any(p.get("retry") and p["sources"] == item["sources"] for p in proposals)
                if (engine_retry or checker_retry) and not item.get("retry") and len(item["sources"]) == 1 \
                        and not already:
                    feedback = (self._checker_feedback(item, entry, workdir) if checker_retry
                                else self._feedback(item, entry, origins, workspace))
                    retry = await self._retry(directory, version, record, task, item, feedback, passing_traces)
                    if retry is not None:  # Saved before the ledger entry, so a resume finds it.
                        retry["retry_of"] = number
                        proposals.append(retry)
                        saved = read_json(directory / "proposals.json")
                        write_json(directory / "proposals.json", {**saved, "proposals": proposals})
            ledger.append(entry)
            write_json(ledger_path, ledger)
            print(f"  rule {index + 1}.{number + 1} ({proposal.kind} at node {proposal.target_node_id}): "
                  f"{entry['decision']}" + (f" - {entry['reason']}" if entry.get("reason") else ""), flush=True)
        return current, ledger

    async def _round(self, index, state):
        tasks, version = self.batches[index], state["current_version"]
        directory = self.round_dir(index)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "run" / ".running").unlink(missing_ok=True)  # We hold the learner lock.
        print(f"round {index + 1}/{len(self.batches)}: {', '.join(t['id'] for t in tasks)} on v{version}", flush=True)
        records, passed = await self._run_worker(directory, tasks, version)
        work, cornerstones = await self._add_cornerstones(directory, records, passed, version)
        passing_traces = (PassingTraces() if all(passed.values()) else
                          self._passing_traces(index, version, records, passed))
        outcomes = await self._diagnose(directory, records, passed, version, passing_traces)
        proposals = await self._proposals(directory, outcomes)
        before = directory / "cornerstones_before.json"  # Lets redo-last-round undo swapped stored cases.
        if not before.exists():
            write_json(before, read_json(self.root / "cornerstones.json"))
        origins = {**read_json(self.root / "cornerstones.json")}
        for context, sources in cornerstones["origins"].items():
            origins[context] = list(dict.fromkeys(origins.get(context, []) + sources))
        final, ledger = await self._insert(directory, index, proposals, records, work, origins, version, passing_traces)

        accepted = [entry["proposal_index"] for entry in ledger if entry["decision"] == "accepted"]
        changed = bool(cornerstones["stored_views"] or accepted)
        after = version + 1 if changed else version
        if changed:
            target = self.version_dir(after)
            if target.parent.exists():
                shutil.rmtree(target.parent)  # Left by an interrupted commit of this round.
            copy_bundle(final, target)
        write_json(self.root / "cornerstones.json", origins)
        rounds = self.rounds()
        rounds[str(index)] = {"round": index, "tasks": [t["id"] for t in tasks], "version_before": version,
                              "version_after": after, "passed": passed,
                              "stored_views": cornerstones["stored_views"],
                              "diagnosed": [episode_key(r) for r in records if not passed[episode_key(r)]],
                              "proposals": len(proposals), "accepted": accepted}
        write_json(self.root / "rounds.json", rounds)
        state = {**state, "round": index + 1, "current_version": after}
        write_json(self.root / "state.json", state)
        for name in ("work", "candidates"):
            shutil.rmtree(directory / name, ignore_errors=True)
        print(f"  -> {sum(passed.values())}/{len(passed)} passed, {len(accepted)} rule(s) kept, tree v{after}",
              flush=True)
        return state

    # -- whole run ----------------------------------------------------------------------------
    async def learn(self):
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self.root / ".learning"
        _acquire(lock)
        try:
            state = self.initialize()
            for index in range(state["round"]):  # Folders left by a crash after a round's commit.
                for name in ("work", "candidates"):
                    shutil.rmtree(self.round_dir(index) / name, ignore_errors=True)
            while state["round"] < len(self.batches):
                state = await self._round(state["round"], state)
            return state
        finally:
            lock.unlink(missing_ok=True)

    def redo_last_round(self):
        """Reopen the last finished round so the next learn repeats it.

        Keeps its scored worker run and every saved inspector verdict; removes inspector
        results that were interrupted (refused calls, e.g. saved by earlier versions), its
        proposals, checker re-runs and the tree version it created. Safe to repeat.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self.root / ".learning"
        _acquire(lock)
        try:
            state, rounds = self.state(), self.rounds()
            if not state or state["round"] == 0:
                raise ValueError("no finished round to redo")
            index = state["round"] - 1
            item = rounds[str(index)]
            if item["version_after"] != state["current_version"]:
                raise ValueError("state.json and rounds.json disagree; not changing anything")
            directory = self.round_dir(index)
            removed = []
            for path in sorted((directory / "diagnoses").glob("*.json")):
                if was_interrupted(read_json(path)):
                    path.unlink()
                    removed.append(path.stem)
            for name in ("work.json", "proposals.json", "ledger.json"):
                (directory / name).unlink(missing_ok=True)
            for name in ("work", "candidates", "validation"):
                shutil.rmtree(directory / name, ignore_errors=True)
            if item["version_after"] != item["version_before"]:
                shutil.rmtree(self.version_dir(item["version_after"]).parent, ignore_errors=True)
            before = directory / "cornerstones_before.json"
            if before.exists():  # Exact, including stored cases a kept rule's re-runs swapped.
                write_json(self.root / "cornerstones.json", read_json(before))
            else:  # Rounds finished before this snapshot existed.
                episodes = set(item["passed"])
                origins = {context: [s for s in sources if s not in episodes]
                           for context, sources in read_json(self.root / "cornerstones.json").items()}
                write_json(self.root / "cornerstones.json", {c: s for c, s in origins.items() if s})
            del rounds[str(index)]
            write_json(self.root / "rounds.json", rounds)
            state = {**state, "round": index, "current_version": item["version_before"]}
            write_json(self.root / "state.json", state)
            print(f"round {index + 1} ({', '.join(item['tasks'])}) reopened on v{item['version_before']}; "
                  f"interrupted inspector results removed: {len(removed)}. Run learn to redo it; its worker "
                  "run and saved verdicts are reused.", flush=True)
            return state
        finally:
            lock.unlink(missing_ok=True)

    def status(self):
        state, rounds = self.state(), self.rounds()
        diagnoses = [read_json(p) for p in sorted((self.root / "rounds").glob("r*/diagnoses/*.json"))]
        ledger = [entry for p in sorted((self.root / "rounds").glob("r*/ledger.json")) for entry in read_json(p)]
        attributions = {}
        for outcome in diagnoses:
            name = (outcome.get("diagnosis") or {}).get("attribution", outcome["status"])
            attributions[name] = attributions.get(name, 0) + 1
        attempts = [ok for item in rounds.values() for ok in item["passed"].values()]
        return {"mode": "incremental", "batch_size": self.config["batch_size"],
                "rounds_done": state["round"] if state else 0, "rounds_total": len(self.batches),
                "current_version": state["current_version"] if state else None,
                "current_skill": str(self.version_dir(state["current_version"])) if state else None,
                "attempts": len(attempts), "passed": sum(attempts),
                "diagnoses": len(diagnoses), "attributions": attributions,
                "eligible": sum(o.get("eligible", False) for o in diagnoses),
                "proposals_tried": len(ledger), "accepted": sum(e["decision"] == "accepted" for e in ledger),
                "rejected_reasons": [e["reason"] for e in ledger if e["decision"] == "rejected"]}


def _acquire(lock):
    """One learner per output_dir. A lock left by a killed learner is taken over on Linux."""
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            pid = int(lock.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = None
        alive = pid is None or not Path("/proc").is_dir() or Path(f"/proc/{pid}").exists()
        if alive:
            raise RuntimeError(f"another learner seems to be running on this output_dir ({lock}); "
                               "if none is, delete that file and rerun") from None
        lock.unlink(missing_ok=True)
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(descriptor, str(os.getpid()).encode())
    os.close(descriptor)
