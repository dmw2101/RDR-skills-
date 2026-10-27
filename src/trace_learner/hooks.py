"""Benchmark-side capabilities the verifier needs; gold labels never reach skill_agent sessions."""

from typing import Protocol


class VerificationHooks(Protocol):
    benchmark: str  # Short name used in the verifier prompt.

    def outcomes(self, records, output_dir) -> dict[str, bool]:
        """Official pass/fail per episode key (records.episode_key)."""
        ...

    def ground_truth(self, task) -> object: ...

    async def rescore(self, task, prediction) -> dict:
        """Official verdict for a candidate prediction: {"passed": bool, "details": ...}."""
        ...

    async def open_inspection(self, task):
        """A fresh session over the task's original public inputs, exposing a "shell" tool."""
        ...
