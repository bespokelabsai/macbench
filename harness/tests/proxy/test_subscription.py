"""model_upstream=subscription in the proxy: ChatGPT logins, refresh, sticky assignment. Fake upstream."""

import base64
import json
import time

import httpx
import pytest

from multi_agent_harness.proxy import Proxy, build_app
from multi_agent_harness.proxy.subscription import CLIENT_ID, REFRESH_URL, Logins

ADMIN = "admin-secret"
BASE = "https://chatgpt.test/backend-api/codex"
REQ = {"model": "gpt-6-luna", "input": "hi", "stream": False}


def jwt(**claims) -> str:
    part = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"h.{part}.s"


def login_file(path, n, exp_in=3600):
    path.write_text(json.dumps({"OPENAI_API_KEY": None, "last_refresh": "2099-01-01T00:00:00Z", "tokens": {
        "access_token": jwt(exp=time.time() + exp_in, n=n), "refresh_token": f"RT-{n}", "id_token": jwt(n=n),
        "account_id": f"acct-{n}"}}))
    return path


@pytest.fixture
def sub(tmp_path):
    seen, answers = [], []  # answers: queued (status, json) for the responses endpoint

    async def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if str(request.url) == REFRESH_URL:
            body = json.loads(request.content)
            n = body["refresh_token"].split("-")[1]
            return httpx.Response(200, json={"access_token": jwt(exp=time.time() + 3600, n=n, fresh=1),
                                             "refresh_token": f"RT-{n}-new"})
        if answers:
            status, data = answers.pop(0)
            if isinstance(data, bytes):  # a raw SSE body, sent with no content-type like the real backend
                return httpx.Response(status, content=data)
            return httpx.Response(status, json=data)
        return httpx.Response(200, json={"object": "response", "usage": {"input_tokens": 100_000, "output_tokens": 0}})

    def make(n_logins=1, exp_in=3600):
        paths = [login_file(tmp_path / f"login-{i}.json", i, exp_in) for i in range(1, n_logins + 1)]
        transport = httpx.MockTransport(upstream)
        proxy = Proxy(ADMIN, 100.0, log_path=str(tmp_path / "usage.jsonl"), keys_path=str(tmp_path / "keys.json"),
                      upstreams={"openai": BASE}, transport=transport, subscription=Logins(paths, transport))
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(proxy)), base_url="http://proxy")
        return proxy, client, paths

    return make, seen, answers, tmp_path


async def register(client, name):
    r = await client.post("/admin/agents", json={"name": name}, headers={"authorization": f"Bearer {ADMIN}"})
    return r.json()["key"]


def records(tmp):
    return [json.loads(l) for l in (tmp / "usage.jsonl").read_text().splitlines()]


async def test_forwards_with_login_token_and_meters_api_equivalent(sub):
    make, seen, _, tmp = sub
    proxy, client, paths = make()
    key = await register(client, "a1")
    r = await client.post("/openai/v1/responses", json=REQ,
                          headers={"authorization": f"Bearer {key}", "originator": "codex_exec", "session_id": "s1"})
    assert r.status_code == 200
    up = seen[-1]
    token = json.loads(paths[0].read_text())["tokens"]["access_token"]
    assert str(up.url) == f"{BASE}/responses"
    assert up.headers["authorization"] == f"Bearer {token}" and up.headers["chatgpt-account-id"] == "acct-1"
    assert up.headers["originator"] == "codex_exec" and up.headers["session_id"] == "s1"
    assert key not in str(up.headers)
    rec = records(tmp)[-1]
    assert rec["billed_as"] == "subscription" and rec["login"] == 1 and rec["usd"] == pytest.approx(0.01)  # API-equivalent: 100k at $0.10/M
    assert token not in (tmp / "usage.jsonl").read_text()
    assert (await client.post("/openai/v1/chat/completions", json=REQ,
                              headers={"authorization": f"Bearer {key}"})).status_code == 400


async def test_refresh_when_expiring_and_on_401(sub):
    make, seen, answers, _ = sub
    _, client, paths = make(exp_in=60)  # inside the 5 minute window
    key = await register(client, "a1")
    assert (await client.post("/openai/v1/responses", json=REQ, headers={"authorization": f"Bearer {key}"})).status_code == 200
    refresh = [r for r in seen if str(r.url) == REFRESH_URL]
    assert len(refresh) == 1
    assert json.loads(refresh[0].content) == {"grant_type": "refresh_token", "client_id": CLIENT_ID,
                                              "refresh_token": "RT-1"}
    saved = json.loads(paths[0].read_text())
    assert saved["tokens"]["refresh_token"] == "RT-1-new" and paths[0].stat().st_mode & 0o777 == 0o600
    assert seen[-1].headers["authorization"] == f"Bearer {saved['tokens']['access_token']}"
    answers.append((401, {"error": {"code": "token_expired"}}))  # revoked early: refresh once, retry
    assert (await client.post("/openai/v1/responses", json=REQ, headers={"authorization": f"Bearer {key}"})).status_code == 200
    assert len([r for r in seen if str(r.url) == REFRESH_URL]) == 2


async def test_agents_spread_over_logins_and_stick(sub):
    make, seen, _, tmp = sub
    proxy, client, _ = make(n_logins=2)
    keys = {n: await register(client, n) for n in ("a1", "a2", "a3")}
    for n in ("a1", "a2", "a3", "a1", "a2"):
        await client.post("/openai/v1/responses", json=REQ, headers={"authorization": f"Bearer {keys[n]}"})
    by_agent = {}
    for r in records(tmp):
        by_agent.setdefault(r["agent"], set()).add(r["login"])
    assert by_agent == {"a1": {1}, "a2": {2}, "a3": {1}}
    accts = [r.headers["chatgpt-account-id"] for r in seen]
    assert accts == ["acct-1", "acct-2", "acct-1", "acct-1", "acct-2"]


async def test_usage_limit_429_is_a_retryable_rate_limit(sub):
    make, _, answers, tmp = sub
    _, client, _ = make()
    key = await register(client, "a1")
    answers.append((429, {"error": {"type": "usage_limit_reached", "resets_at": int(time.time()) + 120}}))
    r = await client.post("/openai/v1/responses", json=REQ, headers={"authorization": f"Bearer {key}"})
    assert r.status_code == 429 and r.headers["x-should-retry"] == "true"
    assert 110 <= int(r.headers["retry-after"]) <= 121
    assert r.json()["error"]["code"] == "rate_limit_exceeded" and "try again in" in r.json()["error"]["message"]
    assert records(tmp)[-1]["reason"] == "subscription_rate_limit"


async def test_stream_without_content_type_is_metered(sub):
    make, _, answers, tmp = sub
    proxy, client, _ = make()
    key = await register(client, "a1")
    done = {"type": "response.completed", "response": {"usage": {"input_tokens": 100_000, "output_tokens": 0}}}
    answers.append((200, b'event: response.created\ndata: {"type":"response.created","response":{"usage":null}}\n\n'
                         b"event: response.completed\ndata: " + json.dumps(done).encode() + b"\n\n"))
    r = await client.post("/openai/v1/responses", json={**REQ, "stream": True}, headers={"authorization": f"Bearer {key}"})
    assert r.status_code == 200 and b"response.completed" in r.content
    rec = records(tmp)[-1]
    assert rec["tokens"]["input"] == 100_000 and rec["usd"] == pytest.approx(0.01) and "error" not in rec
