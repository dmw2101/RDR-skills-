"""Reproducible experiments with selectable conditions and task-boundary resume."""

import asyncio
import hashlib
import importlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote

from . import __version__
from .storage import read_json, write_json
from .contracts import fingerprint, source_fingerprint
from .resolvers import StaticSkillResolver
from .runtime import ModelInitiatedSkillPolicy, NoSkillPolicy, RollingSkillPolicy, SessionContext, run_episode

CONDITIONS = ("none", "full", "reduced")


def selected_conditions(run):
    """Older configs/manifests retain all three conditions when omitted."""
    conditions = run.get("conditions", list(CONDITIONS))
    if (not isinstance(conditions, list) or not conditions
            or any(not isinstance(c, str) or c not in CONDITIONS for c in conditions)
            or len(conditions) != len(set(conditions))):
        raise ValueError("conditions must be a nonempty list of unique none/full/reduced values")
    return tuple(conditions)


def episode_filename(task_id, condition, replicate=0):
    """Readable, portable names with the original identity hash for uniqueness."""
    if not isinstance(task_id, str) or not task_id or condition not in CONDITIONS:
        raise ValueError("expected a document ID and none/full/reduced condition")
    if type(replicate) is not int or replicate < 0:
        raise ValueError("replicate must be a nonnegative integer")
    key = fingerprint([task_id, replicate, condition])
    return f"{quote(task_id, safe='-_.()')}_{condition}_{key}.json"


def rename_episode_files(root):
    """Rename saved episodes without changing records, hashes, or scores."""
    root = Path(root)
    if (root / ".running").exists():
        raise ValueError("stop the run before renaming episode files")
    manifest = read_json(root / "manifest.json")
    directory = root / "episodes"
    if not directory.is_dir():
        raise ValueError("episode directory does not exist")
    planned, names = [], set()
    for path in sorted(directory.glob("*.json")):
        record = read_json(path)
        if record["run_id"] != manifest["run_id"]:
            raise ValueError("episode does not belong to run")
        if record.get("record_sha256") != fingerprint({k: v for k, v in record.items() if k != "record_sha256"}):
            raise ValueError("episode record changed")
        name = episode_filename(record["task_id"], record["condition"], record["replicate"])
        if name.casefold() in names:
            raise ValueError("episode filenames collide; no files renamed")
        names.add(name.casefold())
        target = directory / name
        if target != path:
            if target.exists():
                raise ValueError(f"episode destination already exists: {name}; no files renamed")
            planned.append((path, target))
    for path, target in planned:
        path.rename(target)
    return {"output_dir": str(root), "episodes": len(names), "renamed": len(planned)}


def factory(spec, **overrides):
    """Trusted configuration may register any implementation of the same interface."""
    module, name = spec["factory"].split(":", 1)
    return getattr(importlib.import_module(module), name)(**{**spec.get("options", {}), **overrides})


def load_config(path):
    config = read_json(path)
    required = {"version", "model", "benchmark", "skill", "run"}
    if set(config) != required or config["version"] != 1:
        raise ValueError("expected v1 config with model, benchmark, skill, run")
    run = config["run"]
    if set(run) - {"output_dir", "split", "limit", "task_ids", "replicates", "concurrency", "max_calls", "timeout_seconds", "skill_loading", "conditions"}:
        raise ValueError("unknown run option")
    defaults = {"split": "dev", "limit": 5, "replicates": 1, "concurrency": 1,
                "max_calls": 20, "timeout_seconds": 600, "skill_loading": "model_initiated"}
    config["run"] = run = {**defaults, **run}
    selected_conditions(run)
    for name in ("replicates", "concurrency", "max_calls", "timeout_seconds"):
        if type(run[name]) is not int or run[name] < 1:
            raise ValueError(f"{name} must be a positive integer")
    if run["limit"] is not None and (type(run["limit"]) is not int or run["limit"] < 1):
        raise ValueError("limit must be positive or null")
    if run["split"] not in ("dev", "heldout"):
        raise ValueError("split must be dev or heldout")
    if run["skill_loading"] not in ("model_initiated", "rolling"):
        raise ValueError("skill_loading must be model_initiated or rolling")
    if run.get("task_ids") is not None:
        ids = run["task_ids"]
        if (not isinstance(ids, list) or not ids
                or any(not isinstance(value, str) or not value.strip() for value in ids)
                or len(ids) != len(set(ids))):
            raise ValueError("task_ids must be a nonempty list of unique document IDs")
    return config


def select_tasks(adapter, run):
    """Explicit IDs select an exact subset of the requested split, overriding limit."""
    if run.get("task_ids") is None:
        return adapter.tasks(run["split"], run["limit"])
    available = {task["id"]: task for task in adapter.tasks(run["split"], None)}
    missing = [name for name in run["task_ids"] if name not in available]
    if missing:
        raise ValueError(f"document IDs are not in the prepared {run['split']} split: {missing}")
    return [available[name] for name in run["task_ids"]]


def code_revision():
    root = Path(__file__).parent
    return fingerprint({p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in sorted(root.rglob("*.py"))})


def git_revision():
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def dependency_versions():
    versions = {}
    for name in ("datasets", "huggingface-hub", "numpy", "scipy", "rapidfuzz",
                 "transformers", "torch", "torchvision", "tokenizers"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def make_resolvers(config):
    skill = config["skill"]
    receipt = read_json(skill["receipt"])
    markdown = Path(skill["full_markdown"]).read_bytes().decode("utf-8")
    if source_fingerprint(markdown) != receipt["source_fingerprint"]:
        raise ValueError("full Markdown does not match the service snapshot receipt")
    artifact = receipt["artifact_id"]
    resolvers = {"full": StaticSkillResolver(skill["id"], artifact, markdown),
                 "reduced": factory(skill.get("resolver", {
                     "factory": "skill_agent.resolvers:HttpSkillResolver",
                     "options": {"base_url": "http://127.0.0.1:8765"},
                 }))}
    return receipt, resolvers


async def run_experiment(config, *, resume=False):
    adapter = factory(config["benchmark"])
    run = config["run"]
    conditions = selected_conditions(run)
    tasks = select_tasks(adapter, run)
    if not tasks:
        raise ValueError("no tasks selected")
    names = [episode_filename(task["id"], condition, replicate).casefold()
             for task in tasks for replicate in range(run["replicates"]) for condition in conditions]
    if len(names) != len(set(names)):
        raise ValueError("task IDs produce colliding episode filenames")
    runtime_provenance = await adapter.prepare_runtime()
    receipt, resolvers, probe = None, {}, None
    if any(condition != "none" for condition in conditions):
        receipt, resolvers = make_resolvers(config)
        # Fail before spending inference tokens if service provenance is inconsistent.
        probe = SessionContext(config["skill"]["id"], receipt["artifact_id"], tasks[0]["task"],
                               resolvers["reduced"], receipt["source_fingerprint"],
                               expected_revision=receipt["resolver_revision"])
        if hasattr(resolvers["reduced"], "preflight"):
            from .contracts import ResolveSkillRequest
            resolved = await resolvers["reduced"].preflight(ResolveSkillRequest(
                probe.skill_id, probe.task, {}, probe.artifact_id))
            if (resolved.artifact_id != probe.artifact_id
                    or resolved.source_fingerprint != probe.expected_source
                    or resolved.resolver_revision != probe.expected_revision):
                raise ValueError("skill service provenance differs from receipt")
            probe.capabilities = resolved.capabilities
            probe.events.append({"type": "preflight", "response": resolved.to_dict()})
        else:
            await probe.load_skill()
    manifest = {"config": config, "code_revision": code_revision(), "package_version": __version__,
                "python": sys.version, "platform": platform.platform(), "git_revision": git_revision(),
                "dependency_versions": dependency_versions(),
                "benchmark_fingerprint": adapter.snapshot_fingerprint, "runtime_provenance": runtime_provenance,
                "skill_receipt": receipt, "skill_capabilities": probe.capabilities if probe else [],
                "skill_loading": run.get("skill_loading", "model_initiated").replace("_", "-"),
                "condition_protocols": {c: "skill-blind-v1" if c == "none" else
                                        run.get("skill_loading", "model_initiated").replace("_", "-")
                                        for c in conditions},
                "tasks": tasks, "system_prompt": adapter.system_prompt}
    if "reduced" in resolvers and hasattr(resolvers["reduced"], "provenance"):
        manifest["skill_selector"] = resolvers["reduced"].provenance
    run_id = fingerprint(manifest)
    root = Path(run["output_dir"])
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".running"
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(descriptor)
    try:
        saved = root / "manifest.json"
        if saved.exists():
            previous = read_json(saved)
            if not resume or previous["run_id"] != run_id:
                raise ValueError("existing run differs or --resume not supplied; use a new output directory")
        else:
            if resume:
                raise ValueError("no run exists to resume")
            write_json(saved, {"run_id": run_id, **manifest, "preflight_skill": probe.events if probe else []})
        semaphore = asyncio.Semaphore(run["concurrency"])

        async def attempt(task, replicate, condition):
            directory = root / "episodes"
            path = directory / episode_filename(task["id"], condition, replicate)
            # Read old hash-based filenames too; configuration/revision checks still apply.
            legacy = directory / (fingerprint([task["id"], replicate, condition]) + ".json")
            saved_episode = path if path.exists() else legacy
            if saved_episode.exists():
                record = read_json(saved_episode)
                if record["run_id"] != run_id or (record["task_id"], record["replicate"], record["condition"]) != (task["id"], replicate, condition):
                    raise ValueError("episode does not belong to run")
                if record.get("record_sha256") != fingerprint({k: v for k, v in record.items() if k != "record_sha256"}):
                    raise ValueError("episode record changed")
                return record
            async with semaphore:
                # New model and environment instances prevent state shared across arms.
                context = None
                if condition != "none":
                    materials = None
                    if getattr(resolvers[condition], "uses_materials", False) and hasattr(adapter, "judge_materials"):
                        materials = await asyncio.to_thread(adapter.judge_materials, task)
                    context = SessionContext(config["skill"]["id"], receipt["artifact_id"], task["task"],
                                             resolvers[condition], receipt["source_fingerprint"],
                                             expected_revision=receipt["resolver_revision"] if condition == "reduced" else None,
                                             capabilities=probe.capabilities, materials=materials)
                started = time.monotonic()
                try:
                    model = factory(config["model"])
                    session = await adapter.open_session(task)
                    setup_seconds = time.monotonic() - started
                    policy = (NoSkillPolicy() if condition == "none" else
                              (adapter.rolling_skill_policy() if hasattr(adapter, "rolling_skill_policy")
                               else RollingSkillPolicy()) if run.get("skill_loading") == "rolling" else
                              ModelInitiatedSkillPolicy())
                    result = await run_episode(model=model, session=session, skill_context=context,
                                               system_prompt=adapter.system_prompt, policy=policy,
                                               max_calls=run["max_calls"],
                                               timeout_seconds=run["timeout_seconds"])
                    outcome = asdict(result)
                    outcome["setup_seconds"] = setup_seconds
                    outcome["seconds"] = time.monotonic() - started
                except Exception as exc:
                    outcome = {"status": "setup_failed", "prediction": None,
                               "error": f"{type(exc).__name__}: {exc}", "events": context.events if context else [],
                               "seconds": time.monotonic() - started}
                record = {"run_id": run_id, "task_id": task["id"], "replicate": replicate,
                          "condition": condition, **outcome}
                record["record_sha256"] = fingerprint(record)
                # Atomic record: interruptions repeat only the unfinished episode.
                temporary = path.with_suffix(".tmp")
                write_json(temporary, record)
                temporary.replace(path)
                calls = model_skill_calls(record["events"])
                print(f"{task['id']} rep={replicate} {condition}: {record['status']} "
                      f"(skill calls={calls}, follow-ups={max(0, calls - 1)}, "
                      f"auto refreshes={automatic_skill_refreshes(record['events'])})", flush=True)
                return record

        jobs = []
        for index, task in enumerate(tasks):
            for replicate in range(run["replicates"]):
                shift = (index + replicate) % len(CONDITIONS)
                order = CONDITIONS[shift:] + CONDITIONS[:shift]
                # Filter the existing rotation; preserve the retained arms' relative order.
                jobs.extend(attempt(task, replicate, condition) for condition in order if condition in conditions)
        pending = [asyncio.create_task(job) for job in jobs]
        try:
            records = await asyncio.gather(*pending)
        except BaseException:
            for job in pending:
                job.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            raise
        return {"output_dir": str(root), "episodes": len(records), "run_id": run_id}
    finally:
        lock.unlink(missing_ok=True)


def model_skill_calls(events):
    """Count issued function calls even if execution fails before a tool result is logged."""
    return sum(call.get("function", {}).get("name") == "request_skill"
               for event in events if event["type"] == "model_response"
               for call in (event["message"].get("tool_calls") or []))


def automatic_skill_refreshes(events):
    return sum(event["type"] == "skill" and event.get("selection", {}).get("policy") == "rolling"
               for event in events)


def usage_summary(records):
    result = {}
    for condition in CONDITIONS:
        episodes = [r for r in records if r["condition"] == condition]
        if not episodes:
            continue
        events = [e for r in episodes for e in r["events"]]
        model_events = [e for e in events if e["type"] == "model_response"]
        skill_events = [e for e in events if e["type"] == "skill"]
        skill_errors = [e for e in events if e["type"] == "skill_error"]
        skill_requests = sum(e["type"] == "skill_request" for e in events)
        model_requests = sum(e["type"] == "model_request" for e in events)
        usage = ([e["usage"] for e in model_events] + [e["response"]["usage"] for e in skill_events]
                 + [e["usage"] for e in skill_errors if "usage" in e])
        skill_usage_complete = (skill_requests == len(skill_events) + len(skill_errors)
                                and all("usage" in e for e in skill_errors))
        def total(name):
            values = [u.get(name) for u in usage]
            if model_requests != len(model_events) or not skill_usage_complete or any(x is None for x in values):
                return None
            return sum(values)
        result[condition] = {
            "episodes": len(episodes), "completed": sum(r["status"] == "completed" for r in episodes),
            "failures": {s: sum(r["status"] == s for r in episodes) for s in sorted({r["status"] for r in episodes})},
            "agent_model_calls": model_requests,
            "skill_requests": skill_requests,
            "automatic_skill_refreshes": automatic_skill_refreshes(events),
            "skill_refresh_calls": sum(e["type"] == "tool" and e.get("name") == "request_skill"
                                       for e in events),
            "model_skill_calls": model_skill_calls(events),
            "episodes_with_model_skill_calls": sum(model_skill_calls(r["events"]) > 0 for r in episodes),
            "model_followup_skill_calls": sum(max(0, model_skill_calls(r["events"]) - 1) for r in episodes),
            "resolver_model_calls": (sum(u["model_calls"] for u in
                                         [e["response"]["usage"] for e in skill_events]
                                         + [e["usage"] for e in skill_errors if "usage" in e])
                                     if skill_usage_complete and all(u.get("model_calls") is not None for u in
                                         [e["response"]["usage"] for e in skill_events]
                                         + [e["usage"] for e in skill_errors if "usage" in e]) else None),
            "input_tokens": total("input_tokens"), "output_tokens": total("output_tokens"),
            "cost_usd": total("cost_usd"),
            "cached_input_tokens": (sum(e["usage"]["cached_input_tokens"] for e in model_events)
                                    if model_requests == len(model_events) and all(e["usage"].get("cached_input_tokens") is not None for e in model_events) else None),
            "episode_seconds": sum(r["seconds"] for r in episodes),
            "selection_seconds": sum(e["seconds"] for e in skill_events),
            "selected_markdown_characters": sum(len(e["response"]["markdown"]) for e in skill_events),
        }
    return result


def inspect_skill_requests(root):
    """Inspect real model tool invocations, including an ongoing run; no scoring or inference."""
    root = Path(root)
    manifest = read_json(root / "manifest.json")
    if manifest["run_id"] != fingerprint({k: v for k, v in manifest.items() if k not in ("run_id", "preflight_skill")}):
        raise ValueError("run manifest changed")
    episodes = []
    for path in sorted((root / "episodes").glob("*.json")):
        record = read_json(path)
        if (record["run_id"] != manifest["run_id"] or record.get("record_sha256")
                != fingerprint({k: v for k, v in record.items() if k != "record_sha256"})):
            raise ValueError("episode record changed")
        events = record["events"]
        calls = model_skill_calls(events)
        requests = [{"request": e["request"]["task"], "context": e["request"]["context"],
                     "selection": e.get("selection"),
                     "guidance_characters": len(e["response"]["markdown"]),
                     "markdown_sha256": e["markdown_sha256"],
                     **({"selector": e["response"]["metadata"]["selector"]}
                        if "selector" in e["response"].get("metadata", {}) else {})}
                    for e in events if e["type"] == "skill"]
        episodes.append({"task_id": record["task_id"], "condition": record["condition"],
                         "replicate": record["replicate"], "status": record["status"],
                         "error": record.get("error"),
                         "model_skill_calls": calls, "model_followup_skill_calls": max(0, calls - 1),
                         "automatic_skill_refreshes": automatic_skill_refreshes(events),
                         "distinct_guidance_views": len({r["markdown_sha256"] for r in requests}),
                         "requests": requests})
    run = manifest["config"]["run"]
    expected = len(manifest["tasks"]) * run["replicates"] * len(selected_conditions(run))
    return {"output_dir": str(root), "saved_episodes": len(episodes), "expected_episodes": expected,
            "model_skill_calls": sum(e["model_skill_calls"] for e in episodes), "episodes": episodes}


def paired_bootstrap(document_metrics, *, samples=2000, seed=42):
    """Cluster by document; keep paired arms and all replicates together."""
    import random
    randomizer = random.Random(seed)
    result = {}
    for metric in sorted({row["metric"] for row in document_metrics}):
        grouped = {}
        for row in document_metrics:
            if row["metric"] != metric:
                continue
            value = grouped.setdefault((row["task_id"], row["condition"]), [0, 0])
            value[0] += row["numerator"]
            value[1] += row["denominator"]
        docs = sorted({d for d, _ in grouped})
        def difference(ids, baseline):
            def score(condition):
                return sum(grouped[(d, condition)][0] for d in ids) / sum(grouped[(d, condition)][1] for d in ids)
            return score("reduced") - score(baseline)
        for baseline in ("full", "none"):
            available = {condition for _, condition in grouped}
            if "reduced" not in available or baseline not in available:
                continue
            deltas = sorted(difference(randomizer.choices(docs, k=len(docs)), baseline) for _ in range(samples))
            result[f"{metric}:reduced_minus_{baseline}"] = {
                "delta": difference(docs, baseline),
                "paired_document_bootstrap_95pct": [deltas[int(.025 * samples)], deltas[int(.975 * samples)]],
                "documents": len(docs), "samples": samples,
            }
    return result


def load_run(root):
    """Return the verified manifest and every episode record of a complete run."""
    root = Path(root)
    manifest = read_json(root / "manifest.json")
    if manifest["run_id"] != fingerprint({k: v for k, v in manifest.items() if k not in ("run_id", "preflight_skill")}):
        raise ValueError("run manifest changed")
    records = [read_json(path) for path in sorted((root / "episodes").glob("*.json"))]
    expected = {(t["id"], r, c) for t in manifest["tasks"]
                for r in range(manifest["config"]["run"]["replicates"])
                for c in selected_conditions(manifest["config"]["run"])}
    actual = [(r["task_id"], r["replicate"], r["condition"]) for r in records]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError("run is incomplete; resume before summarizing")
    for record in records:
        if record["run_id"] != manifest["run_id"] or record.get("record_sha256") != fingerprint({k: v for k, v in record.items() if k != "record_sha256"}):
            raise ValueError("episode record changed")
    return manifest, records


def summarize(root):
    root = Path(root)
    manifest, records = load_run(root)
    adapter = factory(manifest["config"]["benchmark"])
    if adapter.snapshot_fingerprint != manifest["benchmark_fingerprint"]:
        raise ValueError("benchmark changed since inference")
    scores = adapter.score(records, root / "scores")
    report = {"run_id": manifest["run_id"], "usage": usage_summary(records),
              "condition_protocols": manifest.get("condition_protocols", {}),
              "benchmark_summaries": scores["summaries"],
              "paired_comparisons": paired_bootstrap(scores["document_metrics"]),
              "preflight_skill": manifest["preflight_skill"],
              "protocol": "Controlled agent harness; not an exact reproduction of a published agent protocol",
              "interpretation": "Confidence intervals are not an equivalence test. Pre-register a non-inferiority margin before final evaluation."}
    write_json(root / "summary.json", report)
    return report
