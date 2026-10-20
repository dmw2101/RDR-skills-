"""Optional local Transformers adapter; model weights live in one separate server.

Only the tokenizer is cached here. New episodes still receive fresh conversations;
neither generated answers nor rendered skills are cached between episodes.
"""

import argparse
import asyncio
import json
from copy import deepcopy
from functools import lru_cache
from urllib.parse import urlparse

from .models import OpenAICompatibleClient


QWEN_MODEL = "Qwen/Qwen3.5-4B@851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a"


@lru_cache(maxsize=2)
def _tokenizer(model):
    from transformers import AutoTokenizer

    name, separator, revision = model.partition("@")
    return AutoTokenizer.from_pretrained(name, revision=revision if separator else "main")


class LocalTransformersClient(OpenAICompatibleClient):
    """Use a local FP16 server with an explicit, non-truncating context budget."""

    def __init__(self, *, base_url="http://127.0.0.1:8000/v1", model=QWEN_MODEL,
                 context_window=8192, max_tokens=2048, timeout=300,
                 temperature=0, seed=42):
        if urlparse(base_url).hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("local model server must use a localhost endpoint")
        if type(context_window) is not int or type(max_tokens) is not int or not 0 < max_tokens < context_window:
            raise ValueError("require 0 < max_tokens < context_window")
        super().__init__(base_url=base_url, model=model, api_key_env=None,
                         temperature=temperature, max_tokens=max_tokens, seed=seed, timeout=timeout)
        self.context_window = context_window

    def _input_tokens(self, messages, tools):
        # API history uses JSON strings; Qwen's template iterates argument mappings.
        # Normalize only this copy, like the Transformers server does internally.
        template_messages = deepcopy(messages)
        for message in template_messages:
            for call in message.get("tool_calls") or []:
                function = call["function"]
                arguments = function.get("arguments", {})
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
                if not isinstance(arguments, dict):
                    raise ValueError("local tool arguments must be a JSON object")
                function["arguments"] = arguments
        options = {"tools": tools} if tools else {}
        # Transformers 5.20 defaults to a mapping; len() must count token IDs.
        tokens = _tokenizer(self.model).apply_chat_template(
            template_messages, tokenize=True, add_generation_prompt=True,
            return_dict=False, truncation=False, enable_thinking=False, **options,
        )
        return len(tokens)

    async def complete(self, messages, tools):
        count = await asyncio.to_thread(self._input_tokens, messages, tools)
        if count + self.max_tokens > self.context_window:
            raise ValueError(
                f"local context budget exceeded: {count} input + {self.max_tokens} output "
                f"> {self.context_window}; no messages were truncated"
            )
        response = await super().complete(messages, tools)
        response.metadata["local_context_window"] = self.context_window
        return response


async def check_tools(client):
    """Two local inference calls verify structured tools and the tool-result round trip."""
    tools = [{"type": "function", "function": {
        "name": "echo", "description": "Return the supplied value.",
        "parameters": {"type": "object", "properties": {"value": {"type": "string"}},
                       "required": ["value"], "additionalProperties": False},
    }}]
    messages = [
        {"role": "system", "content": "Call echo exactly once with value OK. After receiving its result, reply only OK without calling another tool."},
        {"role": "user", "content": "Please perform the echo tool check now."},
    ]
    first = await client.complete(messages, tools)
    calls = first.message.get("tool_calls") or []
    if len(calls) != 1 or calls[0]["function"]["name"] != "echo":
        raise RuntimeError("local smoke check did not return one structured echo tool call")
    if json.loads(calls[0]["function"]["arguments"]) != {"value": "OK"}:
        raise RuntimeError("local smoke check returned unexpected tool arguments")
    messages.extend([first.message, {"role": "tool", "tool_call_id": calls[0]["id"],
                                     "content": json.dumps({"value": "OK"})}])
    second = await client.complete(messages, tools)
    if second.message.get("tool_calls") or (second.message.get("content") or "").strip() != "OK":
        raise RuntimeError("local smoke check failed the tool-result round trip")
    return {"tool_call": "ok", "tool_result_round_trip": "ok", "model": client.model,
            "usage": [first.usage, second.usage]}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local FP16 Qwen server and tool smoke check")
    parser.add_argument("command", choices=("serve", "check"))
    parser.add_argument("--model", default=QWEN_MODEL)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--context-window", type=int, default=8192)
    args = parser.parse_args(argv)
    if args.command == "check":
        client = LocalTransformersClient(base_url=f"http://127.0.0.1:{args.port}/v1",
                                         model=args.model, context_window=args.context_window,
                                         max_tokens=256)
        print(json.dumps(asyncio.run(check_tools(client)), indent=2))
    else:
        import torch
        if not torch.cuda.is_available():
            parser.error("CUDA GPU unavailable; select a T4 GPU runtime in Colab")
        from transformers.cli.serve import Serve

        # One resident model, no quantization/CPU offloading/FlashAttention requirement.
        Serve(force_model=args.model, device="cuda:0", dtype="float16",
              attn_implementation="sdpa", reasoning="off", host="127.0.0.1",
              port=args.port, log_level="info")


if __name__ == "__main__":
    main()
