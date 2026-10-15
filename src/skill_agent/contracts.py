"""The public v1 skill-resolution contract; backend diagnostics are opaque."""

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol
import hashlib
import json
import math


def fingerprint(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def source_fingerprint(markdown: str) -> str:
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ResolveSkillRequest:
    skill_id: str
    task: str
    context: dict[str, Any]
    artifact_id: str

    def __post_init__(self):
        if any(not isinstance(x, str) or not x for x in (self.skill_id, self.artifact_id)):
            raise ValueError("skill_id and artifact_id must be nonempty strings")
        if not isinstance(self.task, str) or not isinstance(self.context, dict):
            raise ValueError("task must be text and context must be an object")
        fingerprint(self.context)  # Check JSON compatibility, including finite numbers.

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class ResolvedSkill:
    markdown: str
    artifact_id: str
    source_fingerprint: str
    resolver_revision: str
    metadata: dict[str, Any] = field(default_factory=dict)
    # Required for fair accounting when a future resolver calls a model.
    usage: dict[str, Any] = field(default_factory=lambda: {
        "model_calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0,
    })

    def __post_init__(self):
        if not isinstance(self.markdown, str):
            raise ValueError("markdown must be text")
        for name in ("artifact_id", "source_fingerprint", "resolver_revision"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be nonempty text")
        if not isinstance(self.metadata, dict) or not isinstance(self.usage, dict):
            raise ValueError("metadata and usage must be objects")
        for name in ("model_calls", "input_tokens", "output_tokens", "cost_usd"):
            value = self.usage.get(name)
            if value is not None and (type(value) not in (int, float) or value < 0 or not math.isfinite(value)):
                raise ValueError(f"invalid resolver usage {name}")
        fingerprint(self.to_dict())

    def to_dict(self):
        return asdict(self)


class SkillResolver(Protocol):
    async def resolve(self, request: ResolveSkillRequest) -> ResolvedSkill: ...
