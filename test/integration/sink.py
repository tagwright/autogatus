# SPDX-License-Identifier: Apache-2.0
"""Request-recording alert sink for the live harness.

Gatus's custom provider POSTs each triggered and resolved alert to /alert.
The sink keeps every request in memory and returns them as JSON from
/_alerts. /health answers 200 for Gatus's base endpoint.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

_lock = threading.Lock()
_alerts: list = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(fmt % args, flush=True)

    def _send(self, code, body=b"", ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/health":
            self._send(200, b"ok")
        elif path == "/_alerts":
            with _lock:
                body = json.dumps(_alerts).encode()
            self._send(200, body, "application/json")
        else:
            self._send(404)

    def do_POST(self):
        parts = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace")
        if parts.path != "/alert":
            self._send(404)
            return
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        with _lock:
            _alerts.append({"ts": time.time(), "query": query, "body": body})
        self._send(200, b"ok")


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
