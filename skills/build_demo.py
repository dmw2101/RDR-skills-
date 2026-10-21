"""Build a self-contained MCRDR demo: pick a skill, set task facts, watch the tree fire.

Usage (from the repository root):
    python skills/build_demo.py --output demo

Every compiled skill bundle under skills/ is loaded with SkillBundle.load, which checks
all hashes, so the page shows exactly the trees that were built. The page reproduces
SkillRenderer in JavaScript (MCRDR traversal, required headings/table headers, source
order and separators); `--check` compares it with the Python renderer through Node.
Example tasks are turned into facts by the real case builder at build time.
The output folder holds index.html and trees/<skill>.html (detailed tree views).
"""

from __future__ import annotations

import argparse
import itertools
import json
import random
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rdr_engine.skill_cases import SkillConditionEvaluator, build_case  # noqa: E402
from rdr_engine.skill_compiler import FeatureSchema, SkillBundle, _table_rows  # noqa: E402
from rdr_engine.skill_renderer import SkillRenderer  # noqa: E402

LABELS = {
    "alfworld": ("ALFWorld", "original seed skill"),
    "pdf": ("PDF", "skill snapshot"),
    "alfworld_gpt55": ("ALFWorld", "SkillOpt-optimized"),
    "docvqa_gpt55": ("DocVQA", "SkillOpt-optimized"),
    "livemath_gpt55": ("LiveMath", "SkillOpt-optimized"),
    "officeqa_gpt55": ("OfficeQA", "SkillOpt-optimized"),
    "searchqa_gpt55": ("SearchQA", "SkillOpt-optimized"),
    "spreadsheetbench_gpt55": ("SpreadsheetBench", "SkillOpt-optimized"),
}
EXAMPLES = {
    "alfworld": ["Heat an apple and put it in a bowl", "Examine the book with the desklamp",
                 "Put two keychains in the drawer", "Clean a plate and put it in the cabinet"],
    "pdf": ["Extract the tables from this scanned invoice", "Rotate the first page by 90 degrees and encrypt the file",
            "Merge these two PDFs into one", "Fill out this PDF form"],
    "alfworld_gpt55": ["Put a clean pan on the stoveburner", "Clean the kettle and put it in a cabinet",
                       "Put a clean dishsponge on the shelf", "Heat a mug and put it on the coffeemachine"],
    "docvqa_gpt55": ["What is the total amount shown in the table?", "Who signed the form as the witness?",
                     "What is the page number of the index entry for Revenue?"],
    "livemath_gpt55": ["Which statement holds for every n? Option E: one of the remaining options is correct, "
                       "but a stronger result can be proven.",
                       "Does the result hold for mu < mu0 or mu <= mu0, if and only if the sign is positive?"],
    "officeqa_gpt55": ["What was the change in new cash raised between the March and June 1981 Treasury offerings?",
                       "Compute the standard deviation of monthly receipts from January to June 1990.",
                       "Convert the 1975 amount from millions of dollars to billions using the exchange rate."],
    "searchqa_gpt55": ["This director, seen here, made the film Vertigo",
                       "Brands such as Oscar Mayer and Ball Park make these",
                       "This lake is the largest in Florida (11 letters)"],
    "spreadsheetbench_gpt55": ["Fix the SUMIFS formula in column D so every month shows its total",
                               "Highlight blank cells in yellow and leave blank inputs empty",
                               "Fill the timetable for each cycle day from the template"],
}
ORDER = list(LABELS)


def skill_data(directory: Path) -> dict:
    manifest = json.loads((directory / "compiled" / "manifest.json").read_text(encoding="utf-8"))
    schema = FeatureSchema.model_validate(manifest["feature_schema"])
    engine, catalog = SkillBundle.load(directory, SkillConditionEvaluator(schema))
    rows = _table_rows(catalog.skill_input.lines)
    header_spans = set(rows.values())
    by_span = {(f.start, f.end): f.id for f in catalog.fragments}
    fragments = []
    for f in catalog.fragments:
        if f.start in rows:
            table = by_span.get(rows[f.start])
        elif (f.start, f.end) in header_spans:
            table = f.id
        else:
            table = None
        fragments.append({"id": f.id, "text": f.text, "start": f.start, "end": f.end, "order": f.order,
                          "role": f.role, "requires": list(f.requires), "table": table})
    nodes = [{"id": n.id, "conditions": list(n.conditions), "conclusion": n.conclusion,
              "stopping": n.stopping, "children": list(n.children)} for n in engine.nodes]
    examples = []
    for task in EXAMPLES.get(directory.name, []):
        examples.append({"label": task, "task": task, "case": build_case(directory.name, task, None, schema)})
    for path in sorted((directory / "cases").glob("*.json")) if (directory / "cases").exists() else []:
        case = json.loads(path.read_text(encoding="utf-8-sig"))
        SkillConditionEvaluator(schema).validate_case(case)
        examples.append({"label": path.stem.replace("_", " "), "task": None, "case": case})
    lines = catalog.skill_input.lines
    title = next((line.lstrip("# ").strip() for line in lines if line.startswith("#")), directory.name)
    name, variant = LABELS.get(directory.name, (directory.name, ""))
    return {
        "id": directory.name, "name": name, "variant": variant, "title": title,
        "features": {k: {"values": v.values, "multiple": v.multiple, "description": v.description}
                     for k, v in schema.features.items()},
        "nodes": nodes, "fragments": fragments, "examples": examples,
        "source_lines": len(lines), "source_chars": sum(len(line) for line in lines),
        "rules": sum(1 for n in nodes[1:] if n["conditions"]),
        "model_id": manifest.get("model_id"), "tree_view": f"trees/{directory.name}.html",
        "_engine": engine, "_catalog": catalog, "_schema": schema,
    }


def python_render(skill: dict, case: dict) -> str:
    engine = skill["_engine"]
    return SkillRenderer().render(engine.predict(case), engine, skill["_catalog"]).markdown


def sample_cases(skill: dict, count: int, seed: int = 7) -> list[dict]:
    randomizer = random.Random(seed)
    features = skill["features"]
    cases = [example["case"] for example in skill["examples"]]
    for _ in range(count):
        case = {}
        for name, spec in features.items():
            if spec["multiple"]:
                case[name] = [v for v in spec["values"] if randomizer.random() < 0.3]
            else:
                case[name] = randomizer.choice(spec["values"])
        cases.append(case)
    empty = {name: [] if spec["multiple"] else ("unknown" if "unknown" in spec["values"] else spec["values"][0])
             for name, spec in features.items()}
    return cases + [empty]


def check_with_node(skills: list[dict], core_js: str) -> None:
    """Compare the page's JavaScript renderer with SkillRenderer on many cases."""
    if shutil.which("node") is None:
        raise SystemExit("--check needs Node.js on PATH")
    payload = []
    for skill in skills:
        for case in sample_cases(skill, 60):
            payload.append({"skill": skill["id"], "case": case, "expected": python_render(skill, case)})
    data = {s["id"]: {k: v for k, v in s.items() if not k.startswith("_")} for s in skills}
    script = core_js + """
const data = %s; const checks = %s; let bad = 0;
for (const c of checks) {
  const got = renderSkill(data[c.skill], c.case).markdown;
  if (got !== c.expected) { bad++; if (bad < 4) console.log('MISMATCH', c.skill, JSON.stringify(c.case)); }
}
console.log(JSON.stringify({checked: checks.length, mismatches: bad}));
process.exit(bad ? 1 : 0);
""" % (json.dumps(data), json.dumps(payload))
    result = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, encoding="utf-8")
    print(result.stdout.strip() or result.stderr.strip())
    if result.returncode:
        raise SystemExit("JavaScript renderer differs from SkillRenderer")


CORE_JS = r"""
function matches(cond, c) {
  const v = c[cond.field];
  if (cond.op === 'eq') return v === cond.value;
  if (cond.op === 'in') return cond.value.includes(v);
  if (cond.op === 'contains') return Array.isArray(v) && v.includes(cond.value);
  throw new Error('unknown op ' + cond.op);
}
function traverse(nodes, c) {
  const fired = new Set(), tested = new Set(), terminals = [];
  const visit = (id) => {
    const n = nodes[id]; tested.add(id);
    if (!n.conditions.every((cond) => matches(cond, c))) return false;
    fired.add(id);
    let childFired = false;
    for (const child of n.children) if (visit(child)) childFired = true;
    if (!childFired && !n.stopping) terminals.push(id);
    return true;
  };
  visit(0);
  return { fired, tested, terminals };
}
function renderSkill(skill, c) {
  const byId = new Map(skill.fragments.map((f) => [f.id, f]));
  const { fired, tested, terminals } = traverse(skill.nodes, c);
  const active = new Set();
  for (const id of terminals) for (const f of skill.nodes[id].conclusion.fragments) active.add(f);
  const pending = [...active];
  while (pending.length) {
    for (const r of byId.get(pending.pop()).requires) if (!active.has(r)) { active.add(r); pending.push(r); }
  }
  const selected = [...active].map((id) => byId.get(id)).sort((a, b) =>
    a.order - b.order || a.end - b.end || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  const instructional = new Set(selected.filter((f) => f.role === 'instruction').map((f) => f.id));
  const tableOf = (f) => (f.table && active.has(f.table) ? f.table : null);
  let out = '', prev = null;
  for (const f of selected) {
    if (prev) {
      const same = tableOf(prev) !== null && tableOf(prev) === tableOf(f);
      out += same || f.start === prev.end + 1 ? '\n' : '\n\n';
    }
    out += f.text.replace(/[\r\n]+$/, '');
    prev = f;
  }
  if (out) out += '\n';
  return { markdown: out, fired, tested, terminals, selected: selected.map((f) => f.id), instructional };
}
"""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MCRDR skill trees</title>
<style>
:root {
  --bg: #ffffff; --panel: #f3f6f8; --tint: #e0f1ef; --ink: #202b36; --navy: #16324f; --teal: #087f73;
  --muted: #596674; --line: #d8e0e6; --amber: #b66b17; --amber-bg: #fff2df; --code: #f7f9fa;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #14181d; --panel: #1c2229; --tint: #16302d; --ink: #e6ebef; --navy: #cfe0f2; --teal: #4fc2b4;
    --muted: #9aa7b3; --line: #2f3943; --amber: #e3a35a; --amber-bg: #33270f; --code: #1a2027;
  }
}
:root[data-theme="dark"] {
  --bg: #14181d; --panel: #1c2229; --tint: #16302d; --ink: #e6ebef; --navy: #cfe0f2; --teal: #4fc2b4;
  --muted: #9aa7b3; --line: #2f3943; --amber: #e3a35a; --amber-bg: #33270f; --code: #1a2027;
}
* { box-sizing: border-box; }
html, body { margin: 0; background: var(--bg); color: var(--ink);
  font: 15px/1.5 Arial, "Helvetica Neue", Helvetica, sans-serif; }
header { padding: 18px 28px 10px; border-bottom: 1px solid var(--line); position: relative; }
.themebtn { position: absolute; right: 28px; top: 18px; font: inherit; font-size: 12.5px; border: 1px solid var(--line);
  background: var(--panel); color: var(--ink); border-radius: 6px; padding: 4px 10px; cursor: pointer; }
.eyebrow { color: var(--teal); font-weight: 700; font-size: 12px; letter-spacing: .08em; text-transform: uppercase; }
h1 { margin: 4px 0 2px; font-size: 26px; color: var(--navy); }
.sub { color: var(--muted); }
nav { display: flex; flex-wrap: wrap; gap: 8px; padding: 12px 28px; border-bottom: 1px solid var(--line); }
nav button { font: inherit; border: 1px solid var(--line); background: var(--panel); color: var(--ink);
  border-radius: 999px; padding: 6px 14px; cursor: pointer; }
nav button small { color: var(--muted); margin-left: 4px; }
nav button[aria-pressed="true"] { background: var(--teal); border-color: var(--teal); color: #fff; }
nav button[aria-pressed="true"] small { color: #d7f1ed; }
main { display: grid; grid-template-columns: minmax(280px, 340px) 1fr; gap: 20px; padding: 18px 28px 28px; }
@media (max-width: 900px) { main { grid-template-columns: 1fr; padding: 16px; } header, nav { padding-left: 16px; padding-right: 16px; } }
section.card { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; margin-bottom: 14px; }
h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin: 0 0 8px; }
.stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
.stat b { display: block; font-size: 22px; color: var(--teal); }
.stat span { font-size: 12px; color: var(--muted); }
.example { display: block; width: 100%; text-align: left; font: inherit; font-size: 13px; color: var(--ink);
  background: var(--bg); border: 1px solid var(--line); border-radius: 8px; padding: 7px 10px; margin: 6px 0; cursor: pointer; }
.example:hover, .example.on { border-color: var(--teal); }
.example em { color: var(--muted); font-style: normal; font-size: 12px; display: block; }
.feature { margin: 10px 0 4px; font-weight: 700; color: var(--navy); font-size: 14px; }
.feature small { font-weight: 400; color: var(--muted); }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip { font: inherit; font-size: 12.5px; border: 1px solid var(--line); background: var(--bg); color: var(--ink);
  border-radius: 999px; padding: 4px 10px; cursor: pointer; }
.chip[aria-pressed="true"] { background: var(--teal); border-color: var(--teal); color: #fff; }
.taskline { font-size: 14px; margin: 0 0 4px; }
.facts { font-family: "Courier New", monospace; font-size: 13px; color: var(--navy); word-break: break-word; }
.tree { display: flex; flex-direction: column; align-items: center; }
.node { border: 1px solid var(--line); background: var(--bg); border-radius: 9px; padding: 8px 10px; min-width: 150px; }
.node .cond { font-family: "Courier New", monospace; font-size: 12.5px; color: var(--navy); }
.node .what { font-size: 12px; color: var(--muted); margin-top: 2px; }
.node .tag { font-size: 11px; font-weight: 700; letter-spacing: .04em; text-transform: uppercase; }
.node.fired { background: var(--tint); border-color: var(--teal); }
.node.terminal { box-shadow: 0 0 0 2px var(--teal) inset; }
.node.untested { opacity: .45; }
.node.stop { border-color: var(--amber); background: var(--amber-bg); }
.node.root, .node.root.fired, .node.root.terminal { background: #16324f; color: #fff; border-color: #16324f; box-shadow: none; }
.node.root .cond, .node.root .what { color: #e8eef4; }
.kids { display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 10px; width: 100%; margin-top: 16px;
  position: relative; padding-top: 14px; border-top: 2px solid var(--line); }
.legend { display: flex; flex-wrap: wrap; gap: 14px; font-size: 12px; color: var(--muted); margin-top: 10px; }
.legend i { display: inline-block; width: 12px; height: 12px; border-radius: 3px; vertical-align: -2px; margin-right: 4px; border: 1px solid var(--line); }
.toolbar { display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 8px; }
.toggle button { font: inherit; font-size: 12.5px; border: 1px solid var(--line); background: var(--bg); color: var(--ink); padding: 4px 10px; cursor: pointer; }
.toggle button:first-child { border-radius: 6px 0 0 6px; } .toggle button:last-child { border-radius: 0 6px 6px 0; }
.toggle button[aria-pressed="true"] { background: var(--navy); color: var(--bg); border-color: var(--navy); }
.meter { height: 8px; background: var(--line); border-radius: 99px; overflow: hidden; margin: 6px 0 2px; }
.meter div { height: 100%; background: var(--teal); }
.guidance { background: var(--bg); border: 1px solid var(--line); border-radius: 8px; padding: 6px 18px; max-height: 70vh; overflow: auto; }
.guidance pre.raw { white-space: pre-wrap; font-size: 12.5px; }
.guidance pre { background: var(--code); border: 1px solid var(--line); border-radius: 6px; padding: 8px; overflow: auto; font-size: 12.5px; }
.guidance table { border-collapse: collapse; margin: 8px 0; font-size: 13px; }
.guidance th, .guidance td { border: 1px solid var(--line); padding: 4px 8px; text-align: left; vertical-align: top; }
.guidance h1, .guidance h2, .guidance h3, .guidance h4 { color: var(--navy); text-transform: none; letter-spacing: 0; }
.guidance h1 { font-size: 20px; } .guidance h2 { font-size: 17px; color: var(--navy); } .guidance h3 { font-size: 15px; } .guidance h4 { font-size: 14px; }
a { color: var(--teal); }
.empty { color: var(--muted); font-style: italic; }
footer { color: var(--muted); font-size: 12px; padding: 0 28px 20px; }
</style>
</head>
<body>
<header>
  <button id="theme" class="themebtn" type="button">Switch to dark</button>
  <div class="eyebrow">Research demo · RDR skill trees</div>
  <h1>MCRDR skill trees, live</h1>
  <div class="sub">Pick a skill, choose an example task or tick task facts, and watch which rules fire and what guidance the agent receives.</div>
</header>
<nav id="skills" aria-label="Skills"></nav>
<main>
  <div>
    <section class="card" id="about"></section>
    <section class="card"><h2>Example tasks</h2><div id="examples"></div></section>
    <section class="card"><h2>Task facts</h2><div id="features"></div></section>
  </div>
  <div>
    <section class="card">
      <div class="toolbar"><h2 style="margin:0">Tree</h2><a id="treeview" target="_blank" rel="noopener">Open full tree view ↗</a></div>
      <div id="taskinfo"></div>
      <div class="tree" id="tree"></div>
      <div class="legend"><span><i style="background:var(--tint);border-color:var(--teal)"></i>fires</span>
        <span><i style="box-shadow:0 0 0 2px var(--teal) inset"></i>reports its guidance</span>
        <span><i style="opacity:.45"></i>not tested (parent did not fire)</span></div>
    </section>
    <section class="card">
      <div class="toolbar"><h2 style="margin:0">Guidance the agent receives</h2>
        <div class="toggle" role="group" aria-label="View"><button id="viewRendered" aria-pressed="true">Rendered</button><button id="viewRaw" aria-pressed="false">Markdown</button></div></div>
      <div id="meter"></div>
      <div class="guidance" id="guidance"></div>
    </section>
  </div>
</main>
<footer id="foot"></footer>
<script id="data" type="application/json">__DATA__</script>
<script>
__CORE__
const SKILLS = JSON.parse(document.getElementById('data').textContent);
const ORDER = Object.keys(SKILLS);
let current = null, kase = {}, chosen = null, raw = false;
const $ = (id) => document.getElementById(id);
const el = (tag, attrs = {}, ...kids) => { const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) { if (k === 'class') e.className = v; else if (k === 'text') e.textContent = v; else e.setAttribute(k, v); }
  for (const k of kids) if (k != null) e.append(k); return e; };
const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

function inline(s) {
  return esc(s).replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
}
function markdown(md) {
  const lines = md.split('\n'); let html = '', i = 0, list = null;
  const close = () => { if (list) { html += '</' + list + '>'; list = null; } };
  while (i < lines.length) {
    const line = lines[i];
    const fence = line.match(/^\s*(```|~~~)/);
    if (fence) { close(); const body = []; i++;
      while (i < lines.length && !lines[i].trim().startsWith(fence[1])) body.push(lines[i++]);
      i++; html += '<pre><code>' + esc(body.join('\n')) + '</code></pre>'; continue; }
    if (/^\s*\|/.test(line)) { close(); const rows = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) rows.push(lines[i++]);
      const cells = (r) => r.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
      let head = null;
      if (rows.length > 1 && cells(rows[1]).every((c) => /^:?-{3,}:?$/.test(c))) { head = cells(rows[0]); rows.splice(0, 2); }
      html += '<table>' + (head ? '<tr>' + head.map((c) => '<th>' + inline(c) + '</th>').join('') + '</tr>' : '') +
        rows.map((r) => '<tr>' + cells(r).map((c) => '<td>' + inline(c) + '</td>').join('') + '</tr>').join('') + '</table>';
      continue; }
    const h = line.match(/^\s{0,3}(#{1,6})\s+(.*)$/);
    if (h) { close(); const level = Math.min(4, h[1].length); html += `<h${level}>` + inline(h[2]) + `</h${level}>`; i++; continue; }
    const li = line.match(/^\s*([-*]|\d+\.)\s+(.*)$/);
    if (li) { const kind = /\d/.test(li[1]) ? 'ol' : 'ul'; if (list !== kind) { close(); html += '<' + kind + '>'; list = kind; }
      html += '<li>' + inline(li[2]) + '</li>'; i++; continue; }
    if (/^\s*(---|\*\*\*)\s*$/.test(line)) { close(); html += '<hr>'; i++; continue; }
    if (!line.trim()) { close(); i++; continue; }
    if (/^\s*<!--.*-->\s*$/.test(line)) { i++; continue; }
    close(); const para = [];
    while (i < lines.length && lines[i].trim() && !/^\s*(```|~~~|\||#{1,6}\s|[-*]\s|\d+\.\s|<!--|---\s*$)/.test(lines[i])) para.push(lines[i++]);
    if (!para.length) para.push(lines[i++]);
    html += '<p>' + inline(para.join(' ')) + '</p>';
  }
  close(); return html;
}

function condText(c) {
  if (c.op === 'eq') return `${c.field} = ${c.value}`;
  if (c.op === 'in') return `${c.field} ∈ {${c.value.join(', ')}}`;
  return `${c.field} ∋ ${c.value}`;
}
function snippet(skill, node) {
  if (node.stopping) return 'stops its parent';
  const ids = (node.conclusion && node.conclusion.fragments) || [];
  const frag = skill.fragments.find((f) => f.id === ids[0]);
  if (!frag) return ids.length ? '' : 'no guidance';
  const text = frag.text.split('\n').find((l) => l.trim()) || '';
  const clean = text.replace(/^[#>*\-\d.\s|]+/, '').replace(/[*`|]/g, ' ').replace(/\s+/g, ' ').trim();
  return (clean.length > 70 ? clean.slice(0, 69) + '…' : clean) + (ids.length > 1 ? `  (+${ids.length - 1})` : '');
}

function defaultCase(skill) {
  const c = {};
  for (const [name, spec] of Object.entries(skill.features))
    c[name] = spec.multiple ? [] : (spec.values.includes('unknown') ? 'unknown' : spec.values[0]);
  return c;
}

function selectSkill(id) {
  current = SKILLS[id]; kase = defaultCase(current); chosen = null;
  for (const b of $('skills').children) b.setAttribute('aria-pressed', String(b.dataset.id === id));
  const s = current;
  $('about').replaceChildren(el('h2', { text: 'Skill' }),
    el('div', { style: 'font-weight:700;color:var(--navy);margin-bottom:6px', text: s.title }),
    el('div', { class: 'stats' },
      el('div', { class: 'stat' }, el('b', { text: String(s.source_lines) }), el('span', { text: 'source lines' })),
      el('div', { class: 'stat' }, el('b', { text: String(s.rules) }), el('span', { text: 'conditional rules' })),
      el('div', { class: 'stat' }, el('b', { text: String(s.nodes.length) }), el('span', { text: 'tree nodes' }))),
    el('div', { style: 'font-size:12px;color:var(--muted);margin-top:8px',
      text: `${s.name}, ${s.variant}. Rules extracted with ${s.model_id || 'unrecorded model'}.` }));
  $('treeview').href = s.tree_view;
  const ex = $('examples'); ex.replaceChildren();
  if (!s.examples.length) ex.append(el('div', { class: 'empty', text: 'No examples for this skill.' }));
  s.examples.forEach((e, i) => {
    const b = el('button', { class: 'example' }, e.task ? '“' + e.task + '”' : 'Preset: ' + e.label,
      el('em', { text: Object.entries(e.case).map(([k, v]) => `${k}: ${Array.isArray(v) ? (v.join(', ') || '—') : v}`).join(' · ') }));
    b.onclick = () => { kase = JSON.parse(JSON.stringify(e.case)); chosen = i; update(); };
    ex.append(b);
  });
  ex.append(Object.assign(el('button', { class: 'example' }, 'Clear all facts', el('em', { text: 'nothing recognised: general guidance only' })),
    { onclick: () => { kase = defaultCase(current); chosen = null; update(); } }));
  update();
}

function update() {
  const s = current;
  [...$('examples').children].forEach((b, i) => b.classList.toggle('on', i === chosen));
  const box = $('features'); box.replaceChildren();
  for (const [name, spec] of Object.entries(s.features)) {
    box.append(el('div', { class: 'feature', title: spec.description }, name + ' ', el('small', { text: spec.multiple ? 'any number' : 'one value' })));
    const chips = el('div', { class: 'chips' });
    for (const v of spec.values) {
      const on = spec.multiple ? kase[name].includes(v) : kase[name] === v;
      const chip = el('button', { class: 'chip', 'aria-pressed': String(on), text: v });
      chip.onclick = () => {
        if (spec.multiple) kase[name] = on ? kase[name].filter((x) => x !== v) : spec.values.filter((x) => x === v || kase[name].includes(x));
        else kase[name] = v;
        chosen = null; update();
      };
      chips.append(chip);
    }
    box.append(chips);
  }
  if (!Object.keys(s.features).length) box.append(el('div', { class: 'empty', text: 'This skill has no task facts: every task gets the general guidance.' }));

  const r = renderSkill(s, kase);
  const ex = chosen !== null ? s.examples[chosen] : null;
  $('taskinfo').replaceChildren(
    ex && ex.task ? el('p', { class: 'taskline' }, el('b', { text: 'Task  ' }), '“' + ex.task + '”') : '',
    el('div', { class: 'facts', text: 'facts = ' + JSON.stringify(kase) }));
  const node = (id, root) => {
    const n = s.nodes[id];
    const cls = ['node', root ? 'root' : '', r.fired.has(id) ? 'fired' : '', r.terminals.includes(id) ? 'terminal' : '',
                 r.tested.has(id) ? '' : 'untested', n.stopping ? 'stop' : ''].join(' ');
    const status = !r.tested.has(id) ? 'not tested' : r.terminals.includes(id) ? 'reports' : r.fired.has(id) ? 'fires' : 'no';
    const box = el('div', { class: cls },
      el('div', { class: 'tag', style: `color:${root ? '#e8eef4' : r.fired.has(id) ? 'var(--teal)' : 'var(--muted)'}`, text: `#${id} · ${status}` }),
      el('div', { class: 'cond', text: root ? 'root: default, used only if no rule fires' : (n.conditions.map(condText).join(' ∧ ') || 'always') }),
      el('div', { class: 'what', text: root ? '' : snippet(s, n) }));
    if (!n.children.length) return box;
    const wrap = el('div', { style: 'width:100%;display:flex;flex-direction:column;align-items:center' }, box);
    const kids = el('div', { class: 'kids' });
    for (const c of n.children) kids.append(node(c, false));
    wrap.append(kids);
    return wrap;
  };
  $('tree').replaceChildren(node(0, true));

  const used = r.selected.map((id) => s.fragments.find((f) => f.id === id)).filter((f) => f.role === 'instruction');
  const total = s.fragments.filter((f) => f.role === 'instruction');
  const share = s.source_chars ? r.markdown.length / s.source_chars : 0;
  $('meter').replaceChildren(
    el('div', { style: 'font-size:13px', text: `${used.length} of ${total.length} guidance fragments · ${r.markdown.length.toLocaleString()} of ${s.source_chars.toLocaleString()} characters of the full skill (${Math.round(share * 100)}%)` }),
    el('div', { class: 'meter' }, el('div', { style: `width:${Math.min(100, share * 100).toFixed(1)}%` })));
  const g = $('guidance');
  if (!r.markdown) g.innerHTML = '<p class="empty">No guidance selected.</p>';
  else if (raw) g.replaceChildren(el('pre', { class: 'raw', text: r.markdown }));
  else g.innerHTML = markdown(r.markdown);
}

for (const id of ORDER) {
  const s = SKILLS[id];
  const b = el('button', { 'data-id': id, 'aria-pressed': 'false' }, s.name, el('small', { text: s.variant }));
  b.onclick = () => selectSkill(id);
  $('skills').append(b);
}
$('viewRendered').onclick = () => { raw = false; $('viewRendered').setAttribute('aria-pressed', 'true'); $('viewRaw').setAttribute('aria-pressed', 'false'); update(); };
$('viewRaw').onclick = () => { raw = true; $('viewRaw').setAttribute('aria-pressed', 'true'); $('viewRendered').setAttribute('aria-pressed', 'false'); update(); };
$('foot').textContent = 'Generated from the verified compiled bundles; the guidance shown is exactly what the skill service returns for these facts.';
const isDark = () => (document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')) === 'dark';
const label = () => { $('theme').textContent = isDark() ? 'Switch to light' : 'Switch to dark'; };
$('theme').onclick = () => { document.documentElement.dataset.theme = isDark() ? 'light' : 'dark'; label(); };
label();
selectSkill(ORDER[0]);
</script>
</body>
</html>
"""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build the interactive MCRDR demo page")
    parser.add_argument("--output", type=Path, default=ROOT / "demo", help="Output folder (default: demo/)")
    parser.add_argument("--skills", nargs="+", help="Skill folders to include (default: every compiled skill)")
    parser.add_argument("--check", action="store_true", help="Verify the page renderer against SkillRenderer via Node")
    args = parser.parse_args(argv)

    names = args.skills or [d.name for d in sorted((ROOT / "skills").iterdir())
                            if (d / "compiled" / "manifest.json").exists()]
    names.sort(key=lambda n: (ORDER.index(n) if n in ORDER else len(ORDER), n))
    skills, skipped = [], []
    for name in names:
        try:
            skills.append(skill_data(ROOT / "skills" / name))
        except Exception as error:  # Report and continue: a half-converted skill must not block the demo.
            skipped.append(f"{name}: {error}")
    if not skills:
        raise SystemExit("no compiled skills could be loaded")
    if args.check:
        check_with_node(skills, CORE_JS)

    out = args.output.resolve()
    (out / "trees").mkdir(parents=True, exist_ok=True)
    for skill in skills:
        subprocess.run([sys.executable, str(ROOT / "skills" / "visualise_tree.py"),
                        str(ROOT / "skills" / skill["id"] / "compiled" / "tree.json"),
                        "-o", str(out / "trees" / f"{skill['id']}.html"),
                        "--title", f"{skill['name']} ({skill['variant']}) MCRDR tree"],
                       check=True, capture_output=True)
    data = json.dumps({s["id"]: {k: v for k, v in s.items() if not k.startswith("_")} for s in skills},
                      ensure_ascii=False).replace("</", "<\\/")
    (out / "index.html").write_text(PAGE.replace("__CORE__", CORE_JS).replace("__DATA__", data), encoding="utf-8")
    print(f"Wrote {out / 'index.html'} with {len(skills)} skills: {', '.join(s['id'] for s in skills)}")
    for line in skipped:
        print(f"  skipped {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
