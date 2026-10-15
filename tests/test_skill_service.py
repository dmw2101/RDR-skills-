import asyncio
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from skill_agent.contracts import ResolveSkillRequest, ResolvedSkill, source_fingerprint
from skill_agent.http import json_request
from skill_agent.resolvers import HttpSkillResolver
from rdr_service.backend import RDRBackend
from rdr_service.server import make_server

ROOT = Path(__file__).resolve().parents[1]


class ServiceTests(unittest.TestCase):
    def start(self, backend):
        server = make_server(backend, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join()
        self.addCleanup(close)
        return f"http://127.0.0.1:{server.server_port}"

    def test_http_contract_works_for_two_unrelated_backends(self):
        class Backend:
            def __init__(self, text):
                self.text = text
            def resolve(self, request):
                return ResolvedSkill(self.text, request.artifact_id, "same-source", "v1", {"arbitrary": [1, 2]})
        for text in ("tree result", "not even a tree"):
            url = self.start(Backend(text))
            result = asyncio.run(HttpSkillResolver(url).resolve(ResolveSkillRequest("pdf", "read", {}, "opaque")))
            self.assertEqual(result.markdown, text)
            self.assertEqual(result.metadata, {"arbitrary": [1, 2]})
            self.assertEqual(json_request(url + "/health")["contract"], "v1")

    def test_service_matches_current_renderer_and_snapshot_survives_source_change(self):
        from rdr_engine.skill_cases import SkillConditionEvaluator, build_case
        from rdr_engine.skill_compiler import FeatureSchema, SkillBundle
        from rdr_engine.skill_renderer import SkillRenderer
        import shutil
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "alfworld"
            shutil.copytree(ROOT / "skills/alfworld", source)
            backend = RDRBackend(Path(temporary) / "store")
            receipt = backend.prepare(source)
            task = "Heat an apple and put it in a bowl"
            request = ResolveSkillRequest("alfworld", task, {}, receipt["artifact_id"])
            result = backend.resolve(request)
            schema = FeatureSchema.model_validate(json.loads((source / "compiled/manifest.json").read_text())["feature_schema"])
            engine, fragments = SkillBundle.load(source, SkillConditionEvaluator(schema))
            rendered = SkillRenderer().render(engine.predict(build_case("alfworld", task, {}, schema)), engine, fragments)
            self.assertEqual(result.markdown, rendered.markdown)
            (source / "initial.md").write_text("replaced source")
            self.assertEqual(backend.resolve(request).markdown, result.markdown)
            url = self.start(backend)
            self.assertEqual(asyncio.run(HttpSkillResolver(url).resolve(request)).markdown, rendered.markdown)

    def test_snapshot_tampering_and_changed_implementation_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            backend = RDRBackend(temporary)
            receipt = backend.prepare(ROOT / "skills/alfworld")
            request = ResolveSkillRequest("alfworld", "heat", {}, receipt["artifact_id"])
            backend.revision = "new-code"
            with self.assertRaisesRegex(ValueError, "code changed"):
                backend.resolve(request)
            backend.revision = receipt["resolver_revision"]
            (Path(temporary) / receipt["artifact_id"] / "compiled/tree.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "content changed"):
                backend.resolve(request)

    def test_pdf_service_selects_different_views_for_different_tasks(self):
        with tempfile.TemporaryDirectory() as temporary:
            backend = RDRBackend(temporary)
            receipt = backend.prepare(ROOT / "skills/pdf")
            results = [backend.resolve(ResolveSkillRequest("pdf", task, {}, receipt["artifact_id"]))
                       for task in ("Extract text from the PDF", "Rotate PDF pages")]
            self.assertNotEqual(results[0].markdown, results[1].markdown)
            self.assertNotEqual(results[0].metadata["fragment_ids"], results[1].metadata["fragment_ids"])
            self.assertEqual(results[0].source_fingerprint, results[1].source_fingerprint)

    def test_invalid_requests_and_wrong_skill_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            backend = RDRBackend(temporary)
            receipt = backend.prepare(ROOT / "skills/alfworld")
            for skill, artifact in (("pdf", receipt["artifact_id"]), ("alfworld", "../x")):
                with self.assertRaises(ValueError):
                    backend.resolve(ResolveSkillRequest(skill, "task", {}, artifact))
            url = self.start(backend)
            with self.assertRaises(RuntimeError):
                json_request(url + "/v1/resolve", {"task": "incomplete"})

    def test_contract_rejects_invalid_json_and_usage(self):
        for context in ([1], {"nan": float("nan")}):
            with self.assertRaises(ValueError):
                ResolveSkillRequest("pdf", "task", context, "opaque")
        with self.assertRaises(ValueError):
            ResolvedSkill("text", "opaque", "source", "v1", usage={"input_tokens": -1})

    def test_agent_imports_without_rdr_gemini_or_site_packages(self):
        script = """
import sys
sys.path.insert(0, sys.argv[1])
class Blocker:
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in ('rdr_engine', 'rdr_service', 'google', 'pydantic'):
            raise AssertionError('agent depended on ' + fullname)
sys.meta_path.insert(0, Blocker())
import skill_agent.runtime, skill_agent.models, skill_agent.experiments, skill_agent.__main__
"""
        subprocess.run([sys.executable, "-S", "-c", script, str(ROOT / "src")], check=True, capture_output=True)
