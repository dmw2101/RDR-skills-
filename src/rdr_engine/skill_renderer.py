"""Compose the fragments of every terminal conclusion of an MCRDR prediction."""

from __future__ import annotations

from dataclasses import dataclass

from .engine import RDREngine
from .skill_compiler import Fragment, FragmentCatalog, _table_rows
from .types import EvaluationStep, Prediction


LEARNED_RULES_HEADER = (
    "These rules were learned from earlier mistakes on tasks like this one, and their conditions "
    "hold for this task. Follow them. Where they differ from the general guidance below, they win."
)


@dataclass(frozen=True)
class RenderedSkill:
    markdown: str
    selected_ids: tuple[str, ...]
    terminal_ids: tuple[int, ...]
    trace: tuple[EvaluationStep, ...]


class SkillRenderer:
    def render(
        self, prediction: Prediction, engine: RDREngine, catalog: FragmentCatalog
    ) -> RenderedSkill:
        engine._check_prediction(prediction)
        base = {fragment.id: fragment for fragment in catalog.fragments}
        learned = {fragment.id: fragment for fragment in catalog.learned}
        by_id = {**base, **learned}
        active: set[str] = set()
        # Only terminal, non-stopping nodes report conclusions; refinements and
        # stopping rules have already replaced or suppressed their parents.
        for item in prediction.conclusions:
            operation = item.conclusion
            if not isinstance(operation, dict) or set(operation) != {"fragments"}:
                raise ValueError(f"node {item.node_id} has an invalid fragment conclusion")
            values = operation["fragments"]
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                raise ValueError(f"node {item.node_id} has an invalid fragments list")
            for fragment_id in values:
                if fragment_id not in by_id:
                    raise ValueError(f"node {item.node_id} refers to missing fragment {fragment_id}")
                active.add(fragment_id)

        pending = list(active)
        while pending:
            fragment_id = pending.pop()
            for required in by_id[fragment_id].requires:
                if required not in by_id:
                    raise ValueError(f"missing required fragment {required}")
                if required not in active:
                    active.add(required)
                    pending.append(required)

        # Learned guidance is shown first, in a block of its own, in the order it was learned;
        # the skill's own fragments follow in their original order.
        learned_ids = [fragment.id for fragment in catalog.learned if fragment.id in active]
        ordered = sorted((item for item in active if item in base),
                         key=lambda item: (base[item].order, base[item].end, item))
        selected = [base[item] for item in ordered]
        rows = _table_rows(catalog.skill_input.lines)
        header_id_by_span = {
            (fragment.start, fragment.end): fragment.id
            for fragment in selected if fragment.role == "non_instruction"
        }

        def table_header(fragment: Fragment) -> str | None:
            span = rows.get(fragment.start)
            if span is not None:
                return header_id_by_span.get(span)
            if (fragment.start, fragment.end) in set(rows.values()):
                return fragment.id
            return None

        pieces: list[str] = []
        previous: Fragment | None = None
        for fragment_id in ordered:
            fragment = base[fragment_id]
            if previous is not None:
                same_table = (
                    table_header(previous) is not None
                    and table_header(previous) == table_header(fragment)
                )
                pieces.append("\n" if same_table or fragment.start == previous.end + 1 else "\n\n")
            pieces.append(fragment.text.rstrip("\r\n"))
            previous = fragment
        markdown = "".join(pieces)
        if learned_ids:
            rules = "\n\n".join(learned[item].text.rstrip("\r\n") for item in learned_ids)
            block = f"<learned_rules>\n{LEARNED_RULES_HEADER}\n\n{rules}\n</learned_rules>"
            markdown = block + ("\n\n" + markdown if markdown else "")
        if markdown:
            markdown += "\n"
        return RenderedSkill(
            markdown,
            tuple(learned_ids + ordered),
            tuple(item.node_id for item in prediction.conclusions),
            prediction.trace,
        )
