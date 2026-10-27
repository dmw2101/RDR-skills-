"""Tables of one learning run from its saved outputs (no model calls)."""

from pathlib import Path
from urllib.parse import quote

from skill_agent.experiments import load_run
from skill_agent.reporting import episode_usage
from skill_agent.storage import read_json, write_json

from .records import episode_key


def _cost(usages):
    """Total cost of usage dicts; None when any of them has no recorded cost."""
    values = [usage.get("cost_usd") for usage in usages]
    return None if any(v is None for v in values) else sum(values)


def _add(costs, stage, value, calls):
    entry = costs.setdefault(stage, {"model_calls": 0, "cost_usd": 0.0})
    entry["model_calls"] += calls
    entry["cost_usd"] = None if value is None or entry["cost_usd"] is None else entry["cost_usd"] + value


def _episode_cost(record):
    usage = episode_usage(record)
    calls = sum(e["type"] == "model_response" for e in record["events"])
    return usage["cost_usd"], calls


def _conditions(proposal):
    parts = []
    for condition in proposal["conditions"]:
        if "new_feature" in condition:
            feature = condition["new_feature"]
            parts.append(f"{feature['name']} (new): {feature['description']}")
        else:
            parts.append(f"{condition['field']} {condition['op']} {condition['value']}")
    return "; ".join(parts)


def _short(text, limit=160):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def learning_report(learner):
    """Write report.json and report.md to the learner's output_dir and return the report."""
    root = learner.root
    passed = learner.outcomes()
    collected = learner.condition_records()
    costs = {}
    for record in collected:
        _add(costs, "1 collect (worker)", *_episode_cost(record))

    proposals_path = root / "proposals.json"
    saved = read_json(proposals_path) if proposals_path.exists() else {"proposals": [], "merge_usage": []}
    ledger = read_json(root / "ledger.json") if (root / "ledger.json").exists() else []
    decided = {}
    for entry in ledger:
        for source in entry["sources"]:
            decided[source] = entry
    proposed = {source: index for index, item in enumerate(saved["proposals"]) for source in item["sources"]}

    failures = []
    for record in collected:
        key = episode_key(record)
        if passed[key]:
            continue
        path = root / "diagnoses" / (quote(key, safe="") + ".json")
        row = {"episode": key, "task_id": record["task_id"], "replicate": record["replicate"]}
        if not path.exists():
            failures.append({**row, "verifier": "not run yet"})
            continue
        outcome = read_json(path)
        _add(costs, "2 verify (inspector)", *_episode_cost(outcome))
        diagnosis = outcome.get("diagnosis") or {}
        entry = decided.get(key)
        row.update({
            "verifier": outcome["status"],
            "attribution": diagnosis.get("attribution"),
            "root_cause": diagnosis.get("root_cause"),
            "fix_validated": outcome.get("fix_validated", False),
            "eligible": outcome["eligible"],
            "ineligible_reason": outcome.get("ineligible_reason"),
            "proposal": proposed.get(key),
            "decision": entry["decision"] if entry else ("pending" if key in proposed else None),
        })
        failures.append(row)

    _add(costs, "3 consolidate (merge)", _cost(saved.get("merge_usage") or []),
         len(saved.get("merge_usage") or []))

    rules = []
    for index, item in enumerate(saved["proposals"]):
        proposal = item["proposal"]
        entry = ledger[index] if index < len(ledger) else None
        rule = {"proposal": index, "kind": proposal["kind"], "node": proposal["target_node_id"],
                "conditions": _conditions(proposal), "guidance": proposal.get("guidance_text"),
                "sources": item["sources"], "decision": entry["decision"] if entry else "pending",
                "reason": entry.get("reason") if entry else None,
                "version": entry.get("version") if entry else None}
        if entry:
            judge_calls = (entry.get("insert") or {}).get("judge_calls", 0)
            costs.setdefault("4 insert (checker)", {"model_calls": 0, "cost_usd": None})
            costs["4 insert (checker)"]["model_calls"] += judge_calls
            if "validation" in entry:
                rule["fixed"] = entry["validation"]["fixed"]
                rule["regressions"] = entry["validation"]["regressions"]
            run = root / "validation" / f"c{index}" / "run"
            if (run / "manifest.json").exists():
                for record in load_run(run)[1]:
                    _add(costs, "5 validate (worker + checker)", *_episode_cost(record))
        rules.append(rule)

    known = [c["cost_usd"] for c in costs.values() if c["cost_usd"] is not None]
    report = {
        "collect_run": learner.manifest["run_id"],
        "episodes": len(collected), "passed": sum(passed[episode_key(r)] for r in collected),
        "failures": failures, "rules": rules, "costs": dict(sorted(costs.items())),
        "total_known_cost_usd": sum(known),
        "status": learner.status(),
    }
    write_json(root / "report.json", report)
    (root / "report.md").write_text(render_markdown(report), encoding="utf-8")
    return report


def render_markdown(report):
    def table(header, rows):
        lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
        lines += ["| " + " | ".join(str(cell).replace("|", "\\|").replace("\n", " ") for cell in row) + " |"
                  for row in rows]
        return "\n".join(lines)

    def money(value):
        return "not recorded" if value is None else f"${value:.4f}"

    status = report["status"]
    lines = [
        "# Learning run",
        "",
        f"- Collected run `{report['collect_run']}`: {report['passed']} of {report['episodes']} episodes passed, "
        f"{report['episodes'] - report['passed']} failed.",
        f"- Diagnosed: {status['diagnoses']}, eligible: {status['eligible']}, rules tried: "
        f"{status['proposals_tried']}, accepted: {status['accepted']}.",
        f"- Current skill version: `{status['current_skill']}`.",
        "",
        "## Failed episodes",
        "",
        table(["Task", "Rep", "Inspector", "Blamed", "Root cause", "Fix re-scored", "Eligible", "Rule", "Decision"],
              [[f["task_id"], f["replicate"], f["verifier"], f.get("attribution") or "",
                _short(f.get("root_cause")), "yes" if f.get("fix_validated") else "no",
                "yes" if f.get("eligible") else _short(f.get("ineligible_reason") or "no", 60),
                "" if f.get("proposal") is None else f["proposal"], f.get("decision") or ""]
               for f in report["failures"]]),
        "",
        "## Rules",
        "",
    ]
    if report["rules"]:
        lines.append(table(
            ["#", "Kind", "Node", "When", "Advice", "From", "Decision", "Why", "Fixed", "Broke"],
            [[r["proposal"], r["kind"], r["node"], _short(r["conditions"], 120), _short(r["guidance"]),
              len(r["sources"]), r["decision"] + (f" (v{r['version']})" if r["version"] else ""),
              _short(r["reason"] or "", 80),
              ", ".join(f"{t}: {'yes' if ok else 'no'}" for t, ok in (r.get("fixed") or {}).items()),
              ", ".join(r.get("regressions") or [])]
             for r in report["rules"]]))
    else:
        lines.append("No rules proposed yet.")
    lines += ["", "## Cost", "",
              table(["Stage", "Model calls", "Cost"],
                    [[stage, c["model_calls"], money(c["cost_usd"])] for stage, c in report["costs"].items()]),
              "",
              f"Known total: {money(report['total_known_cost_usd'])}. The checker's calls while inserting rules "
              "are counted but their cost is not recorded."]
    return "\n".join(lines) + "\n"
