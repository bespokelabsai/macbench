"""Board plugin: the controller API for every seat, on the hub.

Load it with --ek board_plugins=<generated hub plugin>,<this file>. It runs inside the board, so it uses only the
standard library and the board's own packages. The simulator itself runs in a separate process with the task image's
Python (BlueSky needs numpy and friends): /opt/dxb-atc/server.py, unpacked by the generated hub plugin
(hub_bundle.py). This plugin starts it, forwards each seat's calls to it on 127.0.0.1, and stops it at the end.
A seat is an agent's board identity: its bearer token picks the seat, and a seat named in a request body is ignored.
"""
# No `from __future__ import annotations`: the MCP server reads the tool signatures, and Context is imported in register().
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request

PORT = int(os.environ.get("ATC_PORT", "8092"))
PYTHON = os.environ.get("ATC_PYTHON", "/usr/local/bin/python3")
SERVER = os.environ.get("ATC_SERVER", "/opt/dxb-atc/server.py")
STATE = os.environ.get("ATC_STATE", "/var/lib/dxb-atc")
SPEED = os.environ.get("ATC_SPEED", "3")
MAX_BODY_BYTES = 64 * 1024
DAY_IN_PROGRESS = 3   # server.py's exit code when its state already holds a day in progress: it is not started again


class Host:
    def __init__(self, emit):
        self.emit = emit
        self.proc = None
        self.lock = threading.Lock()
        self.log = open("/var/log/dxb-atc-server.log", "a") if os.access("/var/log", os.W_OK) else subprocess.DEVNULL

    def ensure(self):
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return
            if self.proc is not None and self.proc.returncode == DAY_IN_PROGRESS:
                raise OSError("the simulator will not start a new day over the day in progress (see "
                              "/var/log/dxb-atc-server.log)")
            if self.proc is not None:
                self.emit("atc", None, op="server_exit", code=self.proc.returncode)
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", HOME=os.environ.get("HOME", "/root"))
            env.pop("PYTHONPATH", None)
            self.proc = subprocess.Popen([PYTHON, "-u", SERVER, "--port", str(PORT), "--state", STATE, "--speed", SPEED],
                                         stdout=self.log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                                         cwd=os.path.dirname(SERVER))
        for _ in range(300):
            try:
                self.post("/health", {}, timeout=2)
                return
            except OSError:
                if self.proc.poll() is not None:   # it exited at once: the next call reports or restarts it
                    if self.proc.returncode == DAY_IN_PROGRESS:
                        self.emit("atc", None, op="server_refused", code=self.proc.returncode)
                    return
                time.sleep(0.5)

    def post(self, path, body, timeout=60):
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", json.dumps(body).encode(), {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def call(self, seat, endpoint, body):
        self.ensure()
        return self.post("/call", {"seat": seat, "endpoint": endpoint, "body": body})

    def freeze(self):
        try:
            return self.post("/freeze", {}, timeout=120)[1]
        except OSError as e:
            return {"phase": "unknown", "error": str(e)}


def register(board, api, mcp):
    from fastapi import Header, Request
    from fastapi.concurrency import run_in_threadpool
    from fastapi.responses import JSONResponse
    from mcp.server.mcpserver import Context
    from mcp.server.mcpserver.exceptions import ToolError

    def emit(kind, agent, **fields):
        try:
            board._emit(kind, agent, **fields)
        except Exception:  # noqa: BLE001 - logging only
            pass

    host = Host(emit)
    threading.Thread(target=host.ensure, daemon=True, name="atc-start").start()

    def bearer(value):
        return value[7:].strip() if value and value.lower().startswith("bearer ") else None

    @api.post("/atc/{endpoint}")
    async def atc(endpoint: str, request: Request, authorization: str = Header(None)):
        try:
            seat = board.authenticate(bearer(authorization))
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=401)
        raw = bytearray()
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > MAX_BODY_BYTES:
                return JSONResponse({"ok": False, "error": f"the body is over {MAX_BODY_BYTES} bytes"}, status_code=413)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return JSONResponse({"ok": False, "error": "the body is not JSON"}, status_code=400)
        try:
            code, reply = await run_in_threadpool(host.call, seat, endpoint, body)
        except OSError as e:
            return JSONResponse({"ok": False, "error": f"simulator unavailable: {e}"}, status_code=503)
        return JSONResponse(reply, status_code=code)

    @api.post("/admin/atc/freeze")
    def freeze(authorization: str = Header(None)):
        api.state.require_admin(authorization)
        return host.freeze()

    def tool(ctx: Context, endpoint, body):
        try:
            seat = board.authenticate(bearer((ctx.headers or {}).get("authorization")))
        except Exception as e:  # noqa: BLE001
            raise ToolError(str(e)) from None
        code, reply = host.call(seat, endpoint, body)
        if code != 200:
            raise ToolError(json.dumps(reply)[:4000])
        return reply

    @mcp.tool()
    def atc_status(ctx: Context) -> dict:
        """The clock, runway configuration, your sectors and frequency, and the weather. See docs/API.md."""
        return tool(ctx, "status", {})

    @mcp.tool()
    def atc_query(of: str, ctx: Context, where: dict = None, ids: list = None, limit: int = None, offset: int = None) -> dict:
        """Read the radar (traffic), strips (flights), ground, runways, stands, weather, sectors, flow or chart."""
        args = {"of": of, "where": where, "ids": ids, "limit": limit, "offset": offset}
        return tool(ctx, "query", {k: v for k, v in args.items() if v is not None})

    @mcp.tool()
    def atc_events(ctx: Context, after: int = None, kinds: list = None, limit: int = None) -> dict:
        """Your frequency's radio traffic and the airport's events after a sequence number."""
        args = {"after": after, "kinds": kinds, "limit": limit}
        return tool(ctx, "events", {k: v for k, v in args.items() if v is not None})

    @mcp.tool()
    def atc_act(ops: list, ctx: Context) -> dict:
        """Instructions and actions, in order: [{"op": "approach", "flight": "UAE202", "runway": "30L"}, ...]. See docs/API.md."""
        return tool(ctx, "act", {"ops": ops})

    return host
