"""Market office board plugin: the research budget, the research facilities, and the ledger of every test.

Load it with `--ek board_plugins=<path to this file>`. It runs inside the board on the hub, so it uses only
the standard library and the board's own packages. Every quote and every outcome is computed by
`python -m marketsim.office` with the task image's Python. The market simulator (marketsim) is on the hub
only: build/seedprofit_hub_plugin.py (environment/hub/make_hub_plugin.py), unpacks it there. A team never sees it.

The secret that sets the true facts of every product, the ledger, and the full records of test markets live in
/var/lib/seedprofit-hub on the hub, out of the agents' reach. The verifier reads the secret and the ledger.

Rules:
- A test is paid when it is ordered, from the team's shared research budget. Orders that would overspend it are
  refused. A queued test can be cancelled for a refund.
- Each facility runs its tests one at a time (the test market has 2 slots), first come first served. A test
  takes real minutes, and its result is ready when it ends.
- The full result goes only to the agent who ordered it. Everyone can see the ledger: who ordered what, on which
  product, at what cost, and whether it is queued, running or done.
"""
# No `from __future__ import annotations`: the MCP server reads the tool signatures, and Context is imported
# inside register(), so the annotations must be real objects, not strings.
import json
import os
import secrets
import subprocess
import threading
import time

DEFAULTS = {
    "python": "/usr/local/bin/python3",
    "pythonpath": "/opt/marketsim",
    "repo": "/srv/git/team.git",
    "state": "/var/lib/seedprofit-hub",
    "time_scale": 1.0,       # multiplies every test's duration
}
PUBLIC = ("id", "agent", "kind", "target", "facility", "cost_usd", "minutes", "status", "ordered_at", "started_at",
          "ends_at", "note")


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    for k in DEFAULTS:
        v = os.environ.get("SEEDPROFIT_OFFICE_" + k.upper())
        if v is not None:
            cfg[k] = type(DEFAULTS[k])(v)
    return cfg


class OfficeError(Exception):
    pass


class MarketOffice:
    """All office state and rules. Thread safe. Tools and REST routes are thin wrappers over this."""

    def __init__(self, board, cfg=None, now=time.time):
        self.board = board
        self.cfg = cfg or load_config()
        self.now = now
        self.lock = threading.RLock()
        os.makedirs(self.cfg["state"], exist_ok=True)
        os.chmod(self.cfg["state"], 0o700)
        self.secret_file = os.path.join(self.cfg["state"], "secret.json")
        if not os.path.exists(self.secret_file):
            fd = os.open(self.secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({"secret": os.environ.get("SEEDPROFIT_SECRET") or secrets.token_hex(32)}, f)
        with open(self.secret_file) as f:
            self.secret = json.load(f)["secret"]
        self.path = os.path.join(self.cfg["state"], "office.json")
        self.record_dir = os.path.join(self.cfg["state"], "records")
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.s = json.load(f)
        else:
            self.s = {"tests": [], "created_at": self.now()}
        for t in self.s["tests"]:
            # a test's thread does not survive a restart of the hub: run it again from the queue
            if t["status"] == "running" and t.get("result") is None and t.get("error") is None:
                t.update(status="queued", started_at=None, ends_at=None)
        self._catalog = None
        self.running = {}
        # The mid-run announcement (worlds.midrun_shock): seconds after the office starts, from STATE/midrun.json
        # (written by the hub plugin (build/seedprofit_hub_plugin.py)) or SEEDPROFIT_MIDRUN_AT_SEC. 0 or missing: none.
        self.midrun_at = 0.0
        try:
            self.midrun_at = float(json.load(open(os.path.join(self.cfg["state"], "midrun.json"))).get("at_sec") or 0)
        except (OSError, ValueError):
            self.midrun_at = float(os.environ.get("SEEDPROFIT_MIDRUN_AT_SEC", "0") or 0)
        # v1.0, the live company (marketsim/live.py): STATE/live/live.json, written by the hub plugin
        # (build/seedprofit_hub_plugin.py), says when the launch phase ends and how long a month takes. Missing: no live company.
        self.live = None
        try:
            self.live = json.load(open(os.path.join(self.cfg["state"], "live", "live.json")))
            self.midrun_at = 0.0          # the announced change is an event of the live world (month 18)
        except (OSError, ValueError):
            pass
        self.live_busy = False

    # -- plumbing ---------------------------------------------------------------------------------

    def cli(self, *args, payload=None, timeout=900) -> dict:
        # The office runs as root: from the state folder, with no folder of the team's on its import path (the team's
        # main is checked out into the board's working folder for the final grading).
        env = dict(os.environ, PYTHONPATH=self.cfg["pythonpath"], PYTHONSAFEPATH="1")
        payload = dict(payload or {}, state=self.cfg["state"])
        p = subprocess.run([self.cfg["python"], "-P", "-m", "marketsim.office", *args], input=json.dumps(payload),
                           capture_output=True, text=True, timeout=timeout, env=env, cwd=self.cfg["state"])
        try:
            out = json.loads((p.stdout or "").strip().splitlines()[-1])
        except Exception:
            self.emit(None, "office_error", error=(p.stderr or p.stdout).strip()[-800:])
            raise OfficeError("the market office could not do this; the error is in the board's log") from None
        if isinstance(out, dict) and out.get("error"):
            raise OfficeError(out["error"])
        return out

    def catalog(self) -> dict:
        if self._catalog is None:
            self._catalog = self.cli("catalog")
        return self._catalog

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.s, f)
        os.replace(tmp, self.path)

    def grading(self) -> bool:
        """True while the verifier grades a mark (marketsim.runner.grading_lock)."""
        try:
            pid = int(open(os.path.join(self.cfg["state"], "grading.lock")).read().strip() or 0)
            os.kill(pid, 0)
            return True
        except (OSError, ValueError):
            return False

    def emit(self, agent, op, **fields):
        try:
            self.board._emit("market", agent, op=op, **fields)
        except Exception:
            pass

    def budget(self) -> float:
        return float(self.catalog()["rules"]["research_budget"])

    def committed(self) -> float:
        """Failed tests are refunded, like cancelled ones."""
        return sum(t["cost_usd"] for t in self.s["tests"] if t["status"] not in ("cancelled", "failed"))

    def public(self, t):
        return {k: t.get(k) for k in PUBLIC}

    def get(self, test_id):
        for t in self.s["tests"]:
            if t["id"] == test_id:
                return t
        raise OfficeError(f"no test {test_id!r}")

    # -- the clock --------------------------------------------------------------------------------------

    def announcements(self):
        a = self.s.get("announcement")
        return [{k: a[k] for k in ("id", "text", "announced_at")}] if a else []

    # -- the live company ------------------------------------------------------------------------------

    def live_clock(self, now):
        """Months due by now: none in the launch phase, then one more every month_sec."""
        start = self.clock_zero() + float(self.live["launch_sec"])
        if now < start:
            return 0
        return min(int(self.live.get("months", 37)), int((now - start) // float(self.live["month_sec"])) + 1)

    def clock_zero(self):
        """When the run started for the team: keepalive.py says when the agents started (they install for minutes
        after the hub starts); without it, when the office started."""
        return self.s.get("run_started_at") or self.s["created_at"]

    def run_started(self):
        with self.lock:
            if not self.s.get("run_started_at"):
                self.s["run_started_at"] = self.now()
                self.save()
            return {"run_started_at": self.s["run_started_at"]}

    def live_tick(self, now):
        if not self.live or self.live_busy or self.s.get("live_ended") or self.grading():
            return
        if self.s.get("live_months", 0) < self.live_clock(now):
            self.live_busy = True
            threading.Thread(target=self._live_month, daemon=True, name="live-month").start()

    def _live_month(self):
        try:
            out = self.cli("live_month", payload={"secret": self.secret, "repo": self.cfg["repo"], "ref": "main"},
                           timeout=900)
            with self.lock:
                self.s["live_months"] = out.get("months", 0)
                self.s["live_day"] = out.get("day")
                self.s["live_ended"] = bool(out.get("ended"))
                last = (out.get("log") or [{}])[-1]
                self.save()
            self.emit(None, "company_month", month=last.get("month"), day=last.get("last_day"),
                      commit=(last.get("commit") or "")[:10], software_stopped=last.get("software_frozen"))
            change = self.live.get("change_day", 540)
            if change and not self.s.get("announcement") and (out.get("day") or 0) >= change - 31:
                shock = self.cli("live_change", payload={"secret": self.secret})
                with self.lock:
                    self.s["announcement"] = dict(shock, id="M1", announced_at=self.now())
                    self.save()
                self.emit(None, "market_announcement", text=shock["text"])
        except Exception as e:
            self.emit(None, "live_error", error=str(e)[:500])
        finally:
            self.live_busy = False

    def company_status(self, agent):
        if not self.live:
            raise OfficeError("this run has no live company")
        now = self.now()
        start = self.clock_zero() + float(self.live["launch_sec"])
        months = self.s.get("live_months", 0)
        out = {"phase": "ended" if self.s.get("live_ended") else ("launch" if now < start else "live"),
               "launch_ends_in_seconds": max(0, round(start - now)),
               "seconds_per_month": float(self.live["month_sec"]), "months_run": months,
               "months_in_all": int(self.live.get("months", 37)),
               "next_month_starts_in_seconds": None if self.s.get("live_ended") else
               max(0, round(start + months * float(self.live["month_sec"]) - now)),
               "announcements": self.announcements()}
        if months:
            b = self.cli("live_books", payload={"since": None, "until": None})
            out["today"] = b["days"][-1] if b.get("days") else None
            out["months"] = b.get("months")
        return out

    def company_books(self, agent, since=None, until=None):
        if not self.live:
            raise OfficeError("this run has no live company")
        return self.cli("live_books", payload={"since": since, "until": until})

    def tick(self):
        with self.lock:
            now = self.now()
            changed = False
            self.live_tick(now)
            if self.midrun_at and not self.s.get("announcement") and now >= self.s["created_at"] + self.midrun_at:
                shock = self.cli("midrun", payload={"secret": self.secret})
                self.s["announcement"] = dict(shock, id="M1", announced_at=now)
                changed = True
                self.emit(None, "market_announcement", text=shock["text"])
            for t in self.s["tests"]:
                if t["status"] == "running" and now >= t["ends_at"] and (t.get("result") is not None or t.get("error")):
                    t["status"] = "failed" if t.get("error") else "done"
                    changed = True
                    self.emit(t["agent"], "test_done", test=t["id"], kind=t["kind"], target=t["target"])
            slots = self.catalog()["slots"]
            grading = self.grading()
            for fac in sorted({t["facility"] for t in self.s["tests"] if t["status"] == "queued"}):
                if grading and fac == "test_market":
                    continue      # no team software runs while a mark is graded (it waits in the queue)
                busy = sum(1 for t in self.s["tests"] if t["status"] == "running" and t["facility"] == fac)
                cap = slots[fac.split(":")[0]]
                queued = sorted((t for t in self.s["tests"] if t["status"] == "queued" and t["facility"] == fac),
                                key=lambda x: x["ordered_at"])
                for t in queued[:max(0, cap - busy)]:
                    t["status"] = "running"
                    t["started_at"] = now
                    t["ends_at"] = now + 60.0 * t["minutes"] * float(self.cfg["time_scale"])
                    threading.Thread(target=self._run, args=(t["id"],), daemon=True, name=f"test-{t['id']}").start()
                    self.emit(t["agent"], "test_started", test=t["id"], facility=fac)
                    changed = True
            if changed:
                self.save()

    def _run(self, test_id):
        with self.lock:
            t = dict(self.get(test_id))
        a = self.s.get("announcement")
        midrun = {k: a[k] for k in ("kind", "categories", "size", "text")} if a else None
        try:
            res = self.cli("run", payload={"secret": self.secret, "test": t, "repo": self.cfg["repo"],
                                           "record_dir": self.record_dir, "midrun": midrun}, timeout=1800)
            err = None
        except Exception as e:
            res, err = None, str(e)[:1500]
        with self.lock:
            t = self.get(test_id)
            if err:
                t["error"] = err
            else:
                t["result"] = res
            self.save()

    # -- tools --------------------------------------------------------------------------------------

    def info(self, agent):
        c = self.catalog()
        return {"announcements": self.announcements(),
                "research_budget_usd": self.budget(), "test_kinds": c["kinds"], "slots_per_facility": c["slots"],
                "facilities": "one survey panel, one ad test account, one supplier audit desk, one design lab and one "
                              "buyer desk per category (panel:<category>, ads:<category>, audit:<category>, "
                              "lab:<category>, buyers:<category>), and 2 test market slots",
                "catalog": c["products"], "sizes": c["sizes"], "season": c["season"],
                "rules": dict(c["rules"], research_budget=self.budget()),
                "status": self.status(agent)}

    def status(self, agent):
        with self.lock:
            mine = [dict(self.public(t), has_result=t.get("result") is not None) for t in self.s["tests"]
                    if t["agent"] == agent]
            return {"announcements": self.announcements(),
                    "research_budget_usd": self.budget(), "committed_usd": round(self.committed(), 2),
                    "left_usd": round(self.budget() - self.committed(), 2),
                    "running": [self.public(t) for t in self.s["tests"] if t["status"] == "running"],
                    "queued": [self.public(t) for t in self.s["tests"] if t["status"] == "queued"],
                    "mine": mine, "ledger": [self.public(t) for t in self.s["tests"]]}

    def review(self, agent, ref):
        out = self.cli("review", payload={"repo": self.cfg["repo"], "ref": ref or "main"}, timeout=300)
        ok, msg = self.records(ref or "main")
        if isinstance(out, dict):
            out["research_records"] = {"ok": ok, "note": msg}
            if not ok:
                out["ok"] = False
        return out

    def records(self, ref):
        """The grader's honest records check (tests/verify.py, records_gate) on program/research.json at ref: every
        entry must be a finished test of this office, with the office's result unchanged."""
        ref = str(ref or "main")
        if ref.startswith("-") or not all(c.isalnum() or c in "._/@{}~^-" for c in ref) or len(ref) > 200:
            return False, f"{ref!r} is not a branch, tag or commit of the team repository"
        r = subprocess.run(["git", "--git-dir", self.cfg["repo"], "show", "--end-of-options", f"{ref}:program/research.json"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return True, "program/research.json is missing; no research results to check"
        try:
            data = json.loads(r.stdout)
        except ValueError as e:
            return False, f"program/research.json is not valid JSON: {e}"
        items = data.get("tests", []) if isinstance(data, dict) else data
        if not isinstance(items, list):
            return False, "program/research.json must be a list of {id, result}"
        canon = lambda x: json.dumps(x, sort_keys=True, separators=(",", ":"))
        with self.lock:
            by_id = {t["id"]: t for t in self.s["tests"]}
        for i, item in enumerate(items):
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                return False, f"program/research.json entry {i} has no id"
            t = by_id.get(item["id"])
            if t is None or t.get("status") != "done":
                return False, f"program/research.json entry {i}: {item['id']!r} is not a finished test of the office"
            if "result" in item and canon(item["result"]) != canon(t.get("result")):
                return False, (f"program/research.json entry {i}: the result of {item['id']} is not the office's result; "
                               f"copy it unchanged from test_result")
        return True, f"{len(items)} research results match the office's ledger"

    def economics(self, agent, ref):
        return self.cli("economics", payload={"repo": self.cfg["repo"], "ref": ref or "main"})

    def quote(self, agent, kind, target, params):
        return self.cli("quote", payload={"kind": kind, "target": target, "params": params or {}})

    def order(self, agent, kind, target, params, note, ref):
        q = self.quote(agent, kind, target, params)
        with self.lock:
            left = self.budget() - self.committed()
            if q["cost_usd"] > left + 1e-6:
                raise OfficeError(f"refused: the test costs ${q['cost_usd']:,.0f} and ${left:,.0f} of the research "
                                  f"budget is left")
            n = len(self.s["tests"]) + 1
            t = {"id": f"T{n:04d}", "agent": agent, "kind": kind, "target": q["target"], "params": params or {},
                 "ref": ref or "main", "facility": q["facility"], "cost_usd": q["cost_usd"], "minutes": q["minutes"],
                 "status": "queued", "ordered_at": self.now(), "started_at": None, "ends_at": None,
                 "note": str(note or "")[:300], "result": None}
            self.s["tests"].append(t)
            self.save()
            self.emit(agent, "test_ordered", test=t["id"], kind=kind, target=t["target"], cost_usd=t["cost_usd"])
            return {"test": t["id"], "status": "queued", "cost_usd": t["cost_usd"], "facility": t["facility"],
                    "minutes": t["minutes"], "research_left_usd": round(self.budget() - self.committed(), 2)}

    def result(self, agent, test_id):
        with self.lock:
            t = self.get(test_id)
            out = self.public(t)
            out["announcements"] = self.announcements()
            if t["status"] == "running":
                out["seconds_left"] = max(0, round(t["ends_at"] - self.now()))
            if t["agent"] != agent:
                out["result"] = "only the agent who ordered this test can read its result"
            elif t["status"] == "done":
                out["result"] = t["result"]
            elif t["status"] == "failed":
                out["result"] = None
                out["error"] = t.get("error")
            return out

    def record(self, agent, test_id):
        with self.lock:
            t = self.get(test_id)
            if t["agent"] != agent:
                raise OfficeError("only the agent who ordered this test can read its record")
            if t["kind"] != "test_market" or t["status"] != "done":
                raise OfficeError("a record exists only for a finished test_market")
        with open(os.path.join(self.record_dir, f"{test_id}.json")) as f:
            return json.load(f)

    def cancel(self, agent, test_id):
        with self.lock:
            t = self.get(test_id)
            if t["agent"] != agent:
                raise OfficeError("only the agent who ordered a test can cancel it")
            if t["status"] != "queued":
                raise OfficeError(f"only a queued test can be cancelled; this one is {t['status']}")
            t["status"] = "cancelled"
            self.save()
            self.emit(agent, "test_cancelled", test=test_id)
            return {"test": test_id, "status": "cancelled", "refund_usd": t["cost_usd"],
                    "research_left_usd": round(self.budget() - self.committed(), 2)}


def register(board, api, mcp):
    from fastapi import Header, HTTPException
    from mcp.server.mcpserver import Context
    from mcp.server.mcpserver.exceptions import ToolError

    office = MarketOffice(board)

    def clock():
        while True:
            time.sleep(2)
            try:
                office.tick()
            except Exception as e:  # keep the clock alive, record the failure
                office.emit(None, "clock_error", error=str(e)[:500])

    threading.Thread(target=clock, daemon=True, name="market-clock").start()

    def bearer(value):
        return value[7:].strip() if value and value.lower().startswith("bearer ") else None

    def run(ctx: Context, fn, *args):
        try:
            agent = board.authenticate(bearer((ctx.headers or {}).get("authorization")))
            return fn(agent, *args)
        except Exception as e:
            raise ToolError(str(e)) from None

    @mcp.tool()
    def office_info(ctx: Context) -> dict:
        """The market office: the research budget, every test kind with its cost rule, what it needs and what it
        reveals, the facilities, the full public catalog of 30 products, the fee and cost rules, and the status."""
        return run(ctx, office.info)

    @mcp.tool()
    def office_status(ctx: Context) -> dict:
        """Research budget committed and left, tests running and queued, your own tests, and the public ledger of
        every test the team ordered."""
        return run(ctx, office.status)

    @mcp.tool()
    def plan_review(ctx: Context, ref: str = "main") -> dict:
        """Check company/plan.yaml at ref (a branch or commit of the team repository, default main), check that
        program/research.json holds only the office's own results, unchanged, and run ops/ for 14 days in a made-up
        market to check that it starts, answers and returns valid actions. Free. It says nothing about how well the
        company would do."""
        return run(ctx, office.review, ref)

    @mcp.tool()
    def unit_economics(ctx: Context, ref: str = "main") -> dict:
        """Landed cost, fees, fulfillment, storage and return costs per unit for each product in company/plan.yaml
        at ref. Free and instant."""
        return run(ctx, office.economics, ref)

    @mcp.tool()
    def company_status(ctx: Context) -> dict:
        """The live company now: the phase (launch, live or ended), when the launch phase ends, how long a month
        takes, how many months have run and when the next one starts, the latest day of the books, and a summary of
        each month (the commit that ran, whether the software stopped, revenue, cash). Free."""
        return run(ctx, office.company_status)

    @mcp.tool()
    def company_books(ctx: Context, since_day: int = None, until_day: int = None) -> dict:
        """The live company's own books, day by day, for days since_day to until_day (at most 120 days; default the
        last 30): cash, debt, and per product price, ads, stock, rating, reviews, rival prices and yesterday's
        results, with the news and the software's notes. Free."""
        return run(ctx, office.company_books, since_day, until_day)

    @mcp.tool()
    def quote_test(kind: str, target: str, ctx: Context, params: dict = None) -> dict:
        """Price a test without ordering it: cost in USD, duration in real minutes and facility. kind is survey,
        ad_test, supplier_audit, concept_test, subscription_test, bundle_test, buyer_calls, market_report or test_market. target is a catalog product id such as "k1" (for test_market,
        "company"). params as office_info describes for the kind."""
        return run(ctx, office.quote, kind, target, params or {})

    @mcp.tool()
    def order_test(kind: str, target: str, ctx: Context, params: dict = None, note: str = "", ref: str = "main") -> dict:
        """Order a test. It is paid now from the team's research budget, queued at its facility, and run first come
        first served. A test_market runs company/plan.yaml and ops/ as they are at ref (default main) when it starts.
        Returns the test id. Poll test_result(test_id)."""
        return run(ctx, office.order, kind, target, params or {}, note, ref)

    @mcp.tool()
    def test_result(test_id: str, ctx: Context) -> dict:
        """A test's status. When it is done, the agent who ordered it gets the full result."""
        return run(ctx, office.result, test_id)

    @mcp.tool()
    def cancel_test(test_id: str, ctx: Context) -> dict:
        """Cancel a test you ordered that is still queued, for a full refund."""
        return run(ctx, office.cancel, test_id)

    def rest(authorization, fn, *args):
        try:
            agent = board.authenticate(bearer(authorization))
        except Exception as e:
            raise HTTPException(401, str(e)) from None
        try:
            return fn(agent, *args)
        except OfficeError as e:
            raise HTTPException(404, str(e)) from None

    @api.post("/admin/market/started")
    def started(authorization: str = Header(None)):
        """For keepalive.py: the agents have started, so the live company's clock starts now (once)."""
        api.state.require_admin(authorization)
        return office.run_started()

    @api.get("/admin/market/announcement")
    def announcement(authorization: str = Header(None)):
        """For keepalive.py: the mid-run announcement, once it is made."""
        api.state.require_admin(authorization)
        return {"announcements": office.announcements()}

    @api.get("/market/status")
    def status(authorization: str = Header(None)):
        """office_status() over REST, so scripts can watch the queue without model calls."""
        return rest(authorization, office.status)

    @api.get("/market/result/{test_id}")
    def result(test_id: str, authorization: str = Header(None)):
        return rest(authorization, office.result, test_id)

    @api.get("/market/record/{test_id}")
    def record(test_id: str, authorization: str = Header(None)):
        """The full daily record of a finished test_market, for the agent who ordered it."""
        return rest(authorization, office.record, test_id)

    return office
