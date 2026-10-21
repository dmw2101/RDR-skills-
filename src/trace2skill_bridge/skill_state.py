"""A single inspectable skill state: registry, tree, Markdown, and frozen resources."""

import base64
import binascii
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ._json import ValidationError, fields, read, text, write
from .contracts import ContextSnapshot, Expression, FragmentDelta
from .engine import FixtureEngineAdapter, RuleNode, compose_fragments
from .registry import ConditionRegistry


def markdown_blocks(markdown):
    """Split on blank lines outside fences; preserve every input character."""
    lines = markdown.splitlines(keepends=True)
    blocks, current = [], []
    fence = None
    frontmatter = bool(lines and lines[0].strip() == "---")
    in_header = frontmatter
    for index, line in enumerate(lines):
        current.append(line)
        stripped = line.lstrip()
        if in_header:
            if index and line.strip() == "---":
                in_header = False
                blocks.append("".join(current))
                current = []
            continue
        match = re.match(r"(`{3,}|~{3,})", stripped)
        if match:
            marker = match.group(1)
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence) and not stripped[len(marker):].strip():
                fence = None
        if not line.strip() and fence is None:
            # A run of whitespace belongs to its preceding block, not a fragment.
            if "".join(current).strip():
                blocks.append("".join(current))
                current = []
            elif blocks:
                blocks[-1] += "".join(current)
                current = []
    if in_header or fence:
        raise ValidationError("Unclosed Markdown frontmatter or code fence")
    if current:
        blocks.append("".join(current))
    if not blocks or not markdown.strip():
        raise ValidationError("The skill must contain Markdown")
    return tuple(blocks), frontmatter


@dataclass(frozen=True)
class Fragment:
    id: str
    text: str
    replaces: str | None = None

    def __post_init__(self):
        text(self.id, "fragment ID")
        text(self.text, "fragment text")
        if self.replaces is not None:
            text(self.replaces, "replacement target")

    def to_dict(self):
        return {"id": self.id, "text": self.text, "replaces": self.replaces}

    @classmethod
    def from_dict(cls, data):
        fields(data, ("id", "text", "replaces"))
        return cls(**data)


def resource_bytes(resources):
    decoded = {}
    for name, content in resources:
        path = PurePosixPath(name)
        if (not path.parts or path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name
                or str(path) != name or path.parts[0].casefold() == "skill.md"):
            raise ValidationError("Resource paths must stay inside the skill directory")
        try:
            decoded[name] = base64.b64decode(content, validate=True)
        except (ValueError, binascii.Error, TypeError) as exc:
            raise ValidationError("Invalid resource encoding") from exc
    if len(decoded) != len(resources) or len({p.casefold() for p in decoded}) != len(decoded):
        raise ValidationError("Duplicate resource path")
    paths = [PurePosixPath(name.casefold()) for name in decoded]
    if any(a != b and a in b.parents for a in paths for b in paths):
        raise ValidationError("A resource cannot be both a file and a directory")
    return decoded


@dataclass(frozen=True)
class SkillState:
    registry: ConditionRegistry
    nodes: tuple[RuleNode, ...]
    fragments: tuple[Fragment, ...]
    resources: tuple[tuple[str, str], ...] = ()
    protected_contexts: tuple[ContextSnapshot, ...] = ()
    changes: tuple[dict, ...] = ()

    def __post_init__(self):
        if not isinstance(self.registry, ConditionRegistry):
            raise ValidationError("Expected a condition registry")
        if any(type(items) is not tuple for items in (self.nodes, self.fragments, self.resources, self.protected_contexts, self.changes)):
            raise ValidationError("State collections must be immutable tuples")
        if any(not isinstance(f, Fragment) for f in self.fragments):
            raise ValidationError("Expected fragment records")
        FixtureEngineAdapter(self.nodes)
        ids = [f.id for f in self.fragments]
        if len(ids) != len(set(ids)):
            raise ValidationError("Duplicate fragment ID")
        earlier = set()
        for fragment in self.fragments:
            if fragment.replaces is not None and fragment.replaces not in earlier:
                raise ValidationError("Replacement must refer to an earlier fragment")
            earlier.add(fragment.id)
        for node in self.nodes:
            self.registry.validate_expression(node.when)
            if (set(node.delta.add) | set(node.delta.remove)) - set(ids):
                raise ValidationError("Node refers to a missing fragment")
        resource_bytes(self.resources)
        if any(c.skill_id != self.registry.skill_id for c in self.protected_contexts):
            raise ValidationError("Protected context belongs to another skill")
        for context in self.protected_contexts:
            # Ensure persisted facts still reference the append-only registry.
            from .evaluation import prepare_request
            prepare_request(self.registry, context)

    def adapter(self):
        return FixtureEngineAdapter(self.nodes)

    def render(self, statuses):
        result = compose_fragments(self.adapter().traverse(statuses))
        if result.status != "resolved":
            raise ValidationError("Independent branches have conflicting fragment operations")
        selected = set(result.fragments)
        # Two branches replacing the same block differently also conflict, even
        # though their remove operations on the old fragment agree.
        by_id, slots = {f.id: f for f in self.fragments}, {}
        for fragment in self.fragments:
            if fragment.id not in selected:
                continue
            anchor = fragment
            while anchor.replaces is not None:
                anchor = by_id[anchor.replaces]
            if anchor.id in slots:
                raise ValidationError(f"Conflicting replacements of {anchor.id}")
            slots[anchor.id] = fragment.id
        return "".join(f.text for f in self.fragments if f.id in selected)

    def to_dict(self):
        return {"kind": "reference_skill_state", "registry": self.registry.to_dict(),
                "nodes": [n.to_dict() for n in self.nodes], "fragments": [f.to_dict() for f in self.fragments],
                "resources": dict(self.resources), "protected_contexts": [c.to_dict() for c in self.protected_contexts],
                "changes": list(self.changes)}

    def save(self, path):
        # Registry, tree and fragments are published together, never separately.
        write(path, self.to_dict())

    @classmethod
    def from_dict(cls, data):
        fields(data, ("kind", "registry", "nodes", "fragments", "resources", "protected_contexts", "changes"))
        if data["kind"] != "reference_skill_state" or type(data["resources"]) is not dict:
            raise ValidationError("Expected a reference skill state")
        for name in ("nodes", "fragments", "protected_contexts", "changes"):
            if type(data[name]) is not list:
                raise ValidationError(f"{name} must be a list")
        return cls(ConditionRegistry.from_dict(data["registry"]), tuple(RuleNode.from_dict(n) for n in data["nodes"]),
                   tuple(Fragment.from_dict(f) for f in data["fragments"]), tuple(data["resources"].items()),
                   tuple(ContextSnapshot.from_dict(c) for c in data["protected_contexts"]), tuple(data["changes"]))

    @classmethod
    def load(cls, path):
        return cls.from_dict(read(path))


def import_skill(skill_dir, registry):
    """Freeze the base skill and resources. No resource is rewritten by patches."""
    directory = Path(skill_dir).resolve(strict=True)
    skill = directory / "SKILL.md"
    if skill.is_symlink():
        raise ValidationError("Skill snapshots cannot contain symlinks")
    markdown = skill.read_bytes().decode("utf-8")
    blocks, _ = markdown_blocks(markdown)
    fragments = tuple(Fragment(f"base_{i:04d}", block) for i, block in enumerate(blocks, 1))
    resources = []
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValidationError("Skill snapshots cannot contain symlinks")
        if path.is_file() and path != skill:
            resources.append((path.relative_to(directory).as_posix(), base64.b64encode(path.read_bytes()).decode("ascii")))
    root = RuleNode(0, None, Expression("all"), FragmentDelta(tuple(f.id for f in fragments)))
    return SkillState(registry, (root,), fragments, tuple(resources))


def materialize_skill(state, context, recording, output_dir):
    """Fresh condition evaluation, then write a NEW runnable skill directory."""
    from .evaluation import RecordedResponseProvider, evaluate_context
    evaluation = evaluate_context(context, state.registry, provider=RecordedResponseProvider(recording))
    markdown = state.render(evaluation.statuses())
    resources = resource_bytes(state.resources)
    destination = Path(output_dir)
    if destination.exists():
        raise ValidationError("Materialization requires a new directory; original skills are never overwritten")
    destination.mkdir(parents=True)
    (destination / "SKILL.md").write_bytes(markdown.encode("utf-8"))
    for name, content in resources.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return {"status": "materialized_reference_skill", "directory": str(destination),
            "evaluation": evaluation.to_dict(), "model_calls": 0,
            "task_execution": "not_run", "production_ready": False}
