"""Small JSON helpers for inputs, recordings, and experiment logs."""

from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path


class ValidationError(ValueError):
    """Invalid bridge input. Never triggers a model repair."""


def canonical(value):
    def check(item):
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is list:
            for child in item:
                check(child)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                check(child)
            return
        raise ValidationError("Expected strict JSON, without nonfinite numbers or Python objects")
    check(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def loads(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValidationError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result
    def bad_constant(value):
        raise ValidationError(f"Invalid JSON constant: {value}")
    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=bad_constant)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValidationError(f"Invalid JSON: {exc}") from exc


def read(path):
    return loads(Path(path).read_text(encoding="utf-8-sig"))


def write(path, value):
    """Replace one artifact atomically (skill state keeps related data in one file)."""
    canonical(value)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def fields(data, required, optional=()):
    if type(data) is not dict:
        raise ValidationError("Expected a JSON object")
    if not set(required) <= set(data) or set(data) - set(required) - set(optional):
        raise ValidationError(f"Expected fields {sorted(required)}, optional {sorted(optional)}; got {sorted(data)}")


def text(value, name):
    if type(value) is not str or not value.strip():
        raise ValidationError(f"{name} must be nonempty text")
    return value


def strings(value, name, *, nonempty=False, unique=True):
    if type(value) is not list or any(type(item) is not str or not item.strip() for item in value):
        raise ValidationError(f"{name} must be a list of nonempty strings")
    if nonempty and not value:
        raise ValidationError(f"{name} must not be empty")
    if unique and len(value) != len(set(value)):
        raise ValidationError(f"{name} must not contain duplicates")
    return tuple(value)
