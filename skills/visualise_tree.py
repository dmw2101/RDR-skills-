"""Render an RDR tree.json as a self-contained, interactive HTML page.

Usage:
    python visualise_tree.py alfworld/compiled/tree.json
    python visualise_tree.py TREE [-o OUT.html] [--fragments FRAGMENTS.json]
                             [--schema SCHEMA.json] [--hide-unreachable]

The input is the engine's schema-2 MCRDR payload (``root_id`` plus a ``nodes``
table whose entries carry ``id``, ``conditions``, ``conclusion``, ``stopping``,
``evidence`` and ``children``). A child is evaluated only when its parent fires;
every fired node without a fired child reports its conclusion, and a stopping
child suppresses its parent's conclusion. Conditions and conclusions are drawn
as generic JSON; the skill-compiler forms get extra treatment:

* ``{"field", "op": "eq" | "in" | "contains", "value"}`` conditions are pretty-printed and
  used for a conservative static reachability check. Nodes whose path
  constraints contradict each other are greyed out (or hidden).
* ``{"fragments": [...]}`` conclusions are resolved against a
  fragment catalogue (``fragments.json`` beside the tree, by default).

Feature domains for the reachability check come from ``--schema`` or a
``manifest.json`` beside the tree. Without them the check still detects
direct contradictions; it just cannot tell when a field's values run out.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_tree(path: Path) -> tuple[dict[str, Any], dict[int, dict[str, Any]], list[str]]:
    payload = load_json(path)
    if isinstance(payload, list):  # bare node table
        payload = {"nodes": payload}
    if not isinstance(payload, dict) or not isinstance(payload.get("nodes"), list):
        raise ValueError(f"{path}: expected an object with a 'nodes' list")
    warnings: list[str] = []
    nodes: dict[int, dict[str, Any]] = {}
    for index, raw in enumerate(payload["nodes"]):
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: node #{index} is not an object")
        node_id = raw.get("id", index)
        if node_id in nodes:
            warnings.append(f"Duplicate node id {node_id}; later entry ignored.")
            continue
        nodes[node_id] = raw
    for node_id, raw in nodes.items():
        for child in raw.get("children") or []:
            if child not in nodes:
                warnings.append(f"Node #{node_id} lists missing child {child}.")
    root_id = payload.get("root_id", 0)
    if root_id not in nodes:
        raise ValueError(f"{path}: root_id {root_id!r} is not in the node table")
    meta = {key: value for key, value in payload.items() if key != "nodes"}
    meta["root_id"] = root_id
    return meta, nodes, warnings


def load_fragments(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.is_file():
        return {}
    payload = load_json(path)
    items = payload.get("fragments", payload) if isinstance(payload, dict) else payload
    if isinstance(items, dict):  # {id: text | {...}}
        return {
            key: value if isinstance(value, dict) else {"text": str(value)}
            for key, value in items.items()
        }
    return {
        item["id"]: item
        for item in items or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def load_domains(path: Path | None, skill_hint: str | None) -> dict[str, list[Any]]:
    """Return {field: allowed values} from a manifest or feature-schema file."""
    if path is None or not path.is_file():
        return {}
    payload = load_json(path)
    if not isinstance(payload, dict):
        return {}
    payload = payload.get("feature_schema", payload)
    payload = payload.get("features", payload)

    def is_flat(mapping: dict[str, Any]) -> bool:
        return bool(mapping) and all(
            isinstance(spec, dict) and isinstance(spec.get("values"), list)
            for spec in mapping.values()
        )

    if not is_flat(payload):  # multi-skill map such as skills/feature_schemas.json
        payload = payload.get(skill_hint) if skill_hint else None
        if not isinstance(payload, dict) or not is_flat(payload):
            return {}
    return {name: list(spec["values"]) for name, spec in payload.items()}


# --------------------------------------------------------------------------- #
# Condition / conclusion formatting
# --------------------------------------------------------------------------- #


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(", ", ": "))


SCALARS = (str, int, float, bool, type(None))


def is_predicate(condition: Any) -> bool:
    """True for the skill compiler's scalar and membership predicates."""
    if not (isinstance(condition, dict) and isinstance(condition.get("field"), str) and "value" in condition):
        return False
    value = condition["value"]
    if condition.get("op") in ("eq", "contains"):
        return isinstance(value, SCALARS)
    if condition.get("op") == "in":
        return isinstance(value, list) and all(isinstance(item, SCALARS) for item in value)
    return False


def format_condition(condition: Any) -> str:
    if is_predicate(condition):
        op, value = condition["op"], condition["value"]
        if op == "eq":
            return f"{condition['field']} = {value}"
        if op == "contains":
            return f"{condition['field']} contains {value}"
        return f"{condition['field']} ∈ {{{', '.join(map(str, value))}}}"
    if isinstance(condition, str):
        return condition
    return compact(condition)


def is_fragment_set(conclusion: Any) -> bool:
    return (
        isinstance(conclusion, dict)
        and set(conclusion) == {"fragments"}
        and isinstance(conclusion["fragments"], list)
    )


def snippet(text: str, limit: int = 60) -> str:
    line = " ".join(text.split())
    line = line.strip("|# ").replace(" | ", " · ")
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------- #
# Static reachability
# --------------------------------------------------------------------------- #


def show_values(name: str, values: frozenset, negate: bool = False, multiple: bool = False) -> str:
    shown = sorted(map(str, values))
    if multiple:
        return f"{name} {'does not contain' if negate else 'contains'} {', '.join(shown)}"
    if len(shown) == 1:
        return f"{name} {'≠' if negate else '='} {shown[0]}"
    return f"{name} {'∉' if negate else '∈'} {{{', '.join(shown)}}}"


@dataclass(frozen=True)
class FieldConstraint:
    """What one field must (requires) and must not (excludes) be on a path."""

    requires: tuple[tuple[int, frozenset], ...] = ()
    excludes: tuple[tuple[int, frozenset], ...] = ()
    multiple: bool = False

    def candidates(self, domain: list[Any] | None) -> set | None:
        base = None if domain is None else set(domain)
        for _, values in self.requires:
            base = set(values) if base is None else base & values
        if base is None:
            return None  # open domain, no positive requirement: always satisfiable
        for _, values in self.excludes:
            base -= values
        return base

    def describe(self, name: str) -> str:
        parts = [f"#{n} requires {show_values(name, v, multiple=self.multiple)}" for n, v in self.requires]
        parts += [f"#{n} requires {show_values(name, v, negate=True, multiple=self.multiple)}" for n, v in self.excludes]
        return "; ".join(parts) + (", which no list satisfies" if self.multiple else ", which no value satisfies")

    def impossible(self, domain: list[Any] | None) -> bool:
        if self.multiple:
            required = set().union(*(values for _, values in self.requires))
            excluded = set().union(*(values for _, values in self.excludes))
            # All positive memberships may coexist. An empty list is valid too.
            return bool(required & excluded) or (domain is not None and not required <= set(domain))
        remaining = self.candidates(domain)
        return remaining is not None and not remaining


Constraints = dict[str, FieldConstraint]


def apply(constraints: Constraints, condition: dict[str, Any], node_id: int, negate: bool) -> Constraints:
    name = condition["field"]
    values = frozenset([condition["value"]] if condition["op"] in ("eq", "contains") else condition["value"])
    current = constraints.get(name, FieldConstraint(multiple=condition["op"] == "contains"))
    if negate:
        updated = FieldConstraint(current.requires, current.excludes + ((node_id, values),), current.multiple)
    else:
        updated = FieldConstraint(current.requires + ((node_id, values),), current.excludes, current.multiple)
    return {**constraints, name: updated}


def conflict(constraints: Constraints, domains: dict[str, list[Any]]) -> str | None:
    """Describe the first unsatisfiable field, or None if all are satisfiable."""
    for name, constraint in constraints.items():
        if constraint.impossible(domains.get(name)):
            return constraint.describe(name)
    return None


@dataclass
class Analysis:
    status: str = "unvisited"  # live | dead | unvisited
    dead_reason: str | None = None
    can_match: bool = True
    can_fail: bool = True  # Only used to flag rules that always fire when reached.
    notes: list[str] = field(default_factory=list)
    depth: int = 0
    path: list[tuple[int, bool]] = field(default_factory=list)


def analyse(nodes: dict[int, dict[str, Any]], root_id: int, domains: dict[str, list[Any]]) -> tuple[dict[int, Analysis], list[str]]:
    result = {node_id: Analysis() for node_id in nodes}
    warnings: list[str] = []
    seen: set[int] = set()
    # (node, constraints on arrival, dead reason inherited from ancestors, depth, path).
    # Every edge requires the parent to fire, so constraints only accumulate.
    stack: list[tuple[int, Constraints, str | None, int, list[tuple[int, bool]]]] = [
        (root_id, {}, None, 0, [])
    ]
    while stack:
        node_id, constraints, inherited, depth, path = stack.pop()
        if node_id in seen:
            warnings.append(f"Node #{node_id} is reachable from more than one parent (or a cycle); drawn once.")
            continue
        seen.add(node_id)
        info = result[node_id]
        info.depth, info.path = depth, path
        info.status = "dead" if inherited else "live"
        info.dead_reason = inherited

        raw = nodes[node_id]
        conditions = raw.get("conditions") or []
        known = [c for c in conditions if is_predicate(c)]
        opaque = len(known) != len(conditions)

        on_true = constraints
        for condition in known:
            on_true = apply(on_true, condition, node_id, negate=False)
        true_conflict = conflict(on_true, domains)

        false_conflict: str | None = None
        if not conditions:
            false_conflict = "an empty condition list is always true"
        elif len(known) == 1 and not opaque:
            false_conflict = conflict(apply(constraints, known[0], node_id, negate=True), domains)
        elif not opaque:
            # ¬(a ∧ b) is satisfiable if any single negation is.
            reasons = [conflict(apply(constraints, c, node_id, True), domains) for c in known]
            if all(reasons):
                false_conflict = reasons[0]

        info.can_match = true_conflict is None
        info.can_fail = false_conflict is None
        if not inherited:
            if not info.can_match:
                info.notes.append(f"Can never be true here: {true_conflict}.")
            elif not info.can_fail and conditions:
                info.notes.append(f"Always true when reached: {false_conflict}.")

        child_reason = inherited
        if child_reason is None and true_conflict is not None:
            child_reason = f"#{node_id} can never fire on this path: {true_conflict}"
        for child in reversed(raw.get("children") or []):
            if child in nodes:
                stack.append((child, on_true, child_reason, depth + 1, path + [(node_id, True)]))

    orphans = sorted(set(nodes) - seen)
    if orphans:
        warnings.append(f"Nodes not connected to the root: {', '.join(map(str, orphans))}.")
    return result, warnings


# --------------------------------------------------------------------------- #
# Payload for the page
# --------------------------------------------------------------------------- #


def build_payload(
    tree_path: Path,
    meta: dict[str, Any],
    nodes: dict[int, dict[str, Any]],
    analysis: dict[int, Analysis],
    fragments: dict[str, dict[str, Any]],
    domains: dict[str, list[Any]],
    warnings: list[str],
    hide_unreachable: bool,
    title: str,
) -> dict[str, Any]:
    out_nodes = []
    for node_id, raw in nodes.items():
        info = analysis[node_id]
        conditions = raw.get("conditions") or []
        conclusion = raw.get("conclusion")
        stopping = raw.get("stopping") is True
        if stopping:
            conclusion_view = {"type": "stop", "summary": "STOP · suppresses the parent conclusion"}
        elif is_fragment_set(conclusion):
            ops = []
            for frag_id in conclusion["fragments"]:
                frag = fragments.get(frag_id, {})
                text = frag.get("text", "")
                ops.append({
                    "id": frag_id, "text": text,
                    "short": snippet(text) if text else frag_id,
                    "role": frag.get("role"), "lines": [frag.get("start"), frag.get("end")],
                    "missing": bool(fragments) and frag_id not in fragments,
                })
            summary = " · ".join(op["short"] for op in ops[:1])
            if len(ops) > 1:
                summary += f"  (+{len(ops) - 1} more)"
            conclusion_view = {"type": "fragments", "ops": ops, "summary": summary or "no fragments"}
        else:
            text = compact(conclusion)
            conclusion_view = {"type": "json", "json": json.dumps(conclusion, indent=2, ensure_ascii=False),
                               "summary": snippet(text, 48)}
        out_nodes.append({
            "id": node_id,
            "conditions": [format_condition(c) for c in conditions],
            "conditions_json": json.dumps(conditions, indent=2, ensure_ascii=False),
            "conclusion": conclusion_view,
            "evidence": raw.get("evidence") or [],
            "children": [child for child in raw.get("children") or [] if child in nodes],
            "stopping": stopping,
            "status": info.status,
            "dead_reason": info.dead_reason,
            "notes": info.notes,
            "can_match": info.can_match,
            "can_fail": info.can_fail,
            "depth": info.depth,
            "path": [{"id": n, "matched": m} for n, m in info.path],
        })

    live = [n for n in out_nodes if n["status"] == "live"]
    by_id = {n["id"]: n for n in out_nodes}
    stats = {
        "nodes": len(out_nodes),
        "live": len(live),
        "dead": sum(n["status"] == "dead" for n in out_nodes),
        "orphan": sum(n["status"] == "unvisited" for n in out_nodes),
        "depth": max((n["depth"] for n in out_nodes if n["status"] != "unvisited"), default=0),
        "leaves": sum(not n["children"] for n in out_nodes),
        "stops": sum(n["stopping"] for n in out_nodes),
        # Nodes that can report a conclusion: reachable, able to fire, not a
        # stop, and without a child that always fires whenever it does.
        "terminals": sum(
            n["can_match"] and not n["stopping"] and not any(
                not by_id[c]["can_fail"] and by_id[c]["status"] == "live" for c in n["children"]
            )
            for n in live
        ),
    }
    return {
        "title": title,
        "source": str(tree_path),
        "meta": meta,
        "stats": stats,
        "domains": domains,
        "fragments_loaded": bool(fragments),
        "warnings": warnings,
        "hide_unreachable": hide_unreachable,
        "nodes": out_nodes,
    }


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root {
  --bg: #f7f7f5; --panel: #ffffff; --ink: #1d1d1b; --muted: #6b6b66; --line: #d9d8d3;
  --node: #ffffff; --node-line: #b9b8b1; --accent: #2f5fd0; --accent-soft: #e6ecfb;
  --true: #1f8a4c; --false: #c2462c; --dead-bg: #efefec; --dead-ink: #a5a49e;
  --warn-bg: #fff4dc; --warn-ink: #7a5300; --root: #20201e; --root-ink: #ffffff;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #161615; --panel: #1f1f1d; --ink: #ecebe6; --muted: #9a9993; --line: #34332f;
    --node: #23231f; --node-line: #4a4943; --accent: #7ea2ff; --accent-soft: #24304f;
    --true: #5cc98a; --false: #f0826a; --dead-bg: #1b1b19; --dead-ink: #5c5b55;
    --warn-bg: #3a2e12; --warn-ink: #f3cf7a; --root: #ecebe6; --root-ink: #161615;
  }
}
:root[data-theme="dark"] {
  --bg: #161615; --panel: #1f1f1d; --ink: #ecebe6; --muted: #9a9993; --line: #34332f;
  --node: #23231f; --node-line: #4a4943; --accent: #7ea2ff; --accent-soft: #24304f;
  --true: #5cc98a; --false: #f0826a; --dead-bg: #1b1b19; --dead-ink: #5c5b55;
  --warn-bg: #3a2e12; --warn-ink: #f3cf7a; --root: #ecebe6; --root-ink: #161615;
}
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; }
body { background: var(--bg); color: var(--ink); font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif;
       display: flex; flex-direction: column; }
header { padding: 14px 16px 10px; border-bottom: 1px solid var(--line); background: var(--panel); }
h1 { font-size: 17px; margin: 0 0 2px; }
.sub { color: var(--muted); font-size: 12px; word-break: break-all; }
.stats { display: flex; flex-wrap: wrap; gap: 6px 18px; margin-top: 10px; font-size: 13px; }
.stats b { font-variant-numeric: tabular-nums; }
.toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 10px; }
button, label.toggle { font: inherit; font-size: 13px; color: var(--ink); background: var(--bg);
  border: 1px solid var(--line); border-radius: 6px; padding: 4px 10px; cursor: pointer; }
button:hover { border-color: var(--accent); }
label.toggle { display: inline-flex; gap: 6px; align-items: center; }
.legend { display: flex; gap: 14px; flex-wrap: wrap; font-size: 12px; color: var(--muted); margin-left: auto; }
.legend i { display: inline-block; width: 18px; height: 0; border-top: 2px solid; vertical-align: middle; margin-right: 4px; }
.warnings { margin: 10px 0 0; padding: 8px 12px; background: var(--warn-bg); color: var(--warn-ink);
  border-radius: 6px; font-size: 13px; }
.warnings ul { margin: 0; padding-left: 18px; }
main { flex: 1; display: flex; min-height: 0; }
#canvas { flex: 1; overflow: auto; min-width: 0; }
#detail { width: 380px; flex: none; border-left: 1px solid var(--line); background: var(--panel);
  overflow: auto; padding: 14px 16px 24px; }
@media (max-width: 820px) {
  main { flex-direction: column; }
  #canvas { min-height: 60vh; }
  #detail { width: auto; border-left: 0; border-top: 1px solid var(--line); }
  .legend { margin-left: 0; }
}
svg { display: block; }
.edge { fill: none; stroke-width: 1.6; }
.edge.t { stroke: var(--true); } .edge.s { stroke: var(--false); }
.edge.dead { stroke: var(--dead-ink); stroke-dasharray: 4 4; }
.node rect.box { fill: var(--node); stroke: var(--node-line); stroke-width: 1; }
.node.root rect.box { fill: var(--root); stroke: var(--root); }
.node.root text { fill: var(--root-ink); }
.node.dead rect.box { fill: var(--dead-bg); stroke-dasharray: 3 3; }
.node.dead text { fill: var(--dead-ink); }
.node.flag rect.box { stroke: var(--warn-ink); stroke-width: 1.5; }
.node.stop rect.box { stroke: var(--false); stroke-width: 1.5; }
.node.stop .concl { fill: var(--false); }
.node.selected rect.box { stroke: var(--accent); stroke-width: 2.5; }
.node.onpath rect.box { stroke: var(--accent); stroke-width: 1.8; }
.node { cursor: pointer; }
.node text { fill: var(--ink); }
.node .nid { font: 600 11px var(--mono); fill: var(--muted); }
.node.root .nid { fill: var(--root-ink); opacity: .7; }
.node .cond { font: 600 12.5px var(--mono); }
.node .concl { font-size: 11.5px; fill: var(--muted); }
.node.root .concl { fill: var(--root-ink); opacity: .8; }
.node .fold { fill: var(--panel); stroke: var(--node-line); }
.node .foldtxt { font: 700 12px var(--mono); fill: var(--muted); text-anchor: middle; dominant-baseline: central; pointer-events: none; }
#detail h2 { font-size: 15px; margin: 0 0 4px; }
#detail h3 { font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); margin: 18px 0 6px; }
#detail .empty { color: var(--muted); }
.pill { display: inline-block; font-size: 11px; padding: 1px 8px; border-radius: 99px; border: 1px solid var(--line); margin-right: 4px; }
.pill.live { color: var(--true); border-color: var(--true); }
.pill.dead { color: var(--dead-ink); }
.pill.flag { color: var(--warn-ink); border-color: var(--warn-ink); }
code, pre { font-family: var(--mono); font-size: 12px; }
pre { background: var(--bg); border: 1px solid var(--line); border-radius: 6px; padding: 8px; overflow: auto;
  white-space: pre-wrap; word-break: break-word; margin: 6px 0 0; }
.frag { border: 1px solid var(--line); border-radius: 6px; padding: 8px 10px; margin: 6px 0; }
.frag .head { display: flex; gap: 8px; align-items: baseline; font-size: 12px; }
.frag .fid { font-family: var(--mono); color: var(--muted); }
.frag pre { margin-top: 6px; }
.note { background: var(--warn-bg); color: var(--warn-ink); border-radius: 6px; padding: 6px 10px; font-size: 13px; margin: 6px 0; }
.note.dead { background: var(--dead-bg); color: var(--muted); }
ol.path { margin: 0; padding-left: 20px; font-size: 13px; }
ol.path a { color: var(--accent); cursor: pointer; text-decoration: none; }
ol.path .m { font-weight: 700; } ol.path .m.t { color: var(--true); } ol.path .m.f { color: var(--false); }
dl.meta { display: grid; grid-template-columns: max-content 1fr; gap: 2px 12px; font-size: 13px; margin: 0; }
dl.meta dt { color: var(--muted); } dl.meta dd { margin: 0; word-break: break-all; }
</style>
</head>
<body>
<header>
  <h1 id="title"></h1>
  <div class="sub" id="source"></div>
  <div class="stats" id="stats"></div>
  <div class="toolbar">
    <label class="toggle"><input type="checkbox" id="hideDead"> Hide unreachable</label>
    <button id="expand">Expand all</button>
    <button id="collapse">Collapse dead</button>
    <button id="zoomOut" aria-label="Zoom out">−</button>
    <button id="zoomReset">100%</button>
    <button id="zoomIn" aria-label="Zoom in">+</button>
    <button id="fit">Fit</button>
    <div class="legend">
      <span><i style="border-color:var(--true)"></i>child: tested when the parent fires</span>
      <span><i style="border-color:var(--false)"></i>stopping rule</span>
      <span><i style="border-color:var(--dead-ink);border-top-style:dashed"></i>unreachable</span>
    </div>
  </div>
  <div class="warnings" id="warnings" hidden></div>
</header>
<main>
  <div id="canvas"><svg id="svg" xmlns="http://www.w3.org/2000/svg"></svg></div>
  <aside id="detail"></aside>
</main>
<script id="data" type="application/json">__DATA__</script>
<script>
(() => {
  const DATA = JSON.parse(document.getElementById("data").textContent);
  const byId = new Map(DATA.nodes.map(n => [n.id, n]));
  const rootId = DATA.meta.root_id;
  const NW = 250, NH = 58, DNH = 30, COLGAP = 70, ROWGAP = 14, PAD = 24;
  const height = id => byId.get(id).status === "dead" ? DNH : NH;
  const collapsed = new Set();
  let hideDead = DATA.hide_unreachable, selected = rootId, zoom = 1;

  const $ = id => document.getElementById(id);
  const el = (tag, attrs = {}, text) => {
    const e = tag === "svg" || ["g","rect","text","path","circle","title"].includes(tag)
      ? document.createElementNS("http://www.w3.org/2000/svg", tag) : document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    if (text !== undefined) e.textContent = text;
    return e;
  };
  const clip = (s, n) => s.length > n ? s.slice(0, n - 1) + "…" : s;

  // ---- header -----------------------------------------------------------
  $("title").textContent = DATA.title;
  $("source").textContent = DATA.source;
  const s = DATA.stats;
  const statItems = [["Nodes", s.nodes], ["Reachable", s.live], ["Unreachable", s.dead],
    ["Possible terminals", s.terminals], ["Stopping rules", s.stops], ["Leaves", s.leaves], ["Max depth", s.depth]];
  if (s.orphan) statItems.push(["Disconnected", s.orphan]);
  for (const [k, v] of statItems) {
    const sp = el("span"); sp.append(k + ": "); sp.append(el("b", {}, String(v))); $("stats").append(sp);
  }
  if (DATA.warnings.length) {
    const w = $("warnings"); w.hidden = false;
    const ul = el("ul"); DATA.warnings.forEach(t => ul.append(el("li", {}, t))); w.append(ul);
  }
  $("hideDead").checked = hideDead;
  $("hideDead").onchange = e => { hideDead = e.target.checked; render(); };
  $("expand").onclick = () => { collapsed.clear(); render(); };
  $("collapse").onclick = () => {
    // Fold every unreachable node so each dead subtree shows as one grey box.
    DATA.nodes.forEach(n => { if (n.status === "dead") collapsed.add(n.id); });
    render();
  };
  const setZoom = z => { zoom = Math.min(2, Math.max(0.3, z)); $("zoomReset").textContent = Math.round(zoom * 100) + "%"; render(); };
  $("zoomIn").onclick = () => setZoom(zoom * 1.2);
  $("zoomOut").onclick = () => setZoom(zoom / 1.2);
  $("zoomReset").onclick = () => setZoom(1);
  $("fit").onclick = () => { fit(0.25); centreRoot(); };
  function fit(min) {
    const c = $("canvas"), vb = $("svg").viewBox.baseVal;
    setZoom(Math.max(min, Math.min(1, c.clientWidth / vb.width, c.clientHeight / vb.height)));
  }
  function centreRoot() {
    const c = $("canvas"), p = layout().get(rootId);
    c.scrollLeft = 0;
    c.scrollTop = Math.max(0, (p.y + PAD) * zoom - c.clientHeight / 2);
  }

  // ---- layout -----------------------------------------------------------
  // Children in evaluation (insertion) order, top to bottom.
  function kids(n, all) {
    const out = [];
    for (const c of n.children) {
      if (!byId.has(c)) continue;
      if (!all && hideDead && byId.get(c).status === "dead") continue;
      out.push(c);
    }
    return out;
  }
  function layout() {
    const pos = new Map(); let row = 0; const seen = new Set();
    const place = (id, depth) => {
      if (seen.has(id)) return null; seen.add(id);
      const n = byId.get(id);
      const ch = collapsed.has(id) ? [] : kids(n, false);
      const ys = ch.map(c => place(c, depth + 1)).filter(y => y !== null);
      let y;  // vertical centre
      if (!ys.length) { y = row + height(id) / 2; row += height(id) + ROWGAP; }
      else y = (ys[0] + ys[ys.length - 1]) / 2;
      pos.set(id, { x: depth * (NW + COLGAP), y, kids: ch });
      return y;
    };
    place(rootId, 0);
    return pos;
  }

  // ---- render -----------------------------------------------------------
  function render() {
    const pos = layout();
    const svg = $("svg"); svg.replaceChildren();
    let maxX = 0, maxY = 0;
    pos.forEach((p, id) => { maxX = Math.max(maxX, p.x + NW); maxY = Math.max(maxY, p.y + height(id) / 2); });
    const W = maxX + PAD * 2 + 20, H = maxY + PAD * 2;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("width", W * zoom); svg.setAttribute("height", H * zoom);
    const root = el("g", { transform: `translate(${PAD},${PAD})` }); svg.append(root);
    const edges = el("g"), nodesG = el("g"); root.append(edges, nodesG);
    const onPath = new Set((byId.get(selected)?.path || []).map(p => p.id));

    pos.forEach((p, id) => {
      const n = byId.get(id);
      for (const c of p.kids) {
        const q = pos.get(c); if (!q) continue;
        const child = byId.get(c), dead = child.status === "dead";
        const cls = (child.stopping ? "s" : "t") + (dead ? " dead" : "");
        const x1 = p.x + NW, y1 = p.y, x2 = q.x, y2 = q.y, mx = (x1 + x2) / 2;
        edges.append(el("path", { class: "edge " + cls, d: `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}` }));
      }
    });

    pos.forEach((p, id) => {
      const n = byId.get(id);
      const flagged = n.status === "live" && n.notes.length;
      const cls = ["node", id === rootId ? "root" : "", n.status === "dead" ? "dead" : "", n.stopping ? "stop" : "",
        flagged ? "flag" : "", id === selected ? "selected" : "", onPath.has(id) ? "onpath" : ""].join(" ");
      const h = height(id), dead = n.status === "dead";
      const g = el("g", { class: cls, transform: `translate(${p.x},${p.y - h / 2})`, tabindex: 0 });
      g.append(el("title", {}, n.dead_reason || n.notes.join("\n") || `Node #${id}`));
      g.append(el("rect", { class: "box", width: NW, height: h, rx: 8 }));
      const cond = n.conditions.length ? n.conditions.join(" ∧ ") : (id === rootId ? "root · always true" : "always true");
      if (dead) {  // compact: one line, details in the side panel
        g.append(el("text", { class: "nid", x: 10, y: 19 }, "#" + id));
        g.append(el("text", { class: "cond", x: 44, y: 19 }, clip(cond, 27)));
      } else {
        g.append(el("text", { class: "nid", x: 10, y: 17 }, "#" + id + (flagged ? "  ⚠ " + (n.can_match ? "always true" : "never true") : "")));
        g.append(el("text", { class: "cond", x: 10, y: 34 }, clip(cond, 32)));
        g.append(el("text", { class: "concl", x: 10, y: 50 }, clip(n.conclusion.summary, 40)));
      }
      const all = kids(n, false);
      if (all.length) {
        const fx = NW, fy = h / 2;
        const fold = el("g");
        fold.append(el("circle", { class: "fold", cx: fx, cy: fy, r: 8 }));
        fold.append(el("text", { class: "foldtxt", x: fx, y: fy }, collapsed.has(id) ? "+" : "−"));
        fold.addEventListener("click", e => { e.stopPropagation(); collapsed.has(id) ? collapsed.delete(id) : collapsed.add(id); render(); });
        g.append(fold);
      }
      g.addEventListener("click", () => select(id));
      g.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(id); } });
      nodesG.append(g);
    });
  }

  // ---- detail panel -----------------------------------------------------
  function select(id) {
    selected = id;
    // Make sure the selected node is visible.
    for (const p of byId.get(id).path) collapsed.delete(p.id);
    if (hideDead && byId.get(id).status === "dead") { hideDead = false; $("hideDead").checked = false; }
    render(); showDetail(id);
  }
  function showDetail(id) {
    const n = byId.get(id), d = $("detail"); d.replaceChildren();
    d.append(el("h2", {}, `Node #${id}` + (id === rootId ? " (root default)" : n.stopping ? " (stopping rule)" : "")));
    const pills = el("div");
    pills.append(el("span", { class: "pill " + n.status }, n.status === "live" ? "reachable" : n.status === "dead" ? "unreachable" : "disconnected"));
    if (n.status === "live" && !n.can_match) pills.append(el("span", { class: "pill flag" }, "never true"));
    if (n.status === "live" && n.can_match && !n.can_fail && n.conditions.length) pills.append(el("span", { class: "pill flag" }, "always true"));
    d.append(pills);
    if (n.dead_reason) d.append(el("div", { class: "note dead" }, n.dead_reason));
    n.notes.forEach(t => d.append(el("div", { class: "note" }, t)));

    d.append(el("h3", {}, "Conditions (all must hold)"));
    if (!n.conditions.length) d.append(el("div", { class: "empty" }, "None: always true."));
    else { const ul = el("ul"); n.conditions.forEach(c => { const li = el("li"); li.append(el("code", {}, c)); ul.append(li); }); d.append(ul); }

    d.append(el("h3", {}, "Conclusion"));
    if (id === rootId) d.append(el("div", { class: "empty" }, "Reported only when no child of the root fires."));
    if (n.conclusion.type === "stop") {
      d.append(el("div", { class: "note" }, "Stopping rule: when it fires, its parent's conclusion is not reported and nothing replaces it."));
    } else if (n.conclusion.type === "fragments") {
      if (!n.conclusion.ops.length) d.append(el("div", { class: "empty" }, "No fragments."));
      for (const op of n.conclusion.ops) {
        const box = el("div", { class: "frag" }), head = el("div", { class: "head" });
        head.append(el("span", { class: "fid" }, op.id));
        if (op.lines && op.lines[0] != null) head.append(el("span", { class: "fid" }, `lines ${op.lines[0]}–${op.lines[1]}`));
        box.append(head);
        if (op.text) box.append(el("pre", {}, op.text.replace(/\s+$/, "")));
        else if (op.missing) box.append(el("div", { class: "empty" }, "Not found in the fragment catalogue."));
        d.append(box);
      }
      if (!DATA.fragments_loaded) d.append(el("div", { class: "empty" }, "No fragment catalogue loaded. Pass --fragments to show the text."));
    } else d.append(el("pre", {}, n.conclusion.json));

    d.append(el("h3", {}, "Route from root"));
    if (!n.path.length) d.append(el("div", { class: "empty" }, "This is the root."));
    else {
      const ol = el("ol", { class: "path" });
      for (const step of n.path) {
        const li = el("li"), a = el("a", {}, "#" + step.id);
        a.onclick = () => select(step.id);
        const sn = byId.get(step.id);
        li.append(a, " ", el("code", {}, sn.conditions.join(" ∧ ") || "always"), " → ",
          el("span", { class: "m t" }, "fires"));
        ol.append(li);
      }
      const last = el("li"); last.append(el("b", {}, "#" + id)); ol.append(last);
      d.append(ol);
    }

    d.append(el("h3", {}, `Children (${n.children.length})`));
    if (!n.children.length) d.append(el("div", { class: "empty" }, "None: when this node fires it reports its conclusion."));
    const ch = el("ul");
    for (const c of n.children) {
      const li = el("li"), a = el("a", { href: "#" }, "#" + c);
      a.onclick = e => { e.preventDefault(); select(c); }; li.append(a);
      if (byId.get(c).stopping) li.append(el("span", { class: "empty" }, "  (stopping rule)"));
      if (byId.get(c).status === "dead") li.append(el("span", { class: "empty" }, "  (unreachable)"));
      ch.append(li);
    }
    d.append(ch);

    d.append(el("h3", {}, `Evidence (${n.evidence.length})`));
    if (n.evidence.length) d.append(el("pre", {}, JSON.stringify(n.evidence, null, 2)));
    else d.append(el("div", { class: "empty" }, "No stored cases."));

    d.append(el("h3", {}, "Raw conditions"));
    d.append(el("pre", {}, n.conditions_json));

    d.append(el("h3", {}, "Tree"));
    const dl = el("dl", { class: "meta" });
    for (const [k, v] of Object.entries(DATA.meta)) dl.append(el("dt", {}, k), el("dd", {}, typeof v === "object" ? JSON.stringify(v) : String(v)));
    for (const [k, v] of Object.entries(DATA.domains)) dl.append(el("dt", {}, "domain: " + k), el("dd", {}, v.join(", ")));
    d.append(dl);
  }

  render(); showDetail(rootId);
  fit(0.6); centreRoot();
})();
</script>
</body>
</html>
"""


def render_html(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False)
    # Keep the JSON from closing the <script> element early.
    data = data.replace("</", "<\\/").replace("<!--", "<\\!--")
    return PAGE.replace("__TITLE__", html.escape(payload["title"])).replace("__DATA__", data)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Visualise an MCRDR tree.json as an interactive HTML page.")
    parser.add_argument("tree", type=Path, help="path to tree.json")
    parser.add_argument("-o", "--output", type=Path,
                        help="output HTML file (default: <tree stem>_visualisation.html beside the tree)")
    parser.add_argument("--fragments", type=Path,
                        help="fragment catalogue used to resolve fragment ids (default: fragments.json beside the tree)")
    parser.add_argument("--schema", type=Path,
                        help="feature schema or manifest giving field domains (default: manifest.json beside the tree)")
    parser.add_argument("--skill", help="key to pick from a multi-skill schema file (default: the tree's skill folder name)")
    parser.add_argument("--hide-unreachable", action="store_true",
                        help="start with unreachable branches hidden (can be toggled in the page)")
    parser.add_argument("--title", help="page title (default: derived from the tree path)")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # Windows consoles may not be UTF-8
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")

    tree_path: Path = args.tree.resolve()
    folder = tree_path.parent
    skill = args.skill or (folder.parent.name if folder.name == "compiled" else folder.name)

    fragments_path = args.fragments or folder / "fragments.json"
    schema_path = args.schema or folder / "manifest.json"
    for given in (args.fragments, args.schema):
        if given and not given.is_file():
            parser.error(f"file not found: {given}")
    try:
        meta, nodes, warnings = load_tree(tree_path)
        fragments = load_fragments(fragments_path)
        domains = load_domains(schema_path, skill)
    except (OSError, ValueError) as error:  # JSONDecodeError is a ValueError
        parser.error(str(error))
    if args.schema and not domains:
        warnings.append(f"No feature domains found in {args.schema} (skill key '{skill}').")

    analysis, analysis_warnings = analyse(nodes, meta["root_id"], domains)
    title = args.title or f"{skill} RDR tree"
    payload = build_payload(tree_path, meta, nodes, analysis, fragments, domains,
                            warnings + analysis_warnings, args.hide_unreachable, title)

    output = args.output or folder / f"{tree_path.stem}_visualisation.html"
    output.write_text(render_html(payload), encoding="utf-8")

    stats = payload["stats"]
    print(f"Wrote {output}")
    print(f"  nodes={stats['nodes']} reachable={stats['live']} unreachable={stats['dead']} "
          f"terminals={stats['terminals']} stops={stats['stops']} max_depth={stats['depth']}")
    print(f"  fragments: {'resolved from ' + str(fragments_path) if fragments else 'not loaded (ids only)'}")
    print(f"  domains: {', '.join(domains) if domains else 'none (contradiction check only)'}")
    for warning in payload["warnings"]:
        print(f"  warning: {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
