"""Pricing table and usage parsing: provider usage blocks in, normalized tokens and USD out."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

PRICING_PATH = Path(__file__).with_name("pricing.json")
TOKEN_KINDS = ("input", "output", "cache_read", "cache_write", "cache_write_1h")
_DATE_SUFFIX = re.compile(r"-(\d{8}|\d{4}-\d{2}-\d{2})$")
_EVENT_END = re.compile(rb"\r?\n\r?\n")
_MAX_EVENT = 32 << 20  # an event bigger than this is dropped unparsed rather than buffered forever
# Only events containing one of these are JSON-parsed; text deltas pass through untouched.
_PARSE_MARKERS = (b'"usage"', b'"service_tier"', b'"error"', b'message_stop', b'response.completed',
                  b'response.incomplete', b'response.failed', b'"usageMetadata"', b'"finishReason"')


class Pricing:
    def __init__(self, table: dict):
        self.table = table

    @classmethod
    def load(cls, path: str | Path | None = None) -> Pricing:
        return cls(json.loads(Path(path or PRICING_PATH).read_text()))

    def lookup(self, provider: str, model, today: date | None = None) -> dict | None:
        """Price for a model id, also matching a dated snapshot (gpt-x-2026-01-01, claude-x-20260101).
        An entry's "from" {"YYYY-MM-DD": {rates}} holds announced price changes; the latest one
        on or before today applies."""
        if not isinstance(model, str):
            return None
        models = self.table.get(provider, {})
        price = models.get(model) or models.get(_DATE_SUFFIX.sub("", model))
        if price is None and "/" in model:  # a gateway's vendor-prefixed id, e.g. google/gemini-3.8-flash
            return self.lookup(provider, model.split("/", 1)[1], today)
        if not price or "from" not in price:
            return price
        day = (today or date.today()).isoformat()
        out = {k: v for k, v in price.items() if k != "from"}
        for start in sorted(price["from"]):
            if start <= day:
                out |= price["from"][start]
        return out


def zero_tokens() -> dict:
    return dict.fromkeys(TOKEN_KINDS, 0)


def cost(price: dict, tokens: dict, multiplier: float = 1.0) -> float:
    prompt = tokens["input"] + tokens["cache_read"] + tokens["cache_write"] + tokens["cache_write_1h"]
    tier = price.get("long_context")
    if tier and prompt > tier["above_input_tokens"]:
        price = {**price, **tier}
    rates = {
        "input": price["input"],
        "output": price["output"],
        "cache_read": price.get("cache_read", price["input"]),
        "cache_write": price.get("cache_write", price["input"]),
        "cache_write_1h": price.get("cache_write_1h", price.get("cache_write", price["input"])),
    }
    return sum(tokens[k] * rates[k] for k in TOKEN_KINDS) / 1e6 * multiplier


def _merge(acc: dict, new: dict) -> None:
    for k, v in new.items():
        if isinstance(v, dict):
            _merge(acc.setdefault(k, {}), v)
        elif v is not None:
            acc[k] = v


class Meter:
    """Collects usage from a JSON body or from SSE chunks as they pass through."""

    def __init__(self, provider: str):
        self.provider = provider
        self.usage: dict = {}
        self.service_tier: str | None = None
        self.error: str | None = None  # an error event seen inside a 200 stream
        self.done = False  # saw the stream's final event
        self._buf = b""

    def _stream_state(self, obj: dict) -> None:
        kind = obj.get("type")
        err = obj.get("error")
        if self.provider == "gemini":
            if isinstance(err, dict):
                self.error = f"{err.get('status') or err.get('code') or 'error'}: {err.get('message', '')}"
            self.done = self.done or bool(err) or any(
                isinstance(c, dict) and c.get("finishReason") for c in obj.get("candidates") or [])
            return
        if self.provider == "anthropic":
            if kind == "error" and isinstance(err, dict):
                self.error = f"{err.get('type', 'error')}: {err.get('message', '')}"
            self.done = self.done or kind in ("message_stop", "error")
            return
        if kind == "error":  # Responses API: top-level code and message
            self.error = f"{obj.get('code') or 'error'}: {obj.get('message', '')}"
        elif kind == "response.failed":
            e = (obj.get("response") or {}).get("error") or {}
            self.error = f"{e.get('code') or 'response.failed'}: {e.get('message', '')}"
        elif kind is None and isinstance(err, dict):  # chat completions error chunk
            self.error = f"{err.get('code') or err.get('type') or 'error'}: {err.get('message', '')}"
        self.done = self.done or kind in ("response.completed", "response.incomplete", "response.failed", "error")

    def observe(self, obj) -> None:
        if not isinstance(obj, dict):
            return
        if self.provider == "gemini":  # every stream chunk carries the running totals
            if isinstance(obj.get("usageMetadata"), dict):
                self.usage = obj["usageMetadata"]
            return
        if self.provider == "anthropic":
            if obj.get("type") == "message_start":
                obj = obj.get("message") or {}
            if isinstance(obj.get("usage"), dict):
                _merge(self.usage, obj["usage"])  # message_delta carries cumulative counts
            return
        resp = obj.get("response") if isinstance(obj.get("response"), dict) else obj
        if isinstance(resp.get("usage"), dict):
            self.usage = resp["usage"]
        if isinstance(resp.get("service_tier"), str):
            self.service_tier = resp["service_tier"]

    def body(self, data: bytes) -> None:
        try:
            self.observe(json.loads(data))
        except ValueError:
            pass

    def feed(self, chunk: bytes) -> None:
        self._buf += chunk
        while m := _EVENT_END.search(self._buf):
            event, self._buf = self._buf[:m.start()], self._buf[m.end():]
            self._event(event)
        if len(self._buf) > _MAX_EVENT:
            self._buf = b""

    def _event(self, raw: bytes) -> None:
        data = b"\n".join(line[5:].lstrip(b" ") for line in raw.splitlines() if line.startswith(b"data:"))
        if data.strip() == b"[DONE]":  # chat completions
            self.done = True
        elif any(m in data for m in _PARSE_MARKERS):
            try:
                obj = json.loads(data)
            except ValueError:
                return
            if isinstance(obj, dict):
                self._stream_state(obj)
                self.observe(obj)

    def tokens(self) -> dict:
        u = self.usage
        if self.provider == "gemini":  # promptTokenCount includes the cached part; thinking bills as output
            cached = u.get("cachedContentTokenCount") or 0
            prompt = (u.get("promptTokenCount") or 0) + (u.get("toolUsePromptTokenCount") or 0)
            return {"input": max(prompt - cached, 0),
                    "output": (u.get("candidatesTokenCount") or 0) + (u.get("thoughtsTokenCount") or 0),
                    "cache_read": cached, "cache_write": 0, "cache_write_1h": 0}
        if self.provider == "anthropic":
            cc = u.get("cache_creation") or {}
            w5, w1 = cc.get("ephemeral_5m_input_tokens"), cc.get("ephemeral_1h_input_tokens")
            if w5 is None and w1 is None:
                w5, w1 = u.get("cache_creation_input_tokens") or 0, 0
            return {"input": u.get("input_tokens") or 0, "output": u.get("output_tokens") or 0,
                    "cache_read": u.get("cache_read_input_tokens") or 0,
                    "cache_write": w5 or 0, "cache_write_1h": w1 or 0}
        total_in = u.get("input_tokens", u.get("prompt_tokens")) or 0
        details = u.get("input_tokens_details") or u.get("prompt_tokens_details") or {}
        cached = details.get("cached_tokens") or 0
        return {"input": max(total_in - cached, 0), "output": u.get("output_tokens", u.get("completion_tokens")) or 0,
                "cache_read": cached, "cache_write": 0, "cache_write_1h": 0}


def transcript_usage(root: str | Path | list[str | Path], pricing: Pricing | None = None) -> dict:
    """API-equivalent usage of the Claude Code session transcripts (*.jsonl) under `root` (a dir, or
    a list of dirs), priced from the table like a proxy request. Claude Code writes one line per
    content block, each with its message's usage, and a resumed session repeats earlier messages, so
    each assistant message id counts once. Shape as the proxy's /admin/usage total, plus models it
    could not price."""
    pricing = pricing or Pricing.load()
    roots = root if isinstance(root, list) else [root]
    seen: dict[str, tuple] = {}
    for path in sorted(p for r in roots for p in Path(r).rglob("*.jsonl")):
        with path.open(errors="replace") as f:
            for line in f:
                if '"usage"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                msg = obj.get("message") if isinstance(obj, dict) else None
                if not (isinstance(msg, dict) and msg.get("role") == "assistant" and isinstance(msg.get("usage"), dict)):
                    continue
                seen[msg.get("id") or obj.get("uuid") or f"{path}:{len(seen)}"] = (msg.get("model"), msg["usage"])
    tokens, usd, requests, unpriced = zero_tokens(), 0.0, 0, {}
    for model, usage in seen.values():
        m = Meter("anthropic")
        m.usage = usage
        t = m.tokens()
        if not any(t.values()):  # Claude Code's own <synthetic> messages
            continue
        requests += 1
        for k in TOKEN_KINDS:
            tokens[k] += t[k]
        price = pricing.lookup("anthropic", model)
        if price is None:
            unpriced[str(model)] = unpriced.get(str(model), 0) + 1
            continue
        fast = usage.get("speed") == "fast" and price.get("fast_multiplier")
        usd += cost(price, t, price["fast_multiplier"] if fast else 1.0)
    return {"usd": round(usd, 6), "requests": requests, "tokens": tokens, "unpriced": unpriced}
