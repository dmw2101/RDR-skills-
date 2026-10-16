"""Small JSON HTTP transport with bounded responses and no automatic retries."""

import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen


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
        # Do not echo provider response bodies, which may contain private inputs.
        raise RuntimeError(f"HTTP {error.code} from {request.host}") from None
    if len(raw) > max_bytes:
        raise ValueError("HTTP response exceeds size limit; nothing was silently truncated")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("expected a JSON object response")
    return result
