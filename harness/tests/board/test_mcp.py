"""End-to-end: real uvicorn on 127.0.0.1, MCP streamable-HTTP client with a bearer header."""

import asyncio
import json
import socket

import httpx2
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from multi_agent_harness.board import Board
from multi_agent_harness.board.server import build_app


def _data(result):
    assert not result.is_error, result.content
    sc = result.structured_content
    if sc is not None:
        return sc["result"] if set(sc) == {"result"} else sc
    items = [json.loads(c.text) for c in result.content]
    return items[0] if len(items) == 1 else items


async def test_mcp_send_inbox_history_with_bearer_auth(tmp_path):
    board = Board(tmp_path / "b.sqlite", tmp_path / "events.jsonl")
    tok_a, tok_b = board.register_agent("alice"), board.register_agent("bob")
    board.register_agent("carol")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(build_app(board, admin_token="adm", plugins=[]), log_level="warning"))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            await asyncio.sleep(0.02)
        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {tok_a}"})
        async with Client(streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http)) as c:
            assert set(t.name for t in (await c.list_tools()).tools) == {"send", "inbox", "history"}
            seq = _data(await c.call_tool("send", {"text": "hi bob", "to": "bob"}))["seq"]
            assert [(m["from"], m["to"]) for m in await board.inbox("bob")] == [("alice", "bob")]

            waiter = asyncio.create_task(c.call_tool("inbox", {"wait_seconds": 10}))
            await asyncio.sleep(0.2)
            async with httpx2.AsyncClient() as rest:
                r = await rest.post(f"http://127.0.0.1:{port}/api/messages", json={"text": "yo"},
                                    headers={"Authorization": f"Bearer {tok_b}"})
                assert r.status_code == 200
            msgs = _data(await asyncio.wait_for(waiter, 5))
            assert [(m["from"], m["to"], m["text"]) for m in msgs] == [("bob", None, "yo")]
            hist = _data(await c.call_tool("history", {"after": seq - 1}))
            assert [m["text"] for m in hist] == ["hi bob", "yo"]
            assert (await c.call_tool("send", {"text": "x", "to": "zed"})).is_error
            _data(await c.call_tool("send", {"text": "both", "to": ["bob", "carol"]}))
            assert [m["to"] for m in await board.inbox("carol") if m["text"] == "both"] == [["bob", "carol"]]
        await http.aclose()
    finally:
        server.should_exit = True
        await serving
