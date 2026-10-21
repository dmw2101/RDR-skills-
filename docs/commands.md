# PowerShell command guide

Commands below assume **Windows PowerShell 5.1**, Python 3.12, and that PowerShell is already open at the `RDR-skills` repository root. Each command is a single line. Do not add `cd`, `Set-Location`, or a trailing `\`.

## 1. Build a new tree from a skill

This is the full Markdown-to-tree path. The first command asks Gemini to extract the feature schema-compatible rules from `initial.md`; the second compiles those rules into fragments and a tree. Extraction uses the model; compilation is local and offline.

For a skill other than the supplied examples, first add its feature names and allowed values to `skills/feature_schemas.json`. The extractor uses that schema; it does not invent one automatically.

Mark a feature with `"multiple": true` when a query can select several values. Cases then supply a list, and Gemini generates `contains` conditions. Features without this flag keep single values and `eq`/`in` conditions. ALFWorld's `task_type` supports lists.

Install the optional compiler dependency once:

```powershell
py -3.12 -m pip install -e ".[skill-compiler]"
```

Set your Gemini key once per PowerShell session (replace the placeholder):

```powershell
$env:GEMINI_API_KEY = "YOUR_GEMINI_API_KEY"
```

Choose the skill, model, and a fresh filename for the model's rule output:

```powershell
$skillDir = "skills/alfworld"
$model = "gemini-3.7-flash"
$irPath = "$skillDir/compiled/ir.$(Get-Date -Format 'yyyyMMdd-HHmmssfff').json"
```

Extract rules from the skill's complete Markdown file:

```powershell
py -3.12 -m rdr_engine.skill_compiler "$skillDir/initial.md" --features skills/feature_schemas.json --model $model --output $irPath
```

This saves the extracted rule list to the timestamped `$irPath`. It does **not** build the tree yet. `--model` selects the Gemini model that is actually called.

Compile the extracted rules into the tree and skill fragments:

```powershell
py -3.12 -m rdr_engine.skill_tool compile $skillDir --ir $irPath --features skills/feature_schemas.json --model-id $model --force
```

This writes/replaces the compiled files under `skills/alfworld/compiled/`. `--model-id` records a label in the manifest; it does not make an API call. Keep `$skillDir`, `$model`, and `$irPath` in the same PowerShell session for both steps. `--force` replaces the existing compiled bundle.

The compiler builds a flat MCRDR tree: node 0 is the empty root default, node 1 holds the general guidance, and each conditional rule is another child of the root. There is no limit on the number of rules.

Compiled bundles from before the MCRDR change (artifact schema 1) no longer load; rerun the compile command with `--force` to rebuild them from their `ir.json`.

## 2. Query an existing tree with a task type

To see the ALFWorld skill selected for the `heat_place` task type, run:

```powershell
py -3.12 -m rdr_engine.skill_tool render skills/alfworld --case-file skills/alfworld/cases/heat_place.json --show-path
```

The case file contains the task type you are giving the tree:

```json
{"task_type": ["heat_place"]}
```

This reads the existing compiled tree, evaluates it using that `task_type`, and prints the selected Markdown skill. `--show-path` also prints, to the error stream:

- `case=`: the feature values used;
- `trace=`: every evaluated node as an indented outline of `id:true/false`;
- `terminals=`: the nodes whose conclusions were reported;
- `fragments=`: the selected fragment IDs.

Remove `--show-path` to print just the Markdown. Querying does not call Gemini and does not rebuild the tree.

The renderer includes shared guidance (node 1) plus the fragments of every matching task-specific rule. So most of the output can be the same across task types; look at the task-specific section and, if needed, the terminals and fragment IDs printed by `--show-path`.

To use another ALFWorld type, open the case file:

```powershell
notepad skills/alfworld/cases/heat_place.json
```

Replace the list with any combination of `pick_place`, `pick_two_place`, `examine_in_light`, `clean_place`, `heat_place`, and `cool_place`, save, and run the render command again. Use `[]` when no category is known. Labels must match the feature schema exactly. `heat_place` already includes picking and placement; those steps do not require another `pick_place` label.

## 3. Query an existing tree with a natural-language task

For an ALFWorld request, pass the task as ordinary quoted text:

```powershell
py -3.12 -m rdr_engine.skill_tool render skills/alfworld --task "Heat an apple and put it in a bowl" --show-path
```

This maps the request to `task_type=["heat_place"]`, traverses the existing tree, and prints the selected Markdown. Requests containing several recognized goal categories retain all their labels.

Like ALFWorld, this uses word-pattern heuristics and runs offline. All recognized operation categories are retained; an unrecognized request produces `[]` and general guidance. Use a case file for precise feature values. The renderer preserves the original skill's source order; it does not build an execution plan from the query. Remove `--show-path` to print only the selected Markdown.

## 4. View a tree as an interactive HTML diagram

Generate a readable view of the ALFWorld tree:

```powershell
py -3.12 skills/visualise_tree.py skills/alfworld/compiled/tree.json
```

Open the generated page in your default browser:

```powershell
Start-Process skills/alfworld/compiled/tree_visualisation.html
```

The script automatically loads the matching `fragments.json` and `manifest.json`. Click a node to inspect its condition, fragment text, route, and children. Children are drawn in evaluation order; each is tested only when its parent fires, and stopping rules are outlined in red. Use **Hide unreachable** to collapse nodes that cannot match the feature schema. For another skill, replace `skills/alfworld` with its folder, for example `skills/pdf`.

## 5. Convert the six SkillOpt GPT-5.5 optimized skills

The six optimized skill checkpoints from the pinned SkillOpt snapshot (`benchmarks/skillopt/ckpt/*/gpt5.5_skill.md`, MIT license) are set up as skill folders, each with a byte-identical `initial.md` and a `source.json` recording its origin and SHA-256:

| Folder | Benchmark | Feature (several values may hold together) |
|---|---|---|
| `skills/alfworld_gpt55` | ALFWorld | `task_type` (as in `alfworld`), plus `target_object` (one value) |
| `skills/docvqa_gpt55` | DocVQA | `question_cues` |
| `skills/livemath_gpt55` | LiveMathematicianBench | `option_cues` |
| `skills/officeqa_gpt55` | OfficeQA | `question_cues` |
| `skills/searchqa_gpt55` | SearchQA | `clue_cues` |
| `skills/spreadsheetbench_gpt55` | SpreadsheetBench | `operation` |

Their feature names, values and descriptions are in `skills/feature_schemas.json`. Gemini may only use those values in rule conditions.

One command converts all six. Set your Gemini key in the current PowerShell session first (replace the placeholder):

```powershell
$env:GEMINI_API_KEY = "YOUR_GEMINI_API_KEY"
```

```powershell
py -3.12 scripts/convert_skills.py
```

For each skill this:

1. asks Gemini for the rule list. That is one extraction plus at most two repair calls, saved as a new `compiled/ir.<timestamp>.json`.
2. compiles it offline into `compiled/{ir,fragments,tree,manifest}.json`.
3. writes the tree view `compiled/tree_visualisation.html`.

It finishes with a table of rules, tree nodes and fragments per skill. One failing skill does not stop the others; rerunning skips skills that are already compiled.

Useful options:

| Option | Effect |
|---|---|
| `--skills docvqa_gpt55 officeqa_gpt55` | Convert only these folders |
| `--model gemini-3.8-flash` | Gemini model for extraction (this is the default, as used for `alfworld` and `pdf`) |
| `--force` | Rebuild skills that are already compiled (asks Gemini again) |
| `--compile-only` | Rebuild from the newest saved `compiled/ir.<timestamp>.json` without calling Gemini, for example after reviewing a rule list |
| `--dry-run` | Print every step without running anything |

Review each new `compiled/ir.json` before relying on it: it lists which source lines Gemini made general guidance, which it made conditional (and on what), and which it excluded. Then render a skill for a case file or a task:

```powershell
py -3.12 -m rdr_engine.skill_tool render skills/officeqa_gpt55 --case-file skills/officeqa_gpt55/cases/statistics_over_range.json --show-path
py -3.12 -m rdr_engine.skill_tool render skills/searchqa_gpt55 --task "Crossword clue: this lake in Florida (10)" --show-path
```

Each folder has two or three example case files under `cases/`. With `--task`, conservative word-pattern heuristics in `rdr_engine/skill_cases.py` pick the feature values; `--show-path` prints the values they chose. Open a tree view with:

```powershell
Start-Process skills/officeqa_gpt55/compiled/tree_visualisation.html
```

Example tasks for `--task` demonstrations:

| Folder | Task |
|---|---|
| `alfworld_gpt55` | `"clean some kettle and put it in cabinet"` |
| `docvqa_gpt55` | `"What is the total amount in the table?"` |
| `livemath_gpt55` | `"Which holds for every n? B) One of the remaining options is correct, but a stronger result can be proven."` |
| `officeqa_gpt55` | `"What was the standard deviation of monthly receipts from 1950 to 1955?"` |
| `searchqa_gpt55` | `"This man wrote 'The Raven'"` |
| `spreadsheetbench_gpt55` | `"Write a SUMIFS formula to total sales by month"` |

Source quirk: in `alfworld_gpt55/initial.md`, a `### Pick Two Object Bookkeeping` subsection is pasted inside the task-type table (lines 20–27). The table rows after it therefore have no header and are kept as plain lines; the source is not edited.
