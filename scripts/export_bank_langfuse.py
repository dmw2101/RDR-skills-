"""Export the episodes selected by the combined Bank report to Langfuse.

Install separately: py -3.12 -m pip install langfuse requests
Run from the repository root: py -3.12 scripts/export_bank_langfuse.py

An export manifest is written alongside the combined report. Entries that were
sent but could not be verified are not automatically resent: inspect Langfuse
and resolve those entries manually before running again.
"""

import hashlib
import argparse
import json
import os
from pathlib import Path
import sys
import time
import uuid

import requests
from langfuse import get_client


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "experiments" / "bank-windows"
SUMMARY = DATA / "combined-bank34" / "summary.json"
MANIFEST = DATA / "combined-bank34" / "langfuse-export-manifest.json"
SOURCE_DIRS = {
    "original": DATA / "runs" / "bank34-run1" / "results" / "episodes",
    "rerun": DATA / "runs" / "bank-affected6" / "results" / "episodes",
    "429-retry": DATA / "runs" / "bank-affected6-retry429",
}
PREVIOUS_TRACE = "75f4dfe807acb5c659e032d18b01f546"
PREVIOUS_PROJECT = "cmvtsi7710019ad0gdqm6h1uz"
PREVIOUS_RUN = "0b5cc3307ccf6f2ade2d4c9c9fb57234d372773e3b040341cec253498a69f6c3"
FAILED_RUN = None
TRACE_PREFIX = "Bank"


def setting(name):
    value = os.environ.get(name)
    if not value and sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value = winreg.QueryValueEx(key, name)[0]
        except FileNotFoundError:
            pass
    if not value:
        raise RuntimeError(f"Missing {name} environment variable")
    return value


def save_manifest(manifest):
    temporary = MANIFEST.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    temporary.replace(MANIFEST)


def get_with_limits(session, url, params=None):
    for _ in range(4):
        response = session.get(url, params=params, timeout=45)
        if response.status_code != 429:
            response.raise_for_status()
            return response.json()
        delay = min(120, max(1, int(response.headers.get("Retry-After", "60"))))
        print(f"Langfuse read rate limit; waiting {delay} seconds", flush=True)
        time.sleep(delay)
    raise RuntimeError("Langfuse read rate limit persisted")


def selected_episodes():
    if FAILED_RUN is not None:
        report = json.loads((FAILED_RUN / "scores" / "scoring-result.json").read_text(encoding="utf-8"))
        selected = []
        for item in report["details"]:
            if item["condition"] not in ("full", "reduced") or item["effective_success"]:
                continue
            pattern = f"{item['task_id']}_{item['condition']}_*.json"
            hits = list((FAILED_RUN / "episodes").glob(pattern))
            if len(hits) != 1:
                raise RuntimeError(f"Expected one file for {pattern}, found {len(hits)}")
            path = hits[0]
            raw = path.read_bytes()
            episode = json.loads(raw)
            if (episode['task_id'], episode['condition'], episode['replicate'], episode['status']) != (
                item['task_id'], item['condition'], item['replicate'], 'completed'
            ):
                raise RuntimeError(f"Episode identity mismatch or incomplete: {path.name}")
            key = f"{item['task_id']}|{item['condition']}|{item['replicate']}"
            selected.append((key, {'source': 'bank34-strict-v2', 'score': item['official_evaluation']},
                             path, hashlib.sha256(raw).hexdigest(), episode))
        if len({x[0] for x in selected}) != len(selected):
            raise RuntimeError('Duplicate episode identity in scoring results')
        return selected
    report = json.loads(SUMMARY.read_text(encoding="utf-8"))
    selected = []
    for item in report["episode_sources"]:
        pattern = f"{item['task_id']}_{item['condition']}_*.json"
        hits = list(SOURCE_DIRS[item["source"]].rglob(pattern))
        if len(hits) != 1:
            raise RuntimeError(f"Expected one file for {pattern}, found {len(hits)}")
        path = hits[0]
        raw = path.read_bytes()
        episode = json.loads(raw)
        if (episode["task_id"], episode["condition"], episode["replicate"]) != (
            item["task_id"], item["condition"], item["replicate"]
        ):
            raise RuntimeError(f"Episode identity mismatch: {path.name}")
        key = f"{item['task_id']}|{item['condition']}|{item['replicate']}"
        selected.append((key, item, path, hashlib.sha256(raw).hexdigest(), episode))
    if len(selected) != 102 or len({x[0] for x in selected}) != 102:
        raise RuntimeError("Expected 102 distinct selected Bank episodes")
    return selected


def write_episode(client, episode, source, trace_id, score=None):
    events = episode["events"]
    expected = 1
    with client.start_as_current_observation(
        as_type="span", name=f"{TRACE_PREFIX} {episode['task_id']} | {episode['condition']} | imported episode",
        trace_context={"trace_id": trace_id},
        input={"task_id": episode["task_id"], "condition": episode["condition"]},
        output={"status": episode["status"], "prediction": episode.get("prediction"), "error": episode.get("error"), "benchmark_evaluation": score},
        metadata={"source": "historical episode JSON", "timing": "reconstructed at import",
                  "run_id": episode["run_id"], "episode_source": source,
                  "replicate": episode["replicate"], "protocol": "strict-v2 adapted bank" if FAILED_RUN else "mixed tool catalogs"},
    ):
        i = 0
        while i < len(events):
            event = events[i]
            kind = event.get("type", "event")
            following = events[i + 1] if i + 1 < len(events) else {}
            if kind == "skill_request" and following.get("type") == "skill":
                name = "Skill selection turn " + str(event.get("selection", {}).get("turn", "?"))
                args = dict(as_type="retriever", name=name, input=event, output=following)
                i += 2
            elif kind == "model_request" and following.get("type") == "model_response":
                usage = following.get("usage") or {}
                usage_details = {k: usage[k] for k in ("input_tokens", "output_tokens")
                                 if isinstance(usage.get(k), int)}
                args = dict(as_type="generation", name="Model turn " + str(event.get("turn", "?")),
                            model=(following.get("metadata") or {}).get("model"),
                            input={"messages": event.get("messages"), "tools": event.get("tools")},
                            output={"message": following.get("message"), "reported_usage": usage,
                                    "metadata": following.get("metadata")},
                            usage_details=usage_details or None)
                i += 2
            elif kind == "tool":
                args = dict(as_type="tool", name="Tool: " + str(event.get("name", "unknown")),
                            input=event.get("call"), output=event.get("result"))
                i += 1
            else:
                args = dict(as_type="span", name=str(kind), input=event)
                i += 1
            with client.start_as_current_observation(**args):
                pass
            expected += 1
    return expected


def visible_counts(session, base, ids):
    counts = {trace_id: 0 for trace_id in ids}
    cursor = None
    # Inspect the recent observations in pages; exports happen in small batches.
    for _ in range(20):
        params = {"limit": 1000, "fields": "core"}
        if cursor:
            params["cursor"] = cursor
        page = get_with_limits(session, base + "/api/public/v2/observations", params)
        rows = page.get("data", [])
        for row in rows:
            tid = row.get("traceId")
            if tid in counts:
                counts[tid] += 1
        cursor = page.get("meta", {}).get("cursor")
        if not cursor or all(counts.values()):
            break
    return counts


def main():
    global FAILED_RUN, MANIFEST, TRACE_PREFIX
    parser = argparse.ArgumentParser(description='Export Bank episodes to Langfuse.')
    parser.add_argument('--strict-failures', action='store_true', help='Export failed full/reduced episodes from bank34-strict-v2.')
    args = parser.parse_args()
    if args.strict_failures:
        FAILED_RUN = DATA / 'runs' / 'bank34-strict-v2' / 'results'
        MANIFEST = FAILED_RUN / 'langfuse-failures-export-manifest.json'
        TRACE_PREFIX = 'Bank STRICT-V2 FAILED'
    os.environ["LANGFUSE_PUBLIC_KEY"] = setting("LANGFUSE_PUBLIC_KEY")
    os.environ["LANGFUSE_SECRET_KEY"] = os.environ.get("LANGFUSE_SECRET_KEY") or setting("LANGFUSE_PRIVATE_KEY")
    os.environ["LANGFUSE_HOST"] = os.environ.get("LANGFUSE_HOST") or setting("LANGFUSE_BASE_URL")
    base = os.environ["LANGFUSE_HOST"].rstrip("/")
    session = requests.Session()
    session.auth = (os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"])
    projects = get_with_limits(session, base + "/api/public/projects").get("data", [])
    if len(projects) != 1:
        raise RuntimeError(f"Expected one project for these credentials; got {len(projects)}")
    project_id = projects[0]["id"]
    selected = selected_episodes()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {
        "version": 1, "host": base, "project_id": project_id, "episodes": {}}
    if manifest["host"] != base or manifest["project_id"] != project_id:
        raise RuntimeError("Export manifest belongs to a different Langfuse project")

    # The previously imported demonstration trace belongs to this project and run.
    previous_key = "transfer_funds-021|reduced|0"
    prior = next((x for x in selected if x[0] == previous_key), None)
    if (previous_key not in manifest["episodes"] and prior and project_id == PREVIOUS_PROJECT
            and prior[4]["run_id"] == PREVIOUS_RUN):
        observed = get_with_limits(session, base + "/api/public/v2/observations",
                                   {"traceId": PREVIOUS_TRACE, "limit": 100, "fields": "core"})
        if not observed.get("data"):
            raise RuntimeError("Previous example trace was not found; refusing to create a duplicate")
        manifest["episodes"][previous_key] = {"trace_id": PREVIOUS_TRACE, "status": "verified_existing",
                                             "sha256": prior[3], "source": prior[1]["source"]}
        save_manifest(manifest)

    for key, _, _, digest, _ in selected:
        existing = manifest["episodes"].get(key)
        if existing and existing["sha256"] != digest:
            raise RuntimeError(f"Source episode changed since export: {key}")
        if existing and existing["status"] not in ("verified", "verified_existing"):
            raise RuntimeError(f"Unconfirmed previous export for {key}; inspect it before resending")

    remaining = [x for x in selected if x[0] not in manifest["episodes"]]
    print(f"Project {project_id}: {len(selected)} selected; {len(selected)-len(remaining)} already verified; {len(remaining)} to export", flush=True)
    client = get_client()
    for offset in range(0, len(remaining), 8):
        batch = remaining[offset:offset + 8]
        for key, item, path, digest, episode in batch:
            trace_id = uuid.uuid4().hex
            record = {"trace_id": trace_id, "status": "sending", "sha256": digest,
                      "source": item["source"], "file": str(path.relative_to(ROOT))}
            manifest["episodes"][key] = record
            save_manifest(manifest)
            record["expected_observations"] = write_episode(client, episode, item["source"], trace_id, item.get("score"))
            client.flush()
            record["status"] = "sent_unconfirmed"
            save_manifest(manifest)
            print(f"Sent {key} ({record['expected_observations']} observations)", flush=True)
        ids = {manifest["episodes"][x[0]]["trace_id"] for x in batch}
        for attempt in range(10):
            counts = visible_counts(session, base, ids)
            if all(counts[manifest["episodes"][x[0]]["trace_id"]] >=
                   manifest["episodes"][x[0]]["expected_observations"] for x in batch):
                break
            time.sleep(6)
        for key, *_ in batch:
            record = manifest["episodes"][key]
            record["visible_observations"] = counts[record["trace_id"]]
            if record["visible_observations"] >= record["expected_observations"]:
                record["status"] = "verified"
        save_manifest(manifest)
        failed = [x[0] for x in batch if manifest["episodes"][x[0]]["status"] != "verified"]
        if failed:
            raise RuntimeError("Could not verify complete observations for: " + ", ".join(failed))
        print(f"Verified {min(offset+len(batch),len(remaining))}/{len(remaining)} newly exported episodes", flush=True)
    print(f"Complete. Manifest: {MANIFEST}")
    print(f"Project: {base}/project/{project_id}/traces")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Export stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
