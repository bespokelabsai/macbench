"""Call the ATC API from Python. Standard library only.

    import sys, os; sys.path.insert(0, os.path.dirname(__file__))
    from atc import call, act
    print(call("status"))
    radar = call("query", {"of": "traffic"})["result"]["rows"]
    act([{"op": "altitude", "flight": "UAE202", "ft": 5000}])

It uses the harness's board URL and your board token (ATC_URL and ATC_TOKEN override them).
call() returns the reply as a dict. A rate-limited call raises RateLimited, which has retry_after_sec.
"""
import json
import os
import re
import time
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


if os.environ.get("ATC_URL"):
    URL = os.environ["ATC_URL"].rstrip("/")
    HEADERS = {"Authorization": "Bearer " + os.environ.get("ATC_TOKEN", ""), "Content-Type": "application/json"}
else:
    URL = os.environ.get("MULTI_AGENT_HARNESS_BOARD_URL", "").rstrip("/")
    HEADERS = {"Authorization": "Bearer " + os.environ.get("MULTI_AGENT_HARNESS_BOARD_TOKEN", ""),
               "Content-Type": "application/json", **_hub_headers()}


def call(endpoint, body=None, retry=True):
    data = json.dumps(body or {}).encode()
    while True:
        req = urllib.request.Request(f"{URL}/api/atc/{endpoint}", data, HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            reply = json.load(e)
            if e.code == 429:
                if not retry:
                    raise RateLimited(reply)
                time.sleep(reply.get("retry_after_sec", 1.0))
                continue
            return {"http_status": e.code, **reply}


def query(of, **kw):
    r = call("query", {"of": of, **kw})
    return r.get("result") if r.get("ok") else r


def act(ops):
    return call("act", {"ops": ops})


def events(after=0, **kw):
    r = call("events", {"after": after, **kw})
    return r.get("result") if r.get("ok") else r
