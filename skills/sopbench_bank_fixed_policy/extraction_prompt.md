# Bank policy extraction constraints

The input is the official SOPBench Bank agent policy. Preserve it verbatim via source ranges.

- Make exactly one rule for each of the 23 explicitly labeled `* action_name:` blocks under `### Actions with Constraints:`. Use the action name as the rule ID.
- Each rule has exactly one predicate: `needed_actions contains action_name`.
- Each rule's source contains its action label and the entire procedure below it, ending before the next action or section heading. Never split eligibility bullets into separate rules. Keep all nested AND/OR/CHAIN conditions, parameter thresholds, rejection guidance, and ordering together.
- These predicates select which procedure to read, NOT whether the customer is eligible. A rejected request still needs the full applicable procedure.
- Do not include another action's procedure inside a rule. Multiple actions are selected by putting multiple values in `needed_actions`, including helper procedures when needed. Do not invent automatic dependency expansion.
- Put the role description, access-control instructions, core operating principles, condition-interpretation instructions, and internal verification function list in `root`. Exclude only structural headings and blank separators as needed.
- Return rules in source order. Account for every nonblank source line using the existing response schema; do not add response fields.
