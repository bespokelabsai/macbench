"""Run the game host without the board: a small HTTP server with the same /api/game routes, for local development and
for the Harbor oracle on a backend with no harness. One token per seat, given on the command line.

    python3 standalone.py --port 8088 --seats a,b --state DIR --work DIR --logs DIR
"""
import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import host as H


def serve(port, seats, game):
    messages = []  # the team board's messages, what a team of scripted seats tells each other

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            token = self.headers.get("Authorization", "")[7:]
            if not self.path.startswith("/api/inbox"):
                self.reply(404, {"ok": False, "error": "not found"})
            elif token not in seats:
                self.reply(401, {"ok": False, "error": "bad token"})
            else:
                self.reply(200, [m for m in list(messages) if m["from"] != token][-100:])

        def do_POST(self):
            token = self.headers.get("Authorization", "")[7:]
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            endpoint = self.path.rsplit("/", 1)[-1]
            if token not in seats:
                code, reply = 401, {"ok": False, "error": "bad token"}
            elif self.path == "/api/messages":
                messages.append({"from": token, "text": body.get("text")})
                code, reply = 200, {"ok": True}
            elif not self.path.startswith("/api/game/"):
                code, reply = 404, {"ok": False, "error": "not found"}
            else:
                try:
                    code, reply = 200, game.call(token, endpoint, body)
                except H.GameError as e:
                    code, reply = e.code, {"ok": False, "error": str(e), **e.extra}
            self.reply(code, reply)

        def reply(self, code, reply):
            data = json.dumps(reply).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8088)
    ap.add_argument("--seats", required=True)
    ap.add_argument("--state", default=H.STATE)
    ap.add_argument("--work", default="/var/tmp/mindustry-many-fronts")
    ap.add_argument("--logs", default="/var/log/mindustry-many-fronts")
    ap.add_argument("--java", default="java")
    ap.add_argument("--jar", default=None)
    a = ap.parse_args()
    seats = a.seats.split(",")
    game = H.GameHost(seats=lambda: seats, state=a.state, work=a.work, log_dir=a.logs, java=a.java, jar=a.jar)
    threading.Thread(target=game.compile, daemon=True).start()
    threading.Thread(target=game.monitor, daemon=True).start()
    serve(a.port, set(seats), game).serve_forever()


if __name__ == "__main__":
    main()
