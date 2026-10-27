"""Visible-context Bank procedure selection outside the RDR service boundary."""

import json
import hashlib
from dataclasses import replace
from pathlib import Path

from .contracts import ResolveSkillRequest
from .models import OpenAICompatibleClient
from .resolvers import HttpSkillResolver
from .runtime import planning_text


BANK_ROLLING_INSTRUCTIONS = (
    "Procedural guidance is supplied automatically in <active_skill> when available. "
    "Guidance is refreshed using the customer request and recent visible assistant and tool activity. "
    "Use the supplied guidance and Bank tools to assist the customer. Each refresh replaces the "
    "previous view. request_skill is available for compatibility but does not change the automatic view."
)


class SelectorError(ValueError):
    """Retain paid selector usage even if its answer or subsequent lookup fails."""

    def __init__(self, message, usage):
        super().__init__(message)
        self.usage = {"model_calls": 1, "input_tokens": usage.get("input_tokens"),
                      "output_tokens": usage.get("output_tokens"), "cost_usd": usage.get("cost_usd")}


class BankRollingSkillPolicy:
    automatic_guidance = True
    instructions = BANK_ROLLING_INSTRUCTIONS

    async def before_model(self, session, turn):
        responses = [e for e in session.events if e["type"] == "model_response"][-3:]
        first = responses[0]["turn"] if responses else turn
        visible = []
        for event in session.events:
            if event["type"] == "model_response" and event["turn"] >= first:
                text = planning_text(event["message"])
                if text:
                    visible.append("Assistant: " + text)
            elif event["type"] == "user_message" and event["turn"] >= first:
                visible.append("Customer: " + event["content"])
            elif event["type"] == "tool" and event.get("turn", -1) >= first and event["name"] != "request_skill":
                visible.append("Tool " + event["name"] + ": " + json.dumps(event["result"], ensure_ascii=False))
        query = "Customer's original request:\n" + session.task
        if visible:
            query += "\n\nRecent visible conversation and tool results:\n" + "\n".join(visible)
        await session.load_skill(task=query, selection={
            "policy": "rolling", "turn": turn,
            "source_turns": [e["turn"] for e in responses], "input_source": "visible_bank_context",
        })


class BankActionResolver:
    """Classify visible context, then pass explicit action features to the skill service."""

    def __init__(self, *, base_url, selector_model, action_schema, timeout=30):
        self.inner = HttpSkillResolver(base_url, timeout=timeout)
        self.selector = OpenAICompatibleClient(**selector_model)
        schema_bytes = Path(action_schema).read_bytes()
        catalog = json.loads(schema_bytes.decode("utf-8-sig"))
        self.actions = catalog["sopbench_bank_fixed_policy"]["needed_actions"]["values"]
        self.provenance = {"selector": "bank-visible-context-v1",
                           "action_schema_sha256": hashlib.sha256(schema_bytes).hexdigest(),
                           "model": selector_model}
        self.definition = {"type": "function", "function": {
            "name": "select_bank_actions",
            "description": "Select the Bank action procedures relevant to the visible request and recent activity.",
            "parameters": {"type": "object", "properties": {
                "needed_actions": {"type": "array", "items": {"type": "string", "enum": self.actions},
                                   "uniqueItems": True}},
                "required": ["needed_actions"], "additionalProperties": False}}}

    async def preflight(self, request):
        # A service check must not spend model tokens or consume hidden case labels.
        return await self.inner.resolve(ResolveSkillRequest(
            request.skill_id, request.task, {"needed_actions": []}, request.artifact_id))

    async def resolve(self, request):
        system = (
            "Select the Bank procedures needed for the customer request and recent visible activity. "
            "Include the requested operation and helper actions that may be needed, such as login or "
            "admin authentication. Use only the supplied visible text and the action names. "
            "Do not decide whether the customer is eligible. Select all relevant actions when several "
            "apply. If none can be identified return an empty list. Call select_bank_actions exactly once."
        )
        response = await self.selector.complete(
            [{"role": "system", "content": system}, {"role": "user", "content": request.task}],
            [self.definition],
        )
        try:
            if response.metadata.get("finish_reason") in ("length", "content_filter"):
                raise ValueError("selector response was truncated or blocked")
            calls = response.message.get("tool_calls") or []
            if calls:
                if len(calls) != 1 or calls[0].get("function", {}).get("name") != "select_bank_actions":
                    raise ValueError("selector returned an unexpected tool call")
                raw = calls[0]["function"]["arguments"]
            else:
                raw = response.message.get("content")
            try:
                data = json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError("selector did not return valid JSON") from exc
            values = data.get("needed_actions") if isinstance(data, dict) and set(data) == {"needed_actions"} else None
            if (not isinstance(values, list) or any(not isinstance(x, str) or x not in self.actions for x in values)
                    or len(values) != len(set(values))):
                raise ValueError("selector returned invalid action names")
            context = {**request.context, "needed_actions": values}
            result = await self.inner.resolve(ResolveSkillRequest(request.skill_id, request.task, context, request.artifact_id))
        except Exception as exc:
            raise SelectorError(f"selection or skill lookup failed: {type(exc).__name__}: {exc}", response.usage) from exc
        usage = response.usage
        return replace(result, usage={
            "model_calls": 1, "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"), "cost_usd": usage.get("cost_usd"),
        }, metadata={**result.metadata, "selector": {
            "actions": values, "model": response.metadata.get("model"),
            "provider": response.metadata.get("provider"), "finish_reason": response.metadata.get("finish_reason"),
            "cached_input_tokens": usage.get("cached_input_tokens"),
        }})
