"""Import pinned, public procedural references for inspection, without model calls.

The video-annotation review copy is deliberately Git-ignored because its domain
license adds distribution/access-control conditions. No benchmark answers, task
datasets, executable tools, or dependencies are imported into skills/.
"""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "benchmarks/local-data"
AMAZON_DATA = "src/amazon_sop_bench/benchmarks/data"
COBOL = "tasks-extra/cobol-gl-batch-reconcile/environment/skills"
BANKING = "data/tau2/domains/banking_knowledge"
REPOS = {
    "sopbench-li": ("https://github.com/Leezekun/SOPBench.git",
                    "8790ed7406acbd22270f90bd1c4038de3fc5db8f", ["reference"]),
    "sopbench-amazon": ("https://github.com/amazon-science/SOP-Bench.git",
                        "f6d43858b94cf439854038101bce0eecd3e16eae",
                        [f"{AMAZON_DATA}/video_annotation", f"{AMAZON_DATA}/referral_abuse_detection_v2"]),
    "skillsbench": ("https://github.com/benchflow-ai/skillsbench.git",
                    "f860ecf02ef53412a030f8661c83d644b739ff01", [COBOL]),
    "tau2-bench": ("https://github.com/sierra-research/tau2-bench.git",
                   "fc0055dc4e0a316c3f83133267fbd6faaa770992",
                   [f"{BANKING}/documents", f"{BANKING}/prompts"]),
}
CANDIDATES = [
    {"id": "sopbench_bank", "repo": "sopbench-li", "benchmark": "SOPBench Bank (Li et al.)",
     "paper": "https://arxiv.org/abs/2503.08669", "license": "CC BY 4.0 data; MIT code/documentation",
     "entry": "reference/prompts/bank_system_prompt.md",
     "references": {"references/bank.md": "reference/domains/bank.md",
                    "references/tools.json": "reference/schemas/bank_tools.json"},
     "licenses": {"LICENSE.txt": "LICENSE", "DATA_LICENSE.txt": "DATA_LICENSE"},
     "note": "Original bank system prompt, copied without rewriting. AND/OR/ordered prerequisites for bank actions. The reference and tool schema are supporting material, not additions to initial.md."},
    {"id": "sopbench_video_annotation", "repo": "sopbench-amazon", "benchmark": "Amazon SOP-Bench video_annotation",
     "paper": "https://arxiv.org/abs/2506.08119", "license": "CC BY-NC 4.0 plus domain-specific distribution/access-control conditions",
     "entry": f"{AMAZON_DATA}/video_annotation/sop.txt",
     "references": {"references/tools.json": f"{AMAZON_DATA}/video_annotation/toolspecs.json",
                    "references/disclaimer.txt": f"{AMAZON_DATA}/video_annotation/disclaimer.txt"},
     "licenses": {"LICENSE.txt": "LICENSE", "DOMAIN_LICENSE.md": f"{AMAZON_DATA}/video_annotation/LICENSE.md"},
     "local_only": True,
     "note": "Original SOP, copied without rewriting. The initial.md and references review copies are local-only and Git-ignored. The upstream domain notice requests access-controlled storage and restricts redistribution/model training. Do not publish these copies automatically; retain the upstream notices and review the conditions before reuse."},
    {"id": "sopbench_referral_abuse", "repo": "sopbench-amazon", "benchmark": "Amazon SOP-Bench referral_abuse_detection_v2",
     "paper": "https://arxiv.org/abs/2506.08119", "license": "CC BY-NC 4.0",
     "entry": f"{AMAZON_DATA}/referral_abuse_detection_v2/sop.txt",
     "references": {"references/tools.json": f"{AMAZON_DATA}/referral_abuse_detection_v2/toolspecs.json"},
     "licenses": {"LICENSE.txt": "LICENSE"},
     "note": "Original v2 SOP, copied without rewriting. Referral-abuse investigation, risk thresholds, violation history, and enforcement. Noncommercial research/evaluation use; preserve attribution. This exact repository version is not asserted to match the paper's reported evaluation."},
    {"id": "tau_banking", "repo": "tau2-bench", "benchmark": "tau-Knowledge / tau-Banking, v1.0.1",
     "paper": "https://arxiv.org/abs/2603.04370", "license": "MIT",
     "references": {"references/full_kb_prompt.md": f"{BANKING}/prompts/full_kb.md",
                    "references/no_knowledge_prompt.md": f"{BANKING}/prompts/no_knowledge.md"},
     "licenses": {"LICENSE.txt": "LICENSE"},
     "note": "This is a corpus, not one official SKILL.md. initial.md is a deterministic browsing view of all 698 original documents, sorted by filename, with source comments and document-title headings. No document content is rewritten. Original JSON documents are retained. It is not a filled official agent prompt or a chosen procedural subset."},
    {"id": "cobol_gl_reconciliation", "repo": "skillsbench", "benchmark": "SkillsBench tasks-extra/cobol-gl-batch-reconcile",
     "paper": "https://arxiv.org/abs/2602.12670", "license": "Apache-2.0",
     "references": {}, "licenses": {"LICENSE.txt": "LICENSE"},
     "note": "The six original SKILL.md files are preserved under procedures/. initial.md concatenates those files, with source headings, for inspection; it is not an upstream monolithic skill. Only one extra task is associated with this bundle, not the paper's standard benchmark suite. No oracle solution or verifier answers are imported."},
]


def git(directory, *arguments):
    result = subprocess.run(["git", "-C", str(directory), *arguments], check=True,
                            capture_output=True, text=True, encoding="utf-8")
    return result.stdout.strip()


def checkout(name, *, check_only):
    url, revision, paths = REPOS[name]
    directory = DATA / name
    if not (directory / ".git").is_dir():
        if check_only or directory.exists():
            raise ValueError(f"missing or non-Git source checkout: {directory}")
        directory.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--filter=blob:none", "--depth", "1",
                        "--no-checkout", url, str(directory)], check=True)
        git(directory, "config", "core.autocrlf", "false")
        git(directory, "fetch", "--depth", "1", "origin", revision)
        git(directory, "sparse-checkout", "init", "--cone")
        git(directory, "sparse-checkout", "set", *paths)
        git(directory, "checkout", "--detach", revision)
    if git(directory, "rev-parse", "HEAD") != revision:
        raise ValueError(f"source revision differs; not switched automatically: {directory}")
    git(directory, "diff", "--exit-code", "HEAD", "--")
    if not check_only:
        # Add, not set: the existing Telecom checkout must keep its Telecom files.
        git(directory, "sparse-checkout", "add", *paths)
    return directory


def materialize(path, data, *, check_only):
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError(f"existing asset differs; not overwritten: {path}")
    elif check_only:
        raise ValueError(f"missing imported file: {path}")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def import_candidate(spec, source, *, check_only):
    destination = ROOT / "skills" / spec["id"]
    paths = {**spec["references"], **spec["licenses"]}
    if "entry" in spec:
        paths["initial.md"] = spec["entry"]
        markdown = (source / spec["entry"]).read_bytes()
    elif spec["id"] == "tau_banking":
        documents = sorted((source / BANKING / "documents").glob("*.json"))
        if len(documents) != 698:
            raise ValueError("expected the pinned 698-document banking corpus")
        pieces = ["# Tau-Banking knowledge corpus\n\nBrowsing view of the original documents; see README.md.\n"]
        index = ["# Original knowledge documents\n\n| Document | Title |\n|---|---|\n"]
        for path in documents:
            document = json.loads(path.read_text(encoding="utf-8"))
            paths[f"documents/{path.name}"] = path.relative_to(source).as_posix()
            pieces.append(f"<!-- source: documents/{path.name}; id: {document['id']} -->\n"
                          f"## {document['title']}\n\n{document['content']}")
            title = document["title"].replace("|", "\\|").replace("\n", " ")
            index.append(f"| [{document['id']}](<documents/{path.name}>) | {title} |\n")
        markdown = "\n\n---\n\n".join(pieces).encode("utf-8")
        materialize(destination / "DOCUMENTS.md", "".join(index).encode("utf-8"), check_only=check_only)
    else:
        originals = sorted((source / COBOL).glob("*/SKILL.md"))
        if len(originals) != 6:
            raise ValueError("expected six original COBOL skills")
        pieces = ["# COBOL GL reconciliation skill bundle\n\nInspection bundle; six original skills are retained under procedures/.\n"]
        for path in originals:
            local = f"procedures/{path.parent.name}/SKILL.md"
            paths[local] = path.relative_to(source).as_posix()
            pieces.append(f"<!-- source: {local} -->\n## {path.parent.name}\n\n"
                          + path.read_text(encoding="utf-8"))
        markdown = "\n\n---\n\n".join(pieces).encode("utf-8")
    hashes = {}
    for local, original in paths.items():
        data = (source / original).read_bytes()
        materialize(destination / local, data, check_only=check_only)
        hashes[local] = {"source_path": original, "sha256": hashlib.sha256(data).hexdigest()}
    if "entry" not in spec:
        materialize(destination / "initial.md", markdown, check_only=check_only)
    info = {"id": spec["id"], "benchmark": spec["benchmark"], "paper": spec["paper"],
            "source_repository": REPOS[spec["repo"]][0], "source_commit": REPOS[spec["repo"]][1],
            "license": spec["license"], "import_note": spec["note"],
            "redistribution": "local-only review copy" if spec.get("local_only") else "retain upstream license and attribution",
            "initial_sha256": hashlib.sha256(markdown).hexdigest(),
            "initial_words": len(markdown.decode("utf-8").split()), "original_files": hashes}
    materialize(destination / "source.json", (json.dumps(info, indent=2) + "\n").encode("utf-8"), check_only=check_only)
    readme = (f"# {spec['benchmark']}\n\n{spec['note']}\n\n"
              f"Start with `initial.md` ({info['initial_words']:,} whitespace-separated words, not model tokens). "
              "See `source.json` for exact source paths and SHA-256 checksums.\n\n"
              f"Source: [{REPOS[spec['repo']][0]}]({REPOS[spec['repo']][0]}), "
              f"commit `{REPOS[spec['repo']][1]}`.\n\n"
              f"Benchmark paper: [{spec['paper']}]({spec['paper']}).\n\n"
              f"Upstream license: **{spec['license']}**. License notices are retained alongside this file. "
              "The source authors retain their attribution; inclusion here does not imply endorsement.\n\n"
              "Status: inspection assets only. No feature schema, IR, compiled MCRDR tree, "
              "benchmark adapter, or evaluation result is provided for this candidate yet. "
              "No model calls or task executions were used to import it. "
              "Task instances, reference answers, and oracle solutions are not part of this skill.\n")
    materialize(destination / "README.md", readme.encode("utf-8"), check_only=check_only)
    return {"skill": spec["id"], "words": info["initial_words"],
            "original_files": len(hashes), "local_only": spec.get("local_only", False)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-only", action="store_true", help="Verify cached source and copied files, without network or writes")
    args = parser.parse_args(argv)
    try:
        sources = {name: checkout(name, check_only=args.check_only) for name in REPOS}
        results = [import_candidate(spec, sources[spec["repo"]], check_only=args.check_only)
                   for spec in CANDIDATES]
        print(json.dumps(results, indent=2))
        return 0
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr, file=sys.stderr)
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
