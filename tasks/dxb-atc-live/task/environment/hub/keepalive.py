"""`keepalive`: N peers with the full task. The run lasts until its time is up, not until every agent has ended
its session once, because the airport keeps running whether anyone is on watch or not.

An agent that ends its session is woken again, its session resumed with a note giving the time left:
- when a board message sent to it by name arrives, once it has been idle MESSAGE_SETTLE_SEC;
- otherwise every CHECK_IN_SEC.
Nobody is woken with less than MIN_LEFT_SEC left, once the team has spent BUDGET_STOP of the run's budget, or
after MAX_FAILURES failed sessions in a row. The run ends when nobody is working and nobody can be woken, or
at the team timeout, and the day is frozen then.

Use it with --ak orchestration=<this file>, and add plugins/keepalive_plugin.py to --ek board_plugins: that
plugin is the admin route the unread counts come from. Without it, agents are woken on the timer only.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import TYPE_CHECKING, Any

from multi_agent_harness.harbor.agent import PROXY_LOCAL
from multi_agent_harness.harbor.orchestration import PEER, Orchestration

if TYPE_CHECKING:
    from multi_agent_harness.harbor.agent import TeamAgent

log = logging.getLogger("multi_agent_harness.orchestrations.keepalive")

FREEZE_BEFORE_END_SEC = 10  # the last tick before the agents' time is up
CHECK_IN_SEC = 2 * 60       # an idle agent is woken this often
MESSAGE_SETTLE_SEC = 60     # an agent woken for messages to it has been idle at least this long, so they batch up
MIN_LEFT_SEC = 3 * 60       # nobody is woken with less time than this left
BUDGET_STOP = 0.97          # nobody is woken once the team has spent this share of the run's budget
POLL_SEC = 10               # how often the unread counts and the spend are read from the hub
MAX_FAILURES = 3            # an agent whose sessions failed this many times in a row is not woken again
STALL_SEC = 15 * 60         # a running session that makes no model call this long has ended its turn: it is ended
UNREAD = "/api/admin/keepalive/unread"
FREEZE = "/api/admin/atc/freeze"

RUN_NOTE = ("The run lasts {length}. The airport keeps running while you are away. "
            "When you end your session, you will be woken again.\n\n")
WAKE = "{left} of the run remain."


def board_name(team: TeamAgent, slot: str) -> str:
    """The name the board knows sandbox `slot` by (agents have word names, the harness keeps slots)."""
    facing = getattr(team, "facing", None)
    return facing(slot) if callable(facing) else slot


def duration(sec: float) -> str:
    m = max(0, int(sec // 60))
    h, m = divmod(m, 60)
    if h and m:
        return f"{h} h {m} min"
    return f"{h} h" if h else f"{m} min"


def run_length(sec: float) -> str:
    """'4 hours', '1 hour', or as duration() when not a whole number of hours."""
    h, rest = divmod(int(sec), 3600)
    if h and not rest:
        return f"{h} hour" + ("s" if h != 1 else "")
    return duration(sec)


class KeepAlive(Orchestration):
    name = "keepalive"
    cli_subagents = False   # the team is the k agents: the CLIs' own subagents are off

    def __init__(self) -> None:
        self.clock = time.monotonic
        self.t0: float | None = None
        self.idle_since: dict[str, float] = {}
        self.failures: dict[str, int] = {}
        self.unread: dict[str, dict[str, int]] = {}  # agent -> n, direct, latest, latest_direct (keepalive_plugin.py)
        self.woken_for: dict[str, int] = {}   # the newest seq an agent was last woken for
        self.spent = 0.0
        self.budget: float | None = None
        self.last_poll = -math.inf
        self.plugin = True
        self.frozen = False
        self.timeline: list[dict[str, Any]] = []
        self.calls_seen: dict[str, tuple[int, float]] = {}  # slot -> (model requests so far, when that count changed)
        self.stall_stopped: set[str] = set()       # slots whose silent session this orchestration ended

    def roles(self, agents: list[str]) -> dict[str, str]:
        return {a: PEER for a in agents}

    def prompt(self, team: TeamAgent, name: str, instruction: str) -> str | None:
        length = run_length(float(team.timeout_sec)) if team.timeout_sec else "until it is stopped"
        head, sep, _ = team.preamble(name).rpartition("Task:\n")
        return head + RUN_NOTE.format(length=length) + sep + instruction

    def _left(self, team: TeamAgent) -> float:
        if not team.timeout_sec or self.t0 is None:
            return math.inf
        return float(team.timeout_sec) - (self.clock() - self.t0)

    def _blocked(self, team: TeamAgent) -> str | None:
        if self._left(team) < MIN_LEFT_SEC:
            return "time_up"
        if self.budget and self.spent >= BUDGET_STOP * self.budget:
            return "budget_spent"
        return None

    def end_reason(self, team: TeamAgent) -> str | None:
        reason = self._end_reason(team)
        if reason and not self.frozen:
            # The run is over: stop the day now. end_reason is not async, and run() goes on awaiting after it.
            asyncio.ensure_future(self._freeze(team))
        return reason

    def _end_reason(self, team: TeamAgent) -> str | None:
        if team.running:
            return None
        if reason := self._blocked(team):
            return reason
        if team.agents and all(self.failures.get(a, 0) >= MAX_FAILURES for a in team.agents):
            return "all_failing"
        return None

    async def _freeze(self, team: TeamAgent) -> None:
        """Stop the day when the agents' time ends (plugins/atc_plugin.py)."""
        if self.frozen:
            return
        self.frozen = True
        try:
            await team.board_admin("POST", FREEZE, {})
            log.info("day frozen")
        except Exception as e:  # noqa: BLE001 - details.json shows frozen_at is missing; the run goes on
            log.warning("could not freeze the day: %s", type(e).__name__)

    async def start(self, team: TeamAgent) -> None:
        self.t0 = self.clock()
        await self._poll(team)

    @staticmethod
    def _as(team: TeamAgent, slot: str) -> dict[str, str]:
        """{"as": board name} for the timeline when it differs from the slot."""
        name = board_name(team, slot)
        return {"as": name} if name != slot else {}

    def _note(self, op: str, name: str | None, **extra: Any) -> None:
        t = 0.0 if self.t0 is None else self.clock() - self.t0
        self.timeline.append({"t": round(t, 1), "op": op, "name": name, **extra})

    async def _end_silent(self, team: TeamAgent, agents: dict) -> None:
        """End a session that makes no model call for STALL_SEC. Claude Code keeps its process alive while
        background tasks it started run, after its turn has ended, so without this the agent counts as busy and
        is never woken again."""
        now = self.clock()
        for a in list(team.running):
            if board_name(team, a) not in agents:  # no proxy record (e.g., a subscription login): nothing to judge
                continue
            n = int((agents.get(board_name(team, a)) or {}).get("requests") or 0)
            seen = self.calls_seen.get(a)
            if seen is None or seen[0] != n:
                self.calls_seen[a] = (n, now)
            elif now - seen[1] >= STALL_SEC and a not in self.stall_stopped:
                self.stall_stopped.add(a)
                self._note("silent_end", a, quiet_s=round(now - seen[1]), **self._as(team, a))
                log.info("keepalive: %s made no model call for %.0f s; ending its session", a, now - seen[1])
                asyncio.ensure_future(team.stop(a))  # TERM, then KILL after a grace period: do not hold the tick
        for a in list(self.calls_seen):
            if a not in team.running:
                self.calls_seen.pop(a, None)

    async def _poll(self, team: TeamAgent) -> None:
        self.last_poll = self.clock()
        try:
            usage = await team._hub_admin(team._env, "proxy", "GET", f"{PROXY_LOCAL}/admin/usage")
            self.spent = float(((usage or {}).get("total") or {}).get("usd") or 0.0)
            budget = (usage or {}).get("budget_usd")
            self.budget = float(budget) if budget else None
            await self._end_silent(team, (usage or {}).get("agents") or {})
        except Exception as e:  # a hub hiccup; the next poll tries again
            log.warning("keepalive: usage unavailable: %s: %s", type(e).__name__, e)
        if not self.plugin:
            return
        try:
            got = await team.board_admin("GET", UNREAD)
            self.unread = {a: {k: int(v.get(k) or 0) for k in ("n", "direct", "latest", "latest_direct")}
                           for a, v in ((got or {}).get("unread") or {}).items()}
        except Exception as e:
            if "404" in str(e):  # keepalive_plugin.py is not loaded: the timer only from now on
                self.plugin = False
                log.warning("keepalive: %s is missing, so agents are woken on the timer only", UNREAD)
            else:
                log.warning("keepalive: unread counts unavailable: %s: %s", type(e).__name__, e)

    async def tick(self, team: TeamAgent) -> None:
        now = self.clock()
        if self._left(team) <= FREEZE_BEFORE_END_SEC:
            await self._freeze(team)
        ended = False
        for a in team.agents:
            if a in team.running or a in self.idle_since:
                continue
            self.idle_since[a] = now
            ended = True
            code = team.exits.get(a)
            # a session ended here for silence finished its turn first; its exit code is not a failure
            stalled = a in self.stall_stopped
            self.stall_stopped.discard(a)
            self.failures[a] = 0 if code == 0 or stalled else self.failures.get(a, 0) + 1
            self._note("end", a, exit=code, **self._as(team, a))
        if ended or now - self.last_poll >= POLL_SEC:
            await self._poll(team)
        if self._blocked(team):
            return
        # An agent can end while _poll awaits, after the loop above; it counts as idle from now.
        idle = sorted((a for a in team.agents if a not in team.running and self.failures.get(a, 0) < MAX_FAILURES),
                      key=lambda a: self.idle_since.get(a, now))
        for a in idle:
            since = now - self.idle_since.get(a, now)
            u = self.unread.get(board_name(team, a), {})
            if u.get("latest_direct", 0) > self.woken_for.get(a, 0) and since >= MESSAGE_SETTLE_SEC:
                kind = "messages"
            elif since >= CHECK_IN_SEC:
                kind = "check_in"
            else:
                continue
            self._wake(team, a, kind)

    def _wake(self, team: TeamAgent, name: str, kind: str) -> bool:
        left = self._left(team)
        text = WAKE.format(left=duration(left)) if left < math.inf else "You have been woken again."
        try:
            team.launch(name, text, resume=True)
        except Exception as e:
            log.warning("keepalive: could not wake %s: %s: %s", name, type(e).__name__, e)
            self.failures[name] = MAX_FAILURES
            self._note("wake_failed", name, error=type(e).__name__)
            return False
        self.idle_since.pop(name, None)
        self.woken_for[name] = self.unread.get(board_name(team, name), {}).get("latest", 0)
        self._note("wake", name, kind=kind, **self._as(team, name))
        return True

    def record(self, team: TeamAgent) -> dict[str, Any]:
        wakes = [e for e in self.timeline if e["op"] == "wake"]
        counts = {"wakes": len(wakes), **{k: sum(e.get("kind") == k for e in wakes) for k in ("messages", "check_in")},
                  "failed_wakes": sum(e["op"] == "wake_failed" for e in self.timeline), "spent_usd": round(self.spent, 2)}
        return {"metadata": {"keepalive": {**counts, "timeline": self.timeline}}, "ledger": {"keepalive": counts}}
