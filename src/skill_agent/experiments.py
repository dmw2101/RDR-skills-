"""Reproducible three-arm experiments with task-boundary resume."""

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

from . import __version__
from .storage import read_json, write_json
from .contracts import fingerprint, source_fingerprint
from .resolvers import StaticSkillResolver
from .runtime import SessionContext, run_episode

CONDITIONS = ("none", "full", "reduced")


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
    if set(run) - {"output_dir", "split", "limit", "replicates", "concurrency", "max_calls", "timeout_seconds"}:
        raise ValueError("unknown run option")
    defaults = {"split": "dev", "limit": 5, "replicates": 1, "concurrency": 1,
                "max_calls": 20, "timeout_seconds": 600}
    config["run"] = run = {**defaults, **run}
    for name in ("replicates", "concurrency", "max_calls", "timeout_seconds"):
        if type(run[name]) is not int or run[name] < 1:
            raise ValueError(f"{name} must be a positive integer")
    if run["limit"] is not None and (type(run["limit"]) is not int or run["limit"] < 1):
        raise ValueError("limit must be positive or null")
    if run["split"] not in ("dev", "heldout"):
        raise ValueError("split must be dev or heldout")
    return config


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
    for name in ("datasets", "huggingface-hub", "numpy", "scipy", "rapidfuzz"):
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
    resolvers = {"none": StaticSkillResolver(skill["id"], artifact, markdown, empty=True),
                 "full": StaticSkillResolver(skill["id"], artifact, markdown),
                 "reduced": factory(skill.get("resolver", {
                     "factory": "skill_agent.resolvers:HttpSkillResolver",
                     "options": {"base_url": "http://127.0.0.1:8765"},
                 }))}
    return receipt, resolvers


async def run_experiment(config, *, resume=False):
    adapter = factory(config["benchmark"])
    receipt, resolvers = make_resolvers(config)
    run = config["run"]
    tasks = adapter.tasks(run["split"], run["limit"])
    if not tasks:
        raise ValueError("no tasks selected")
    runtime_provenance = await adapter.prepare_runtime()
    # Fail before spending inference tokens if service provenance is inconsistent.
    probe = SessionContext(config["skill"]["id"], receipt["artifact_id"], tasks[0]["task"],
                           resolvers["reduced"], receipt["source_fingerprint"],
                           expected_revision=receipt["resolver_revision"])
    await probe.load_skill()
    manifest = {"config": config, "code_revision": code_revision(), "package_version": __version__,
                "python": sys.version, "platform": platform.platform(), "git_revision": git_revision(),
                "dependency_versions": dependency_versions(),
                "benchmark_fingerprint": adapter.snapshot_fingerprint, "runtime_provenance": runtime_provenance,
                "skill_receipt": receipt, "tasks": tasks, "system_prompt": adapter.system_prompt}
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
            write_json(saved, {"run_id": run_id, **manifest, "preflight_skill": probe.events})
        semaphore = asyncio.Semaphore(run["concurrency"])

        async def attempt(task, replicate, condition):
            key = fingerprint([task["id"], replicate, condition])
            path = root / "episodes" / f"{key}.json"
            if path.exists():
                record = read_json(path)
                if record["run_id"] != run_id or (record["task_id"], record["replicate"], record["condition"]) != (task["id"], replicate, condition):
                    raise ValueError("episode does not belong to run")
                if record.get("record_sha256") != fingerprint({k: v for k, v in record.items() if k != "record_sha256"}):
                    raise ValueError("episode record changed")
                return record
            async with semaphore:
                # New model and environment instances prevent state shared across arms.
                context = SessionContext(config["skill"]["id"], receipt["artifact_id"], task["task"],
                                         resolvers[condition], receipt["source_fingerprint"],
                                         expected_revision=receipt["resolver_revision"] if condition == "reduced" else None)
                started = time.monotonic()
                try:
                    model = factory(config["model"])
                    session = await adapter.open_session(task)
                    setup_seconds = time.monotonic() - started
                    result = await run_episode(model=model, session=session, skill_context=context,
                                               system_prompt=adapter.system_prompt, max_calls=run["max_calls"],
                                               timeout_seconds=run["timeout_seconds"])
                    outcome = asdict(result)
                    outcome["setup_seconds"] = setup_seconds
                    outcome["seconds"] = time.monotonic() - started
                except Exception as exc:
                    outcome = {"status": "setup_failed", "prediction": None,
                               "error": f"{type(exc).__name__}: {exc}", "events": context.events,
                               "seconds": time.monotonic() - started}
                record = {"run_id": run_id, "task_id": task["id"], "replicate": replicate,
                          "condition": condition, **outcome}
                record["record_sha256"] = fingerprint(record)
                # Atomic record: interruptions repeat only the unfinished episode.
                temporary = path.with_suffix(".tmp")
                write_json(temporary, record)
                temporary.replace(path)
                print(f"{task['id']} rep={replicate} {condition}: {record['status']}", flush=True)
                return record

        jobs = []
        for index, task in enumerate(tasks):
            for replicate in range(run["replicates"]):
                shift = (index + replicate) % len(CONDITIONS)
                order = CONDITIONS[shift:] + CONDITIONS[:shift]
                jobs.extend(attempt(task, replicate, condition) for condition in order)
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


def usage_summary(records):
    result = {}
    for condition in CONDITIONS:
        episodes = [r for r in records if r["condition"] == condition]
        events = [e for r in episodes for e in r["events"]]
        model_events = [e for e in events if e["type"] == "model_response"]
        skill_events = [e for e in events if e["type"] == "skill"]
        skill_requests = sum(e["type"] == "skill_request" for e in events)
        model_requests = sum(e["type"] == "model_request" for e in events)
        usage = [e["usage"] for e in model_events] + [e["response"]["usage"] for e in skill_events]
        def total(name):
            values = [u.get(name) for u in usage]
            if model_requests != len(model_events) or skill_requests != len(skill_events) or any(x is None for x in values):
                return None
            return sum(values)
        result[condition] = {
            "episodes": len(episodes), "completed": sum(r["status"] == "completed" for r in episodes),
            "failures": {s: sum(r["status"] == s for r in episodes) for s in sorted({r["status"] for r in episodes})},
            "agent_model_calls": model_requests,
            "resolver_model_calls": (sum(e["response"]["usage"]["model_calls"] for e in skill_events)
                                     if skill_requests == len(skill_events) and all(e["response"]["usage"].get("model_calls") is not None for e in skill_events) else None),
            "input_tokens": total("input_tokens"), "output_tokens": total("output_tokens"),
            "cost_usd": total("cost_usd"),
            "cached_input_tokens": (sum(e["usage"]["cached_input_tokens"] for e in model_events)
                                    if model_requests == len(model_events) and all(e["usage"].get("cached_input_tokens") is not None for e in model_events) else None),
            "episode_seconds": sum(r["seconds"] for r in episodes),
            "selection_seconds": sum(e["seconds"] for e in skill_events),
            "selected_markdown_characters": sum(len(e["response"]["markdown"]) for e in skill_events),
        }
    return result


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
            deltas = sorted(difference(randomizer.choices(docs, k=len(docs)), baseline) for _ in range(samples))
            result[f"{metric}:reduced_minus_{baseline}"] = {
                "delta": difference(docs, baseline),
                "paired_document_bootstrap_95pct": [deltas[int(.025 * samples)], deltas[int(.975 * samples)]],
                "documents": len(docs), "samples": samples,
            }
    return result


def summarize(root):
    root = Path(root)
    manifest = read_json(root / "manifest.json")
    if manifest["run_id"] != fingerprint({k: v for k, v in manifest.items() if k not in ("run_id", "preflight_skill")}):
        raise ValueError("run manifest changed")
    records = [read_json(path) for path in (root / "episodes").glob("*.json")]
    expected = {(t["id"], r, c) for t in manifest["tasks"]
                for r in range(manifest["config"]["run"]["replicates"]) for c in CONDITIONS}
    actual = [(r["task_id"], r["replicate"], r["condition"]) for r in records]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError("run is incomplete; resume before summarizing")
    for record in records:
        if record["run_id"] != manifest["run_id"] or record.get("record_sha256") != fingerprint({k: v for k, v in record.items() if k != "record_sha256"}):
            raise ValueError("episode record changed")
    adapter = factory(manifest["config"]["benchmark"])
    if adapter.snapshot_fingerprint != manifest["benchmark_fingerprint"]:
        raise ValueError("benchmark changed since inference")
    scores = adapter.score(records, root / "scores")
    report = {"run_id": manifest["run_id"], "usage": usage_summary(records),
              "benchmark_summaries": scores["summaries"],
              "paired_comparisons": paired_bootstrap(scores["document_metrics"]),
              "preflight_skill": manifest["preflight_skill"],
              "protocol": "Controlled agent harness; not an exact reproduction of a published agent protocol",
              "interpretation": "Confidence intervals are not an equivalence test. Pre-register a non-inferiority margin before final evaluation."}
    write_json(root / "summary.json", report)
    return report
