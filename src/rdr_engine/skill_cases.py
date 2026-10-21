"""Evaluate typed skill predicates and derive conservative pre-execution cases."""

from __future__ import annotations

import re
from typing import Any, Mapping

from .skill_compiler import FeatureSchema


FeatureValue = str | list[str]


class SkillConditionEvaluator:
    def __init__(self, feature_schema: FeatureSchema) -> None:
        self.feature_schema = feature_schema

    def validate_case(self, case: Any, required_fields: set[str] | None = None) -> None:
        if not isinstance(case, dict):
            raise ValueError("a skill case must be a JSON object")
        required = required_fields if required_fields is not None else set(self.feature_schema.features)
        for field in required:
            spec = self.feature_schema.features.get(field)
            if spec is None:
                raise ValueError(f"unknown case feature {field!r}")
            if field not in case:
                raise ValueError(f"missing required case feature {field!r}")
            value = case[field]
            if spec.multiple:
                if not isinstance(value, list) or any(
                    not isinstance(item, str) or item not in spec.values for item in value
                ):
                    raise ValueError(f"invalid value for {field!r}; expected a list from {spec.values}")
                if len(value) != len(set(value)):
                    raise ValueError(f"duplicate values for {field!r}")
            elif not isinstance(value, str) or value not in spec.values:
                raise ValueError(f"invalid value for {field!r}; allowed: {spec.values}")

    def evaluate(self, condition: Any, case: Any) -> bool:
        if not isinstance(condition, dict) or set(condition) != {"field", "op", "value"}:
            raise ValueError("invalid skill predicate")
        field, op, value = condition["field"], condition["op"], condition["value"]
        if not isinstance(field, str) or field not in self.feature_schema.features:
            raise ValueError(f"unknown predicate feature {field!r}")
        self.validate_case(case, {field})
        spec = self.feature_schema.features[field]
        allowed = spec.values
        if spec.multiple:
            if op == "contains" and isinstance(value, str) and value in allowed:
                return value in case[field]
            raise ValueError(f"invalid predicate for {field!r}; use contains for a multiple-value feature")
        if op == "eq" and isinstance(value, str) and value in allowed:
            return case[field] == value
        if op == "in" and isinstance(value, list) and value and all(
            isinstance(item, str) and item in allowed for item in value
        ):
            return case[field] in value
        raise ValueError(f"invalid predicate for {field!r}")


def _alfworld_task_types(task: str) -> list[str]:
    cues = {
        "examine_in_light": r"\b(examine|inspect)\b",
        "clean_place": r"\b(clean|wash|washed)\b",
        "heat_place": r"\b(heat|hot|warm)\b",
        "cool_place": r"\b(cool|cold|chill)\b",
        "pick_two_place": r"\b(two|2)\b",
    }
    matched = [name for name, pattern in cues.items() if re.search(pattern, task, re.I)]
    if matched:
        return matched
    return ["pick_place"] if re.search(r"\b(put|place|move)\b", task, re.I) else []


def build_case(
    skill_id: str, task: str, visible_inputs: Mapping[str, FeatureValue] | None,
    feature_schema: FeatureSchema,
) -> dict[str, FeatureValue]:
    """Use only the task and explicitly visible pre-execution feature values.

    Heuristics provide a convenient demo case. Pass an explicit feature value in
    visible_inputs when the task wording is ambiguous or inputs need inspection.
    """
    if not isinstance(task, str):
        raise ValueError("task must be text")
    case: dict[str, FeatureValue] = {
        field: [] if spec.multiple else "unknown"
        for field, spec in feature_schema.features.items()
        if spec.multiple or "unknown" in spec.values
    }
    if skill_id == "alfworld":
        case["task_type"] = _alfworld_task_types(task)
    elif skill_id == "livemathematicianbench":
        case["has_quantifier"] = (
            "yes" if re.search(r"\b(all|every|each|exists|there is|some|any)\b", task, re.I)
            else "no"
        )
    elif skill_id == "spreadsheetbench":
        if re.search(r"\b(aggregate|aggregation|sort|group|filter|merge|transform|bulk)\b", task, re.I):
            case["operation"] = "bulk_transform"
        elif re.search(r"\b(cell|range|value)\b", task, re.I):
            case["operation"] = "simple_cell_edit"
        else:
            case["operation"] = "other" if task.strip() else "unknown"
        if re.search(r"\bpreserve\b.{0,25}\bformatting\b", task, re.I):
            case["preserve_formatting"] = "yes"

    if skill_id == "pdf":
        case.update(_pdf_case(task))
    elif skill_id == "alfworld_gpt55":
        case["task_type"] = _alfworld_task_types(task)
        case["target_object"] = _alfworld_target_object(task)
    elif skill_id in CUE_FEATURES:
        field, cues = CUE_FEATURES[skill_id]
        case[field] = _matching_cues(task, cues)

    # Older bundles omitted `multiple` and still expect one scalar value.
    for field, value in list(case.items()):
        spec = feature_schema.features[field]
        if spec.multiple and isinstance(value, str):
            case[field] = [] if value == "unknown" else [value]
        elif not spec.multiple and isinstance(value, list):
            case[field] = value[0] if len(value) == 1 else "unknown"

    for field, value in (visible_inputs or {}).items():
        if field not in feature_schema.features:
            raise ValueError(f"unknown visible feature {field!r}")
        case[field] = value
    SkillConditionEvaluator(feature_schema).validate_case(case)
    return case


def _pdf_case(task: str) -> dict[str, FeatureValue]:
    """Infer all matching PDF operations and explicit source-type cues."""
    cues = {
        "extract_content": (
            r"\b(extract(?:ing|ion)?|read(?:ing)?|ocr)\b"
            r"|\bmake\b.{0,40}\bsearchable\b"
        ),
        "combine_or_split": r"\b(merg(?:e|ing)|combin(?:e|ing)|split(?:ting)?|separat(?:e|ing))\b",
        "rotate_pages": r"\b(rotat(?:e|ing|ion)|reorient)\b",
        "create_or_watermark": r"\b(creat(?:e|ing)|generat(?:e|ing)|watermark(?:s|ing)?)\b",
        "fill_forms": r"\b(fill(?:ing)?|complete)\b.{0,40}\b(forms?|fields?)\b",
        "password_security": (
            r"\b(encrypt(?:ing|ion)?|decrypt(?:ing|ion)?|unlock)\b"
            r"|\bpassword[- ]protect\b"
            r"|\b(add|remove|set|change)\b.{0,40}\bpassword\b"
        ),
    }
    matched = [name for name, pattern in cues.items() if re.search(pattern, task, re.I)]

    scanned = bool(re.search(r"\b(scanned|image[- ](?:only|based)|ocr)\b", task, re.I))
    text_based = bool(re.search(
        r"\b(text[- ]based|born[- ]digital|digitally[- ]generated|selectable text)\b", task, re.I
    ))
    if re.search(r"\bmixed\b", task, re.I) or (scanned and text_based):
        source_kind = "mixed"
    elif scanned:
        source_kind = "scanned_image"
    elif text_based:
        source_kind = "text_based"
    else:
        source_kind = "unknown"
    return {"operation": matched, "source_kind": source_kind}


def _matching_cues(task: str, cues: Mapping[str, str]) -> list[str]:
    """All cue labels whose pattern occurs in the visible task, in schema order."""
    return [name for name, pattern in cues.items() if re.search(pattern, task, re.I)]


# Conservative wording cues for the SkillOpt GPT-5.5 optimized skills. Each table
# lists every value of that skill's one multiple-value feature, in schema order.
_DOCVQA_CUES = {
    "value_or_count": (
        r"\bhow (many|much)\b|\bnumber of\b|\bwhen\b|\bwhich (year|date|day|month)\b"
        r"|\b(amount|total|value|date|year|time|percent(age)?|price|cost|count|page( number)?|graph|chart|plot)s?\b"
    ),
    "name_or_quote": r"\b(who|whose|name[sd]?|title|company|organi[sz]ation|person)\b|[\"“][^\"”]{2,}[\"”]",
    "table_lookup": r"\btables?\b(?! of contents)|\b(rows?|columns?|headers?|cells?)\b",
    "form_or_label": r"\b(forms?|receipts?|invoices?|fields?|label(ed|led)?|sender|recipient|payee|addressed to|signed by|party|role)\b",
    "list_or_index": r"\b(table of contents|contents|index|bullet(ed)?|list(ed)?|point (number|no\.?)|item (number|no\.?))\b",
    "handwriting_or_anchor": r"\b(hand-?written|handwriting|signature|margin|written (next to|beside|near|below|above))\b",
}

_LIVEMATH_CUES = {
    "stronger_result_option": r"stronger (result|statement|conclusion)|remaining options",
    "nested_strength": (
        r"\b(strongest|stronger|weaker|weakest|sharp(er|est)?|optimal (constant|rate|bound|exponent)"
        r"|best (possible )?(constant|rate|bound|exponent)|blow-?ups?|unbounded)\b"
    ),
    "quantifier_or_equivalence": (
        r"\b(for (every|all|each|any)|there exists?|if and only if|iff|exactly when|necessary and sufficient"
        r"|converse(ly)?|equivalen(t|ce)|characteri[sz]\w*)\b|[∀∃⟺⇔]|\\(forall|exists|iff)\b"
    ),
    "threshold_or_inequality": (
        r"<=|>=|[≤≥<>]|\\(le|ge|leq|geq|lt|gt)\b|\bnon-?strict\b|\bstrict(ly)? (inequality|less|greater)\b"
        r"|\bthresholds?\b|\bendpoints?\b|\bcritical (value|exponent|parameter)\b"
    ),
    "quantitative_estimate": (
        r"\b[Oo]\(|\b(estimates?|asymptotic(ally)?|exponents?|logarithm(ic)?|log factors?|rate of convergence"
        r"|uniform(ly)? convergen\w*|pointwise)\b"
    ),
    "local_vs_global": (
        r"\b(local(ly)?|locali[sz]ation|global(ly)?|complet(ed|ion)|at each prime|eventually"
        r"|sufficiently (large|small))\b"
    ),
}

_OFFICEQA_CUES = {
    "external_time_series": (
        r"\b(FRED|time series|series (code|id)|actual (value|rate|observation)|official (rate|series|data)"
        r"|published (rate|value)|Federal Reserve|BLS|CPI|consumer price index|unemployment rate)\b"
    ),
    "period_range": (
        r"\b(1[89]\d\d|20\d\d)\s*(-|–|to|through|and)\s*(1[89]\d\d|20\d\d)\b"
        r"|\b(between|from)\b.{0,40}\b(to|through|until|and)\b.{0,25}\b(1[89]\d\d|20\d\d)\b"
        r"|\b(each|every) (month|quarter|year)\b|\b(monthly|quarterly|inclusive)\b|\bfiscal years\b"
    ),
    "unit_or_currency_conversion": (
        r"\b(convert(ed|ing)?|exchange rates?|currenc(y|ies)|(in|of) (thousands|millions|billions)"
        r"|per (U\.?S\.? )?dollar|euros?|yen|francs?|sterling)\b"
    ),
    "treasury_financing": (
        r"\b(offerings?|offered|auction(s|ed)?|tenders?|subscriptions?|refunding|new cash|allot(ted|ments?))\b"
    ),
    "derived_comparison": (
        r"\b(change|difference|differ(ed|s)?|gap|share|ratio|proportion|percent(age)? (of|change)"
        r"|increase[ds]?|decrease[ds]?|higher|lower|more than|less than)\b"
    ),
    "statistical_calculation": (
        r"\b(standard deviation|variance|z-?scores?|regression|correlation|growth rate|CAGR"
        r"|compound(ed)? (annual )?growth|median|mean|average|smooth(ed|ing)?|forecast\w*|trend|moving average)\b"
    ),
    "multi_stage_lookup": (
        r"\bin the (month|year|quarter|period) (when|in which|with)\b|\bthe (year|month|quarter) (that|when|in which)\b"
        r"|\bfor that (year|month|quarter|period|entity)\b|\bthen (find|use|report|compute)\b"
    ),
    "security_quotation": r"\b(quot(e|es|ed|ations?)|bid|asked|yields?|coupon|32nds?|price of the (bond|note|bill))\b",
}

_SEARCHQA_CUES = {
    "this_type_clue": r"\b(this|these)\s+[a-z]+|\b(he|she|his|her|him)\b",
    "crossword_or_letter_count": r"\(\d+\)|\b\d+[- ]letters?\b|\bcrossword\b|, or what\b",
    "quotation_or_lyric": (
        r"[\"“][^\"”]{3,}[\"”]|\b(lyrics?|poem|nursery rhyme|rhymes?|slogan|quotation|quote[sd]?|motto)\b"
    ),
    "creative_work": (
        r"\b(book|novel|film|movie|play|song|album|poem|show|sitcom|opera|musical|painting|author"
        r"|wrote|written|directed|starred|stars|played|character|set in)s?\b"
    ),
    "examples_given": r"\b(include[sd]?|including|such as|e\.g\.)\b|\b\w+, \w+(, \w+)+\b|\b\w+ / \w+\b",
    "name_or_word_meaning": (
        r"\b(means?|meaning|named after|name comes from|word for|derived from|etymolog\w*"
        r"|from the (Latin|Greek|French|German|Spanish|Arabic|Hebrew))\b"
    ),
    "geographic_feature": (
        r"\b(lakes?|rivers?|bays?|mount(ain)?s?|mt\.?|islands?|gorges?|canyons?|peninsulas?|straits?"
        r"|seas?|oceans?|deserts?|volcano(es)?|falls|valleys?)\b"
    ),
    "company_or_brand": r"\b(company|companies|corporations?|brands?|firms?|makers?|manufacturers?|founded|inc\.?|corp\.?|logos?)\b",
    "person_answer": (
        r"\bthis (man|woman|president|king|queen|author|actor|actress|singer|artist|poet|composer|scientist"
        r"|inventor|explorer|leader|saint|pope|general|emperor|person|playwright|novelist)\b|\bwho\b|\b(he|she)\b"
    ),
    "visual_reference": r"\b(seen here|pictured|shown here|in this (photo|picture|image|clip|video))\b",
    "wordplay": r"\b(pun|puns|rhymes with|anagram|sounds like|homophones?|play on words)\b",
}

_SPREADSHEET_CUES = {
    "formula_or_macro": (
        r"\b(formulas?|functions?|sumifs?|countifs?|averageifs?|vlookup|hlookup|xlookup|index|iferror"
        r"|vba|macros?)\b|\bif\("
    ),
    "lookup_or_join": r"\b(look ?ups?|vlookup|xlookup|index|match(es|ing)?|join|merge|map(ping)?|corresponding|cross-?reference)\b",
    "date_or_time": (
        r"\b(dates?|days?|weeks?|weekdays?|weekends?|workdays?|months?|monthly|years?|yearly|quarters?"
        r"|times?|hours?|minutes?|periods?|fiscal)\b"
    ),
    "aggregation_or_summary": (
        r"\b(sum|sums|totals?|count|counts|average|mean|aggregate|subtotals?|crosstab|pivot|summary|summari[sz]e)\b"
    ),
    "filter_or_list": r"\b(filter\w*|extract\w*|unique|distinct|dedup\w*|duplicates?|only (those|the rows))\b",
    "formatting": (
        r"\b(format(ting|ted)?|colou?r(ed|s)?|highlight\w*|bold|italic|fonts?|borders?|align(ment)?|background)\b"
    ),
    "schedule_or_calendar": r"\b(schedules?|timetables?|rotas?|rosters?|calendars?|shifts?|class periods?)\b",
    "every_nth_row": r"\bevery (other|\d+(st|nd|rd|th)?|nth)\b|\boffset\b|\bnth\b",
    "residual_balancing": r"\b(residuals?|balanc(e|ing)|redistribut\w*|distribut\w*|min(imum)? and max(imum)?|constraints?)\b",
    "blank_handling": r"\b(blank|empty|null)\b",
    "text_matching": (
        r"\b(begins? with|starts? with|ends? with|contains?|containing|substring|exact match|case-insensitive"
        r"|partial match)\b"
    ),
}

CUE_FEATURES: dict[str, tuple[str, dict[str, str]]] = {
    "docvqa_gpt55": ("question_cues", _DOCVQA_CUES),
    "livemath_gpt55": ("option_cues", _LIVEMATH_CUES),
    "officeqa_gpt55": ("question_cues", _OFFICEQA_CUES),
    "searchqa_gpt55": ("clue_cues", _SEARCHQA_CUES),
    "spreadsheetbench_gpt55": ("operation", _SPREADSHEET_CUES),
}

_ALFWORLD_OBJECTS = {
    "pan": r"\b(frying ?)?pans?\b|\bsaucepans?\b",
    "kettle_or_teapot": r"\b(kettle|teapot|tea pot)s?\b",
    "dishsponge": r"\bdish ?sponges?\b",
    "other_kitchen_item": (
        r"\b(apple|bread|egg|lettuce|potato(es)?|tomato(es)?|mug|cup|plate|bowl|pot|knife|knives|butter ?knife"
        r"|fork|spoon|spatula|ladle|wine ?bottle|salt ?shaker|pepper ?shaker|kitchen ?ware)s?\b"
    ),
    "office_or_bedroom_item": (
        r"\b(pen|pencil|book|cd|cell ?phone|laptop|key ?chain|credit ?card|alarm ?clock|newspaper|pillow"
        r"|remote ?control|watch|statue|tissue ?box)s?\b"
    ),
}


def _alfworld_target_object(task: str) -> str:
    """The first recognized object family in the goal; specific families win."""
    for name, pattern in _ALFWORLD_OBJECTS.items():
        if re.search(pattern, task, re.I):
            return name
    return "other" if task.strip() else "unknown"
