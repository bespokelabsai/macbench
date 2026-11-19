"""ChatGPT subscription logins for the openai route (model_upstream=subscription).

Each login is a Codex auth.json from `codex login` (tokens.access_token, refresh_token, account_id),
a root-only file on the hub. Agents never see them: they call the proxy with their own key, and
the proxy calls https://chatgpt.com/backend-api/codex/responses with the login's access token and
ChatGPT-Account-ID, as Codex itself does in ChatGPT mode (openai/codex codex-rs/model-provider).

An agent sticks to one login, new agents go to the login with the fewest agents. Access tokens
are refreshed here, under a per-login lock since refresh tokens are single use, and written back
to the login's file so a restarted proxy keeps the latest ones.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

# Same endpoint and client id as Codex (codex-rs/login/src/auth/manager.rs).
REFRESH_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REFRESH_EARLY_SEC = 300  # Codex refreshes 5 minutes before expiry
REFRESH_MAX_AGE_SEC = 8 * 86400  # and when the last refresh is 8 days old
# Codex request headers worth passing on; none of them carry auth.
FORWARD_HEADERS = ("originator", "version", "session_id", "x-client-request-id", "openai-beta",
                   "user-agent", "x-codex-turn-state", "x-codex-turn-metadata", "x-codex-window-id",
                   "x-openai-subagent")


class SubscriptionError(Exception):
    """A login that can't produce a token. The message never holds a token."""


def jwt_claims(token: str) -> dict:
    """The unverified payload of a JWT, or {}."""
    try:
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return claims if isinstance(claims, dict) else {}
    except (IndexError, ValueError):
        return {}


def check_login(data) -> str | None:
    """Why `data` is not a usable ChatGPT login, or None."""
    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, dict):
        return "no tokens; sign in with ChatGPT, not an API key"
    if not all(isinstance(tokens.get(k), str) and tokens[k] for k in ("access_token", "refresh_token")):
        return "tokens need access_token and refresh_token"
    return None


def account_id(tokens: dict) -> str | None:
    if tokens.get("account_id"):
        return tokens["account_id"]
    for jwt in (tokens.get("id_token"), tokens.get("access_token")):
        if isinstance(jwt, str):
            auth = jwt_claims(jwt).get("https://api.openai.com/auth") or {}
            if isinstance(auth, dict) and auth.get("chatgpt_account_id"):
                return auth["chatgpt_account_id"]
    return None


class Login:
    def __init__(self, index: int, path: str | Path, transport: httpx.AsyncBaseTransport | None = None):
        self.index = index
        self.path = Path(path)
        self.data = json.loads(self.path.read_text())
        if problem := check_login(self.data):
            raise SubscriptionError(f"login {index} ({self.path.name}): {problem}")
        self.lock = asyncio.Lock()
        # Its own client so ChatGPT's Cloudflare cookies stay with the login they came to.
        self.client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(900, connect=15))

    @property
    def tokens(self) -> dict:
        return self.data["tokens"]

    @property
    def access_token(self) -> str:
        return self.tokens["access_token"]

    @property
    def account_id(self) -> str | None:
        return account_id(self.tokens)

    def stale(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        exp = jwt_claims(self.access_token).get("exp")
        if isinstance(exp, (int, float)) and exp - REFRESH_EARLY_SEC <= now:
            return True
        try:
            last = datetime.fromisoformat(str(self.data.get("last_refresh")).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return not isinstance(exp, (int, float))  # no expiry and no refresh time: refresh to be safe
        return now - last >= REFRESH_MAX_AGE_SEC

    async def refresh(self, url: str) -> None:
        body = {"grant_type": "refresh_token", "client_id": CLIENT_ID, "refresh_token": self.tokens["refresh_token"]}
        try:
            r = await self.client.post(url, json=body, headers={"content-type": "application/json"})
        except httpx.HTTPError as e:
            raise SubscriptionError(f"login {self.index}: refresh failed: {type(e).__name__}") from None
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code != 200 or not isinstance(data, dict) or not isinstance(data.get("access_token"), str):
            err = data.get("error") if isinstance(data, dict) else None
            code = (err.get("code") if isinstance(err, dict) else err) or "no access_token"
            raise SubscriptionError(f"login {self.index}: refresh answered {r.status_code} ({code}); "
                                    "sign in again with codex login")
        for k in ("access_token", "refresh_token", "id_token"):
            if isinstance(data.get(k), str) and data[k]:
                self.tokens[k] = data[k]
        self.data["last_refresh"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self._save()

    def _save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.data, f, indent=2)
        tmp.replace(self.path)


class Logins:
    def __init__(self, paths: list[str | Path], transport: httpx.AsyncBaseTransport | None = None,
                 refresh_url: str = REFRESH_URL):
        if not paths:
            raise SubscriptionError("no subscription logins")
        self.logins = [Login(i + 1, p, transport) for i, p in enumerate(paths)]
        self.refresh_url = refresh_url
        self.assigned: dict[str, int] = {}  # agent -> login index

    def login_for(self, agent: str) -> Login:
        """The agent's login, assigning the least used one on first sight."""
        if agent not in self.assigned:
            load = {lg.index: 0 for lg in self.logins}
            for i in self.assigned.values():
                if i in load:
                    load[i] += 1
            self.assigned[agent] = min(load, key=lambda i: (load[i], i))
        return self.logins[self.assigned[agent] - 1]

    async def token(self, login: Login, rejected: str | None = None) -> str:
        """A current access token. rejected is one upstream just refused with 401: refresh unless
        another request already did."""
        async with login.lock:
            if rejected is not None and login.access_token != rejected:
                return login.access_token
            if rejected is not None or login.stale():
                await login.refresh(self.refresh_url)
            return login.access_token

    def headers(self, login: Login, token: str, request_headers) -> dict[str, str]:
        out = {h: v for h in FORWARD_HEADERS if (v := request_headers.get(h))}
        out["authorization"] = f"Bearer {token}"
        if acct := login.account_id:
            out["chatgpt-account-id"] = acct
        return out

    async def aclose(self) -> None:
        for lg in self.logins:
            await lg.client.aclose()


def retry_after(status_headers, body: bytes, now: float | None = None) -> tuple[float, str]:
    """Seconds until a usage-limited login frees up, and the error type, from a ChatGPT 429."""
    now = time.time() if now is None else now
    try:
        err = (json.loads(body) or {}).get("error") or {}
    except (ValueError, AttributeError):
        err = {}
    err = err if isinstance(err, dict) else {}
    kind = str(err.get("type") or err.get("code") or "rate_limited")
    if isinstance(err.get("resets_at"), (int, float)):
        return max(1.0, err["resets_at"] - now), kind
    for value in (err.get("resets_in_seconds"), status_headers.get("retry-after")):
        try:
            return max(1.0, float(value)), kind
        except (TypeError, ValueError):
            pass
    return 60.0, kind
