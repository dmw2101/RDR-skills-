"""Readable, untruncated log of every rule the inspector proposed, written after each round.

Decision V1 (docs/learning-design-decisions.md): every proposed rule, kept or rejected, retry
drafts included, with its full conditions (learned conditions' definitions and examples), its
advice, the inspector's reasoning, the judge's answers, the stored successes it fires on, the
checker's re-runs and the decision. Built from the round's saved files; it changes nothing.
"""

import json
from urllib.parse import quote

from skill_agent.storage import read_json


def _read(path, default=None):
    return read_json(path) if path.exists() else default


def _quote(text):
    return "\n".join("> " + line if line else ">" for line in str(text).strip().splitlines()) or "> (empty)"


def _documents(sources):
    return ", ".join(dict.fromkeys(source.split("|")[0] for source in sources)) or "none"


def _definitions(learner):
    """Every learned condition proposed so far, by name, for conditions that reuse one."""
    found = {}
    for item in learner.rounds().values():
        saved = _read(learner.round_dir(item["round"]) / "proposals.json", {"proposals": []})
        for proposal in saved["proposals"]:
            for condition in proposal["proposal"]["conditions"]:
                if "new_feature" in condition:
                    found.setdefault(condition["new_feature"]["name"], condition["new_feature"])
    return found


def _conditions(proposal, definitions):
    lines = []
    for condition in proposal["conditions"]:
        feature = condition.get("new_feature")
        if feature is None and str(condition.get("field", "")).startswith("lf_"):
            feature = definitions.get(condition["field"])
            prefix = f"`{condition['field']} {condition['op']} {condition['value']}` (a learned condition from " \
                     "an earlier rule)"
        elif feature is not None:
            prefix = f"`{feature['name']}` is true (a new learned condition)"
        else:
            lines.append(f"- `{condition['field']} {condition['op']} {json.dumps(condition['value'])}` "
                         "(a condition from the original skill)")
            continue
        lines.append(f"- {prefix}")
        if feature:
            lines.append(f"  - Definition: {feature['description']}")
            if feature.get("examples"):
                lines.append("  - True for: " + "; ".join(feature["examples"]))
            if feature.get("counterexamples"):
                lines.append("  - False for: " + "; ".join(feature["counterexamples"]))
    return lines


def _diagnosis_lines(directory, item):
    lines = []
    files = [item["diagnosis_file"]] if item.get("diagnosis_file") else [
        quote(source, safe="") + ".json" for source in item["sources"]]
    for name in files:
        outcome = _read(directory / "diagnoses" / name)
        if outcome is None:
            continue
        diagnosis = outcome.get("diagnosis") or {}
        lines.append(f"- Attribution: {diagnosis.get('attribution')}; failing turn: {diagnosis.get('failing_turn')}")
        lines.append(f"- Root cause: {diagnosis.get('root_cause')}")
        for evidence in diagnosis.get("evidence") or []:
            lines.append(f"  - Evidence: {evidence}")
        lines.append(f"- Fix checked against the answer key: "
                     f"{'passed' if outcome.get('fix_validated') else 'not passed'} "
                     f"({outcome.get('rescores', 0)} rescore call(s))")
        rationale = ((diagnosis.get("proposal") or {}).get("rationale"))
        if rationale:
            lines.append(f"- Rationale: {rationale}")
    return lines


def _rule_section(learner, item, number, proposal_item, entry, definitions, origins):
    directory = learner.round_dir(item["round"])
    label = f"{item['round'] + 1}.{number + 1}"
    proposal = proposal_item["proposal"]
    decision = (entry or {}).get("decision", "not decided yet")
    title = f"### Rule {label}: {decision.upper()}"
    if proposal_item.get("retry_of") is not None:
        title += f" (retry of rule {item['round'] + 1}.{proposal_item['retry_of'] + 1})"
    lines = [title, ""]
    insert = (entry or {}).get("insert") or {}
    level = (entry or {}).get("level")
    lines.append(f"- **Document(s):** {_documents(proposal_item['sources'])}")
    where = f"- **Where:** {proposal['kind']} at node {proposal['target_node_id']}"
    if level is not None:
        where += f" (the new rule is at level {level})"
    if insert.get("note"):
        where += f"; {insert['note']}"
    lines.append(where)
    lines.append("- **When (all must hold):**")
    lines += ["  " + line for line in _conditions(proposal, definitions)]
    if proposal.get("guidance_text"):
        lines += ["- **Advice added:**", "", _quote(proposal["guidance_text"]), ""]
    if proposal.get("remove_fragment_ids"):
        lines.append(f"- **Advice removed (fragments):** {', '.join(proposal['remove_fragment_ids'])}")
    if proposal.get("anchor_fragment_id"):
        lines.append(f"- **Shown after fragment:** {proposal['anchor_fragment_id']}")
    lines.append("- **Inspector:**")
    lines += ["  " + line for line in _diagnosis_lines(directory, proposal_item)]
    if insert.get("judged"):
        lines.append("- **Judge on the failing case:** " + ", ".join(
            f"`{name}` = {value}" for name, value in insert["judged"].items()))
    fired_on = (entry or {}).get("changed_documents")
    if fired_on is None:  # Classic mode: the stored successes that blocked it.
        fired_on = list(dict.fromkeys(source.split("|")[0] for context in insert.get("conflicts") or []
                                      for source in origins.get(context, [])))
        if insert.get("conflicts") and not fired_on:
            fired_on = [f"{len(insert['conflicts'])} stored situation(s) no longer on record"]
    lines.append(f"- **Stored successes it fires on:** {', '.join(fired_on) if fired_on else 'none'}")
    reruns = (entry or {}).get("reruns") or ((entry or {}).get("validation") or {}).get("passes")
    if reruns:
        fixed = set(((entry or {}).get("validation") or {}).get("fixed") or {})
        changed = set((entry or {}).get("changed_documents") or [])
        rerun_mode = (entry or {}).get("changed_successes", "reject") == "rerun"
        lines += ["- **Checker re-runs:**", "", "  | Document | Why re-run | Result |", "  |---|---|---|"]
        for task, runs in reruns.items():
            if task in fixed:
                why = "the failure this rule fixes"
            elif task in changed:
                why = "a stored success the rule fires on"
            elif rerun_mode:
                why = "a success whose advice changes (found by replay)"
            else:
                why = "an earlier success, protected (classic mode)"
            result = ", ".join("pass" if run else "fail" for run in runs) if isinstance(runs, list) else runs
            lines.append(f"  | {task} | {why} | {result} |")
        lines.append("")
    reason = (entry or {}).get("reason")
    lines.append(f"- **Decision:** {decision}" + (f": {reason}" if reason else ""))
    if decision == "accepted":
        lines.append(f"- **Tree after this round:** v{item['version_after']}")
        refreshed = ((entry or {}).get("refresh") or {}).get("records") or {}
        if refreshed:
            lines.append(f"- **Stored cases replaced by their passing re-run:** {', '.join(refreshed)}")
    return lines + [""]


def round_section(learner, item, definitions=None, origins=None):
    """Markdown for one finished round: its document(s), every proposed rule, failures without one."""
    directory = learner.round_dir(item["round"])
    definitions = definitions if definitions is not None else _definitions(learner)
    origins = origins if origins is not None else _read(learner.root / "cornerstones.json", {})
    passed = item["passed"]
    lines = [f"## Round {item['round'] + 1}: {', '.join(item['tasks'])} "
             f"(tree v{item['version_before']} -> v{item['version_after']})", "",
             "Worker: " + ", ".join(f"{source.split('|')[0]} {'passed' if ok else 'failed'}"
                                    for source, ok in passed.items()), ""]
    saved = _read(directory / "proposals.json", {"proposals": []})
    ledger = {entry["proposal_index"]: entry for entry in _read(directory / "ledger.json", [])}
    for number, proposal_item in enumerate(saved["proposals"]):
        lines += _rule_section(learner, item, number, proposal_item, ledger.get(number), definitions, origins)
    proposed = {source for p in saved["proposals"] for source in p["sources"]}
    unproposed = [source for source, ok in passed.items() if not ok and source not in proposed]
    for source in unproposed:
        outcome = _read(directory / "diagnoses" / (quote(source, safe="") + ".json"))
        diagnosis = (outcome or {}).get("diagnosis") or {}
        lines += [f"### {source.split('|')[0]}: failed, no rule proposed", "",
                  f"- Attribution: {diagnosis.get('attribution')}",
                  f"- Root cause: {diagnosis.get('root_cause')}",
                  f"- Why no rule: {(outcome or {}).get('ineligible_reason') or 'no eligible diagnosis'}", ""]
    if not saved["proposals"] and not unproposed:
        lines += ["No rule proposed: every attempt passed.", ""]
    return lines


def write_rules_log(learner, index=None):
    """Write ``rounds/rNNN/rules.md`` for round ``index`` (if given) and regenerate ``rules.md``."""
    rounds = sorted(learner.rounds().values(), key=lambda entry: entry["round"])
    definitions = _definitions(learner)
    origins = _read(learner.root / "cornerstones.json", {})
    sections = {item["round"]: round_section(learner, item, definitions, origins) for item in rounds}
    header = ["# Rules proposed during learning", "",
              "Every rule the inspector proposed, kept or rejected, in full. Written after each round; "
              "see docs/learning-design-decisions.md (V1).", ""]
    if index is not None and index in sections:
        (learner.round_dir(index) / "rules.md").write_text(
            "\n".join(header + sections[index]) + "\n", encoding="utf-8")
    text = header + [line for number in sorted(sections) for line in sections[number]]
    (learner.root / "rules.md").write_text("\n".join(text) + "\n", encoding="utf-8")
    return learner.root / "rules.md"
