"""Strict JSON snapshots; no domain logic and no executable serialization."""

from __future__ import annotations

import json
import math

from .types import InvalidPayloadError, JSONValue


def clone(value: JSONValue) -> JSONValue:
    active: set[int] = set()

    def visit(item: JSONValue) -> JSONValue:
        if item is None or type(item) in (bool, int, str):
            return item
        if type(item) is float:
            if not math.isfinite(item):
                raise InvalidPayloadError("JSON numbers must be finite")
            return item
        if type(item) not in (list, dict):
            raise InvalidPayloadError("Payloads must use JSON types and string object keys")
        identity = id(item)
        if identity in active:
            raise InvalidPayloadError("Circular payloads are not JSON-compatible")
        active.add(identity)
        try:
            if isinstance(item, list):
                return [visit(child) for child in item]
            if any(type(key) is not str for key in item):
                raise InvalidPayloadError("JSON object keys must be strings")
            return {key: visit(child) for key, child in item.items()}
        finally:
            active.remove(identity)

    try:
        return visit(value)
    except RecursionError as error:
        raise InvalidPayloadError("Payload nesting exceeds the Python recursion limit") from error


def key(value: JSONValue) -> str:
    return json.dumps(clone(value), sort_keys=True, separators=(",", ":"), allow_nan=False)
