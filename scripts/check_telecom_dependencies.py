"""Compile the telecom MMS example and inspect dependency-aware resolution."""

import argparse
import json
from pathlib import Path
from shutil import copy2

from rdr_engine.dependencies import DependencyReview, Link, build_bundle
from rdr_engine.skill_compiler import FeatureSchema, LineRange, SkillBundle, SkillInput
from rdr_service.backend import RDRBackend
from skill_agent.contracts import ResolveSkillRequest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    source = Path("skills/telecom/initial.md")
    line_count = len(SkillInput.from_markdown(source).lines)
    procedures = {
        "overdue_bill": (103, 119), "line_suspension": (120, 130),
        "cellular_service": (217, 261), "mobile_data": (262, 325),
        "mms": (326, line_count),
    }
    base = args.output / "base/telecom"
    base.mkdir(parents=True)
    copy2(source, base)
    ir = {
        "root": [{"start": start, "end": end} for start, end in [(1, 102), (131, 216)]],
        "rules": [{
            "id": name,
            "when": [{"field": "operation", "op": "contains", "value": name}],
            "source": [{"start": start, "end": end}],
        } for name, (start, end) in procedures.items()],
        "excluded": [],
    }
    ir_path = args.output / "telecom-ir.json"
    ir_path.write_text(json.dumps(ir, indent=2), encoding="utf-8")
    schema = FeatureSchema.model_validate({"features": {"operation": {
        "description": "Procedures relevant to the task",
        "values": list(procedures), "multiple": True,
    }}})
    SkillBundle.build(base, ir_path=ir_path, feature_schema=schema, model_id="manual-demo")
    review = DependencyReview(links=[
        Link(procedure="mms", prerequisite="mobile_data", evidence=[LineRange(start=332, end=334)],
             reason="MMS requires working mobile data and explicitly refers to its guide."),
        Link(procedure="mobile_data", prerequisite="cellular_service",
             evidence=[LineRange(start=268, end=269)],
             reason="Mobile data requires cellular service. Include the guide explaining the check."),
    ], deferred=[
        Link(procedure="cellular_service", prerequisite="line_suspension",
             evidence=[LineRange(start=255, end=261)],
             reason="Suspension guidance depends on the observed cause of the service problem."),
        Link(procedure="line_suspension", prerequisite="overdue_bill",
             evidence=[LineRange(start=120, end=129)],
             reason="Payment applies if the suspension involves overdue bills. An expired contract has a different rule."),
    ])
    (args.output / "review.json").write_text(review.model_dump_json(indent=2), encoding="utf-8")
    bundle = args.output / "with-dependencies/telecom"
    build_bundle(base, review, bundle)
    backend = RDRBackend(args.output / "snapshots")
    artifact_id = backend.prepare(bundle)["artifact_id"]
    catalog = backend.condition_catalog("telecom", artifact_id)
    evaluations = []
    for entry in catalog["conditions"]:
        selected = entry["predicate"]["value"] == "mms"
        evaluations.append({"condition_id": entry["id"], "result": "true" if selected else "false",
                            "evidence": "Customer cannot send pictures." if selected else ""})
    resolved = backend.resolve(ResolveSkillRequest(
        "telecom", "I cannot send pictures by text.", {}, artifact_id,
        condition_evaluations={"catalog_fingerprint": catalog["catalog_fingerprint"],
                               "evaluations": evaluations},
    ))
    added = [link["prerequisite"] for link in resolved.metadata["dependencies"]["added"]]
    if added != ["mobile_data", "cellular_service"]:
        raise RuntimeError(f"unexpected telecom dependencies: {added}")
    (args.output / "guidance.md").write_text(resolved.markdown, encoding="utf-8")
    (args.output / "resolution.json").write_text(json.dumps(resolved.metadata, indent=2), encoding="utf-8")
    print("MMS -> mobile data -> cellular service. Suspension and overdue bills remain deferred.")


if __name__ == "__main__":
    main()
