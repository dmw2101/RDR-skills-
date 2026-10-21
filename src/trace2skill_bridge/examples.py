"""Hand-authored examples, not captured model outputs or benchmark evidence."""

from .contracts import ContextSnapshot, Evidence, Expression, FragmentDelta, Judgment, Truth
from .engine import FixtureEngineAdapter, RuleNode
from .registry import ConditionDefinition, ConditionRegistry


def example_registry():
    definitions = (
        ConditionDefinition("working_with_workbook", "The current request operates on an Excel workbook.",
            ("Edit the attached Excel workbook.",), ("Explain a mathematical theorem.",)),
        ConditionDefinition("aggregation_requested", "The requested operation groups or aggregates workbook data.",
            ("Group revenue by month.",), ("Rename a sheet without aggregating data.",)),
        ConditionDefinition("contains_formulas", "At least one cell in the input workbook contains an Excel formula.",
            ("Inspection reports that B2 contains =SUM(A1:A2).",),
            ("Inspection reports that every cell is a constant.",)),
    )
    return ConditionRegistry("xlsx", definitions)


def example_context(*, inspected=False):
    observations = (Evidence("conditional_note", "If the workbook contains formulas, recalculate before checking totals.",
                             "visible-document-note"),)
    facts = ()
    if inspected:
        observations += (Evidence("inspection_1", "Input workbook Sheet1!B2 has formula =SUM(A1:A2).", "authored-inspection-example"),)
        facts = (Judgment("contains_formulas", Truth.TRUE, ("inspection_1",)),)
    return ContextSnapshot("xlsx", "Group the revenue in this Excel workbook by month.", observations, facts)


def example_recording(*, inspected=False):
    """An explicit example input and its hand-authored output."""
    return {"registry": example_registry().to_dict(), "context": example_context(inspected=inspected).to_dict(),
            "response": {"judgments": [
        Judgment("working_with_workbook", Truth.TRUE, ("task",)).to_dict(),
        Judgment("aggregation_requested", Truth.TRUE, ("task",)).to_dict(),
        Judgment("contains_formulas", Truth.TRUE if inspected else Truth.UNKNOWN,
                 ("inspection_1",) if inspected else ("conditional_note",)).to_dict(),
    ]}}


def example_tree():
    condition = lambda name: Expression("condition", name)
    return FixtureEngineAdapter((
        RuleNode(0, None, Expression("all"), FragmentDelta(("general_guidance",))),
        RuleNode(1, 0, condition("working_with_workbook"), FragmentDelta(("preserve_unrelated_sheets",))),
        RuleNode(2, 1, Expression("all", children=(condition("aggregation_requested"), condition("contains_formulas"))),
                 FragmentDelta(("preserve_formulas_when_aggregating",))),
    ))
