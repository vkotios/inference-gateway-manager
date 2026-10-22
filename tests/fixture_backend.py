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
        value = (
            [{"id": 0, "is_processing": False, "n_ctx": 4096}]
            if self.path == "/slots"
            else {"status": "ok"}
        )
        # Optional source-derived protocol snapshot for engine contract tests.
        slots = Path(sys.argv[2]) / "slots.json" if len(sys.argv) > 2 else None
        if self.path == "/slots" and slots is not None and slots.is_file():
            value = json.loads(slots.read_text())
        # Optional warmup simulation: the slot stays busy while --busy-while-exists=<path> exists.
        if self.path == "/slots" and Handler.busy_until is not None:
            busy = Path(Handler.busy_until).exists()
            value = [{"id": 0, "is_processing": busy, "n_ctx": 4096}]
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


# Opt-in only: other tests pass arbitrary paths and settings as positional argv.
for arg in sys.argv[2:]:
    if arg.startswith("--busy-while-exists="):
        Handler.busy_until = arg.split("=", 1)[1]
ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
