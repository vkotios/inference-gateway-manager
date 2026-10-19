"""Disposable HTTP backend for tests; never used as an inference result."""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Handler(BaseHTTPRequestHandler):
    busy_until = None

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/slots" and Handler.busy_until is not None:
            busy = Path(Handler.busy_until).exists()
            value = [{"id": 0, "is_processing": busy, "n_ctx": 4096}]
        elif self.path == "/slots":
            value = [{"id": 0, "is_processing": False, "n_ctx": 4096}]
        else:
            value = {"status": "ok"}
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b"data: fixture\n\ndata: [DONE]\n\n")


if len(sys.argv) > 3:
    Handler.busy_until = sys.argv[3]
ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
