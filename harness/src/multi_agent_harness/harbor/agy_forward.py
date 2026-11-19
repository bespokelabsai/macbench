"""Relays agy's Gemini API calls from 127.0.0.1 to the hub's proxy, adding the hub's access headers.

agy can't add headers to its model calls, and on some backends (Daytona's preview token) every
request to the hub needs one. This runs in the agent sandbox next to agy, on whatever python3 the
image has (stdlib only). The agent's own proxy key passes through untouched; the header values are
the ones the agent already holds for git and the board.

    python3 agy_forward.py PORTFILE

Listens on an ephemeral 127.0.0.1 port, writes it to PORTFILE, and exits when its parent does.
Env: MULTI_AGENT_HARNESS_GEMINI_UPSTREAM (the proxy's /gemini root),
MULTI_AGENT_HARNESS_HUB_HEADER_NAMES (JSON list) and MULTI_AGENT_HARNESS_HUB_HEADER_1..k.
"""

import http.client
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

UP = urlsplit(os.environ["MULTI_AGENT_HARNESS_GEMINI_UPSTREAM"])
NAMES = json.loads(os.environ.get("MULTI_AGENT_HARNESS_HUB_HEADER_NAMES") or "[]")
HUB = {n: os.environ[f"MULTI_AGENT_HARNESS_HUB_HEADER_{i}"] for i, n in enumerate(NAMES, 1)}
PASS = {"content-type", "accept", "x-goog-api-key", "x-goog-api-client", "user-agent"}
DROP = {"content-length", "transfer-encoding", "connection", "keep-alive", "content-encoding"}


class Relay(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _reply(self, status, message):
        data = json.dumps({"error": {"code": status, "message": message, "status": "UNAVAILABLE"}}).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if not self.path.startswith("/v1beta/"):
            return self._reply(404, "not found")
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        headers = {k: v for k, v in self.headers.items() if k.lower() in PASS}
        headers.update(HUB)  # dict.update and no newer syntax: images may have an old python3
        headers["accept-encoding"] = "identity"
        cls = http.client.HTTPSConnection if UP.scheme == "https" else http.client.HTTPConnection
        conn = cls(UP.netloc, timeout=900)
        try:
            conn.request("POST", UP.path.rstrip("/") + self.path, body=body, headers=headers)
            resp = conn.getresponse()
        except OSError as e:
            conn.close()
            return self._reply(502, f"hub unreachable from the forwarder: {type(e).__name__}")
        try:
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in DROP:
                    self.send_header(k, v)
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            while True:  # streamed as it arrives, for SSE
                chunk = resp.read1(65536)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):  # agy hung up mid-stream (e.g. on an error event)
            self.close_connection = True
        finally:
            conn.close()


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Relay)
    server.daemon_threads = True
    tmp = sys.argv[1] + ".tmp"
    with open(tmp, "w") as f:
        f.write(str(server.server_address[1]))
    os.replace(tmp, sys.argv[1])
    parent = os.getppid()

    def watch():
        while os.getppid() == parent:
            time.sleep(2)
        os._exit(0)

    threading.Thread(target=watch, daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
