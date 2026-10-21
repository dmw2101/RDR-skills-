"""Offline scenarios for the registry -> evaluate -> traverse flow."""

from __future__ import annotations

import copy
import io
import itertools
import platform
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from ._json import ValidationError, canonical, loads, read, write
from .contracts import Attribute, ContextSnapshot, Evidence, Expression, FragmentDelta, Truth
from .engine import FixtureEngineAdapter, RuleNode, compose_fragments, inspect_upstream
from .evaluation import ModelAccessDisabled, RecordedResponseProvider, evaluate_context, prepare_request, validate_response
from .examples import example_context, example_registry, example_recording, example_tree
from .registry import ConditionDefinition, ConditionRegistry, import_typed_predicate, inspected_judgments
from .runtime import run_context


def require(value, message):
    if not value:
        raise AssertionError(message)


def rejects(call, error=ValidationError):
    try:
        call()
    except error:
        return
    raise AssertionError(f"Expected {error.__name__}")


def run_acceptance(output_dir):
    scenarios, network_attempts = [], []

    def no_network(*args, **kwargs):
        network_attempts.append("attempted")
        raise AssertionError("Network use is forbidden during offline acceptance")

    def scenario(name, operation):
        try:
            detail = operation()
            scenarios.append({"name": name, "status": "passed", "detail": detail})
        except Exception as exc:
            scenarios.append({"name": name, "status": "failed", "detail": f"{type(exc).__name__}: {exc}"})

    def ref(name):
        return Expression("condition", name)

    def composition(nodes, statuses=None):
        return compose_fragments(FixtureEngineAdapter(nodes).traverse(statuses or {}))

    root = RuleNode(0, None, Expression("all"), FragmentDelta(("parent",)))
    with patch("socket.socket", side_effect=no_network), patch("socket.create_connection", side_effect=no_network):
        with tempfile.TemporaryDirectory(prefix="trace2skill-bridge-") as temporary:
            directory = Path(temporary)
            registry, context = example_registry(), example_context()
            request = prepare_request(registry, context)
            recording = example_recording()
            response = recording["response"]

            def inheritance():
                child = RuleNode(1, 0, Expression("all"), FragmentDelta(("child",)))
                require(composition((root, child)).fragments == ("child", "parent"), "Parent disappeared")
                removal = replace(child, delta=FragmentDelta(("child",), ("parent",)))
                require(composition((root, removal)).fragments == ("child",), "Child removal failed")
                grandchild = RuleNode(2, 1, Expression("all"), FragmentDelta(("parent",)))
                require(composition((root, removal, grandchild)).fragments == ("child", "parent"), "Reinstatement failed")
            scenario("parent_retention_explicit_removal_and_reinstatement", inheritance)

            def ancestry():
                nodes = (root, RuleNode(1, 0, ref("a"), FragmentDelta(("middle",))),
                         RuleNode(2, 1, ref("b"), FragmentDelta(("child",))))
                for status in (Truth.FALSE, Truth.UNKNOWN):
                    matches = FixtureEngineAdapter(nodes).traverse({"a": status, "b": Truth.TRUE})
                    require(not matches[-1].evaluated, "Blocked child was evaluated")
                    require(compose_fragments(matches).fragments == ("parent",), "Blocked child contributed")
            scenario("false_and_unknown_ancestors_block_children", ancestry)

            def siblings():
                first = RuleNode(1, 0, Expression("all"), FragmentDelta(("shared",)))
                second = RuleNode(2, 0, Expression("all"), FragmentDelta(("shared",)))
                require(composition((root, first, second)).fragments == ("parent", "shared"), "Duplicate addition")
                conflicting = replace(second, delta=FragmentDelta(remove=("shared",)))
                result = composition((root, first, conflicting))
                require(result.status == "unresolved" and not result.fragments, "Sibling conflict selected partial guidance")
            scenario("independent_additions_and_conflicts", siblings)

            def invalid_nodes():
                rejects(lambda: FixtureEngineAdapter((root, RuleNode(1, 9, Expression("all"), FragmentDelta()))))
                rejects(lambda: FixtureEngineAdapter((root, RuleNode(1, 2, Expression("all"), FragmentDelta()),
                                                      RuleNode(2, 1, Expression("all"), FragmentDelta()))))
                rejects(lambda: FragmentDelta(("same",), ("same",)))
            scenario("invalid_tree_or_fragment_delta_rejected", invalid_nodes)

            def logic():
                for a, b in itertools.product(Truth, repeat=2):
                    both = Expression.from_dict({"all": [{"condition": "a"}, {"condition": "b"}]})
                    either = Expression.from_dict({"any": [{"condition": "a"}, {"condition": "b"}]})
                    expected_all = Truth.FALSE if Truth.FALSE in (a, b) else Truth.TRUE if a is b is Truth.TRUE else Truth.UNKNOWN
                    expected_any = Truth.TRUE if Truth.TRUE in (a, b) else Truth.FALSE if a is b is Truth.FALSE else Truth.UNKNOWN
                    require(both.evaluate({"a": a, "b": b}) is expected_all, "AND mismatch")
                    require(either.evaluate({"a": a, "b": b}) is expected_any, "OR mismatch")
                negative = Expression.from_dict({"not": {"condition": "a"}})
                require(negative.evaluate({"a": Truth.UNKNOWN}) is Truth.UNKNOWN, "Unknown became false")
                require(not negative.proves_exclusion({"a": Truth.UNKNOWN}), "Unknown established exclusion")
                rejects(lambda: ref("missing").evaluate({}))
            scenario("three_valued_logic_and_unknown_exclusion", logic)

            def append_only():
                existing = registry.get("contains_formulas")
                require(registry.register(existing) is registry, "Reusing an ID changed the registry")
                rejects(lambda: registry.register(replace(existing, definition="Only the active sheet has formulas.")))
                duplicate = replace(existing, id="duplicate", definition=existing.definition.upper())
                rejects(lambda: registry.register(duplicate))
                new = ConditionDefinition("active_sheet_contains_formulas", "The active sheet contains a formula.")
                extended = registry.register(new)
                require(extended.get(existing.id) == existing and len(registry.conditions) == 3, "Existing conditions changed")
            scenario("append_only_conditions_reuse_and_duplicate_detection", append_only)

            def persistence():
                path = directory / "registry.json"
                registry.save(path)
                require(ConditionRegistry.load(path) == registry, "Registry round trip changed conditions")
                minimal = ConditionRegistry.from_dict({"skill_id": "xlsx", "conditions": [{"id": "one", "definition": "A workbook is open."}]})
                require(minimal.get("one").definition == "A workbook is open.", "Minimal registry failed")
                minimal_context = ContextSnapshot.from_dict({"skill_id": "xlsx", "task": "Open a workbook."})
                require(not minimal_context.observations, "Minimal context failed")
            scenario("simple_registry_and_context_json", persistence)

            def typed_conditions():
                typed = ConditionRegistry("xlsx")
                for name, predicate in (
                    ("formulas", {"field": "has_formulas", "op": "eq", "value": "yes"}),
                    ("bulk", {"field": "operation", "op": "in", "value": ["sort", "group"]}),
                    ("group", {"field": "operations", "op": "contains", "value": "group"}),
                ):
                    typed = import_typed_predicate(typed, condition_id=name, predicate=predicate, definition=f"Observed {name}.")
                observed = ContextSnapshot("xlsx", "Group data.", attributes=(
                    Attribute("has_formulas", "yes", ("task",)), Attribute("operation", "group", ("task",)),
                    Attribute("operations", ("group", "sort"), ("task",))))
                require(all(j.status is Truth.TRUE for j in inspected_judgments(typed, observed).values()), "Typed lookup failed")
                unknown = ContextSnapshot("xlsx", "Inspect it.", attributes=(Attribute("has_formulas", "unknown", ("task",)),))
                require(not inspected_judgments(typed, unknown), "Unknown attribute became false")
            scenario("typed_predicates_and_missing_inspection", typed_conditions)

            def input_validation():
                rejects(lambda: loads('{"task":"a","task":"b"}'))
                rejects(lambda: loads('{"value":NaN}'))
                rejects(lambda: ContextSnapshot("xlsx", "Task", (Evidence("task", "duplicate", "note"),)))
                rejects(lambda: ContextSnapshot.from_dict({"skill_id": "xlsx", "task": "Task", "ignored": True}))
            scenario("invalid_inputs_and_evidence_ids_rejected", input_validation)

            def coverage():
                for judgments in (
                    response["judgments"][:-1],
                    response["judgments"] + [response["judgments"][0]],
                    response["judgments"] + [{"condition_id": "invented", "status": "unknown", "evidence_refs": []}],
                ):
                    rejects(lambda judgments=judgments: validate_response(request, {"judgments": judgments}))
            scenario("exact_condition_coverage", coverage)

            def evidence_validation():
                for change in ({"evidence_refs": ["missing"]}, {"evidence_refs": []}, {"status": True}):
                    broken = copy.deepcopy(response)
                    broken["judgments"][0].update(change)
                    rejects(lambda broken=broken: validate_response(request, broken))
            scenario("invalid_status_or_evidence_rejected", evidence_validation)

            def authoritative_facts():
                req = prepare_request(registry, example_context(inspected=True))
                good = example_recording(inspected=True)["response"]
                validate_response(req, good)
                for change in ({"status": "false"}, {"status": "unknown"}, {"evidence_refs": ["task"]}):
                    bad = copy.deepcopy(good)
                    bad["judgments"][2].update(change)
                    rejects(lambda bad=bad: validate_response(req, bad))
            scenario("known_inspection_facts_respected", authoritative_facts)

            def refresh():
                before = run_context(context, registry, provider=RecordedResponseProvider(recording), adapter=example_tree())
                after = run_context(example_context(inspected=True), registry,
                                    provider=RecordedResponseProvider(example_recording(inspected=True)), adapter=example_tree())
                fragment = "preserve_formulas_when_aggregating"
                require(fragment not in before["composition"]["fragment_ids"], "Hypothetical statement activated guidance")
                require(fragment in after["composition"]["fragment_ids"], "New observation did not change traversal")
                require("preserve_unrelated_sheets" in after["composition"]["fragment_ids"], "Parent guidance disappeared")
            scenario("new_context_refreshes_conditions_and_traversal", refresh)

            def always_evaluate():
                provider = RecordedResponseProvider(recording)
                first = run_context(context, registry, provider=provider, adapter=example_tree())
                second = run_context(context, registry, provider=provider, adapter=example_tree())
                require(provider.invocations == 2, "Repeated context bypassed evaluation")
                require(canonical(first) == canonical(second), "Recorded replay changed results")
                rejects(lambda: evaluate_context(context, registry), ModelAccessDisabled)
            scenario("every_refresh_evaluates_even_for_identical_context", always_evaluate)

            def recording_inputs():
                provider = RecordedResponseProvider(recording)
                rejects(lambda: evaluate_context(example_context(inspected=True), registry, provider=provider))
                extended = registry.register(ConditionDefinition("new_condition", "The current task creates a chart."))
                rejects(lambda: evaluate_context(context, extended, provider=provider))
            scenario("explicit_recording_must_describe_current_inputs", recording_inputs)

            def no_retry():
                broken = copy.deepcopy(recording)
                broken["response"]["judgments"] = []
                provider = RecordedResponseProvider(broken)
                rejects(lambda: evaluate_context(context, registry, provider=provider))
                require(provider.invocations == 1, "Malformed output triggered retries")
            scenario("malformed_output_does_not_trigger_repair", no_retry)

            def expression_registry():
                registry.validate_expression(ref("contains_formulas"))
                rejects(lambda: registry.validate_expression(ref("invented")))
                rejects(lambda: prepare_request(ConditionRegistry("pdf"), context))
            scenario("condition_references_and_skill_scope_checked", expression_registry)

            def engine_probe():
                result = inspect_upstream()
                require(result["status"] == "unsupported", "Review changed upstream capabilities")
                blocked = run_context(context, registry, provider=RecordedResponseProvider(recording))
                require(blocked["status"] == "blocked_upstream" and blocked["traversal"] is None, "Unsupported engine was used")
            scenario("unsupported_upstream_still_blocked", engine_probe)

            def cli():
                from .__main__ import main
                registry.save(directory / "registry.json")
                write(directory / "context.json", context.to_dict())
                write(directory / "recording.json", recording)
                write(directory / "tree.json", example_tree().to_dict())
                common = ["--registry", str(directory / "registry.json"), "--context", str(directory / "context.json")]
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    require(main(["prepare", *common, "--output", str(directory / "request.json")]) == 0, "Prepare failed")
                    require(main(["evaluate", *common, "--recording", str(directory / "recording.json"), "--engine", "reference",
                                  "--tree", str(directory / "tree.json"), "--output", str(directory / "result.json")]) == 0,
                            "Explicit recording workflow failed")
                    require(main(["evaluate", *common, "--output", str(directory / "disabled.json")]) == 2,
                            "Evaluation ran without a recording")
                result = read(directory / "result.json")
                require(result["evaluation"]["input"]["context"] == context.to_dict(), "Run log lost its input")
                require("output" in result["evaluation"], "Run log lost its judgments")
            scenario("cli_evaluation_and_plain_input_output_logs", cli)

            def resources():
                from importlib.resources import files
                for resource in files("trace2skill_bridge").joinpath("schemas").iterdir():
                    if resource.name.endswith(".json"):
                        require(type(loads(resource.read_text(encoding="utf-8"))) is dict, "Malformed schema")
                prompt = request.to_dict()["system_prompt"]
                require("unknown" in prompt and "DATA" in prompt, "Missing prompt instructions")
            scenario("prompt_and_schema_resources", resources)

            from .integration_acceptance import run_integration_scenarios
            run_integration_scenarios(scenario, directory)

    report = {"status": "passed" if all(s["status"] == "passed" for s in scenarios) and not network_attempts else "failed",
              "verified_at_utc": datetime.now(timezone.utc).isoformat(), "python": platform.python_version(),
              "executable": sys.executable, "model_calls": 0, "network_attempts": len(network_attempts),
              "scenarios": scenarios, "upstream_engine": inspect_upstream(),
              "pending": ["Real model interpretation has not been assessed.",
                          "Production traversal awaits the corrected upstream adapter.",
                          "Live structured MAP inference and fresh task execution are not validated.",
                          "Replay checks guidance preservation, not execution accuracy or semantic patch quality."]}
    output_dir = Path(output_dir)
    write(output_dir / "report.json", report)
    lines = ["# Offline acceptance", "", f"Status: {report['status']}", "",
             f"Scenarios: {len(scenarios)}; model calls: 0; network attempts: {len(network_attempts)}", "",
             "| Scenario | Result |", "| --- | --- |"]
    lines.extend(f"| {s['name']} | {s['status']} |" for s in scenarios)
    lines.extend(f"\nFailure: {s['name']}: {s['detail']}" for s in scenarios if s["status"] != "passed")
    lines += ["", "## Pending", "", *[f"- {item}" for item in report["pending"]]]
    (output_dir / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report
