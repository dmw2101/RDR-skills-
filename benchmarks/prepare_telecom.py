"""Prepare pinned Telecom assets and an optional isolated runtime; never call an LLM."""

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import venv


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "benchmarks/telecom/source.json"
CHECKOUT = ROOT / "benchmarks/local-data/tau2-bench"
VENV = ROOT / "benchmarks/local-data/tau2-venv"
RECEIPT = ROOT / "benchmarks/local-data/telecom/provenance.json"
SKILL = ROOT / "skills/telecom"


def run(*command, cwd=None, capture=False, env=None):
    return subprocess.run(
        [str(part) for part in command], cwd=cwd, env=env, check=True,
        text=True, encoding="utf-8", capture_output=capture,
    )


def git(*arguments, capture=False):
    return run("git", "-C", CHECKOUT, *arguments, capture=capture)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def prepare_checkout(source, *, check_only):
    if not (CHECKOUT / ".git").is_dir():
        if check_only:
            raise ValueError("Telecom is not prepared; run this script without --check-only")
        if CHECKOUT.exists():
            raise ValueError(f"refusing to replace an existing non-checkout: {CHECKOUT}")
        CHECKOUT.parent.mkdir(parents=True, exist_ok=True)
        run("git", "-c", "core.autocrlf=false", "clone", "--depth", "1",
            "--filter=blob:none", "--sparse", "--branch", source["release"],
            source["repository"], CHECKOUT)
    revision = git("rev-parse", "HEAD", capture=True).stdout.strip()
    if revision != source["commit"]:
        raise ValueError(f"unexpected benchmark revision {revision}; expected {source['commit']}")
    # Never reset changes or silently switch an existing benchmark checkout.
    git("diff", "--exit-code", "HEAD", "--")
    if not check_only:
        git("sparse-checkout", "set", *source["sparse_paths"])
    required = [*source["skill_sources"], "LICENSE", "pyproject.toml",
                "src/tau2/evaluator/evaluator.py",
                "data/tau2/domains/telecom/tasks.json",
                "data/tau2/domains/telecom/split_tasks.json",
                "data/tau2/domains/telecom/db.toml",
                "data/tau2/domains/telecom/user_db.toml",
                "data/tau2/user_simulator/simulation_guidelines_tools.md"]
    for name in required:
        if not (CHECKOUT / name).is_file():
            raise ValueError(f"incomplete checkout: {name}")


def materialize_skill(source, *, check_only):
    main, manual = [(CHECKOUT / name).read_text(encoding="utf-8")
                    for name in source["skill_sources"]]
    # Match get_environment(policy_type="manual", solo_mode=False) exactly.
    markdown = ("<main_policy>\n" + main + "\n</main_policy>\n"
                + "<tech_support_policy>\n" + manual + "\n</tech_support_policy>")
    content = markdown.encode("utf-8")
    info = {
        "id": "telecom", "benchmark": "tau2-bench Telecom",
        "source_repository": source["repository"], "source_release": source["release"],
        "source_commit": source["commit"], "source_paths": source["skill_sources"],
        "source_sha256": {name: sha256((CHECKOUT / name).read_bytes())
                          for name in source["skill_sources"]},
        "composition": "Exact default dual-control manual policy returned by get_environment()",
        "sha256": sha256(content), "license": "MIT; see LICENSE.txt",
    }
    expected = {
        SKILL / "initial.md": content,
        SKILL / "source.json": (json.dumps(info, indent=2) + "\n").encode(),
        SKILL / "LICENSE.txt": (CHECKOUT / "LICENSE").read_bytes(),
    }
    for path, data in expected.items():
        if path.exists():
            if path.read_bytes().replace(b"\r\n", b"\n") != data:
                raise ValueError(f"existing skill asset differs; not overwritten: {path}")
        elif check_only:
            raise ValueError(f"missing skill asset: {path}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    return markdown


def inventory(source, markdown):
    domain = CHECKOUT / "data/tau2/domains/telecom"
    tasks = json.loads((domain / "tasks.json").read_text(encoding="utf-8"))
    splits = json.loads((domain / "split_tasks.json").read_text(encoding="utf-8"))
    by_id = {task["id"]: task for task in tasks}
    if len(by_id) != len(tasks):
        raise ValueError("duplicate task IDs")
    for name, ids in splits.items():
        if len(ids) != len(set(ids)) or set(ids) - by_id.keys():
            raise ValueError(f"invalid official task split: {name}")
    base = splits[source["evaluation_split"]]
    families = Counter(task_id.split("]", 1)[0].lstrip("[") for task_id in base)
    return {
        "release": source["release"], "commit": source["commit"],
        "checkout": str(CHECKOUT), "skill": str(SKILL / "initial.md"),
        "skill_words": len(markdown.split()), "skill_characters": len(markdown),
        "task_pool": len(tasks), "official_splits": {key: len(ids) for key, ids in splits.items()},
        "evaluation_split": source["evaluation_split"], "evaluation_families": dict(sorted(families.items())),
        "note": "Splits overlap; the full generated pool is not the 114-task base evaluation.",
    }


def runtime_check():
    python = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        raise ValueError("Telecom venv is not installed; run this script with --install")
    code = """
import importlib.metadata, json, sys
from pathlib import Path
from tau2.domains.telecom.environment import get_environment, get_tasks
from tau2.evaluator.evaluator import evaluate_simulation
env = get_environment()
assert env.policy == Path(sys.argv[1]).read_text(encoding="utf-8")
tasks = get_tasks("base")
assert len(tasks) == 114
print(json.dumps({"tau2_version": importlib.metadata.version("tau2"),
                  "base_tasks_loaded": len(tasks), "agent_tools": len(env.get_tools()),
                  "user_tools": len(env.get_user_tools()),
                  "policy_matches_skill": True, "official_evaluator_importable": callable(evaluate_simulation),
                  "model_calls": 0}))
"""
    environment = {**os.environ, "TAU2_DATA_DIR": str(CHECKOUT / "data"),
                   "PYTHONUTF8": "1", "LITELLM_LOCAL_MODEL_COST_MAP": "True"}
    result = run(python, "-c", code, SKILL / "initial.md", cwd=CHECKOUT,
                 capture=True, env=environment)
    return json.loads(result.stdout)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", help="Install official text runtime in its own venv")
    parser.add_argument("--check-only", action="store_true", help="Verify existing assets without downloading or writing")
    parser.add_argument("--check-runtime", action="store_true", help="Also load the installed environment and evaluator offline")
    args = parser.parse_args(argv)
    if args.install and args.check_only:
        parser.error("--install cannot be combined with --check-only")
    try:
        source = json.loads(SOURCE.read_text(encoding="utf-8"))
        prepare_checkout(source, check_only=args.check_only)
        markdown = materialize_skill(source, check_only=args.check_only)
        summary = inventory(source, markdown)
        if args.install:
            if not (3, 12) <= sys.version_info[:2] < (3, 14):
                raise ValueError("the pinned tau2 runtime needs Python 3.12 or 3.13")
            python = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            if not python.exists():
                venv.create(VENV, with_pip=True)
            run(python, "-m", "pip", "install", "-e", CHECKOUT)
        if args.install or args.check_runtime:
            summary["runtime"] = runtime_check()
        if not args.check_only:
            # Record the actual present source and resolved dependencies, not just the tag.
            files = git("ls-files", capture=True).stdout.splitlines()
            hashes = {name: sha256((CHECKOUT / name).read_bytes())
                      for name in files if (CHECKOUT / name).is_file()}
            receipt = {"source": source, "inventory": summary, "files_sha256": hashes}
            python = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            if python.exists():
                receipt["pip_freeze"] = run(python, "-m", "pip", "freeze", capture=True).stdout.splitlines()
            RECEIPT.parent.mkdir(parents=True, exist_ok=True)
            RECEIPT.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(summary, indent=2))
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr, file=sys.stderr)
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
