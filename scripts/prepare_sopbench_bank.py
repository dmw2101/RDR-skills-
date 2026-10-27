"""Fetch the pinned SOPBench Bank environment and case data for the adapter."""

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "benchmarks/local-data/sopbench-li"
URL = "https://github.com/Leezekun/SOPBench.git"
REVISION = "8790ed7406acbd22270f90bd1c4038de3fc5db8f"


def git(*args):
    return subprocess.check_output(["git", "-C", str(DESTINATION), *args], text=True).strip()


def main():
    if not DESTINATION.exists():
        DESTINATION.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-c", "core.autocrlf=false", "--filter=blob:none", "--depth", "1", "--no-checkout", URL, str(DESTINATION)], check=True)
        git("fetch", "--depth", "1", "origin", REVISION)
        git("sparse-checkout", "init", "--cone")
        git("sparse-checkout", "set", "env", "data")
        git("checkout", "--detach", REVISION)
    elif not (DESTINATION / ".git").is_dir():
        raise ValueError(f"existing source directory is not a Git checkout: {DESTINATION}")
    if git("rev-parse", "HEAD") != REVISION:
        raise ValueError("existing source checkout is on a different revision; it was not changed")
    subprocess.run(["git", "-C", str(DESTINATION), "diff", "--exit-code", "HEAD", "--", "env", "data"], check=True)
    git("sparse-checkout", "add", "env", "data")
    if not (DESTINATION / "env/task.py").is_file() or not (DESTINATION / "data/bank_tasks.json").is_file():
        raise ValueError("pinned checkout is incomplete")
    print(f"SOPBench Bank source ready at {DESTINATION}, revision {REVISION}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"error: {error}") from None
