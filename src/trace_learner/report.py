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
            costs.setdefault("4 judge while adding rules", {"model_calls": 0, "cost_usd": None})
            costs["4 judge while adding rules"]["model_calls"] += judge_calls
            if "validation" in entry:
                rule["fixed"] = entry["validation"]["fixed"]
                rule["regressions"] = entry["validation"]["regressions"]
            run = root / "validation" / f"c{index}" / "run"
            if (run / "manifest.json").exists():
                for record in load_run(run)[1]:
                    _add(costs, "5 checker re-runs (worker + judge)", *_episode_cost(record))
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


def table(header, rows):
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(str(cell).replace("|", "\\|").replace("\n", " ") for cell in row) + " |"
              for row in rows]
    return "\n".join(lines)


def money(value):
    return "not recorded" if value is None else f"${value:.4f}"


def render_markdown(report):
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
              f"Known total: {money(report['total_known_cost_usd'])}. The judge's calls while adding rules "
              "are counted but their cost is not recorded."]
    return "\n".join(lines) + "\n"


def incremental_report(learner):
    """Report of an incremental learning run: every learning attempt in order, the rules and the cost."""
    rounds = learner.rounds()
    costs, attempts, rules = {}, [], []
    for key in sorted(rounds, key=int):
        item = rounds[key]
        directory = learner.round_dir(item["round"])
        _, records = load_run(directory / "run")
        saved = (read_json(directory / "proposals.json") if (directory / "proposals.json").exists()
                 else {"proposals": [], "merge_usage": []})
        ledger = read_json(directory / "ledger.json") if (directory / "ledger.json").exists() else []
        _add(costs, "3 merge (inspector suggestions)", _cost(saved.get("merge_usage") or []),
             len(saved.get("merge_usage") or []))
        judge = costs.setdefault("4 judge while adding examples and rules", {"model_calls": 0, "cost_usd": None})
        if (directory / "work.json").exists():
            judge["model_calls"] += read_json(directory / "work.json").get("judge_calls", 0)
        proposed = {source: number for number, proposal in enumerate(saved["proposals"])
                    for source in proposal["sources"]}
        decided = {source: entry for entry in ledger for source in entry["sources"]}
        label = item["round"] + 1
        for record in records:
            _add(costs, "1 worker (learning attempts)", *_episode_cost(record))
            episode = episode_key(record)
            row = {"round": label, "task_id": record["task_id"], "replicate": record["replicate"],
                   "tree": item["version_before"], "passed": item["passed"][episode]}
            path = directory / "diagnoses" / (quote(episode, safe="") + ".json")
            if not row["passed"] and path.exists():
                outcome = read_json(path)
                _add(costs, "2 inspector", *_episode_cost(outcome))
                diagnosis = outcome.get("diagnosis") or {}
                entry = decided.get(episode)
                row.update({"inspector": outcome["status"], "attribution": diagnosis.get("attribution"),
                            "root_cause": diagnosis.get("root_cause"),
                            "fix_validated": outcome.get("fix_validated", False), "eligible": outcome["eligible"],
                            "ineligible_reason": outcome.get("ineligible_reason"),
                            "rule": f"{label}.{proposed[episode] + 1}" if episode in proposed else None,
                            "decision": entry["decision"] if entry else None})
            attempts.append(row)
        for entry in ledger:
            proposal = entry["proposal"]
            judge["model_calls"] += (entry.get("insert") or {}).get("judge_calls", 0)
            run = directory / "validation" / f"c{entry['proposal_index']}" / "run"
            if (run / "manifest.json").exists():
                for record in load_run(run)[1]:
                    _add(costs, "5 checker re-runs (worker + judge)", *_episode_cost(record))
            rules.append({"rule": f"{label}.{entry['proposal_index'] + 1}", "kind": proposal["kind"],
                          "node": proposal["target_node_id"], "conditions": _conditions(proposal),
                          "guidance": proposal.get("guidance_text"), "sources": entry["sources"],
                          "decision": entry["decision"], "reason": entry.get("reason"),
                          "fixed": (entry.get("validation") or {}).get("fixed"),
                          "regressions": (entry.get("validation") or {}).get("regressions"),
                          "tree_after": item["version_after"] if entry["decision"] == "accepted" else None})
    known = [c["cost_usd"] for c in costs.values() if c["cost_usd"] is not None]
    passed = [row["passed"] for row in attempts]
    half = len(passed) // 2
    report = {"mode": "incremental", "attempts": attempts, "rules": rules, "costs": dict(sorted(costs.items())),
              "total_known_cost_usd": sum(known), "status": learner.status(),
              "first_half": {"passed": sum(passed[:half]), "attempts": half},
              "second_half": {"passed": sum(passed[half:]), "attempts": len(passed) - half}}
    write_json(learner.root / "report.json", report)
    (learner.root / "report.md").write_text(render_incremental(report), encoding="utf-8")
    return report


def render_incremental(report):
    status, first, second = report["status"], report["first_half"], report["second_half"]
    lines = [
        "# Learning run, one batch at a time",
        "",
        f"- Batch size {status['batch_size']}: {status['rounds_done']} of {status['rounds_total']} rounds done; "
        f"the tree is now v{status['current_version']} (`{status['current_skill']}`).",
        f"- Worker attempts passed: {status['passed']} of {status['attempts']}. First half "
        f"{first['passed']}/{first['attempts']}, second half {second['passed']}/{second['attempts']} "
        "(different documents, so only a rough sign of learning).",
        f"- Inspector diagnoses: {status['diagnoses']}, eligible: {status['eligible']}; rules tried: "
        f"{status['proposals_tried']}, kept by the checker: {status['accepted']}.",
        "",
        "## Learning attempts, in order",
        "",
        table(["Round", "Document", "Rep", "Tree", "Worker", "Inspector blamed", "Root cause", "Fix proven",
               "Rule", "Checker"],
              [[a["round"], a["task_id"], a["replicate"], f"v{a['tree']}", "passed" if a["passed"] else "failed",
                a.get("attribution") or ("" if a["passed"] else a.get("inspector", "not run")),
                _short(a.get("root_cause"), 100), "" if a["passed"] else ("yes" if a.get("fix_validated") else "no"),
                a.get("rule") or ("" if a["passed"] or a.get("eligible") else
                                  _short(a.get("ineligible_reason") or "", 50)),
                a.get("decision") or ""]
               for a in report["attempts"]]),
        "",
        "## Rules",
        "",
    ]
    if report["rules"]:
        lines.append(table(
            ["Rule", "Kind", "Node", "When", "Advice", "Checker", "Why", "Fixed", "Broke", "Tree after"],
            [[r["rule"], r["kind"], r["node"], _short(r["conditions"], 120), _short(r["guidance"]),
              r["decision"], _short(r["reason"] or "", 80),
              ", ".join(f"{t}: {'yes' if ok else 'no'}" for t, ok in (r.get("fixed") or {}).items()),
              ", ".join(r.get("regressions") or []), f"v{r['tree_after']}" if r["tree_after"] is not None else ""]
             for r in report["rules"]]))
    else:
        lines.append("No rules proposed yet.")
    lines += ["", "## Cost", "",
              table(["Stage", "Model calls", "Cost"],
                    [[stage, c["model_calls"], money(c["cost_usd"])] for stage, c in report["costs"].items()]),
              "",
              f"Known total: {money(report['total_known_cost_usd'])}. The judge's calls while adding examples and "
              "rules are counted but their cost is not recorded; its calls during worker runs are included."]
    return "\n".join(lines) + "\n"
