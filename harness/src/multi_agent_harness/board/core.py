"""Board core: send, inbox and history over one message table, plus an append-only event log.

A message goes to everyone (to=None), to one named agent, or to a list of them. That is the
whole model.
Anything fancier (task boards, mentions, topology rules, push delivery) plugs in through a
Policy, event subscribers, or a plugin's register() hook rather than by growing this module.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .ratelimit import RateLimiter

AGENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
MAX_TEXT = 8000
MAX_LIMIT = 200
MAX_WAIT = 60.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (name TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL,
    inbox_cursor INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, role TEXT);
CREATE TABLE IF NOT EXISTS messages (seq INTEGER PRIMARY KEY AUTOINCREMENT, author TEXT NOT NULL,
    recipient TEXT, text TEXT NOT NULL, ts REAL NOT NULL);
"""
# recipient is NULL for a broadcast, else the recipients joined with "," (names can't hold one).
TO_ME = "instr(',' || recipient || ',', ',' || :me || ',') > 0"
# What `agent` may see: every broadcast, messages to it, and its own sends.
VISIBLE = f"(recipient IS NULL OR {TO_ME} OR author = :me)"


class BoardError(Exception):
    """A request the board refuses. The message is safe to show the agent."""


class RateLimited(BoardError):
    """An agent went over its op rate. retry_after is in seconds."""

    def __init__(self, message: str, retry_after: float):
        super().__init__(message)
        self.retry_after = retry_after


class Policy:
    """Default policy: everything is allowed.

    Subclass and override authorize() to restrict who may do what, e.g. a hierarchy
    where workers may only message leads. Raise BoardError to refuse. Actions are
    `send` (details: to, None for a broadcast, else a name or a list of names; recipients, the
    list of agents it reaches), `inbox` and `history` (details: after). board.role(name) is the
    role the harness registered an agent with, or None.

    rate_per_sec and rate_burst are a token bucket per agent over every board op
    (an inbox long-poll is one op). 0 turns it off. The server sets them from
    BOARD_RATE_PER_SEC and BOARD_RATE_BURST when those are given.
    """

    max_text = MAX_TEXT
    rate_per_sec = 5.0
    rate_burst = 30

    def authorize(self, board: "Board", agent: str, action: str, **details: Any) -> None:
        return None


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Board:
    def __init__(self, db_path: str | Path = ":memory:", log_path: str | Path | None = None,
                 policy: Policy | None = None):
        self.policy = policy or Policy()
        self._db = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()
        self._log = open(log_path, "a", buffering=1) if log_path else None
        self._subscribers: list[Callable[[dict[str, Any]], None]] = []
        self._handed: dict[str, set[int]] = {}  # agent -> seqs it got some other way (a wake prompt)
        # Event the inbox waiters sleep on, with its loop. send() swaps it out and sets it
        # thread-safely, since sync tools and REST handlers run in worker threads.
        self._wakeup: tuple[asyncio.Event, asyncio.AbstractEventLoop] | None = None
        self.limiter = RateLimiter(self.policy.rate_per_sec, self.policy.rate_burst)
        # Refusals are logged at most once per agent per second; `count` says how many a line covers.
        self._rl_lock = threading.Lock()
        self._rl_pending: dict[str, int] = {}
        self._rl_logged: dict[str, float] = {}

    # -- events ---------------------------------------------------------

    def subscribe(self, fn: Callable[[dict[str, Any]], None]) -> None:
        """Call fn with every event dict. Plugins use this for push delivery, metrics, NPCs."""
        self._subscribers.append(fn)

    def _emit(self, event: str, agent: str | None, **fields: Any) -> None:
        record = {"ts": time.time(), "event": event, "agent": agent, **fields}
        if self._log:
            with self._lock:
                self._log.write(json.dumps(record) + "\n")
        for fn in self._subscribers:
            fn(record)

    def _authorize(self, agent: str, action: str, **details: Any) -> None:
        """Ask the policy; a refusal is logged as a `refused` event before it reaches the agent."""
        try:
            self.policy.authorize(self, agent, action, **details)
        except BoardError as e:
            self._emit("refused", agent, action=action, reason=str(e), **details)
            raise

    def _spend(self, agent: str, action: str) -> None:
        """Charge one op to the agent's bucket, or refuse with RateLimited."""
        wait = self.limiter.take(agent)
        with self._rl_lock:
            count = self._rl_pending.get(agent, 0) + (1 if wait else 0)
            now = time.monotonic()
            log = count > 0 and (not wait or now - self._rl_logged.get(agent, float("-inf")) >= 1.0)
            if log:
                self._rl_logged[agent] = now
            self._rl_pending[agent] = 0 if log else count
        if log:
            self._emit("rate_limited", agent, action=action if wait else None, count=count)
        if wait:
            raise RateLimited(
                f"rate limited: over {self.limiter.rate:g} board ops/sec (burst {self.limiter.burst:g}). "
                f"Slow down and retry in {wait:.1f}s. To wait for messages, call inbox(wait_seconds=30) "
                "instead of polling in a loop.", wait)

    # -- agents ---------------------------------------------------------

    def register_agent(self, name: str, role: str | None = None) -> str:
        """Create an agent and return its token. Only the harness calls this. `role` is free
        text the harness's orchestration assigns (e.g. "lead"); policies and plugins read it."""
        if not AGENT_RE.match(name):
            raise BoardError(f"invalid agent name {name!r}")
        token = secrets.token_urlsafe(24)
        with self._lock:
            try:
                self._db.execute("INSERT INTO agents (name, token_hash, created_at, role) VALUES (?, ?, ?, ?)",
                                 (name, _hash(token), time.time(), role))
            except sqlite3.IntegrityError:
                raise BoardError(f"agent {name!r} already exists") from None
        self._emit("register", name, **({"role": role} if role else {}))
        return token

    def role(self, name: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT role FROM agents WHERE name = ?", (name,)).fetchone()
        return row[0] if row else None

    def authenticate(self, token: str | None) -> str:
        if token:
            with self._lock:
                row = self._db.execute("SELECT name FROM agents WHERE token_hash = ?", (_hash(token),)).fetchone()
            if row:
                return row[0]
        raise BoardError("unknown or missing agent token")

    def agents(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self._db.execute("SELECT name FROM agents ORDER BY name")]

    # -- messages -------------------------------------------------------

    def _recipients(self, agent: str, to: str | list[str] | None) -> list[str] | None:
        """None for a broadcast, else the named agents, deduplicated, in the order given."""
        if to is None:
            return None
        names = list(dict.fromkeys([to] if isinstance(to, str) else to))
        if not names:
            raise BoardError("to is empty; leave it out to send to everyone")
        known = set(self.agents())
        for n in names:
            if n == agent or n not in known:
                raise BoardError(f"no other agent named {n!r}; teammates are {', '.join(sorted(known))}")
        return names

    def send(self, agent: str, text: str, to: str | list[str] | None = None) -> dict[str, Any]:
        """Send to every teammate (to=None), to one named agent, or to a list of them. Returns
        the new seq. A one-name list is the same as the name."""
        self._spend(agent, "send")
        if not text or not text.strip():
            raise BoardError("empty message")
        if len(text) > self.policy.max_text:
            raise BoardError(f"message longer than {self.policy.max_text} chars")
        names = self._recipients(agent, to)
        to = None if names is None else names[0] if len(names) == 1 else names
        reach = names if names is not None else [a for a in self.agents() if a != agent]
        self._authorize(agent, "send", to=to, recipients=reach)
        return self._store_message(agent, text, names, to)

    def notify(self, text: str, to: str) -> dict[str, Any]:
        """Harness-only notification, independent of agent policy and rate limits."""
        if not text.strip() or len(text) > MAX_TEXT:
            raise BoardError("invalid notification text")
        names = self._recipients("@harness", to)
        return self._store_message("@harness", text, names, to, event="notify")

    def _store_message(self, agent: str, text: str, names: list[str] | None,
                       to: str | list[str] | None, event: str = "send") -> dict[str, Any]:
        with self._lock:
            ts = time.time()
            seq = self._db.execute("INSERT INTO messages (author, recipient, text, ts) VALUES (?, ?, ?, ?)",
                                   (agent, None if names is None else ",".join(names), text, ts)).lastrowid
            wakeup, self._wakeup = self._wakeup, None
        self._emit(event, agent, seq=seq, to=to, text=text)
        if wakeup is not None:
            event, loop = wakeup
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:  # that loop is closed; nobody is waiting on it
                pass
        return {"seq": seq}

    def _messages(self, where: str, params: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(f"SELECT seq, author, recipient, text, ts FROM messages WHERE {where} "
                                    "ORDER BY seq LIMIT :limit", {**params, "limit": limit}).fetchall()
        return [{"seq": s, "from": a, "to": r if r is None or "," not in r else r.split(","), "text": t, "ts": ts}
                for s, a, r, t, ts in rows]

    def history(self, agent: str, after: int = 0, limit: int = 50) -> list[dict[str, Any]]:
        """Everything you can see, oldest first, after seq `after`."""
        self._spend(agent, "history")
        limit = max(1, min(limit, MAX_LIMIT))
        self._authorize(agent, "history", after=after)
        msgs = self._messages(f"seq > :after AND {VISIBLE}", {"after": after, "me": agent}, limit)
        self._emit("history", agent, after=after, returned=len(msgs))
        return msgs

    def mark_handed(self, agent: str, seqs: Any) -> None:
        """Keep messages `agent` was already given outside inbox() (a plugin's wake prompt) out of its inbox."""
        with self._lock:
            self._handed.setdefault(agent, set()).update(seqs)

    def _inbox_now(self, agent: str, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            cursor = self._db.execute("SELECT inbox_cursor FROM agents WHERE name = ?", (agent,)).fetchone()[0]
            skip = {s for s in self._handed.get(agent, ()) if s > cursor}
            if agent in self._handed:
                self._handed[agent] = skip
        where = f"seq > :cursor AND author != :me AND (recipient IS NULL OR {TO_ME})"
        if skip:
            where += f" AND seq NOT IN ({','.join(str(int(s)) for s in sorted(skip))})"
        return self._messages(where, {"cursor": cursor, "me": agent}, limit)

    async def inbox(self, agent: str, wait_seconds: float = 0, limit: int = 50) -> list[dict[str, Any]]:
        """Messages to you or to everyone, from others, since your last inbox call.

        With wait_seconds > 0 (at most 60) this blocks until something arrives or the wait runs out,
        so an idle agent costs nothing while it waits.
        """
        self._spend(agent, "inbox")
        limit = max(1, min(limit, MAX_LIMIT))
        self._authorize(agent, "inbox")
        wait_seconds = max(0.0, min(float(wait_seconds), MAX_WAIT))
        deadline = time.monotonic() + wait_seconds
        loop = asyncio.get_running_loop()
        while True:
            # Take the event before checking, so a send landing in between still wakes us.
            with self._lock:
                if self._wakeup is None:
                    self._wakeup = (asyncio.Event(), loop)
                event = self._wakeup[0]
            msgs = self._inbox_now(agent, limit)
            remaining = deadline - time.monotonic()
            if msgs or remaining <= 0:
                break
            try:
                await asyncio.wait_for(event.wait(), remaining)
            except asyncio.TimeoutError:
                pass
        if msgs:
            with self._lock:
                self._db.execute("UPDATE agents SET inbox_cursor = MAX(inbox_cursor, ?) WHERE name = ?",
                                 (msgs[-1]["seq"], agent))
        self._emit("inbox", agent, wait_seconds=wait_seconds, returned=len(msgs), seqs=[m["seq"] for m in msgs])
        return msgs
