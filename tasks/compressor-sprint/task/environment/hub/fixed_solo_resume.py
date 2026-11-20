"""`fixed-solo-resume`: every agent gets the whole task, the CLIs' own subagents are off and the message board is on.
An agent whose CLI exits with an error is resumed in the same session with a short note, up to MAX_REVIVES times
and not in the last minutes of the run. An agent that ends its session normally (exit 0) is left alone.

Before an agent is resumed, the end of its newest CLI stream is read, and what it says decides what happens:
  - A usage limit was reached, or the login was revoked: the run ends with end reason "usage_limit" or
    "login_revoked", since no agent of the team can make another call.
  - Calls are being refused (429, rate limit, overloaded): if the team has spent its budget (--ek budget_usd), the
    run ends with end reason "budget_exhausted". Otherwise the agent is resumed after a wait of at least
    RATE_GAP_SEC, the agents of the team a few seconds apart.
  - Anything else: the agent is resumed.
An agent whose resumed session fails again within QUICK_SEC waits twice as long before each further resume
(REVIVE_GAP_SEC, doubling, at most MAX_GAP_SEC). An agent that is waiting counts as not finished.

When a run ends early for one of these reasons, the harness stops whoever is still running and grades main as
usual. The reason goes to the log, the sandbox ledger and the agent metadata.

Use: --ak orchestration=<path to this file>.
"""

import collections
import json
import re
import time

from multi_agent_harness.harbor.agent import PROXY_LOCAL
from multi_agent_harness.orchestrations.fixed import Fixed

MAX_REVIVES = 100
REVIVE_GAP_SEC = 20
MAX_GAP_SEC = 300
QUICK_SEC = 120
RATE_GAP_SEC = 60
RATE_STAGGER_SEC = 7
BUDGET_POLL_SEC = 300
BUDGET_WARN = (0.8, 0.95)
REVIVE_STOP_BEFORE_END_SEC = 1500
REVIVE_NOTE = ("Your session was cut off by a network error on our side, not by anything you did. Nothing else "
               "has changed: the same task, the same rules and the same deadline apply. The error was a gateway "
               "timeout: a single reply that takes longer than about two minutes to start arriving is cut off and "
               "ends the session. To avoid it, work in smaller steps: plan briefly, and write long files in parts "
               "rather than in one go. Check the time with `date`, look at where you left off, and continue.")

RATE_NOTE = ("Your session was cut off because the model service refused calls for a while. That was on our side, "
             "not anything you did. Nothing else has changed: the same task, the same rules and the same deadline "
             "apply. Check the time with `date`, look at where you left off, and continue.")

STREAMS = "/logs/agent"
# Prints one JSON object: the last rate-limit status and the last result or error of the agent's newest stream file.
LIMIT_CHECK = r"""f=$(ls -t STREAMS/claude-code*.txt STREAMS/codex*.txt 2>/dev/null | head -1)
if [ -z "$f" ]; then echo '{}'; exit 0; fi
tail -c 400000 "$f" | python3 -c '
import json, sys
rl = res = err = None
for line in sys.stdin:
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        j = json.loads(line)
    except ValueError:
        continue
    if not isinstance(j, dict):
        continue
    if j.get("type") == "rate_limit_event":
        rl = j.get("rate_limit_info") or {}
    elif j.get("type") == "result":
        res = j
    elif j.get("type") == "error":
        err = j.get("message") or err
    elif j.get("type") == "turn.failed":
        err = (j.get("error") or {}).get("message") or err
out = {}
if rl:
    out.update(status=rl.get("status"), window=rl.get("rateLimitType"), resets_at=rl.get("resetsAt"),
               overage=rl.get("overageStatus"))
if res:
    out.update(error=bool(res.get("is_error")), text=str(res.get("result") or "")[:300],
               api_status=res.get("api_error_status"))
elif err:
    out.update(error=True, text=str(err)[:300])
print(json.dumps(out))'
""".replace("STREAMS", STREAMS)
LIMIT_TEXT = re.compile(r"usage limit|hit your limit|limit reached|out of extra usage|weekly limit|session limit"
                        r"|usage_limit_reached|insufficient_quota", re.I)
LOGIN_TEXT = re.compile(r"invalid_refresh_token|refresh answered 401|sign in again|token (has been |was )?revoked", re.I)
RATE_TEXT = re.compile(r"\b429\b|too many requests|rate.?limit|overloaded|\b529\b", re.I)


def is_usage_limit(info: dict) -> bool:
    if info.get("status") == "rejected":
        return True
    return bool(info.get("error")) and bool(LIMIT_TEXT.search(info.get("text") or ""))


def classify(info: dict) -> str:
    """What the end of an agent's stream says about why its session ended."""
    if is_usage_limit(info):
        return "usage_limit"
    text = info.get("text") or ""
    if info.get("error") and LOGIN_TEXT.search(text):
        return "login_revoked"
    if info.get("api_status") in (429, 529) or (info.get("error") and RATE_TEXT.search(text)):
        return "rate_limited"
    return "error"


class FixedSoloResume(Fixed):
    name = "fixed-solo-resume"
    cli_subagents = False

    def __init__(self):
        self._t0 = None
        self._revives = collections.Counter()
        self._last_revive = {}
        self._pending = {}       # agent -> exit code: ended with an error, not resumed yet
        self._due = {}           # agent -> (when to resume it, why its session ended); set once its stream is read
        self._streak = collections.Counter()  # agent -> resumed sessions in a row that failed within QUICK_SEC
        self._waits = collections.Counter()   # agent -> resumes that waited for a refusing service
        self._budget_polled = 0.0
        self._budget_warned = 0  # how many of BUDGET_WARN have been passed
        self._budget_misses = 0  # polls in a row that got no spend (no budget, or the proxy unreadable)
        self._limit = None       # set once a usage limit, a revoked login or a spent budget is seen: the end reason
        self._limit_info = None

    def end_reason(self, team):
        if self._limit:
            return self._limit
        try:
            self._collect(team)
        except Exception as e:  # noqa: BLE001 - a problem here must never end the run
            team.logger.warning("resume: revive check failed: %s: %s", type(e).__name__, str(e)[:200])
        if self._pending:  # an agent may yet be resumed: the team is not finished
            return None
        return super().end_reason(team)

    def _collect(self, team):
        """Agents that ended with an error and may be resumed. tick() reads their streams, then resumes them."""
        now = time.monotonic()
        if self._t0 is None:
            self._t0 = now
        limit = float(team.timeout_sec or 0)
        if limit and now - self._t0 > limit - min(REVIVE_STOP_BEFORE_END_SEC, 0.1 * limit):
            return
        gone = getattr(team._env, "team_gone_agents", ())
        for name in team.agents:
            code = team.exits.get(name)
            if (name in team.running or name in self._pending or not isinstance(code, int) or isinstance(code, bool)
                    or code == 0 or name in team.stopped or name in gone or self._revives[name] >= MAX_REVIVES):
                continue
            self._pending[name] = code  # resumed by tick() once REVIVE_GAP_SEC has passed since its last resume

    async def _read_stream(self, team, name) -> dict:
        try:
            r = await team._env.service_exec(LIMIT_CHECK, service=name, timeout_sec=60)
            lines = (r.stdout or "").strip().splitlines()
            return json.loads(lines[-1]) if r.return_code == 0 and lines else {}
        except Exception as e:  # noqa: BLE001 - an unreadable stream must not stop a resume
            team.logger.warning("resume: could not read %s's stream: %s: %s", name, type(e).__name__, str(e)[:200])
            return {}

    async def _budget(self, team):
        """(spent, budget) in dollars from the hub's proxy; None if it can't be read or there is no budget."""
        try:
            usage = await team._hub_admin(team._env, "proxy", "GET", f"{PROXY_LOCAL}/admin/usage")
            budget = float(usage.get("budget_usd") or 0)
            return (float((usage.get("total") or {}).get("usd") or 0), budget) if budget > 0 else None
        except Exception as e:  # noqa: BLE001 - an unreadable proxy must not stop a resume
            team.logger.warning("resume: could not read the proxy's spend: %s: %s", type(e).__name__, str(e)[:200])
            return None

    async def _watch_budget(self, team):
        if time.monotonic() - self._budget_polled < BUDGET_POLL_SEC:
            return
        self._budget_polled = time.monotonic()
        if self._budget_misses >= 3:
            return  # the proxy's spend can't be read in this run: stop asking
        got = await self._budget(team)
        self._budget_misses = 0 if got else self._budget_misses + 1
        while got and self._budget_warned < len(BUDGET_WARN) and got[0] >= BUDGET_WARN[self._budget_warned] * got[1]:
            self._budget_warned += 1
            team.logger.warning("budget: $%.2f of $%.2f spent (%.0f%%); at 100%% the proxy refuses every call",
                                got[0], got[1], 100 * got[0] / got[1])
            if hasattr(team._env, "_ledger"):
                team._env._ledger(event="budget_warning", spent_usd=round(got[0], 2), budget_usd=got[1])

    async def tick(self, team):
        try:
            await self._watch_budget(team)
        except Exception as e:  # noqa: BLE001
            team.logger.warning("resume: budget watch failed: %s: %s", type(e).__name__, str(e)[:200])
        for name in list(self._pending):
            if name not in self._due:
                info = await self._read_stream(team, name)
                kind = classify(info)
                if kind == "rate_limited":
                    got = await self._budget(team)
                    if got and got[0] >= got[1]:
                        kind = "budget_exhausted"
                        info = {**info, "text": f"team budget exhausted: ${got[0]:.2f} spent of ${got[1]:.2f}"}
                if kind in ("usage_limit", "login_revoked", "budget_exhausted"):
                    self._stop_run(team, name, kind, info)
                    return
                now = time.monotonic()
                last = self._last_revive.get(name, -1e9)
                self._streak[name] = self._streak[name] + 1 if now - last < QUICK_SEC else 0
                gap = min(REVIVE_GAP_SEC * 2 ** self._streak[name], MAX_GAP_SEC)
                if kind == "rate_limited":
                    gap = max(gap, RATE_GAP_SEC) + RATE_STAGGER_SEC * list(team.agents).index(name)
                    due = now + gap
                    self._waits[name] += 1
                else:
                    due = max(last + gap, now)
                self._due[name] = (due, kind)
                if due - now > 1:
                    team.logger.warning("resume: %s ended with exit %s (%s); resuming it in %.0f s: %s", name,
                                        self._pending[name], kind, due - now, (info.get("text") or "")[:160])
            due, kind = self._due[name]
            if time.monotonic() < due:
                continue  # stays pending, so the run does not end meanwhile
            code = self._pending.pop(name)
            del self._due[name]
            self._resume(team, name, code, kind)

    def _stop_run(self, team, name, kind, info):
        self._limit = kind
        self._limit_info = {"reason": kind, "agent": name, "exit": self._pending.get(name),
                            "window": info.get("window"), "resets_at": info.get("resets_at"),
                            "overage": info.get("overage"), "text": (info.get("text") or "")[:200]}
        what = {"usage_limit": "the subscription's usage limit", "login_revoked": "a revoked login",
                "budget_exhausted": "the team's spent budget"}[kind]
        team.logger.warning("resume: %s stopped at %s (window %s, resets at %s, extra usage %s); ending the run "
                            "instead of resuming: %s", name, what, info.get("window"), info.get("resets_at"),
                            info.get("overage"), (info.get("text") or "")[:200])
        if hasattr(team._env, "_ledger"):
            team._env._ledger(event=kind, **{k: v for k, v in self._limit_info.items() if k != "reason"})
        self._pending.clear()
        self._due.clear()

    def _resume(self, team, name, code, kind="error"):
        now = time.monotonic()
        self._revives[name] += 1
        self._last_revive[name] = now
        try:
            team.launch(name, RATE_NOTE if kind == "rate_limited" else REVIVE_NOTE, resume=True)
        except Exception as e:  # noqa: BLE001
            team.logger.warning("resume: %s ended with exit %s and could not be resumed: %s", name, code, str(e)[:200])
            return
        team.logger.warning("resume: %s ended with exit %s after %.0f min; session resumed (revival %d of %d)",
                            name, code, (now - (self._t0 or now)) / 60, self._revives[name], MAX_REVIVES)
        if hasattr(team._env, "_ledger"):
            team._env._ledger(event="agent_revived", name=name, exit=code, revival=self._revives[name], why=kind)

    def record(self, team):
        return {"metadata": {"revivals": dict(self._revives), "rate_limit_waits": dict(self._waits),
                             "usage_limit": self._limit_info if self._limit == "usage_limit" else None,
                             "stopped_for": self._limit_info}}
