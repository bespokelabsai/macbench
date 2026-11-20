"""`keepalive`: N peers with the full task, as in `fixed`, but the run lasts until its time is up or the
team's model budget is spent, not until every agent has ended its session once.

In a `fixed` run an agent that finishes its part ends its CLI session and never comes back. In the first
16-agent run of this task, 13 of the 16 agents ended within 50 minutes of an 8 hour run, each after
handing its part to a teammate, and the run ended after 2.2 hours with $1,554 of its $2,500 unspent.

Here an agent that has ended is woken again, its CLI session resumed with a short note that gives the
time left, the team's spend and, with graded marks (the harness's --ak grade_at), the next mark:
- when a board message sent to it by name arrives that is newer than the ones it was last woken for,
  once it has been idle MESSAGE_SETTLE_SEC, or such a broadcast, once it has been idle BROADCAST_SETTLE_SEC;
- every CHECK_IN_SEC otherwise;
- and when nobody on the team is working, the agent idle longest is woken ALL_IDLE_SEC after the last
  one ended.
Nobody is woken with less than MIN_LEFT_SEC left, once the team has spent BUDGET_STOP of its budget, or
after MAX_FAILURES failed sessions in a row. The run ends when nobody is working and nobody can be
woken, or at the team timeout.

Use it with --ak orchestration=<this file>, and add keepalive_plugin.py to --ek board_plugins: that
plugin is the admin route the unread counts come from. Without it, agents are woken on the timers only.
"""

from __future__ import annotations

import logging
import math
import time
from typing import TYPE_CHECKING, Any

from multi_agent_harness.harbor.agent import PROXY_LOCAL
from multi_agent_harness.harbor.orchestration import PEER, Orchestration

if TYPE_CHECKING:
    from multi_agent_harness.harbor.agent import TeamAgent

log = logging.getLogger("multi_agent_harness.orchestrations.keepalive")

CHECK_IN_SEC = 30 * 60      # an idle agent with no new messages is woken this often
ALL_IDLE_SEC = 5 * 60       # when nobody works, the agent idle longest is woken this long after the last one ended
MESSAGE_SETTLE_SEC = 60     # an agent woken for messages to it has been idle at least this long, so they batch up
BROADCAST_SETTLE_SEC = 10 * 60  # and one woken for broadcasts only, at least this long
MIN_LEFT_SEC = 10 * 60      # nobody is woken with less time than this left
BUDGET_STOP = 0.97          # nobody is woken once the team has spent this share of its budget
POLL_SEC = 30               # how often the unread counts and the spend are read from the hub
MAX_FAILURES = 3            # an agent whose sessions failed this many times in a row is not woken again
UNREAD = "/api/admin/keepalive/unread"

RUN_NOTE = """\
How this run works: it lasts {hours}{money}, whichever comes first. It does not end when \
you finish your part. When you have nothing useful left to do, end your session as usual. You will be \
woken again, with the time left and the team's spend, when a teammate messages you, and every \
{check_in} minutes otherwise. {graded}

"""
GRADED_AT_END = "Main is graded exactly as pushed when the run ends."
# With graded marks (the harness's --ak grade_at), the manual states the schedule too.
GRADED_AT_MARKS = ("Main is graded exactly as pushed at {marks}; the grade {last} is your score, and you do not see "
                   "the earlier ones. Keep main ready to submit at all times.")

WAKE = """\
Wake-up from the harness: {left} of the run remain{money}. {why}{mark}
Read inbox() and pull main, then do the next thing that most raises the task's score. If nothing is \
worth doing now, end your session; you will be woken again."""


def board_name(team: TeamAgent, slot: str) -> str:
    """The name the board knows sandbox `slot` by. The multi-agent harness gives agents word names
    (`garnet-poppy-bouncing`) while the harness keeps its slots (`agent-3`). The unread counts are keyed by
    board name, everything else here by slot. An older harness has no names, so the two are the same."""
    facing = getattr(team, "facing", None)
    return facing(slot) if callable(facing) else slot


def mark_words(sec: float) -> str:
    """'hour 4', 'minute 8': how a mark is named (as the harness's marks.words)."""
    if sec % 3600 == 0:
        return f"hour {int(sec // 3600)}"
    if sec % 60 == 0:
        return f"minute {int(sec // 60)}"
    return f"second {sec:g}"


def graded_note(team: TeamAgent) -> str:
    """The run note's sentence on grading: at the end only, or at each graded mark."""
    plan = getattr(team, "mark_plan", None) or []
    if not plan:
        return GRADED_AT_END
    names = [mark_words(m["mark_s"]) for m in plan]
    units = {n.split()[0] for n in names}
    if len(units) == 1 and len(names) > 1:   # hours 1, 2, 4, 8 and 16
        nums = [n.split()[1] for n in names]
        at = f"{units.pop()}s " + ", ".join(nums[:-1]) + " and " + nums[-1]
    else:
        at = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    if plan[-1]["final"]:
        return GRADED_AT_MARKS.format(marks=at, last=f"at {names[-1]}, when the run ends,")
    return GRADED_AT_MARKS.format(marks=f"{at}, and when the run ends", last="when the run ends")


def next_mark_note(team: TeamAgent) -> str:
    """' The next grading of main is at hour 4, in 1 h 12 min.' for a wake-up note, or ''."""
    nxt = getattr(team, "next_mark", None)
    m = nxt() if callable(nxt) else None
    if not m:
        return ""
    what = "The final grading of main" if m["final"] else "The next grading of main"
    return f" {what} is at {mark_words(m['mark_s'])} of the run, in {duration(m['in_s'])}."


def duration(sec: float) -> str:
    m = max(0, int(sec // 60))
    h, m = divmod(m, 60)
    if h and m:
        return f"{h} h {m} min"
    return f"{h} h" if h else f"{m} min"


class KeepAlive(Orchestration):
    name = "keepalive"
    # Claude Code's Task/Agent tool, Codex's multi_agent and agy's subagents stay off: a team of k is k agents.
    cli_subagents = False

    def __init__(self) -> None:
        self.clock = time.monotonic
        self.t0: float | None = None
        self.idle_since: dict[str, float] = {}
        self.all_idle_since: float | None = None
        self.failures: dict[str, int] = {}
        self.unread: dict[str, dict[str, int]] = {}  # agent -> n, direct, latest, latest_direct (see keepalive_plugin.py)
        self.woken_for: dict[str, int] = {}   # the newest seq an agent was last woken for
        self.spent = 0.0
        self.budget: float | None = None
        self.last_poll = -math.inf
        self.plugin = True
        self.timeline: list[dict[str, Any]] = []

    def roles(self, agents: list[str]) -> dict[str, str]:
        return {a: PEER for a in agents}

    def prompt(self, team: TeamAgent, name: str, instruction: str) -> str | None:
        hours = duration(float(team.timeout_sec)) if team.timeout_sec else "until it is stopped"
        money = (f" or until the team's budget for model calls (${self.budget:,.0f} at API prices) is spent"
                 if self.budget else "")
        note = RUN_NOTE.format(hours=hours, money=money, check_in=CHECK_IN_SEC // 60, graded=graded_note(team))
        head, sep, _ = team.preamble(name).rpartition("Task:\n")
        return head + note + sep + instruction

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
        if team.running:
            return None
        if reason := self._blocked(team):
            return reason
        if team.agents and all(self.failures.get(a, 0) >= MAX_FAILURES for a in team.agents):
            return "all_failing"
        return None

    async def start(self, team: TeamAgent) -> None:
        self.t0 = self.clock()
        await self._poll(team)

    @staticmethod
    def _as(team: TeamAgent, slot: str) -> dict[str, str]:
        """{"as": board name} for the timeline when it differs from the slot, so a replay can match the
        board's events to the slots' wakes."""
        name = board_name(team, slot)
        return {"as": name} if name != slot else {}

    def _note(self, op: str, name: str | None, **extra: Any) -> None:
        t = 0.0 if self.t0 is None else self.clock() - self.t0
        self.timeline.append({"t": round(t, 1), "op": op, "name": name, **extra})

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
        ended = False
        for a in team.agents:
            if a in team.running or a in self.idle_since:
                continue
            self.idle_since[a] = now
            ended = True
            code = team.exits.get(a)
            self.failures[a] = 0 if code == 0 else self.failures.get(a, 0) + 1
            self._note("end", a, exit=code, **self._as(team, a))
        if team.running:
            self.all_idle_since = None
        elif self.all_idle_since is None:
            self.all_idle_since = now
        if ended or now - self.last_poll >= POLL_SEC:
            await self._poll(team)
        if self._blocked(team):
            return
        nobody = not team.running
        # An agent can end while _poll awaits, after the loop above; it counts as idle from now.
        idle = sorted((a for a in team.agents if a not in team.running and self.failures.get(a, 0) < MAX_FAILURES),
                      key=lambda a: self.idle_since.get(a, now))
        woke = 0
        for a in idle:
            since = now - self.idle_since.get(a, now)
            u, seen = self.unread.get(board_name(team, a), {}), self.woken_for.get(a, 0)
            n = u.get("n", 0)
            to_me = u.get("latest_direct", 0) > seen and since >= MESSAGE_SETTLE_SEC
            if to_me or (u.get("latest", 0) > seen and since >= BROADCAST_SETTLE_SEC):
                d = u.get("direct", 0)
                kind = "messages" if to_me else "broadcasts"
                why = (f"{n} new board message{'s are' if n != 1 else ' is'} waiting for you"
                       + (f", {d} of them sent to you by name." if d else ", sent to everyone."))
            elif since >= CHECK_IN_SEC:
                kind, why = "check_in", "This is a regular check-in."
            elif nobody and not woke and self.all_idle_since is not None and now - self.all_idle_since >= ALL_IDLE_SEC:
                kind, why = "all_idle", "Nobody on the team is working right now."
            else:
                continue
            if self._wake(team, a, kind, why):
                woke += 1

    def _wake(self, team: TeamAgent, name: str, kind: str, why: str) -> bool:
        money = (f", and the team has spent ${self.spent:,.0f} of its ${self.budget:,.0f} budget for model calls"
                 if self.budget else "")
        text = WAKE.format(left=duration(self._left(team)) if self._left(team) < math.inf else "an open-ended time",
                           money=money, why=why, mark=next_mark_note(team))
        try:
            team.launch(name, text, resume=True)
        except Exception as e:
            log.warning("keepalive: could not wake %s: %s: %s", name, type(e).__name__, e)
            self.failures[name] = MAX_FAILURES
            self._note("wake_failed", name, error=type(e).__name__)
            return False
        self.idle_since.pop(name, None)
        self.all_idle_since = None
        self.woken_for[name] = self.unread.get(board_name(team, name), {}).get("latest", 0)
        self._note("wake", name, kind=kind, spent=round(self.spent, 2), **self._as(team, name))
        return True

    def record(self, team: TeamAgent) -> dict[str, Any]:
        wakes = [e for e in self.timeline if e["op"] == "wake"]
        counts = {"wakes": len(wakes), **{k: sum(e.get("kind") == k for e in wakes) for k in ("messages", "broadcasts", "check_in", "all_idle")},
                  "failed_wakes": sum(e["op"] == "wake_failed" for e in self.timeline), "spent_usd": round(self.spent, 2)}
        return {"metadata": {"keepalive": {**counts, "timeline": self.timeline}}, "ledger": {"keepalive": counts}}
