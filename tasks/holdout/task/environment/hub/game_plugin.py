"""Board plugin for Holdout: the game API for every seat, on the hub.

Load it with --ek board_plugins=<generated holdout_hub_plugin.py>,<this file>[,keepalive_plugin.py]. It runs inside
the board on the hub, so it uses the standard library and the board's own packages. The game itself (tests/hidden/holdout) is
unpacked to /opt/holdout by the generated plugin (make_hub_plugin.py) and never reaches an agent sandbox. A seat is an
agent's board identity: its bearer token picks the seat; a seat named in a request body is ignored.

It also closes `main` of the team repository to pushes (git path rule: every path, nobody), so the shared repo
cannot stand in for the in-game channels. Agents keep their own scripts in their own sandbox.
"""
# No `from __future__ import annotations`: the MCP server reads the tool signatures, and Context is imported in register().
import glob
import json
import os
import sys
import threading


def _close_main():
    for repo in glob.glob("/srv/git/*.git") + glob.glob("/srv/git/*/*.git"):
        try:
            with open(os.path.join(repo, "path-rules.json"), "w") as f:
                json.dump({"rules": [{"prefix": "", "agents": [], "note": "Holdout: survivors talk through the game, not git"}]}, f)
        except OSError:
            pass


def register(board, api, mcp):
    from fastapi import Header, Request
    from fastapi.concurrency import run_in_threadpool
    from fastapi.responses import JSONResponse
    from mcp.server.mcpserver import Context
    from mcp.server.mcpserver.exceptions import ToolError

    sys.path.insert(0, os.environ.get("HOLDOUT_HUB", "/opt/holdout"))
    from holdout.engine import GameError
    from holdout.host import MAX_BODY_BYTES, GameHost

    def emit(kind, agent, **fields):
        try:
            board._emit(kind, agent, **fields)
        except Exception:  # noqa: BLE001 - logging only
            pass

    host = GameHost(seats=lambda: list(board.agents()), emit=emit)
    threading.Thread(target=_close_main, daemon=True, name="holdout-git").start()

    def bearer(value):
        return value[7:].strip() if value and value.lower().startswith("bearer ") else None

    @api.post("/game/{endpoint}")
    async def game(endpoint: str, request: Request, authorization: str = Header(None)):
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
            return await run_in_threadpool(host.call, seat, endpoint, body)
        except GameError as e:
            return JSONResponse({"ok": False, "error": str(e), **e.extra}, status_code=e.code)

    @api.post("/admin/game/freeze")
    def freeze(authorization: str = Header(None)):
        api.state.require_admin(authorization)
        return host.freeze()

    @api.get("/admin/game/score")
    def score(authorization: str = Header(None)):
        api.state.require_admin(authorization)
        with host.lock:
            return host.game.grade_state() if host.game else {"grade": 0.0, "started": False}

    @api.get("/admin/game/dead")
    def dead(authorization: str = Header(None)):
        """Seats whose survivor is dead right now. keepalive.py wakes an agent that ended its session while dead."""
        api.state.require_admin(authorization)
        with host.lock:
            return {"dead": host.game.dead_seats() if host.game else []}

    def tool(ctx, endpoint, body):
        try:
            seat = board.authenticate(bearer((ctx.headers or {}).get("authorization")))
            return host.call(seat, endpoint, body)
        except Exception as e:  # noqa: BLE001
            raise ToolError(str(e)) from None

    @mcp.tool()
    def holdout_status(ctx: Context) -> dict:
        """Your survivor (position, health, satiety, inventory, orders), the time and day, places you know, the team,
        and the stronghold's stockpile, builds and helpers when you are close enough to see them. Every reply also
        carries `heard`: what your survivor heard since your last call. See docs/API.md."""
        return tool(ctx, "status", {})

    @mcp.tool()
    def holdout_look(ctx: Context) -> dict:
        """What your survivor can see: a text map around you (legend included), zombies, teammates, strangers, helpers,
        items on the ground, places (with their details when you are close) and ripe berry bushes in sight. Costs 2
        rate tokens."""
        return tool(ctx, "look", {})

    @mcp.tool()
    def holdout_events(ctx: Context, after: int = 0, wait_seconds: float = 0, limit: int = 100) -> dict:
        """Your event log after seq `after`: orders done or failed, what you hear (say, shout, radio), bites, hordes,
        builds finished. wait_seconds (max 30) blocks until something new arrives."""
        return tool(ctx, "events", {"after": after, "wait_seconds": wait_seconds, "limit": limit})

    @mcp.tool()
    def holdout_act(ops: list, ctx: Context, replace: bool = False) -> dict:
        """Give your survivor orders, in order: [{"op": ..., ...}], at most 16 per call. Timed orders queue (up to 8)
        and run in game time; say/shout/radio/eat/start_build, admit/turn_away/quarantine/assign and a few others
        happen at once. replace=true clears
        your queue first. docs/API.md lists every op."""
        return tool(ctx, "act", {"ops": ops, "replace": replace})

    return host
