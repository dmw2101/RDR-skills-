import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, patch

from skill_agent.models import OpenAICompatibleClient
from skill_agent.pdf_session import VenvPDFSession, OutputLimitError, ShellTool, process, venv_identity


class PDFSessionTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(importlib.util.find_spec("reportlab") and importlib.util.find_spec("pypdf"),
                         "install the pdf-runtime extra for PDF integration")
    async def test_extracts_real_pdf_with_venv_python(self):
        from reportlab.pdfgen.canvas import Canvas

        with tempfile.TemporaryDirectory() as temp:
            pdf = Path(temp) / "invoice.pdf"
            canvas = Canvas(str(pdf))
            canvas.drawString(40, 750, "Invoice total 123")
            canvas.save()
            session = await VenvPDFSession(pdf=pdf, schema={"text": "string"}, task="extract").start()
            try:
                output = await session.tools["shell"].execute({"command":
                    "python -c \"import json; from pypdf import PdfReader; "
                    "json.dump({'text':PdfReader('inputs/document.pdf').pages[0].extract_text()}, "
                    "open('answer.json','w',encoding='utf-8'))\""})
                self.assertEqual(output["exit_code"], 0, output)
                self.assertIn("Invoice total 123", (await session.collect())["text"])
            finally:
                await session.close()

    async def test_public_inputs_venv_commands_and_fresh_workspaces(self):
        with tempfile.TemporaryDirectory() as temp:
            pdf = Path(temp) / "original.pdf"
            pdf.write_bytes(b"%PDF fake fixture")
            (Path(temp) / "gold.json").write_text('{"secret":"gold"}')
            session = await VenvPDFSession(pdf=pdf, schema={"type": "object"}, task="extract").start()
            other = await VenvPDFSession(pdf=pdf, schema={}, task="extract").start()
            directory = session.directory
            try:
                self.assertEqual(sorted(p.name for p in (directory / "inputs").iterdir()), ["document.pdf", "schema.json"])
                self.assertNotEqual(directory, other.directory)
                self.assertEqual(session.prompt.count(str(directory)), 0)
                with patch.dict(os.environ, {"MODEL_API_KEY": "must-not-be-inherited"}):
                    # Real shell + Python: check the interpreter, cwd, and secret filtering.
                    result = await session.tools["shell"].execute({"command":
                        "python -c \"import json, os, sys; "
                        "json.dump({'prefix':sys.prefix,'cwd':os.getcwd(),'key':os.getenv('MODEL_API_KEY')}, "
                        "open('answer.json','w'))\""})
                self.assertEqual(result["exit_code"], 0, result)
                value = await session.collect()
                self.assertEqual(value["prefix"], sys.prefix)
                self.assertEqual(Path(value["cwd"]), directory)
                self.assertIsNone(value["key"])
                self.assertFalse((other.directory / "answer.json").exists())
                with patch("skill_agent.pdf_session.process", new=AsyncMock(return_value=(0, "ok"))) as command:
                    await session.tools["shell"].execute({"command": "python --version"})
                self.assertEqual(command.call_args.kwargs["timeout"], 60)
                self.assertEqual(command.call_args.kwargs["limit"], 128_000)
            finally:
                await session.close()
                await other.close()
            self.assertFalse(directory.exists())
            await session.close()  # Idempotent cleanup.

    async def test_subprocess_time_and_output_limits(self):
        with self.assertRaises(TimeoutError):
            await process(sys.executable, "-c", "import time; time.sleep(10)", timeout=.05)
        with self.assertRaisesRegex(OutputLimitError, "output budget"):
            await process(sys.executable, "-c", "print('x'*5000)", limit=100)

    async def test_invalid_answer_is_not_repaired(self):
        with tempfile.TemporaryDirectory() as temp:
            session = VenvPDFSession(pdf="unused", schema={}, task="test")
            session.directory = Path(temp)
            with self.assertRaisesRegex(ValueError, "no answer"):
                await session.collect()
            for value in ("not json", "[]", '{"_error":"bad"}'):
                (session.directory / "answer.json").write_text(value)
                with self.assertRaises(ValueError):
                    await session.collect()
            (session.directory / "answer.json").write_text("x" * 1_000_001)
            with self.assertRaises(OutputLimitError):
                await session.collect()
            (session.directory / "answer.json").write_bytes(b'\xef\xbb\xbf{"result":1}')
            self.assertEqual(await session.collect(), {"result": 1})

    async def test_runtime_requires_venv_dependencies_and_records_tools(self):
        probe = {"venv": True, "python": "test", "prefix": "/venv", "packages": {"pypdf": "5.9.0"}}
        with patch("skill_agent.pdf_session.process", new=AsyncMock(return_value=(0, json.dumps(probe)))), patch(
                "skill_agent.pdf_session.shutil.which", return_value=None):
            identity = await venv_identity(sys.executable)
        self.assertEqual(identity["runtime"], "local-venv")
        self.assertEqual(identity["packages"], probe["packages"])
        self.assertIsNone(identity["system_tools"]["pdftotext"])
        for result, message in (((1, "missing package"), "dependencies"),
                                ((0, '{"venv":false}'), "venv's Python")):
            with patch("skill_agent.pdf_session.process", new=AsyncMock(return_value=result)):
                with self.assertRaisesRegex(ValueError, message):
                    await venv_identity(sys.executable)


class ModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_compatible_request_and_tool_response_preserve_usage(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                received.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                data = {"id": "example", "model": "test-model", "choices": [{"finish_reason": "tool_calls",
                    "message": {"role": "assistant", "content": None, "tool_calls": [
                        {"id": "one", "type": "function", "function": {"name": "shell", "arguments": "{}"}}]}}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 3, "prompt_tokens_details": {"cached_tokens": 4}}}
                raw = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.dict(os.environ, {"TEST_MODEL_KEY": "test-only"}):
                client = OpenAICompatibleClient(base_url=f"http://127.0.0.1:{server.server_port}/v1",
                    model="test-model", api_key_env="TEST_MODEL_KEY", seed=42)
                result = await client.complete([{"role": "user", "content": "hello"}], [ShellTool.definition])
            self.assertEqual(received[0][0], "/v1/chat/completions")
            self.assertEqual(received[0][1]["seed"], 42)
            self.assertNotIn("test-only", json.dumps(received))
            self.assertEqual(result.usage["cached_input_tokens"], 4)
            self.assertEqual(result.message["tool_calls"][0]["id"], "one")
            self.assertIsNone(result.usage["cost_usd"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_unencrypted_remote_endpoint_rejected(self):
        with self.assertRaises(ValueError):
            OpenAICompatibleClient(base_url="http://remote.example/v1", model="x")
