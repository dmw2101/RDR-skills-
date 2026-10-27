"""A working copy of a compiled skill that grows through engine-checked MCRDR revisions."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from rdr_engine import Accept, Add, RDREngine, Replace, Revise, Stop
from rdr_engine._json import key
from rdr_engine.learned import (
    CONTEXT_KEY, FEATURE_PREFIX, LearnedFeature, LearnedLayer, extend_schema, load_layer, save_learned_bundle,
)
from rdr_engine.skill_cases import SkillConditionEvaluator
from rdr_engine.skill_compiler import FeatureSchema, SkillBundle
from rdr_engine.skill_renderer import SkillRenderer

from .proposals import Proposal

BUNDLE_FILES = ("initial.md", "compiled/manifest.json", "compiled/ir.json", "compiled/fragments.json",
                "compiled/tree.json", "compiled/learned.json")


class ProposalAuthor:
    """Offer exactly the verifier's conjunction; the engine checks it against cornerstones."""

    def __init__(self, conditions):
        self.conditions = list(conditions)

    def generate_candidates(self, case, cornerstones):
        return list(self.conditions)

    def select_condition(self, candidates, case, cornerstones, request, attempt):
        return list(self.conditions)


@dataclass
class InsertResult:
    status: str  # "revised" or "unresolved"
    reason: str | None = None
    new_node_ids: list[int] = field(default_factory=list)
    fragment_id: str | None = None
    parent_id: int | None = None
    judge_calls: int = 0

    def to_dict(self):
        return dict(self.__dict__)


def copy_bundle(source, target):
    """Copy only the files a skill bundle needs; never overwrite an existing version."""
    source, target = Path(source), Path(target)
    if target.exists():
        raise ValueError(f"version directory already exists: {target}")
    for name in BUNDLE_FILES:
        if (source / name).exists():
            (target / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, target / name)
    return target


class SkillWorkspace:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        compiled = self.directory / "compiled"
        manifest = json.loads((compiled / "manifest.json").read_text(encoding="utf-8"))
        self.schema = FeatureSchema.model_validate(manifest["feature_schema"])
        self.engine, self.catalog = SkillBundle.load(self.directory, SkillConditionEvaluator(self.schema))
        self.layer = load_layer(compiled, manifest) or LearnedLayer()

    # -- cases -----------------------------------------------------------------------
    def case_for(self, observed_case, context_text):
        """A stored case: observed base features, judged learned features, and its context ID.

        Learned values judged at run time (present in the observed case) are recorded
        as judgments for that context; features never judged there read "unknown".
        """
        context_id = self.layer.add_context(context_text)  # Already the judge's bounded text.
        for name in self.layer.features:
            if observed_case is not None and observed_case.get(name) in ("true", "false", "unknown"):
                self.layer.judgments.setdefault(context_id, {}).setdefault(name, observed_case[name])
        judged = self.layer.judgments.get(context_id, {})
        case = {}
        for name, spec in self.schema.features.items():
            if name.startswith(FEATURE_PREFIX):
                case[name] = judged.get(name, "unknown")
            elif observed_case is not None and name in observed_case:
                case[name] = observed_case[name]
            else:
                case[name] = [] if spec.multiple else "unknown"
        case[CONTEXT_KEY] = context_id
        SkillConditionEvaluator(self.schema).validate_case({k: v for k, v in case.items() if k != CONTEXT_KEY})
        return case

    def stored_cases(self):
        seen, cases = set(), []
        for node in self.engine.nodes:
            for example in node.evidence:
                if key(example) not in seen:
                    seen.add(key(example))
                    cases.append(example)
        return cases

    async def accept(self, observed_case, context_text, judge=None):
        """Store a case from a passing episode as a cornerstone of every node it reaches.

        With a judge, learned features still unjudged for this context are judged
        first, so the accepted output never rests on an unjudged "unknown".
        """
        case = self.case_for(observed_case, context_text)
        calls = 0
        if judge is not None and self.layer.features:
            calls = await self._ensure_judged([case[CONTEXT_KEY]], sorted(self.layer.features), judge)
            case = self.case_for(observed_case, context_text)
        result = self.engine.learn(self.engine.predict(case), Accept())
        return result.status == "accepted", calls

    def render(self, case):
        visible = {k: v for k, v in case.items() if k != CONTEXT_KEY}
        return SkillRenderer().render(self.engine.predict(visible), self.engine, self.catalog)

    # -- learned features ----------------------------------------------------------------
    def _rebuild(self, nodes=None, author=None):
        data = self.engine.to_dict()
        if nodes is not None:
            data["nodes"] = nodes
        self.engine = RDREngine.from_dict(data, evaluator=SkillConditionEvaluator(self.schema), author=author)

    def _add_features(self, features):
        for feature in features:
            existing = self.layer.features.get(feature.name)
            spec = LearnedFeature(description=feature.description, examples=feature.examples,
                                  counterexamples=feature.counterexamples)
            if existing is not None and existing.description != spec.description:
                raise ValueError(f"learned feature {feature.name} already exists with another definition")
            if existing is None and feature.name in self.schema.features:
                raise ValueError(f"feature name {feature.name} is already in the skill schema")
            self.layer.features.setdefault(feature.name, spec)
        self.schema = extend_schema(self.schema, self.layer)
        # Unjudged stored cases carry "unknown" until a rule under their parent needs them.
        nodes = self.engine.to_dict()["nodes"]
        for node in nodes:
            for example in node["evidence"]:
                for name in self.layer.features:
                    example.setdefault(name, "unknown")
        self._rebuild(nodes)

    async def _ensure_judged(self, context_ids, names, judge):
        """Judge missing (context, feature) pairs and refresh stored evidence with the values."""
        calls = 0
        definitions = {name: self.layer.features[name].model_dump() for name in names}
        for context_id in context_ids:
            missing = [n for n in names if n not in self.layer.judgments.get(context_id, {})]
            if not missing:
                continue
            values, _ = await judge.judge({n: definitions[n] for n in missing},
                                          self.layer.contexts[context_id])
            calls += 1
            self.layer.judgments.setdefault(context_id, {}).update(values)
        if calls:
            def outputs():
                return [[item.conclusion for item in self.engine.predict(case).conclusions]
                        for case in self.stored_cases()]
            before = outputs()
            nodes = self.engine.to_dict()["nodes"]
            for node in nodes:
                for example in node["evidence"]:
                    example.update(self.layer.judgments.get(example.get(CONTEXT_KEY), {}))
            previous = self.engine
            self._rebuild(nodes)
            # Values judged now may only matter to rules not inserted yet.
            if outputs() != before:
                self.engine = previous
                raise ValueError("judging a stored case changed an existing output")
        return calls

    # -- proposals -----------------------------------------------------------------------
    async def apply(self, proposal: Proposal, observed_case, context_text, judge, provenance):
        """Insert one proposal for the failing case; leave everything unchanged unless revised."""
        saved = (self.engine.to_dict(), self.layer.model_copy(deep=True), self.schema, self.catalog)
        try:
            result = await self._apply(proposal, observed_case, context_text, judge, provenance)
        except Exception as exc:
            result = InsertResult("unresolved", f"{type(exc).__name__}: {exc}")
        if result.status != "revised":
            # Roll back everything, judgments included: stored evidence must match them.
            data, self.layer, self.schema, self.catalog = saved
            self.engine = RDREngine.from_dict(data, evaluator=SkillConditionEvaluator(self.schema))
        return result

    async def _apply(self, proposal, observed_case, context_text, judge, provenance):
        if proposal.new_features:
            self._add_features(proposal.new_features)
        conditions = proposal.predicates()
        names = sorted({c["field"] for c in conditions if c["field"].startswith(FEATURE_PREFIX)})
        unknown = [c["field"] for c in conditions if c["field"] not in self.schema.features]
        if unknown:
            return InsertResult("unresolved", f"unknown condition features: {unknown}")
        case = self.case_for(observed_case, context_text)
        prediction = self.engine.predict(case)
        parent = proposal.target_node_id
        if parent >= len(self.engine.nodes):
            return InsertResult("unresolved", f"node {parent} does not exist")
        if parent not in prediction.fired:
            return InsertResult("unresolved", f"node {parent} did not fire for the failing case")
        # The engine compares the rule against every stored case that reaches the parent.
        contexts = [case[CONTEXT_KEY]] + [
            example[CONTEXT_KEY] for example in self.stored_cases()
            if CONTEXT_KEY in example and parent in self.engine.predict(example).fired]
        calls = await self._ensure_judged(list(dict.fromkeys(contexts)), names, judge) if names else 0
        case = self.case_for(observed_case, context_text)
        for condition in conditions:
            if not SkillConditionEvaluator(self.schema).evaluate(condition, case):
                return InsertResult("unresolved", f"condition {condition} is not true for the failing "
                                    "context (learned features must be judged true there)", judge_calls=calls)
        fragment_id = None
        if proposal.guidance_text:
            anchor = proposal.anchor_fragment_id or self._default_anchor(proposal, parent)
            fragment_id = self.layer.add_fragment(proposal.guidance_text, anchor, self.catalog,
                                                  provenance).id
            self.catalog = type(self.catalog)(self.catalog.fragments, self.catalog.skill_input,
                                              self.catalog.ir, tuple(self.layer.fragments))
        self._rebuild(author=ProposalAuthor(conditions))
        prediction = self.engine.predict(case)
        if proposal.kind == "add":
            change = Add({"fragments": [fragment_id]}, parent)
        elif proposal.kind == "stop":
            change = Stop(parent)
        else:
            current = self.engine.nodes[parent].conclusion
            fragments = [f for f in (current or {}).get("fragments", [])
                         if f not in proposal.remove_fragment_ids]
            missing = set(proposal.remove_fragment_ids) - set((current or {}).get("fragments", []))
            if missing:
                return InsertResult("unresolved", f"node {parent} does not render {sorted(missing)}",
                                    judge_calls=calls)
            change = Replace(parent, {"fragments": fragments + ([fragment_id] if fragment_id else [])})
        try:
            update = self.engine.learn(prediction, Revise((change,)))
        except ValueError as exc:  # Invalid feedback, e.g. a target that did not conclude.
            return InsertResult("unresolved", str(exc), judge_calls=calls)
        finally:
            self._rebuild()  # Drop the per-proposal author.
        if update.status != "revised":
            return InsertResult("unresolved", update.reason, judge_calls=calls)
        if fragment_id is not None and fragment_id not in self.render(case).selected_ids:
            return InsertResult("unresolved", "the new guidance does not render for the failing case",
                                judge_calls=calls)
        return InsertResult("revised", None, list(update.new_node_ids), fragment_id, parent, calls)

    def _default_anchor(self, proposal, node_id):
        """Place guidance after the target's last fragment, or at the end for the root."""
        conclusion = self.engine.nodes[node_id].conclusion or {}
        fragments = [f for f in conclusion.get("fragments", []) if not f.startswith("learned_")]
        return fragments[-1] if fragments else None

    def save(self):
        save_learned_bundle(self.directory, self.engine.to_dict(), self.layer, self.schema)
        # Reload through the normal checked path, so a saved version is always servable.
        SkillBundle.load(self.directory)

    def summary(self):
        return {"nodes": len(self.engine.nodes), "revision": self.engine.revision,
                "learned_fragments": len(self.layer.fragments), "learned_features": sorted(self.layer.features),
                "stored_cases": len(self.stored_cases())}
