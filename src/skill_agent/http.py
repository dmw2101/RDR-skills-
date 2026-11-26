"""Small JSON HTTP transport with bounded responses and no automatic retries."""

import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class HTTPStatusError(RuntimeError):
    """A non-2xx response. The message never echoes the body, which may contain private inputs;
    the body's start is kept in ``body`` for callers that decide to record it (the judge, J7)."""

    def __init__(self, status, host, retry_after=None, body=""):
        super().__init__(f"HTTP {status} from {host}")
        self.status, self.retry_after, self.body = status, retry_after, body


def _error_body(error, limit=1000):
    """The start of an error response's body as text, or "" when it cannot be read."""
    try:
        return error.read(limit).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - the status is what matters; a missing body is not an error.
        return ""


def _retry_after(headers):
    """Seconds from a numeric Retry-After header, or None."""
    try:
        return max(0.0, float((headers or {}).get("Retry-After")))
    except (TypeError, ValueError):
        return None


def json_request(url, payload=None, *, headers=None, timeout=60, max_bytes=8_000_000):
    body = None if payload is None else json.dumps(payload, allow_nan=False).encode()
    # Identify our client explicitly; Groq rejects urllib's default signature.
    request = Request(url, body, {
        "Content-Type": "application/json",
        "User-Agent": "rdr-skills-evaluation/0.1",
        **(headers or {}),
    })
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(max_bytes + 1)
    except HTTPError as error:
        raise HTTPStatusError(error.code, request.host, _retry_after(error.headers), _error_body(error)) from None
    if len(raw) > max_bytes:
        raise ValueError("HTTP response exceeds size limit; nothing was silently truncated")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("expected a JSON object response")
    return result
