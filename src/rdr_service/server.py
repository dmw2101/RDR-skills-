"""Local-only versioned HTTP boundary, also usable with a different backend."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from skill_agent.contracts import ResolveSkillRequest


def make_server(backend, host="127.0.0.1", port=8765):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(30)

        def log_message(self, *args):
            pass  # No task text in default HTTP logs.

        def reply(self, status, value):
            raw = json.dumps(value, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path == "/health":
                self.reply(200, {"contract": "v1", "status": "ok"})
            else:
                self.reply(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/v1/resolve":
                self.reply(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1_000_000:
                    raise ValueError("request exceeds size limit or is empty")
                data = json.loads(self.rfile.read(length))
                request = ResolveSkillRequest(**data)
                result = backend.resolve(request)
            except (ValueError, TypeError, KeyError, OSError) as exc:
                self.reply(400, {"error": type(exc).__name__})
                return
            except Exception:
                self.reply(500, {"error": "resolution failed"})
                return
            self.reply(200, result.to_dict())

    return ThreadingHTTPServer((host, port), Handler)
