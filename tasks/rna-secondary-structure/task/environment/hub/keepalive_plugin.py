"""Board plugin for keepalive.py (load it with --ek board_plugins=<this file>). Two admin routes, which need the
board's admin token and are never routed to agents:

- GET  /api/admin/keepalive/unread: for each agent, how many messages are unread and the seq of the newest one,
  counting all of them and only those sent to it by name. Unread means what inbox() would return now: messages
  from others, to that agent or to everyone, after its inbox cursor. It changes nothing (cursors stay put).
- POST /api/admin/keepalive/event: writes one `keepalive` event (a wake, a session end) to the board log, so the
  wakes can be counted from board-events.jsonl afterwards. Agents can't read board events, only messages."""

from typing import Any

from fastapi import Body, Header


def register(board, api, mcp):
    @api.get("/admin/keepalive/unread")
    def unread(authorization: str | None = Header(None)):
        api.state.require_admin(authorization)
        out = {}
        for a in board.agents():
            msgs = board._inbox_now(a, 100000)
            direct = [m for m in msgs if m["to"] is not None]
            out[a] = {"n": len(msgs), "direct": len(direct), "latest": max((m["seq"] for m in msgs), default=0),
                      "latest_direct": max((m["seq"] for m in direct), default=0)}
        return {"unread": out}

    @api.post("/admin/keepalive/event")
    def event(body: dict[str, Any] = Body(...), authorization: str | None = Header(None)):
        api.state.require_admin(authorization)
        fields = {k: v for k, v in body.items() if k not in ("ts", "event", "agent")}
        board._emit("keepalive", body.get("agent"), **fields)
        return {"ok": True}
