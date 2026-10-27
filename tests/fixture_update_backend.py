#!/usr/bin/env python3
"""Disposable compatibility-test protocol; never model inference."""

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if "--help" in sys.argv:
    print("--host --port --context")
    raise SystemExit(0)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        value = (
            [{"is_processing": False, "n_ctx": 4096}] if self.path == "/slots" else {"status": "ok"}
        )
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(value).encode())

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        mode = Path(__file__).with_name("mode")
        mode = mode.read_text() if mode.exists() else "ok"
        if mode == "slow":
            time.sleep(1)
        text = "INCORRECT" if mode == "bad_answer" else "IGM_UPDATE_OK"
        body = json.dumps({"choices": [{"message": {"content": text}}]}).encode()
        if payload.get("stream"):
            body = (
                "data: "
                + json.dumps({"choices": [{"delta": {"content": text}}]})
                + "\n\n"
                + ("" if mode == "bad_stream" else "data: [DONE]\n\n")
            ).encode()
        self.send_response(200)
        self.send_header(
            "Content-Type", "text/event-stream" if payload.get("stream") else "application/json"
        )
        self.end_headers()
        self.wfile.write(body)


# Actual script interpreter is pinned by the test setup, not this fixture's shebang.
if os.environ.get("IGM_UPDATE_FIXTURE_RECEIPT"):
    with Path(os.environ["IGM_UPDATE_FIXTURE_RECEIPT"]).open("a") as receipt:
        receipt.write(str(os.getpid()) + "\n")
ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
