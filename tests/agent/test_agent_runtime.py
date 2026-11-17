import asyncio
import copy
import json
import unittest

from skill_agent.contracts import ResolvedSkill, source_fingerprint
from skill_agent.models import ModelResponse
from skill_agent.resolvers import StaticSkillResolver
from skill_agent.runtime import NoSkillPolicy, SKILL_REQUEST_INSTRUCTIONS, SessionContext, run_episode


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
    async def test_no_skill_omits_catalog_guidance_and_tool_on_every_turn(self):
        class ForbiddenResolver:
            async def resolve(self, request):
                raise AssertionError("no-skill must never resolve guidance")
        class Ping:
            definition = {"type": "function", "function": {"name": "ping", "parameters": {"type": "object"}}}
            async def execute(self, args):
                return {"ok": True}
        state = context(ForbiddenResolver())
        state.capabilities = [{"name": "SECRET_TOPIC", "description": "SECRET_PROCEDURE"}]
        state.active_skills = {"pdf": "SECRET_GUIDANCE"}
        model = FakeModel([{"role": "assistant", "tool_calls": [
            {"id": "one", "type": "function", "function": {"name": "ping", "arguments": "{}"}}]},
            {"role": "assistant", "content": "done"}])
        session = FakeSession()
        session.tools["ping"] = Ping()
        result = await run_episode(model=model, session=session, skill_context=state,
                                   system_prompt="base", policy=NoSkillPolicy())
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(model.requests), 2)
        for messages, tools in zip(model.requests, model.definitions):
            self.assertEqual(messages[0], {"role": "system", "content": "base"})
            self.assertEqual(messages[1], {"role": "user", "content": session.prompt})
            self.assertEqual(tools, [session.tools["ping"].definition])
            self.assertNotIn("SECRET_", json.dumps(messages))
        self.assertFalse(any(e["type"].startswith("skill") for e in result.events))

    async def test_full_and_reduced_share_the_skill_interface(self):
        class Reduced:
            async def resolve(self, request):
                return ResolvedSkill("selected text", "snapshot", source_fingerprint("whole skill"), "backend")
        resolvers = [StaticSkillResolver("pdf", "snapshot", "whole skill"), Reduced()]
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
        self.assertNotIn("\n<active_skill>\n", initial_requests[0][0]["content"])
        self.assertIn("Text and table extraction", initial_requests[0][0]["content"])
        self.assertEqual(requests[0][1:-1], requests[1][1:-1])
        # Tool receipts differ by condition; task and model request remain shared.
        self.assertTrue(requests[0][0]["content"].startswith("base\n\n" + SKILL_REQUEST_INSTRUCTIONS))
        # Every skill arm is told how to treat learned rules (decision K2).
        self.assertIn("<learned_rules> block", SKILL_REQUEST_INSTRUCTIONS)
        self.assertIn("check the answer against each rule", SKILL_REQUEST_INSTRUCTIONS)
        self.assertIn("whole skill", requests[0][0]["content"])
        self.assertIn("selected text", requests[1][0]["content"])

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

    async def test_rolling_query_keeps_the_task_on_every_refresh(self):
        # Decision K3: the task's conditions must hold for the whole attempt.
        from skill_agent.runtime import RollingSkillPolicy
        tasks = []
        class Recording:
            async def resolve(self, request):
                tasks.append(request.task)
                return ResolvedSkill("text", "snapshot", source_fingerprint("whole skill"), "v1")
        for keep, later in ((True, "Read the PDF\n\nnow writing the JSON file"), (False, "now writing the JSON file")):
            tasks.clear()
            state = context(Recording())
            policy = RollingSkillPolicy(keep_task=keep)
            await policy.before_model(state, 0)
            state.events.append({"type": "model_response", "turn": 0,
                                 "message": {"role": "assistant", "content": "now writing the JSON file"}})
            await policy.before_model(state, 1)
            self.assertEqual(tasks, ["Read the PDF", later])
        self.assertEqual(state.events[-1]["selection"]["input_source"], "recent_model_turns")
        self.assertTrue(RollingSkillPolicy().keep_task)

    async def test_preloaded_skill_is_in_the_system_prompt_without_the_skill_interface(self):
        # Decision H4: the plain baseline, built like Trace2Skill's preloaded agent.
        from skill_agent.runtime import AUTOMATIC_SKILL_INSTRUCTIONS, PreloadedSkillPolicy
        text = "---\nname: pdf\ndescription: PDF things\n---\n# PDF guide\nUse pdftotext."
        class Ping:
            definition = {"type": "function", "function": {"name": "ping", "parameters": {"type": "object"}}}
            async def execute(self, args):
                return {"ok": True}
        call = {"role": "assistant", "content": "",
                "tool_calls": [{"id": "one", "type": "function", "function": {"name": "ping", "arguments": "{}"}}]}
        model = FakeModel([call, {"role": "assistant", "content": "done"}])
        session = FakeSession()
        session.tools["ping"] = Ping()
        state = SessionContext("pdf", "snapshot", "Read the PDF", StaticSkillResolver("pdf", "snapshot", text),
                               source_fingerprint(text))
        state.capabilities = [{"name": "TOPIC", "description": "x"}]
        result = await run_episode(model=model, session=session, skill_context=state, system_prompt="base",
                                   policy=PreloadedSkillPolicy(["FORMS.md", "scripts"]))
        self.assertEqual(result.status, "completed")
        system = model.requests[0][0]["content"]
        self.assertEqual(model.requests[0][0], model.requests[1][0])  # Given once: never changes.
        self.assertTrue(system.startswith("base\n\n## Relevant Skill"))
        self.assertIn("<skill_content>\n# PDF guide\nUse pdftotext.\n</skill_content>", system)
        self.assertIn("(FORMS.md, scripts) are in the current working folder", system)
        self.assertIn("Mandatory Skill Usage Protocol", system)
        for absent in ("name: pdf", "<active_skill>", "<skill_catalog>", "<learned_rules>",
                       AUTOMATIC_SKILL_INSTRUCTIONS[:60], SKILL_REQUEST_INSTRUCTIONS[:60]):
            self.assertNotIn(absent, system)
        self.assertEqual([t["function"]["name"] for t in model.definitions[0]], ["ping"])
        skills = [event for event in state.events if event["type"] == "skill"]
        self.assertEqual([event["selection"]["policy"] for event in skills], ["preloaded"])

    async def test_cut_off_attempt_keeps_its_answer_when_the_session_allows(self):
        # Decision H3: a run that hits its step limit is still scored on the answer it wrote.
        class Ping:
            definition = {"type": "function", "function": {"name": "ping", "parameters": {"type": "object"}}}
            async def execute(self, args):
                return {"ok": True}
        call = {"role": "assistant", "content": "",
                "tool_calls": [{"id": "one", "type": "function", "function": {"name": "ping", "arguments": "{}"}}]}
        for keep in (True, False):
            session = FakeSession()
            session.tools["ping"] = Ping()
            session.collect_on_failure = keep
            result = await run_episode(model=FakeModel([call]), session=session, skill_context=None,
                                       system_prompt="base", policy=NoSkillPolicy(), max_calls=2)
            self.assertEqual(result.status, "budget_exhausted")
            self.assertEqual(result.prediction, {"answer": 42} if keep else None)

    async def test_root_only_tree_gets_exactly_the_full_skill_prompt(self):
        # Decision H5: same wrapping, so the base tree differs from the full skill only by sampling;
        # learned rules add their block and one protocol line, nothing else.
        import tempfile
        from pathlib import Path
        from rdr_engine.skill_compiler import SkillBundle
        from skill_agent.runtime import LEARNED_RULES_BULLET, OnceSkillPolicy, PreloadedSkillPolicy
        from trace_learner.workspace import SkillWorkspace, copy_bundle
        root = Path(__file__).resolve().parents[2] / "skills/pdf"
        full = (root / "initial.md").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temp:
            copy = copy_bundle(root, Path(temp) / "pdf")
            SkillBundle.reshape(copy, "root_only")
            tree = SkillWorkspace(copy).render({"operation": [], "source_kind": "unknown"}).markdown
        learned = "<learned_rules>\nThese rules were learned.\n\nCopy values verbatim.\n</learned_rules>\n\n" + tree

        async def system_prompt(text, policy):
            state = SessionContext("pdf", "snapshot", "Read the PDF", StaticSkillResolver("pdf", "snapshot", text),
                                   source_fingerprint(text))
            model = FakeModel()
            await run_episode(model=model, session=FakeSession(), skill_context=state, system_prompt="base",
                              policy=policy)
            return model.requests[0][0]["content"], [t["function"]["name"] for t in model.definitions[0]]
        files = ["FORMS.md", "REFERENCE.md", "scripts"]
        full_prompt, full_tools = await system_prompt(full, PreloadedSkillPolicy(files))
        tree_prompt, tree_tools = await system_prompt(tree, OnceSkillPolicy(files))
        self.assertEqual(tree_prompt, full_prompt)
        self.assertEqual(tree_tools, full_tools)
        learned_prompt, _ = await system_prompt(learned, OnceSkillPolicy(files))
        self.assertIn(LEARNED_RULES_BULLET, learned_prompt)
        self.assertNotIn(LEARNED_RULES_BULLET, tree_prompt)
        self.assertEqual(learned_prompt.replace(LEARNED_RULES_BULLET, "").replace(
            "<learned_rules>\nThese rules were learned.\n\nCopy values verbatim.\n</learned_rules>\n\n", ""), full_prompt)

    async def test_once_policy_builds_guidance_only_before_the_first_step(self):
        # Decision K4: one view from the task, kept for the whole attempt.
        from skill_agent.runtime import OnceSkillPolicy
        tasks = []
        class Recording:
            async def resolve(self, request):
                tasks.append(request.task)
                return ResolvedSkill("fixed guidance", "snapshot", source_fingerprint("whole skill"), "v1")
        class Ping:
            definition = {"type": "function", "function": {"name": "ping", "parameters": {"type": "object"}}}
            async def execute(self, args):
                return {"ok": True}
        call = {"role": "assistant", "content": "reading the form",
                "tool_calls": [{"id": "one", "type": "function", "function": {"name": "ping", "arguments": "{}"}}]}
        model = FakeModel([call, dict(call), {"role": "assistant", "content": "done"}])
        session = FakeSession()
        session.tools["ping"] = Ping()
        state = context(Recording())
        result = await run_episode(model=model, session=session, skill_context=state,
                                   system_prompt="base", policy=OnceSkillPolicy())
        self.assertEqual(result.status, "completed")
        self.assertEqual(tasks, ["Read the PDF"])
        skills = [event for event in state.events if event["type"] == "skill"]
        self.assertEqual([event["selection"]["policy"] for event in skills], ["initial"])
        self.assertEqual(len(model.requests), 3)
        for request in model.requests:  # The same guidance stays in every step's prompt.
            self.assertEqual(request[0], model.requests[0][0])
            self.assertIn("<skill_content>\nfixed guidance\n</skill_content>", request[0]["content"])
        self.assertEqual([t["function"]["name"] for t in model.definitions[0]], ["ping"])  # No request_skill.

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
