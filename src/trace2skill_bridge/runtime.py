"""Validate once, then pass a fixed set of judgments to a read-only adapter."""

from .engine import EngineCompatibilityError, UpstreamEngineAdapter, compose_fragments, inspect_upstream
from .evaluation import evaluate_context


def run_context(context, registry, *, provider=None, adapter=None):
    evaluated = evaluate_context(context, registry, provider=provider)
    adapter = adapter if adapter is not None else UpstreamEngineAdapter()
    capabilities = adapter.capabilities()
    result = {"evaluation": evaluated.to_dict(), "capabilities": capabilities.to_dict(),
              "model_calls": 0, "production_ready": False}
    if not capabilities.contract_supported:
        result.update(status="blocked_upstream", traversal=None, composition=None,
                      compatibility=inspect_upstream())
        return result
    try:
        matches = adapter.traverse(evaluated.statuses())
    except EngineCompatibilityError as exc:
        result.update(status="blocked_upstream", traversal=None, composition=None, reason=str(exc))
        return result
    composition = compose_fragments(matches)
    result.update(status="reference_resolved" if composition.status == "resolved" else "unresolved",
                  traversal=[m.to_dict() for m in matches], composition=composition.to_dict())
    return result
