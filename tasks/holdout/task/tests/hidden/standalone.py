"""The Holdout game host for one survivor, without the harness's hub: the game behind a small HTTP server on
127.0.0.1. solution/solve.sh uses it to play the reference bot with plain Harbor. A player reaches the game only
through this rate-limited API.

    python3 standalone.py --port 8088 --token agent --state /var/lib/holdout --stop-after 7200

POST /api/game/<status|look|events|act> with "Authorization: Bearer <token>" -> the seat "agent".
POST /api/admin/game/freeze with the admin token in <state>/admin.token (root-only) freezes the world.
--stop-after N freezes the world N seconds after it starts, so the score is read from a still world.
"""
import argparse
import json
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from holdout.engine import GameError  # noqa: E402
from holdout.host import MAX_BODY_BYTES, GameHost  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8088)
    ap.add_argument("--token", default="agent")
    ap.add_argument("--state", default="/var/lib/holdout")
    ap.add_argument("--stop-after", type=float, default=0)
    a = ap.parse_args()
    os.makedirs(a.state, exist_ok=True)
    os.chmod(a.state, 0o700)
    admin = secrets.token_urlsafe(24)
    with open(os.path.join(a.state, "admin.token"), "w") as f:
        f.write(admin)
    os.chmod(os.path.join(a.state, "admin.token"), 0o600)

    def emit(kind, agent, **fields):
        print(kind, agent, fields, file=sys.stderr, flush=True)

    host = GameHost(seats=lambda: ["agent"], emit=emit, state_dir=a.state)

    if a.stop_after:
        def stopper():
            while True:
                time.sleep(5)
                if host.t_start is not None and host.elapsed() >= a.stop_after:
                    host.freeze()
                    return
        threading.Thread(target=stopper, daemon=True).start()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            auth = self.headers.get("Authorization", "")
            tok = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY_BYTES:
                return self._send(413, {"ok": False, "error": f"the body is over {MAX_BODY_BYTES} bytes"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._send(400, {"ok": False, "error": "the body is not JSON"})
            if self.path == "/api/admin/game/freeze":
                if not secrets.compare_digest(tok, admin):
                    return self._send(401, {"ok": False, "error": "admin only"})
                return self._send(200, host.freeze())
            if not self.path.startswith("/api/game/"):
                return self._send(404, {"ok": False, "error": "not found"})
            if not secrets.compare_digest(tok, a.token):
                return self._send(401, {"ok": False, "error": "bad token"})
            try:
                return self._send(200, host.call("agent", self.path[len("/api/game/"):], body))
            except GameError as e:
                return self._send(e.code, {"ok": False, "error": str(e), **e.extra})

    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()


if __name__ == "__main__":
    main()
