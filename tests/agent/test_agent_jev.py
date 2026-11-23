import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skill_agent import models
from skill_agent.http import HTTPStatusError
from skill_agent.jev import JevClient, JevJudge, question
from skill_agent.learned_features import FeatureJudge, make_feature_judge
from skill_agent.models import ModelResponse

FEATURES = {"lf_forms": {"description": "The PDF has filled form fields.", "examples": ["Name: Pope"],
                         "counterexamples": ["a blank form"]},
            "lf_tables": {"description": "The PDF has a totals table."}}


def answer(choice, true):
    return {"type": "choice", "choice": choice, "confidence": abs(2 * true - 1),
            "probabilities": {"true": true, "false": round(1 - true, 6)}}


class FakeJev:
    """Stands in for JevClient.ask; returns the given answers."""

    def __init__(self, answers):
        self.answers, self.requests = answers, []

    async def ask(self, state, questions):
        self.requests.append((state, questions))
        return ModelResponse({"role": "assistant", "content": None, "answers": self.answers},
                             {"input_tokens": 900, "output_tokens": 0, "cached_input_tokens": None, "cost_usd": None},
                             {"model": "jev-1.14.0", "provider": "typesafe", "finish_reason": "stop",
                              "latency_seconds": 0.3, "retry_wait_seconds": 0.0, "switch_seconds": 0.0})


class JevJudgeTests(unittest.IsolatedAsyncioTestCase):
    # Decision J7: each condition is a true/false choice; the more likely option is the answer.

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.log = Path(self.temporary.name) / "judge_calls.jsonl"

    def test_a_condition_becomes_a_true_false_choice(self):
        asked = question(FEATURES["lf_forms"])
        self.assertEqual(asked["type"], "choice")
        self.assertEqual(sorted(asked["criteria"]), ["false", "true"])
        self.assertIn("The PDF has filled form fields.", asked["instructions"])
        self.assertIn("True examples: Name: Pope", asked["instructions"])
        self.assertIn("False examples: a blank form", asked["instructions"])
        self.assertNotIn("unknown", json.dumps(asked))

    async def test_choices_become_values_and_probabilities_are_logged(self):
        client = FakeJev({"lf_forms": answer("true", 0.9), "lf_tables": answer("false", 0.38)})
        values, response = await JevJudge(client, log=self.log).judge(FEATURES, "the text")
        self.assertEqual(values, {"lf_forms": "true", "lf_tables": "false"})
        state, questions = client.requests[0]
        self.assertEqual(state, "the text")
        self.assertEqual(sorted(questions), ["lf_forms", "lf_tables"])
        line = json.loads(self.log.read_text(encoding="utf-8"))
        self.assertTrue(line["usable"])
        self.assertEqual(line["input_tokens"], 900)
        self.assertEqual(line["latency_seconds"], 0.3)
        self.assertEqual(line["probabilities"]["lf_tables"], {"true": 0.38, "false": 0.62})
        self.assertFalse((self.log.parent / "judge_evidence").exists())  # Jev never answers unknown.

    async def test_a_missing_choice_is_unusable_and_counts_as_unknown(self):
        client = FakeJev({"lf_forms": answer("true", 0.9)})
        values, response = await JevJudge(client, log=self.log).judge(FEATURES, "the text")
        self.assertEqual(values, {"lf_forms": "unknown", "lf_tables": "unknown"})
        self.assertIn("lf_tables", response.metadata["judge_unusable"])
        self.assertFalse(json.loads(self.log.read_text(encoding="utf-8"))["usable"])
        self.assertEqual(len(list((self.log.parent / "judge_evidence").glob("*-unusable.json"))), 1)

    async def test_jev_writes_no_reasons(self):
        judge = JevJudge(FakeJev({}))
        self.assertFalse(judge.explains)
        self.assertTrue(FeatureJudge(object()).explains)
        with self.assertRaises(NotImplementedError):
            await judge.explain(FEATURES, "x")


class JevClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_configured_model_supplies_the_jev_judge(self):
        client = JevClient(model="jev-1.14.0")
        self.assertIsInstance(make_feature_judge(client), JevJudge)
        self.assertEqual(client.url, "https://api.typesafe.ai/v1/systemone")

    def test_a_key_is_required(self):
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            JevClient(model="jev-1.14.0")

    async def test_request_and_response(self):
        sent = {}

        def fake(url, payload=None, *, headers=None, timeout=60, **_):
            sent.update(url=url, payload=payload, headers=headers)
            return {"model": "jev-1.14.0", "answers": {"lf_forms": answer("true", 0.8)},
                    "usage": {"input_tokens": 1200, "output_tokens": 0}}
        with mock.patch.object(models, "json_request", fake):
            response = await JevClient(model="jev-1.14.0").ask("text", {"lf_forms": question(FEATURES["lf_forms"])})
        self.assertEqual(sent["url"], "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(sent["headers"], {"Authorization": "Bearer test-key"})
        self.assertEqual(sent["payload"]["model"], "jev-1.14.0")
        self.assertEqual(sent["payload"]["state"], "text")
        self.assertEqual(response.message["answers"]["lf_forms"]["choice"], "true")
        self.assertEqual(response.usage["input_tokens"], 1200)
        self.assertIsNotNone(response.metadata["latency_seconds"])

    async def test_overloaded_is_retried(self):
        calls = []

        def fake(url, payload=None, *, headers=None, timeout=60, **_):
            calls.append(1)
            if len(calls) == 1:
                raise HTTPStatusError(529, "api.typesafe.ai", retry_after=0)
            return {"model": "jev-1.14.0", "answers": {}, "usage": {"input_tokens": 1, "output_tokens": 0}}
        client = JevClient(model="jev-1.14.0")
        client.retry_delays = (0,)
        with mock.patch.object(models, "json_request", fake):
            response = await client.ask("text", {})
        self.assertEqual(len(calls), 2)
        self.assertEqual(response.metadata["retries"], 1)

    async def test_jev_cannot_be_a_chat_model(self):
        with self.assertRaises(TypeError):
            await JevClient(model="jev-1.14.0").complete([], [])


if __name__ == "__main__":
    unittest.main()
