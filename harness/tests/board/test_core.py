import asyncio
from pathlib import Path

import pytest

from multi_agent_harness.board import Board, BoardError, Policy

HIERARCHY = Path(__file__).resolve().parents[2] / "examples" / "policies" / "hierarchy.py"


def make(*names, policy=None):
    board = Board(policy=policy)
    for n in names:
        board.register_agent(n)
    return board


def texts(msgs):
    return [m["text"] for m in msgs]


async def test_broadcast_reaches_everyone_dm_only_its_target():
    b = make("a", "b", "c")
    b.send("a", "all")
    b.send("a", "just b", to="b")
    assert texts(await b.inbox("b")) == ["all", "just b"]
    assert texts(await b.inbox("c")) == ["all"]
    m = b.history("b")[1]
    assert (m["from"], m["to"], m["text"]) == ("a", "b", "just b") and set(m) == {"seq", "from", "to", "text", "ts"}
    assert b.history("b")[0]["to"] is None


async def test_inbox_cursor_advances_and_skips_own_sends():
    b = make("a", "b")
    b.send("a", "mine")
    b.send("a", "mine too", to="b")
    b.send("b", "one")
    assert texts(await b.inbox("a")) == ["one"]
    assert await b.inbox("a") == []
    b.send("b", "two", to="a")
    assert texts(await b.inbox("a")) == ["two"]


async def test_inbox_wait_wakes_on_send_from_another_thread():
    b = make("a", "b")
    waiter = asyncio.create_task(b.inbox("a", wait_seconds=10))
    await asyncio.sleep(0.05)
    # REST handlers and sync MCP tools send from worker threads.
    await asyncio.to_thread(b.send, "b", "ping")
    assert texts(await asyncio.wait_for(waiter, 2)) == ["ping"]


def test_history_shows_only_what_you_can_see():
    b = make("a", "b", "c")
    s1 = b.send("a", "all")["seq"]
    b.send("a", "to b", to="b")
    b.send("c", "c to a", to="a")
    b.send("b", "b to a", to="a")
    assert texts(b.history("c")) == ["all", "c to a"]
    assert texts(b.history("b")) == ["all", "to b", "b to a"]
    assert texts(b.history("a", after=s1, limit=2)) == ["to b", "c to a"]


def test_unknown_or_self_to_is_an_error():
    b = make("a", "b")
    with pytest.raises(BoardError, match="no other agent named 'zed'"):
        b.send("a", "hi", to="zed")
    with pytest.raises(BoardError):
        b.send("a", "hi", to="a")
    assert b.history("b") == []


async def test_send_to_a_list_reaches_only_those_named():
    b = make("a", "b", "c", "d")
    b.send("a", "b and c", to=["b", "c", "b"])
    b.send("a", "one-name list", to=["d"])
    for who, seen in (("b", ["b and c"]), ("c", ["b and c"]), ("d", ["one-name list"])):
        assert texts(await b.inbox(who)) == seen
    assert [m["to"] for m in b.history("a")] == [["b", "c"], "d"]
    assert texts(b.history("d")) == ["one-name list"]
    for bad in (["b", "zed"], ["a"], []):
        with pytest.raises(BoardError):
            b.send("a", "x", to=bad)


def test_policy_gets_the_recipients():
    seen = []

    class Spy(Policy):
        def authorize(self, board, agent, action, **details):
            if action == "send":
                seen.append((details["to"], details["recipients"]))

    b = make("a", "b", "c", policy=Spy())
    b.register_agent("lead", role="lead")
    b.send("a", "x", to=["b", "c"])
    b.send("a", "y", to="b")
    b.send("a", "z")
    assert seen == [(["b", "c"], ["b", "c"]), ("b", ["b"]), (None, ["b", "c", "lead"])]
    assert b.role("lead") == "lead" and b.role("a") is None


def test_hierarchy_policy_refusals_are_logged(tmp_path):
    import json

    from multi_agent_harness.board.server import load_policy

    log = tmp_path / "events.jsonl"
    b = Board(log_path=log, policy=load_policy(str(HIERARCHY)))
    for n in ("lead", "w1", "w2"):
        b.register_agent(n)
    b.send("lead", "do x", to="w1")
    b.send("lead", "everyone do y")
    b.send("w1", "done", to="lead")
    for to in ("w2", None, ["lead", "w2"]):
        with pytest.raises(BoardError):
            b.send("w1", "psst", to=to)
    refused = [e for e in map(json.loads, log.read_text().splitlines()) if e["event"] == "refused"]
    assert [(e["agent"], e["action"], e["to"]) for e in refused] == [("w1", "send", "w2"), ("w1", "send", None),
                                                                    ("w1", "send", ["lead", "w2"])]


def test_token_bucket_limits_and_refills():
    from multi_agent_harness.board.ratelimit import RateLimiter

    now = [0.0]
    rl = RateLimiter(2, 3, clock=lambda: now[0])
    assert [rl.take("a") for _ in range(3)] == [0, 0, 0]
    assert rl.take("a") == pytest.approx(0.5)
    assert rl.take("b") == 0  # buckets are per key
    now[0] += 0.5
    assert rl.take("a") == 0 and rl.take("a") > 0
    now[0] += 100
    assert [rl.take("a") for _ in range(3)] == [0, 0, 0] and rl.take("a") > 0  # refill caps at burst
    assert RateLimiter(0, 3).take("a") == 0 and not RateLimiter(5, 0).enabled


async def test_rate_limit_refuses_and_logs(tmp_path):
    import json

    from multi_agent_harness.board import RateLimited

    class Tight(Policy):
        rate_per_sec, rate_burst = 0.001, 3

    log = tmp_path / "events.jsonl"
    b = Board(log_path=log, policy=Tight())
    b.register_agent("a")
    b.register_agent("b")
    b.send("a", "1")
    await b.inbox("a", wait_seconds=0.05)  # a long-poll is one op
    b.history("a")
    for _ in range(3):
        with pytest.raises(RateLimited, match=r"inbox\(wait_seconds"):
            b.send("a", "flood")
    b.send("b", "still fine")
    limited = [e for e in map(json.loads, log.read_text().splitlines()) if e["event"] == "rate_limited"]
    # refusals inside one second share a log line; the rest are carried to the next one
    assert [(e["agent"], e["action"], e["count"]) for e in limited] == [("a", "send", 1)]
    assert b._rl_pending["a"] == 2


PLUGIN = """
def register(board, api, mcp):
    @mcp.tool()
    def ping() -> str:
        return "pong"
"""


def test_extensions_load_by_path_log_config_and_refusals(tmp_path, monkeypatch):
    import json

    from multi_agent_harness.board.server import build_app

    (tmp_path / "plug.py").write_text(PLUGIN)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOARD_POLICY", str(HIERARCHY))
    monkeypatch.setenv("BOARD_PLUGINS", "plug.py")  # relative to the working directory
    monkeypatch.setenv("BOARD_LOG", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("BOARD_DB", str(tmp_path / "b.sqlite"))
    board = build_app(admin_token="adm").state.board
    assert type(board.policy).__name__ == "HierarchyPolicy"
    board.register_agent("w1")
    board.register_agent("w2")
    with pytest.raises(BoardError):
        board.send("w1", "psst", to="w2")
    events = [json.loads(l) for l in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert events[0]["event"] == "config"
    assert (events[0]["policy"], events[0]["policy_class"], events[0]["plugins"]) == \
        ("hierarchy.py", "HierarchyPolicy", ["plug.py"])
    assert events[0]["rate_per_sec"] == 5.0
    refused = [e for e in events if e["event"] == "refused"]
    assert [(e["agent"], e["action"], e["to"]) for e in refused] == [("w1", "send", "w2")]


def test_extension_errors(tmp_path):
    from multi_agent_harness.board.server import ExtensionError, load_plugins, load_policy

    assert type(load_policy(None)) is Policy  # unset keeps allow-all
    pol = "from multi_agent_harness.board import Policy\nclass {}(Policy):\n    pass\n"
    (tmp_path / "none.py").write_text("from multi_agent_harness.board import Policy\nX = 1\n")
    (tmp_path / "two.py").write_text(pol.format("A") + pol.format("B").split("\n", 1)[1])
    (tmp_path / "broken.py").write_text("import no_such_module\n")
    (tmp_path / "noreg.py").write_text("X = 1\n")
    cases = {"missing.py": "board policy .*: no such file",
             "hierarchy:HierarchyPolicy": "not a path to a .py file",
             "none.py": "exactly one subclass of multi_agent_harness.board.Policy, found 0: none",
             "two.py": "exactly one subclass of multi_agent_harness.board.Policy, found 2: A, B",
             "broken.py": "failed to load: ModuleNotFoundError"}
    for name, match in cases.items():
        with pytest.raises(ExtensionError, match=match):
            load_policy(str(tmp_path / name) if name.endswith(".py") else name)
    with pytest.raises(ExtensionError, match="board plugin .*noreg.py has no register"):
        load_plugins([str(tmp_path / "noreg.py")])
    with pytest.raises(ExtensionError, match="board plugin .*: no such file"):
        load_plugins([str(tmp_path / "missing.py")])
