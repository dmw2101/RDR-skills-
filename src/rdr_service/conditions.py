"""A snapshot-derived catalog and a deterministic condition-result evaluator."""

import json

from skill_agent.contracts import fingerprint


def condition_id(condition):
    return "c_" + fingerprint(condition)


def describe(condition):
    if isinstance(condition, str):
        return condition
    if isinstance(condition, dict):
        if isinstance(condition.get("description"), str):
            return condition["description"]
        field, op, value = (condition.get(k) for k in ("field", "op", "value"))
        if field in ("needed_actions", "operation", "task_type") and op in ("contains", "eq"):
            return "Guidance for '" + str(value).replace("_", " ") + "' is relevant to the current guidance need."
        return "Evaluate the literal predicate against observed facts: " + json.dumps(condition, sort_keys=True)
    raise ValueError("condition needs a text or object representation")


def build_catalog(engine, artifact_id):
    entries = {}
    nodes = []
    for node in engine.nodes:
        ids = []
        for condition in node.conditions:
            cid = condition_id(condition)
            entry = entries.setdefault(cid, {"id": cid, "predicate": condition,
                                             "description": describe(condition), "node_ids": []})
            if node.id not in entry["node_ids"]:
                entry["node_ids"].append(node.id)
            ids.append(cid)
        nodes.append({"id": node.id, "conditions": ids, "children": list(node.children),
                      "stopping": node.stopping})
    result = {"version": 1, "artifact_id": artifact_id, "tree_fingerprint": fingerprint(engine.to_dict()),
              "conditions": list(entries.values()), "nodes": nodes}
    return {**result, "catalog_fingerprint": fingerprint(result)}


def validate_results(catalog, payload):
    if not isinstance(payload, dict) or set(payload) != {"catalog_fingerprint", "evaluations"}:
        raise ValueError("expected catalog fingerprint and evaluations")
    if payload["catalog_fingerprint"] != catalog["catalog_fingerprint"]:
        raise ValueError("condition catalog changed")
    known = {entry["id"] for entry in catalog["conditions"]}
    values = payload["evaluations"]
    if not isinstance(values, list):
        raise ValueError("evaluations must be a list")
    results = {}
    for value in values:
        if not isinstance(value, dict) or set(value) != {"condition_id", "result", "evidence"}:
            raise ValueError("invalid condition evaluation fields")
        cid, truth, evidence = (value[k] for k in ("condition_id", "result", "evidence"))
        if not isinstance(cid, str) or cid not in known or cid in results:
            raise ValueError("unknown or duplicate condition ID")
        if truth not in ("true", "false", "unknown") or not isinstance(evidence, str) or len(evidence) > 500:
            raise ValueError("invalid condition result or evidence")
        if truth in ("true", "unknown") and not evidence.strip():
            raise ValueError("true and unknown results need an evidence reference")
        results[cid] = truth
    if set(results) != known:
        raise ValueError("missing condition evaluations")
    return results


class UnknownCondition(ValueError):
    pass


class LookupConditionEvaluator:
    def __init__(self, results):
        self.results = results

    def evaluate(self, condition, case):
        cid = condition_id(condition)
        truth = self.results[cid]
        if truth == "unknown":
            raise UnknownCondition("unresolved condition: " + cid)
        return truth == "true"
