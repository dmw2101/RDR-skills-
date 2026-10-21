"""Conservative placement, provisional insertion, and offline regression replay."""

from dataclasses import replace
from itertools import product
from pathlib import Path

from ._json import ValidationError, canonical, write
from .contracts import ContextSnapshot, Expression, FragmentDelta, Truth
from .engine import RuleNode, inspect_upstream
from .evaluation import RecordedResponseProvider, evaluate_context
from .patches import PatchProposal
from .skill_state import Fragment, markdown_blocks
from .trace2skill import verify_source


class NeedsReview(ValidationError):
    """A valid proposal that cannot safely be placed or expressed automatically."""


def _guards(nodes):
    by_id = {n.id: n for n in nodes}
    result = {}
    for node in nodes:
        path, current = [], node
        while current is not None:
            path.append(current.when)
            current = by_id.get(current.parent_id)
        result[node.id] = Expression("all", children=tuple(reversed(path)))
    return result


def _implies(scope, guard):
    """Only logical implication, not a guess about natural-language similarity."""
    ids = sorted(scope.condition_ids | guard.condition_ids)
    if len(ids) > 12:
        raise NeedsReview("Placement implication exceeds 12 conditions; narrow the procedure scope")
    for values in product((Truth.FALSE, Truth.TRUE), repeat=len(ids)):
        statuses = dict(zip(ids, values))
        if scope.evaluate(statuses) is Truth.TRUE and guard.evaluate(statuses) is not Truth.TRUE:
            return False
    return True


def choose_parent(state, scope, statuses):
    """Find one most-specific scope anchor. Never use incidental true features."""
    if scope.evaluate(statuses) is not Truth.TRUE:
        raise NeedsReview("The source context does not establish the procedure scope")
    guards = _guards(state.nodes)
    eligible = {m.node.id for m in state.adapter().traverse(statuses)
                if m.status is Truth.TRUE and _implies(scope, guards[m.node.id])}
    by_id = {n.id: n for n in state.nodes}
    parents = set()
    for node_id in eligible:
        parent = by_id[node_id].parent_id
        while parent is not None:
            parents.add(parent)
            parent = by_id[parent].parent_id
    anchors = sorted(eligible - parents)
    if len(anchors) != 1:
        raise NeedsReview(f"Ambiguous independent scope anchors: {anchors}; no placement guessed")
    return anchors[0]


def _add_edits(state, proposal, parent_id, statuses, node_id):
    active_text = state.render(statuses)
    if active_text != proposal.request.failure.skill_text:
        raise NeedsReview("The failed execution used different guidance; recreate the proposal against the current skill")
    selected = set()
    from .engine import compose_fragments
    selected.update(compose_fragments(state.adapter().traverse(statuses)).fragments)
    by_id = {n.id: n for n in state.nodes}
    lineage, current = set(), parent_id
    while current is not None:
        lineage.add(current)
        current = by_id[current].parent_id
    inherited = {fragment for n in state.nodes if n.id in lineage for fragment in n.delta.add} & selected
    fragments, additions, removals = list(state.fragments), [], []
    for index, edit in enumerate(proposal.edits, 1):
        if edit.file != "SKILL.md":
            raise NeedsReview("Only SKILL.md instruction edits are supported; scripts/references require review")
        target = None
        if edit.old_text.strip():
            candidates = [f for f in state.fragments if f.id in selected and f.text.strip() == edit.old_text.strip()]
            if len(candidates) != 1:
                raise NeedsReview("Replacement/removal must match exactly one whole active Markdown block")
            target = candidates[0]
            if target.id not in inherited:
                raise NeedsReview("The target instruction belongs to an independent branch, not the selected ancestry")
            if target.text.lstrip().startswith("---"):
                raise NeedsReview("Frontmatter and metadata are immutable")
            if target.id in removals:
                raise NeedsReview("Two edits target the same block")
            removals.append(target.id)
        if edit.new_text.strip():
            markdown_blocks(edit.new_text)
            if edit.new_text.lstrip().startswith("---"):
                raise NeedsReview("A learned instruction cannot introduce frontmatter")
            if any(f.text.strip() == edit.new_text.strip() and f.id not in removals for f in fragments):
                raise NeedsReview("The proposed instruction already exists; no duplicate inserted")
            fragment = Fragment(f"learned_{node_id:04d}_{index:02d}", "\n\n" + edit.new_text.strip() + "\n\n",
                                None if target is None else target.id)
            if target is None:
                fragments.append(fragment)
            else:
                fragments.insert(next(i for i, f in enumerate(fragments) if f.id == target.id) + 1, fragment)
            additions.append(fragment.id)
    return tuple(fragments), FragmentDelta(tuple(additions), tuple(removals))


def stage_patch(state, proposal, protected_recordings):
    """Return (candidate, report). Never mutate the original state or skill files.

    Replay proves guidance preservation on supplied contexts, NOT task accuracy.
    A production commit additionally needs fresh execution and a conforming engine.
    """
    report = {"status": "pending_review", "model_calls": 0, "production_ready": False,
              "task_execution": "not_run", "replay": []}
    try:
        # Revalidate loaded objects, as well as recordings, at the boundary.
        proposal = PatchProposal.from_dict(proposal.to_dict())
        verify_source(proposal.request.failure)
        if state.registry != proposal.request.registry:
            raise NeedsReview("Registry changed since proposal creation; recreate against the current registry")
        registry = proposal.extended_registry()
        statuses = {j.condition_id: j.status for j in proposal.judgments}
        if proposal.when.evaluate(statuses) is not Truth.TRUE:
            raise NeedsReview("The source context does not establish the exception condition")
        parent_id = choose_parent(state, proposal.scope, statuses)
        node_id = max(n.id for n in state.nodes) + 1
        fragments, delta = _add_edits(state, proposal, parent_id, statuses, node_id)
        guard = Expression("all", children=(proposal.scope, proposal.when))
        node = RuleNode(node_id, parent_id, guard, delta)
        candidate = replace(state, registry=registry, nodes=state.nodes + (node,), fragments=fragments)
        candidate.render(statuses)  # Includes replacement and independent-branch checks.
        effective = _guards(candidate.nodes)[node_id]
        if effective.evaluate(statuses) is not Truth.TRUE:
            raise NeedsReview("The proposed exception would not activate on its source case")
        if candidate.render(statuses) == state.render(statuses):
            raise NeedsReview("The proposed exception does not change the source guidance")
        if not protected_recordings:
            raise NeedsReview("Supply at least one protected-context replay before publishing a reference candidate")
        contexts = tuple(ContextSnapshot.from_dict(r["context"]) for r in protected_recordings)
        if any(old not in contexts for old in state.protected_contexts):
            raise NeedsReview("Replay must include every previously protected context")
        if len({canonical(c.to_dict()) for c in contexts}) != len(contexts):
            raise NeedsReview("Duplicate protected replay context")
        for index, (context, recording) in enumerate(zip(contexts, protected_recordings), 1):
            evaluation = evaluate_context(context, registry, provider=RecordedResponseProvider(recording))
            judgments = evaluation.statuses()
            # Unknown is not evidence that a protected case is excluded, even
            # when traversal currently leaves its output unchanged.
            excluded = effective.proves_exclusion(judgments)
            before, after = state.render(judgments), candidate.render(judgments)
            passed = excluded and before == after
            report["replay"].append({"case": index, "excluded": excluded, "guidance_unchanged": before == after,
                                     "passed": passed, "evaluation": evaluation.to_dict()})
            if not passed:
                raise NeedsReview("Protected context is changed or not known to be excluded; refine the condition")
        source = proposal.request.failure
        change = {"node_id": node_id, "parent_id": parent_id, "task_id": source.task_id,
                  "source": source.source, "proposal": proposal.to_dict(),
                  "validation": "recorded_guidance_replay_only"}
        candidate = replace(candidate, protected_contexts=contexts, changes=state.changes + (change,))
        report.update(status="ready_reference", parent_id=parent_id, node_id=node_id,
                      source_guidance=candidate.render(statuses), delta=delta.to_dict())
        return candidate, report
    except (ValidationError, OSError, KeyError, TypeError) as exc:
        report["reason"] = str(exc)
        return None, report


def publish_patch(state, proposal, protected_recordings, output_dir, *, engine="upstream"):
    """Publish a NEW offline candidate, or a diagnostic. Never overwrite inputs."""
    destination = Path(output_dir)
    if destination.exists():
        raise ValidationError("Insertion requires a new output directory")
    if engine not in ("upstream", "reference"):
        raise ValidationError("Unknown engine")
    if engine == "upstream":
        report = {"status": "blocked_upstream", "upstream": inspect_upstream(), "model_calls": 0,
                  "production_ready": False, "reason": "Production inherited-fragment integration is not yet available"}
        candidate = None
    else:
        candidate, report = stage_patch(state, proposal, protected_recordings)
    if candidate is not None:
        candidate.save(destination / "skill-state.json")
    write(destination / "report.json", report)
    return report
