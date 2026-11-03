"""Retry only HTTP 429 episodes, preserving their original records."""

import asyncio
import copy
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

from skill_agent.experiments import load_run, run_experiment
from skill_agent.storage import write_json


ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = ROOT / "experiments/bank-windows/runs/bank-affected6/results"
RETRIES = ROOT / "experiments/bank-windows/runs/bank-affected6-retry429"


def main():
    if not os.environ.get("MODEL_API_KEY"):
        raise ValueError("Set MODEL_API_KEY before starting retries")
    manifest, records = load_run(ORIGINAL)
    failed = [r for r in records if r["status"] == "failed" and "HTTP 429 from " in (r.get("error") or "")]
    if not failed:
        print("No 429 episodes to retry.")
        return
    RETRIES.mkdir(parents=True, exist_ok=True)
    try:
        with urlopen("http://127.0.0.1:8766/health", timeout=2):
            raise ValueError("Port 8766 already has a service; stop it before retrying")
    except OSError:
        pass
    service_log = (RETRIES / "service.log").open("a", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "rdr_service", "serve", "--store", "experiments/bank-snapshots", "--port", "8766"],
        cwd=ROOT, stdout=service_log, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        ready = False
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError("Bank skill service exited; inspect retry service.log")
            try:
                with urlopen("http://127.0.0.1:8766/health", timeout=1) as response:
                    ready = json.load(response).get("status") == "ok"
            except OSError:
                pass
            if ready:
                break
            time.sleep(.2)
        if not ready:
            raise RuntimeError("Bank skill service did not start")
        os.chdir(ROOT)
        for index, record in enumerate(failed, 1):
            name = f"{record['task_id']}_{record['condition']}"
            result = RETRIES / name / "results"
            if result.exists():
                raise ValueError(f"Retry output already exists: {result}")
            config = copy.deepcopy(manifest["config"])
            config["model"]["options"].update(rate_limit_retries=2, rate_limit_delay_seconds=60)
            config["run"].update(output_dir=str(result), task_ids=[record["task_id"]],
                                 conditions=[record["condition"]], limit=1)
            result.parent.mkdir(parents=True, exist_ok=True)
            write_json(result.parent / "experiment-config.json", config)
            print(f"Retry {index}/{len(failed)}: {record['task_id']} / {record['condition']}", flush=True)
            asyncio.run(run_experiment(config))
    finally:
        if process.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            else:
                process.terminate()
                process.wait(timeout=10)
        service_log.close()


if __name__ == "__main__":
    main()
