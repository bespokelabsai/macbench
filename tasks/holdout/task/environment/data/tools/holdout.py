"""Call the Holdout game from your own programs. Standard library only.

    import sys; sys.path.insert(0, "/app/tools")
    from holdout import call
    print(call("status"))
    print(call("look")["grid"])
    call("act", {"ops": [{"op": "move_to", "x": 40, "y": 30}]})
    call("events", {"after": 0, "wait_seconds": 20})

From the shell:  python3 /app/tools/holdout.py status | look | events [AFTER] | act '<json list of ops>'

call() returns the reply as a dict. A rate-limited call raises RateLimited, which has retry_after_sec.
Programs are held to the same limits as you: one survivor, its body, and your seat's rate limit.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request


class RateLimited(Exception):
    def __init__(self, reply):
        super().__init__(reply.get("error"))
        self.retry_after_sec = reply.get("retry_after_sec", 1.0)


def _hub_headers():
    """The hub's access headers, as the team's git remote already sends them."""
    try:
        with open("/run/multi-agent-harness/gitconfig") as f:
            return dict(re.findall(r"extraHeader = ([^:\s]+): (.*)", f.read()))
    except OSError:
        return {}


URL = os.environ.get("MULTI_AGENT_HARNESS_BOARD_URL", os.environ.get("HOLDOUT_URL", "http://127.0.0.1:8790")).rstrip("/")
HEADERS = {"Authorization": "Bearer " + os.environ.get("MULTI_AGENT_HARNESS_BOARD_TOKEN", os.environ.get("HOLDOUT_TOKEN", "")),
           "Content-Type": "application/json", **_hub_headers()}


def call(endpoint, body=None):
    req = urllib.request.Request(f"{URL}/api/game/{endpoint}", json.dumps(body or {}).encode(), HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        reply = json.load(e)
        if e.code == 429:
            raise RateLimited(reply) from None
        return {"http_status": e.code, **reply}


if __name__ == "__main__":
    a = sys.argv[1:] or ["status"]
    if a[0] == "act":
        out = call("act", {"ops": json.loads(a[1]), "replace": "--replace" in a})
    elif a[0] == "events":
        out = call("events", {"after": int(a[1]) if len(a) > 1 else 0, "wait_seconds": float(a[2]) if len(a) > 2 else 0})
    else:
        out = call(a[0])
    if a[0] == "look" and "grid" in out:
        print("\n".join(out.pop("grid")))
    print(json.dumps(out, indent=1))
