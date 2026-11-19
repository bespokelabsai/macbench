"""Model API proxy: agents call it with their own key, it forwards with the real key and meters USD.

Only the routes below are relayed; anything else is a 404, so this is never an open relay.
Config is all environment so a Harbor environment can start it with one command:
  PROXY_ADMIN_TOKEN         required; registers agents and reads usage
  PROXY_BUDGET_USD          required; hard cap on total spend
  PROXY_AGENT_BUDGET_USD    optional hard cap per agent
  PROXY_LOG                 JSONL usage records (default proxy-usage.jsonl); replayed on start
  PROXY_KEYS                agent key hashes (default proxy-keys.json)
  PROXY_PRICING             optional pricing.json override
  ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY   the key sent to each format's upstream, read by name
  PROXY_ANTHROPIC_UPSTREAM, PROXY_OPENAI_UPSTREAM, PROXY_GEMINI_UPSTREAM
                            upstream base URLs (default the providers' own APIs)
  PROXY_UPSTREAM_HEADERS    optional JSON object of extra upstream headers; "{agent}" becomes the agent's name
  PROXY_MODEL_MAP           optional JSON file {name an agent sends: model it is billed as}, for gateway aliases
  PROXY_CODEX_LOGINS        optional Codex auth.json paths (os.pathsep separated): the openai route then
                            goes to the ChatGPT backend on those subscription logins (subscription.py)
  PROXY_RATE_PER_SEC, PROXY_RATE_BURST   per-agent token bucket on requests (default 4/sec, burst 20; 0 = off)
  PROXY_MAX_CONCURRENT      per-agent requests in flight (default 12; 0 = off)

Budget checks happen before forwarding, so requests already in flight when a cap is crossed
still complete and are billed: spend can overshoot a cap by the cost of those requests.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from ..board.ratelimit import RateLimiter
from .metering import Meter, Pricing, cost, zero_tokens
from .subscription import Logins, SubscriptionError, retry_after

UPSTREAMS = {"anthropic": "https://api.anthropic.com", "openai": "https://api.openai.com",
             "gemini": "https://generativelanguage.googleapis.com"}
API_KEY_VARS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}
# Gemini API methods relayed under /gemini/v1beta/models/{model}:{method}; True = metered.
GEMINI_METHODS = {"generateContent": True, "streamGenerateContent": True, "countTokens": False}
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
# Extra upstream headers can't replace these.
_RESERVED_HEADERS = {"content-type", "accept", "accept-encoding", "x-api-key", "x-goog-api-key", "authorization",
                     "anthropic-version", "anthropic-beta", "host", "content-length"}
_DROP_RESPONSE_HEADERS = {"content-length", "content-encoding", "transfer-encoding", "connection",
                          "keep-alive", "set-cookie"}


def _bearer(value: str | None) -> str | None:
    if value and value.lower().startswith("bearer "):
        return value[7:].strip()
    return None


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _error(provider: str, status: int, kind: str, message: str, retry_after: float | None = None) -> JSONResponse:
    """An error in the provider's own shape, so the agent's CLI shows the message.

    A 429 with retry_after is a rate limit the CLI should wait out and retry. Any other
    429 is the budget, which retrying cannot fix.
    """
    if provider == "anthropic":
        body = {"type": "error", "error": {"type": kind, "message": message}}
    elif provider == "gemini":
        body = {"error": {"code": status, "message": message, "status": kind}}
    else:
        body = {"error": {"message": message, "type": kind, "code": kind}}
    headers = None
    if retry_after is not None:
        headers = {"retry-after": str(max(1, math.ceil(retry_after))),
                   "retry-after-ms": str(max(1, round(retry_after * 1000))), "x-should-retry": "true"}
    elif status == 429:
        headers = {"x-should-retry": "false"}
    return JSONResponse(body, status_code=status, headers=headers)


class RegisterBody(BaseModel):
    name: str


class Proxy:
    def __init__(self, admin_token: str, budget_usd: float, agent_budget_usd: float | None = None,
                 log_path: str = "proxy-usage.jsonl", keys_path: str = "proxy-keys.json",
                 pricing: Pricing | None = None, upstreams: dict | None = None,
                 api_keys: dict | None = None, transport: httpx.AsyncBaseTransport | None = None,
                 upstream_headers: dict | None = None, model_map: dict | None = None,
                 rate_per_sec: float = 4.0, rate_burst: float = 20, max_concurrent: int = 12,
                 subscription: Logins | None = None):
        self.admin_token = admin_token
        self.budget_usd = budget_usd
        self.agent_budget_usd = agent_budget_usd
        self.log_path = Path(log_path)
        self.keys_path = Path(keys_path)
        self.pricing = pricing or Pricing.load()
        self.upstreams = {**UPSTREAMS, **(upstreams or {})}
        self.api_keys = api_keys or {}
        self.upstream_headers = {k: str(v) for k, v in (upstream_headers or {}).items()
                                 if k.lower() not in _RESERVED_HEADERS}
        self.model_map = dict(model_map or {})
        self.client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(900, connect=15))
        self.agents: dict[str, str] = json.loads(self.keys_path.read_text()) if self.keys_path.exists() else {}
        self._by_hash = {h: name for name, h in self.agents.items()}
        self.totals: dict[str, dict] = {}
        self.limiter = RateLimiter(rate_per_sec, rate_burst)
        self.max_concurrent = int(max_concurrent)
        self.in_flight: dict[str, int] = {}
        self._register_lock = threading.Lock()  # sync handlers run in a threadpool
        self.subscription = subscription  # ChatGPT logins for the openai route, instead of a key
        self._replay()

    # -- auth and accounting --

    def is_admin(self, authorization: str | None) -> bool:
        return hmac.compare_digest(_bearer(authorization) or "", self.admin_token)

    def register(self, name: str) -> str:
        if not _NAME.match(name):
            raise HTTPException(400, "agent name must match [A-Za-z0-9_.-]{1,64}")
        with self._register_lock:
            if name in self.agents:
                raise HTTPException(409, f"agent {name!r} already registered")
            key = "multi-agent-harness-" + secrets.token_urlsafe(32)
            self.agents[name] = _hash(key)
            self._by_hash[self.agents[name]] = name
            tmp = self.keys_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.agents))
            tmp.replace(self.keys_path)
            return key

    def authenticate(self, key: str | None) -> str | None:
        return self._by_hash.get(_hash(key)) if key else None

    def _bucket(self, agent: str) -> dict:
        return self.totals.setdefault(agent, {"usd": 0.0, "requests": 0, "tokens": zero_tokens()})

    def _add(self, agent: str | None, usd: float, tokens: dict) -> None:
        for name in ("*", agent) if agent else ("*",):
            b = self._bucket(name)
            b["usd"] += usd
            b["requests"] += 1
            for k, v in tokens.items():
                b["tokens"][k] = b["tokens"].get(k, 0) + v

    def _replay(self) -> None:
        if not self.log_path.exists():
            return
        for line in self.log_path.read_text().splitlines():
            try:
                r = json.loads(line)
                self._add(r.get("agent"), float(r.get("usd") or 0), r.get("tokens") or {})
                if self.subscription and r.get("agent") and isinstance(r.get("login"), int):
                    self.subscription.assigned.setdefault(r["agent"], r["login"])  # keep agents sticky
            except (ValueError, TypeError, AttributeError):
                continue

    def _record(self, agent, provider, model, path, status, t0, tokens=None, usd=0.0, **extra) -> None:
        tokens = tokens or zero_tokens()
        self._add(agent, usd, tokens)
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "agent": agent, "provider": provider,
               "model": model, "path": path, "status": status, "tokens": tokens, "usd": round(usd, 8),
               "latency_ms": round((time.monotonic() - t0) * 1000, 1), **extra}
        with self.log_path.open("a") as f:
            f.write(json.dumps(rec) + "\n")

    def over_budget(self, agent: str) -> str | None:
        total = self._bucket("*")["usd"]
        if total >= self.budget_usd:
            return f"team budget exhausted: ${total:.4f} spent of ${self.budget_usd:.2f}"
        mine = self._bucket(agent)["usd"]
        if self.agent_budget_usd is not None and mine >= self.agent_budget_usd:
            return f"agent budget exhausted for {agent}: ${mine:.4f} spent of ${self.agent_budget_usd:.2f}"
        return None

    def acquire(self, agent: str) -> tuple[str, float] | None:
        """Take a concurrency slot and a rate token, or return why not and how long to wait.
        On success the caller must release() the slot."""
        if self.max_concurrent > 0 and self.in_flight.get(agent, 0) >= self.max_concurrent:
            return (f"rate limit: {agent} already has {self.max_concurrent} requests in flight. "
                    "Please try again in 1s."), 1.0
        if wait := self.limiter.take(agent):
            return (f"rate limit: {agent} is over {self.limiter.rate:g} requests/sec "
                    f"(burst {self.limiter.burst:g}). Please try again in {wait:.2f}s."), wait
        self.in_flight[agent] = self.in_flight.get(agent, 0) + 1
        return None

    def release(self, agent: str) -> None:
        self.in_flight[agent] = max(0, self.in_flight.get(agent, 0) - 1)

    def usage(self) -> dict:
        agents = {name: self._bucket(name) for name in self.agents}
        agents.update({k: v for k, v in self.totals.items() if k not in ("*", None)})
        return {"budget_usd": self.budget_usd, "agent_budget_usd": self.agent_budget_usd,
                "total": self._bucket("*"), "agents": agents}

    @staticmethod
    def multiplier(provider: str, body: dict, price: dict) -> tuple[float, str | None]:
        """Price modifiers set in the request. Unknown ones are refused rather than under-billed."""
        if provider == "gemini":  # no request field changes the Standard price; a tier we don't know is refused
            tier = body.get("serviceTier") or body.get("service_tier")
            return 1.0, None if tier in (None, "standard", "STANDARD") else f"serviceTier={tier!r} is not priced"
        if provider == "anthropic":
            m = 1.0
            speed = body.get("speed")
            if speed == "fast" and price.get("fast_multiplier"):
                m *= price["fast_multiplier"]
            elif speed not in (None, "standard"):
                return m, f"speed={speed!r} is not priced for this model"
            geo = body.get("inference_geo")
            if geo == "us":
                m *= 1.1
            elif geo not in (None, "global"):
                return m, f"inference_geo={geo!r} is not priced"
            return m, None
        tier = body.get("service_tier")
        if tier in (None, "auto", "default", "flex"):
            return 1.0, None  # flex bills at half; metering it as standard over-counts, never under
        if tier in ("priority", "fast"):
            return 2.0, None
        return 1.0, f"service_tier={tier!r} is not priced"

    # -- forwarding --

    async def forward(self, provider: str, path: str, request: Request, metered: bool = True,
                      model: str | None = None) -> Response:
        """Relay one request. metered=False is for free endpoints: no price needed, recorded at $0.
        `model` is for formats that name it in the path (Gemini) rather than the body."""
        t0 = time.monotonic()
        agent = self.authenticate(request.headers.get("x-api-key") or request.headers.get("x-goog-api-key")
                                  or _bearer(request.headers.get("authorization")))
        if agent is None:
            self._record(None, provider, None, path, 401, t0)
            return _error(provider, 401, "authentication_error", "missing or unknown agent key")
        raw = await request.body()
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            self._record(agent, provider, None, path, 400, t0)
            return _error(provider, 400, "invalid_request_error", "request body must be a JSON object")
        model = model or body.get("model")
        if limited := self.acquire(agent):
            msg, wait = limited
            self._record(agent, provider, model, path, 429, t0, error=msg, reason="rate_limit")
            kind = "rate_limit_error" if provider == "anthropic" else "rate_limit_exceeded"
            return _error(provider, 429, kind, msg, retry_after=wait)
        released = False

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                self.release(agent)

        try:
            return await self._forward(provider, path, request, metered, t0, agent, body, raw, model, release)
        except BaseException:
            release()
            raise

    async def _forward(self, provider, path, request, metered, t0, agent, body, raw, model, release) -> Response:
        """The rest of forward(), holding one of the agent's concurrency slots until release()."""
        billed = self.model_map.get(model, model) if isinstance(model, str) else model
        bill = {"billed_as": billed} if billed != model else {}
        sub = self.subscription if provider == "openai" else None
        if sub:  # priced at API rates from the table, paid by the subscription
            bill = {"billed_as": "subscription"}
            if path != "v1/responses":
                msg = "only /v1/responses is served on a ChatGPT subscription"
                self._record(agent, provider, model, path, 400, t0, error=msg, **bill)
                release()
                return _error(provider, 400, "invalid_request_error", msg)
        price = self.pricing.lookup(provider, billed) if metered else {"input": 0, "output": 0}
        mult, problem = self.multiplier(provider, body, price) if price and metered else (1.0, None)
        if price is None or problem:
            name = f"{model!r} (billed as {billed!r})" if billed != model else repr(model)
            msg = problem or f"model {name} has no price in the proxy's table; refused so the budget holds"
            self._record(agent, provider, model, path, 400, t0, error=msg, **bill)
            release()
            return _error(provider, 400, "invalid_request_error", msg)
        if over := self.over_budget(agent):
            self._record(agent, provider, model, path, 429, t0, error=over, reason="budget", **bill)
            release()
            return _error(provider, 429, "budget_exceeded", over)
        real_key = self.api_keys.get(provider)
        if not real_key and not sub:
            self._record(agent, provider, model, path, 503, t0, error="upstream key not configured", **bill)
            release()
            return _error(provider, 503, "api_error", f"proxy has no {provider} key configured")

        stream = bool(body.get("stream")) or path.endswith(":streamGenerateContent")
        if provider == "openai" and path.endswith("chat/completions") and stream:
            opts = body.get("stream_options") or {}
            if not opts.get("include_usage"):  # otherwise a streamed chat call would go unmetered
                body["stream_options"] = {**opts, "include_usage": True}
                raw = json.dumps(body).encode()

        headers = {"content-type": "application/json", "accept-encoding": "identity"}
        if accept := request.headers.get("accept"):
            headers["accept"] = accept
        if provider == "anthropic":
            headers["x-api-key"] = real_key
            for h in ("anthropic-version", "anthropic-beta"):
                if v := request.headers.get(h):
                    headers[h] = v
        elif provider == "gemini":
            headers["x-goog-api-key"] = real_key
        elif not sub:  # a subscription login's token is added per request in send()
            headers["authorization"] = f"Bearer {real_key}"
        for k, v in self.upstream_headers.items():
            headers[k] = v.replace("{agent}", agent)
        url = f"{self.upstreams[provider].rstrip('/')}/{path}"
        client, login, token = self.client, None, None
        if sub:  # the ChatGPT backend serves /responses right under its base URL
            url = f"{self.upstreams[provider].rstrip('/')}/{path.removeprefix('v1/')}"
            login = sub.login_for(agent)
            bill["login"] = login.index
            client = login.client
        # A key in the query (the Gemini API allows ?key=) is the agent's, never forwarded.
        query = "&".join(p for p in request.url.query.split("&") if p and not p.startswith("key="))
        if query:
            url += "?" + query

        async def send(rejected: str | None = None):
            nonlocal token
            h = headers
            if login is not None:
                token = await sub.token(login, rejected)
                h = {**headers, **sub.headers(login, token, request.headers)}
            return await client.send(client.build_request("POST", url, content=raw, headers=h), stream=True)

        try:
            resp = await send()
            if login is not None and resp.status_code == 401:  # revoked or expired early: refresh once
                await resp.aclose()
                resp = await send(rejected=token)
        except SubscriptionError as e:
            self._record(agent, provider, model, path, 502, t0, error=str(e), reason="subscription_auth", **bill)
            release()
            return _error(provider, 502, "api_error", str(e))
        except httpx.HTTPError as e:
            self._record(agent, provider, model, path, 502, t0, error=type(e).__name__, **bill)
            release()
            return _error(provider, 502, "api_error", f"upstream unreachable: {type(e).__name__}")

        if login is not None and resp.status_code == 429:  # the login's plan limit: wait it out
            try:
                data = await resp.aread()
            finally:
                await resp.aclose()
            wait, kind = retry_after(resp.headers, data)
            msg = (f"rate limit: subscription login {login.index} is at its usage limit ({kind}). "
                   f"Please try again in {math.ceil(wait)}s.")
            self._record(agent, provider, model, path, 429, t0, error=msg, reason="subscription_rate_limit", **bill)
            release()
            return _error(provider, 429, "rate_limit_exceeded", msg, retry_after=wait)

        meter = Meter(provider)
        out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _DROP_RESPONSE_HEADERS}

        finished = False

        def finish(complete: bool) -> None:
            nonlocal finished
            if finished:
                return
            finished = True
            release()
            m = mult
            if provider == "openai" and meter.service_tier in ("priority", "fast"):
                m = max(m, 2.0)
            tokens = meter.tokens() if metered else zero_tokens()
            extra = dict(bill)
            if meter.error:  # the provider sent an error event inside a 200 stream
                extra |= {"reason": "stream_error", "error": meter.error}
            elif not (complete or meter.done):  # hanging up after the final event is normal
                extra |= {"error": "stream ended early"}
            self._record(agent, provider, model, path, resp.status_code, t0, tokens, cost(price, tokens, m),
                         stream=stream, **extra)

        async def close(complete: bool = False) -> None:
            finish(complete)
            await resp.aclose()

        ctype = resp.headers.get("content-type", "")
        # The ChatGPT backend streams with no content-type at all, so a streamed request counts too.
        if ctype.startswith("text/event-stream") or (stream and not ctype):
            async def relay():
                complete = False
                try:
                    async for chunk in resp.aiter_bytes():
                        meter.feed(chunk)
                        yield chunk
                    complete = True
                finally:
                    await close(complete)

            # The background task covers a client that disconnects before the body starts.
            return StreamingResponse(relay(), status_code=resp.status_code, headers=out_headers,
                                     background=BackgroundTask(close))

        try:
            data = await resp.aread()
        finally:
            await resp.aclose()
        meter.body(data)
        finish(True)
        return Response(data, status_code=resp.status_code, headers=out_headers)


def build_app(proxy: Proxy | None = None) -> FastAPI:
    if proxy is None:
        admin = os.environ.get("PROXY_ADMIN_TOKEN")
        budget = os.environ.get("PROXY_BUDGET_USD")
        if not admin or not budget:
            raise SystemExit("PROXY_ADMIN_TOKEN and PROXY_BUDGET_USD are required")
        agent_budget = os.environ.get("PROXY_AGENT_BUDGET_USD")
        limits = {k: float(os.environ[v]) for k, v in (("rate_per_sec", "PROXY_RATE_PER_SEC"),
                  ("rate_burst", "PROXY_RATE_BURST"), ("max_concurrent", "PROXY_MAX_CONCURRENT"))
                  if os.environ.get(v)}
        proxy = Proxy(
            admin, float(budget), float(agent_budget) if agent_budget else None,
            log_path=os.environ.get("PROXY_LOG", "proxy-usage.jsonl"),
            keys_path=os.environ.get("PROXY_KEYS", "proxy-keys.json"),
            pricing=Pricing.load(os.environ.get("PROXY_PRICING")),
            upstreams={p: os.environ[f"PROXY_{p.upper()}_UPSTREAM"] for p in UPSTREAMS
                       if os.environ.get(f"PROXY_{p.upper()}_UPSTREAM")},
            api_keys={p: os.environ.get(v) for p, v in API_KEY_VARS.items()},
            upstream_headers=json.loads(os.environ.get("PROXY_UPSTREAM_HEADERS") or "{}"),
            model_map=json.loads(Path(os.environ["PROXY_MODEL_MAP"]).read_text())
            if os.environ.get("PROXY_MODEL_MAP") else None,
            subscription=Logins(os.environ["PROXY_CODEX_LOGINS"].split(os.pathsep))
            if os.environ.get("PROXY_CODEX_LOGINS") else None,
            **limits,
        )

    @asynccontextmanager
    async def lifespan(_):
        yield
        await proxy.client.aclose()
        if proxy.subscription:
            await proxy.subscription.aclose()

    app = FastAPI(title="proxy", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.proxy = proxy

    @app.post("/admin/agents")
    def register(body: RegisterBody, authorization: str | None = Header(None)):
        if not proxy.is_admin(authorization):
            raise HTTPException(401, "admin token required")
        return {"agent": body.name, "key": proxy.register(body.name)}

    @app.get("/admin/usage")
    def usage(authorization: str | None = Header(None)):
        if not proxy.is_admin(authorization):
            raise HTTPException(401, "admin token required")
        return proxy.usage()

    @app.post("/anthropic/v1/messages")
    async def anthropic_messages(request: Request):
        return await proxy.forward("anthropic", "v1/messages", request)

    @app.post("/anthropic/v1/messages/count_tokens")
    async def anthropic_count_tokens(request: Request):
        return await proxy.forward("anthropic", "v1/messages/count_tokens", request, metered=False)

    @app.post("/openai/v1/responses")
    async def openai_responses(request: Request):
        return await proxy.forward("openai", "v1/responses", request)

    @app.post("/openai/v1/chat/completions")
    async def openai_chat(request: Request):
        return await proxy.forward("openai", "v1/chat/completions", request)

    @app.post("/gemini/v1beta/models/{target}")
    async def gemini(target: str, request: Request):
        model, _, method = target.partition(":")
        if method not in GEMINI_METHODS or not _NAME.match(model):
            raise HTTPException(404, "not found")
        return await proxy.forward("gemini", f"v1beta/models/{model}:{method}", request,
                                   metered=GEMINI_METHODS[method], model=model)

    return app


def main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Run the model API proxy")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8791)
    args = parser.parse_args()
    uvicorn.run(build_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
