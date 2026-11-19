"""Board plugin for keepalive.py: GET /api/admin/keepalive/unread gives, for each agent, how many messages are
unread and the seq of the newest one, counting all of them and only those sent to it by name.

Unread means what inbox() would return now: messages from others, sent to that agent or to everyone,
after its inbox cursor. The route needs the admin token, and it changes nothing (cursors stay put)."""

from fastapi import Header


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
