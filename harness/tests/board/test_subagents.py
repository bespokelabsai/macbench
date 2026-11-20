"""The async-subagents board plugin: loaded at runtime, lead-only tools, pool, delivery and wake."""

import asyncio
import json
import socket

import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from multi_agent_harness.board import Board
from multi_agent_harness.board.core import BoardError
from multi_agent_harness.board.server import build_app
from multi_agent_harness.board.subagents import Pool

ADMIN = {"Authorization": "Bearer adm"}


async def test_plugin_loads_at_runtime_and_lists_lead_tools_to_the_lead_only(tmp_path):
    board = Board(tmp_path / "b.sqlite", tmp_path / "events.jsonl")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(build_app(board, admin_token="adm", plugins=[]), log_level="warning"))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    base = f"http://127.0.0.1:{port}"
    try:
        while not server.started:
            await asyncio.sleep(0.02)
        async with httpx2.AsyncClient() as rest:
            body = {"modules": ["multi_agent_harness.board.subagents"]}
            assert (await rest.post(f"{base}/api/admin/plugins", json=body)).status_code == 401
            r = await rest.post(f"{base}/api/admin/plugins", json=body, headers=ADMIN)
            assert r.status_code == 200 and r.json()["plugins"] == ["multi_agent_harness.board.subagents"]
            lead, peer = board.register_agent("agent-1", "lead"), board.register_agent("peer", "peer")
            r = await rest.post(f"{base}/api/admin/plugins", json={"modules": ["json"]}, headers=ADMIN)
            assert r.status_code == 409  # too late once an agent exists
            r = await rest.post(f"{base}/api/admin/subagents/pool",
                                json={"lead": "agent-1", "slots": ["agent-2", "agent-3"],
                                      "resources": {"agent-2": {"gpus": 0}, "agent-3": {"gpus": 1}}},
                                headers=ADMIN)
            assert r.status_code == 200  # a route the plugin added after the server started

        names = {}
        for who, tok in (("lead", lead), ("peer", peer)):
            http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {tok}"})
            async with Client(streamable_http_client(f"{base}/mcp", http_client=http)) as c:
                names[who] = {t.name for t in (await c.list_tools()).tools}
                if who == "lead":
                    async with httpx2.AsyncClient() as rest:
                        pool_url = f"{base}/api/admin/subagents/pool"
                        for resources in ({}, {"agent-3": {"gpus": 1}}):
                            await rest.post(pool_url, headers=ADMIN, json={"lead": "agent-1",
                                            "slots": ["agent-2", "agent-3"], "resources": resources})
                            tool = next(t for t in (await c.list_tools()).tools if t.name == "spawn_agent")
                            assert ("gpu=True" in tool.description) == bool(resources)
                res = await c.call_tool("spawn_agent", {"instructions": "count to 3"})
                if who == "lead":
                    assert not res.is_error and json.loads(res.content[0].text)["status"] == "working"
                    assert json.loads(res.content[0].text)["slot"] == "agent-2"
                    res = await c.call_tool("spawn_agent", {"instructions": "GPU task", "gpu": True})
                    assert not res.is_error and json.loads(res.content[0].text)["slot"] == "agent-3"
                    status = await c.call_tool("agent_status", {"detail": True})
                    assert json.loads(status.content[0].text)["capacity"]["free_gpu_slots"] == 0
                else:
                    assert res.is_error and "only the lead" in res.content[0].text
            await http.aclose()
        assert names["lead"] == {"send", "inbox", "history", "spawn_agent", "terminate_agent", "agent_status"}
        assert names["peer"] == {"send", "inbox", "history"}
        async with httpx2.AsyncClient() as rest:
            url = f"{base}/api/admin/subagents/lost"
            assert (await rest.post(url, json={"slot": "agent-3"})).status_code == 401
            lost = await rest.post(url, json={"slot": "agent-3"}, headers=ADMIN)
            assert lost.status_code == 200 and lost.json()["terminated"] == ["agent-3"]
        events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
        assert {"event": "plugins", "plugins": ["multi_agent_harness.board.subagents"]}.items() <= events[1].items()
    finally:
        server.should_exit = True
        await serving


def _pool(tmp_path, slots=("agent-2",)):
    board = Board(tmp_path / "b.sqlite")
    board.policy.rate_per_sec = 0
    board.limiter.rate = 0
    board.register_agent("agent-1", "lead")
    pool = Pool(board)
    board.subscribe(pool.on_event)
    pool.configure("agent-1", list(slots))
    return board, pool


async def test_spawn_is_immediate_and_queues_when_the_pool_is_full(tmp_path):
    board, pool = _pool(tmp_path)
    assert pool.spawn("agent-1", "a") == {"name": "agent-2", "status": "working"}
    assert pool.spawn("agent-1", "b") == {"name": "agent-3", "status": "queued"}
    assert set(board.agents()) == {"agent-1", "agent-2", "agent-3"}  # messageable right away
    acts = pool.take_actions()["actions"]
    assert [(a["op"], a["name"], a["slot"]) for a in acts] == [("start", "agent-2", "agent-2")]
    assert board.authenticate(acts[0]["token"]) == "agent-2"
    with pytest.raises(BoardError, match="only the lead"):
        pool.spawn("agent-2", "c")

    pool.terminate("agent-1", "agent-2")
    acts = pool.take_actions()["actions"]
    assert [(a["op"], a["name"], a["slot"]) for a in acts] == [("stop", "agent-2", "agent-2"),
                                                               ("start", "agent-3", "agent-2")]
    assert pool.status("agent-1") == {"agent-2": "terminated", "agent-3": "working"}


async def test_final_response_reaches_the_lead_and_a_lead_message_wakes(tmp_path):
    board, pool = _pool(tmp_path)
    pool.spawn("agent-1", "a")
    pool.take_actions()
    assert pool.finished("agent-2", "all done", 0)["delivered"]
    [msg] = await board.inbox("agent-1")
    assert msg["from"] == "agent-2" and msg["to"] == "agent-1" and msg["text"].endswith("\nall done")
    assert pool.status("agent-1") == {"agent-2": "idle"}

    board.send("agent-1", "everyone: hello")  # a broadcast does not wake anyone
    assert pool.take_actions()["actions"] == []
    board.send("agent-1", "now do b", to="agent-2")
    assert [(a["op"], a["message"]) for a in pool.take_actions()["actions"]] == [("wake", "now do b")]
    assert pool.status("agent-1") == {"agent-2": "working"}
    # the wake prompt carried "now do b", so its inbox has only what it didn't get
    assert [m["text"] for m in await board.inbox("agent-2")] == ["everyone: hello"]

    board.send("agent-1", "and c", to="agent-2")  # sent while it works, never read: wakes it on finish
    pool.finished("agent-2", "b done", 0)
    assert [(a["op"], a["message"]) for a in pool.take_actions()["actions"]] == [("wake", "and c")]
    pool.finished("agent-2", "c done", 0)  # nothing new from the lead: stays idle
    assert pool.take_actions()["actions"] == [] and pool.status("agent-1") == {"agent-2": "idle"}


def test_async_strategy_plugin_loads_as_a_package_module():
    from multi_agent_harness.board.server import load_plugin_modules
    from multi_agent_harness.harbor.orchestration import resolve

    names = list(resolve("async").board_plugins)
    assert names == ["multi_agent_harness.board.subagents"]
    assert [m.__name__ for m in load_plugin_modules(names)] == names


def test_gpu_scheduler_cpu_preference_and_no_head_of_line_blocking(tmp_path):
    _, pool = _pool(tmp_path, ("gpu", "cpu"))
    pool.configure("agent-1", ["gpu", "cpu"], {"gpu": {"gpus": 1}, "cpu": {"gpus": 0}})
    cpu = pool.spawn("agent-1", "CPU work")
    waiting_cpu = pool.spawn("agent-1", "CPU waiting")
    pool.finished(cpu["name"], "done")  # idle CPU slot must not permit GPU spillover
    assert pool.subs[waiting_cpu["name"]]["status"] == "queued"
    gpu = pool.spawn("agent-1", "GPU work", True)
    waiting_gpu = pool.spawn("agent-1", "GPU waiting", True)
    assert cpu["slot"] == "cpu" and gpu["slot"] == "gpu"
    assert waiting_gpu["resources"] is None and waiting_gpu["gpu_required"]
    pool.terminate("agent-1", cpu["name"])
    assert pool.subs[waiting_gpu["name"]]["status"] == "queued"
    assert pool.subs[waiting_cpu["name"]]["slot"] == "cpu"
    pool.finished(gpu["name"], "done")
    assert pool.subs[waiting_gpu["name"]]["status"] == "queued"  # idle retains GPU
    pool.terminate("agent-1", gpu["name"])
    assert pool.subs[waiting_gpu["name"]]["slot"] == "gpu"
    pool.lose("cpu")
    with pytest.raises(BoardError, match="no usable CPU"):
        pool.spawn("agent-1", "CPU after loss")


def test_all_gpu_pool_accepts_ordinary_work_but_prioritizes_gpu_queue(tmp_path):
    _, pool = _pool(tmp_path, ("gpu",))
    pool.configure("agent-1", ["gpu"], {"gpu": {"gpus": 1}})
    first = pool.spawn("agent-1", "ordinary work")
    ordinary = pool.spawn("agent-1", "next ordinary")
    required = pool.spawn("agent-1", "requires GPU", True)
    assert first["slot"] == "gpu" and not first["gpu_required"]
    pool.terminate("agent-1", first["name"])
    assert pool.subs[required["name"]]["slot"] == "gpu"
    pool.terminate("agent-1", required["name"])
    assert pool.subs[ordinary["name"]]["slot"] == "gpu"
    pool.lose("gpu")
    with pytest.raises(BoardError, match="no usable"):
        pool.spawn("agent-1", "no capacity")


def test_gpu_request_without_capacity_does_not_register_an_agent(tmp_path):
    board, pool = _pool(tmp_path)
    before = board.agents()
    with pytest.raises(BoardError, match="no usable GPU"):
        pool.spawn("agent-1", "GPU", True)
    assert board.agents() == before and not pool.subs


async def test_lost_gpu_terminates_unschedulable_queue_and_notifies_once(tmp_path):
    board, pool = _pool(tmp_path, ("gpu", "cpu"))
    pool.configure("agent-1", ["gpu", "cpu"], {"gpu": {"gpus": 1}, "cpu": {"gpus": 0}})
    active = pool.spawn("agent-1", "GPU", True)
    queued = pool.spawn("agent-1", "GPU queue", True)
    assert pool.lose("gpu")["terminated"] == [active["name"], queued["name"]]
    assert pool.status("agent-1", True)["capacity"]["gpu_slots"] == 0
    assert all(a["slot"] != "gpu" for a in pool.take_actions()["actions"])
    with pytest.raises(BoardError, match="no usable GPU"):
        pool.spawn("agent-1", "more GPU", True)
    assert pool.spawn("agent-1", "CPU")["slot"] == "cpu"
    pool.lose("gpu")
    messages = await board.inbox("agent-1")
    assert len(messages) == 1 and "resources are unavailable" in messages[0]["text"]


async def test_loss_notice_failure_does_not_block_cpu_work(tmp_path, monkeypatch):
    board, pool = _pool(tmp_path, ("gpu", "cpu"))
    pool.configure("agent-1", ["gpu", "cpu"], {"gpu": {"gpus": 1}, "cpu": {"gpus": 0}})
    pool.spawn("agent-1", "GPU", True)
    send = board.send
    def refused(*args, **kwargs):
        raise BoardError("temporary refusal")
    monkeypatch.setattr(board, "send", refused)
    pool.lose("gpu")
    cpu = pool.spawn("agent-1", "CPU")
    assert [a["name"] for a in pool.take_actions()["actions"]] == [cpu["name"]]
    monkeypatch.setattr(board, "send", send)
    assert not pool.take_actions()["actions"]
    assert len(await board.inbox("agent-1")) == 1


def test_concurrent_completion_and_board_reads(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    board, pool = _pool(tmp_path)
    worker = pool.spawn("agent-1", "work")["name"]
    token = board.register_agent("observer")
    def read():
        for _ in range(300):
            assert board.authenticate(token) == "observer"
            assert board.role(worker) == "subagent"
            assert worker in board.agents()
            board.history("agent-1")
            board._inbox_now(worker, 50)
    def complete():
        for _ in range(100):
            pool.finished(worker, "done", 0)
            board.send("agent-1", "wake", to=worker)
            pool.take_actions()
    with ThreadPoolExecutor(4) as threads:
        futures = [threads.submit(read) for _ in range(3)] + [threads.submit(complete)]
        for future in futures:
            future.result(timeout=15)
