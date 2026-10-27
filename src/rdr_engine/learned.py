"""Learned guidance and judged features layered over an exact compiled skill.

The compiled source fragments stay verbatim. Trace-driven learning adds new
guidance text as learned fragments, positioned after an existing anchor
fragment, and new conditions as LLM-judged features whose values are
true/false/unknown. Stored cornerstone cases carry an opaque ``_context`` ID
whose visible text lives here, so later features can be judged on them too.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from pydantic import Field, model_validator

from .skill_compiler import (
    ARTIFACT_VERSION, FeatureSchema, FeatureSpec, FragmentCatalog, StrictModel, _json_bytes, _sha256,
)

LEARNED_VERSION = 1
LEARNED_FILE = "learned.json"
FEATURE_PREFIX = "lf_"
JUDGED_VALUES = ["true", "false", "unknown"]
CONTEXT_KEY = "_context"


class LearnedFeature(StrictModel):
    description: str = Field(min_length=1)
    examples: list[str] = Field(default_factory=list)
    counterexamples: list[str] = Field(default_factory=list)


class LearnedFragment(StrictModel):
    id: str = Field(pattern=r"^learned_[0-9a-f]{16}$")
    text: str = Field(min_length=1)
    anchor: str | None
    requires: list[str]
    provenance: dict[str, Any]


class LearnedLayer(StrictModel):
    schema_version: int = LEARNED_VERSION
    features: dict[str, LearnedFeature] = Field(default_factory=dict)
    fragments: list[LearnedFragment] = Field(default_factory=list)
    contexts: dict[str, str] = Field(default_factory=dict)
    # context ID -> feature -> judged value; a missing entry means "not judged yet".
    judgments: dict[str, dict[str, str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_shape(self) -> LearnedLayer:
        if self.schema_version != LEARNED_VERSION:
            raise ValueError(f"learned layer version must be {LEARNED_VERSION}")
        for name in self.features:
            if re.fullmatch(FEATURE_PREFIX + r"[a-z0-9_]+", name) is None:
                raise ValueError(f"learned feature names must match {FEATURE_PREFIX}<snake_case>: {name!r}")
        ids = [fragment.id for fragment in self.fragments]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate learned fragment ID")
        for fragment in self.fragments:
            if fragment.id != learned_fragment_id(fragment.text, fragment.anchor):
                raise ValueError(f"learned fragment {fragment.id} does not match its text")
        for context_id, text in self.contexts.items():
            if context_id != context_fingerprint(text):
                raise ValueError(f"context {context_id} does not match its text")
        for context_id, values in self.judgments.items():
            if context_id not in self.contexts:
                raise ValueError(f"judgments refer to unknown context {context_id}")
            for name, value in values.items():
                if name not in self.features or value not in JUDGED_VALUES:
                    raise ValueError(f"invalid judgment {name}={value!r} for {context_id}")
        return self

    def check_against(self, catalog: FragmentCatalog, schema: FeatureSchema) -> None:
        """Anchors and dependencies must name base fragments; features must be in the schema."""
        base = {fragment.id: fragment for fragment in catalog.fragments}
        for fragment in self.fragments:
            if fragment.anchor is not None and fragment.anchor not in base:
                raise ValueError(f"learned fragment {fragment.id} has unknown anchor {fragment.anchor}")
            for required in fragment.requires:
                if required not in base or base[required].role != "non_instruction":
                    raise ValueError(f"learned fragment {fragment.id} has invalid dependency {required}")
        for name in self.features:
            if schema.features.get(name) != feature_spec(self.features[name]):
                raise ValueError(f"learned feature {name} is missing from the bundle feature schema")

    def add_fragment(self, text: str, anchor: str | None, catalog: FragmentCatalog,
                     provenance: dict[str, Any]) -> LearnedFragment:
        """Add (or reuse) guidance text; it inherits the anchor's heading dependencies."""
        text = text.strip("\r\n") + "\n"
        identifier = learned_fragment_id(text, anchor)
        for fragment in self.fragments:
            if fragment.id == identifier:
                return fragment
        base = {fragment.id: fragment for fragment in catalog.fragments}
        if anchor is not None and anchor not in base:
            raise ValueError(f"unknown anchor fragment {anchor}")
        requires = list(base[anchor].requires) if anchor is not None else []
        if anchor is not None and base[anchor].role == "non_instruction":
            requires.append(anchor)  # Anchored directly under a heading.
        fragment = LearnedFragment(id=identifier, text=text, anchor=anchor,
                                   requires=list(dict.fromkeys(requires)), provenance=provenance)
        self.fragments.append(fragment)
        return fragment

    def add_context(self, text: str) -> str:
        identifier = context_fingerprint(text)
        self.contexts.setdefault(identifier, text)
        return identifier


def learned_fragment_id(text: str, anchor: str | None) -> str:
    digest = hashlib.sha256(f"{anchor or ''}:".encode() + text.encode()).hexdigest()[:16]
    return f"learned_{digest}"


def context_fingerprint(text: str) -> str:
    return "ctx_" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def feature_spec(feature: LearnedFeature) -> FeatureSpec:
    return FeatureSpec(description=feature.description, values=list(JUDGED_VALUES))


def extend_schema(schema: FeatureSchema, layer: LearnedLayer) -> FeatureSchema:
    features = dict(schema.features)
    for name, feature in layer.features.items():
        if name in features and features[name] != feature_spec(feature):
            raise ValueError(f"learned feature {name} conflicts with the base schema")
        features[name] = feature_spec(feature)
    return FeatureSchema(features=features)


def load_layer(compiled: Path, manifest: dict[str, Any]) -> LearnedLayer | None:
    """Return the verified learned layer, or None for an unlearned bundle."""
    path = compiled / LEARNED_FILE
    expected = manifest.get("learned_sha256")
    if expected is None and not path.exists():
        return None
    if expected is None or not path.exists():
        raise ValueError("learned.json and the manifest disagree; rebuild or restore the learned bundle")
    data = path.read_bytes()
    if _sha256(data) != expected:
        raise ValueError("learned.json changed since learning")
    return LearnedLayer.model_validate_json(data.decode("utf-8"), strict=True)


def save_learned_bundle(skill_dir: str | Path, tree_data: dict[str, Any], layer: LearnedLayer,
                        schema: FeatureSchema) -> None:
    """Write tree, learned layer, and an updated manifest for a learned working copy."""
    compiled = Path(skill_dir).resolve() / "compiled"
    manifest = json.loads((compiled / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != ARTIFACT_VERSION:
        raise ValueError("only current skill bundles can store learned rules")
    tree_bytes = (json.dumps(tree_data, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode()
    learned_bytes = _json_bytes(layer.model_dump())
    (compiled / "tree.json").write_bytes(tree_bytes)
    (compiled / LEARNED_FILE).write_bytes(learned_bytes)
    manifest.update({
        "feature_schema": schema.model_dump(),
        "feature_schema_sha256": _sha256(_json_bytes(schema.model_dump())),
        "tree_sha256": _sha256(tree_bytes),
        "learned_sha256": _sha256(learned_bytes),
    })
    (compiled / "manifest.json").write_bytes(_json_bytes(manifest))
