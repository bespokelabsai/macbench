"""Call the game API from your own programs. Standard library only.

    from game import call
    print(call("status"))
    print(call("query", {"of": "units"}))
    print(call("status", {"unit": "unit-2"}))   # one of your units; the default is your first

call() returns the reply as a dict. A rate-limited call raises GameRateLimit, which has retry_after_sec.
"""
import json
import os
import re
import urllib.error
import urllib.request


class GameRateLimit(Exception):
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


URL = os.environ.get("MULTI_AGENT_HARNESS_BOARD_URL", "").rstrip("/")
HEADERS = {"Authorization": "Bearer " + os.environ.get("MULTI_AGENT_HARNESS_BOARD_TOKEN", ""),
           "Content-Type": "application/json", **_hub_headers()}


def request(path, body=None):
    """POST with a body, GET without."""
    req = urllib.request.Request(URL + path, None if body is None else json.dumps(body).encode(), HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"http_status": e.code, **json.load(e)}


def call(endpoint, body=None):
    reply = request("/api/game/" + endpoint, body or {})
    if reply.get("http_status") == 429:
        raise GameRateLimit(reply)
    return reply
