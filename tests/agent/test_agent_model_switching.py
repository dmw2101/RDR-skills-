"""Local model servers that take turns on one GPU (SwitchingClient), with fake vLLM servers."""

import asyncio
import unittest
from unittest.mock import patch

from skill_agent.experiments import timing_summary
from skill_agent.models import SwitchingClient

SMALL, BIG = "http://127.0.0.1:8000", "http://127.0.0.1:8001"
REPLY = {"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 3, "completion_tokens": 1}}


class FakeServers:
    """Two vLLM servers: their sleep state, every control call, and calls in flight per server."""

    def __init__(self, sleeping):
        self.sleeping, self.controls, self.in_flight, self.overlap = dict(sleeping), [], {SMALL: 0, BIG: 0}, False

    def is_sleeping(self, server):
        return self.sleeping[server]

    def control(self, server, path):
        self.controls.append((server, path))
        self.sleeping[server] = path.startswith("/sleep")

    async def post(self, client, payload):
        if self.sleeping[client.server]:
            raise AssertionError(f"called {client.server} while it sleeps")
        self.in_flight[client.server] += 1
        self.overlap |= all(self.in_flight.values())  # Both servers busy at once must never happen.
        await asyncio.sleep(0.01)
        self.in_flight[client.server] -= 1
        return REPLY, 0, {"latency_seconds": 0.01, "retry_wait_seconds": 0.0}


def client(port, peer):
    return SwitchingClient(base_url=f"http://127.0.0.1:{port}/v1", model="m", api_key_env=None,
                           peers=[f"http://127.0.0.1:{peer}/v1"])


class SwitchingTests(unittest.IsolatedAsyncioTestCase):
    async def run_calls(self, servers, *batches):
        """Each batch runs concurrently; batches run in order. Returns the responses."""
        async def post(client_self, payload):
            return await servers.post(client_self, payload)

        responses = []
        with patch("skill_agent.models._is_sleeping", side_effect=servers.is_sleeping), \
                patch("skill_agent.models._control", side_effect=servers.control), \
                patch.object(SwitchingClient, "_post", post):
            for batch in batches:
                responses += await asyncio.gather(*(c.complete([{"role": "user", "content": "x"}], [])
                                                    for c in batch))
        return responses

    async def test_turns_switch_only_when_needed_and_never_overlap(self):
        servers = FakeServers({SMALL: False, BIG: True})  # The small model starts awake.
        small, big = client(8000, 8001), client(8001, 8000)
        responses = await self.run_calls(servers, [small, small], [small, big, big], [small])
        self.assertEqual(servers.controls, [(SMALL, "/sleep?level=1"), (BIG, "/wake_up"),
                                            (BIG, "/sleep?level=1"), (SMALL, "/wake_up")])
        self.assertFalse(servers.overlap)
        self.assertTrue(all(r.message["content"] == "ok" for r in responses))
        self.assertEqual([r.metadata["switch_seconds"] > 0 for r in responses[:2]], [False, False])
        self.assertTrue(all("latency_seconds" in r.metadata for r in responses))

    def test_only_distinct_local_servers(self):
        with self.assertRaisesRegex(ValueError, "local"):
            SwitchingClient(base_url="https://api.example.com/v1", model="m", api_key_env=None,
                            peers=["http://127.0.0.1:8001/v1"])
        with self.assertRaisesRegex(ValueError, "distinct"):
            client(8000, 8000)

    def test_timing_summary_splits_an_arms_time(self):
        model = [{"metadata": {"latency_seconds": 2.0, "switch_seconds": 1.0, "retry_wait_seconds": 0.0}},
                 {"metadata": {"latency_seconds": 4.0, "switch_seconds": 0.0, "retry_wait_seconds": 5.0}}]
        skill = [{"seconds": 1.5, "response": {"metadata": {"feature_judge": {"latency_seconds": 1.2}}}}]
        summary = timing_summary([{"seconds": 20.0}], model, skill)
        self.assertEqual((summary["model_seconds"], summary["model_seconds_per_call"]), (6.0, 3.0))
        self.assertEqual((summary["judge_calls"], summary["judge_seconds"]), (1, 1.2))
        self.assertEqual((summary["model_switch_seconds"], summary["retry_wait_seconds"]), (1.0, 5.0))
        self.assertAlmostEqual(summary["other_seconds"], 20.0 - 6.0 - 1.5 - 1.0 - 5.0)
        old = timing_summary([{"seconds": 3.0}], [{"metadata": {}}], [])  # Records from before timing.
        self.assertIsNone(old["model_seconds"])
        self.assertIsNone(old["other_seconds"])


if __name__ == "__main__":
    unittest.main()
