"""Prepare immutable snapshots and resolve them using the MCRDR skill pipeline."""

import hashlib
import json
import re
from pathlib import Path

from skill_agent.contracts import ResolvedSkill, fingerprint, source_fingerprint


def implementation_revision():
    import rdr_engine
    root = Path(rdr_engine.__file__).parent
    return fingerprint({"engine/" + p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in sorted(root.glob("*.py"))} | {
                            "service/" + p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in sorted(Path(__file__).parent.glob("*.py"))})


def skill_capabilities(directory):
    """Describe actual compiled rule coverage using source headings, without procedures."""
    lines = (directory / "initial.md").read_text(encoding="utf-8").splitlines()
    ir = json.loads((directory / "compiled/ir.json").read_text(encoding="utf-8"))
    headings, fence = [], None
    for number, line in enumerate(lines, 1):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        heading = re.match(r"^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if fence is None and heading:
            headings.append((number, heading.group(1)))
    capabilities = []
    for rule in ir["rules"]:
        sections = []
        for span in rule["source"]:
            preceding = [title for number, title in headings if number <= span["start"]]
            selected = preceding[-1:] + [title for number, title in headings
                                        if span["start"] < number <= span["end"]]
            sections.extend(title for title in selected if title not in sections)
        capabilities.append({"name": rule["id"].replace("_", " "),
                             "description": "; ".join(sections) or "Conditional procedural guidance"})
    return capabilities


class RDRBackend:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.revision = implementation_revision()

    def prepare(self, skill_dir):
        from rdr_engine.skill_compiler import SkillBundle
        directory = Path(skill_dir).resolve()
        SkillBundle.load(directory)  # Fail before copying stale or incomplete bundles.
        names = ["initial.md", "compiled/manifest.json", "compiled/ir.json",
                 "compiled/fragments.json", "compiled/tree.json"]
        contents = {name: (directory / name).read_bytes() for name in names}
        hashes = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
        info = {"skill_id": directory.name, "files": hashes, "resolver_revision": self.revision,
                "source_fingerprint": source_fingerprint(contents["initial.md"].decode("utf-8"))}
        artifact_id = fingerprint(info)
        target = self.root / artifact_id
        if target.exists():
            self._load_info(artifact_id)
        else:
            self.root.mkdir(parents=True, exist_ok=True)
            import tempfile
            with tempfile.TemporaryDirectory(dir=self.root) as temporary:
                staging = Path(temporary) / "snapshot"
                for name, data in contents.items():
                    path = staging / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                (staging / "snapshot.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
                staging.rename(target)
        return {"artifact_id": artifact_id, "source_fingerprint": info["source_fingerprint"],
                "resolver_revision": self.revision}

    def _load_info(self, artifact_id):
        if re.fullmatch(r"[a-f0-9]{64}", artifact_id) is None:
            raise ValueError("unknown artifact")
        directory = self.root / artifact_id
        info = json.loads((directory / "snapshot.json").read_text(encoding="utf-8"))
        if fingerprint(info) != artifact_id:
            raise ValueError("snapshot metadata changed")
        if info["resolver_revision"] != self.revision:
            raise ValueError("resolver code changed: prepare a new snapshot and start a new run")
        for name, checksum in info["files"].items():
            if hashlib.sha256((directory / name).read_bytes()).hexdigest() != checksum:
                raise ValueError("snapshot content changed")
        return directory, info

    def resolve(self, request):
        from rdr_engine import RDREngine
        from rdr_engine.skill_cases import SkillConditionEvaluator, build_case
        from rdr_engine.skill_compiler import FeatureSchema, SkillBundle
        from rdr_engine.skill_renderer import SkillRenderer
        directory, info = self._load_info(request.artifact_id)
        if info["skill_id"] != request.skill_id:
            raise ValueError("artifact does not belong to requested skill")
        manifest = json.loads((directory / "compiled/manifest.json").read_text(encoding="utf-8"))
        schema = FeatureSchema.model_validate(manifest["feature_schema"])
        # Public observed facts can contain other keys; only this adapter interprets them.
        visible = {k: v for k, v in request.context.items() if k in schema.features}
        rule_catalog = None
        if request.condition_evaluations is not None:
            from .conditions import build_catalog, validate_results, LookupConditionEvaluator, UnknownCondition
            from rdr_engine.types import EvaluationError
            tree, _ = SkillBundle.load(directory)
            rule_catalog = build_catalog(tree, request.artifact_id)
            evaluator = LookupConditionEvaluator(validate_results(rule_catalog, request.condition_evaluations))
            case = {"selection_input": request.selection_input or {"task": request.task}}
        else:
            evaluator = SkillConditionEvaluator(schema)
            case = build_case(request.skill_id, request.task, visible, schema)
        engine, catalog = SkillBundle.load(directory, evaluator)
        try:
            rendered = SkillRenderer().render(engine.predict(case), engine, catalog)
        except Exception as exc:
            if rule_catalog is not None and isinstance(exc, EvaluationError) and isinstance(exc.__cause__, UnknownCondition):
                raise ValueError(str(exc.__cause__)) from exc
            raise
        # Opaque diagnostics: every evaluated node, and the terminal nodes whose
        # conclusions were rendered. The agent records these but never reads them.
        metadata = {
            "engine": "mcrdr",
            "engine_schema_version": RDREngine.SCHEMA_VERSION,
            "case": case,
            "fragment_ids": list(rendered.selected_ids),
            "terminal_ids": list(rendered.terminal_ids),
            "trace": [{"node_id": step.node_id, "matched": step.matched, "depth": step.depth}
                      for step in rendered.trace],
        }
        if rule_catalog is not None:
            metadata.update(catalog_fingerprint=rule_catalog["catalog_fingerprint"],
                            tree_fingerprint=rule_catalog["tree_fingerprint"],
                            condition_evaluations=request.condition_evaluations["evaluations"])
        return ResolvedSkill(rendered.markdown, request.artifact_id, info["source_fingerprint"],
                             self.revision, metadata, capabilities=skill_capabilities(directory))

    def condition_catalog(self, skill_id, artifact_id):
        from rdr_engine.skill_compiler import SkillBundle
        from .conditions import build_catalog
        directory, info = self._load_info(artifact_id)
        if info["skill_id"] != skill_id:
            raise ValueError("artifact does not belong to requested skill")
        engine, _ = SkillBundle.load(directory)
        return {**build_catalog(engine, artifact_id), "skill_id": skill_id,
                "source_fingerprint": info["source_fingerprint"], "resolver_revision": self.revision,
                "capabilities": skill_capabilities(directory)}
