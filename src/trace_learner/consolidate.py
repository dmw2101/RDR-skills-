"""Merge validated proposals that target the same node (Trace2Skill's consolidation step)."""

import json
from importlib.resources import files

from pydantic import ValidationError

from .proposals import Proposal


def group_key(proposal):
    return (proposal["kind"], proposal["target_node_id"])


async def consolidate(diagnoses, merge_model=None):
    """diagnoses: [{"source": episode key, "proposal": dict}] -> [{"proposal", "sources", "merged"}].

    Without a merge model every proposal stays separate; insertion order still puts
    frequently targeted nodes first, and the engine rejects proposals made redundant.
    """
    groups = {}
    for item in diagnoses:
        groups.setdefault(group_key(item["proposal"]), []).append(item)
    ordered = sorted(groups.values(), key=lambda group: (-len(group), group_key(group[0]["proposal"])))
    merged, usage = [], []
    for group in ordered:
        if merge_model is None or len(group) == 1:
            merged.extend({"proposal": item["proposal"], "sources": [item["source"]], "merged": False}
                          for item in group)
            continue
        results, response = await _merge_group(merge_model, group)
        usage.append(response.usage)
        merged.extend(results)
    return merged, usage


async def _merge_group(model, group):
    sources = [item["source"] for item in group]
    tool = {"type": "function", "function": {
        "name": "submit_merged",
        "description": "Submit the merged proposals.",
        "parameters": {"type": "object", "properties": {"merged": {"type": "array", "items": {
            "type": "object", "properties": {
                "proposal": {"type": "object", "description": json.dumps(Proposal.model_json_schema())},
                "sources": {"type": "array", "items": {"type": "string", "enum": sources}}},
            "required": ["proposal", "sources"], "additionalProperties": False}}},
            "required": ["merged"], "additionalProperties": False}}}
    payload = [{"source": item["source"], "proposal": item["proposal"],
                "root_cause": item.get("root_cause")} for item in group]
    response = await model.complete([
        {"role": "system", "content": files("trace_learner").joinpath("prompts/merge.md").read_text(encoding="utf-8")},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=1)},
    ], [tool])
    calls = response.message.get("tool_calls") or []
    try:
        if len(calls) != 1 or calls[0]["function"]["name"] != "submit_merged":
            raise ValueError("merge model did not call submit_merged once")
        items = json.loads(calls[0]["function"]["arguments"])["merged"]
        covered = [source for item in items for source in item["sources"]]
        if sorted(covered) != sorted(sources):
            raise ValueError("merged proposals must cover every source exactly once")
        results = []
        for item in items:
            proposal = Proposal.model_validate_json(json.dumps(item["proposal"]))
            if group_key(proposal.model_dump()) != group_key(group[0]["proposal"]):
                raise ValueError("merged proposals must keep kind and target node")
            results.append({"proposal": proposal.model_dump(), "sources": item["sources"], "merged": True})
        return results, response
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        # An unusable merge keeps the original proposals rather than inventing one.
        return ([{"proposal": item["proposal"], "sources": [item["source"]], "merged": False,
                  "merge_error": f"{type(exc).__name__}: {exc}"} for item in group], response)
