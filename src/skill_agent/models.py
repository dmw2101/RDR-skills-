"""OpenAI-compatible Chat Completions; no SDK or RDR dependency."""

import asyncio
import os
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

from .http import json_request


@dataclass(frozen=True)
class ModelResponse:
    message: dict
    usage: dict
    metadata: dict


class ModelClient(Protocol):
    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse: ...


class OpenAICompatibleClient:
    def __init__(self, *, base_url, model, api_key_env="MODEL_API_KEY", temperature=0,
                 max_tokens=2048, seed=None, timeout=90, **unknown):
        if unknown:
            raise ValueError(f"unknown model settings: {sorted(unknown)}")
        parsed = urlparse(base_url)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")):
            raise ValueError("model endpoint must use HTTPS (or localhost HTTP)")
        if not model or not os.environ.get(api_key_env):
            raise ValueError(f"set model and environment variable {api_key_env}")
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model, self.key = model, os.environ[api_key_env]
        self.temperature, self.max_tokens = temperature, max_tokens
        self.seed, self.timeout = seed, timeout

    async def complete(self, messages, tools):
        payload = {"model": self.model, "messages": messages,
                   "temperature": self.temperature, "max_tokens": self.max_tokens}
        if tools:
            payload.update(tools=tools, tool_choice="auto")
        if self.seed is not None:
            payload["seed"] = self.seed
        data = await asyncio.to_thread(
            json_request, self.url, payload,
            headers={"Authorization": f"Bearer {self.key}"}, timeout=self.timeout,
        )
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
            "cost_usd": None,
        }, {"id": data.get("id"), "model": data.get("model"),
            "system_fingerprint": data.get("system_fingerprint"),
            "finish_reason": choice.get("finish_reason"), "provider_usage": usage})
