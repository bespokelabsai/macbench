import asyncio

import httpx
import pytest

from multi_agent_harness.board import Board, BoardError, Policy
from multi_agent_harness.board.server import build_api


@pytest.mark.asyncio
async def test_notifications_are_admin_only_and_wake_lead():
    class NoSend(Policy):
        def authorize(self, board, agent, action, **details):
            if action == "send":
                raise BoardError("agent sends disabled")

    board = Board(policy=NoSend())
    token = board.register_agent("lead", "lead")
    board.register_agent("peer")
    events = []
    board.subscribe(events.append)
    app = build_api(board, "admin-secret")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        body = {"to": "lead", "text": "60 seconds remain. Keep going."}
        for headers in ({}, {"Authorization": f"Bearer {token}"}):
            assert (await client.post("/admin/notifications", json=body, headers=headers)).status_code == 401
        waiter = asyncio.create_task(board.inbox("lead", wait_seconds=10))
        await asyncio.sleep(0)
        response = await client.post("/admin/notifications", json=body,
                                     headers={"Authorization": "Bearer admin-secret"})
        assert response.status_code == 200
        messages = await asyncio.wait_for(waiter, 1)
        assert [(m["from"], m["text"]) for m in messages] == [("@harness", body["text"])]
        assert await board.inbox("peer") == []
        assert any(e["event"] == "notify" and e["agent"] == "@harness" for e in events)
        assert not any(e["event"] == "send" and e["agent"] == "@harness" for e in events)  # not an agent message
        assert (await client.post("/admin/notifications", json={**body, "to": "missing"},
                                 headers={"Authorization": "Bearer admin-secret"})).status_code == 400
