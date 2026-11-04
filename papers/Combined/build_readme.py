"""Generate papers/Combined/README.md (run: python papers/Combined/build_readme.py). Numbers are copied from the survey page's data,
which was transcribed from the papers; absolute Trace2Skill scores are computed here as
baseline + reported delta."""
from pathlib import Path

OUT = Path(__file__).resolve().parent / "README.md"


def table(header, rows, num_from=1):
    align = ["---"] * num_from + ["---:"] * (len(header) - num_from)
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(align) + " |"]
    lines += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(lines)


def fmt(v, d=1):
    return "–" if v is None else f"{v:.{d}f}"


# ------------------------------------------------------------------ SkillOpt Table 1
SOH = ["SearchQA", "Spreadsheet", "OfficeQA", "DocVQA", "LiveMath", "ALFWorld"]
D = ["No skill", "Human skill", "LLM skill", "Trace2Skill", "TextGrad", "GEPA", "SkillOpt"]
H = ["No skill", "Human skill", "LLM skill", "EvoSkill", "SkillOpt"]
SO = [
    ["GPT-5.5 · direct chat", D, [[77.7,41.8,33.1,78.8,37.6,83.6],[81.8,72.9,66.9,90.1,38.4,91.8],[80.9,43.2,51.7,89.6,40.0,93.3],[82.4,49.6,65.7,90.6,52.0,87.3],[81.4,41.1,42.0,87.2,49.2,82.8],[84.8,73.6,63.9,89.1,43.2,85.8],[87.3,80.7,72.1,91.2,66.9,95.5]]],
    ["GPT-5.4 · direct chat", D, [[76.9,41.4,50.0,77.6,36.8,75.4],[80.1,59.3,59.3,88.5,41.6,74.6],[79.4,46.1,20.4,90.4,36.8,83.6],[81.2,53.2,51.2,89.3,40.0,73.1],[82.5,38.6,54.7,85.3,36.6,78.4],[82.4,61.1,60.4,88.3,41.6,74.6],[83.1,62.5,62.8,91.2,44.0,91.0]]],
    ["GPT-5.4-mini · direct chat", D, [[75.9,36.1,22.1,71.4,14.7,73.1],[77.2,42.9,45.9,85.0,28.8,56.7],[76.1,36.8,36.6,86.4,28.0,65.7],[78.6,40.7,20.9,88.5,32.8,82.8],[77.5,38.2,30.0,84.0,27.2,70.9],[79.4,42.5,45.3,83.7,27.2,81.3],[80.2,47.5,48.8,90.9,32.8,85.8]]],
    ["GPT-5.4-nano · direct chat", D, [[55.8,23.5,16.3,30.8,23.2,34.3],[66.1,41.8,46.5,73.5,16.8,29.9],[62.4,33.6,12.8,71.7,20.0,65.7],[69.9,35.4,16.3,77.3,25.6,58.2],[73.4,32.5,38.7,68.3,20.8,42.5],[73.2,37.2,45.0,71.2,24.8,68.7],[74.8,42.5,50.0,80.2,27.2,69.4]]],
    ["GPT-5.2 · direct chat", D, [[71.9,38.2,34.9,73.1,20.8,68.7],[75.8,48.2,55.2,89.0,23.2,56.7],[73.7,38.6,14.0,87.7,25.6,68.7],[79.2,43.9,43.0,87.7,28.8,76.9],[82.6,32.1,51.1,87.2,22.4,76.1],[82.7,54.3,53.4,89.5,28.8,82.8],[83.1,57.1,56.4,89.6,36.0,85.1]]],
    ["Qwen3.5-4B · direct chat", D, [[68.1,9.3,14.5,86.9,22.4,30.6],[66.3,16.4,22.7,87.8,18.4,28.4],[65.0,10.4,20.9,88.0,28.8,55.2],[68.5,19.3,16.3,88.0,27.2,64.9],[60.7,13.9,20.9,85.6,10.6,53.7],[68.6,16.9,22.7,85.1,28.8,60.4],[71.2,23.9,29.7,89.0,52.0,81.3]]],
    ["Qwen3.6-35B-A3B · direct chat", D, [[72.7,38.2,45.9,87.6,31.2,59.7],[74.1,44.3,41.9,88.4,29.6,44.8],[72.6,42.9,46.5,88.4,24.8,60.4],[75.4,33.2,32.0,90.4,29.6,70.9],[76.4,22.9,33.7,84.5,7.2,67.9],[75.8,45.4,43.6,88.0,31.2,73.9],[80.3,47.5,47.1,91.4,41.6,82.1]]],
    ["GPT-5.5 · Codex harness", H, [[81.8,27.5,38.3,87.2,35.2,None],[84.1,50.7,40.0,88.8,48.8,None],[83.4,25.0,34.3,89.8,45.6,None],[61.4,67.5,42.4,89.3,63.2,None],[87.3,85.0,51.1,92.2,78.4,None]]],
    ["GPT-5.5 · Claude Code harness", H, [[81.9,22.1,57.6,86.6,40.8,None],[83.7,37.1,66.3,88.0,44.0,None],[82.4,37.1,56.4,89.6,44.8,None],[84.0,75.0,70.3,87.2,52.0,None],[85.9,80.4,71.5,90.1,56.5,None]]],
]
ALL_SO_METHODS = ["No skill", "Human skill", "LLM skill", "Trace2Skill", "TextGrad", "GEPA", "EvoSkill", "SkillOpt"]


def skillopt_grid(col):
    j = SOH.index(col)
    rows = []
    for name, methods, data in SO:
        vals = {m: data[i][j] for i, m in enumerate(methods)}
        if all(v is None for v in vals.values()):
            continue
        best = max(v for v in vals.values() if v is not None)
        cells = []
        for m in ALL_SO_METHODS:
            v = vals.get(m)
            cells.append("" if m not in vals else ("–" if v is None else (f"**{v:.1f}**" if v == best else f"{v:.1f}")))
        rows.append([name] + cells)
    return table(["Model · harness"] + ALL_SO_METHODS, rows)


# ------------------------------------------------------------------ WikiSkill Table 1
WKH = ["LiveMath", "SealQA", "SpreadSheet", "OfficeQA", "ALFWorld"]
WKM = ["No skill", "Trace2Skill", "EvoSkill", "SkillOpt", "WikiSkill"]
WKD = [
    ["Qwen-3.5-4B", [[29.1,32.5,14.6,30.2,24.4],[31.5,37.6,17.5,31.0,42.8],[41.7,37.3,18.6,29.5,41.5],[48.7,33.3,14.0,34.5,45.3],[49.7,39.4,21.1,28.5,53.7]]],
    ["Qwen-3.5-9B", [[28.2,26.3,24.3,35.9,34.7],[33.1,36.9,26.5,38.4,48.8],[58.1,34.5,35.4,34.9,48.5],[48.7,29.4,29.0,38.0,55.7],[56.3,43.1,33.6,40.5,63.4]]],
    ["Qwen-3.6-27B", [[33.9,27.5,40.8,42.1,52.8],[36.3,37.3,53.3,54.3,55.5],[57.3,32.9,59.5,52.5,64.2],[51.9,34.5,53.2,54.8,59.2],[61.9,41.6,81.7,53.7,77.6]]],
    ["Gemma-4-31B", [[33.9,30.6,48.3,43.3,50.4],[32.3,37.7,58.5,43.2,57.2],[29.8,38.4,56.4,39.9,52.6],[40.1,36.1,63.1,44.4,61.9],[56.7,41.2,68.0,44.2,64.4]]],
    ["Gemini-3.5-Flash", [[33.0,29.4,50.5,48.6,85.9],[41.9,44.3,56.0,50.0,85.9],[44.6,43.6,55.4,51.2,85.9],[49.7,28.2,66.1,49.8,85.9],[72.6,44.7,76.6,60.7,85.9]]],
]


def wikiskill_grid(col):
    j = WKH.index(col)
    rows = []
    for name, data in WKD:
        vals = [r[j] for r in data]
        best = max(vals)
        rows.append([name] + [f"**{v:.1f}**" if v == best else f"{v:.1f}" for v in vals])
    return table(["Inference model"] + WKM, rows)


# ------------------------------------------------------------------ Trace2Skill Table 1
T1H = ["Vrf", "Soft", "Hard", "WikiTQ", "HiTab"]
BASE = {
    "No Skill": [27.67, 28.90, 17.57, 21.50, 14.42, 19.00, 18.00, 4.60, 13.33, 19.12, 18.19],
    "Human-Written": [48.33, 36.30, 17.03, 74.68, 41.31, 9.67, 13.03, 3.37, 9.02, 5.30, 26.93],
    "Parametric": [26.17, 36.60, 17.50, 23.73, 17.36, 20.17, 13.70, 3.87, 20.14, 13.94, 19.23],
}
EVO = [
    ["122B · Deepening", "Human-Written", [
        ["+Error", [17.50, 10.30, 10.40, 1.62, 2.14, 27.00, 9.44, 2.86, 9.26, 9.55, 9.28]],
        ["+Success", [18.00, 8.60, 8.70, -10.35, -0.95, 19.66, 6.84, 1.33, 12.09, 22.33, 8.15]],
        ["+Combined", [21.50, 10.87, 12.50, 4.56, 2.97, 21.16, 8.84, 1.80, 6.64, 11.94, 9.65]]]],
    ["122B · Creation", "Parametric", [
        ["+Error", [22.83, 3.77, 5.87, 7.89, -1.70, 8.66, 9.53, 4.00, 2.06, 9.64, 6.79]],
        ["+Success", [15.33, -0.93, 4.33, 23.70, 19.89, 12.83, 11.57, 6.13, 30.36, 28.90, 16.96]],
        ["+Combined", [14.00, -0.63, 3.53, 32.32, 23.11, 15.50, 14.50, 7.23, 29.70, 27.71, 18.62]]]],
    ["35B · Deepening", "Human-Written", [
        ["+Error", [16.67, 8.50, 8.14, -6.36, -2.38, 17.33, 9.17, 4.83, 2.71, 8.08, 5.64]],
        ["+Success", [2.17, 2.73, 3.30, 1.46, 1.70, 12.33, 5.87, 1.23, 43.23, 34.70, 12.44]],
        ["+Combined", [6.67, 3.87, 4.17, 2.65, 2.44, 20.00, 5.77, 2.36, 42.20, 36.46, 14.04]]]],
    ["35B · Creation", "Parametric", [
        ["+Error", [1.00, -7.70, 1.03, 57.65, 28.25, 3.83, 7.30, 2.66, 12.66, 17.81, 15.22]],
        ["+Success", [5.33, -4.57, 2.43, 9.09, 2.34, 5.66, 5.80, 2.63, 3.31, 18.94, 5.65]],
        ["+Combined", [8.33, -5.83, 2.00, 30.82, 16.93, 4.33, 9.73, 4.73, 18.00, 25.22, 13.31]]]],
]


def t2s_table1():
    rows = [[k] + [f"{v:.2f}" for v in vals] for k, vals in BASE.items()]
    for author, base, conds in EVO:
        for cond, deltas in conds:
            rows.append([f"Author {author} {cond}"] + [f"{b + d:.2f}" for b, d in zip(BASE[base], deltas)])
    header = ["Skill"] + [f"122B {h}" for h in T1H] + [f"35B {h}" for h in T1H] + ["Avg"]
    return table(header, rows)


# ------------------------------------------------------------------ Trace2Skill DocVQA / math
def t2s_docvqa():
    base = {"122B user": (0.6300, 70.27), "35B user": (0.6582, 73.17)}
    deltas = [
        ("122B +Error", (0.1949, 17.69), (0.1974, 16.64)), ("122B +Success", (0.1639, 14.89), (0.1668, 14.23)),
        ("122B +Combined", (0.2534, 22.25), (0.2049, 17.22)), ("35B +Error", (0.0884, 6.79), (0.1267, 10.15)),
        ("35B +Success", (0.1572, 14.53), (0.2132, 18.27)), ("35B +Combined", (0.0958, 7.73), (0.2158, 18.83)),
    ]
    rows = [["No skill", "0.6300 / 70.27", "0.6582 / 73.17"]]
    for name, a, b in deltas:
        rows.append([f"Author {name}", f"{0.6300 + a[0]:.4f} / {70.27 + a[1]:.2f}", f"{0.6582 + b[0]:.4f} / {73.17 + b[1]:.2f}"])
    return table(["Skill (created from no skill)", "122B user ANLS / Acc", "35B user ANLS / Acc"], rows)


def t2s_math():
    base = [91.0, 90.4, 89.0, 83.3]
    deltas = [("122B +Error", [4.0, 2.9, 5.0, 5.0]), ("122B +Success", [2.0, 0.4, 2.0, 4.6]), ("122B +Combined", [3.0, -1.2, 6.0, 5.5]),
              ("35B +Error", [3.0, 1.3, 4.0, 0.5]), ("35B +Success", [-1.0, -1.2, 0.0, -1.2]), ("35B +Combined", [1.0, -0.4, 1.0, 0.0])]
    rows = [["No skill"] + [fmt(v) for v in base]]
    rows += [[f"Author {n}"] + [fmt(b + d) for b, d in zip(base, ds)] for n, ds in deltas]
    return table(["Skill (created from no skill)", "122B DAPO test", "122B AIME 2026", "35B DAPO test", "35B AIME 2026"], rows)


# ------------------------------------------------------------------ write
S = []
S.append("""# Combined benchmarks and targets

Every benchmark we consider compatible with our pipeline, drawn from Trace2Skill,
SkillOpt, WikiSkill and outside work, plus every reported result on those benchmarks
that we need to match or beat. Results on benchmarks we do not run are left out.

- **Data:** [`sources.json`](sources.json) lists each benchmark with its pinned source.
  Items shared with a paper folder point to that folder's download instead of
  copying it (`ref`). New external sets download into `local-data/` (git-ignored).
- **Fetch:** `python papers/fetch.py Combined --list`, then `python papers/fetch.py Combined`
  for the small sets, or `--only <id> ...` for large, gated or adapter-dependent ones.
- **Sources of numbers:** the paper's own table or figure is named under each block.
  Values were transcribed by hand; check the paper before quoting one in a write-up.

**Compatibility rule.** The worker gets a task (and usually a file), the judge can read
the same inputs as text, and a deterministic scorer gives the answer key so the
inspector and checker can re-score. Fit codes used below:

| Code | Meaning |
|---|---|
| **Spec** | Task + input file + exact answer key. Fits the pipeline as it is (an adapter for the file type may be needed) |
| **Text** | Text-only task with an exact answer key. Fits, but the judge has only the question text to condition on |
| **Adapter** | Needs a new environment or tool adapter before it fits (live search, simulator, containers) |

## 1. Benchmarks
""")

BENCH = [
    ("Spreadsheets and tables", [
        ("SpreadsheetBench-Verified 400", "Instruction + .xlsx", "Cell values at the answer position", "Spec", "T2S, SkillOpt, WikiSkill, Regression Tax, EvoSkill/XSkill/SkillGen (via T2S)", "`spreadsheetbench-verified`, `spreadsheetbench-t2s`", "Fetched"),
        ("SpreadsheetBench 912 (Soft / Hard)", "Instruction + .xlsx", "Cell values (soft / hard test cases)", "Spec", "T2S", "`spreadsheetbench-t2s`", "Fetched"),
        ("WikiTableQuestions (spreadsheet form)", "Question + table", "Answer accuracy", "Spec", "T2S", "`wikitq`", "Raw fetched; conversion to .xlsx not released"),
        ("HiTab (spreadsheet form)", "Question + hierarchical table", "Answer accuracy", "Spec", "T2S", "`hitab`", "Raw fetched; conversion not released"),
        ("SheetCopilot", "Instruction + .xlsx", "Workbook checks", "Spec", "none (external)", "`sheetcopilot`", "Fetched"),
    ]),
    ("Documents and PDFs", [
        ("OfficeQA", "Question + Treasury Bulletin files", "Exact / ~1% numeric match", "Spec", "SkillOpt, WikiSkill, EvoSkill", "`officeqa`", "On demand (gated)"),
        ("OfficeQA-Pro", "Question + long financial PDFs", "~1% numeric match", "Spec", "Regression Tax", "`officeqa` (`officeqa_pro.csv`)", "On demand (gated)"),
        ("VAREX (Flat, Nested, Table)", "PDF form + JSON schema", "Field exact match", "Spec", "T2S (Flat), our pilot", "`varex`", "On demand; Nested/Table need the prepare step extended"),
        ("VRDU Registration Forms", "PDF form", "Field match", "Spec", "T2S (learning set)", "`vrdu-registration`", "On demand"),
        ("DocVQA", "Question + page image", "ANLS / exact match", "Spec (needs vision or OCR)", "T2S, SkillOpt", "`docvqa`, `docvqa-full`", "On demand"),
        ("TAT-DQA", "Question + financial report PDF", "Exact match / numeric F1", "Spec", "none (external)", "`tat-dqa`, `tat-dqa-docs`", "Questions fetched; PDFs on demand"),
        ("FinanceBench (open 150)", "Question + 10-K / 10-Q PDF", "Answer match (often needs a judge)", "Spec (scoring partly fuzzy)", "none (external)", "`financebench`, `financebench-pdfs`", "Questions fetched; PDFs on demand"),
        ("Kleister NDA / Charity", "Long PDF", "Key-field F1", "Spec", "none (external)", "`kleister-nda`, `kleister-charity`", "Fetched (NDA includes 726 PDFs)"),
        ("FUNSD", "Form image", "Entity / link F1", "Spec (needs vision or OCR)", "none (external)", "`funsd`", "Fetched"),
        ("CORD v2", "Receipt image", "Field F1 / tree accuracy", "Spec (needs vision or OCR)", "none (external)", "`cord`", "On demand"),
        ("SROIE", "Receipt image", "Key-field F1", "Spec (needs vision or OCR)", "none (external)", "`sroie`", "Manual (registration)"),
        ("DocILE", "Invoice PDF", "Field / line-item F1", "Spec", "none (external)", "`docile`", "Manual (token)"),
        ("InfographicVQA", "Question + infographic", "ANLS", "Spec (needs vision)", "none (external)", "`infographicvqa`", "On demand"),
        ("ChartQA", "Question + chart image", "Relaxed accuracy (5% numeric)", "Spec (needs vision)", "none (external)", "`chartqa`", "Test split fetched"),
        ("SlideVQA", "Question + slide deck images", "Exact match / F1", "Spec (needs vision)", "none (external)", "`slidevqa`", "On demand (gated)"),
    ]),
    ("Office files and multi-domain skill benchmarks", [
        ("OfficeBench (DOCX subset)", "Instruction + .docx", "Local verifier", "Spec", "T2S", "`officebench`", "On demand"),
        ("TSBench (Talk-to-Your-Slides)", "Instruction + .pptx", "Local verifier", "Spec", "T2S", "`tsbench`", "On demand"),
        ("SkillsBench", "Task + files in a container", "Deterministic test scripts", "Adapter (Harbor containers)", "SkillsBench, CoEvoSkills", "`skillsbench`", "On demand"),
    ]),
    ("Text reasoning and QA", [
        ("LiveMathematicianBench", "Multiple-choice question", "Option letter", "Text", "SkillOpt, WikiSkill", "`livemathematicianbench`", "Fetched (SkillOpt split)"),
        ("SearchQA", "Clue + search snippets", "Exact match", "Text", "SkillOpt", "`searchqa`", "On demand"),
        ("DAPO-Math", "Math problem (+ Python tool)", "Final answer", "Text", "T2S", "`dapo-math`", "Fetched"),
        ("AIME 2026", "Math problem", "Final answer (avg@8)", "Text", "T2S", "`aime-2026`", "Fetched"),
        ("AIME 2024 / 2025", "Math problem", "Final answer", "Text", "Dynamic Cheatsheet", "`aime-2024`, `aime-2025`", "Fetched"),
        ("GPQA-Diamond", "Multiple-choice question", "Option letter", "Text", "Dynamic Cheatsheet", "`gpqa-diamond` (also inside `dynamic-cheatsheet/data/`)", "Gated copy on demand; DC copy fetched"),
        ("Game of 24", "Four numbers", "Valid expression", "Text", "Dynamic Cheatsheet", "`dynamic-cheatsheet` (problems only in `embeddings/GameOf24.csv`)", "Fetched"),
        ("OlympiadBench → Omni-MATH", "Math problem", "Final answer", "Text", "SkillOpt (transfer)", "`olympiadbench`, `omni-math`", "Omni-MATH fetched; OlympiadBench on demand"),
    ]),
    ("Agentic (need an adapter)", [
        ("SealQA (Seal-0, Seal-Hard)", "Question + live web search", "Fuzzy match", "Adapter (live search; not replayable)", "WikiSkill, EvoSkill, SkillOpt (appendix)", "`sealqa`", "Fetched"),
        ("ALFWorld", "Text household simulator", "Goal reached", "Adapter (simulator)", "SkillOpt, WikiSkill, Memp, SkillRL, SkillGen", "`alfworld`", "On demand"),
        ("SOPBench Bank", "Customer request + tool database", "Oracle (permission + DB state + procedure)", "Adapter (already wired in this repo)", "SOPBench", "`sopbench-bank`", "In repo"),
    ]),
]
for group, rows in BENCH:
    S.append(f"**{group}**\n\n" + table(["Benchmark", "Input", "Scorer", "Fit", "Results reported by", "Item id", "Status"], rows, num_from=7) + "\n")

S.append("""### Left out

| Benchmark | Why it does not fit |
|---|---|
| WebArena, Mind2Web, WebShop | Live or self-hosted websites; no fixed input to replay |
| AppWorld, τ²-bench, BFCL | Simulated users or multi-app environments; τ² uses an LLM user, so re-scores are not deterministic |
| SWE-bench Verified, SWE-Skills-Bench | The input is a whole repository and runs are costly; skills changed SWE-Skills-Bench scores by only +0.34 on average |
| GAIA, HLE, BrowseComp | Mostly live search and knowledge, not procedure |
| ScienceWorld, TravelPlanner | Simulators or constraint planners; same adapter problem as ALFWorld, with no numbers from the three main papers |
| GEPA suite (HotpotQA, IFBench, HoVer, PUPA) | Results are for multi-stage DSPy programs, not a single worker; PUPA is LLM-judged |
| FiNER, Formula (ACE) | ACE's exact subsets are not identified |
| XSkill multimodal sets, SkillGen extras | Vision-tool or very small splits, no overlap with our pipeline |

## 2. Accuracy targets

Scores are percentages unless noted. **Bold** marks the best method per model row. All
SkillOpt and WikiSkill rows start each method from that paper's own starting skill
(SkillOpt: a per-benchmark initial skill; WikiSkill: empty).
""")

S.append("""### 2.1 SpreadsheetBench-Verified, 80 / 40 / 280 split

SkillOpt Table 1 (test 280). TextGrad and GEPA are direct-chat only; EvoSkill is harness only.

""" + skillopt_grid("Spreadsheet") + """

WikiSkill Table 1 (test 280, mean of 3 evolution runs).

""" + wikiskill_grid("SpreadSheet") + """

WikiSkill Table 3, wiki ablation, Gemini-3.5-Flash: no skill 50.5 · wiki for the inference agent only 44.4 · no wiki 49.9 · both 80.2 · proposer only (default) 76.6.

SkillOpt Table 3, ablations on GPT-5.5: default 77.5 · no learning-rate bound 75.7 · no rejected-edit buffer 72.9 · no meta skill 75.7 · no meta skill and no slow update 55.0 · one training example 47.5.
""")

S.append("""### 2.2 SpreadsheetBench, Trace2Skill protocol (200 / 200 split, Soft/Hard on 2,529, WikiTQ 2,810, HiTab 1,585)

Trace2Skill Table 1, converted to absolute scores (baseline + reported delta). Deepening
starts from Anthropic's `xlsx` skill (Human-Written), Creation from a model-written seed
(Parametric). Avg = (in-distribution mean + OOD mean) / 2 over both users. Mean of 3 seeds.

""" + t2s_table1() + """

Other Trace2Skill results on Vrf (Qwen3.5-122B user unless noted):

""" + table(["Result", "Score"], [
    ["Head-to-head, Fig. 7: no skill / Human-Written / XSkill / SkillGen", "27.7 / 48.3 / 23.0 / 27.5"],
    ["EvoSkill in Claude Code harness / in ReAct harness (Fig. 7)", "33.5 / 59.5"],
    ["Trace2Skill +Error / +Combined (Fig. 7)", "65.8 / 69.8"],
    ["+Error with Bayesian-optimised patch selection (Table 6)", "65.83 → 69.83"],
    ["Sequential consolidation B=4 / B=1 / parallel, 122B · 35B (Table 4)", "59.00 · 26.17 / 61.83 · 26.00 / 65.83 · 27.00"],
    ["Soft / Hard for those three rows, 122B", "40.63 / 20.63 · 44.40 / 25.40 · 46.60 / 27.43"],
    ["Soft / Hard for those three rows, 35B", "22.37 / 7.47 · 23.83 / 10.57 · 22.20 / 8.20"],
    ["ReasoningBank retrieval vs Trace2Skill, Vrf 122B / 35B (Table 5)", "56.00 vs 69.83 / 20.50 vs 29.67"],
    ["ReasoningBank vs Trace2Skill, Soft / Hard 122B", "40.10 / 21.30 vs 47.17 / 29.53"],
    ["skill-creator vs Trace2Skill +Error, Deepening avg over 4 pairs (Table 14)", "base 29.00 → 23.33 vs 48.63"],
    ["skill-creator vs Trace2Skill +Error, Creation avg", "base 23.17 → 22.50 vs 32.25"],
    ["Gemma-4-31B user: no skill / human / self-evolved +Error (Fig. 3)", "41.2 / 37.7 / 60.5"],
    ["GPT-5.5-high user: no skill / human / self-evolved +Error (Fig. 3)", "32.5 / 52.0 / 83.5"],
    ["Agentic vs log-only error analyst, Avg: 122B Deepen / 122B Create / 35B Deepen / 35B Create (Fig. 4)", "36.2 vs 28.0 / 26.0 vs 26.6 / 32.6 vs 28.8 / 34.5 vs 26.5"],
]) + "\n")

S.append("""### 2.3 SpreadsheetBench (392 tasks) and OfficeQA-Pro (94 tasks), Regression Tax

Regression Tax (arXiv 2607.22520) Table 2. One run per condition; skills were written
from no-skill failure traces by three skill creators. A gain is a task fixed, a regression
a task broken, both against no skill. Bold Net = p < .05 (exact McNemar).

""" + table(["Benchmark", "Stack", "Library", "No skill", "With skill", "Gains", "Regressions", "Net"], [
    ["OfficeQA-Pro", "OpenCode · MiniMax-M2.7", "anthropic / openai / Ours", "51.1", "52.1 / 55.3 / 60.6", "14 / 16 / 15", "13 / 12 / 6", "+1 / +4 / +9"],
    ["OfficeQA-Pro", "Codex · GPT-5.4-mini", "anthropic / openai / Ours", "54.3", "56.4 / 56.4 / 57.4", "17 / 11 / 16", "15 / 9 / 13", "+2 / +2 / +3"],
    ["OfficeQA-Pro", "Claude Code · Sonnet 4.6", "anthropic / openai / Ours", "72.3", "80.9 / 77.7 / 79.8", "10 / 12 / 11", "2 / 7 / 4", "**+8** / +5 / +7"],
    ["SpreadsheetBench", "OpenCode · MiniMax-M2.7", "anthropic / openai / Ours", "63.3", "70.2 / 64.3 / 65.6", "62 / 45 / 48", "35 / 41 / 39", "**+27** / +4 / +9"],
    ["SpreadsheetBench", "Codex · GPT-5.4-mini", "anthropic / openai / Ours", "65.8", "67.6 / 66.6 / 67.1", "30 / 30 / 28", "23 / 27 / 23", "+7 / +3 / +5"],
    ["SpreadsheetBench", "Claude Code · Sonnet 4.6", "anthropic / openai / Ours", "70.2", "81.1 / 81.1 / 82.1", "59 / 63 / 66", "16 / 20 / 19", "**+43** / **+43** / **+47**"],
], num_from=3) + """

Only the three Claude Code SpreadsheetBench rows survive a Bonferroni correction over the 18 comparisons.
""")

S.append("""### 2.4 OfficeQA, 50 / 24 / 172 split

SkillOpt Table 1:

""" + skillopt_grid("OfficeQA") + """

WikiSkill Table 1:

""" + wikiskill_grid("OfficeQA") + """

WikiSkill Table 3 (Gemini-3.5-Flash): no skill 48.6 · inference-agent wiki only 51.0 · no wiki 55.2 · both 55.6 · proposer only (default) 60.7.

EvoSkill (Claude Code · Opus 4.5, its own split with 5–15% training): baseline 60.6 → 67.9 exact match (its Table 1 shows 68.1).
""")

S.append("""### 2.5 DocVQA

SkillOpt Table 1 (exact match, test 374):

""" + skillopt_grid("DocVQA") + """

Trace2Skill Table 3 (50 examples to evolve, 5,299 test; Acc = ANLS ≥ 0.5), converted to absolute:

""" + t2s_docvqa() + """

Trace2Skill, 122B, ANLS / Acc: sequential B=4 0.8674 / 90.80 · B=1 0.8694 / 91.05 · parallel 0.8833 / 92.52 (Table 10). ReasoningBank vs Trace2Skill Acc: 90.90 vs 92.52 (122B), 89.62 vs 92.00 (35B) (Table 11).
""")

S.append("""### 2.6 VAREX, VRDU, OfficeBench, TSBench

""" + table(["Result", "Score"], [
    ["Trace2Skill, VRDU Registration traces → VAREX Flat validation, Anthropic `pdf` base → evolved (Table 13)", "76.9 → 85.3"],
    ["Trace2Skill, TSBench training → deck-disjoint OOD, `pptx` base → evolved", "72.5 → 88.8"],
    ["Trace2Skill, generated tasks → OfficeBench DOCX holdout (64), `docx` base → evolved", "79.7 → 87.5"],
    ["VAREX paper, image input, field EM: Gemini 2.5 Pro / Flash", "98.0 / 97.3"],
    ["VAREX paper: Qwen3-VL 8B / GPT-4o / Qwen3-VL 2B", "96.6 / 94.8 / 34.2"],
    ["VAREX paper: plain text → spatial text input, gain", "+3 to +18 pp"],
    ["**Ours**, VAREX Flat 30 dev docs × 3 runs, Qwen3.7 Flash worker: none / full skill / MCRDR-reduced", "82.89 / 89.06 / 90.44"],
    ["**Ours**, perfect documents (of 90): none / full / reduced", "61 / 62 / 68"],
]) + "\n")

S.append("""### 2.7 SkillsBench

SkillsBench (Tables 2 and 6; 87 tasks, 18 agent-model configurations; selected rows):

""" + table(["Configuration", "No skill", "Self-generated", "Curated"], [
    ["Mean of 18 configurations", "33.9", "–", "50.5"],
    ["Claude Code · Opus 4.7", "43.0", "34.9", "61.2"],
    ["Codex · GPT-5.5", "46.8", "35.5", "66.5"],
    ["Gemini CLI · Gemini 3.1 Pro", "36.0", "24.5", "60.8"],
    ["OpenHands · GPT-5.4 Mini", "29.9", "–", "41.4"],
    ["OpenHands · DeepSeek V4 Flash", "27.5", "–", "44.7"],
]) + """

CoEvoSkills (85 tasks, Claude Code · Opus 4.6): no skill 30.6 · self-generated 32.0 · Anthropic skill-creator 34.1 · human-curated 53.5 · **CoEvoSkills 71.1** · without its surrogate verifier 41.1. GPT-5.2 Codex self-evolved 69.8 vs no skill 29.6. Haiku 4.5 using Opus-evolved skills 54.5 vs no skill 10.4.
""")

S.append("""### 2.8 LiveMathematicianBench

SkillOpt Table 1 (35 / 17 / 125):

""" + skillopt_grid("LiveMath") + """

WikiSkill Table 1 (35 / 18 / 124):

""" + wikiskill_grid("LiveMath") + """

SkillOpt Table 3 ablations (GPT-5.5): default 61.3 · no learning-rate bound 57.3 · no buffer 58.9 · no meta skill 58.1 · no meta and slow update 59.7 · one training example 59.1.
""")

S.append("""### 2.9 SearchQA

SkillOpt Table 1 (400 / 200 / 1,400):

""" + skillopt_grid("SearchQA") + """

SkillOpt Table 5, no skill → GPT-5.5 optimizer / target-matched optimizer: GPT-5.4-mini 75.9 → 80.2 / 78.3 · GPT-5.4-nano 55.8 → 74.8 / 69.9. Table 3 ablations (GPT-5.5): default 87.1 · no learning-rate bound 84.6 · no buffer 85.5 · no meta skill 85.1 · no meta and slow update 86.3 · one training example 81.0.
""")

S.append("""### 2.10 SealQA

WikiSkill Table 1 (Seal-0, 16 / 10 / 85):

""" + wikiskill_grid("SealQA") + """

EvoSkill (Claude Code · Opus 4.5, Seal-0, 111 questions): 26.6 → 38.7.
""")

S.append("""### 2.11 Math: DAPO, AIME, GPQA, Game of 24, Omni-MATH

Trace2Skill Table 2 (skills created from no skill; 400 DAPO to evolve, 100 held out; AIME 2026 avg@8 over 30), converted to absolute:

""" + t2s_math() + """

Trace2Skill Table 8, DAPO / AIME 2026: no skill 92.0 / 90.4 (122B), 89.0 / 83.3 (35B) · sequential B=4 93.0 / 91.7, 89.0 / 68.3 · B=1 94.0 / 85.8, 90.0 / 81.7 · parallel 95.0 / 91.7, 94.0 / 88.3. ReasoningBank: 94.0 / 90.8 (122B), 91.0 / 80.8 (35B) (Table 9).

""" + table(["Dynamic Cheatsheet (Claude 3.5 Sonnet unless noted)", "Baseline", "DC-Cumulative", "DC-Retrieval-Synthesis"], [
    ["AIME 2024", "23.3", "50.0", "46.7"],
    ["AIME 2025", "6.7", "36.7", "30.0"],
    ["GPQA-Diamond", "59.6", "61.1", "68.7"],
    ["Game of 24 (GPT-4o)", "10.0", "93.0", "99.0"],
]) + """

SkillOpt Table 4, OlympiadBench-trained skill evaluated on Omni-MATH, no skill → transferred: GPT-5.4 56.6 → 60.3 · mini 34.8 → 36.6 · nano 38.8 → 40.1.
""")

S.append("""### 2.12 ALFWorld

SkillOpt Table 1 (39 / 18 / 134; direct chat only):

""" + skillopt_grid("ALFWorld") + """

WikiSkill Table 1 (same split; Gemini-3.5-Flash scores 100% on validation, so nothing was evolved for it):

""" + wikiskill_grid("ALFWorld") + """

""" + table(["Other ALFWorld results", "Success"], [
    ["Memp (test): GPT-4o no memory → proceduralised memory", "42.14 → 77.86"],
    ["Memp: Claude 3.5 Sonnet / Qwen2.5-72B", "34.97 → 74.72 / 41.25 → 77.19"],
    ["SkillRL, Qwen2.5-7B: base / ReAct / Reflexion", "14.8 / 31.2 / 42.7"],
    ["SkillRL: ExpeL / MemP / Mem0 / GRPO", "46.3 / 41.4 / 33.6 / 77.6"],
    ["SkillRL with an o3 teacher", "89.9"],
    ["SkillGen, GPT-5.4-Nano, valid_unseen: base → skill", "72.94 → 94.90"],
]) + "\n")

S.append("""### 2.13 SOPBench Bank

No skill-method results yet. Base-model pass rates (SOPBench Table 3), Bank (153) / all domains (903): o4-mini-high 76.47 / 76.08 · GPT-4.1 71.89 / 67.22 · GPT-4.1-mini 62.75 / 47.07 · Qwen2.5-32B (ReAct) 41.83 / 39.65 · Qwen2.5-7B (ReAct) 5.88 / 11.30.

### 2.14 No skill-method results yet

TAT-DQA, FinanceBench, Kleister NDA / Charity, FUNSD, CORD, SROIE, DocILE, InfographicVQA,
ChartQA, SlideVQA and SheetCopilot have no published skill or memory-method results that
we found. On these, our runs would set the first skill baseline; report no skill, full
skill and MCRDR-selected skill side by side.
""")

S.append("""## 3. Learning cost

Cost of building the skill, only for benchmarks above. Figures marked *estimate* are ours.

""" + table(["Method · benchmark", "Reported figure", "Source"], [
    ["SkillOpt training tokens (GPT-5.5 student and optimizer)", "SearchQA 213.8M · Spreadsheet 21.4M · OfficeQA 20.8M · DocVQA 188.2M · LiveMath 23.2M · ALFWorld 59.3M", "SkillOpt Table 6"],
    ["SkillOpt tokens per test-point gained", "Spreadsheet 0.6M · OfficeQA 1.1M · LiveMath 3.6M · ALFWorld 15.9M · SearchQA 37.9M · DocVQA 46.4M", "SkillOpt Table 6"],
    ["SkillOpt accepted edits", "1–4 per benchmark (median 2.5)", "SkillOpt Table 6"],
    ["*Estimate:* SkillOpt dollars at GPT-5.5 list price ($5 in / $30 out per M)", "Spreadsheet $107–161 · OfficeQA $104–156 · LiveMath $116–174 · ALFWorld $297–445 · DocVQA $941–1,412 · SearchQA $1,069–1,604", "Table 6 tokens × $5–7.5 per M"],
    ["*Estimate:* SkillOpt runs on SpreadsheetBench 80/40/280", "≈750 target-model rollouts and ≈80–170 optimizer calls", "From its default config"],
    ["Trace2Skill consolidation wall-clock (8×A100), parallel / seq B=4 / seq B=1", "Spreadsheet ~3 / 15 / 60 min · math 2.0 / 3.8 / 25.9 min · DocVQA 4.8 / 5.2 / 35.9 min", "T2S Tables 4, 8, 10"],
    ["Trace2Skill whole project", "~20,000 A100 GPU-hours (all experiments)", "T2S App. A"],
    ["Trace2Skill sequential depth", "≈3 LLM rounds (128 parallel analysts, merge batch 32)", "T2S §B.1"],
    ["*Estimate:* Trace2Skill per spreadsheet skill", "≈200 rollouts + ≈200 analyst runs + ≈12 merges; no validation rollouts. With BO selection +700–1,700 rollouts", "T2S §2, App. E"],
    ["WikiSkill optimizer calls per iteration (N training tasks, batch B)", "WikiSkill 1 + T_ReAct (T ≈ 10–20) · EvoSkill 2N/B · SkillOpt K·N/B (K ≈ 6–8) · Trace2Skill N + (1 + 1/(c−1))·N/B + 1", "WikiSkill App. D.2"],
    ["*Estimate:* same, SpreadsheetBench N = 80", "WikiSkill ≈11–21 · SkillOpt ≈12–16 · Trace2Skill ≈84 per iteration, plus 80 training + 40 validation rollouts; up to 8 iterations", "Formulas above"],
    ["CoEvoSkills on SkillsBench", "4.1 verification cycles per task; 3,000 s evolution timeout per task", "CoEvoSkills App. B"],
    ["SkillRL on ALFWorld", "~30 h on 8×H100 per experiment (24 h of RL)", "SkillRL App. D.2"],
    ["SkillGen (average over its benchmarks, incl. ALFWorld)", "5.6M tokens per skill (2.2–10.2M), ~$8.2 at GPT-5.4-Mini prices", "SkillGen Table 4"],
    ["EvoSkill, Memp, Dynamic Cheatsheet, Regression Tax", "No build-cost figures reported", "–"],
    ["**Ours**, per failed document", "1 worker run + 1 inspector run (with re-scores) + judge calls per rule + checker re-runs of matching cornerstones", "`docs/verifier-learning.md`"],
]) + "\n")

S.append("""## 4. Inference cost

Cost of using the skill on each test task.

""" + table(["Method · benchmark", "Reported figure", "Source"], [
    ["SkillOpt deployed skill", "300–2,000 tokens added; no extra model calls", "SkillOpt §4.4"],
    ["SkillOpt skill size, initial → learned (words)", "Spreadsheet 224 → 1,918 · OfficeQA 145 → 916 · DocVQA 81 → 306 · ALFWorld 516 → 2,034 · LiveMath 154 → 479 · SearchQA 16 → 1,507", "`../SkillOpt/skills/`"],
    ["Trace2Skill released xlsx skills, SKILL.md / all .md files (words)", "Anthropic xlsx 1,481 · 122B-combined 6,270 / 11,173 · 35B-combined 3,122 · xlsx-122B (created) 3,053 / 4,795 · xlsx-35B (created) 778 / 1,521", "`../Trace2Skill/skills/`"],
    ["WikiSkill final skills", "SpreadSheet 1.4 skills, 142.5 lines · OfficeQA 1.8, 102.9 · ALFWorld 1.6, 93.5 · LiveMath 1.1, 84.6", "WikiSkill Table 4"],
    ["SkillsBench curated vs no skill", "Claude Code Opus 4.7 $5.21 → $6.74 (4.33M → 6.43M tokens) · Codex GPT-5.5 $3.08 → $2.99 · Gemini CLI 1.17M → 1.93M tokens", "SkillsBench Table 15"],
    ["SkillsBench runtime", "OpenHands + Opus 4.7: 10.0 → 6.5 min per task with curated skills", "SkillsBench App. L.4"],
    ["Dynamic Cheatsheet, AIME 2024", "370 tokens per answer (baseline) → 1,035 (DC-RS) → 1,831 (DC-Cu)", "DC §5"],
    ["Memp, ALFWorld steps (GPT-4o)", "23.76 → 15.01", "Memp Table 1"],
    ["**Ours**, VAREX pilot median input tokens per episode", "none 18,828 · full skill 51,094 · MCRDR-reduced 43,238 (−15.4% vs full)", "Pilot deck v2"],
    ["**Ours**, mean input tokens per episode", "49.8k · 76.9k · 77.4k; about $0.02 per episode on OpenRouter", "Pilot deck v2"],
]) + "\n")

S.append("""## 5. Transfer and robustness

**Cross-model transfer, WikiSkill Table 2** (skill source → inference model; SpreadSheet / OfficeQA):

""" + table(["Inference model", "Skill source: none / self / others", "SpreadSheet", "OfficeQA"], [
    ["Qwen-3.5-4B", "none / self / Qwen-3.6-27B / Gemini-Flash", "14.6 / 21.1 / 33.0 / 23.0", "30.2 / 28.5 / 25.4 / 32.2"],
    ["Qwen-3.5-9B", "none / self / Qwen-3.5-4B / Qwen-3.6-27B / Gemini-Flash", "24.3 / 33.6 / 25.0 / 50.5 / 48.8", "35.9 / 40.5 / 40.3 / 39.9 / 40.5"],
    ["Qwen-3.6-27B", "none / self / Qwen-3.5-4B / Gemini-Flash", "40.8 / 81.7 / 40.6 / 76.0", "42.1 / 53.7 / 52.9 / 52.5"],
    ["Gemma-4-31B", "none / self / Qwen-3.5-4B / Qwen-3.6-27B / Gemini-Flash", "48.3 / 68.0 / 37.1 / 72.0 / 68.8", "43.3 / 44.2 / 42.1 / 44.2 / 43.4"],
    ["Gemini-3.5-Flash", "none / self / Qwen-3.5-4B / Qwen-3.6-27B", "50.5 / 76.6 / 18.1 / 63.4", "48.6 / 60.7 / 48.5 / 47.7"],
], num_from=2) + """

**Transfer, SkillOpt Table 4** (no skill / in-domain / transferred):

""" + table(["Shift", "No skill", "In-domain", "Transferred"], [
    ["Spreadsheet, GPT-5.4 skill → 5.4-mini", "36.1", "47.5", "45.5"],
    ["Spreadsheet, GPT-5.4 skill → 5.4-nano", "23.5", "42.5", "26.5"],
    ["LiveMath, GPT-5.4 skill → 5.4-mini", "14.7", "32.8", "19.2"],
    ["LiveMath, GPT-5.4 skill → 5.4-nano", "23.2", "27.2", "28.8"],
    ["Spreadsheet, Codex → Claude Code (GPT-5.5)", "22.1", "80.4", "81.8"],
    ["Spreadsheet, Claude Code → Codex", "27.5", "85.0", "71.1"],
    ["LiveMath, Codex → Claude Code", "40.8", "56.5", "42.4"],
    ["LiveMath, Claude Code → Codex", "35.2", "78.4", "48.0"],
]) + """

**Trace2Skill transfer:** Table 1 (§2.2) already crosses authors and users (122B ↔ 35B) and
tests out of distribution on WikiTQ and HiTab. A GPT-5.5 user running the Qwen-122B-authored
skill scores 78.5 (no skill 32.5); GPT-5.5 authoring from Qwen traces lifts the Qwen-122B user
from 27.7 to 68.0 (Fig. 6). EvoSkill moves from 33.5 to 59.5 on the same benchmark just by
changing harness (Fig. 7).

**Regressions (items that passed before and fail after):**

""" + table(["Source", "Finding"], [
    ["Regression Tax", "553 gains vs 324 regressions over 18 conditions: regressions cancel 59% of gains (OfficeQA-Pro 66%, SpreadsheetBench 56%). Every condition breaks at least 2 tasks. OfficeQA-Pro regressions: 73% grounding displacement, 17% skill-description osmosis"],
    ["SkillsBench", "13 of 87 tasks get worse with curated skills. Compact skills average +19.0 pp, comprehensive ones +0.7 pp"],
    ["Trace2Skill Table 1", "Success-only patches can hurt OOD: 122B Deepening +Success drops WikiTQ by 10.35. 35B-authored Creation skills cut the 122B user's Soft score by up to 7.70"],
    ["WikiSkill Table 2", "Weak-model skills can harm strong models: Qwen-3.5-4B spreadsheet skills take Gemini-Flash from 50.5 to 18.1"],
    ["**Ours**", "First learning run: the checker refused the only proposed rule because its condition also matched 7 of 8 passing documents"],
]) + """

**Run-to-run variance:**

""" + table(["Source", "Protocol", "Spread"], [
    ["Trace2Skill", "3 seeds", "SD of Avg ≤ 3.4 (Table 7)"],
    ["WikiSkill", "3 full evolution runs; ties by paired bootstrap, p < 0.05", "Not tabulated per cell"],
    ["SkillOpt", "One split (seed 42)", "Not reported in the tables used here"],
    ["Regression Tax", "One run per condition", "Not estimated"],
    ["**Ours**, VAREX pilot", "3 runs × 30 documents", "Reduced − full per run: +7.55 / −1.51 / −1.89 pp (pooled +1.38)"],
]) + "\n")

S.append("""## 6. Data efficiency

Training data each method used to reach the scores above.

""" + table(["Method · benchmark", "Training data used"], [
    ["Trace2Skill, SpreadsheetBench", "200 evolve tasks, 1 trajectory each (~70 error lessons; 323 patches in the 122B Combined run); no validation set (32 tasks only for BO selection)"],
    ["Trace2Skill, DAPO / DocVQA", "400 problems / 50 examples"],
    ["Trace2Skill, PDF / PPTX / DOCX", "VRDU Registration traces / TSBench training decks / generated DOCX tasks"],
    ["SkillOpt (train / selection)", "SpreadsheetBench 80 / 40 · OfficeQA 50 / 24 · DocVQA 107 / 53 · LiveMath 35 / 17 · SearchQA 400 / 200 · ALFWorld 39 / 18"],
    ["SkillOpt, one training example (GPT-5.5)", "SearchQA 81.0 vs 87.1 · Spreadsheet 47.5 vs 77.5 · LiveMath 59.1 vs 61.3 (Table 2a)"],
    ["WikiSkill (train / val)", "LiveMath 35 / 18 · SealQA 16 / 10 · SpreadSheet 80 / 40 · OfficeQA 50 / 24 · ALFWorld 39 / 18; full-batch, up to 8 iterations; 1.1–1.8 skills kept of 1.9–4.7 proposed"],
    ["EvoSkill, OfficeQA / SealQA", "5–15% of the data for training"],
    ["Dynamic Cheatsheet", "No training set: the cheatsheet is built online, question by question, during the test pass"],
    ["CoEvoSkills, SkillsBench", "Per-task evolution with a surrogate verifier (4.1 cycles per task)"],
    ["**Ours**, VAREX", "Pilot 30 dev documents; first learning run 10 held-out documents (2 failures, 2 rules proposed, 0 kept)"],
]) + "\n")

S.append("""## 7. Caveats

- **Same name, different split.** SpreadsheetBench-Verified is 200/200 in Trace2Skill, 80/40/280 in
  SkillOpt and WikiSkill, and 392 tasks in Regression Tax. DocVQA tests 5,299 items in Trace2Skill
  and 374 in SkillOpt. Compare scores only within one block.
- **Baselines are re-implemented.** SkillOpt's and WikiSkill's Trace2Skill numbers are their own
  rebuilds (with a validation gate and their starting skills), not Trace2Skill's released method.
- **Different starting skills.** Anthropic's skills (Trace2Skill), per-benchmark initial skills
  (SkillOpt), empty (WikiSkill), contributor skills (SkillsBench).
- **SkillOpt ablation defaults differ from Table 1.** Its Table 3 "default" row (SearchQA 87.1, Spreadsheet
  77.5, LiveMath 61.3) does not equal the Table 1 SkillOpt scores for GPT-5.5 (87.3, 80.7, 66.9).
- **Internal inconsistencies.** Trace2Skill's no-skill DAPO score is 91.0 in Table 2 and 92.0 in
  Tables 8–9. EvoSkill's OfficeQA result is 68.1 in its table and 67.9 in its text.
- **Unreleased pieces** (exact reproduction impossible): Trace2Skill's WikiTQ/HiTab conversions,
  DAPO IDs and `xlsx-basic` seed; WikiSkill's code and its LiveMath and SealQA splits.
""")

OUT.write_text("\n".join(S), encoding="utf-8")
print("wrote", OUT, len("\n".join(S)), "chars")
