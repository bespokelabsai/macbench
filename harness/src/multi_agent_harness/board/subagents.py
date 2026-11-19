"""Board plugin for async subagents: the lead's spawn, terminate and status tools.

The board holds who exists and in what state; the harness on the host does the work. A spawn
registers the subagent on the board at once (so the lead can message it straight away) and
either binds it to a free sandbox from the pool or queues it. The harness polls
GET /api/admin/subagents/actions for what to start, wake or stop, and reports each finished CLI
run to POST /api/admin/subagents/finished, which delivers the final response to the lead as a
message from that subagent and leaves it idle. A direct message from the lead to an idle
subagent wakes it with that message.

Statuses: queued (no sandbox free yet), working, idle, terminated. The tools are listed to and
callable by the agent registered with role "lead" only.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from fastapi import Header
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ListToolsResult
from pydantic import BaseModel, Field

from .. import names as naming
from .core import Board, BoardError, RateLimited

LEAD_TOOLS = ("spawn_agent", "terminate_agent", "agent_status")
ROLE = "subagent"


def _bearer(value: str | None) -> str | None:
    return value[7:].strip() if value and value.lower().startswith("bearer ") else None


class Pool:
    """Subagent state. Every method is safe to call from any thread."""

    def __init__(self, board: Board):
        self.board = board
        self.lock = threading.Lock()
        self.lead: str | None = None
        self.slots: list[str] = []
        self.resources: dict[str, dict[str, Any]] = {}
        self.losses: dict[str, dict[str, Any]] = {}
        self.reported_losses: set[str] = set()
        self.holder: dict[str, str] = {}          # sandbox -> subagent bound to it
        self.subs: dict[str, dict[str, Any]] = {}  # name -> {"status", "slot"}
        self.queue: list[str] = []
        self.waiting: dict[str, dict[str, Any]] = {}  # queued name -> its start action
        self.actions: list[dict[str, Any]] = []   # for the host, in order
        self.delivered: dict[str, set[int]] = {}   # name -> seqs already handed over as a wake prompt
        self._n = 0
        self._names = None  # word subagent names from a seed (names.py); None numbers them

    def configure(self, lead: str, slots: list[str], resources: dict[str, dict[str, Any]] | None = None,
                  name_seed: str | None = None) -> None:
        with self.lock:
            self.lead, self.slots = lead, list(slots)
            self._names = naming.candidates(name_seed) if name_seed is not None else None
            self.resources = {s: dict((resources or {}).get(s, {"gpus": 0})) for s in slots}
            self._schedule()

    def _caller_is_lead(self, caller: str) -> None:
        if self.board.role(caller) != "lead":
            raise BoardError("only the lead can manage subagents")

    def _name(self) -> str:
        taken = set(self.board.agents())
        if self._names is not None:
            return naming.fresh(self._names, taken | set(self.subs) | set(self.slots))
        while True:
            self._n += 1
            name = f"agent-{self._n}"
            if name not in taken and name not in self.subs:
                return name

    def _free_slots(self) -> list[str]:
        free = []
        for s in self.slots:
            h = self.holder.get(s)
            if s not in self.losses and (h is None or self.subs[h]["status"] == "terminated"):
                free.append(s)
        return free

    def _has_gpu(self, slot: str) -> bool:
        return bool(self.resources[slot].get("gpus", 0))

    def _accepts(self, slot: str, gpu: bool) -> bool:
        # Reserve GPUs in mixed pools, even if their CPU slots are busy or lost.
        return self._has_gpu(slot) if gpu else (not self._has_gpu(slot) or
                                                all(self._has_gpu(s) for s in self.slots))

    def _schedule(self) -> None:
        """Reserve mixed-pool GPUs; all-GPU pools admit ordinary work behind GPU requests."""
        for slot in self._free_slots():
            compatible = [n for n in self.queue if self._accepts(slot, self.subs[n]["gpu_required"])]
            if not compatible:
                continue
            name = min(compatible, key=lambda n: not self.subs[n]["gpu_required"])
            self.queue.remove(name)
            self.holder[slot] = name
            self.subs[name].update(status="working", slot=slot)
            self.actions.append({**self.waiting.pop(name), "slot": slot})

    def spawn(self, caller: str, instructions: str, gpu: bool = False) -> dict[str, Any]:
        self._caller_is_lead(caller)
        if not instructions or not instructions.strip():
            raise BoardError("instructions are empty")
        if not isinstance(gpu, bool):
            raise BoardError("gpu must be a boolean")
        with self.lock:
            usable = [s for s in self.slots if s not in self.losses]
            if not any(self._accepts(s, gpu) for s in usable):
                raise BoardError("no usable GPU slots in the subagent pool" if gpu else
                                 "no usable CPU slots in the subagent pool")
            name = self._name()
            token = self.board.register_agent(name, ROLE)
            self.subs[name] = {"status": "queued", "slot": None, "gpu_required": gpu}
            self.waiting[name] = {"op": "start", "name": name, "instructions": instructions, "token": token}
            self.queue.append(name)
            self._schedule()
            status = self.subs[name]["status"]
            assignment = self._assignment(name)
            has_gpus = any(self._has_gpu(s) for s in self.slots)
        self.board._emit("subagent", caller, op="spawn", name=name, status=status, **assignment)
        # Preserve the old result shape on CPU-only runs.
        return {"name": name, "status": status, **(assignment if has_gpus else {})}

    def _assignment(self, name: str) -> dict[str, Any]:
        sub = self.subs[name]
        slot = sub["slot"]
        return {"slot": slot, "gpu_required": sub["gpu_required"],
                "resources": dict(self.resources[slot]) if slot else None,
                "available": slot is not None and slot not in self.losses and sub["status"] != "terminated"}

    def terminate(self, caller: str, name: str) -> dict[str, Any]:
        self._caller_is_lead(caller)
        with self.lock:
            sub = self.subs.get(name)
            if sub is None:
                raise BoardError(f"no subagent named {name!r}")
            before = sub["status"]
            if before != "terminated":
                sub["status"] = "terminated"
                if before == "queued":
                    self.queue.remove(name)
                    self.waiting.pop(name, None)
                else:
                    self.actions.append({"op": "stop", "name": name, "slot": sub["slot"]})
                self._schedule()  # its sandbox goes to the next queued subagent, after the stop
        if before != "terminated":
            self.board._emit("subagent", caller, op="terminate", name=name, was=before)
        return {"name": name, "status": "terminated"}

    def status(self, caller: str, detail: bool = False) -> dict[str, Any]:
        self._caller_is_lead(caller)
        with self.lock:
            if detail:
                usable = [s for s in self.slots if s not in self.losses]
                free = self._free_slots()
                return {"agents": {n: {"status": s["status"], **self._assignment(n)} for n, s in self.subs.items()},
                        "capacity": {"slots": len(usable), "gpu_slots": sum(self._has_gpu(s) for s in usable),
                                     "free_slots": len(free), "free_gpu_slots": sum(self._has_gpu(s) for s in free)}}
            return {n: s["status"] for n, s in self.subs.items()}

    def lose(self, slot: str) -> dict[str, Any]:
        """Idempotently remove a lost slot, terminating work that can no longer be scheduled."""
        with self.lock:
            if slot not in self.slots:
                raise BoardError(f"unknown subagent slot {slot!r}")
            new_loss = slot not in self.losses
            if new_loss:
                affected = []
                self.losses[slot] = {"slot": slot, "terminated": affected}
                holder = self.holder.get(slot)
                if holder and self.subs[holder]["status"] != "terminated":
                    self.subs[holder]["status"] = "terminated"
                    affected.append(holder)
                # Don't deliver pending starts/wakes for a sandbox that no longer exists.
                self.actions = [a for a in self.actions if a.get("slot") != slot]
                usable = [s for s in self.slots if s not in self.losses]
                for name in list(self.queue):
                    if not any(self._accepts(s, self.subs[name]["gpu_required"]) for s in usable):
                        self.subs[name]["status"] = "terminated"
                        self.queue.remove(name)
                        self.waiting.pop(name, None)
                        affected.append(name)
                self._schedule()
            result = self.losses[slot]
            reported = slot in self.reported_losses
        if new_loss:
            self.board._emit("subagent", None, op="slot_lost", **result)
        if not reported:
            author = self.holder.get(slot) or next(iter(result["terminated"]), None)
            if author is None:
                # A never-used pool sandbox can also disappear; give the notice a distinct
                # sender because the board excludes self-authored messages from inbox().
                author = "harness-status"
                if author not in self.board.agents():
                    self.board.register_agent(author, "system")
            names = result["terminated"]
            text = (f"[harness] Sandbox {slot} was lost; its resources are unavailable. "
                    f"Terminated {len(names)} subagent(s): {', '.join(names[:10]) or 'none'}. "
                    "Use agent_status(detail=True) for current assignments.")
            try:
                self.board.send(author, text[:self.board.policy.max_text], to=self.lead)
            except BoardError:
                # A rate limit or custom message policy must not stall healthy workers.
                # take_actions retries the notice without redoing the state transition.
                return result
            with self.lock:
                self.reported_losses.add(slot)
        return result

    def _wake(self, name: str, msgs: list[dict[str, Any]]) -> None:
        """Caller holds the lock and has checked the subagent is idle."""
        self.subs[name]["status"] = "working"
        self.delivered.setdefault(name, set()).update(m["seq"] for m in msgs)
        self.board.mark_handed(name, [m["seq"] for m in msgs])  # the wake prompt has them; inbox() shouldn't repeat them
        texts = [m["text"] for m in msgs]
        self.actions.append({"op": "wake", "name": name, "slot": self.subs[name]["slot"],
                             "message": "\n\n".join(texts)})

    def on_event(self, ev: dict[str, Any]) -> None:
        """A direct message from the lead to an idle subagent wakes it."""
        if ev.get("event") != "send" or self.lead is None or ev.get("agent") != self.lead or not ev.get("to"):
            return
        to = ev["to"] if isinstance(ev["to"], list) else [ev["to"]]
        woken = []
        with self.lock:
            for n in to:
                if self.subs.get(n, {}).get("status") == "idle":
                    self._wake(n, [ev])
                    woken.append(n)
        for n in woken:
            self.board._emit("subagent", self.lead, op="wake", name=n, seq=ev.get("seq"))

    def finished(self, name: str, text: str | None, exit: Any = None) -> dict[str, Any]:
        """The host saw this subagent's CLI exit. Deliver its final response and idle it; a
        message the lead sent it meanwhile that it never read wakes it again right away."""
        with self.lock:
            sub = self.subs.get(name)
            if sub is None or sub["status"] != "working":
                return {"delivered": False}
            sub["status"] = "idle"
        body = (text or "").strip() or f"(no final response; exit {exit})"
        head = f"[final response from {name}; it is idle now, send it a message to wake it]\n"
        limit = self.board.policy.max_text
        msg = head + (body if len(head) + len(body) <= limit else body[: limit - len(head) - 14] + "\n[truncated]")
        for _ in range(5):
            try:
                seq = self.board.send(name, msg, to=self.lead)["seq"]
                break
            except RateLimited as e:
                time.sleep(e.retry_after)
        else:
            seq = None
        self.board._emit("subagent", name, op="finish", name=name, exit=exit, seq=seq)
        unread = [m for m in self.board._inbox_now(name, 50)  # direct from the lead, not yet handed over
                  if m["from"] == self.lead and m["to"] is not None
                  and m["seq"] not in self.delivered.get(name, ())]
        with self.lock:
            if unread and self.subs[name]["status"] == "idle":
                self._wake(name, unread)
            else:
                unread = []
        if unread:
            self.board._emit("subagent", self.lead, op="wake", name=name, seq=None)
        return {"delivered": seq is not None, "seq": seq}

    def take_actions(self) -> dict[str, Any]:
        with self.lock:
            notices = self.losses.keys() - self.reported_losses
        for slot in sorted(notices):
            self.lose(slot)
        with self.lock:
            actions, self.actions = self.actions, []
            return {"actions": actions, "status": {n: s["status"] for n, s in self.subs.items()}}


class PoolBody(BaseModel):
    lead: str
    slots: list[str]
    resources: dict[str, dict[str, Any]] = Field(default_factory=dict)
    name_seed: str | None = None


class LostBody(BaseModel):
    slot: str


class FinishedBody(BaseModel):
    name: str
    text: str | None = None
    exit: int | str | None = None


def register(board: Board, api, mcp) -> Pool:
    pool = Pool(board)
    board.subscribe(pool.on_event)

    def call(ctx: Context, fn, *args):
        """Run a pool method as the caller; a refusal reaches the agent with its reason."""
        try:
            return fn(board.authenticate(_bearer((ctx.headers or {}).get("authorization"))), *args)
        except BoardError as e:
            raise ToolError(str(e)) from None

    @mcp.tool()
    def spawn_agent(instructions: str, ctx: Context, gpu: bool = False) -> dict:
        """Start a new subagent with these instructions and return its name at once, without
        waiting for it. It sees only your instructions, not your task. Its status is `working`,
        or `queued` until a sandbox frees up. Idle subagents retain their slots until terminated. Lead only."""
        return call(ctx, pool.spawn, instructions, gpu)

    @mcp.tool()
    def terminate_agent(name: str, ctx: Context) -> dict:
        """Stop a subagent for good; its sandbox goes to the next queued subagent. Lead only."""
        return call(ctx, pool.terminate, name)

    @mcp.tool()
    def agent_status(ctx: Context, detail: bool = False) -> dict:
        """Every subagent's status. detail=True also shows assigned resources and free pool capacity. Lead only."""
        return call(ctx, pool.status, detail)

    async def hide_lead_tools(ctx, call_next):
        """tools/list shows the lead tools to the lead only."""
        result = await call_next(ctx)
        if ctx.method != "tools/list":
            return result
        headers = getattr(ctx.request, "headers", None) or {}
        try:
            caller = board.authenticate(_bearer(headers.get("authorization")))
        except BoardError:
            caller = None
        lead = caller is not None and board.role(caller) == "lead"
        gpu_note = (" gpu=True requires a GPU. Mixed pools reserve GPU slots; gpu=False can use a GPU "
                    "only when the pool has no CPU slots. Queued GPU requests take priority there.")
        has_gpus = any(pool._has_gpu(s) for s in pool.slots)
        if isinstance(result, ListToolsResult):
            tools = [t for t in result.tools if lead or t.name not in LEAD_TOOLS]
            if lead and has_gpus:
                tools = [t.model_copy(update={"description": (t.description or "") + gpu_note})
                         if t.name == "spawn_agent" else t for t in tools]
            return result.model_copy(update={"tools": tools})
        if isinstance(result, dict) and isinstance(result.get("tools"), list):
            tools = [t for t in result["tools"] if lead or t.get("name") not in LEAD_TOOLS]
            if lead and has_gpus:
                tools = [{**t, "description": (t.get("description") or "") + gpu_note}
                         if t.get("name") == "spawn_agent" else t for t in tools]
            return {**result, "tools": tools}
        return result

    mcp.middleware.append(hide_lead_tools)

    @api.post("/admin/subagents/pool")
    def configure(body: PoolBody, authorization: str | None = Header(None)):
        api.state.require_admin(authorization)
        pool.configure(body.lead, body.slots, body.resources, body.name_seed)
        return {"lead": body.lead, "slots": body.slots}

    @api.post("/admin/subagents/lost")
    def lost(body: LostBody, authorization: str | None = Header(None)):
        api.state.require_admin(authorization)
        return pool.lose(body.slot)

    @api.get("/admin/subagents/actions")
    def actions(authorization: str | None = Header(None)):
        api.state.require_admin(authorization)
        return pool.take_actions()

    @api.post("/admin/subagents/finished")
    def finished(body: FinishedBody, authorization: str | None = Header(None)):
        api.state.require_admin(authorization)
        return pool.finished(body.name, body.text, body.exit)

    return pool
