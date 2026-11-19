"""Command-line client over the REST API, for agents without MCP and for the harness.

Config: BOARD_URL (default http://127.0.0.1:8790), BOARD_TOKEN, and BOARD_ADMIN_TOKEN for register.
Every command prints JSON on stdout. Errors print {"error": ...} and exit 1.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import httpx


def _call(method: str, path: str, token_env: str = "BOARD_TOKEN", wait: float = 0, **kwargs: Any) -> Any:
    url = os.environ.get("BOARD_URL", "http://127.0.0.1:8790").rstrip("/") + "/api" + path
    token = os.environ.get(token_env)
    if not token:
        raise SystemExit(_fail(f"{token_env} is not set"))
    r = httpx.request(method, url, headers={"Authorization": f"Bearer {token}"},
                      timeout=30 + wait, **kwargs)
    if r.status_code >= 400:
        try:
            body = r.json()
            msg = body.get("error") or body.get("detail") or body
        except ValueError:
            msg = r.text
        raise SystemExit(_fail(msg))
    return r.json()


def _fail(msg: Any) -> int:
    print(json.dumps({"error": msg}))
    return 1


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="multi-agent-harness-board-cli", description="Message board client")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("register", help="create an agent (needs BOARD_ADMIN_TOKEN)").add_argument("name")
    send = sub.add_parser("send", help="send to everyone, or to the agents named with --to")
    send.add_argument("--to", action="append", help="a recipient; repeat it or use a,b for several")
    send.add_argument("text")
    inbox = sub.add_parser("inbox", help="new messages since your last inbox call")
    inbox.add_argument("--wait", type=float, default=0, help="seconds to block for new messages (max 60)")
    inbox.add_argument("--limit", type=int, default=50)
    history = sub.add_parser("history", help="everything you can see, oldest first")
    history.add_argument("--after", type=int, default=0)
    history.add_argument("--limit", type=int, default=50)
    a = p.parse_args(argv)

    if a.cmd == "register":
        out = _call("POST", "/admin/agents", "BOARD_ADMIN_TOKEN", json={"name": a.name})
    elif a.cmd == "send":
        to = [n for t in a.to for n in t.split(",") if n] if a.to else None
        out = _call("POST", "/messages", json={"text": a.text, "to": to[0] if to and len(to) == 1 else to})
    elif a.cmd == "history":
        out = _call("GET", "/messages", params={"after": a.after, "limit": a.limit})
    else:
        out = _call("GET", "/inbox", wait=a.wait, params={"wait_seconds": a.wait, "limit": a.limit})
    json.dump(out, sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
