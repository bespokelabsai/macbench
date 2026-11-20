import httpx

from multi_agent_harness import gateway
from multi_agent_harness.gateway import BOARD, GIT, PROXY, route


def test_routes_by_prefix_and_refuses_admin():
    assert route("/mcp") == f"{BOARD}/mcp"
    assert route("/api/inbox") == f"{BOARD}/api/inbox"
    assert route("/proxy/openai/v1/responses") == f"{PROXY}/openai/v1/responses"
    assert route("/git/team.git/info/refs") == f"{GIT}/team.git/info/refs"
    for refused in ("/api/admin/agents", "/proxy/admin/usage", "/git/../api/admin/agents",
                    "/mcpx", "/", "/other"):
        assert route(refused) is None, refused


async def test_forward_drops_the_preview_token(monkeypatch):
    seen = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)

        async def body():  # a streamed body, like a real upstream
            yield b"up"
        return httpx.Response(200, content=body())

    real = httpx.AsyncClient
    monkeypatch.setattr(gateway.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(upstream), **kw))
    app = gateway.build_app()
    async with real(transport=httpx.ASGITransport(app=app), base_url="https://hub") as c:
        r = await c.post("/proxy/openai/v1/responses?x=1", content=b"{}",
                         headers={"Authorization": "Bearer k", "X-Daytona-Preview-Token": "secret"})
        assert r.status_code == 200 and r.text == "up"
        assert (await c.get("/healthz")).text == "ok"
    [req] = seen
    assert str(req.url) == f"{PROXY}/openai/v1/responses?x=1"
    assert req.headers["authorization"] == "Bearer k" and "x-daytona-preview-token" not in req.headers
