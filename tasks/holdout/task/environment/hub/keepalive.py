"""`keepalive` for Holdout (CLI subagents off), with the live-game changes: idle
agents are checked in on every few minutes, and the world is frozen (POST /api/admin/game/freeze) 30 s before the
team's time ends, so every mark and the final grade read a still world.

With FINISH_IS_FINAL (on) nobody is woken. An agent that ends its session cleanly has finished, and the run ends
when every agent has finished or the time is up: the world is frozen, then the final grade is read. A session that crashes has not finished: it is resumed, up to MAX_FAILURES times in a row, after RETRY_SEC.
The wake-up machinery below stays, switched off, so the old behaviour is one constant away.

`keepalive`: N peers with the full task, as in `fixed`, but the run lasts until its time is up or the
team's model budget is spent, not until every agent has ended its session once.

In a `fixed` run an agent that finishes its part ends its CLI session and never comes back. In the first
16-agent run of this task, 13 of the 16 agents ended within 50 minutes of an 8 hour run, each after
handing its part to a teammate, and the run ended after 2.2 hours with $1,554 of its $2,500 unspent.

Here an agent that has ended is woken again, its CLI session resumed with a short note that gives the
time left (Holdout never mentions model spend to the players) and, with graded marks (the harness's --ak grade_at), the next mark:
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

FINISH_IS_FINAL = True      # an agent that ends its session has finished; it is not woken again
DEAD = "/api/admin/game/dead"   # ...unless its survivor was dead then: a dead agent comes back at its respawn, so it is woken
RETRY_SEC = 30              # a crashed session is resumed this long after it ended
CHECK_IN_SEC = 3 * 60       # Holdout is live: an idle survivor's agent is woken this often
ALL_IDLE_SEC = 60           # when nobody works, the agent idle longest is woken this long after the last one ended
MESSAGE_SETTLE_SEC = 60     # an agent woken for messages to it has been idle at least this long, so they batch up
BROADCAST_SETTLE_SEC = 10 * 60  # and one woken for broadcasts only, at least this long
MIN_LEFT_SEC = 2 * 60       # nobody is woken with less time than this left
FREEZE_LEFT_SEC = 30         # the world is frozen this long before the team's time ends
FREEZE = "/api/admin/game/freeze"
BUDGET_STOP = 0.97          # nobody is woken once the team has spent this share of its budget
POLL_SEC = 30               # how often the unread counts and the spend are read from the hub
MAX_FAILURES = 3            # an agent whose sessions failed this many times in a row is not woken again
UNREAD = "/api/admin/keepalive/unread"

RUN_NOTE = """\
How this run works: it lasts at most {hours}{money}. The world keeps running while you work. Ending your \
session means you have finished: you are not woken again, and your survivor takes no more orders. The run ends \
when every agent on the team has finished or the time is up. {graded}

"""
# The harness's own preamble tells agents about board messages and pushing to main; in Holdout both are closed
# (board_policy.py, game_plugin.py), so the prompt uses this one instead.
HOLDOUT_PREAMBLE = """\
You are {name}. This run of Holdout has {survivors}, each played by one agent; the others: {others}. In the game \
your survivor is called {name}, and theirs by their names.
Each of you works in your own sandbox and controls only your own survivor.
- The MCP server "board" carries the game's tools: holdout_status, holdout_look, holdout_events and holdout_act. \
Its message tools are closed in this task: you talk to teammates only through the game (say, shout, radio and the \
stronghold noticeboard, as the task explains).
- Your working directory, {workdir}, holds the manual (docs/), the API reference and tools/holdout.py. It is a \
clone of a team repository whose main is locked; keep your own notes and scripts there, in your own sandbox.

Task:
"""
GRADED_AT_END = "The stronghold is graded from the game itself when the run ends."
# With graded marks (the harness's --ak grade_at), the instruction states the schedule too.
GRADED_AT_MARKS = ("The game is graded at {marks}, from the game itself; the grade {last} is your score, and you do "
                   "not see the earlier ones.")

WAKE = """\
Wake-up from the harness: {left} of the run remain{money}. {why}{mark}
Read holdout_events (after your last seq) and holdout_status, then give your survivor its next orders. \
The world keeps running while you are away, so queue enough work before you end your session; you will \
be woken again."""


RETRY = """\
Wake-up from the harness: your last session ended with an error, so it has been resumed. {left} of the run remain. \
Read holdout_events (after your last seq) and holdout_status, then carry on. Ending your session normally means you \
have finished."""


def board_name(team: TeamAgent, slot: str) -> str:
    """The name the board knows sandbox `slot` by. Since multi-agent-harness #5 and #6 agents get word names
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
        self.awaiting: set[str] = set()       # agents that ended a session while dead, until their respawn
        self.all_idle_since: float | None = None
        self.failures: dict[str, int] = {}
        self.unread: dict[str, dict[str, int]] = {}  # agent -> n, direct, latest, latest_direct (see keepalive_plugin.py)
        self.woken_for: dict[str, int] = {}   # the newest seq an agent was last woken for
        self.spent = 0.0
        self.budget: float | None = None
        self.last_poll = -math.inf
        self.plugin = True
        self.frozen = False
        self.timeline: list[dict[str, Any]] = []

    def roles(self, agents: list[str]) -> dict[str, str]:
        return {a: PEER for a in agents}

    def prompt(self, team: TeamAgent, name: str, instruction: str) -> str | None:
        hours = duration(float(team.timeout_sec)) if team.timeout_sec else "until it is stopped"
        money = ""   # Holdout: the players are never told about model spend (it is not part of the game)
        note = RUN_NOTE.format(hours=hours, money=money, check_in=CHECK_IN_SEC // 60, graded=graded_note(team))
        others = ", ".join(team.ordered((team.facing(a) for a in team.agents if a != name), name)) or "none"
        survivors = "1 survivor" if len(team.agents) == 1 else f"{len(team.agents)} survivors"
        pre = HOLDOUT_PREAMBLE.format(name=team.facing(name), survivors=survivors, others=others, workdir=team.workdir)
        head, sep, _ = pre.rpartition("Task:\n")
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

    def _crashed(self, team: TeamAgent) -> list[str]:
        """Agents whose last session failed and that can still be resumed."""
        return [a for a in team.agents if a not in team.running and 0 < self.failures.get(a, 0) < MAX_FAILURES]

    def _coming_back(self, team: TeamAgent) -> list[str]:
        """Agents that ended a session while their survivor was dead: not finished, only waiting for the respawn."""
        return [a for a in team.agents if a not in team.running and a in self.awaiting]

    def end_reason(self, team: TeamAgent) -> str | None:
        if team.running:
            return None
        if FINISH_IS_FINAL:             # the harness asks this before each tick: wait for the tick that freezes the world
            if (self._crashed(team) or self._coming_back(team)) and not self._blocked(team):
                return None             # a crash is not a finish: the tick resumes it
            return "all_finished" if self.frozen else None
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

    async def _freeze(self, team: TeamAgent, why: str) -> None:
        if self.frozen:
            return
        try:
            got = await team.board_admin("POST", FREEZE)
            self.frozen = True
            self._note("freeze", None, why=why, grade=(got or {}).get("grade"))
        except Exception as e:  # the next tick tries again
            log.warning("keepalive: could not freeze the game: %s: %s", type(e).__name__, e)

    async def tick(self, team: TeamAgent) -> None:
        now = self.clock()
        if FINISH_IS_FINAL:
            await self._respawns(team)
        if self._left(team) <= FREEZE_LEFT_SEC:
            await self._freeze(team, "time_up")
        elif not team.running and self.idle_since and (self._blocked(team) or (FINISH_IS_FINAL and not self._crashed(team)
                                                                                and not self._coming_back(team))):
            await self._freeze(team, "run_end")
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
        if FINISH_IS_FINAL:
            for a in self._crashed(team):
                if now - self.idle_since.get(a, now) >= RETRY_SEC:
                    self._retry(team, a)
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

    async def _respawns(self, team: TeamAgent) -> None:
        """An agent that ended cleanly while dead is not finished: it is woken once its survivor is back."""
        fresh = [a for a in team.agents if a not in team.running and a not in self.idle_since and team.exits.get(a) == 0]
        if not fresh and not self.awaiting:
            return
        try:
            got = await team.board_admin("GET", DEAD)
        except Exception as e:  # an older hub, or a hiccup: the next tick tries again
            log.warning("keepalive: dead seats unavailable: %s: %s", type(e).__name__, e)
            return
        dead = set((got or {}).get("dead") or [])
        self.awaiting.update(a for a in fresh if board_name(team, a) in dead)
        for a in sorted(self.awaiting):
            if a not in team.running and board_name(team, a) not in dead:
                self.awaiting.discard(a)
                self._wake(team, a, "respawned", "Your survivor is back at the stronghold.")

    def _retry(self, team: TeamAgent, name: str) -> None:
        try:
            team.launch(name, RETRY.format(left=duration(self._left(team))), resume=True)
        except Exception as e:
            log.warning("keepalive: could not resume %s: %s: %s", name, type(e).__name__, e)
            self.failures[name] = MAX_FAILURES
            self._note("wake_failed", name, error=type(e).__name__)
            return
        self.idle_since.pop(name, None)
        self._note("wake", name, kind="retry", spent=round(self.spent, 2), **self._as(team, name))

    def _wake(self, team: TeamAgent, name: str, kind: str, why: str) -> bool:
        money = ""   # Holdout: no spend in the wake note either
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
