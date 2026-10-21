"""Offline evaluation, structured MAP proposals, and reference insertion."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ._json import ValidationError, read, write
from .contracts import ContextSnapshot
from .engine import FixtureEngineAdapter, inspect_upstream
from .evaluation import ModelAccessDisabled, RecordedResponseProvider, prepare_request
from .examples import example_context, example_registry, example_recording, example_tree
from .registry import ConditionRegistry
from .runtime import run_context


def demo(output_dir):
    output_dir = Path(output_dir)
    registry = example_registry()
    registry.save(output_dir / "registry.json")
    write(output_dir / "fixture-tree.json", example_tree().to_dict())
    reports = []
    for inspected in (False, True):
        name = "after-inspection" if inspected else "before-inspection"
        directory = output_dir / name
        context = example_context(inspected=inspected)
        request = prepare_request(registry, context)
        recording = example_recording(inspected=inspected)
        write(directory / "context.json", context.to_dict())
        write(directory / "request.json", request.to_dict())
        write(directory / "recording.json", recording)
        result = run_context(context, registry, provider=RecordedResponseProvider(recording), adapter=example_tree())
        write(directory / "result.json", result)
        reports.append({"snapshot": name, "status": result["status"],
                        "fragment_ids": result["composition"]["fragment_ids"]})
    summary = {"kind": "hand_authored_reference_example", "model_calls": 0,
               "semantic_quality": "not_measured", "snapshots": reports, "upstream": inspect_upstream()}
    write(output_dir / "summary.json", summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline Trace2Skill condition evaluation and conditional patch integration")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "evaluate"):
        command = sub.add_parser(name)
        command.add_argument("--registry", type=Path, required=True)
        command.add_argument("--context", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)
        if name == "evaluate":
            command.add_argument("--recording", type=Path, help="Explicit offline input/output example; default refuses inference")
            command.add_argument("--engine", choices=("upstream", "reference"), default="upstream")
            command.add_argument("--tree", type=Path, help="Authored reference tree; requires --engine reference")
    check = sub.add_parser("check-engine")
    check.add_argument("--output", type=Path)
    example = sub.add_parser("demo")
    example.add_argument("--output-dir", type=Path, default=Path("experiments/trace2skill-bridge/demo-simple"))
    acceptance = sub.add_parser("acceptance")
    acceptance.add_argument("--output-dir", type=Path, default=Path("experiments/trace2skill-bridge/verification"))
    integration = sub.add_parser("integration-demo", help="Two recorded conditional corrections; no model calls")
    integration.add_argument("--output-dir", type=Path, default=Path("experiments/trace2skill-bridge/integration-demo"))
    initialize = sub.add_parser("init-skill", help="Snapshot base Markdown and unchanged resources")
    initialize.add_argument("--skill-dir", type=Path, required=True)
    initialize.add_argument("--registry", type=Path, required=True)
    initialize.add_argument("--output", type=Path, required=True)
    failure = sub.add_parser("import-failure", help="Read upstream verifier and verified analyst outputs")
    for flag in ("evaluation", "parsed-errors", "analysis-dir", "context", "skill-text", "output"):
        failure.add_argument(f"--{flag}", type=Path, required=True)
    failure.add_argument("--task-id", required=True)
    for name in ("prepare-patch", "export-map"):
        command = sub.add_parser(name)
        command.add_argument("--registry", type=Path, required=True)
        command.add_argument("--failure", type=Path, required=True)
        if name == "export-map":
            command.add_argument("--recording", type=Path, required=True)
            command.add_argument("--output-dir", type=Path, required=True)
        else:
            command.add_argument("--output", type=Path, required=True)
    insertion = sub.add_parser("insert", help="Stage and replay; reference backend must be explicitly selected")
    insertion.add_argument("--state", type=Path, required=True)
    insertion.add_argument("--proposal", type=Path, required=True)
    insertion.add_argument("--protected", type=Path, required=True, help="List of explicit context-evaluation recordings")
    insertion.add_argument("--engine", choices=("upstream", "reference"), default="upstream")
    insertion.add_argument("--output-dir", type=Path, required=True)
    render = sub.add_parser("render-skill", help="Materialize a new skill for a recorded context")
    for flag in ("state", "context", "recording", "output-dir"):
        render.add_argument(f"--{flag}", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "integration-demo":
            from .integration_example import integration_demo
            result = integration_demo(args.output_dir)
            print(f"Recorded integration: {len(result['steps'])} insertions; {args.output_dir}; model calls: 0")
            return 0
        if args.command == "init-skill":
            from .skill_state import import_skill
            if args.output.exists():
                raise ValidationError("Initial state output must be a new file")
            import_skill(args.skill_dir, ConditionRegistry.load(args.registry)).save(args.output)
            print(f"Frozen skill state: {args.output}")
            return 0
        if args.command == "import-failure":
            from .trace2skill import load_failure
            failed = load_failure(task_id=args.task_id, context=ContextSnapshot.from_dict(read(args.context)),
                                  skill_text=args.skill_text.read_bytes().decode("utf-8"),
                                  evaluation_file=args.evaluation, parsed_errors_file=args.parsed_errors,
                                  analysis_dir=args.analysis_dir)
            write(args.output, failed.to_dict())
            print(f"Verified analysis imported: {args.output}; proposed correction not yet validated")
            return 0
        if args.command in ("prepare-patch", "export-map"):
            from .patches import FailedExecution, PatchRequest
            from .trace2skill import ExceptionMapCreator, verify_source
            registry = ConditionRegistry.load(args.registry)
            failed = FailedExecution.from_dict(read(args.failure))
            verify_source(failed)
            if args.command == "prepare-patch":
                write(args.output, PatchRequest(registry, failed).to_dict())
                print(f"Tree-free patch request: {args.output}; model calls: 0")
            else:
                ExceptionMapCreator().run_map_phase(registry, [failed], [read(args.recording)], args.output_dir)
                print(f"Structured MAP proposal exported: {args.output_dir}; reducer not called; model calls: 0")
            return 0
        if args.command == "insert":
            from .insertion import publish_patch
            from .patches import PatchProposal
            from .skill_state import SkillState
            protected = read(args.protected)
            if type(protected) is not list:
                raise ValidationError("--protected must contain a list of context-evaluation recordings")
            report = publish_patch(SkillState.load(args.state), PatchProposal.from_dict(read(args.proposal)),
                                   protected, args.output_dir, engine=args.engine)
            print(f"{report['status']}; {args.output_dir}; production ready: false; model calls: 0")
            return 0 if report["status"] == "ready_reference" else 2
        if args.command == "render-skill":
            from .skill_state import SkillState, materialize_skill
            report = materialize_skill(SkillState.load(args.state), ContextSnapshot.from_dict(read(args.context)),
                                       read(args.recording), args.output_dir)
            print(f"{report['status']}: {args.output_dir}; task execution not run")
            return 0
        if args.command == "demo":
            result = demo(args.output_dir)
            print(f"Reference example written to {args.output_dir}; model calls: 0; upstream: {result['upstream']['status']}")
            return 0
        if args.command == "acceptance":
            from .acceptance import run_acceptance
            report = run_acceptance(args.output_dir)
            print(f"Offline acceptance: {report['status']}; scenarios: {len(report['scenarios'])}; model calls: 0")
            return 0 if report["status"] == "passed" else 1
        if args.command == "check-engine":
            result = inspect_upstream()
            if args.output:
                write(args.output, result)
            import json
            print(json.dumps(result, indent=2))
            return 0 if result["status"] == "supported" else 2
        registry = ConditionRegistry.load(args.registry)
        context = ContextSnapshot.from_dict(read(args.context))
        if args.command == "prepare":
            write(args.output, prepare_request(registry, context).to_dict())
            print(f"Evaluation request written to {args.output}; model calls: 0")
            return 0
        if args.engine == "reference" and args.tree is None:
            raise ValidationError("--engine reference requires --tree")
        if args.engine != "reference" and args.tree is not None:
            raise ValidationError("--tree requires --engine reference")
        provider = None if args.recording is None else RecordedResponseProvider.from_file(args.recording)
        adapter = None if args.tree is None else FixtureEngineAdapter.from_dict(read(args.tree))
        result = run_context(context, registry, provider=provider, adapter=adapter)
        write(args.output, result)
        print(f"{result['status']}; report: {args.output}; model calls: 0")
        return 0 if result["status"] == "reference_resolved" else 2
    except (ValidationError, ModelAccessDisabled, OSError) as exc:
        print(f"Bridge stopped without inference: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
