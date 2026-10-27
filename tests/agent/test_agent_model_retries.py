import unittest
from unittest.mock import AsyncMock, patch

from skill_agent.http import HTTPStatusError, _retry_after
from skill_agent.models import OpenAICompatibleClient

REPLY = {"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 3, "completion_tokens": 1, "cost": 0.001}}


def client():
    return OpenAICompatibleClient(base_url="http://127.0.0.1:1/v1", model="m", api_key_env=None)


class ModelRetryTests(unittest.IsolatedAsyncioTestCase):
    async def complete(self, responses):
        """Each response is a reply dict or an exception to raise; returns (response, waits, calls)."""
        calls = []

        def fake_request(*args, **kwargs):
            calls.append(args)
            item = responses[len(calls) - 1]
            if isinstance(item, Exception):
                raise item
            return item

        sleep = AsyncMock()
        with patch("skill_agent.models.json_request", side_effect=fake_request), \
                patch("skill_agent.models.asyncio.sleep", sleep):
            response = await client().complete([{"role": "user", "content": "hi"}], [])
        return response, [c.args[0] for c in sleep.await_args_list], len(calls)

    async def test_rate_limits_and_server_errors_are_retried_with_growing_waits(self):
        response, waits, calls = await self.complete(
            [HTTPStatusError(429, "openrouter.ai"), HTTPStatusError(503, "openrouter.ai"), REPLY])
        self.assertEqual(response.message["content"], "ok")
        self.assertEqual((response.metadata["retries"], calls, waits), (2, 3, [5, 15]))
        self.assertEqual(response.usage["cost_usd"], 0.001)  # Only the successful call is billed.

    async def test_retry_after_is_honoured_up_to_a_cap(self):
        _, waits, _ = await self.complete([HTTPStatusError(429, "h", retry_after=40),
                                           HTTPStatusError(429, "h", retry_after=9999), REPLY])
        self.assertEqual(waits, [40, OpenAICompatibleClient.MAX_RETRY_WAIT])

    async def test_other_errors_and_exhausted_retries_raise(self):
        with self.assertRaisesRegex(HTTPStatusError, "HTTP 400"):
            await self.complete([HTTPStatusError(400, "h")])
        refusals = [HTTPStatusError(429, "h")] * (len(OpenAICompatibleClient.retry_delays) + 1)
        with self.assertRaisesRegex(HTTPStatusError, "HTTP 429 from h"):
            await self.complete(refusals)

    def test_retry_after_header_parsing(self):
        self.assertEqual(_retry_after({"Retry-After": "12"}), 12.0)
        self.assertIsNone(_retry_after({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}))
        self.assertIsNone(_retry_after(None))


if __name__ == "__main__":
    unittest.main()
