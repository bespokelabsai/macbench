"""`keepalive` for spectrum-to-molecule: N peers with the full task, as in the harness's `fixed`, but the run lasts
until the agent timeout instead of until every agent has ended its CLI session once.

In our smoke runs every agent ended its own session after 15 to 67 minutes of a 4 h day. Here an agent that has
ended is woken again: its CLI session is resumed (codex exec resume / claude --resume) with a short note.

When an agent is woken. It must not be running, must have had fewer than MAX_WAKES wakes, and its last wake must
be at least MIN_GAP_SEC ago. Then, first match wins:
- messages: a board message sent to it by name, newer than any it was last woken for, once it has been idle
  MESSAGE_SETTLE_SEC (so a burst batches up);
- broadcasts: such a message to everyone, once idle BROADCAST_SETTLE_SEC;
- check_in: idle IDLE_SEC with nothing new;
- all_idle: nobody on the team is working; the agent idle longest (one per tick) is woken ALL_IDLE_SEC after the
  team went idle.
Nobody is woken with less than STOP_WAKING_SEC of the agent timeout left, once the team has spent BUDGET_STOP of
its model budget (--ek budget_usd, metered at API prices even on subscription), or after MAX_FAILURES failed
sessions in a row.

How the run ends:
- deadline: END_MARGIN_SEC before the end of the agents' time, whoever is still working is stopped. The harness
  then stops their CLIs (TERM, KILL after 30 s; their sessions and logs are still collected), takes its final
  checkpoint, saves the team repo, checks main out for the verifier and collects the logs. That end of the run
  must finish inside Harbor's own agent limit, and it takes from 3 min to over 30 on a big team repo, so the end
  of the agents' time comes from the harness (team.time_left()): --ak timeout_sec after the agents started, or
  earlier, early enough for the end of the run to fit before Harbor's limit. A harness without time_left() gives
  only --ak timeout_sec from this strategy's start, and then END_MARGIN_SEC alone has to cover the end of the run.
- earlier, when nobody is working and nobody can be woken again: time_up (under STOP_WAKING_SEC left),
  budget_spent, wakes_used (every agent at MAX_WAKES), all_failing.

The agent timeout is --ak timeout_sec, else the task's [agent] timeout_sec. On a resume from a checkpoint the
harness sets timeout_sec to the time the checkpoint had left, and this strategy starts fresh: its clock starts
at the resume and the wake counts start at 0 again.

Logging. Every wake and session end goes to (1) the board log, as a `keepalive` event through
keepalive_plugin.py's admin route, (2) <trial dir>/keepalive.jsonl, written live from the host, with the exact
text of each wake, and (3) the agent metadata and sandbox ledger at the end (record()).

Settings: host environment variables, read when harbor starts. Defaults are for a 4 h run:
  KEEPALIVE_MAX_WAKES=16          wakes per agent
  KEEPALIVE_MIN_GAP_SEC=600       between two wakes of the same agent
  KEEPALIVE_IDLE_SEC=600          idle this long with nothing new: check-in
  KEEPALIVE_MESSAGE_SETTLE_SEC=60
  KEEPALIVE_BROADCAST_SETTLE_SEC=300
  KEEPALIVE_ALL_IDLE_SEC=120
  KEEPALIVE_STOP_WAKING_SEC=600   no wakes in the last 10 min
  KEEPALIVE_END_MARGIN_SEC=180    the run ends this long before the agent timeout
  KEEPALIVE_BUDGET_STOP=0.97

Use it with --ak orchestration=environment/hub/keepalive.py and --ek board_plugins=environment/hub/keepalive_plugin.py
(paths from task/). Without the plugin agents are woken on the timers only, and wakes are logged to keepalive.jsonl
and the metadata but not the board log. The first prompt is the harness's standard one, as in `fixed`.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from multi_agent_harness.harbor.agent import PROXY_LOCAL
from multi_agent_harness.harbor.orchestration import PEER, Orchestration

if TYPE_CHECKING:
    from multi_agent_harness.harbor.agent import TeamAgent

log = logging.getLogger("multi_agent_harness.orchestrations.keepalive")


def _env(name: str, default: float) -> float:
    raw = os.environ.get(f"KEEPALIVE_{name}", "").strip()
    return float(raw) if raw else default


POLL_SEC = 30           # how often the unread counts and the spend are read from the hub
MAX_FAILURES = 3        # an agent whose sessions failed this many times in a row is not woken again
UNREAD = "/api/admin/keepalive/unread"
EVENT = "/api/admin/keepalive/event"
KINDS = ("messages", "broadcasts", "check_in", "all_idle")

# What a woken agent reads. "the end of the day" matches the task instruction ("we'll use whatever is on `main`
# at the end of the day").
WAKE = ("{news}It's not the end of the day yet; there are about {left} left. If you can still make the "
        "identifications more accurate, keep going. Check the board for what your teammates are doing.")
NEWS_DIRECT = "You have new messages on the board. "
NEWS_BROADCAST = "There are new messages on the board. "


def board_name(team: TeamAgent, slot: str) -> str:
    """The name the board knows sandbox `slot` by (word names; the harness keeps slots `agent-N`)."""
    facing = getattr(team, "facing", None)
    return facing(slot) if callable(facing) else slot


def duration(sec: float) -> str:
    m = max(1, int(round(sec / 60)))
    h, m = divmod(m, 60)
    if h and m:
        return f"{h} h {m} min"
    return f"{h} h" if h else f"{m} min"


class KeepAlive(Orchestration):
    name = "keepalive"

    def __init__(self) -> None:
        self.max_wakes = int(_env("MAX_WAKES", 16))
        self.min_gap = _env("MIN_GAP_SEC", 600)
        self.idle_sec = _env("IDLE_SEC", 600)
        self.message_settle = _env("MESSAGE_SETTLE_SEC", 60)
        self.broadcast_settle = _env("BROADCAST_SETTLE_SEC", 300)
        self.all_idle_sec = _env("ALL_IDLE_SEC", 120)
        self.stop_waking = _env("STOP_WAKING_SEC", 600)
        self.end_margin = _env("END_MARGIN_SEC", 180)
        self.budget_stop = _env("BUDGET_STOP", 0.97)
        self.clock = time.monotonic
        self.t0: float | None = None
        self.timeout: float | None = None
        self.team_left = None  # the harness's TeamAgent.time_left, when it has one
        self.idle_since: dict[str, float] = {}
        self.all_idle_since: float | None = None
        self.failures: dict[str, int] = {}
        self.wakes: dict[str, int] = {}
        self.last_wake: dict[str, float] = {}
        self.unread: dict[str, dict[str, int]] = {}  # board name -> n, direct, latest, latest_direct
        self.woken_for: dict[str, int] = {}   # slot -> the newest seq it was last woken for
        self.spent = 0.0
        self.budget: float | None = None
        self.last_poll = -math.inf
        self.plugin = True
        self.timeline: list[dict[str, Any]] = []
        self.log_path: Path | None = None

    def settings(self) -> dict[str, float]:
        return {"max_wakes": self.max_wakes, "min_gap_sec": self.min_gap, "idle_sec": self.idle_sec,
                "message_settle_sec": self.message_settle, "broadcast_settle_sec": self.broadcast_settle,
                "all_idle_sec": self.all_idle_sec, "stop_waking_sec": self.stop_waking,
                "end_margin_sec": self.end_margin, "budget_stop": self.budget_stop}

    def roles(self, agents: list[str]) -> dict[str, str]:
        return {a: PEER for a in agents}

    def prompt(self, team: TeamAgent, name: str, instruction: str) -> str | None:
        return team.preamble(name) + instruction

    # -- time and limits ---------------------------------------------------

    def _left(self) -> float:
        """Seconds until the end of the agents' time: the harness's own count when it gives one, which ends early
        enough for the end of the run to fit inside Harbor's agent limit; else timeout from this strategy's start."""
        if self.team_left is not None:
            left = self.team_left()
            if left is not None:
                return left
        if not self.timeout or self.t0 is None:
            return math.inf
        return self.timeout - (self.clock() - self.t0)

    def _blocked(self) -> str | None:
        if self._left() < self.stop_waking:
            return "time_up"
        if self.budget and self.spent >= self.budget_stop * self.budget:
            return "budget_spent"
        return None

    def _wakeable(self, a: str) -> bool:
        return self.failures.get(a, 0) < MAX_FAILURES and self.wakes.get(a, 0) < self.max_wakes

    def end_reason(self, team: TeamAgent) -> str | None:
        if self._left() <= self.end_margin:
            return "deadline"
        if team.running:
            return None
        if reason := self._blocked():
            return reason
        if team.agents and not any(self._wakeable(a) for a in team.agents):
            return ("all_failing" if all(self.failures.get(a, 0) >= MAX_FAILURES for a in team.agents)
                    else "wakes_used")
        return None

    # -- lifecycle ---------------------------------------------------------

    async def start(self, team: TeamAgent) -> None:
        self.t0 = self.clock()
        self.timeout = float(team.timeout_sec) if team.timeout_sec else (
            getattr(getattr(team, "_env", None), "team_task_agent_timeout_sec", None))
        left = getattr(team, "time_left", None)
        self.team_left = left if callable(left) else None
        logs = getattr(team, "logs_dir", None)
        if logs is not None:
            self.log_path = Path(logs).parent / "keepalive.jsonl"
        left = self._left()
        self._note("start", None, timeout_sec=self.timeout, left_s=round(left) if left < math.inf else None,
                   **self.settings())
        await self._poll(team)

    def _note(self, op: str, name: str | None, **extra: Any) -> dict[str, Any]:
        t = 0.0 if self.t0 is None else self.clock() - self.t0
        rec = {"t": round(t, 1), "op": op, "name": name, **extra}
        self.timeline.append(rec)
        if self.log_path is not None:
            try:
                with self.log_path.open("a") as f:
                    f.write(json.dumps({"ts": time.time(), **rec}) + "\n")
            except OSError as e:
                log.warning("keepalive: can't write %s: %s", self.log_path, e)
        return rec

    async def _board_event(self, team: TeamAgent, rec: dict[str, Any]) -> None:
        if not self.plugin:
            return
        body = {k: v for k, v in rec.items() if k != "name"}
        body["agent"] = rec.get("as") or rec.get("name")
        try:
            await team.board_admin("POST", EVENT, body)
        except Exception as e:  # logging only; the wake has happened
            log.warning("keepalive: board event failed: %s: %s", type(e).__name__, e)

    async def _poll(self, team: TeamAgent) -> None:
        self.last_poll = self.clock()
        try:
            usage = await team._hub_admin(team._env, "proxy", "GET", f"{PROXY_LOCAL}/admin/usage")
            self.spent = float(((usage or {}).get("total") or {}).get("usd") or 0.0)
            budget = (usage or {}).get("budget_usd")
            self.budget = float(budget) if budget else None
        except Exception as e:  # a hub hiccup; the next poll tries again
            log.warning("keepalive: usage unavailable: %s: %s", type(e).__name__, e)
        if not self.plugin:
            return
        try:
            got = await team.board_admin("GET", UNREAD)
            self.unread = {a: {k: int(v.get(k) or 0) for k in ("n", "direct", "latest", "latest_direct")}
                           for a, v in ((got or {}).get("unread") or {}).items()}
        except Exception as e:
            if "404" in str(e):  # keepalive_plugin.py is not loaded: timers only from now on
                self.plugin = False
                log.warning("keepalive: %s is missing, so agents are woken on the timers only", UNREAD)
            else:
                log.warning("keepalive: unread counts unavailable: %s: %s", type(e).__name__, e)

    async def tick(self, team: TeamAgent) -> None:
        now = self.clock()
        ended = []
        for a in team.agents:
            if a in team.running or a in self.idle_since:
                continue
            self.idle_since[a] = now
            code = team.exits.get(a)
            self.failures[a] = 0 if code == 0 else self.failures.get(a, 0) + 1
            ended.append(self._note("end", a, exit=code, session=self.wakes.get(a, 0) + 1,
                                    **self._as(team, a)))
        if team.running:
            self.all_idle_since = None
        elif self.all_idle_since is None:
            self.all_idle_since = now
        if ended or now - self.last_poll >= POLL_SEC:
            await self._poll(team)
        for rec in ended:
            await self._board_event(team, rec)
        if self._blocked() or self._left() <= self.end_margin:
            return
        nobody = not team.running
        idle = sorted((a for a in team.agents if a not in team.running and self._wakeable(a)),
                      key=lambda a: self.idle_since.get(a, now))
        woke = 0
        for a in idle:
            if now - self.last_wake.get(a, -math.inf) < self.min_gap:
                continue
            since = now - self.idle_since.get(a, now)
            u, seen = self.unread.get(board_name(team, a), {}), self.woken_for.get(a, 0)
            to_me = u.get("latest_direct", 0) > seen and since >= self.message_settle
            if to_me or (u.get("latest", 0) > seen and since >= self.broadcast_settle):
                kind, news = ("messages", NEWS_DIRECT) if to_me else ("broadcasts", NEWS_BROADCAST)
            elif since >= self.idle_sec:
                kind, news = "check_in", ""
            elif (nobody and not woke and self.all_idle_since is not None
                  and now - self.all_idle_since >= self.all_idle_sec):
                kind, news = "all_idle", ""
            else:
                continue
            if await self._wake(team, a, kind, news):
                woke += 1

    @staticmethod
    def _as(team: TeamAgent, slot: str) -> dict[str, str]:
        name = board_name(team, slot)
        return {"as": name} if name != slot else {}

    async def _wake(self, team: TeamAgent, name: str, kind: str, news: str) -> bool:
        left = self._left()
        until_end = left - self.end_margin   # the run ends end_margin before the agent timeout
        text = WAKE.format(news=news, left=duration(until_end) if left < math.inf else "a few hours")
        try:
            team.launch(name, text, resume=True)
        except Exception as e:
            log.warning("keepalive: could not wake %s: %s: %s", name, type(e).__name__, e)
            self.failures[name] = MAX_FAILURES
            rec = self._note("wake_failed", name, error=f"{type(e).__name__}: {e}"[:300], **self._as(team, name))
            await self._board_event(team, rec)
            return False
        now = self.clock()
        self.idle_since.pop(name, None)
        self.all_idle_since = None
        self.last_wake[name] = now
        self.wakes[name] = self.wakes.get(name, 0) + 1
        self.woken_for[name] = self.unread.get(board_name(team, name), {}).get("latest", 0)
        rec = self._note("wake", name, kind=kind, n=self.wakes[name], left_s=round(left),
                         spent=round(self.spent, 2), text=text, **self._as(team, name))
        log.info("keepalive: woke %s (%s, wake %d of %d, %s left)", name, kind, self.wakes[name],
                 self.max_wakes, duration(left) if left < math.inf else "?")
        await self._board_event(team, rec)
        return True

    def record(self, team: TeamAgent) -> dict[str, Any]:
        wakes = [e for e in self.timeline if e["op"] == "wake"]
        counts = {"wakes": len(wakes), **{k: sum(e.get("kind") == k for e in wakes) for k in KINDS},
                  "per_agent": {a: self.wakes.get(a, 0) for a in team.agents},
                  "failed_wakes": sum(e["op"] == "wake_failed" for e in self.timeline),
                  "spent_usd": round(self.spent, 2), "settings": self.settings()}
        self._note("record", None, end_reason=getattr(team, "end_reason", None),
                   **{k: v for k, v in counts.items() if k != "settings"})
        return {"metadata": {"keepalive": {**counts, "timeline": self.timeline}}, "ledger": {"keepalive": counts}}
