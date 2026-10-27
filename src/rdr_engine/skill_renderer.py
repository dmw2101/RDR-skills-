"""Compose the fragments of every terminal conclusion of an MCRDR prediction."""

from __future__ import annotations

from dataclasses import dataclass

from .engine import RDREngine
from .skill_compiler import Fragment, FragmentCatalog, _table_rows
from .types import EvaluationStep, Prediction


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

        learned_order = {fragment.id: index for index, fragment in enumerate(catalog.learned)}

        def position(fragment_id: str) -> tuple[float, float, int, int, str]:
            # Learned guidance follows its anchor (or the end), after any base fragment there.
            if fragment_id in base:
                fragment = base[fragment_id]
                return (fragment.order, fragment.end, 0, 0, fragment.id)
            anchor = base.get(learned[fragment_id].anchor)
            if anchor is None:
                return (float("inf"), float("inf"), 1, learned_order[fragment_id], fragment_id)
            return (anchor.order, anchor.end, 1, learned_order[fragment_id], fragment_id)

        ordered = sorted(active, key=position)
        selected = [base[item] for item in ordered if item in base]
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
            fragment = base.get(fragment_id)
            if pieces:
                if fragment is None or previous is None:
                    separator = "\n\n"  # Learned guidance is always its own block.
                else:
                    same_table = (
                        table_header(previous) is not None
                        and table_header(previous) == table_header(fragment)
                    )
                    separator = "\n" if same_table or fragment.start == previous.end + 1 else "\n\n"
                pieces.append(separator)
            pieces.append(by_id[fragment_id].text.rstrip("\r\n"))
            previous = fragment
        markdown = "".join(pieces)
        if markdown:
            markdown += "\n"
        return RenderedSkill(
            markdown,
            tuple(ordered),
            tuple(item.node_id for item in prediction.conclusions),
            prediction.trace,
        )
