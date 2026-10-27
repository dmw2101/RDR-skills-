"""OpenAI-compatible Chat Completions; no SDK or RDR dependency."""

import asyncio
import os
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

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
        data, retries = await self._post(payload)
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
            "finish_reason": choice.get("finish_reason"), "provider_usage": usage, "retries": retries})

    async def _post(self, payload):
        """POST once, retrying rate limits and temporary server errors; returns (data, retries)."""
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        for attempt in range(len(self.retry_delays) + 1):
            try:
                data = await asyncio.to_thread(json_request, self.url, payload, headers=headers,
                                               timeout=self.timeout)
                return data, attempt
            except HTTPStatusError as exc:
                if exc.status not in RETRY_STATUSES or attempt == len(self.retry_delays):
                    raise
                wait = self.retry_delays[attempt]
                if exc.retry_after is not None:
                    wait = max(wait, min(exc.retry_after, self.MAX_RETRY_WAIT))
                print(f"model call refused ({exc}); retry {attempt + 1}/{len(self.retry_delays)} "
                      f"in {wait:g}s", flush=True)
                await asyncio.sleep(wait)
