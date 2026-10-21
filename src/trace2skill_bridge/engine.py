"""Engine boundary and explicitly nonproduction reference traversal/composition."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ._json import ValidationError, fields
from .contracts import Expression, FragmentDelta, Truth


class EngineCompatibilityError(RuntimeError):
    pass


@dataclass(frozen=True)
class EngineCapabilities:
    backend: str
    matched_nodes: bool
    inherited_contributions: bool
    fragment_deltas: bool
    sibling_conflict_reporting: bool
    production: bool

    @property
    def contract_supported(self):
        return all((self.matched_nodes, self.inherited_contributions, self.fragment_deltas, self.sibling_conflict_reporting))

    def to_dict(self):
        return dict(vars(self), contract_supported=self.contract_supported)


@dataclass(frozen=True)
class RuleNode:
    id: int
    parent_id: int | None
    when: Expression
    delta: FragmentDelta

    def __post_init__(self):
        if type(self.id) is not int or self.id < 0:
            raise ValidationError("Node ID must be a nonnegative integer")
        if self.parent_id is not None and (type(self.parent_id) is not int or self.parent_id < 0):
            raise ValidationError("Parent ID must be a nonnegative integer or null")
        if not isinstance(self.when, Expression) or not isinstance(self.delta, FragmentDelta):
            raise ValidationError("Invalid node expression or fragment delta")

    @classmethod
    def from_dict(cls, data):
        fields(data, ("id", "parent_id", "when", "delta"))
        return cls(data["id"], data["parent_id"], Expression.from_dict(data["when"]), FragmentDelta.from_dict(data["delta"]))

    def to_dict(self):
        return {"id": self.id, "parent_id": self.parent_id, "when": self.when.to_dict(), "delta": self.delta.to_dict()}


@dataclass(frozen=True)
class NodeMatch:
    node: RuleNode
    status: Truth
    evaluated: bool

    def to_dict(self):
        return {"node_id": self.node.id, "parent_id": self.node.parent_id,
                "status": self.status.value, "evaluated": self.evaluated, "delta": self.node.delta.to_dict()}


@dataclass(frozen=True)
class CompositionResult:
    status: str
    fragments: tuple[str, ...]
    matched_node_ids: tuple[int, ...]
    conflicts: tuple[dict, ...] = ()

    def to_dict(self):
        return {"status": self.status, "fragment_ids": list(self.fragments),
                "matched_node_ids": list(self.matched_node_ids), "conflicts": list(self.conflicts)}


class EngineAdapter(Protocol):
    def capabilities(self) -> EngineCapabilities: ...
    def traverse(self, judgments: dict[str, Truth]) -> tuple[NodeMatch, ...]: ...


def _validate_tree(nodes):
    if not nodes or any(not isinstance(n, RuleNode) for n in nodes):
        raise ValidationError("A fixture requires RuleNode records")
    by_id = {node.id: node for node in nodes}
    if len(by_id) != len(nodes):
        raise ValidationError("Duplicate node ID")
    roots = [n for n in nodes if n.parent_id is None]
    if len(roots) != 1 or roots[0].id != 0 or roots[0].when.to_dict() != {"all": []}:
        raise ValidationError("Expected one unconditional root with ID 0")
    children = {node.id: [] for node in nodes}
    for node in nodes:
        if node.parent_id is not None:
            if node.parent_id not in by_id:
                raise ValidationError("Missing parent node")
            children[node.parent_id].append(node.id)
    seen = set()
    stack = [0]
    while stack:
        node_id = stack.pop()
        if node_id in seen:
            raise ValidationError("Cycle in fixture tree")
        seen.add(node_id)
        stack.extend(children[node_id])
    if seen != set(by_id):
        raise ValidationError("Unreachable or cyclic fixture nodes")
    return by_id, children


def compose_fragments(matches: tuple[NodeMatch, ...]) -> CompositionResult:
    """Specific descendants override ancestors; incomparable opposing edits conflict.

    For each fragment, keep the most-specific contributing nodes. If those
    independent nodes disagree, return no usable partial fragment selection.
    This is a reference contract, not a change to the upstream learning engine.
    """
    by_id, _ = _validate_tree(tuple(match.node for match in matches))
    statuses = {match.node.id: match.status for match in matches}
    for match in matches:
        if not isinstance(match.status, Truth) or type(match.evaluated) is not bool:
            raise ValidationError("Invalid node match")
        if match.status is Truth.TRUE:
            if not match.evaluated:
                raise ValidationError("An unevaluated node cannot match")
            parent = match.node.parent_id
            if parent is not None and statuses[parent] is not Truth.TRUE:
                raise ValidationError("A child cannot match without a true parent")

    ancestors = {}
    for node in by_id.values():
        lineage = set()
        parent = node.parent_id
        while parent is not None:
            lineage.add(parent)
            parent = by_id[parent].parent_id
        ancestors[node.id] = lineage
    edits = {}
    active = [m.node for m in matches if m.status is Truth.TRUE]
    for node in active:
        for op, fragments in (("add", node.delta.add), ("remove", node.delta.remove)):
            for fragment in fragments:
                edits.setdefault(fragment, []).append((node.id, op))
    fragments, conflicts = [], []
    for fragment, events in sorted(edits.items()):
        maximal = [(node_id, op) for node_id, op in events
                   if not any(node_id in ancestors[other] for other, _ in events)]
        if len({op for _, op in maximal}) > 1:
            conflicts.append({"fragment_id": fragment, "reason": "incompatible_independent_branches",
                              "operations": [{"node_id": n, "operation": op} for n, op in sorted(maximal)]})
        elif maximal[0][1] == "add":
            fragments.append(fragment)
    return CompositionResult("unresolved" if conflicts else "resolved", () if conflicts else tuple(fragments),
                             tuple(node.id for node in active), tuple(conflicts))


class FixtureEngineAdapter:
    """Reference traversal for staged trees; never the production learning backend."""

    def __init__(self, nodes):
        self.nodes = tuple(nodes)
        self._by_id, self._children = _validate_tree(self.nodes)

    def capabilities(self):
        return EngineCapabilities("reference_fixture", True, True, True, True, False)

    def traverse(self, judgments):
        missing = set().union(*(n.when.condition_ids for n in self.nodes)) - set(judgments)
        if missing:
            raise ValidationError(f"Missing tree condition judgments: {sorted(missing)}")
        matches = []
        stack = [(0, True)]
        while stack:
            node_id, parent_true = stack.pop()
            node = self._by_id[node_id]
            status = node.when.evaluate(judgments) if parent_true else Truth.UNKNOWN
            matches.append(NodeMatch(node, status, parent_true))
            stack.extend((child, status is Truth.TRUE) for child in reversed(self._children[node_id]))
        return tuple(matches)

    def to_dict(self):
        return {"kind": "authored_reference_fixture", "nodes": [n.to_dict() for n in self.nodes]}

    @classmethod
    def from_dict(cls, data):
        fields(data, ("kind", "nodes"))
        if data["kind"] != "authored_reference_fixture" or type(data["nodes"]) is not list:
            raise ValidationError("Only explicitly authored fixture trees are accepted")
        return cls(RuleNode.from_dict(n) for n in data["nodes"])


def inspect_upstream():
    """Execute a small read-only conformance probe, without importing the LLM compiler."""
    from rdr_engine import RDREngine

    class ProbeEvaluator:
        def evaluate(self, condition, case):
            raise AssertionError("Unconditional probe nodes should not evaluate predicates")

    def node(node_id, children, fragments):
        return {"id": node_id, "conditions": [], "conclusion": {"add": fragments, "remove": []},
                "stopping": False, "evidence": [], "children": children}
    try:
        engine = RDREngine.from_dict({"schema_version": RDREngine.SCHEMA_VERSION, "root_id": 0,
                    "revision": 0, "max_selection_attempts": 3,
                    "nodes": [node(0, [1], []), node(1, [2], ["parent_guidance"]), node(2, [], ["child_guidance"])]},
                    evaluator=ProbeEvaluator())
        prediction = engine.predict({"probe": "inheritance"})
        fired = list(prediction.fired)
        reported = [item.node_id for item in prediction.conclusions]
        inherited = {1, 2} <= set(reported)
        observed = {"fired_node_ids": fired, "reported_conclusion_node_ids": reported}
    except (AttributeError, TypeError, ValueError) as exc:
        fired, inherited = [], False
        observed = {"probe_error": str(exc)}
    capabilities = EngineCapabilities("upstream_mcrdr", bool(fired), inherited, False, False, False)
    reasons = []
    if not inherited:
        reasons.append("The inheritance probe does not report both parent and child contributions")
    reasons.append("An upstream add/remove renderer and sibling-conflict contract have not been adapted or verified")
    return {"status": "supported" if capabilities.contract_supported else "unsupported",
            "capabilities": capabilities.to_dict(), "probe": observed, "reasons": reasons,
            "scope": "read-only probe; no learning, skill changes, or model calls"}


class UpstreamEngineAdapter:
    def capabilities(self):
        return EngineCapabilities(**{k: v for k, v in inspect_upstream()["capabilities"].items() if k != "contract_supported"})

    def traverse(self, judgments):
        raise EngineCompatibilityError("Upstream integration is blocked until inherited fragment deltas and conflict handling pass conformance")
