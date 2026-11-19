"""`async`: the first slot (agent-1) leads alone and spawns subagents at will; the other sandboxes are their pool.

The board plugin (multi_agent_harness.board.subagents) holds the subagents and their status and gives the lead
its tools; tick() does what it asks (start, wake, stop) and reports each finished run back, which
delivers the final response to the lead. The run ends when the lead finishes. The board knows the
sandboxes by their agent-facing names (team.facing), so those are the pool's slots; tick() maps
them back."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from multi_agent_harness import names as naming
from multi_agent_harness.harbor.orchestration import LEAD, SUBAGENT, Orchestration

if TYPE_CHECKING:
    from multi_agent_harness.harbor.agent import TeamAgent

log = logging.getLogger("multi_agent_harness.orchestrations.async")

LEAD_PREAMBLE = """\
You are {name}, the lead. You work in your own sandbox and can create subagents to help you, each \
in its own sandbox, with these tools of the MCP server "board":
- spawn_agent(instructions) starts a subagent and returns its name at once, without waiting for it. \
It sees only your instructions, not the task below, so give it everything it needs. Up to {pool} \
work at a time; more wait as queued until you terminate one.
- When a subagent finishes, its final response reaches you as a board message from it, and it goes \
idle. Sending an idle subagent a message (send(text, to=its name)) wakes it with that message as new \
instructions.
- terminate_agent(name) stops one for good. agent_status() shows each as working, idle, queued or \
terminated.
The message board is the MCP server "board": send(text, to=None) (to=None reaches everyone; to can \
also be one name or a list of names), inbox(wait_seconds=60) waits for messages, history() re-reads. \
Subagents can message you and each other.
{workdir} is your own clone of the team git repository (remote origin, branch main, credentials \
already set up), and it starts with the task's files. Each subagent has its own clone; you see its \
work only after it pushes, so pull to get it. You can push to main and to your own branches under \
{name}/. Pushes to main must be fast-forwards: if one is rejected, pull --rebase and push again. \
Force pushes and deleting branches are refused.
The run ends when you finish: working subagents are stopped and main is graded exactly as pushed, \
so anything not committed to main is gone. Wait (inbox) for the subagents you need, and finish only \
once the work is on main.

Task:
"""

SUBAGENT_PREAMBLE = """\
You are {name}, a subagent. {lead} leads the team and sent you the instructions below. You work in \
your own sandbox.
- The message board is the MCP server "board": send(text, to=None) (to=None reaches everyone; to \
can also be one name or a list of names), inbox(wait_seconds=60) waits for messages, history() \
re-reads. Use it to reach {lead} or the other subagents.
- {workdir} is your own clone of the team git repository (remote origin, branch main, credentials \
already set up). Others see your work only after you commit and push it; pull to get theirs. You can \
push to main and to your own branches under {slot}/. Pushes to main must be fast-forwards: if one is \
rejected, pull --rebase and push again. Force pushes and deleting branches are refused.
When you finish, your final response is delivered to {lead} as a message, and you wait until {lead} \
sends you new instructions.

Instructions from {lead}:
"""

WAKE = "New message from {lead}:\n{message}\n\nCheck inbox() for anything else sent to you while you were idle."


class AsyncSubagents(Orchestration):
    name = "async"
    board_plugins = ("multi_agent_harness.board.subagents",)
    identity_role = SUBAGENT
    cli_subagents = False  # its subagents are the board's; Codex's own would shadow spawn_agent

    def __init__(self) -> None:
        self.lead: str | None = None
        self.pool: list[str] = []
        self.slot_of: dict[str, str] = {}  # agent-facing name -> sandbox
        self.active: dict[str, str] = {}  # sandbox -> subagent whose run is not reported yet
        self.status: dict[str, str] = {}
        self.timeline: list[dict[str, Any]] = []
        self.reported_lost: set[str] = set()
        self._t0 = time.monotonic()

    def roles(self, agents: list[str]) -> dict[str, str]:
        if len(agents) < 2:
            raise ValueError("orchestration async needs n_agents >= 2: the lead plus a pool for its subagents")
        return {a: LEAD if i == 0 else SUBAGENT for i, a in enumerate(agents)}

    def on_board(self, agents: list[str]) -> list[str]:
        return agents[:1]

    def prompt(self, team: TeamAgent, name: str, instruction: str) -> str | None:
        if team.roles[name] != LEAD:
            return None
        return LEAD_PREAMBLE.format(name=team.facing(name), pool=len(team.agents) - 1, workdir=team.workdir) + instruction

    def end_reason(self, team: TeamAgent) -> str | None:
        return None if self.lead in team.running else "lead_finished"

    async def start(self, team: TeamAgent) -> None:
        self.lead = next(a for a, r in team.roles.items() if r == LEAD)
        self.pool = [a for a in team.agents if a != self.lead]
        self.slot_of = {team.facing(a): a for a in self.pool}
        self._t0 = time.monotonic()
        seed = f"{team.name_seed}:subagents" if team.name_mode == naming.WORDS else None
        await team.board_admin("POST", "/api/admin/subagents/pool",
                               {"lead": team.facing(self.lead), "slots": list(self.slot_of),
                                "resources": {team.facing(a): r for a, r in team.agent_resources.items()},
                                "name_seed": seed})

    def _note(self, op: str, name: str, slot: str | None, **extra: Any) -> None:
        self.timeline.append({"t": round(time.monotonic() - self._t0, 1), "op": op, "name": name,
                              "sandbox": slot, **extra})

    async def _finished(self, team: TeamAgent, name: str, slot: str, text: str | None, exit: Any) -> None:
        r = await team.board_admin("POST", "/api/admin/subagents/finished", {"name": name, "text": text, "exit": exit})
        self._note("finish", name, slot, exit=exit, delivered=bool(r and r.get("delivered")))

    async def tick(self, team: TeamAgent) -> None:
        try:
            for slot, name in list(self.active.items()):
                if slot not in team.running:
                    if slot in team.lost and await team.sandbox_gone(slot):
                        await team.board_admin("POST", "/api/admin/subagents/lost", {"slot": team.facing(slot)})
                        self.reported_lost.add(slot)
                        del self.active[slot]
                        self._note("lost", name, slot)
                        continue
                    exit = team.runs.get(name, {}).get("exit")
                    await self._finished(team, name, slot, await team.final_response(name), exit)
                    del self.active[slot]
            got = await team.board_admin("GET", "/api/admin/subagents/actions", secret=True)
        except Exception as e:  # a hub hiccup; the next tick tries again
            log.warning("async tick failed: %s: %s", type(e).__name__, e)
            return
        self.status = got.get("status", self.status)
        for a in got.get("actions", []):
            op, name, slot = a["op"], a["name"], self.slot_of.get(a.get("slot"), a.get("slot"))
            if slot in self.reported_lost:
                continue
            try:
                if op == "stop":
                    if self.active.get(slot) == name:
                        del self.active[slot]
                    await team.stop(slot)
                elif op == "start":
                    prompt = SUBAGENT_PREAMBLE.format(name=name, lead=team.facing(self.lead), workdir=team.workdir,
                                                      slot=team.facing(slot))
                    team.launch(slot, prompt + a["instructions"], identity=name, board_token=a["token"])
                    self.active[slot] = name
                elif op == "wake":
                    team.launch(slot, WAKE.format(lead=team.facing(self.lead), message=a["message"]), identity=name, resume=True)
                    self.active[slot] = name
                self._note(op, name, slot)
            except Exception as e:
                log.warning("async %s %s on %s failed: %s: %s", op, name, slot, type(e).__name__, e)
                self._note(op, name, slot, error=type(e).__name__)
                if op != "stop":
                    await self._finished(team, name, slot, f"(could not {op}: {type(e).__name__})", None)

    def record(self, team: TeamAgent) -> dict[str, Any]:
        ops = [e["op"] for e in self.timeline if "error" not in e]
        counts = {"spawns": len(self.status), "starts": ops.count("start"), "wakes": ops.count("wake"),
                  "terminations": sum(s == "terminated" for s in self.status.values()),
                  "final_responses": sum(1 for e in self.timeline if e["op"] == "finish" and e.get("delivered"))}
        return {"metadata": {"subagents": {**counts, "status": dict(self.status), "timeline": self.timeline}},
                "ledger": {"subagents": counts}}
