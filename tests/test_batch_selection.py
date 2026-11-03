"""Contract and behaviour checks for the two initial guidance modes."""

import asyncio
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from rdr_engine import RDREngine
from rdr_engine.types import EvaluationError
from rdr_service.backend import RDRBackend
from rdr_service.conditions import build_catalog, condition_id, LookupConditionEvaluator, validate_results
from rdr_service.server import make_server
from skill_agent.contracts import ResolveSkillRequest, ResolvedSkill, SkillSelectionError, source_fingerprint
from skill_agent.models import ModelResponse
from skill_agent.runtime import InitialSkillPolicy, InitialWithRequestsPolicy, SessionContext, run_episode
from skill_agent.selection import BatchLLMSkillResolver

ROOT = Path(__file__).resolve().parents[1]


def call(name, args=None, ident="call"):
    return {"id": ident, "type": "function", "function": {"name": name, "arguments": json.dumps(args or {})}}


class ConditionTests(unittest.TestCase):
    def test_ancestry_conjunctions_stops_duplicates_and_unknown(self):
        a, b = "task is a loan", "login failed"
        def node(i, conditions, children=(), stopping=False):
            return {"id": i, "conditions": conditions, "conclusion": None if stopping else {"fragments": []},
                    "children": list(children), "stopping": stopping, "evidence": []}
        raw = {"schema_version": 2, "root_id": 0, "revision": 0, "max_selection_attempts": 3,
               "nodes": [node(0, [], [1, 3]), node(1, [a], [2]), node(2, [b], stopping=True), node(3, [a, b])]}
        lookup = LookupConditionEvaluator({condition_id(a): "false", condition_id(b): "unknown"})
        engine = RDREngine.from_dict(raw, evaluator=lookup)
        cat = build_catalog(engine, "snapshot")
        self.assertEqual(len(cat["conditions"]), 2)
        self.assertEqual(cat["conditions"][0]["node_ids"], [1, 3])
        self.assertNotIn(2, [step.node_id for step in engine.predict({}).trace])
        lookup.results[condition_id(b)] = "true"
        self.assertNotIn(2, [step.node_id for step in engine.predict({}).trace])
        lookup.results[condition_id(a)] = "true"
        lookup.results[condition_id(b)] = "unknown"
        with self.assertRaises(EvaluationError):
            engine.predict({})
        lookup.results[condition_id(b)] = "true"
        prediction = engine.predict({})
        self.assertEqual([c.node_id for c in prediction.conclusions], [3])
        vals = [{"condition_id": x["id"], "result": "false", "evidence": ""} for x in cat["conditions"]]
        payload = {"catalog_fingerprint": cat["catalog_fingerprint"], "evaluations": vals}
        validate_results(cat, payload)
        for malformed in (vals[:-1], vals + vals[:1], [{**vals[0], "condition_id": "missing"}, vals[1]]):
            with self.assertRaises(ValueError):
                validate_results(cat, {**payload, "evaluations": malformed})
        with self.assertRaises(ValueError):
            validate_results(cat, {**payload, "catalog_fingerprint": "stale"})


class SelectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.backend = RDRBackend(self.temp.name)
        receipt = self.backend.prepare(ROOT / "skills/alfworld")
        self.request = ResolveSkillRequest("alfworld", "Heat an apple", {}, receipt["artifact_id"],
            selection_input={"phase": "initial", "task": "Heat an apple", "question": "Heat an apple", "trace": []})
        server = make_server(self.backend, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join()))
        self.url = f"http://127.0.0.1:{server.server_port}"

    async def test_unconditional_catalog_needs_no_model_call(self):
        fake = AsyncMock()
        with patch("skill_agent.selection.OpenAICompatibleClient", return_value=fake):
            resolver = BatchLLMSkillResolver(base_url=self.url, selector_model={})
        resolver._catalog = AsyncMock(return_value={"conditions": [], "catalog_fingerprint": "catalog",
                                                   "tree_fingerprint": "tree"})
        resolver.inner.resolve = AsyncMock(return_value=ResolvedSkill("general", self.request.artifact_id,
                                                                      "source", "revision"))
        result = await resolver.resolve(self.request)
        fake.complete.assert_not_called()
        self.assertEqual(result.usage["model_calls"], 0)
        self.assertEqual(resolver.inner.resolve.call_args.args[0].condition_evaluations["evaluations"], [])

    async def test_one_call_then_real_service_traversal_and_invalid_output_usage(self):
        class Selector:
            retry_events = []
            calls = 0
            invalid = False
            async def complete(self, messages, tools):
                self.calls += 1
                packet = json.loads(messages[1]["content"])
                vals = [{"condition_id": c["id"], "result": "true" if c["predicate"]["value"] == "heat_place" else "false",
                         "evidence": "task: heat an apple"} for c in packet["conditions"]]
                if self.invalid:
                    vals = vals[:-1]
                return ModelResponse({"role": "assistant", "tool_calls": [call("evaluate_conditions", {"evaluations": vals})]},
                                     {"input_tokens": 12, "output_tokens": 5, "cost_usd": .001}, {})
        fake = Selector()
        with patch("skill_agent.selection.OpenAICompatibleClient", return_value=fake):
            resolver = BatchLLMSkillResolver(base_url=self.url, selector_model={})
        probe = await resolver.preflight(self.request)
        self.assertEqual(fake.calls, 0)
        self.assertIn("condition_catalog", probe.metadata)
        result = await resolver.resolve(self.request)
        self.assertEqual(fake.calls, 1)
        self.assertEqual(result.metadata["terminal_ids"], [1, 6])
        self.assertEqual(result.usage["input_tokens"], 12)
        fake.invalid = True
        with self.assertRaises(SkillSelectionError) as caught:
            await resolver.resolve(self.request)
        self.assertEqual(fake.calls, 2)  # No formatting repair call.
        self.assertEqual(caught.exception.usage["cost_usd"], .001)


class ModeTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, policy, fail_refresh=False):
        class Resolver:
            def __init__(self): self.requests = []
            async def resolve(self, request):
                self.requests.append(copy.deepcopy(request))
                if fail_refresh and len(self.requests) > 1:
                    raise SkillSelectionError("unresolved", usage={"model_calls": 1, "input_tokens": 4,
                                                              "output_tokens": 1, "cost_usd": .01})
                return ResolvedSkill("INITIAL_SECRET" if len(self.requests) == 1 else "LOGIN_SECRET",
                                     "artifact", source_fingerprint("source"), "rev")
        class Tool:
            definition = {"type": "function", "function": {"name": "observe", "parameters": {"type": "object"}}}
            async def execute(self, args): return {"observed": args}
        class Session:
            prompt = "Help with a loan"
            tools = {"observe": Tool()}
            async def collect(self): return {"done": True}
            async def close(self): pass
        class Model:
            def __init__(self): self.requests = []
            async def complete(self, messages, tools):
                self.requests.append((copy.deepcopy(messages), tools))
                if len(self.requests) == 1:
                    calls = [call("observe", {"step": "before"}, "a")]
                    if any(t["function"]["name"] == "request_skill" for t in tools):
                        calls += [call("request_skill", {"request": "Explain login"}, "b")]
                    calls += [call("observe", {"step": "after"}, "c")]
                    message = {"role": "assistant", "content": "I need login guidance", "tool_calls": calls}
                else:
                    message = {"role": "assistant", "content": "done"}
                return ModelResponse(message, {"input_tokens": 1, "output_tokens": 1}, {})
        resolver, model = Resolver(), Model()
        context = SessionContext("bank", "artifact", "Loan task", resolver, source_fingerprint("source"))
        result = await run_episode(model=model, session=Session(), skill_context=context,
                                   system_prompt="base", policy=policy)
        return resolver, model, context, result

    async def test_initial_only_loads_once_and_has_no_skill_tool(self):
        resolver, model, context, result = await self.exercise(InitialSkillPolicy())
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(resolver.requests), 1)
        self.assertEqual(resolver.requests[0].selection_input["trace"], [])
        for messages, tools in model.requests:
            self.assertNotIn("request_skill", [t["function"]["name"] for t in tools])
            self.assertIn("INITIAL_SECRET", messages[0]["content"])

    async def test_followup_trace_causality_replacement_and_error_retention(self):
        for fail in (False, True):
            resolver, model, context, result = await self.exercise(InitialWithRequestsPolicy(), fail)
            self.assertEqual(result.status, "completed")
            packet = resolver.requests[1].selection_input
            self.assertEqual(packet["guidance_request"], "Explain login")
            text = json.dumps(packet)
            self.assertIn("before", text)
            self.assertNotIn("after", text)
            self.assertNotIn("INITIAL_SECRET", text)
            self.assertNotIn("request_skill", text)
            self.assertEqual(context.active_skills["bank"], "INITIAL_SECRET" if fail else "LOGIN_SECRET")
            next_prompt = model.requests[1][0][0]["content"]
            self.assertIn("INITIAL_SECRET" if fail else "LOGIN_SECRET", next_prompt)
            if not fail:
                self.assertNotIn("INITIAL_SECRET", next_prompt)
            self.assertEqual(sum(e["type"] == "skill_error" for e in result.events), int(fail))

    async def test_initial_failure_is_unresolved_before_inference(self):
        class Resolver:
            async def resolve(self, request): raise SkillSelectionError("unknown")
        class Model:
            async def complete(self, *args): raise AssertionError("must not infer")
        class Session:
            prompt = "task"
            tools = {}
            async def close(self): pass
        context = SessionContext("bank", "artifact", "task", Resolver(), source_fingerprint("source"))
        result = await run_episode(model=Model(), session=Session(), skill_context=context,
                                   system_prompt="base", policy=InitialSkillPolicy())
        self.assertEqual(result.status, "unresolved")


if __name__ == "__main__":
    unittest.main()
