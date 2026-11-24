"""A local stand-in for AgentGuard's ingestion API, for tests and manual runs.

Run alone:  python3 tests/fake_server.py [port]   (prints a summary of each batch)
"""

import base64
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


class FakeAgentGuard(object):
    """Records requests. `status`, `delay` and `event_errors` control the answers."""

    def __init__(self, public_key="pk-lf-test", secret_key="sk-lf-test-secret-0000", status=207, delay=0.0):
        self.public_key = public_key
        self.secret_key = secret_key
        self.status = status
        self.delay = delay
        self.event_errors = None  # callable(event) -> (status, message) or None
        self.requests = []
        self.lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _authorized(self):
                expected = "Basic " + base64.b64encode(("%s:%s" % (owner.public_key, owner.secret_key)).encode()).decode()
                return self.headers.get("Authorization") == expected

            def _reply(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if owner.delay:
                    time.sleep(owner.delay)
                if not self._authorized():
                    return self._reply(401, {"message": "Invalid credentials"})
                if self.path == "/api/public/projects":
                    return self._reply(200, {"data": [{"id": "p1", "name": "test-project"}]})
                return self._reply(404, {"message": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                if owner.delay:
                    time.sleep(owner.delay)
                with owner.lock:
                    owner.requests.append({"path": self.path, "headers": dict(self.headers), "raw": raw})
                if not self._authorized():
                    return self._reply(401, {"message": "Invalid credentials"})
                if owner.status >= 400:
                    return self._reply(owner.status, {"message": "fake failure"})
                body = json.loads(raw)
                successes, errors = [], []
                for ev in body.get("batch", []):
                    err = owner.event_errors(ev) if owner.event_errors else None
                    if err:
                        errors.append({"id": ev["id"], "status": err[0], "message": err[1]})
                    else:
                        successes.append({"id": ev["id"], "status": 201})
                return self._reply(owner.status, {"successes": successes, "errors": errors})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def events(self):
        """All ingestion events received with valid auth, in order."""
        out = []
        for r in self.requests:
            if r["path"] == "/api/public/ingestion":
                out.extend(json.loads(r["raw"]).get("batch", []))
        return out


if __name__ == "__main__":
    srv = FakeAgentGuard()
    if len(sys.argv) > 1:
        srv.server.server_close()
        srv.server = HTTPServer(("127.0.0.1", int(sys.argv[1])), srv.server.RequestHandlerClass)
    srv.start()
    print("fake AgentGuard at %s (public key %s, secret %s)" % (srv.url, srv.public_key, srv.secret_key))
    seen = 0
    try:
        while True:
            time.sleep(0.5)
            evs = srv.events()
            for ev in evs[seen:]:
                b = ev["body"]
                print(ev["type"], b.get("name"), b.get("model") or "", json.dumps(b.get("usageDetails") or {}))
            seen = len(evs)
    except KeyboardInterrupt:
        srv.stop()
