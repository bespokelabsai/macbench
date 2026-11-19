import json

import httpx
import pytest

from multi_agent_harness.proxy import Proxy, build_app

ADMIN = "admin-secret"
SSE = [
    b'event: message_start\ndata: {"type":"message_start","message":{"model":"claude-opus-5-5","usage":'
    b'{"input_tokens":1000,"cache_read_input_tokens":2000,"cache_creation_input_tokens":0,"output_tokens":1}}}\n\n',
    b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n',
    b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},',
    b'"usage":{"output_tokens":500}}\n\nevent: message_stop\ndata: {"type":"message_stop"}\n\n',
]


@pytest.fixture
def env(tmp_path):
    seen = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        if request.url.path == "/v1/messages" and body.get("stream"):
            async def chunks():
                for c in SSE:
                    yield c
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=chunks())
        if request.url.path == "/v1/messages":
            return httpx.Response(200, json={"type": "message", "usage": {
                "input_tokens": 1_000_000, "output_tokens": 100_000}})
        return httpx.Response(200, json={"object": "response", "usage": {
            "input_tokens": 1_000_000, "input_tokens_details": {"cached_tokens": 500_000}, "output_tokens": 0}})

    def make(budget=100.0, agent_budget=None, **kw):
        kw = {"upstreams": {"anthropic": "http://up-a", "openai": "http://up-o"},
              "api_keys": {"anthropic": "REAL-A", "openai": "REAL-O"}, **kw}
        proxy = Proxy(ADMIN, budget, agent_budget, log_path=str(tmp_path / "usage.jsonl"),
                      keys_path=str(tmp_path / "keys.json"), transport=httpx.MockTransport(upstream), **kw)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(proxy)), base_url="http://proxy")
        return proxy, client

    return make, seen, tmp_path


async def register(client, name="a1"):
    r = await client.post("/admin/agents", json={"name": name}, headers={"authorization": f"Bearer {ADMIN}"})
    assert r.status_code == 200
    return r.json()["key"]


MSG = {"model": "claude-opus-5-5", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]}


async def test_auth_required_and_no_open_relay(env):
    make, seen, _ = env
    _, client = make()
    assert (await client.post("/admin/agents", json={"name": "x"})).status_code == 401
    assert (await client.post("/anthropic/v1/messages", json=MSG)).status_code == 401
    assert (await client.post("/anthropic/v1/messages", json=MSG, headers={"x-api-key": "nope"})).status_code == 401
    key = await register(client)
    assert (await client.post("/anthropic/v1/models", json=MSG, headers={"x-api-key": key})).status_code == 404
    assert (await client.get("/docs")).status_code == 404
    assert seen == []


async def test_key_swapped_and_nonstreaming_metered(env):
    make, seen, tmp = env
    proxy, client = make()
    key = await register(client)
    r = await client.post("/anthropic/v1/messages?beta=true", json=MSG,
                          headers={"x-api-key": key, "anthropic-version": "2023-06-01", "anthropic-beta": "b1"})
    assert r.status_code == 200
    up = seen[-1]
    assert up.headers["x-api-key"] == "REAL-A" and up.headers["anthropic-beta"] == "b1"
    assert str(up.url) == "http://up-a/v1/messages?beta=true"
    assert key not in str(up.headers) and "authorization" not in up.headers
    # 1M input at $4 + 100k output at $20
    assert proxy.usage()["agents"]["a1"]["usd"] == pytest.approx(6.0)

    r = await client.post("/openai/v1/responses", json={"model": "gpt-5.4", "input": "hi"},
                          headers={"authorization": f"Bearer {key}"})
    assert r.status_code == 200 and seen[-1].headers["authorization"] == "Bearer REAL-O"
    # 1M prompt is over 272K, so long-context rates: 500k uncached at $5 + 500k cached at $0.50
    rec = json.loads((tmp / "usage.jsonl").read_text().splitlines()[-1])
    assert rec["usd"] == pytest.approx(2.75) and rec["agent"] == "a1" and rec["status"] == 200


async def test_anthropic_sse_passthrough_and_metered(env):
    make, _, _ = env
    proxy, client = make()
    key = await register(client)
    r = await client.post("/anthropic/v1/messages", json={**MSG, "stream": True}, headers={"x-api-key": key})
    assert r.status_code == 200 and r.content == b"".join(SSE)
    tokens = proxy.usage()["agents"]["a1"]["tokens"]
    assert tokens["input"] == 1000 and tokens["cache_read"] == 2000 and tokens["output"] == 500
    assert proxy.usage()["total"]["usd"] == pytest.approx((1000 * 4 + 2000 * 0.2 + 500 * 20) / 1e6)


async def test_budget_refusal(env):
    make, seen, tmp = env
    proxy, client = make(budget=5.0)
    key = await register(client)
    assert (await client.post("/anthropic/v1/messages", json=MSG, headers={"x-api-key": key})).status_code == 200
    r = await client.post("/anthropic/v1/messages", json=MSG, headers={"x-api-key": key})
    assert r.status_code == 429 and "budget" in r.json()["error"]["message"]
    assert len(seen) == 1
    # spend survives a restart
    proxy2, client2 = make(budget=5.0)
    assert (await client2.post("/anthropic/v1/messages", json=MSG, headers={"x-api-key": key})).status_code == 429

    _, client3 = make(budget=100.0, agent_budget=1.0)
    other = await register(client3, "a2")
    assert (await client3.post("/anthropic/v1/messages", json=MSG, headers={"x-api-key": other})).status_code == 200
    assert (await client3.post("/anthropic/v1/messages", json=MSG, headers={"x-api-key": key})).status_code == 429


async def test_count_tokens_forwarded_free_but_budgeted(env):
    make, seen, _ = env
    proxy, client = make(budget=5.0)
    key = await register(client)
    body = {**MSG, "model": "claude-unpriced"}
    assert (await client.post("/anthropic/v1/messages/count_tokens", json=body)).status_code == 401
    r = await client.post("/anthropic/v1/messages/count_tokens", json=body, headers={"x-api-key": key})
    assert r.status_code == 200 and seen[-1].url.path == "/v1/messages/count_tokens"
    assert seen[-1].headers["x-api-key"] == "REAL-A" and proxy.usage()["total"]["usd"] == 0
    await client.post("/anthropic/v1/messages", json=MSG, headers={"x-api-key": key})  # $6, over the cap
    r = await client.post("/anthropic/v1/messages/count_tokens", json=body, headers={"x-api-key": key})
    assert r.status_code == 429


async def test_unknown_model_refused(env):
    make, seen, _ = env
    _, client = make()
    key = await register(client)
    r = await client.post("/anthropic/v1/messages", json={**MSG, "model": "claude-free-9"}, headers={"x-api-key": key})
    assert r.status_code == 400 and seen == []


async def test_rate_limit_is_a_retryable_429_unlike_budget(env):
    make, seen, tmp = env
    proxy, client = make()
    proxy.limiter.rate, proxy.limiter.burst = 0.001, 2
    key = await register(client)
    for _ in range(2):
        assert (await client.post("/anthropic/v1/messages", json=MSG, headers={"x-api-key": key})).status_code == 200
    r = await client.post("/anthropic/v1/messages", json=MSG, headers={"x-api-key": key})
    assert r.status_code == 429 and r.json()["error"]["type"] == "rate_limit_error"
    assert int(r.headers["retry-after"]) >= 1 and r.headers["x-should-retry"] == "true"
    assert "try again in" in r.json()["error"]["message"] and len(seen) == 2
    rec = json.loads((tmp / "usage.jsonl").read_text().splitlines()[-1])
    assert rec["status"] == 429 and rec["reason"] == "rate_limit"

    proxy, client = make(budget=5.0)  # $12 already spent above
    r = await client.post("/anthropic/v1/messages", json=MSG, headers={"x-api-key": key})
    assert r.status_code == 429 and r.headers["x-should-retry"] == "false" and "retry-after" not in r.headers
    assert json.loads((tmp / "usage.jsonl").read_text().splitlines()[-1])["reason"] == "budget"


async def test_concurrency_cap(tmp_path):
    import asyncio

    gate = asyncio.Event()

    async def slow(request):
        await gate.wait()
        return httpx.Response(200, json={"type": "message", "usage": {"input_tokens": 1, "output_tokens": 1}})

    proxy = Proxy(ADMIN, 100.0, log_path=str(tmp_path / "u.jsonl"), keys_path=str(tmp_path / "k.json"),
                  upstreams={"anthropic": "http://up"}, api_keys={"anthropic": "REAL"},
                  transport=httpx.MockTransport(slow), max_concurrent=1)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(proxy)), base_url="http://proxy")
    key = await register(client)
    first = asyncio.create_task(client.post("/anthropic/v1/messages", json=MSG, headers={"x-api-key": key}))
    await asyncio.sleep(0.05)
    r = await client.post("/anthropic/v1/messages", json=MSG, headers={"x-api-key": key})
    assert r.status_code == 429 and "in flight" in r.json()["error"]["message"]
    gate.set()
    assert (await first).status_code == 200 and proxy.in_flight["a1"] == 0
    assert (await client.post("/anthropic/v1/messages", json=MSG, headers={"x-api-key": key})).status_code == 200


async def test_stream_error_logged_and_detected(env):
    from multi_agent_harness.proxy import Meter

    m = Meter("anthropic")
    m.feed(SSE[0] + b'event: error\ndata: {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}\n\n')
    assert m.error == "overloaded_error: Overloaded" and m.tokens()["input"] == 1000

    m = Meter("openai")
    m.feed(b'data: {"type":"response.failed","response":{"error":{"code":"server_error","message":"boom"},'
           b'"usage":null}}\n\n')
    assert m.error == "server_error: boom" and m.done

    m = Meter("openai")
    m.feed(b'data: {"type":"response.output_text.delta","delta":"the \\"error\\" word"}\n\n'
           b'data: {"type":"response.completed","response":{"error":null,"usage":{"input_tokens":5}}}\n\n')
    assert m.error is None and m.done and m.tokens()["input"] == 5

    # through the proxy: still status 200, but the record says what went wrong
    make, _, tmp = env
    SSE.append(b'event: error\ndata: {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}\n\n')
    try:
        _, client = make()
        key = await register(client)
        await client.post("/anthropic/v1/messages", json={**MSG, "stream": True}, headers={"x-api-key": key})
    finally:
        SSE.pop()
    rec = json.loads((tmp / "usage.jsonl").read_text().splitlines()[-1])
    assert rec["status"] == 200 and rec["reason"] == "stream_error" and "Overloaded" in rec["error"]


async def test_gateway_upstream_headers_and_model_map(env):
    """A model gateway: one root and key for both formats, attribution headers, aliases billed as
    their real model, and anything unmapped or unpriced refused before forwarding."""
    make, seen, tmp = env
    proxy, client = make(upstreams={"anthropic": "http://gw", "openai": "http://gw"},
                         api_keys={"anthropic": "GW-KEY", "openai": "GW-KEY"},
                         upstream_headers={"X-Gateway-Request-Tag": "trial1.{agent}",
                                           "X-Gateway-Workload-Id": "wl-1", "Authorization": "nope"},
                         model_map={"gw-opus": "claude-opus-5-5", "gw-luna": "gpt-5.6-luna", "gw-unpriced": "gpt-6.1-sol-pro"})
    key = await register(client)
    r = await client.post("/anthropic/v1/messages", json={**MSG, "model": "gw-opus"}, headers={"x-api-key": key})
    assert r.status_code == 200
    up = seen[-1]
    assert str(up.url) == "http://gw/v1/messages" and up.headers["x-api-key"] == "GW-KEY"
    assert up.headers["x-gateway-request-tag"] == "trial1.a1" and up.headers["x-gateway-workload-id"] == "wl-1"
    assert "authorization" not in up.headers and json.loads(up.content)["model"] == "gw-opus"
    r = await client.post("/openai/v1/responses", json={"model": "gw-luna", "input": "hi"},
                          headers={"authorization": f"Bearer {key}"})
    assert r.status_code == 200 and seen[-1].headers["authorization"] == "Bearer GW-KEY"
    n = len(seen)
    for model in ("gw-unpriced", "no-such-alias"):  # mapped but unpriced, and not mapped at all
        r = await client.post("/openai/v1/responses", json={"model": model, "input": "hi"},
                              headers={"authorization": f"Bearer {key}"})
        assert r.status_code == 400 and "no price" in r.json()["error"]["message"]
    assert len(seen) == n
    recs = [json.loads(l) for l in (tmp / "usage.jsonl").read_text().splitlines()]
    assert recs[0]["model"] == "gw-opus" and recs[0]["billed_as"] == "claude-opus-5-5"
    assert recs[0]["usd"] == pytest.approx(4 + 2)  # priced as Opus 5.5
    # gpt-5.6-luna long context (1M prompt): 500k input at $0.4 + 500k cached at $0.04
    assert recs[1]["billed_as"] == "gpt-5.6-luna" and recs[1]["usd"] == pytest.approx(0.2 + 0.02)
    assert recs[2]["billed_as"] == "gpt-6.1-sol-pro" and "billed_as" not in recs[3]


@pytest.mark.parametrize("provider,model", [
    ("openai", "gpt-5"), ("openai", "gpt-5.1"), ("openai", "gpt-5.2"), ("openai", "gpt-5.3-codex"),
    ("anthropic", "claude-sonnet-4-5-20250929"), ("anthropic", "claude-opus-4-5-20251101"),
])
def test_dated_model_ids_are_priced(provider, model):
    # dated ids a gateway may report as the real model behind an alias
    from multi_agent_harness.proxy.metering import Pricing
    price = Pricing.load().lookup(provider, model)
    assert price and price["input"] > 0 and price["output"] > 0 and "cache_read" in price


async def test_gemini_key_swap_and_metering(tmp_path):
    from datetime import date

    from multi_agent_harness.proxy.metering import Pricing
    seen = []
    usage = {"promptTokenCount": 1_000_000, "cachedContentTokenCount": 200_000,
             "candidatesTokenCount": 100_000, "thoughtsTokenCount": 100_000}

    async def upstream(request):
        seen.append(request)
        if request.url.path.endswith(":streamGenerateContent"):
            chunks = [b'data: {"candidates":[{"content":{"parts":[{"text":"h"}]}}],"usageMetadata":'
                      b'{"promptTokenCount":1000000,"candidatesTokenCount":1}}\r\n\r\n',
                      b'data: {"candidates":[{"content":{"parts":[{"text":"i"}]},"finishReason":"STOP"}],'
                      b'"usageMetadata":' + json.dumps(usage).encode() + b'}\r\n\r\n']

            async def gen():
                for c in chunks:
                    yield c
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=gen())
        return httpx.Response(200, json={"candidates": [], "usageMetadata": usage})

    proxy = Proxy(ADMIN, 100.0, log_path=str(tmp_path / "usage.jsonl"), keys_path=str(tmp_path / "keys.json"),
                  upstreams={"gemini": "http://up-g"}, api_keys={"gemini": "REAL-G"},
                  transport=httpx.MockTransport(upstream))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(proxy)), base_url="http://proxy")
    key = await register(client)
    body = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}
    base = "/gemini/v1beta/models/gemini-3.1-pro-preview"
    r = await client.post(f"{base}:streamGenerateContent?alt=sse", json=body, headers={"x-goog-api-key": key})
    assert r.status_code == 200 and b'"finishReason":"STOP"' in r.content
    r = await client.post(f"{base}:generateContent?key={key}", json=body, headers={"x-goog-api-key": key})
    assert r.status_code == 200
    assert [str(u.url) for u in seen] == ["http://up-g/v1beta/models/gemini-3.1-pro-preview:streamGenerateContent?alt=sse",
                                          "http://up-g/v1beta/models/gemini-3.1-pro-preview:generateContent"]
    assert all(u.headers["x-goog-api-key"] == "REAL-G" and key not in str(u.headers) for u in seen)
    recs = [json.loads(line) for line in (tmp_path / "usage.jsonl").read_text().splitlines()]
    assert [r["model"] for r in recs] == ["gemini-3.1-pro-preview"] * 2 and "error" not in recs[0]
    assert recs[0]["tokens"] == {"input": 800_000, "output": 200_000, "cache_read": 200_000,
                                 "cache_write": 0, "cache_write_1h": 0}
    # prompt 1M > 200k: long-context rates, 800k at $4 + 200k cached at $0.4 + 200k output at $18
    assert recs[0]["usd"] == recs[1]["usd"] == pytest.approx(3.2 + 0.08 + 3.6)
    assert (await client.post(f"{base}:embedContent", json=body, headers={"x-goog-api-key": key})).status_code == 404
    flash = Pricing.load()
    assert flash.lookup("gemini", "gemini-3.8-flash", date(2026, 12, 31))["input"] == 0.75
    assert flash.lookup("gemini", "gemini-3.8-flash", date(2027, 1, 1))["input"] == 1.5
