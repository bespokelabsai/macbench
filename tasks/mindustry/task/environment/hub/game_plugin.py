"""Board plugin for Mindustry Many Fronts: the game API, for every seat, on the hub.

Load it with --ek board_plugins=<generated hub plugin>,<this file>. It runs in the board on the hub, so it uses the
standard library and the board's own packages. All game logic is in host.py and the Java bridge, unpacked to
/opt/mindustry-many-fronts by the generated plugin (make_hub_plugin.py). A seat is an agent's board identity: its bearer
token picks the seat, and a seat named in a request body is ignored.
"""
# No `from __future__ import annotations`: the MCP server reads the tool signatures, and Context is imported in register().
import importlib.util
import os
import threading


def _load_host():
    path = os.environ.get("MMF_HOST_PY", "/opt/mindustry-many-fronts/host.py")
    spec = importlib.util.spec_from_file_location("mmf_host", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def register(board, api, mcp):
    import json

    from fastapi import Header, Request
    from fastapi.concurrency import run_in_threadpool
    from fastapi.responses import JSONResponse
    from mcp.server.mcpserver import Context
    from mcp.server.mcpserver.exceptions import ToolError

    H = _load_host()

    def emit(kind, agent, **fields):
        try:
            board._emit(kind, agent, **fields)
        except Exception:  # noqa: BLE001 - logging only
            pass

    host = H.GameHost(seats=lambda: list(board.agents()), emit=emit)
    threading.Thread(target=host.compile, daemon=True, name="mmf-compile").start()
    threading.Thread(target=host.monitor, daemon=True, name="mmf-monitor").start()

    def bearer(value):
        return value[7:].strip() if value and value.lower().startswith("bearer ") else None

    @api.post("/game/{endpoint}")
    async def game(endpoint: str, request: Request, authorization: str = Header(None)):
        try:
            seat = board.authenticate(bearer(authorization))
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=401)
        raw = bytearray()
        async for chunk in request.stream():  # stop reading at the cap, so a huge body costs this process nothing
            raw += chunk
            if len(raw) > H.MAX_BODY_BYTES:
                return JSONResponse({"ok": False, "error": f"the body is over {H.MAX_BODY_BYTES} bytes"}, status_code=413)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return JSONResponse({"ok": False, "error": "the body is not JSON"}, status_code=400)
        try:
            return await run_in_threadpool(host.call, seat, endpoint, body, len(raw))
        except H.GameError as e:
            return JSONResponse({"ok": False, "error": str(e), **e.extra}, status_code=e.code)

    @api.post("/admin/game/freeze")
    def freeze(authorization: str = Header(None)):
        api.state.require_admin(authorization)
        return host.freeze()

    def tool(ctx: Context, endpoint, body):
        try:
            seat = board.authenticate(bearer((ctx.headers or {}).get("authorization")))
            return host.call(seat, endpoint, body)
        except Exception as e:  # noqa: BLE001
            raise ToolError(str(e)) from None

    @mcp.tool()
    def game_status(ctx: Context, unit: str = None) -> dict:
        """One of your units, your team's core, the wave number and the world index. your_units lists your units. See docs/API.md."""
        return tool(ctx, "status", {"unit": unit} if unit else {})

    @mcp.tool()
    def game_query(of: str, ctx: Context, filter: list = None, area: dict = None, fields: list = None,
                   limit: int = None, after: int = None, unit: str = None) -> dict:
        """Read a collection, within this unit's sight (default your first unit). of="collections" lists them. See docs/API.md."""
        args = {"filter": filter, "area": area, "fields": fields, "limit": limit, "after": after, "unit": unit}
        return tool(ctx, "query", {"of": of, **{k: v for k, v in args.items() if v is not None}})

    @mcp.tool()
    def game_events(ctx: Context, latest: int = None, after: int = None, cursor: str = None, filter: list = None,
                    fields: list = None, limit: int = None, unit: str = None) -> dict:
        """Read this unit's event log (default your first unit). Give exactly one of latest, after or cursor. See docs/API.md."""
        args = {"latest": latest, "after": after, "cursor": cursor, "filter": filter, "fields": fields, "limit": limit, "unit": unit}
        return tool(ctx, "events", {k: v for k, v in args.items() if v is not None})

    @mcp.tool()
    def game_act(ops: list, ctx: Context, unit: str = None) -> dict:
        """Apply inputs in order for this unit (default your first unit): {"op": ..., ...}, at most 16 per call and 64 list items per op. of="ops" in game_query gives every op's schema."""
        return tool(ctx, "act", {"ops": ops, **({"unit": unit} if unit else {})})

    return host
