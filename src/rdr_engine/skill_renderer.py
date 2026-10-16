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
        by_id = {fragment.id: fragment for fragment in catalog.fragments}
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

        selected = sorted((by_id[item] for item in active), key=lambda f: (f.order, f.end, f.id))
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
        for fragment in selected:
            if previous is not None:
                same_table = (
                    table_header(previous) is not None
                    and table_header(previous) == table_header(fragment)
                )
                separator = "\n" if same_table or fragment.start == previous.end + 1 else "\n\n"
                pieces.append(separator)
            pieces.append(fragment.text.rstrip("\r\n"))
            previous = fragment
        markdown = "".join(pieces)
        if markdown:
            markdown += "\n"
        return RenderedSkill(
            markdown,
            tuple(fragment.id for fragment in selected),
            tuple(item.node_id for item in prediction.conclusions),
            prediction.trace,
        )
