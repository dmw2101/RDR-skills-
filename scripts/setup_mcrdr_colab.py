#!/usr/bin/env python3
"""Set up (and optionally run) the MCRDR VAREX experiments on Colab or another Linux host.

Standalone and standard-library only, so it can be downloaded and run before the
repository exists:

    !wget -q -O /content/setup_mcrdr_colab.py https://raw.githubusercontent.com/dmw2101/RDR-skills-/verifier-mcrdr/scripts/setup_mcrdr_colab.py
    %run /content/setup_mcrdr_colab.py --experiment qwen-dev30 --run-name mcrdr-qwen-rolling-dev30

Every step is idempotent: system tools, the checkout, the venv, prepared data and
the skill snapshot are reused when already present. Nothing is ever deleted, and an
existing results directory is only reused with --resume. Generated configs, snapshots,
logs and results live under <workdir>/experiments/, which Git ignores.

With --learn it also writes a trace_learner config that learns from these documents batch by
batch (--batch-size, default 1): the learner runs the worker on each batch with the tree learned
so far (outputs in <workdir>/experiments/learning/<run-name>):

    %run /content/setup_mcrdr_colab.py --experiment qwen-dev30 --heldout-batch 30 --conditions reduced \
        --run-name learn-heldout30 --learn

--heldout-batch N runs on the first N held-out documents instead of dev documents, so learning
never sees the dev documents used for evaluation. --skill-version runs a learned tree (e.g.
<workdir>/experiments/learning/<run-name>/versions/v1/pdf) with the feature judge:

    %run /content/setup_mcrdr_colab.py --experiment qwen-dev30 --conditions reduced \
        --skill-version /content/rdr-skills-mcrdr/experiments/learning/learn-heldout30/versions/v1/pdf \
        --run-name learned-v1-dev30-run1

This is an independent script for the MCRDR branches, not a copy of the external
setup_rdr_colab.py used for the single-classification runs.
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
BRANCH = "verifier-mcrdr"
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
# Learner models and settings (OpenRouter); --learn fills in this run's paths.
LEARNER_BASE = "examples/learning/varex.learner.json"
LEARNER_STAGES = ("learn",)


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
    def learning(self) -> Path:
        return self.experiments / "learning"

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
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        try:
            if capture:
                result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, env=env,
                                        encoding="utf-8", errors="replace")
                detail = (result.stderr or result.stdout or "").strip()[-2000:]
            else:
                # Notebook kernels (Colab, VS Code) do not show a child's inherited
                # stdout, so stream every line through `out` as it arrives.
                tail: list[str] = []
                with subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                      errors="replace", bufsize=1) as process:
                    for line in process.stdout:
                        line = line.rstrip("\n")
                        self.out(line)
                        tail = (tail + [line])[-40:]
                result = subprocess.CompletedProcess(command, process.returncode, "", "")
                detail = "\n".join(tail)
        except FileNotFoundError as error:
            raise SetupError(f"command not found: {command[0]}") from error
        if check and result.returncode != 0:
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


def data_selection(experiment: str, limit: int | None, heldout_batch: int | None = None) -> tuple[str, list[str]]:
    """Return the data directory (relative to the repo) and `skill_agent prepare` arguments."""
    if heldout_batch is not None:
        if EXPERIMENTS[experiment]["selection"] != "dev":
            raise SetupError(f"--heldout-batch cannot be used with the {experiment} experiment")
        if type(heldout_batch) is not int or heldout_batch < 1:
            raise SetupError("--heldout-batch must be a positive number of documents")
        # The first N held-out documents of the same deterministic order; never a dev document.
        return f"benchmarks/local-data/varex-heldout-{heldout_batch}", ["--heldout-limit", str(heldout_batch)]
    if EXPERIMENTS[experiment]["selection"] == "task_ids":
        return "benchmarks/local-data/varex-skill-requests", ["--task-ids", *SKILL_REQUEST_IDS]
    if type(limit) is not int or not 1 <= limit <= 30:
        raise SetupError("dev experiments need --limit between 1 and 30 (the prepared dev split)")
    # Dev documents are a fixed prefix of one deterministic order, so a smaller
    # selection is always the start of a larger one.
    return f"benchmarks/local-data/varex-dev-{limit}", ["--dev-limit", str(limit)]


def receipt_path(options: argparse.Namespace, paths: Paths) -> Path:
    if options.skill_version is None:
        return paths.receipt
    return paths.experiments / f"pdf-snapshot-{options.run_name}.json"


def skill_source(options: argparse.Namespace, paths: Paths) -> Path:
    """The skill folder to snapshot: the original PDF skill or a learned version of it."""
    if options.skill_version is None:
        return paths.workdir / "skills" / "pdf"
    source = Path(options.skill_version)
    source = source if source.is_absolute() else paths.workdir / source
    if source.name != "pdf":  # The service derives the skill (and its case heuristics) from this name.
        raise SetupError(f"--skill-version must be a versions/vN/pdf folder, not {source}")
    return source


def build_config(base: dict, options: argparse.Namespace, paths: Paths, judge_model: dict | None = None) -> dict:
    """Base MCRDR experiment config plus overrides, with absolute paths.

    judge_model is required with --skill-version: learned features are judged on every refresh.
    """
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
    if options.heldout_batch is not None:
        if options.limit is not None:
            raise SetupError("--heldout-batch already chooses the documents; remove --limit")
        run["split"], run["limit"] = "heldout", None

    data_dir, _ = data_selection(options.experiment, run.get("limit"), options.heldout_batch)
    config["benchmark"]["options"]["data_dir"] = str(paths.workdir / data_dir)
    if list(run.get("conditions") or ()) == ["none"]:
        if options.skill_version is not None:
            raise SetupError("--skill-version needs a skill condition (e.g. --conditions reduced)")
        config["skill"] = {}  # Skill-blind: no snapshot, service, catalog or guidance.
    else:
        skill = config["skill"]
        skill["full_markdown"] = str(paths.workdir / skill["full_markdown"])  # Full arm: always the original.
        skill["receipt"] = str(receipt_path(options, paths))
        skill["resolver"]["options"]["base_url"] = f"http://127.0.0.1:{options.service_port}"
        if options.skill_version is not None:
            skill_source(options, paths)
            if judge_model is None:
                raise SetupError("--skill-version needs the feature judge model")
            skill["resolver"] = {"factory": "skill_agent.learned_features:LearnedFeatureResolver",
                                 "options": {"base_url": f"http://127.0.0.1:{options.service_port}",
                                             "judge_model": copy.deepcopy(judge_model)}}
    return config


def needs_service(config: dict) -> bool:
    return bool(config["skill"])


def build_learner_config(base: dict, options: argparse.Namespace, paths: Paths, run_config: dict) -> dict:
    """Incremental learner config for this run's documents: the example's models, this host's paths.

    The learner runs the worker itself, batch by batch (options.batch_size documents) on the tree
    learned so far, using the run config written beside it for the model, data and limits.
    """
    if "reduced" not in (run_config["run"].get("conditions") or ("none", "full", "reduced")):
        raise SetupError("--learn needs the reduced condition in the run (e.g. --conditions reduced)")
    if type(options.batch_size) is not int or options.batch_size < 1:
        raise SetupError("--batch-size must be a positive number of documents")
    config = copy.deepcopy(base)
    config.pop("collect_run", None)
    config.update(name=options.run_name, mode="incremental", batch_size=options.batch_size,
                  run_config=str(paths.configs / f"{options.run_name}.json"),
                  skill_dir=str(paths.workdir / "skills" / "pdf"),
                  output_dir=str(paths.learning / options.run_name))
    config["validation"].update(service_url=f"http://127.0.0.1:{options.service_port}",
                                store=str(paths.snapshots))
    return config


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


def apt_prefix() -> list[str]:
    return [] if (os.name == "nt" or os.geteuid() == 0 or shutil.which("sudo") is None) else ["sudo"]


def venv_package() -> str:
    """Debian/Ubuntu ship venv support per Python version (Colab's python3 needs e.g. python3.12-venv)."""
    return f"python{sys.version_info.major}.{sys.version_info.minor}-venv"


def ensure_venv(options, paths: Paths, runner: Runner) -> None:
    runner.step("Python environment")
    venv = paths.workdir / ".venv"
    if paths.python.exists() and not runner.dry_run:
        has_pip = runner.run([paths.python, "-m", "pip", "--version"], check=False, capture=True).returncode == 0
        if not has_pip:  # Left by a venv creation that failed at its pip step; holds nothing else.
            runner.note(f"{venv} has no pip (an earlier creation failed); rebuilding it")
            shutil.rmtree(venv)
    if not paths.python.exists():
        created = runner.run([sys.executable, "-m", "venv", venv], check=False)
        if created.returncode != 0:
            if shutil.which("apt-get") is None:
                raise SetupError(f"could not create {venv}; check that this Python includes venv and pip")
            runner.note(f"venv creation failed; installing {venv_package()} and retrying")
            shutil.rmtree(venv, ignore_errors=True)
            runner.run([*apt_prefix(), "apt-get", "update", "-qq"])
            runner.run([*apt_prefix(), "apt-get", "install", "-y", "-qq", venv_package()])
            runner.run([sys.executable, "-m", "venv", venv])
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
    relative, arguments = data_selection(options.experiment, run_limit, options.heldout_batch)
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
    source = skill_source(options, paths)
    if not runner.dry_run and not (source / "compiled" / "manifest.json").is_file():
        raise SetupError(f"{source} is not a compiled skill folder")
    runner.note(f"skill snapshot from {source}")
    runner.run([paths.python, "-m", "rdr_service", "prepare", source,
                "--store", paths.snapshots, "--output", receipt_path(options, paths)], cwd=paths.workdir)
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


def read_example(paths: Paths, relative: str) -> dict:
    candidates = [paths.workdir / relative, Path(__file__).resolve().parents[1] / relative]
    for candidate in candidates:
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise SetupError(f"cannot find {relative}; the checkout must be on {BRANCH}")


def read_base(paths: Paths, experiment: str) -> dict:
    return read_example(paths, f"examples/agent/{EXPERIMENTS[experiment]['config']}")


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
        runner.run([*apt_prefix(), "apt-get", "update", "-qq"])
        runner.run([*apt_prefix(), "apt-get", "install", "-y", "-qq", *APT_PACKAGES])

    ensure_repo(options, paths, runner)
    ensure_venv(options, paths, runner)

    judge_model = (read_example(paths, LEARNER_BASE)["judge"]["model"]
                   if options.skill_version is not None else None)
    config = build_config(read_base(paths, options.experiment), options, paths, judge_model)
    learner = (build_learner_config(read_example(paths, LEARNER_BASE), options, paths, config)
               if options.learn else None)
    if learner is not None and options.run:
        raise SetupError("with --learn the learner runs the worker itself, batch by batch; remove --run")
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
    learner_path = paths.configs / f"{options.run_name}.learner.json"
    if learner is not None:
        runner.write(learner_path, json.dumps(learner, indent=2) + "\n")

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
        if receipt_path(options, paths).exists() and needs_service(config):
            receipt = json.loads(receipt_path(options, paths).read_text(encoding="utf-8"))
            runner.note(f"resolver revision {receipt['resolver_revision']}")
    if learner is not None:
        runner.note(f"learn batch by batch ({options.batch_size} document(s) at a time) on the tree learned so "
                    "far; learn makes model calls and resumes after an interruption, report and status do not "
                    "(see docs/verifier-learning.md):")
        for stage in (*LEARNER_STAGES, "report"):
            runner.out("!" + shlex.join([str(paths.python), "-m", "trace_learner", stage,
                                         "--config", str(learner_path)]))
    elif options.run:
        for command in commands:
            runner.run(command, cwd=paths.workdir)
    else:
        runner.note("run these from any cell (they use absolute paths):")
        for command in commands:
            runner.out("!" + shlex.join(str(part) for part in command))
    runner.note("after all runs are summarized, compare them (see docs/agent-commands.md, step 9):")
    runner.out("!" + shlex.join([str(paths.python), "-m", "skill_agent", "report",
                                 "--spec", str(paths.experiments / "report-spec.json")]))
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
    parser.add_argument("--heldout-batch", type=int,
                        help="Use the first N held-out documents instead of dev documents (e.g. to learn from)")
    parser.add_argument("--skill-version",
                        help="Learned skill folder to serve in the reduced arm, e.g. .../versions/v1/pdf")
    parser.add_argument("--replicates", type=int)
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--conditions", nargs="+", choices=("none", "full", "reduced"))
    parser.add_argument("--skill-loading", choices=("rolling", "model_initiated"))
    parser.add_argument("--service-port", type=int, default=SERVICE_PORT)
    parser.add_argument("--run", action="store_true", help="Also run, inspect and summarize")
    parser.add_argument("--resume", action="store_true", help="Continue an existing run directory")
    parser.add_argument("--dry-run", action="store_true", help="Print every step without executing")
    parser.add_argument("--learn", action="store_true",
                        help="Write a trace_learner config that learns from these documents, batch by batch")
    parser.add_argument("--batch-size", type=int, default=1,
                        help="Documents per learning round with --learn (default 1: one at a time)")
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
