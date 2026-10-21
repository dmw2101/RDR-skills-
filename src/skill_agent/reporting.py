"""Pilot-deck metrics from completed, summarized run folders; no inference or rescoring.

A JSON spec groups run folders into series (for example SCRDR and MCRDR) and runs.
One run may combine several folders, such as a full/reduced run and a separate
skill-blind run over the same documents. Every number comes from the verified
episode records and the official per-document scores written by `summarize`.

The metric definitions are listed in DEFINITIONS and written into every report.
"""

import math
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from .contracts import fingerprint
from .experiments import CONDITIONS, load_run, paired_bootstrap
from .storage import read_json, write_json

DISPLAY_ORDER = ("full", "reduced", "none")
LABELS = {"full": "Full skill", "reduced": "RDR-reduced", "none": "Skill-blind"}
SHORT = {"full": "Full", "reduced": "Reduced", "none": "Blind"}
SPEC_KEYS = {"series", "outliers", "top_documents", "bootstrap_samples"}
DEFINITIONS = [
    "Field exact match: correct normalized fields / scored fields, pooled over documents (official VAREX scorer).",
    "Completed: episodes whose status is 'completed'; unfinished episodes score zero even if an answer file exists.",
    "Perfect documents: episodes in which every scored field matches.",
    "Input tokens per episode: reported agent-model plus resolver input tokens.",
    "Median and p90: linear interpolation between order statistics; mean = total / episodes.",
    "Top-document share: per condition, input tokens summed by document over all runs; share of the top documents.",
    "Differences: pooled over fields (reduced minus baseline); intervals are paired document bootstraps.",
]


# --------------------------------------------------------------------------- #
# Loading and checks
# --------------------------------------------------------------------------- #


def load_spec(path):
    path = Path(path)
    spec = read_json(path)
    if not isinstance(spec, dict) or set(spec) - SPEC_KEYS or not isinstance(spec.get("series"), dict) or not spec["series"]:
        raise ValueError(f"report spec needs a nonempty 'series' object and only {sorted(SPEC_KEYS)}")
    base = path.resolve().parent
    series = {}
    for name, runs in spec["series"].items():
        if not name.strip() or not isinstance(runs, dict) or not runs:
            raise ValueError(f"series {name!r} needs a nonempty object of runs")
        series[name] = {}
        for label, folders in runs.items():
            if (not label.strip() or not isinstance(folders, list) or not folders
                    or any(not isinstance(f, str) or not f.strip() for f in folders)):
                raise ValueError(f"{name} / {label}: list one or more run folders")
            series[name][label] = [(base / folder).resolve() for folder in folders]
    options = {"outliers": spec.get("outliers", 5), "top_documents": spec.get("top_documents", 5),
               "bootstrap_samples": spec.get("bootstrap_samples", 2000)}
    for key, value in options.items():
        if type(value) is not int or value < 1:
            raise ValueError(f"{key} must be a positive integer")
    return series, options


def load_folder(root):
    root = Path(root)
    manifest, records = load_run(root)
    result_path, request_path = root / "scores" / "scoring-result.json", root / "scores" / "scoring-request.json"
    if not result_path.exists() or not request_path.exists():
        raise ValueError(f"{root}: no official scores; run `skill_agent summarize {root}` first")
    scored = sorted(r.get("record_sha256") for r in read_json(request_path)["records"])
    if scored != sorted(r["record_sha256"] for r in records):
        raise ValueError(f"{root}: scores were computed from different episodes; rerun summarize")
    return {"root": root, "manifest": manifest, "records": records,
            "metrics": read_json(result_path)["document_metrics"]}


def episode_usage(record):
    events = record["events"]
    responses = [e for e in events if e["type"] == "model_response"]
    skills = [e for e in events if e["type"] == "skill"]
    usages = [e.get("usage") or {} for e in responses] + [e["response"].get("usage") or {} for e in skills]
    complete = (sum(e["type"] == "model_request" for e in events) == len(responses)
                and sum(e["type"] == "skill_request" for e in events) == len(skills))
    totals = {}
    for name in ("input_tokens", "output_tokens", "cost_usd"):
        values = [u.get(name) for u in usages]
        totals[name] = sum(v for v in values if v is not None)
        totals[name + "_complete"] = all(v is not None for v in values)
    return {"input_tokens": totals["input_tokens"], "output_tokens": totals["output_tokens"],
            "cost_usd": totals["cost_usd"] if totals["cost_usd_complete"] else None,
            "usage_complete": complete and totals["input_tokens_complete"] and totals["output_tokens_complete"]}


def folder_engine(folder):
    """MCRDR responses name their engine; single-classification ones carried a `path`."""
    engines = set()
    for record in folder["records"]:
        if record["condition"] != "reduced":
            continue
        for event in record["events"]:
            if event["type"] == "skill":
                metadata = event["response"].get("metadata") or {}
                engines.add(metadata.get("engine") or ("scrdr" if "path" in metadata else "unknown"))
    return sorted(engines)


def reduced_views(folder):
    """Map each reduced-arm resolver request to the guidance hashes it returned."""
    views = {}
    for record in folder["records"]:
        if record["condition"] != "reduced":
            continue
        for event in record["events"]:
            if event["type"] == "skill":
                key = fingerprint([event["request"]["task"], event["request"]["context"]])
                views.setdefault(key, set()).add(event["markdown_sha256"])
    return views


def settings_of(manifest):
    config = manifest["config"]
    run = config["run"]
    has_skill = any(c != "none" for c in run.get("conditions", CONDITIONS))
    return {"model": config["model"], "split": run.get("split"), "max_calls": run.get("max_calls"),
            "timeout_seconds": run.get("timeout_seconds"),
            "skill_loading": run.get("skill_loading", "model_initiated") if has_skill else None,
            "system_prompt_sha256": fingerprint(manifest.get("system_prompt")),
            "benchmark_fingerprint": manifest.get("benchmark_fingerprint")}


def collect(series_spec):
    """Load folders, build one row per episode, and check pairing within each run."""
    episodes, folders, warnings, views = [], [], [], {}
    for series, runs in series_spec.items():
        for run, roots in runs.items():
            seen = {}
            for root in roots:
                folder = load_folder(root)
                metrics = {(m["task_id"], m["condition"], m["replicate"], m["metric"]): m for m in folder["metrics"]}
                conditions = sorted({r["condition"] for r in folder["records"]})
                for condition in conditions:
                    if condition in seen:
                        raise ValueError(f"{series} / {run}: condition {condition!r} appears in both "
                                         f"{seen[condition]} and {root}")
                    seen[condition] = root
                engines = folder_engine(folder)
                for key, hashes in reduced_views(folder).items():
                    views.setdefault(series, {}).setdefault(key, set()).update(hashes)
                receipt = folder["manifest"].get("skill_receipt") or {}
                folders.append({"series": series, "run": run, "folder": str(root), "conditions": conditions,
                                "episodes": len(folder["records"]), "engine": engines,
                                "resolver_revision": receipt.get("resolver_revision"),
                                "git_revision": folder["manifest"].get("git_revision"),
                                "settings": settings_of(folder["manifest"])})
                for record in folder["records"]:
                    key = (record["task_id"], record["condition"], record["replicate"])
                    em, perfect = metrics.get(key + ("normalized_field_em",)), metrics.get(key + ("perfect_document",))
                    if em is None or perfect is None:
                        raise ValueError(f"{root}: no field scores for {key}; rerun summarize")
                    episodes.append({"series": series, "run": run, "task_id": record["task_id"],
                                     "replicate": record["replicate"], "condition": record["condition"],
                                     "status": record["status"], "error": record.get("error"),
                                     "fields_correct": em["numerator"], "fields_total": em["denominator"],
                                     "perfect": perfect["numerator"], **episode_usage(record)})
            units = {}
            for e in episodes:
                if e["series"] == series and e["run"] == run:
                    units.setdefault(e["condition"], set()).add((e["task_id"], e["replicate"]))
            if len({frozenset(v) for v in units.values()}) > 1:
                raise ValueError(f"{series} / {run}: conditions cover different documents, so they are not paired")
    for series in series_spec:
        engines = {engine for f in folders if f["series"] == series for engine in f["engine"]}
        if len(engines - {"unknown"}) > 1:
            raise ValueError(f"series {series!r} mixes engines {sorted(engines)}; split it into separate series")
        revisions = {f["resolver_revision"] for f in folders if f["series"] == series and f["resolver_revision"]}
        if len(revisions) > 1:
            warnings.append(f"{series}: runs used {len(revisions)} different resolver revisions")
    reference = folders[0]["settings"]
    for key in reference:
        values = {fingerprint(f["settings"][key]) for f in folders
                  if not (key == "skill_loading" and f["settings"][key] is None)}
        if len(values) > 1:
            warnings.append(f"setting {key!r} differs between folders; see provenance")
    return episodes, folders, warnings, views


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


def percentile(values, q):
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * q
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def aggregate(episodes):
    if not episodes:
        return None
    inputs = [e["input_tokens"] for e in episodes]
    costs = [e["cost_usd"] for e in episodes]
    fields_total = sum(e["fields_total"] for e in episodes)
    fields_correct = sum(e["fields_correct"] for e in episodes)
    known_cost = None if any(c is None for c in costs) else sum(costs)
    return {
        "episodes": len(episodes),
        "completed": sum(e["status"] == "completed" for e in episodes),
        "perfect_documents": sum(e["perfect"] for e in episodes),
        "fields_correct": fields_correct, "fields_total": fields_total,
        "field_exact_match": fields_correct / fields_total if fields_total else None,
        "input_tokens": {"median": percentile(inputs, .5), "mean": sum(inputs) / len(inputs),
                         "p90": percentile(inputs, .9), "total": sum(inputs)},
        "output_tokens": {"mean": sum(e["output_tokens"] for e in episodes) / len(episodes),
                          "total": sum(e["output_tokens"] for e in episodes)},
        "cost_usd": {"total": known_cost, "mean": None if known_cost is None else known_cost / len(episodes),
                     "episodes_with_unreported_cost": sum(c is None for c in costs)},
        "episodes_with_incomplete_usage": sum(not e["usage_complete"] for e in episodes),
    }


def differences(by_condition):
    result = {}
    reduced = by_condition.get("reduced")
    for baseline in ("full", "none"):
        if reduced and by_condition.get(baseline):
            result[f"reduced_minus_{baseline}"] = (reduced["field_exact_match"]
                                                   - by_condition[baseline]["field_exact_match"])
    return result


def metric_rows(episodes):
    rows = []
    for e in episodes:
        rows.append({"task_id": e["task_id"], "condition": e["condition"], "replicate": e["replicate"],
                     "metric": "normalized_field_em", "numerator": e["fields_correct"], "denominator": e["fields_total"]})
        rows.append({"task_id": e["task_id"], "condition": e["condition"], "replicate": e["replicate"],
                     "metric": "perfect_document", "numerator": e["perfect"], "denominator": 1})
    return rows


def series_report(series, runs, episodes, options):
    mine = [e for e in episodes if e["series"] == series]
    conditions = [c for c in DISPLAY_ORDER if any(e["condition"] == c for e in mine)]
    by_run = {}
    for run in runs:
        rows = [e for e in mine if e["run"] == run]
        stats = {c: aggregate([e for e in rows if e["condition"] == c]) for c in conditions}
        stats = {c: v for c, v in stats.items() if v}
        by_run[run] = {"conditions": stats, "differences": differences(stats),
                       "paired_bootstrap": paired_bootstrap(metric_rows(rows), samples=options["bootstrap_samples"])}
    pooled = {c: aggregate([e for e in mine if e["condition"] == c]) for c in conditions}
    medians = {c: v["input_tokens"]["median"] for c, v in pooled.items()}
    top = {}
    for condition in conditions:
        per_document = {}
        for e in mine:
            if e["condition"] == condition:
                per_document[e["task_id"]] = per_document.get(e["task_id"], 0) + e["input_tokens"]
        ranked = sorted(per_document.items(), key=lambda item: (-item[1], item[0]))
        total = sum(per_document.values())
        chosen = ranked[:options["top_documents"]]
        top[condition] = {"documents": [d for d, _ in chosen],
                          "share": sum(v for _, v in chosen) / total if total else None}
    index = {(e["run"], e["task_id"], e["replicate"], e["condition"]): e for e in mine}
    outliers = {}
    for condition in conditions:
        ranked = sorted((e for e in mine if e["condition"] == condition),
                        key=lambda e: (-e["input_tokens"], e["run"], e["task_id"]))[:options["outliers"]]
        outliers[condition] = [{
            "run": e["run"], "task_id": e["task_id"], "replicate": e["replicate"], "status": e["status"],
            "input_tokens": e["input_tokens"],
            "paired_input_tokens": {c: index[(e["run"], e["task_id"], e["replicate"], c)]["input_tokens"]
                                    for c in conditions if (e["run"], e["task_id"], e["replicate"], c) in index},
        } for e in ranked]
    return {
        "conditions": conditions,
        "runs": by_run,
        "pooled": {"conditions": pooled, "differences": differences(pooled),
                   "paired_bootstrap": paired_bootstrap(metric_rows(mine), samples=options["bootstrap_samples"]),
                   "median_input_reduction_vs_full": (
                       (medians["full"] - medians["reduced"]) / medians["full"]
                       if medians.get("full") and "reduced" in medians else None)},
        "top_document_input_share": top,
        "outlier_episodes": outliers,
        "incomplete_episodes": [{k: e[k] for k in ("condition", "run", "task_id", "replicate", "status", "error")}
                                for e in mine if e["status"] != "completed"],
    }


def guidance_identity(names, hashes):
    """Reduced-arm views for the same resolver request must not differ between engines."""
    hashes = {name: hashes.get(name, {}) for name in names}
    result = {}
    for other in names[1:]:
        shared = set(hashes[names[0]]) & set(hashes[other])
        differing = [key for key in shared if hashes[names[0]][key] != hashes[other][key]]
        result[f"{other}_vs_{names[0]}"] = {"shared_requests": len(shared), "identical": len(shared) - len(differing),
                                            "differing": len(differing)}
    within = {series: sum(len(v) > 1 for v in views.values()) for series, views in hashes.items()}
    return {"between_series": result, "requests_with_several_views_within_series": within}


def build_report(spec_path):
    series_spec, options = load_spec(spec_path)
    episodes, folders, warnings, views = collect(series_spec)
    report = {
        "spec": str(Path(spec_path).resolve()), "options": options, "definitions": DEFINITIONS,
        "series": {name: series_report(name, runs, episodes, options) for name, runs in series_spec.items()},
        "provenance": folders, "warnings": warnings,
    }
    names = list(series_spec)
    if len(names) > 1:
        baseline = report["series"][names[0]]["pooled"]["conditions"]
        comparison = {}
        for other in names[1:]:
            current = report["series"][other]["pooled"]["conditions"]
            comparison[f"{other}_vs_{names[0]}"] = {
                condition: {
                    "field_exact_match": current[condition]["field_exact_match"] - baseline[condition]["field_exact_match"],
                    "completed": current[condition]["completed"] - baseline[condition]["completed"],
                    "perfect_documents": current[condition]["perfect_documents"] - baseline[condition]["perfect_documents"],
                    "median_input_change": (current[condition]["input_tokens"]["median"]
                                            / baseline[condition]["input_tokens"]["median"] - 1
                                            if baseline[condition]["input_tokens"]["median"] else None),
                    "mean_input_change": (current[condition]["input_tokens"]["mean"]
                                          / baseline[condition]["input_tokens"]["mean"] - 1
                                          if baseline[condition]["input_tokens"]["mean"] else None),
                } for condition in DISPLAY_ORDER if condition in current and condition in baseline}
        report["series_comparison"] = comparison
        report["guidance_identity"] = guidance_identity(names, views)
    return report


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #


def _round(value, places=0):
    quantum = Decimal(1).scaleb(-places)
    return Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)


def pct(value):
    return "—" if value is None else f"{_round(value * 100, 2)}%"


def pp(value):
    return "—" if value is None else f"{'+' if value >= 0 else '−'}{_round(abs(value) * 100, 2)} pp"


def tokens(value):
    return "—" if value is None else f"{int(_round(value)):,}"


def kilo(value):
    return "—" if value is None else f"{_round(value / 1000, 1)}k"


def money(value):
    return "not reported" if value is None else f"${_round(value, 4)}"


def table(header, rows):
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    return lines + ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]


def render_markdown(report):
    out = ["# VAREX skill-guidance report", ""]
    for name, data in report["series"].items():
        conditions = data["conditions"]
        pooled = data["pooled"]["conditions"]
        head = [LABELS[c] for c in conditions]
        out += [f"## {name}", "", "### Field exact match (normalized)", ""]
        rows = [[run] + [pct(r["conditions"][c]["field_exact_match"]) if c in r["conditions"] else "—" for c in conditions]
                for run, r in data["runs"].items()]
        fields = {pooled[c]["fields_total"] for c in conditions}
        label = f"Pooled ({fields.pop():,} fields)" if len(fields) == 1 else "Pooled"
        rows.append([label] + [pct(pooled[c]["field_exact_match"]) for c in conditions])
        out += table(["Run"] + head, rows) + [""]
        out.append("Correct fields: " + "     ".join(
            f"{SHORT[c]} {pooled[c]['fields_correct']:,} / {pooled[c]['fields_total']:,}" for c in conditions))
        out += ["", "### Completion and perfect documents", ""]
        out += table([""] + head, [
            ["Completed"] + [f"{pooled[c]['completed']} / {pooled[c]['episodes']}" for c in conditions],
            ["Perfect documents"] + [f"{pooled[c]['perfect_documents']} / {pooled[c]['episodes']}" for c in conditions],
        ]) + [""]
        compared = [b for b in ("full", "none") if b in conditions and "reduced" in conditions]
        if compared:
            out += ["### Reduced accuracy difference", ""]
            rows = [[run] + [pp(r["differences"].get(f"reduced_minus_{b}")) for b in compared]
                    for run, r in data["runs"].items()]
            rows.append(["Pooled"] + [pp(data["pooled"]["differences"].get(f"reduced_minus_{b}")) for b in compared])
            out += table(["Run"] + [f"Reduced − {SHORT[b].lower()}" for b in compared], rows) + [""]
            for b in compared:
                ci = data["pooled"]["paired_bootstrap"].get(f"normalized_field_em:reduced_minus_{b}")
                if ci:
                    low, high = ci["paired_document_bootstrap_95pct"]
                    out.append(f"Pooled reduced − {SHORT[b].lower()} 95% document-bootstrap interval: "
                               f"{pp(low)} to {pp(high)} ({ci['documents']} documents).")
            out.append("")
        out += ["### Input usage per episode", ""]
        rows = [["Median (p50) input tokens"] + [tokens(pooled[c]["input_tokens"]["median"]) for c in conditions],
                ["Mean input tokens"] + [kilo(pooled[c]["input_tokens"]["mean"]) for c in conditions],
                ["p90 input tokens"] + [tokens(pooled[c]["input_tokens"]["p90"]) for c in conditions],
                ["Mean output tokens"] + [kilo(pooled[c]["output_tokens"]["mean"]) for c in conditions],
                ["Reported cost (total)"] + [money(pooled[c]["cost_usd"]["total"]) for c in conditions],
                ["Reported cost (mean per episode)"] + [money(pooled[c]["cost_usd"]["mean"]) for c in conditions],
                ["Episodes with incomplete usage"] + [pooled[c]["episodes_with_incomplete_usage"] for c in conditions]]
        out += table([""] + head, rows) + [""]
        reduction = data["pooled"]["median_input_reduction_vs_full"]
        if reduction is not None:
            word = "fewer" if reduction >= 0 else "more"
            out += [f"Reduced uses {_round(abs(reduction) * 100, 1)}% {word} median input tokens than full skill.", ""]
        out += ["### Variability", ""]
        out.append("Share of input tokens from the five highest-usage documents: " + " · ".join(
            f"{SHORT[c]} {pct(data['top_document_input_share'][c]['share'])}" for c in conditions))
        out += ["", "Highest-input episodes:", ""]
        rows = []
        for c in conditions:
            for o in data["outlier_episodes"][c]:
                rows.append([LABELS[c], o["run"], o["task_id"], tokens(o["input_tokens"]), o["status"]]
                            + [tokens(o["paired_input_tokens"].get(p)) for p in conditions])
        out += table(["Condition", "Run", "Document", "Input tokens", "Status"]
                     + [f"{SHORT[c]} input" for c in conditions], rows) + [""]
        if data["incomplete_episodes"]:
            out += ["Incomplete episodes (scored as zero):", ""]
            out += table(["Condition", "Run", "Document", "Status"],
                         [[LABELS[e["condition"]], e["run"], e["task_id"], e["status"]]
                          for e in data["incomplete_episodes"]]) + [""]
    if "series_comparison" in report:
        out += ["## Series comparison", ""]
        for name, conditions in report["series_comparison"].items():
            out += [f"### {name.replace('_vs_', ' vs ')}", ""]
            out += table(["Condition", "Field exact match", "Completed", "Perfect documents",
                          "Median input", "Mean input"],
                         [[LABELS[c], pp(v["field_exact_match"]), f"{v['completed']:+d}",
                           f"{v['perfect_documents']:+d}", pct(v["median_input_change"]),
                           pct(v["mean_input_change"])] for c, v in conditions.items()]) + [""]
        for name, check in report["guidance_identity"]["between_series"].items():
            out.append(f"Reduced guidance for identical resolver requests ({name.replace('_vs_', ' vs ')}): "
                       f"{check['identical']} of {check['shared_requests']} shared requests identical, "
                       f"{check['differing']} differing.")
        out.append("")
    out += ["## Provenance", ""]
    out += table(["Series", "Run", "Conditions", "Episodes", "Engine", "Resolver revision", "Folder"],
                 [[f["series"], f["run"], ", ".join(f["conditions"]), f["episodes"],
                   ", ".join(f["engine"]) or "—", (f["resolver_revision"] or "—")[:12], f["folder"]]
                  for f in report["provenance"]]) + [""]
    if report["warnings"]:
        out += ["Warnings:", ""] + [f"- {w}" for w in report["warnings"]] + [""]
    out += ["Definitions:", ""] + [f"- {line}" for line in report["definitions"]]
    return "\n".join(out) + "\n"


def write_report(spec_path, output_dir=None):
    report = build_report(spec_path)
    target = Path(output_dir) if output_dir else Path(spec_path).resolve().parent / "report"
    write_json(target / "report.json", report)
    (target / "report.md").write_text(render_markdown(report), encoding="utf-8")
    return {"report_json": str(target / "report.json"), "report_markdown": str(target / "report.md"),
            "series": list(report["series"]), "warnings": report["warnings"]}
