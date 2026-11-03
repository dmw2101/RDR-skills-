"""OpenAI-compatible Chat Completions; no SDK or RDR dependency."""

import asyncio
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib.error import URLError
from urllib.parse import urlparse

from .http import HTTPRequestError, json_request


def _retry_after_seconds(value):
    """Parse either standard Retry-After form; invalid values use backoff."""
    if not value:
        return None
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except (TypeError, ValueError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


@dataclass(frozen=True)
class ModelResponse:
    message: dict
    usage: dict
    metadata: dict


class ModelClient(Protocol):
    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse: ...


class OpenAICompatibleClient:
    def __init__(self, *, base_url, model, api_key_env="MODEL_API_KEY", temperature=0,
                 max_tokens=2048, seed=None, timeout=90, provider=None,
                 rate_limit_retries=2, rate_limit_delay_seconds=60,
                 transient_retries=None, transient_delay_seconds=2,
                 max_retry_delay_seconds=120, **unknown):
        if unknown:
            raise ValueError(f"unknown model settings: {sorted(unknown)}")
        if provider is not None and not isinstance(provider, dict):
            raise ValueError("provider settings must be an object")
        if type(rate_limit_retries) is not int or rate_limit_retries < 0:
            raise ValueError("rate_limit_retries must be a nonnegative integer")
        if (type(rate_limit_delay_seconds) not in (int, float)
                or not math.isfinite(rate_limit_delay_seconds) or rate_limit_delay_seconds <= 0):
            raise ValueError("rate_limit_delay_seconds must be positive")
        if transient_retries is None:
            transient_retries = rate_limit_retries
        if type(transient_retries) is not int or transient_retries < 0:
            raise ValueError("transient_retries must be a nonnegative integer")
        if (type(transient_delay_seconds) not in (int, float)
                or not math.isfinite(transient_delay_seconds) or transient_delay_seconds <= 0):
            raise ValueError("transient_delay_seconds must be positive")
        if (type(max_retry_delay_seconds) not in (int, float)
                or not math.isfinite(max_retry_delay_seconds) or max_retry_delay_seconds <= 0):
            raise ValueError("max_retry_delay_seconds must be positive")
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
        self.rate_limit_retries = rate_limit_retries
        self.rate_limit_delay_seconds = rate_limit_delay_seconds
        self.transient_retries = transient_retries
        self.transient_delay_seconds = transient_delay_seconds
        self.max_retry_delay_seconds = max_retry_delay_seconds
        self.retry_events = []

    async def complete(self, messages, tools):
        payload = {"model": self.model, "messages": messages,
                   "temperature": self.temperature, "max_tokens": self.max_tokens}
        if tools:
            payload.update(tools=tools, tool_choice="auto")
        if self.seed is not None:
            payload["seed"] = self.seed
        if self.provider is not None:
            payload["provider"] = self.provider
        attempt = 0
        while True:
            try:
                data = await asyncio.to_thread(
                    json_request, self.url, payload,
                    headers={"Authorization": f"Bearer {self.key}"} if self.key else {}, timeout=self.timeout,
                )
                break
            except (HTTPRequestError, URLError, TimeoutError, ConnectionError) as exc:
                is_rate_limit = isinstance(exc, HTTPRequestError) and exc.status_code == 429
                is_transient_http = (isinstance(exc, HTTPRequestError)
                                     and (exc.status_code == 408 or 500 <= exc.status_code < 600))
                is_network = isinstance(exc, (URLError, TimeoutError, ConnectionError))
                if not (is_rate_limit or is_transient_http or is_network):
                    raise
                limit = self.rate_limit_retries if is_rate_limit else self.transient_retries
                if attempt >= limit:
                    raise
                base = self.rate_limit_delay_seconds if is_rate_limit else self.transient_delay_seconds
                delay = min(self.max_retry_delay_seconds, base * (2 ** attempt))
                requested = _retry_after_seconds(exc.retry_after) if isinstance(exc, HTTPRequestError) else None
                if requested is not None:
                    # Never retry before the provider's requested delay. Very long waits
                    # are reported as failures rather than exceeding the configured bound.
                    if requested > self.max_retry_delay_seconds:
                        raise
                    delay = max(delay, requested)
                attempt += 1
                reason = f"HTTP {exc.status_code}" if isinstance(exc, HTTPRequestError) else type(exc).__name__
                self.retry_events.append({"reason": reason, "attempt": attempt, "delay_seconds": delay})
                print(f"Model request {reason}; waiting {delay:g} seconds before retry {attempt}/{limit}.", flush=True)
                await asyncio.sleep(delay)
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
            "finish_reason": choice.get("finish_reason"), "provider_usage": usage})
