"""Fetch the datasets and licensed skills listed in a paper folder's sources.json.

    python papers/fetch.py Trace2Skill            # every item marked "now"
    python papers/fetch.py SkillOpt --only docvqa  # one item, including on-demand ones
    python papers/fetch.py WikiSkill --list        # show items, sizes and status
    python papers/fetch.py SkillOpt --check-only   # verify checksums, no network

Downloads go to <paper>/local-data/<id>/ and licensed skills to
<paper>/skills/licensed/. Both are git-ignored: never commit them. Gated Hugging
Face datasets (OfficeQA, GPQA, SlideVQA) need the terms accepted on the website
and HF_TOKEN set. A "ref" item reuses another paper folder's item instead of
downloading a second copy; "repo" items already live in this repository; "manual"
items need a registration step and only print instructions.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

PAPERS = Path(__file__).resolve().parent
REPO = PAPERS.parent
BENCHMARKS = REPO / "benchmarks"
AGENT = "rdr-skills-benchmark-prep/1.0"


# ---------------------------------------------------------------- helpers
def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def request(url: str) -> Request:
    headers = {"User-Agent": AGENT}
    token = os.environ.get("HF_TOKEN")
    if token and url.startswith("https://huggingface.co/"):
        headers["Authorization"] = f"Bearer {token}"
    return Request(url, headers=headers)


def download(url: str, destination: Path, expected: str | None, check_only: bool) -> str:
    if not destination.exists():
        if check_only:
            raise FileNotFoundError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        part = destination.with_name(destination.name + ".part")
        with urlopen(request(url), timeout=300) as response, part.open("wb") as target:
            shutil.copyfileobj(response, target)
        part.replace(destination)
    actual = sha256(destination)
    if expected and actual != expected:
        raise ValueError(f"checksum mismatch for {destination}: expected {expected}, got {actual}")
    return actual


def inside(root: Path, name: str) -> Path:
    target = (root / name.replace("\\", "/")).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError(f"unsafe archive path: {name}")
    return target


def extract(archive: Path, destination: Path) -> None:
    """Extract a .zip or .tar.gz, refusing paths that escape the destination."""
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as source:
            for member in source.infolist():
                target = inside(destination, member.filename)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                elif not (target.exists() and target.stat().st_size == member.file_size):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with source.open(member) as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
    else:
        with tarfile.open(archive, "r:*") as source:
            for member in source:
                target = inside(destination, member.name)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    if target.exists() and target.stat().st_size == member.size:
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with source.extractfile(member) as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                else:
                    raise ValueError(f"unsupported archive entry: {member.name}")


def load_module(path: Path, local: Path):
    """Import one of benchmarks/*.py with its output folder redirected to `local`."""
    spec = importlib.util.spec_from_file_location(f"_bench_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.LOCAL = local
    return module


# ---------------------------------------------------------------- methods
def fetch_hf(item: dict, target: Path, check_only: bool) -> dict:
    repo, revision = item["repo"], item["revision"]
    files = list(item.get("files", []))
    if item.get("patterns"):
        if check_only:  # listing needs the network; just confirm something is there
            if not target.exists() or not any(target.iterdir()):
                raise FileNotFoundError(target)
            return {"present": str(target)}
        with urlopen(request(f"https://huggingface.co/api/datasets/{repo}/revision/{revision}"), timeout=120) as r:
            names = [s["rfilename"] for s in json.load(r)["siblings"]]
        files += [n for n in names if any(fnmatch.fnmatch(n, p) for p in item["patterns"])]
    sums = {}
    for name in files:
        url = f"https://huggingface.co/datasets/{repo}/resolve/{revision}/{name}"
        destination = target / (Path(name).name if item.get("flatten") else name)
        sums[name] = download(url, destination, item.get("sha256", {}).get(name), check_only)
        if item.get("extract") and not check_only:
            extract(destination, target)
    return sums


def fetch_url(item: dict, target: Path, check_only: bool) -> dict:
    sums = {}
    for url in item["urls"]:
        name = item.get("names", {}).get(url) or url.rsplit("/", 1)[-1]
        destination = target / name
        sums[name] = download(url, destination, item.get("sha256", {}).get(name), check_only)
        if item.get("extract") and not check_only and name.endswith((".zip", ".tar.gz", ".tgz")):
            extract(destination, target)
    return sums


def fetch_git(item: dict, target: Path, check_only: bool) -> dict:
    """Shallow (and optionally sparse) checkout of one pinned commit."""
    if (target / ".git").exists():
        head = subprocess.run(["git", "-C", str(target), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
        if head != item["commit"]:
            raise ValueError(f"{target} is at {head}, expected {item['commit']}")
        return {"commit": head}
    if check_only:
        raise FileNotFoundError(target)
    target.mkdir(parents=True, exist_ok=True)
    git = ["git", "-C", str(target)]
    subprocess.run(git + ["init", "-q"], check=True)
    subprocess.run(git + ["remote", "add", "origin", item["repo"]], check=True)
    fetch = ["fetch", "-q", "--depth", "1"]
    if item.get("paths"):
        subprocess.run(git + ["sparse-checkout", "set", "--no-cone", *item["paths"]], check=True)
        fetch.append("--filter=blob:none")
    subprocess.run(git + fetch + ["origin", item["commit"]], check=True)
    subprocess.run(git + ["checkout", "-q", "FETCH_HEAD"], check=True)
    return {"commit": item["commit"]}


def fetch_copy(item: dict, target: Path, check_only: bool, paper: Path) -> dict:
    """Copy folders out of another item that is already fetched."""
    source_root = paper / item["from"]
    copied = {}
    for relative in item["paths"]:
        source = source_root / relative
        if not source.exists():
            raise FileNotFoundError(f"{source} (fetch the item it comes from first)")
        destination = target / Path(relative).name
        if not check_only and not destination.exists():
            shutil.copytree(source, destination)
        copied[relative] = str(destination.relative_to(paper))
    return copied


def fetch_delegate(item: dict, target: Path, check_only: bool) -> dict:
    """Run one of the repo's existing preparation routines into this paper's folder."""
    call = item["call"]
    if call == "varex":
        sys.path.insert(0, str(REPO / "src"))
        from skill_agent.benchmarks.varex import prepare_varex
        return {"result": str(prepare_varex(str(target), **item.get("kwargs", {})))}
    if call == "alfworld":
        args = [sys.executable, str(BENCHMARKS / "prepare_alfworld.py"), "--data-root", str(target)]
        subprocess.run(args + (["--check-only"] if check_only else []), check=True)
        return {"data_root": str(target)}
    if call in ("docvqa", "searchqa"):
        module = load_module(BENCHMARKS / "materialize_remote.py", target.parent)
        getattr(module, call)()
        return {"split_root": str(target)}
    raise ValueError(f"unknown delegate call: {call}")


def materialize(name: str, local: Path) -> None:
    """Rebuild the exact SkillOpt split for SpreadsheetBench or LiveMath under `local`."""
    module = load_module(BENCHMARKS / "materialize_small.py", local)
    getattr(module, name)()


# ---------------------------------------------------------------- main
def load_items(paper: Path) -> list[dict]:
    sources = json.loads((paper / "sources.json").read_text(encoding="utf-8"))
    return sources["datasets"] + sources.get("licensed_skills", [])


def resolve(paper: Path, item: dict) -> tuple[Path, dict]:
    """Follow a "ref" item to the paper folder and item that actually hold the data."""
    if item["method"] != "ref":
        return paper, item
    other = PAPERS / item["paper"]
    target = next((i for i in load_items(other) if i["id"] == item["item"]), None)
    if target is None:
        raise ValueError(f"{item['id']}: {item['paper']} has no item {item['item']}")
    return other, target


def location(paper: Path, item: dict) -> Path:
    if item["method"] == "repo":
        return REPO / item["path"]
    owner, real = resolve(paper, item)
    return owner / real.get("dest", f"local-data/{real['id']}")


def run(paper_name: str, only: list[str] | None, check_only: bool, list_only: bool) -> int:
    paper = PAPERS / paper_name
    items = load_items(paper)
    if list_only:
        for item in items:
            where = location(paper, item)
            status = "present" if where.exists() and any(where.iterdir()) else "missing"
            print(f"{item['id']:<28} {item['when']:<10} ~{item.get('approx_mb', '?'):>6} MB  {status:<8} {item['name']}")
        return 0
    chosen = [i for i in items if (i["id"] in only if only else i["when"] == "now")]
    if only and len(chosen) != len(only):
        known = {i["id"] for i in items}
        raise SystemExit(f"unknown id(s): {sorted(set(only) - known)}")
    log_path = paper / "local-data" / "fetch-log.json"
    log = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else {}
    failures = 0
    for item in chosen:
        print(f"== {item['id']}: {item['name']} (~{item.get('approx_mb', '?')} MB)", flush=True)
        if item["method"] == "manual":
            print(f"   manual download: {item['instructions']}")
            continue
        if item["method"] == "repo":
            where = REPO / item["path"]
            ok = where.exists()
            print(f"   {'ok' if ok else 'MISSING'} -> {item['path']} (already in this repo)")
            failures += not ok
            continue
        owner, real = resolve(paper, item)
        if owner != paper:
            print(f"   reusing {owner.name}/{real['id']}")
        target = owner / real.get("dest", f"local-data/{real['id']}")
        try:
            method = real["method"]
            if method == "hf":
                result = fetch_hf(real, target, check_only)
            elif method == "url":
                result = fetch_url(real, target, check_only)
            elif method == "git":
                result = fetch_git(real, target, check_only)
            elif method == "copy":
                result = fetch_copy(real, target, check_only, owner)
            elif method == "delegate":
                result = fetch_delegate(real, target, check_only)
            else:
                raise ValueError(f"unknown method {method}")
            if real.get("materialize") and not check_only:
                materialize(real["materialize"], target.parent)
            log[item["id"]] = result
            print(f"   ok -> {target.relative_to(REPO)}")
        except Exception as exc:  # keep going so one bad source does not block the rest
            failures += 1
            print(f"   FAILED: {exc}", file=sys.stderr)
    if not check_only:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(json.dumps(log, indent=2), encoding="utf-8")
    return 1 if failures else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paper", choices=sorted(p.name for p in PAPERS.iterdir() if (p / "sources.json").exists()))
    parser.add_argument("--only", nargs="+", help="fetch just these item ids (on-demand items included)")
    parser.add_argument("--check-only", action="store_true", help="verify what is on disk; no network")
    parser.add_argument("--list", action="store_true", help="list items and whether they are present")
    args = parser.parse_args()
    raise SystemExit(run(args.paper, args.only, args.check_only, args.list))


if __name__ == "__main__":
    main()
