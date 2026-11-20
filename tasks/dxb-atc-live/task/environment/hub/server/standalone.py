"""Serve the live day without the board: the same POST /api/atc/<endpoint> routes as atc_plugin.py, with one bearer
token per seat given on the command line. For the Harbor oracle on a backend with no harness (solution/solve.sh) and
for local development. Runs the simulator in this process, so it needs the task image's Python (BlueSky).

    python3 standalone.py --port 8091 --seats controller [--speed 3] [--state /var/lib/dxb-atc] [--auto-start MINUTES]
                          [--reference | --reference-if FILE]

--reference adds the rule-based controller in standin.py for every sector no seat has open. Only the reference
solution uses it; on the live day a sector no seat has open has no controller. --reference-if adds it only if FILE
exists when the day starts (the image's entrypoint creates that root-only file in a run of the reference solution).
Like server.py, it refuses to start when --state already holds the record of a day in progress (exit code 3).
"""
import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import server as S

MAX_BODY_BYTES = 64 * 1024


def serve(live, port, seats):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            auth = self.headers.get("Authorization", "")
            token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
            n = int(self.headers.get("Content-Length", 0))
            if not self.path.startswith("/api/atc/"):
                return self._reply(404, {"ok": False, "error": "not found"})
            if token not in seats:
                return self._reply(401, {"ok": False, "error": "bad token"})
            if n > MAX_BODY_BYTES:
                return self._reply(413, {"ok": False, "error": f"the body is over {MAX_BODY_BYTES} bytes"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._reply(400, {"ok": False, "error": "the body is not JSON"})
            try:
                code, reply = live.call(token, self.path[len("/api/atc/"):], body)
            except Exception as e:  # noqa: BLE001
                code, reply = 500, {"ok": False, "error": f"internal error: {type(e).__name__}: {e}"[:500]}
            return self._reply(code, reply)

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    return srv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8091)
    ap.add_argument("--seats", required=True, help="comma-separated tokens; each token is also the seat's name")
    ap.add_argument("--scenario", default=os.path.join(S.HERE, "scenarios", "live.json"))
    ap.add_argument("--state", default="/var/lib/dxb-atc")
    ap.add_argument("--speed", type=float, default=3.0)
    ap.add_argument("--reference", action="store_true")
    ap.add_argument("--reference-if")
    ap.add_argument("--auto-start", type=float, help="start the day after this many minutes if no seat has called")
    a = ap.parse_args()
    controller = None
    if a.reference:
        from standin import StandIn
        controller = StandIn()
    try:
        live = S.Live(a.scenario, a.state, a.speed, controller=controller)
    except S.DayInProgress as e:
        S.exit_day_in_progress(e)
    if a.reference_if:
        start = live.start

        def start_checked():
            if live.controller is None and os.path.isfile(a.reference_if):
                from standin import StandIn
                live.controller = StandIn()
            start()
        live.start = start_checked
    if a.auto_start is not None:
        live.auto_start(a.auto_start)
    srv = serve(live, a.port, set(a.seats.split(",")))
    print(f"atc standalone on 127.0.0.1:{a.port}, speed {a.speed}x", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
