"""OpenAI-compatible Chat Completions; no SDK or RDR dependency."""

import asyncio
import os
import time
import weakref
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .http import HTTPStatusError, json_request

# Rate limits and temporary server errors; refused requests are not billed, so retrying is free.
RETRY_STATUSES = frozenset({429, 502, 503, 504})


@dataclass(frozen=True)
class ModelResponse:
    message: dict
    usage: dict
    metadata: dict


class ModelClient(Protocol):
    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse: ...


class OpenAICompatibleClient:
    # Seconds to wait before each retry of a call refused with RETRY_STATUSES; a longer
    # Retry-After from the provider (up to MAX_RETRY_WAIT) is honoured.
    retry_delays = (5, 15, 30, 60)
    MAX_RETRY_WAIT = 120

    def __init__(self, *, base_url, model, api_key_env="MODEL_API_KEY", temperature=0,
                 max_tokens=2048, seed=None, timeout=90, provider=None, **unknown):
        if unknown:
            raise ValueError(f"unknown model settings: {sorted(unknown)}")
        if provider is not None and not isinstance(provider, dict):
            raise ValueError("provider settings must be an object")
        parsed = urlparse(base_url)
        local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
            raise ValueError("model endpoint must use HTTPS (or localhost HTTP)")
        if api_key_env is None and not local:
            raise ValueError("keyless model endpoints must be localhost")
        key = os.environ.get(api_key_env) if api_key_env else None
        if not model or (api_key_env is not None and not key):
            raise ValueError(f"set model and environment variable {api_key_env}")
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model, self.key = model, key
        self.temperature, self.max_tokens = temperature, max_tokens
        self.seed, self.timeout = seed, timeout
        self.provider = dict(provider) if provider is not None else None

    async def complete(self, messages, tools):
        payload = {"model": self.model, "messages": messages,
                   "temperature": self.temperature, "max_tokens": self.max_tokens}
        if tools:
            payload.update(tools=tools, tool_choice="auto")
        if self.seed is not None:
            payload["seed"] = self.seed
        if self.provider is not None:
            payload["provider"] = self.provider
        data, retries, timing = await self._post(payload)
        choice = data["choices"][0]
        original = choice["message"]
        # Preserve tool-call structure and provider-specific assistant fields.
        message = {k: v for k, v in original.items() if k != "role"}
        message["role"] = "assistant"
        usage = data.get("usage") or {}
        details = usage.get("prompt_tokens_details") or {}
        return ModelResponse(message, {
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
            "cached_input_tokens": details.get("cached_tokens"),
            "cost_usd": usage.get("cost"),
        }, {"id": data.get("id"), "model": data.get("model"), "provider": data.get("provider"),
            "system_fingerprint": data.get("system_fingerprint"),
            "finish_reason": choice.get("finish_reason"), "provider_usage": usage, "retries": retries,
            **timing})

    async def _post(self, payload):
        """POST once, retrying rate limits and temporary server errors; returns (data, retries, timing).

        timing: the successful request's own wall time (``latency_seconds``) and the time spent
        waiting before retries (``retry_wait_seconds``), so a call's latency never includes
        provider back-off. Returned per call, so concurrent calls on one client never mix.
        """
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        waited = 0.0
        for attempt in range(len(self.retry_delays) + 1):
            try:
                started = time.monotonic()
                data = await asyncio.to_thread(json_request, self.url, payload, headers=headers,
                                               timeout=self.timeout)
                return data, attempt, {"latency_seconds": time.monotonic() - started, "retry_wait_seconds": waited}
            except HTTPStatusError as exc:
                if exc.status not in RETRY_STATUSES or attempt == len(self.retry_delays):
                    raise
                wait = self.retry_delays[attempt]
                if exc.retry_after is not None:
                    wait = max(wait, min(exc.retry_after, self.MAX_RETRY_WAIT))
                print(f"model call refused ({exc}); retry {attempt + 1}/{len(self.retry_delays)} "
                      f"in {wait:g}s", flush=True)
                await asyncio.sleep(wait)
                waited += wait


# -- local model servers that take turns on one GPU ---------------------------------------------


def _server_root(url):
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _is_sleeping(server):
    """vLLM's sleep state (its /is_sleeping endpoint needs VLLM_SERVER_DEV_MODE=1)."""
    return bool(json_request(server + "/is_sleeping", timeout=30).get("is_sleeping"))


def _control(server, path):
    """POST a vLLM control endpoint (/sleep, /wake_up); they answer with an empty body."""
    with urlopen(Request(server + path, b"", {"User-Agent": "rdr-skills-evaluation/0.1"}, method="POST"),
                 timeout=900) as response:
        response.read()


def _hand_over(server, peers):
    """Put every awake peer to sleep (freeing its GPU memory), then wake ``server``.

    Returns whether anything was actually put to sleep or woken.
    """
    changed = False
    for peer in peers:
        if not _is_sleeping(peer):
            _control(peer, "/sleep?level=1")
            changed = True
    if _is_sleeping(server):
        _control(server, "/wake_up")
        changed = True
    if _is_sleeping(server):
        raise RuntimeError(f"model server {server} did not wake up")
    return changed


class _GpuTurns:
    """Which local model server holds the GPU; shared by every SwitchingClient on one event loop.

    A switch waits until no call to another server is in flight, so nothing is cut off; calls
    to the server that is awake run concurrently as usual.
    """

    def __init__(self):
        self.awake, self.active, self.condition = None, {}, asyncio.Condition()

    async def acquire(self, server, peers):
        switched = 0.0
        async with self.condition:
            while not (self.awake == server and not await asyncio.to_thread(_is_sleeping, server)):
                if any(count and other != server for other, count in self.active.items()):
                    await self.condition.wait()
                    continue
                started = time.monotonic()
                if await asyncio.to_thread(_hand_over, server, peers):  # Checking alone is not a switch.
                    switched += time.monotonic() - started
                self.awake = server
            self.active[server] = self.active.get(server, 0) + 1
        return switched

    async def release(self, server):
        async with self.condition:
            self.active[server] -= 1
            self.condition.notify_all()


_TURNS = weakref.WeakKeyDictionary()  # event loop -> _GpuTurns


class SwitchingClient(OpenAICompatibleClient):
    """An OpenAICompatibleClient for local vLLM servers that share one GPU with ``peers``.

    Before each call its server is woken and every peer put to sleep (vLLM sleep mode,
    level 1: weights move to CPU memory). The switch time is reported separately as
    ``switch_seconds`` and never counted in ``latency_seconds``. See
    docs/learning-design-decisions.md (G1).
    """

    def __init__(self, *, peers, **options):
        super().__init__(**options)
        self.server = _server_root(self.url)
        self.peers = [_server_root(peer) for peer in peers]
        hosts = {urlparse(url).hostname for url in [self.server, *self.peers]}
        if not hosts <= {"localhost", "127.0.0.1", "::1"} or self.server in self.peers:
            raise ValueError("SwitchingClient takes turns only between distinct local servers")

    async def complete(self, messages, tools):
        loop = asyncio.get_running_loop()
        turns = _TURNS.setdefault(loop, _GpuTurns())
        switched = await turns.acquire(self.server, self.peers)
        try:
            response = await super().complete(messages, tools)
        finally:
            await turns.release(self.server)
        response.metadata["switch_seconds"] = switched
        return response
