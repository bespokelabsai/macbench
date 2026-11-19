"""HTTP server: MCP tools for agents at /mcp, a REST mirror at /api for the harness and CLI.

Config is all environment so a Harbor environment can start it with one command:
  BOARD_ADMIN_TOKEN  required; lets the harness register agents
  BOARD_DB           SQLite path (default board.sqlite)
  BOARD_LOG          JSONL event log path (default board-events.jsonl)
  BOARD_POLICY       optional path to a .py file defining one Policy subclass (default allows everything)
  BOARD_PLUGINS      optional comma-separated paths to .py files exposing register(board, api, mcp)
                     (harness package modules can load later through POST /api/admin/plugins, until an
                     agent registers)
  BOARD_ALLOWED_HOSTS optional comma-separated Host values when not bound to localhost
  BOARD_RATE_PER_SEC, BOARD_RATE_BURST  per-agent token bucket (default 5/sec, burst 30; 0 = off)

At startup the board logs a `config` event naming the policy and plugin files in force.
`multi-agent-harness-board --check` only loads BOARD_POLICY and BOARD_PLUGINS, and exits non-zero if either fails.
"""

from __future__ import annotations

import hmac
import importlib
import math
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel
from starlette.applications import Starlette

from .. import pyfile
from .core import Board, BoardError, Policy, RateLimited

INSTRUCTIONS = (
    "Message board shared with your teammates. send(text) reaches everyone, send(text, to=name) "
    "one teammate, send(text, to=[name, ...]) several. inbox(wait_seconds=60) waits for new messages instead of polling in a loop, "
    "and history() re-reads everything you can see."
)


class ExtensionError(RuntimeError):
    """BOARD_POLICY or BOARD_PLUGINS names something that can't be loaded."""


def load_policy(path: str | None) -> Policy:
    """An instance of the one Policy subclass the .py file at `path` defines, or the allow-all
    default when path is empty."""
    if not path:
        return Policy()
    try:
        file = pyfile.locate(path, "board policy")
        module = pyfile.import_file(file, "board policy")
        cls = pyfile.one_subclass(module, Policy, "multi_agent_harness.board.Policy", file, "board policy")
    except ValueError as e:
        raise ExtensionError(str(e)) from None
    cls.source_file = file.name
    return cls()


def _check_plugin(mod: Any, name: str) -> Any:
    if not callable(getattr(mod, "register", None)):
        raise ExtensionError(f"board plugin {name} has no register(board, api, mcp)")
    return mod


def load_plugins(paths: list[str]) -> list[Any]:
    """Load each plugin .py file and check it has register(board, api, mcp)."""
    mods = []
    for path in paths:
        try:
            file = pyfile.locate(path, "board plugin")
            mod = pyfile.import_file(file, "board plugin")
        except ValueError as e:
            raise ExtensionError(str(e)) from None
        mods.append(_check_plugin(mod, str(file)))
    return mods


def load_plugin_modules(names: list[str]) -> list[Any]:
    """Import plugin modules from the harness package by name: the ones a strategy brings."""
    mods = []
    for name in names:
        try:
            mod = importlib.import_module(name)
        except Exception as e:  # noqa: BLE001 - any import failure is a config error
            raise ExtensionError(f"board plugin {name!r} failed to import: {type(e).__name__}: {e}") from e
        mods.append(_check_plugin(mod, repr(name)))
    return mods


def _env_plugins() -> list[str]:
    return [p.strip() for p in os.environ.get("BOARD_PLUGINS", "").split(",") if p.strip()]


def _bearer(value: str | None) -> str | None:
    if value and value.lower().startswith("bearer "):
        return value[7:].strip()
    return None


def build_mcp(board: Board) -> MCPServer:
    mcp = MCPServer("board", instructions=INSTRUCTIONS)

    def me(ctx: Context) -> str:
        headers = ctx.headers or {}
        return board.authenticate(_bearer(headers.get("authorization")))

    @mcp.tool()
    def send(text: str, ctx: Context, to: str | list[str] | None = None) -> dict:
        """Send a message. to=None (the default) reaches every teammate; otherwise it goes to the
        one agent named, or to each agent in a list of names. Returns the new seq. At most 8000 chars."""
        return board.send(me(ctx), text, to=to)

    @mcp.tool()
    async def inbox(ctx: Context, wait_seconds: float = 0, limit: int = 50) -> list[dict]:
        """Messages to you or to everyone, from others, since your last inbox call.
        Set wait_seconds (max 60) to block until something arrives."""
        return await board.inbox(me(ctx), wait_seconds=wait_seconds, limit=limit)

    @mcp.tool()
    def history(ctx: Context, after: int = 0, limit: int = 50) -> list[dict]:
        """Everything you can see (all broadcasts, your sends, messages to you), oldest first,
        after seq `after`. limit is capped at 200."""
        return board.history(me(ctx), after=after, limit=limit)

    return mcp


class RegisterBody(BaseModel):
    name: str
    role: str | None = None


class PluginsBody(BaseModel):
    modules: list[str]


class NotificationBody(BaseModel):
    text: str
    to: str


class SendBody(BaseModel):
    text: str
    to: str | list[str] | None = None


def build_api(board: Board, admin_token: str) -> FastAPI:
    api = FastAPI(title="board")

    @api.exception_handler(BoardError)
    async def _board_error(_, exc: BoardError):
        if isinstance(exc, RateLimited):
            return JSONResponse({"error": str(exc)}, status_code=429,
                                headers={"retry-after": str(max(1, math.ceil(exc.retry_after)))})
        return JSONResponse({"error": str(exc)}, status_code=400)

    def me(authorization: str | None) -> str:
        try:
            return board.authenticate(_bearer(authorization))
        except BoardError as e:
            raise HTTPException(401, str(e)) from None

    def require_admin(authorization: str | None) -> None:
        if not hmac.compare_digest(_bearer(authorization) or "", admin_token):
            raise HTTPException(401, "admin token required")

    # Plugins guard their own admin routes with this (the gateway never routes /api/admin).
    api.state.require_admin = require_admin

    @api.post("/admin/agents")
    def register(body: RegisterBody, authorization: str | None = Header(None)):
        require_admin(authorization)
        return {"agent": body.name, "token": board.register_agent(body.name, body.role)}

    @api.post("/admin/notifications")
    def notify(body: NotificationBody, authorization: str | None = Header(None)):
        require_admin(authorization)
        return board.notify(body.text, body.to)

    @api.post("/messages")
    def send(body: SendBody, authorization: str | None = Header(None)):
        return board.send(me(authorization), body.text, to=body.to)

    @api.get("/inbox")
    async def inbox(wait_seconds: float = 0, limit: int = 50, authorization: str | None = Header(None)):
        return await board.inbox(me(authorization), wait_seconds=wait_seconds, limit=limit)

    @api.get("/messages")
    def history(after: int = 0, limit: int = 50, authorization: str | None = Header(None)):
        return board.history(me(authorization), after=after, limit=limit)

    return api


def build_app(board: Board | None = None, admin_token: str | None = None, host: str = "127.0.0.1",
              allowed_hosts: list[str] | None = None, plugins: list[str] | None = None) -> Starlette:
    admin_token = admin_token or os.environ.get("BOARD_ADMIN_TOKEN")
    if not admin_token:
        raise SystemExit("BOARD_ADMIN_TOKEN is required")
    if board is None:
        policy = load_policy(os.environ.get("BOARD_POLICY"))
        for attr, var in (("rate_per_sec", "BOARD_RATE_PER_SEC"), ("rate_burst", "BOARD_RATE_BURST")):
            if os.environ.get(var):
                setattr(policy, attr, float(os.environ[var]))
        board = Board(os.environ.get("BOARD_DB", "board.sqlite"),
                      os.environ.get("BOARD_LOG", "board-events.jsonl"), policy)
    mcp = build_mcp(board)
    api = build_api(board, admin_token)
    plugins = [p.strip() for p in plugins] if plugins is not None else _env_plugins()
    for mod in load_plugins(plugins):
        mod.register(board, api, mcp)
    plugin_files = [Path(p).name for p in plugins]
    loaded = list(plugin_files)

    @api.post("/admin/plugins")
    def add_plugins(body: PluginsBody, authorization: str | None = Header(None)):
        """Load more plugins into the running board, for a harness whose strategy brings its own
        tools. Only before any agent registers, so every agent connects to the full tool set."""
        api.state.require_admin(authorization)
        new = [m.strip() for m in body.modules if m.strip() and m.strip() not in loaded]
        if new and board.agents():
            raise HTTPException(409, "plugins load only before any agent registers")
        try:
            mods = load_plugin_modules(new)
        except ExtensionError as e:
            raise HTTPException(400, str(e)) from None
        for path, mod in zip(new, mods):
            mod.register(board, api, mcp)
            loaded.append(path)
        if new:
            board._emit("plugins", None, plugins=new)
        return {"plugins": loaded}

    # First line of a run's record: what the board was configured with: the policy and plugin
    # file names (a policy built in code shows as module:Class) and the limits.
    cls = type(board.policy)
    policy = ("default" if cls is Policy else getattr(cls, "source_file", None)
              or f"{cls.__module__}:{cls.__qualname__}")
    board._emit("config", None, policy=policy, policy_class=cls.__qualname__, plugins=plugin_files,
                rate_per_sec=board.limiter.rate, rate_burst=board.limiter.burst,
                max_text=board.policy.max_text)
    security = None
    if allowed_hosts:
        security = TransportSecuritySettings(allowed_hosts=allowed_hosts)
    app = mcp.streamable_http_app(host=host, transport_security=security)
    app.mount("/api", api)
    app.state.board = board
    return app


def main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Run the message board")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--check", action="store_true",
                        help="only load BOARD_POLICY and BOARD_PLUGINS, then exit")
    args = parser.parse_args()
    try:
        if args.check:
            load_policy(os.environ.get("BOARD_POLICY"))
            load_plugins(_env_plugins())
            print("board extensions ok")
            return
        hosts = [h for h in os.environ.get("BOARD_ALLOWED_HOSTS", "").split(",") if h]
        app = build_app(host=args.host, allowed_hosts=hosts or None)
    except ExtensionError as e:
        raise SystemExit(str(e)) from None
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
