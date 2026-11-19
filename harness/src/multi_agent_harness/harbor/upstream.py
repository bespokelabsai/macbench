"""Where the hub's proxy sends model calls: a base URL and a key per provider format.

    --ek model_upstream=direct     (default) api.anthropic.com / api.openai.com /
                                   generativelanguage.googleapis.com with ANTHROPIC_API_KEY /
                                   OPENAI_API_KEY / GEMINI_API_KEY, each only if set
    --ek model_upstream=subscription   the openai format on ChatGPT subscription logins (Codex
                                   auth.json files, see codex_logins); no anthropic upstream.
                                   With --ak harness=claude-code instead: claude-subscription,
                                   Claude Code in each agent sandbox on the runner's own
                                   CLAUDE_CODE_OAUTH_TOKEN (claude setup-token), talking to
                                   Anthropic directly; the hub and its proxy never see the token.
                                   A mixed team (--ak team) with both gets both: Codex agents
                                   on the logins, Claude Code agents on the token
    --ek model_upstream=<url>      any gateway serving the formats at that root (/v1/messages,
                                   /v1/responses, /v1beta/models), with the provider keys as in direct

The proxy itself only knows URLs, keys, extra upstream headers and an optional model map (the
name an agent sends -> the model it is billed as). The modes are resolved here, on the host, before
any sandbox exists. Everything is stdlib so the host needs no extra dependency.
"""

from __future__ import annotations

import json
import re
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

PROVIDERS = ("anthropic", "openai", "gemini")
DIRECT = {"anthropic": "https://api.anthropic.com", "openai": "https://api.openai.com",
          "gemini": "https://generativelanguage.googleapis.com"}
KEY_NAMES = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}

CHATGPT_CODEX_URL = "https://chatgpt.com/backend-api/codex"  # Codex's base URL in ChatGPT mode
CODEX_LOGINS_VAR = "MULTI_AGENT_HARNESS_CODEX_LOGINS"  # a dir of auth.json files; default ~/.codex/auth.json
CODEX_REFRESH_URL = "https://auth.openai.com/oauth/token"  # = proxy.subscription.REFRESH_URL
CLAUDE_TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN"  # from claude setup-token, read from the host env by name
CLAUDE_ROUTE_VAR = "MULTI_AGENT_HARNESS_CLAUDE_ROUTE"  # on subscription, Claude Code through the proxy: "direct" (ANTHROPIC_API_KEY)
# 0 lets Claude Code agents run where bubblewrap cannot (Docker without unprivileged user namespaces): their Bash and
# MCP subprocesses then inherit the token. Default 1: Claude Code scrubs it, which needs a working bubblewrap.
CLAUDE_SCRUB_VAR = "MULTI_AGENT_HARNESS_CLAUDE_ENV_SCRUB"
# What Claude Code needs on an env token: model requests only (code.claude.com/docs/en/network-config).
# OAuth sign-in, refresh (platform.claude.com) and claude.ai connectors don't happen with it.
CLAUDE_AGENT_HOSTS = ["api.anthropic.com"]


@dataclass
class Upstream:
    mode: str  # direct, subscription, claude-subscription or custom
    urls: dict[str, str]  # provider -> base URL, only for providers that have a key
    keys: dict[str, str]
    headers: dict[str, str] = field(default_factory=dict)  # sent upstream; "{agent}" becomes the agent's name
    model_map: dict[str, str] | None = None
    logins: list[bytes] = field(default_factory=list, repr=False)  # subscription credential files
    login_paths: list[Path] = field(default_factory=list)  # where each login came from on the host
    auth_urls: list[str] = field(default_factory=list)  # token refresh endpoints the hub must also reach
    tokens: list[str] = field(default_factory=list, repr=False)  # the logins' tokens, for redaction
    agent_hosts: list[str] = field(default_factory=list)  # hosts the agent sandboxes reach themselves
    # Claude Code agents run on the runner's CLAUDE_CODE_OAUTH_TOKEN and reach agent_hosts themselves.
    claude_subscription: bool = False

    @property
    def probe_urls(self) -> list[str]:
        return list(dict.fromkeys([*self.urls.values(), *self.auth_urls]))

    @property
    def hosts(self) -> list[str]:
        """The hostnames the hub must reach, in provider order."""
        return list(dict.fromkeys(urlparse(u).hostname for u in self.probe_urls))

    @property
    def secrets(self) -> list[str]:
        return [*self.keys.values(), *self.tokens]

    def proxy_env(self) -> dict[str, str]:
        """The proxy's env for this upstream. Direct adds only the keys, as it always has."""
        env = {KEY_NAMES[p]: k for p, k in self.keys.items()}
        env |= {f"PROXY_{p.upper()}_UPSTREAM": u for p, u in self.urls.items() if u != DIRECT[p]}
        if self.headers:
            env["PROXY_UPSTREAM_HEADERS"] = json.dumps(self.headers)
        return env


def check_mode(mode: str | None) -> str:
    """Normalize --ek model_upstream, failing before anything starts."""
    m = str(mode or "direct").strip()
    if m.lower() in ("direct", "subscription"):
        return m.lower()
    u = urlparse(m)
    if u.scheme in ("http", "https") and u.hostname and not (u.username or u.password):
        return m.rstrip("/")
    raise ValueError("model_upstream must be direct, subscription or an http(s) URL without credentials")


def resolve(mode: str, environ, session_id: str, harness: str | Iterable[str] | None = None) -> Upstream:
    """The upstream for `mode`, reading keys from `environ` by name. `harness` is the agents' CLI
    (--ak harness), or the set of them in a mixed team (--ak team); it only matters for subscription."""
    mode = check_mode(mode)
    harnesses = {harness} if isinstance(harness, str) else set(harness or ())
    if mode == "subscription" and harnesses == {"claude-code"}:
        return claude_subscription(environ)
    if mode == "subscription":
        up = codex_subscription(environ)
        claude_direct = environ.get(CLAUDE_ROUTE_VAR, "").strip().lower() == "direct"
        if "claude-code" in harnesses and not claude_direct:  # a mixed team: Claude Code agents on the runner's token
            claude = claude_subscription(environ)
            up.tokens += claude.tokens
            up.agent_hosts, up.claude_subscription = claude.agent_hosts, True
        # A mixed team keeps the other formats on their API keys when they are set: Antigravity on GEMINI_API_KEY, and
        # Claude Code on ANTHROPIC_API_KEY through the proxy when MULTI_AGENT_HARNESS_CLAUDE_ROUTE=direct.
        for p in ("anthropic", "gemini"):
            if p == "anthropic" and up.claude_subscription:
                continue
            if environ.get(KEY_NAMES[p]):
                up.urls[p], up.keys[p] = DIRECT[p], environ[KEY_NAMES[p]]
        return up
    keys = {p: environ[KEY_NAMES[p]] for p in PROVIDERS if environ.get(KEY_NAMES[p])}
    root = DIRECT if mode == "direct" else dict.fromkeys(PROVIDERS, mode)
    return Upstream("direct" if mode == "direct" else "custom", {p: root[p] for p in keys}, keys)


def codex_subscription(environ) -> Upstream:
    """The openai format on ChatGPT subscription logins, held on the hub (see codex_logins)."""
    logins, tokens, paths = [], [], codex_logins(environ)
    for path in paths:
        raw = path.read_bytes()
        try:
            data = json.loads(raw)
        except ValueError:
            raise ValueError(f"{path} is not JSON") from None
        tok = data.get("tokens") if isinstance(data, dict) else None
        if not isinstance(tok, dict) or not (isinstance(tok.get("access_token"), str) and tok["access_token"]
                                             and isinstance(tok.get("refresh_token"), str) and tok["refresh_token"]):
            raise ValueError(f"{path} has no ChatGPT tokens; sign in with codex login, not an API key")
        logins.append(raw)
        tokens += [v for k, v in data["tokens"].items() if k.endswith("_token") and isinstance(v, str) and v]
    return Upstream("subscription", {"openai": CHATGPT_CODEX_URL}, {}, logins=logins, login_paths=paths,
                    auth_urls=[CODEX_REFRESH_URL], tokens=tokens)


def save_refreshed_login(path: Path, uploaded: bytes, current: bytes) -> str:
    """Write the hub's copy of a login (`current`) back to its host file, since ChatGPT refresh tokens
    are single use: once the hub's proxy refreshes, the host's copy is dead. Only when the hub's copy
    changed and still holds tokens, and the host file is still what was uploaded (a new codex login
    on the host wins). Returns what happened: saved, unchanged, host-changed or invalid."""
    if current == uploaded:
        return "unchanged"
    try:
        data = json.loads(current)
        tok = data.get("tokens") if isinstance(data, dict) else None
        if not (isinstance(tok, dict) and all(isinstance(tok.get(k), str) and tok[k]
                                              for k in ("access_token", "refresh_token"))):
            return "invalid"
    except ValueError:
        return "invalid"
    try:
        if path.read_bytes() != uploaded:
            return "host-changed"
    except OSError:
        return "host-changed"
    tmp = path.with_name(path.name + ".harness-tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(current)
    tmp.replace(path)
    return "saved"


def claude_subscription(environ) -> Upstream:
    """Claude Code on the runner's own subscription token. Nothing goes through the hub: the
    proxy gets no anthropic upstream and the token only ever reaches the agent sandboxes, where
    unmodified Claude Code reads it from CLAUDE_CODE_OAUTH_TOKEN and calls Anthropic itself."""
    tok = environ.get(CLAUDE_TOKEN_VAR) or ""
    if not tok:
        raise ValueError(f"model_upstream=subscription with harness=claude-code needs {CLAUDE_TOKEN_VAR} in the "
                         "host environment: run claude setup-token and put the token in .env")
    if not re.fullmatch(r"[A-Za-z0-9._~+/=-]+", tok):
        raise ValueError(f"{CLAUDE_TOKEN_VAR} is not a token (unexpected characters)")
    return Upstream("claude-subscription", {}, {}, tokens=[tok], agent_hosts=list(CLAUDE_AGENT_HOSTS),
                    claude_subscription=True)


def codex_logins(environ) -> list[Path]:
    """The Codex auth.json files to run on: every *.json and */auth.json in the dir named by
    MULTI_AGENT_HARNESS_CODEX_LOGINS, else $CODEX_HOME/auth.json or ~/.codex/auth.json."""
    if d := environ.get(CODEX_LOGINS_VAR):
        root = Path(d).expanduser()
        paths = sorted(root.glob("*.json")) + sorted(root.glob("*/auth.json"))
        if not paths:
            raise ValueError(f"{CODEX_LOGINS_VAR}={d} holds no *.json or */auth.json")
        return paths
    home = Path(environ.get("CODEX_HOME") or Path(environ.get("HOME") or os.path.expanduser("~")) / ".codex")
    path = home.expanduser() / "auth.json"
    if not path.is_file():
        raise ValueError(f"model_upstream=subscription needs a ChatGPT login: run codex login on the host "
                         f"(no {path}), or set {CODEX_LOGINS_VAR} to a dir of auth.json files")
    return [path]

