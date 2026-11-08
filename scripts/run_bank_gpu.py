#!/usr/bin/env python3
"""Prepare and run the Bank selective disclosure comparison on one Linux GPU."""

import argparse
import asyncio
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from rdr_service.backend import RDRBackend
from skill_agent.__main__ import doctor
from skill_agent.contracts import ResolveSkillRequest, SkillSelectionError
from skill_agent.experiments import (factory, inspect_skill_requests, load_config,
                                     run_experiment, select_tasks, summarize)
from skill_agent.local_models import QWEN_MODEL, LocalTransformersClient, check_tools
from skill_agent.storage import read_json, write_json


ROOT = Path(__file__).resolve().parents[1]
SKILL_ID = "sopbench_bank_fixed_policy"
BUNDLES = Path("skills/variants/bank-defensive-v2")
ARMS = (
    ("none", "none", "initial_only", "base"),
    ("full", "full", "initial_only", "base"),
    ("initial", "reduced", "initial_only", "base"),
    ("requests", "reduced", "initial_with_requests", "base"),
    ("dependencies", "reduced", "initial_only", "with-dependencies"),
)


def setup():
    from huggingface_hub import snapshot_download

    subprocess.run([sys.executable, "scripts/prepare_sopbench_bank.py"], check=True)
    model_id, revision = QWEN_MODEL.split("@", 1)
    print(f"Downloading {model_id} at {revision} to the Hugging Face cache.", flush=True)
    print(snapshot_download(model_id, revision=revision), flush=True)


def gpu_context_window():
    # Exit the probe process before serving so it releases its CUDA context.
    probe = subprocess.check_output([sys.executable, "-c", """
import json
import torch

if not torch.cuda.is_available():
    raise SystemExit('CUDA is unavailable. Run inside an NVIDIA GPU allocation with a compatible driver.')
torch.empty(1, device='cuda:0')
free, total = torch.cuda.mem_get_info(0)
print(json.dumps({'device': torch.cuda.get_device_name(0), 'free': free, 'total': total,
                  'cuda': torch.version.cuda}))
"""], text=True)
    gpu = json.loads(probe)
    free, total = (gpu[key] / 1024 ** 3 for key in ("free", "total"))
    print(f"GPU: {gpu['device']} ({free:.2f} / {total:.2f} GiB free), PyTorch CUDA {gpu['cuda']}", flush=True)
    return 16384 if free >= 22 else 8192


def check_ports(*ports):
    if len(set(ports)) != len(ports) or any(not 1024 <= port <= 65535 for port in ports):
        raise ValueError("use distinct service and model ports between 1024 and 65535")
    for port in ports:
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError as error:
                raise RuntimeError(f"localhost port {port} is in use; choose another port") from error


@contextmanager
def service(module, arguments, url, log):
    with log.open("a", encoding="utf-8") as stream:
        process = subprocess.Popen([sys.executable, "-u", "-m", module, *arguments],
                                   stdout=stream, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 300
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    with urlopen(url, timeout=2):
                        break
                except (URLError, TimeoutError):
                    time.sleep(1)
            else:
                raise RuntimeError(f"{module} failed to start. See {log}")
            yield
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def prepare_configs(args, run_dir, model_options, store):
    backend = RDRBackend(store)
    receipts = {}
    configs = {}
    template = "initial_only" if args.command == "pilot" else "initial_only34"
    for arm, condition, mode, variant in ARMS:
        arm_dir = run_dir / arm
        config_path = arm_dir / "config.json"
        config = load_config(f"examples/agent/sopbench.bank.{template}.json")
        config["model"] = {"factory": "skill_agent.local_models:LocalTransformersClient",
                           "options": model_options}
        config["run"].update(conditions=[condition], skill_loading=mode, timeout_seconds=1800,
                             output_dir=str(arm_dir / "results"))
        if condition == "none":
            config["skill"] = {}
        else:
            bundle = BUNDLES / variant / SKILL_ID
            if variant not in receipts:
                receipts[variant] = run_dir / f"{variant}-snapshot.json"
                write_json(receipts[variant], backend.prepare(bundle))
            config["skill"].update(full_markdown=str(bundle / "initial.md"), receipt=str(receipts[variant]))
            config["skill"]["resolver"]["options"].update(
                base_url=f"http://127.0.0.1:{args.service_port}", selector_client="local_transformers",
                selector_model=model_options, selection_attempts=1)
        if config_path.exists():
            if not args.resume or read_json(config_path) != config:
                raise ValueError(f"saved configuration differs: {config_path}; choose a new run name")
        else:
            write_json(config_path, config)
        configs[arm] = config
    return configs


async def run_suite(configs, run_dir):
    for arm, config in configs.items():
        results = await doctor(config)
        errors = [value for value in results.values() if str(value).startswith("ERROR:")]
        if errors:
            raise RuntimeError(f"{arm} setup: {'; '.join(errors)}")

    actor = factory(configs["none"]["model"])
    print("Checking the local model's tool round trip.", flush=True)
    write_json(run_dir / "model-check.json", await check_tools(actor))
    config = configs["dependencies"]
    adapter = factory(config["benchmark"])
    task = select_tasks(adapter, {"split": "dev", "task_ids": ["transfer_funds-021"]})[0]["task"]
    skill = config["skill"]
    artifact = read_json(skill["receipt"])["artifact_id"]
    print("Checking a Bank selector response and dependency resolution.", flush=True)
    try:
        selection = await factory(skill["resolver"]).resolve(ResolveSkillRequest(SKILL_ID, task, {}, artifact))
    except SkillSelectionError as error:
        write_json(run_dir / "selector-check.json", {"error": str(error), "details": error.details,
                                                     "usage": error.usage})
        raise
    write_json(run_dir / "selector-check.json", selection.to_dict())
    if not selection.metadata["dependencies"]["directly_selected"]:
        raise RuntimeError("the selector check returned no relevant Bank procedure; inspect selector-check.json")

    failed = []
    for arm, config in configs.items():
        output = Path(config["run"]["output_dir"])
        print(f"Running Bank arm {arm}: {output}", flush=True)
        try:
            await run_experiment(config, resume=(output / "manifest.json").is_file())
            summarize(output)
        except (OSError, ValueError, RuntimeError) as error:
            failed.append(arm)
            print(f"{arm}: {error}", file=sys.stderr, flush=True)
        if (output / "manifest.json").is_file():
            write_json(output.parent / "skill-inspection.json", inspect_skill_requests(output))
    if failed:
        raise RuntimeError(f"arms needing attention: {', '.join(failed)}; see {run_dir}")


def run(args):
    check_ports(args.model_port, args.service_port)
    run_dir = args.results_root.resolve() / args.name
    if args.resume != run_dir.is_dir():
        raise ValueError("choose a new run name, or use --resume for an existing run")
    default_window = gpu_context_window()
    previous = run_dir / "none/config.json"
    if args.resume and previous.is_file():
        default_window = read_json(previous)["model"]["options"]["context_window"]
    window = args.context_window if args.context_window is not None else default_window
    if window <= 2048:
        raise ValueError("context window must leave room beyond the 2048-token response budget")
    run_dir.mkdir(parents=True, exist_ok=args.resume)
    store = str(run_dir / "snapshots")
    model_url = f"http://127.0.0.1:{args.model_port}/v1"
    options = {"base_url": model_url, "model": QWEN_MODEL, "context_window": window,
               "max_tokens": 2048, "temperature": 0, "seed": 42, "timeout": 300}
    configs = prepare_configs(args, run_dir, options, store)
    with ExitStack() as stack:
        stack.enter_context(service("rdr_service", ["serve", "--store", store, "--port", str(args.service_port)],
                                    f"http://127.0.0.1:{args.service_port}/health", run_dir / "skill-service.log"))
        stack.enter_context(service("skill_agent.local_models", ["serve", "--port", str(args.model_port)],
                                    model_url + "/models", run_dir / "model-server.log"))
        asyncio.run(run_suite(configs, run_dir))
    print(f"Bank results: {run_dir}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="download the pinned Bank data and Qwen checkpoint")
    for name in ("pilot", "bank34"):
        command = commands.add_parser(name)
        command.add_argument("--name", required=True)
        command.add_argument("--results-root", type=Path, default=Path("experiments/bank-gpu"))
        command.add_argument("--context-window", type=int)
        command.add_argument("--model-port", type=int, default=8000)
        command.add_argument("--service-port", type=int, default=8766)
        command.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.command != "setup" and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.name):
        parser.error("use letters, digits, dots, underscores or hyphens in the run name")
    if args.command != "setup":
        args.results_root = args.results_root.resolve()
    os.chdir(ROOT)
    try:
        setup() if args.command == "setup" else run(args)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"error: {error}\n")


if __name__ == "__main__":
    def interrupt(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("Bank launcher interrupted. Resume with the same suite and run name.")
