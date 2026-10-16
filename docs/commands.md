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
