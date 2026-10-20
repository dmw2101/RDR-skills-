"""Replaceable sources of Markdown, independent of any tree implementation."""

import asyncio

from .contracts import ResolveSkillRequest, ResolvedSkill, source_fingerprint
from .http import json_request


class HttpSkillResolver:
    def __init__(self, base_url, *, timeout=30):
        self.url = base_url.rstrip("/") + "/v1/resolve"
        self.timeout = timeout

    async def resolve(self, request: ResolveSkillRequest) -> ResolvedSkill:
        data = await asyncio.to_thread(json_request, self.url, request.to_dict(), timeout=self.timeout)
        result = ResolvedSkill(**data)
        if result.artifact_id != request.artifact_id:
            raise ValueError("skill service returned a different artifact")
        return result


class StaticSkillResolver:
    def __init__(self, skill_id, artifact_id, markdown, *, empty=False):
        self.skill_id, self.artifact_id = skill_id, artifact_id
        self.markdown = markdown
        self.empty = empty

    async def resolve(self, request):
        if request.skill_id != self.skill_id or request.artifact_id != self.artifact_id:
            raise ValueError("unknown static skill snapshot")
        return ResolvedSkill(
            "" if self.empty else self.markdown, self.artifact_id,
            source_fingerprint(self.markdown), "static-v1",
            {"condition": "none" if self.empty else "full"},
        )
