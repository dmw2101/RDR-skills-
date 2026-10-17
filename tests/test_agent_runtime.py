import asyncio
import copy
import json
import unittest

from skill_agent.contracts import ResolvedSkill, source_fingerprint
from skill_agent.models import ModelResponse
from skill_agent.resolvers import StaticSkillResolver
from skill_agent.runtime import SKILL_REQUEST_INSTRUCTIONS, SessionContext, run_episode


class FakeModel:
    def __init__(self, replies=None):
        self.replies = replies or [{"role": "assistant", "content": "finished"}]
        self.requests = []
        self.definitions = []

    async def complete(self, messages, tools):
        self.requests.append(copy.deepcopy(messages))
        self.definitions.append(copy.deepcopy(tools))
        message = self.replies[min(len(self.requests) - 1, len(self.replies) - 1)]
        return ModelResponse(message, {"input_tokens": 10, "output_tokens": 2,
                                      "cached_input_tokens": 0, "cost_usd": None}, {"model": "fake"})


class FakeSession:
    prompt = "Read the PDF"

    def __init__(self):
        self.tools = {}
        self.closed = False

    async def collect(self):
        return {"answer": 42}

    async def close(self):
        self.closed = True


def context(resolver=None):
    text = "whole skill"
    return SessionContext("pdf", "snapshot", "Read the PDF", resolver or StaticSkillResolver(
        "pdf", "snapshot", text), source_fingerprint(text))


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_arms_change_only_skill_section(self):
        class Reduced:
            async def resolve(self, request):
                return ResolvedSkill("selected text", "snapshot", source_fingerprint("whole skill"), "backend")
        resolvers = [StaticSkillResolver("pdf", "snapshot", "whole skill", empty=True),
                     StaticSkillResolver("pdf", "snapshot", "whole skill"), Reduced()]
        requests, initial_requests = [], []
        for resolver in resolvers:
            model = FakeModel([{"role": "assistant", "tool_calls": [{
                "id": "help", "type": "function", "function": {
                    "name": "request_skill", "arguments": '{"request":"Read the PDF"}'}}]},
                {"role": "assistant", "content": "finished"}])
            session, state = FakeSession(), context(resolver)
            state.capabilities = [{"name": "extraction", "description": "Text and table extraction"}]
            result = await run_episode(model=model, session=session, skill_context=state, system_prompt="base")
            self.assertEqual(result.status, "completed")
            self.assertTrue(session.closed)
            self.assertIn("request_skill", [t["function"]["name"] for t in model.definitions[0]])
            initial_requests.append(model.requests[0])
            requests.append(model.requests[1])
        self.assertEqual(initial_requests[0], initial_requests[1])
        self.assertEqual(initial_requests[1], initial_requests[2])
        self.assertNotIn("\n<active_skill>\n", initial_requests[0][0]["content"])
        self.assertIn("Text and table extraction", initial_requests[0][0]["content"])
        self.assertEqual(requests[0][1:-1], requests[1][1:-1])
        # Tool receipts differ by condition; task and model request remain shared.
        self.assertEqual(requests[1][1:-1], requests[2][1:-1])
        self.assertTrue(requests[0][0]["content"].startswith("base\n\n" + SKILL_REQUEST_INSTRUCTIONS))
        self.assertIn("whole skill", requests[1][0]["content"])
        self.assertIn("selected text", requests[2][0]["content"])

    async def test_refresh_replaces_managed_text_and_keeps_audit(self):
        class Changing:
            async def resolve(self, request):
                text = "OCR guidance" if request.context.get("scanned") else "initial guidance"
                return ResolvedSkill(text, "snapshot", source_fingerprint("whole skill"), "v1")
        class Refresh:
            async def before_model(self, state, turn):
                await state.load_skill({"scanned": True} if turn else None)
        class Ping:
            definition = {"type": "function", "function": {"name": "ping", "parameters": {"type": "object"}}}
            async def execute(self, args):
                return {"scanned": True}
        model = FakeModel([{"role": "assistant", "tool_calls": [
            {"id": "one", "type": "function", "function": {"name": "ping", "arguments": "{}"}}]},
            {"role": "assistant", "content": "done"}])
        session = FakeSession()
        session.tools["ping"] = Ping()
        state = context(Changing())
        result = await run_episode(model=model, session=session, skill_context=state,
                                   system_prompt="base", policy=Refresh())
        self.assertEqual(result.status, "completed")
        self.assertIn("initial guidance", model.requests[0][0]["content"])
        self.assertIn("OCR guidance", model.requests[1][0]["content"])
        self.assertNotIn("initial guidance", json.dumps(model.requests[1]))
        self.assertEqual(len([e for e in state.events if e["type"] == "skill"]), 2)

    async def test_model_initiated_request_can_use_same_load_operation(self):
        resolved = []
        class Changing:
            async def resolve(self, request):
                resolved.append(request)
                return ResolvedSkill("guidance: " + request.task, "snapshot",
                                     source_fingerprint("whole skill"), "v1")
        state = context(Changing())
        model = FakeModel([{"role": "assistant", "tool_calls": [
            {"id": "ocr", "type": "function", "function": {"name": "request_skill", "arguments": json.dumps({
                "request": "OCR this PDF", "context": {"source_kind": "scanned_image"}})}}]},
            {"role": "assistant", "tool_calls": [
                {"id": "merge", "type": "function", "function": {"name": "request_skill",
                 "arguments": json.dumps({"request": "Merge PDFs"})}}]},
            {"role": "assistant", "content": "done"}])
        session = FakeSession()
        result = await run_episode(model=model, session=session, skill_context=state, system_prompt="base")
        self.assertEqual(result.status, "completed")
        self.assertEqual([r.task for r in resolved], ["OCR this PDF", "Merge PDFs"])
        self.assertEqual([r.context for r in resolved], [{"source_kind": "scanned_image"}, {}])
        self.assertNotIn("\n<active_skill>\n", model.requests[0][0]["content"])
        self.assertEqual(state.task, "Read the PDF")
        self.assertEqual(state.observed, {})
        self.assertIn("guidance: OCR this PDF", model.requests[1][0]["content"])
        self.assertNotIn("guidance: Read the PDF", model.requests[1][0]["content"])
        self.assertIn("guidance: Merge PDFs", model.requests[2][0]["content"])
        self.assertNotIn("guidance: OCR this PDF", model.requests[2][0]["content"])
        self.assertEqual(sum(e["type"] == "skill" for e in state.events), 2)
        self.assertNotIn("guidance:", model.requests[1][-1]["content"])
        self.assertTrue(json.loads(model.requests[1][-1]["content"])["guidance_available"])
        self.assertNotIn("request_skill", session.tools)

    async def test_budgets_close_session(self):
        model = FakeModel([{"role": "assistant", "tool_calls": [
            {"id": "bad", "type": "function", "function": {"name": "missing", "arguments": "{}"}}]}])
        session = FakeSession()
        result = await run_episode(model=model, session=session, skill_context=context(), system_prompt="base", max_calls=1)
        self.assertEqual(result.status, "budget_exhausted")
        self.assertTrue(session.closed)
        self.assertEqual(len(model.requests), 1)

    async def test_wall_time_budget(self):
        class SlowModel:
            async def complete(self, *args):
                await asyncio.sleep(10)
        session = FakeSession()
        result = await run_episode(model=SlowModel(), session=session, skill_context=context(),
                                   system_prompt="base", timeout_seconds=.01)
        self.assertEqual(result.status, "budget_exhausted")
        self.assertTrue(session.closed)

    async def test_provenance_failure_never_falls_back(self):
        session = FakeSession()
        model = FakeModel([{"role": "assistant", "tool_calls": [{
            "id": "help", "type": "function", "function": {
                "name": "request_skill", "arguments": '{"request":"Read the PDF"}'}}]}])
        state = context(StaticSkillResolver("pdf", "snapshot", "different source"))
        result = await run_episode(model=model, session=session, skill_context=state, system_prompt="base")
        self.assertEqual(result.status, "failed")
        self.assertEqual(len(model.requests), 1)
        self.assertNotIn("\n<active_skill>\n", model.requests[0][0]["content"])
        self.assertTrue(session.closed)

    async def test_malformed_tool_arguments_are_observations(self):
        model = FakeModel([{"role": "assistant", "tool_calls": [
            {"id": "bad", "type": "function", "function": {"name": "missing", "arguments": "oops"}}]},
            {"role": "assistant", "content": "done"}])
        result = await run_episode(model=model, session=FakeSession(), skill_context=context(), system_prompt="base")
        self.assertEqual(result.status, "completed")
        self.assertIn("error", json.loads(model.requests[1][-1]["content"]))
