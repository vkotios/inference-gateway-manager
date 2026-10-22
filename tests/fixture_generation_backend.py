"""Disposable backend that streams scripted events; never used as an inference result.

argv: PORT MARKER --sse-script=FILE --json-reply=FILE [--sse-delay=SECONDS]
"""

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

OPTIONS = dict(arg[2:].split("=", 1) for arg in sys.argv[3:] if arg.startswith("--"))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply_json(self, value):
        payload = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self.reply_json(
            [{"id": 0, "is_processing": False, "n_ctx": 4096}]
            if self.path == "/slots"
            else {"status": "ok"}
        )

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        if not body.get("stream"):
            return self.reply_json(json.loads(Path(OPTIONS["json-reply"]).read_text()))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for event in json.loads(Path(OPTIONS["sse-script"]).read_text()):
            self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
            self.wfile.flush()
            time.sleep(float(OPTIONS.get("sse-delay", 0)))
        self.wfile.write(b"data: [DONE]\n\n")


ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
