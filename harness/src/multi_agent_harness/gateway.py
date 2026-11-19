"""multi-agent-harness-gateway: the hub's one exposed port. Sandbox backends expose one port per URL (on Daytona,
one preview host per port), so the board, the model proxy and the git remote sit behind a single listener that routes
by path to the services on localhost:

    /mcp, /api     board   path kept
    /proxy/...     proxy   prefix stripped
    /git/...       git     prefix stripped
    /healthz       "ok"

Admin endpoints are refused here; TeamAgent calls them from the hub itself. Daytona's preview
token header is dropped before forwarding, so no service sees or logs it. Responses stream, which
covers MCP's SSE and streamed model output.

    multi-agent-harness-gateway --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import PlainTextResponse, StreamingResponse
from starlette.routing import Route

BOARD, PROXY, GIT = "http://127.0.0.1:8790", "http://127.0.0.1:8791", "http://127.0.0.1:8792"
PREVIEW_HEADER = "x-daytona-preview-token"
_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
        "transfer-encoding", "upgrade", "host", "content-length", PREVIEW_HEADER}
_ADMIN = re.compile(r"^(/api)?/admin(/|$)")
METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]


def route(path: str, board: str = BOARD, proxy: str = PROXY, git: str = GIT) -> str | None:
    """Upstream URL for a request path, or None to refuse it."""
    if ".." in path:
        return None
    for prefix, base, strip in (("/mcp", board, False), ("/api", board, False),
                                ("/proxy", proxy, True), ("/git", git, True)):
        if path == prefix or path.startswith(prefix + "/"):
            rest = (path[len(prefix):] if strip else path) or "/"
            return None if _ADMIN.match(rest) else base + rest
    return None


def build_app(board: str = BOARD, proxy: str = PROXY, git: str = GIT) -> Starlette:
    client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10.0))

    async def health(_: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    async def forward(request: Request):
        url = route(request.url.path, board, proxy, git)
        if url is None:
            return PlainTextResponse("not found", status_code=404)
        if request.url.query:
            url += "?" + request.url.query
        headers = [(k, v) for k, v in request.headers.items() if k.lower() not in _HOP]
        upstream = client.build_request(request.method, url, headers=headers, content=await request.body())
        try:
            resp = await client.send(upstream, stream=True)
        except httpx.HTTPError as e:
            return PlainTextResponse(f"upstream unavailable: {type(e).__name__}", status_code=502)
        out = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP}
        return StreamingResponse(resp.aiter_raw(), status_code=resp.status_code, headers=out,
                                 background=BackgroundTask(resp.aclose))

    @asynccontextmanager
    async def lifespan(_):
        yield
        await client.aclose()

    return Starlette(routes=[Route("/healthz", health), Route("/{path:path}", forward, methods=METHODS)],
                     lifespan=lifespan)


def main() -> None:
    import argparse

    import uvicorn

    p = argparse.ArgumentParser(prog="multi-agent-harness-gateway", description="The hub's single entry port")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    a = p.parse_args()
    uvicorn.run(build_app(), host=a.host, port=a.port)


if __name__ == "__main__":
    main()
