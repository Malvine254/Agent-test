"""Minimal Bot Connector stand-in for local testing.

Accepts every POST (the bot's outbound reply / streaming activities) on any
path, appends the JSON body to /tmp/replies.jsonl, and returns a ResourceResponse
so the Teams SDK is satisfied. Run on a port the test activity's serviceUrl points at.
"""
import json
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

LOG = "/tmp/replies.jsonl"


class Handler(BaseHTTPRequestHandler):
    def _ok(self):
        body = json.dumps({"id": str(uuid.uuid4())}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            data = {"_raw": raw.decode("utf-8", "replace")}
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"path": self.path, "activity": data}) + "\n")
        self._ok()

    def do_GET(self):
        self._ok()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 3979), Handler).serve_forever()
