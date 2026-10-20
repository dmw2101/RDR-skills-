#!/usr/bin/env python3
"""Set up (and optionally run) the MCRDR VAREX experiments on Colab or another Linux host.

Standalone and standard-library only, so it can be downloaded and run before the
repository exists:

    !wget -q -O /content/setup_mcrdr_colab.py https://raw.githubusercontent.com/dmw2101/RDR-skills-/MCRDR-testing/scripts/setup_mcrdr_colab.py
    %run /content/setup_mcrdr_colab.py --experiment qwen-dev30 --run-name mcrdr-qwen-rolling-dev30

Every step is idempotent: system tools, the checkout, the venv, prepared data and
the skill snapshot are reused when already present. Nothing is ever deleted, and an
existing results directory is only reused with --resume. Generated configs, snapshots,
logs and results live under <workdir>/experiments/, which Git ignores.

This is an independent script for the MCRDR-testing branch, not a copy of the
external setup_rdr_colab.py used for the single-classification runs.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO_URL = "https://github.com/dmw2101/RDR-skills-.git"
BRANCH = "MCRDR-testing"
WORKDIR = "/content/rdr-skills-mcrdr"
# 8765 is the default of the single-classification service; never talk to it by accident.
SERVICE_PORT = 8766
LOCAL_MODEL_PORT = 8000
EXTRAS = ("skill-service", "agent-benchmarks", "pdf-runtime")
APT_PACKAGES = ("poppler-utils", "tesseract-ocr", "qpdf", "fonts-dejavu-core", "python3-venv")
SYSTEM_TOOLS = ("pdftotext", "tesseract", "qpdf")
# Keep in sync with skill_agent.local_models.QWEN_MODEL (a test checks this).
QWEN_MODEL = "Qwen/Qwen3.5-4B@851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a"
TORCH_INDEX = "https://download.pytorch.org/whl/cu126"
TORCH_PACKAGES = ("torch==2.9.1", "torchvision==0.24.1")
SKILL_REQUEST_IDS = ("FS5441-3E", "LS-275si")

# Base configuration and data selection for each MCRDR experiment.
EXPERIMENTS = {
    "pilot": {"config": "varex.mcrdr.pilot.json", "selection": "dev"},
    "skill-requests": {"config": "varex.mcrdr.skill-requests.json", "selection": "task_ids"},
    "qwen-dev30": {"config": "varex.mcrdr.qwen-dev30.json", "selection": "dev"},
    "deepseek-dev30": {"config": "varex.mcrdr.deepseek-dev30.json", "selection": "dev"},
}

API_ONLY_OPTIONS = ("model", "base_url", "provider", "max_input_price", "max_output_price")


class SetupError(RuntimeError):
    """A precondition failed; the message says what to fix. Nothing was deleted."""


@dataclass(frozen=True)
class Paths:
    workdir: Path

    @property
    def python(self) -> Path:
        if os.name == "nt":
            return self.workdir / ".venv" / "Scripts" / "python.exe"
        return self.workdir / ".venv" / "bin" / "python"

    @property
    def experiments(self) -> Path:
        return self.workdir / "experiments"

    @property
    def snapshots(self) -> Path:
        return self.experiments / "snapshots"

    @property
    def receipt(self) -> Path:
        return self.experiments / "pdf-snapshot-mcrdr.json"

    @property
    def configs(self) -> Path:
        return self.experiments / "configs"

    @property
    def results(self) -> Path:
        return self.experiments / "results"

    @property
    def logs(self) -> Path:
        return self.experiments / "logs"

    @property
    def install_marker(self) -> Path:
        return self.workdir / ".venv" / ".mcrdr-setup-install.json"


class Runner:
    """Runs (or, in dry-run mode, only records and prints) every side effect."""

    def __init__(self, *, dry_run: bool = False, out=print) -> None:
        self.dry_run = dry_run
        self.out = out
        self.records: list[tuple[str, str]] = []

    def step(self, title: str) -> None:
        self.records.append(("step", title))
        self.out(f"\n== {title}")

    def note(self, text: str) -> None:
        self.records.append(("note", text))
        self.out(f"   {text}")

    def run(self, command, *, cwd: Path | None = None, check: bool = True, capture: bool = False):
        command = [str(part) for part in command]
        shown = shlex.join(command)
        self.records.append(("run", shown))
        self.out(f"$ {shown}" + (f"    # in {cwd}" if cwd else ""))
        if self.dry_run:
            return subprocess.CompletedProcess(command, 0, "", "")
        try:
            result = subprocess.run(command, cwd=cwd, text=True, capture_output=capture)
        except FileNotFoundError as error:
            raise SetupError(f"command not found: {command[0]}") from error
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-2000:] if capture else ""
            raise SetupError(f"command failed ({result.returncode}): {shown}\n{detail}".rstrip())
        return result

    def start(self, command, *, cwd: Path, log: Path, pid_file: Path):
        command = [str(part) for part in command]
        shown = shlex.join(command)
        self.records.append(("start", shown))
        self.out(f"$ {shown} > {log} 2>&1 &")
        if self.dry_run:
            return None
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as stream:
            process = subprocess.Popen(command, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL, start_new_session=True)
        pid_file.write_text(str(process.pid), encoding="utf-8")
        return process

    def write(self, path: Path, text: str) -> None:
        self.records.append(("write", str(path)))
        if self.dry_run:
            self.out(f"would write {path}:\n{text}")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self.out(f"wrote {path}")


# --------------------------------------------------------------------------- #
# Pure configuration logic (unit tested)
# --------------------------------------------------------------------------- #


def data_selection(experiment: str, limit: int | None) -> tuple[str, list[str]]:
    """Return the data directory (relative to the repo) and `skill_agent prepare` arguments."""
    if EXPERIMENTS[experiment]["selection"] == "task_ids":
        return "benchmarks/local-data/varex-skill-requests", ["--task-ids", *SKILL_REQUEST_IDS]
    if type(limit) is not int or not 1 <= limit <= 30:
        raise SetupError("dev experiments need --limit between 1 and 30 (the prepared dev split)")
    # Dev documents are a fixed prefix of one deterministic order, so a smaller
    # selection is always the start of a larger one.
    return f"benchmarks/local-data/varex-dev-{limit}", ["--dev-limit", str(limit)]


def build_config(base: dict, options: argparse.Namespace, paths: Paths) -> dict:
    """Base MCRDR experiment config plus overrides, with absolute paths."""
    config = copy.deepcopy(base)
    run = config["run"]
    if options.backend == "local":
        given = [name for name in API_ONLY_OPTIONS if getattr(options, name) is not None]
        if given:
            raise SetupError("--backend local uses the pinned local Qwen server; remove "
                             + ", ".join("--" + name.replace("_", "-") for name in given))
        config["model"] = {
            "factory": "skill_agent.local_models:LocalTransformersClient",
            "options": {
                "base_url": f"http://127.0.0.1:{LOCAL_MODEL_PORT}/v1",
                "model": QWEN_MODEL,
                "context_window": 8192,
                "max_tokens": 2048,
                "temperature": 0,
                "seed": 42,
                "timeout": 300,
            },
        }
        run["concurrency"] = 1
        run["timeout_seconds"] = 1800
    model = config["model"]["options"]
    for name in ("model", "base_url", "max_tokens"):
        if getattr(options, name) is not None:
            model[name] = getattr(options, name)
    if options.provider is not None or options.max_input_price is not None or options.max_output_price is not None:
        provider = dict(model.get("provider") or {})
        if options.provider is not None:
            provider.update(only=[options.provider], allow_fallbacks=False, require_parameters=True)
        prices = dict(provider.get("max_price") or {})
        if options.max_input_price is not None:
            prices["prompt"] = options.max_input_price
        if options.max_output_price is not None:
            prices["completion"] = options.max_output_price
        if prices:
            provider["max_price"] = prices
        model["provider"] = provider

    for name in ("limit", "replicates", "concurrency"):
        if getattr(options, name) is not None:
            run[name] = getattr(options, name)
    if options.conditions is not None:
        run["conditions"] = list(options.conditions)
    if options.skill_loading is not None:
        run["skill_loading"] = options.skill_loading
    run["output_dir"] = str(paths.results / options.run_name)

    data_dir, _ = data_selection(options.experiment, run.get("limit"))
    config["benchmark"]["options"]["data_dir"] = str(paths.workdir / data_dir)
    if list(run.get("conditions") or ()) == ["none"]:
        config["skill"] = {}  # Skill-blind: no snapshot, service, catalog or guidance.
    else:
        skill = config["skill"]
        skill["full_markdown"] = str(paths.workdir / skill["full_markdown"])
        skill["receipt"] = str(paths.receipt)
        skill["resolver"]["options"]["base_url"] = f"http://127.0.0.1:{options.service_port}"
    return config


def needs_service(config: dict) -> bool:
    return bool(config["skill"])


# --------------------------------------------------------------------------- #
# Side effects
# --------------------------------------------------------------------------- #


def missing_tools() -> list[str]:
    import importlib.util

    missing = [tool for tool in SYSTEM_TOOLS if shutil.which(tool) is None]
    if importlib.util.find_spec("ensurepip") is None:  # Debian splits venv support into python3-venv.
        missing.append("python3-venv")
    return missing


def http_json(url: str, timeout: float = 2.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read() or b"null")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def wait_for(url: str, process, *, seconds: int, label: str, log: Path, ok=lambda value: value is not None):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if ok(http_json(url)):
            return
        if process is not None and process.poll() is not None:
            tail = log.read_text(encoding="utf-8", errors="replace")[-3000:] if log.exists() else ""
            raise SetupError(f"{label} exited early (code {process.returncode}); log {log}:\n{tail}")
        time.sleep(3)
    raise SetupError(f"{label} did not answer at {url} within {seconds}s; see {log}")


def stop_own_process(pid_file: Path, marker: str, runner: Runner) -> None:
    """Stop a process this script started earlier, and only if it still looks like ours."""
    if runner.dry_run or not pid_file.exists():
        return
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except (ValueError, OSError):
        pid_file.unlink(missing_ok=True)
        return
    if marker not in cmdline:
        pid_file.unlink(missing_ok=True)
        return
    runner.note(f"stopping previous {marker} (pid {pid})")
    os.kill(pid, signal.SIGTERM)
    for _ in range(40):
        if not Path(f"/proc/{pid}").exists():
            break
        time.sleep(0.25)
    pid_file.unlink(missing_ok=True)


def ensure_repo(options, paths: Paths, runner: Runner) -> None:
    runner.step(f"Repository: {options.branch}")
    if not (paths.workdir / ".git").exists():
        if paths.workdir.exists() and any(paths.workdir.iterdir()):
            raise SetupError(f"{paths.workdir} exists but is not a Git checkout; choose another --workdir")
        runner.run(["git", "clone", "--branch", options.branch, options.repo, paths.workdir])
        return
    current = runner.run(["git", "-C", paths.workdir, "rev-parse", "--abbrev-ref", "HEAD"], capture=True)
    if not runner.dry_run and current.stdout.strip() != options.branch:
        raise SetupError(f"{paths.workdir} is on {current.stdout.strip()!r}, not {options.branch!r}; "
                         "switch it yourself or use another --workdir")
    changed = runner.run(["git", "-C", paths.workdir, "status", "--porcelain", "--untracked-files=no"],
                         capture=True)
    if not runner.dry_run and changed.stdout.strip():
        raise SetupError(f"{paths.workdir} has local changes to tracked files; commit or stash them first")
    runner.run(["git", "-C", paths.workdir, "fetch", "origin", options.branch])
    runner.run(["git", "-C", paths.workdir, "merge", "--ff-only", f"origin/{options.branch}"])


def ensure_venv(options, paths: Paths, runner: Runner) -> None:
    runner.step("Python environment")
    if not paths.python.exists():
        runner.run([sys.executable, "-m", "venv", paths.workdir / ".venv"])
    extras = list(EXTRAS) + (["local-model"] if options.backend == "local" else [])
    pyproject = paths.workdir / "pyproject.toml"
    wanted = {"extras": extras,
              "pyproject_sha256": hashlib.sha256(pyproject.read_bytes()).hexdigest() if pyproject.exists() else None}
    try:
        installed = json.loads(paths.install_marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        installed = None
    if installed == wanted:
        runner.note("dependencies already installed for this pyproject.toml; skipping pip")
        return
    if options.backend == "local":
        runner.run([paths.python, "-m", "pip", "install", "-q", *TORCH_PACKAGES, "--index-url", TORCH_INDEX])
    runner.run([paths.python, "-m", "pip", "install", "-q", "-e", f".[{','.join(extras)}]"], cwd=paths.workdir)
    runner.write(paths.install_marker, json.dumps(wanted))


def ensure_data(options, run_limit, paths: Paths, runner: Runner) -> None:
    relative, arguments = data_selection(options.experiment, run_limit)
    target = paths.workdir / relative
    runner.step(f"VAREX data: {relative}")
    if (target / "provenance.json").exists():
        runner.note("already prepared; reusing it")
        return
    if target.exists():
        raise SetupError(f"{target} exists without provenance.json (an interrupted or foreign download); "
                         "move it aside and rerun")
    runner.run([paths.python, "-m", "skill_agent", "prepare", "--output", target, *arguments], cwd=paths.workdir)


def ensure_service(options, paths: Paths, runner: Runner) -> None:
    runner.step(f"MCRDR skill service on port {options.service_port}")
    runner.run([paths.python, "-m", "rdr_service", "prepare", "skills/pdf",
                "--store", paths.snapshots, "--output", paths.receipt], cwd=paths.workdir)
    pid_file = paths.logs / "rdr_service.pid"
    stop_own_process(pid_file, "rdr_service", runner)
    health = f"http://127.0.0.1:{options.service_port}/health"
    if not runner.dry_run and http_json(health) is not None:
        raise SetupError(f"another service already answers on port {options.service_port}; "
                         "stop it or pass a different --service-port")
    log = paths.logs / "rdr_service.log"
    process = runner.start([paths.python, "-m", "rdr_service", "serve", "--store", paths.snapshots,
                            "--port", options.service_port], cwd=paths.workdir, log=log, pid_file=pid_file)
    if not runner.dry_run:
        wait_for(health, process, seconds=60, label="skill service", log=log,
                 ok=lambda value: isinstance(value, dict) and value.get("contract") == "v1")


def ensure_local_model(paths: Paths, runner: Runner) -> None:
    runner.step(f"Local Qwen server on port {LOCAL_MODEL_PORT}")
    models = f"http://127.0.0.1:{LOCAL_MODEL_PORT}/v1/models"
    if not runner.dry_run and http_json(models) is not None:
        runner.note("a model server is already running; reusing it")
    else:
        log = paths.logs / "local_model.log"
        process = runner.start([paths.python, "-m", "skill_agent.local_models", "serve",
                                "--port", LOCAL_MODEL_PORT], cwd=paths.workdir, log=log,
                               pid_file=paths.logs / "local_model.pid")
        if not runner.dry_run:
            runner.note("waiting for the model to load (the first start downloads the weights)")
            wait_for(models, process, seconds=1800, label="local model server", log=log)
    runner.run([paths.python, "-m", "skill_agent.local_models", "check", "--port", LOCAL_MODEL_PORT],
               cwd=paths.workdir)


def read_base(paths: Paths, experiment: str) -> dict:
    name = EXPERIMENTS[experiment]["config"]
    candidates = [paths.workdir / "examples" / "agent" / name,
                  Path(__file__).resolve().parents[1] / "examples" / "agent" / name]
    for candidate in candidates:
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise SetupError(f"cannot find examples/agent/{name}; the checkout must be on {BRANCH}")


def setup(options: argparse.Namespace, runner: Runner) -> dict:
    paths = Paths(Path(options.workdir).resolve())
    results = paths.results / options.run_name
    if results.exists() and not options.resume:
        raise SetupError(f"{results} already exists; choose a new --run-name or pass --resume")

    runner.step("System PDF tools")
    missing = missing_tools()
    if not missing:
        runner.note("pdftotext, tesseract, qpdf and venv support are available")
    else:
        runner.note("missing: " + ", ".join(missing))
        prefix = [] if (os.name == "nt" or os.geteuid() == 0 or shutil.which("sudo") is None) else ["sudo"]
        runner.run([*prefix, "apt-get", "update", "-qq"])
        runner.run([*prefix, "apt-get", "install", "-y", "-qq", *APT_PACKAGES])

    ensure_repo(options, paths, runner)
    ensure_venv(options, paths, runner)

    config = build_config(read_base(paths, options.experiment), options, paths)
    placeholders = [key for key, value in config["model"]["options"].items()
                    if isinstance(value, str) and "YOUR-" in value]
    if placeholders:
        raise SetupError(f"the {options.experiment} config has placeholder model settings "
                         f"({', '.join(placeholders)}); pass --base-url and --model for your provider")
    ensure_data(options, config["run"].get("limit"), paths, runner)
    if needs_service(config):
        ensure_service(options, paths, runner)
    else:
        runner.step("Skill service")
        runner.note("skill-blind (none-only) run: no snapshot or service needed")
    if options.backend == "local":
        ensure_local_model(paths, runner)

    runner.step("Run configuration")
    config_path = paths.configs / f"{options.run_name}.json"
    runner.write(config_path, json.dumps(config, indent=2) + "\n")

    runner.step("Checks")
    if options.backend == "api" and not os.environ.get("MODEL_API_KEY"):
        runner.note("WARNING: MODEL_API_KEY is not set in this runtime; set it before running")
    doctor = runner.run([paths.python, "-m", "skill_agent", "doctor", "--config", config_path],
                        cwd=paths.workdir, check=False, capture=True)
    if not runner.dry_run:
        runner.out(doctor.stdout)
        if doctor.returncode != 0:
            raise SetupError("doctor reported errors (see above); fix them and rerun this script")

    run_command = [paths.python, "-m", "skill_agent", "run", "--config", config_path]
    if options.resume:
        run_command.append("--resume")
    commands = [run_command,
                [paths.python, "-m", "skill_agent", "inspect-skills", results],
                [paths.python, "-m", "skill_agent", "summarize", results]]
    runner.step("Ready")
    if not runner.dry_run:
        commit = runner.run(["git", "-C", paths.workdir, "rev-parse", "--short", "HEAD"], capture=True)
        runner.note(f"branch {options.branch} at {commit.stdout.strip()}")
        if paths.receipt.exists() and needs_service(config):
            receipt = json.loads(paths.receipt.read_text(encoding="utf-8"))
            runner.note(f"resolver revision {receipt['resolver_revision']}")
    if options.run:
        for command in commands:
            runner.run(command, cwd=paths.workdir)
    else:
        runner.note("run these from any cell (they use absolute paths):")
        for command in commands:
            runner.out("!" + shlex.join(str(part) for part in command))
    return config


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Set up the MCRDR VAREX experiments (Colab/Linux)")
    parser.add_argument("--experiment", choices=sorted(EXPERIMENTS), default="qwen-dev30")
    parser.add_argument("--run-name", required=True, help="Results go to <workdir>/experiments/results/<run-name>")
    parser.add_argument("--branch", default=BRANCH)
    parser.add_argument("--repo", default=REPO_URL)
    parser.add_argument("--workdir", default=WORKDIR)
    parser.add_argument("--backend", choices=("api", "local"), default="api")
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--provider", help="OpenRouter provider to pin, e.g. alibaba")
    parser.add_argument("--max-input-price", type=float, help="USD per million prompt tokens")
    parser.add_argument("--max-output-price", type=float, help="USD per million completion tokens")
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--limit", type=int, help="Number of dev documents (1-30)")
    parser.add_argument("--replicates", type=int)
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--conditions", nargs="+", choices=("none", "full", "reduced"))
    parser.add_argument("--skill-loading", choices=("rolling", "model_initiated"))
    parser.add_argument("--service-port", type=int, default=SERVICE_PORT)
    parser.add_argument("--run", action="store_true", help="Also run, inspect and summarize")
    parser.add_argument("--resume", action="store_true", help="Continue an existing run directory")
    parser.add_argument("--dry-run", action="store_true", help="Print every step without executing")
    options = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", options.run_name):
        parser.error("--run-name may only contain letters, digits, '.', '_' and '-'")
    if options.conditions is not None and len(set(options.conditions)) != len(options.conditions):
        parser.error("--conditions must not repeat a condition")
    return options


def main(argv=None) -> int:
    options = parse_args(argv)
    runner = Runner(dry_run=options.dry_run)
    try:
        setup(options, runner)
    except SetupError as error:
        print(f"\nSETUP STOPPED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    code = main()
    if code:  # %run treats SystemExit(0) as noise; only exit on failure.
        sys.exit(code)
