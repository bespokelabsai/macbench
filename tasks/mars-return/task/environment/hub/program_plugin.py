"""Program office board plugin: the R&D budget, the test facilities, and the ledger of every test.

Load it with `--ek board_plugins=<path to this file>`. It runs inside the board on the hub, so it uses
only the standard library and the board's own packages. Every quote and every test outcome is
computed by `python -m designlang.hidden.office` with the task image's Python. That module compiles the
team's SysML model (design/**/*.sysml on a branch of the team repository) with the hidden compiler and runs
the hidden simulator (marsflight). Both are on the hub only: marsflight_hub_plugin.py, which make_hub_plugin.py
generates, unpacks them there. A team never sees either.

Tests name their target by its part path in the model, e.g. "vehicle.S1.mainEngines". The office
compiles the model when a test is ordered and keeps the compiled target, so a test measures the design
as it was when ordered. A team reads only what a test measures (absolute values of the built article);
the simulator's own result, with its model values, goes in the ledger the verifier reads.

The secret that draws the hidden as-built truth of the hardware, and the ledger, live in
/var/lib/marsflight-hub on the hub, out of the agents' reach. The verifier reads both from there.

Rules:
- A test is paid when it is ordered, from the team's shared R&D budget. Orders that would overspend
  it are refused. A queued test can be cancelled for a refund.
- Each facility runs one test at a time (there are two engine stands), first come first served.
  A test takes real minutes, and its result is ready when it ends.
- The full result goes only to the agent who ordered it. Everyone can see the ledger: who ordered
  what, for which part key, at what cost, and whether it is queued, running or done.
"""
# No `from __future__ import annotations`: the MCP server reads the tool signatures, and Context is
# imported inside register(), so the annotations must be real objects, not strings.
import importlib.util
import json
import os
import secrets
import subprocess
import threading
import time


def _load_roles():
    """roles.py sits beside this file; the board loads plugins by path, so import it by path too. On a
    hub only the plugin files are uploaded, so marsflight_hub_plugin.py unpacks roles.py and names it."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "roles.py")
    if not os.path.exists(path):
        path = os.environ.get("MARSFLIGHT_ROLES_PY", path)
    spec = importlib.util.spec_from_file_location("mars_roles", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


R = _load_roles()


SIM_KIND = "mission_sim"   # designlang/hidden/office.py runs it; see _start_sim


def _flag(name) -> bool:
    return os.environ.get("MARSFLIGHT_" + name, "1").strip().lower() in ("1", "true", "yes", "on")


DEFAULTS = {
    "python": "/opt/conda/envs/marsflight/bin/python3",
    "pythonpath": "/opt/marsflight:/opt/designlang",
    "repo": "/srv/git/team.git",   # the team repository on the hub
    "state": "/var/lib/marsflight-hub",
    "time_scale": 1.0,        # multiplies every test's duration (tests use less than 1)
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    for k in DEFAULTS:
        v = os.environ.get("MARSFLIGHT_PROGRAM_" + k.upper())
        if v is not None:
            cfg[k] = type(DEFAULTS[k])(v)
    return cfg


class ProgramError(Exception):
    pass


PUBLIC = ("id", "agent", "kind", "key", "target_name", "facility", "cost_usd", "minutes", "status", "ordered_at",
          "started_at", "ends_at", "note")


class ProgramOffice:
    """All program state and rules. Thread safe. Tools and REST routes are thin wrappers over this."""

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
                json.dump({"secret": secrets.token_hex(32)}, f)
        self.path = os.path.join(self.cfg["state"], "program.json")
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.s = json.load(f)
        else:
            self.s = {"tests": [], "created_at": self.now()}
        for t in self.s["tests"]:
            # a mission simulation's thread does not survive a restart of the hub: fly it again from the queue
            if t["kind"] == SIM_KIND and t["status"] == "running" and t.get("outcome") is None:
                t.update(status="queued", facility=None, started_at=None, ends_at=None)
        self._catalog = None
        # subsystem roles (MARSFLIGHT_ROLES). failure investigations
        # (MARSFLIGHT_INVESTIGATION). Both off by default; off, nothing below changes behavior.
        self.roles_on = _flag("ROLES")
        self.investigation_on = _flag("INVESTIGATION")
        self.s.setdefault("investigations", [])
        self.roles = R.RoleBook(self.s.setdefault("roles", {}), self._team(), self.now) if self.roles_on else None

    # -- plumbing --------------------------------------------------------

    def cli(self, *args, payload=None, timeout=600) -> dict:
        env = dict(os.environ, PYTHONPATH=self.cfg["pythonpath"])
        p = subprocess.run([self.cfg["python"], "-m", "designlang.hidden.office", *args], input=json.dumps(payload or {}),
                           capture_output=True, text=True, timeout=timeout, env=env)
        try:
            out = json.loads((p.stdout or "").strip().splitlines()[-1])
        except Exception:
            raise ProgramError(f"program office error: {(p.stderr or p.stdout).strip()[-800:]}") from None
        if isinstance(out, dict) and out.get("error"):
            raise ProgramError(out["error"])
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

    def emit(self, agent, op, **fields):
        try:
            self.board._emit("program", agent, op=op, **fields)
        except Exception:
            pass

    def budget(self) -> float:
        return float(self.catalog()["budgets"]["rnd_budget_usd"])

    def committed(self) -> float:
        return (sum(t["cost_usd"] for t in self.s["tests"] if t["status"] != "cancelled")
                + sum(i.get("cost_usd", 0.0) for i in self.s.get("investigations", [])))

    # -- roles ------------------------------------------------

    def _team(self) -> list:
        try:
            return list(self.board.agents())
        except Exception:
            return []

    def touch(self, agent):
        """Any call by an agent counts as activity for the 30-minute take-over rule."""
        if self.roles is not None and agent:
            with self.lock:
                self.roles.team = self._team() or self.roles.team
                self.roles.touch(agent)

    def design_files(self, ref) -> dict:
        """design/**/*.sysml at ref in the team repository, as {path: text}."""
        repo, ref = self.cfg["repo"], str(ref or "main")
        ls = subprocess.run(["git", "-C", repo, "ls-tree", "-r", "--name-only", ref, "--", "design/"],
                            capture_output=True, text=True, timeout=60)
        if ls.returncode != 0:
            raise ProgramError(f"cannot read design/ at {ref}: {ls.stderr.strip()[-300:]}")
        out = {}
        for p in ls.stdout.split():
            if p.endswith(".sysml"):
                show = subprocess.run(["git", "-C", repo, "show", f"{ref}:{p}"], capture_output=True, text=True, timeout=60)
                out[p] = show.stdout
        return out

    def role_of(self, target, ref) -> str:
        if self.roles is None:
            return None
        return R.target_owner(target, self.design_files(ref))

    def _roles_call(self, fn, *args):
        with self.lock:
            self.roles.team = self._team() or self.roles.team
            try:
                out = fn(*args)
            except R.RoleError as e:
                raise ProgramError(str(e)) from None
            self.save()
            self.emit(args[0], "roles", roles=self.roles.view()["roles"])
            return out

    def claim_role(self, agent, role) -> dict:
        self._need_roles()
        return self._roles_call(self.roles.claim, agent, role)

    def release_role(self, agent, role) -> dict:
        self._need_roles()
        return self._roles_call(self.roles.release, agent, role)

    def hand_over_role(self, agent, role, to) -> dict:
        self._need_roles()
        return self._roles_call(self.roles.hand_over, agent, role, to)

    def roles_status(self, agent, ref="main") -> dict:
        self._need_roles()
        d = self.roles.view(agent)
        try:
            d["open_file_problems"] = R.open_file_violations(self.design_files(ref))
        except ProgramError as e:
            d["open_file_problems"] = [str(e)]
        return d

    def _need_roles(self):
        if self.roles is None:
            raise ProgramError("roles are off in this program (MARSFLIGHT_ROLES is not set)")

    def _may_see(self, agent, t) -> bool:
        if self.roles is not None and t.get("role"):
            return self.roles.owner(t["role"]) == agent
        return t["agent"] == agent

    # -- failure investigations ------------------------------

    def _investigate_cli(self, payload) -> dict:
        env = dict(os.environ, PYTHONPATH=self.cfg["pythonpath"])
        p = subprocess.run([self.cfg["python"], "-m", "marsflight.program.investigation"], input=json.dumps(payload),
                           capture_output=True, text=True, timeout=120, env=env)
        try:
            out = json.loads((p.stdout or "").strip().splitlines()[-1])
        except Exception:
            raise ProgramError(f"investigation error: {(p.stderr or p.stdout).strip()[-500:]}") from None
        if isinstance(out, dict) and out.get("error"):
            raise ProgramError(out["error"])
        return out

    def _failed_test(self, agent, test_id) -> dict:
        if not self.investigation_on:
            raise ProgramError("investigations are off in this program (MARSFLIGHT_INVESTIGATION is not set)")
        t = self.find(test_id)
        if t["status"] != "done" or not (t.get("outcome") or {}).get("failure"):
            raise ProgramError(f"{test_id} has not failed; only a failed test can be investigated")
        if not self._may_see(agent, t):
            raise ProgramError(f"only the agent who may read {test_id}'s result can investigate it")
        return t

    def _secret(self) -> str:
        with open(self.secret_file) as f:
            return json.load(f)["secret"]

    def investigate(self, agent, test_id) -> dict:
        """Free: synthetic telemetry around the failure, and the price of a root cause."""
        t = self._failed_test(agent, test_id)
        team = dict(t.get("team_result") or {})
        tel = self._investigate_cli({"op": "telemetry", "kind": t["kind"], "raw_failure": t["outcome"]["failure"],
                                     "team": team, "test_id": t["id"], "secret": self._secret()})
        price = self._investigate_cli({"op": "price", "kind": t["kind"], "cost_usd": t["cost_usd"]})["price_usd"]
        tel["root_cause_price_usd"] = price
        tel["root_cause_bought"] = any(i["test"] == t["id"] for i in self.s["investigations"])
        return tel

    def buy_root_cause(self, agent, test_id) -> dict:
        """Paid from the R&D budget: the cause class, whether it is a design flaw, and what to change."""
        t = self._failed_test(agent, test_id)
        for i in self.s["investigations"]:
            if i["test"] == t["id"]:
                return dict(i["finding"], cost_usd=0.0, note="already bought")
        price = self._investigate_cli({"op": "price", "kind": t["kind"], "cost_usd": t["cost_usd"]})["price_usd"]
        finding = self._investigate_cli({"op": "root_cause", "kind": t["kind"], "raw_failure": t["outcome"]["failure"]})
        with self.lock:
            left = self.budget() - self.committed()
            if price > left + 1e-6:
                raise ProgramError(f"a root cause costs ${price / 1e6:.2f}M but only ${left / 1e6:.2f}M of the R&D budget is left")
            self.s["investigations"].append({"test": t["id"], "agent": agent, "cost_usd": price, "at": self.now(),
                                             "finding": finding})
            self.save()
            self.emit(agent, "root_cause_bought", test=t["id"], cost_usd=price)
        return dict(finding, cost_usd=price, rnd_left_usd=round(self.budget() - self.committed(), 0))

    def find(self, test_id) -> dict:
        for t in self.s["tests"]:
            if t["id"] == str(test_id):
                return t
        raise ProgramError(f"no test {test_id!r}")

    def source(self, ref) -> dict:
        return {"repo": self.cfg["repo"], "ref": str(ref or "main")}

    def public(self, t) -> dict:
        d = {k: t.get(k) for k in PUBLIC}
        if t.get("role"):
            d["role"] = t["role"]
        if t["status"] == "done":
            d["failed"] = bool((t.get("outcome") or {}).get("failure"))
        return d

    # -- the clock -------------------------------------------------------

    def tick(self):
        with self.lock:
            now = self.now()
            changed = False
            for t in self.s["tests"]:
                if t["status"] == "running" and t["kind"] == SIM_KIND and t.get("outcome") is None:
                    continue   # a mission simulation is done when its flights are, not at a set time
                if t["status"] == "running" and now >= t["ends_at"]:
                    t["status"] = "done"
                    changed = True
                    self.emit(t["agent"], "test_done", test=t["id"], kind=t["kind"], key=t["key"],
                              failed=bool((t.get("outcome") or {}).get("failure")))
            facilities = self.catalog()["facilities"]
            busy = {t["facility"] for t in self.s["tests"] if t["status"] == "running"}
            for inst, ftype in sorted(facilities.items()):
                if inst in busy:
                    continue
                queued = [t for t in self.s["tests"] if t["status"] == "queued" and t["facility_type"] == ftype]
                if not queued:
                    continue
                t = min(queued, key=lambda x: x["ordered_at"])
                done = [x for x in self.s["tests"] if x["status"] == "done"]
                if t["kind"] == SIM_KIND:
                    self._start_sim(t, inst, done, now)
                    busy.add(inst)
                    changed = True
                    continue
                try:
                    r = self.cli("run", payload={"kind": t["kind"], "spec": t["spec"], "params": t["params"],
                                                 "test_id": t["id"], "ledger": done, "secret_file": self.secret_file})
                    t["outcome"], t["team_result"] = r["raw"], r["team"]
                except Exception as e:
                    t["status"] = "cancelled"
                    t["cancel_reason"] = f"the test could not run: {e}"
                    changed = True
                    continue
                t["status"], t["facility"] = "running", inst
                t["started_at"], t["ends_at"] = now, now + 60.0 * t["minutes"] * float(self.cfg["time_scale"])
                busy.add(inst)
                changed = True
                self.emit(t["agent"], "test_started", test=t["id"], facility=inst)
            if changed:
                self.save()

    def _start_sim(self, t, inst, done, now):
        """A mission simulation flies for real minutes: run it in a thread, outside the lock, and mark the test done
        when its flights end (tick), or cancelled and refunded if it could not run."""
        t["status"], t["facility"] = "running", inst
        t["started_at"], t["ends_at"] = now, now + 60.0 * t["minutes"]
        self.emit(t["agent"], "test_started", test=t["id"], facility=inst)
        payload = {"kind": t["kind"], "spec": t["spec"], "params": t["params"], "test_id": t["id"],
                   "ledger": done, "secret_file": self.secret_file}

        def work():
            try:
                r = self.cli("run", payload=payload, timeout=60.0 * t["minutes"] + 600)
                outcome, team, reason = r["raw"], r["team"], None
            except Exception as e:  # noqa: BLE001
                outcome, team, reason = None, None, f"the simulation could not run: {e}"
            with self.lock:
                if reason:
                    t["status"], t["cancel_reason"] = "cancelled", reason
                    self.emit(t["agent"], "test_cancelled", test=t["id"])
                else:
                    t["outcome"], t["team_result"], t["ends_at"] = outcome, team, self.now()
                self.save()
            self.tick()

        threading.Thread(target=work, name=f"sim-{t['id']}", daemon=True).start()

    # -- what agents can do ----------------------------------------------

    def info(self, agent) -> dict:
        c = self.catalog()
        return {"budgets": c["budgets"], "facilities": c["facilities"], "tests": c["tests"],
                "catalog_engines": c["catalog_engines"],
                "rules": ["Tests are paid from the shared R&D budget when ordered; orders that would overspend it are refused.",
                          "A queued test can be cancelled for a refund; a running one cannot.",
                          "Each facility runs one test at a time, first come first served. There are two engine stands.",
                          "Results go only to the agent who ordered the test. The ledger of who ordered what is public.",
                          "A test's target is a part path in the team's SysML model, e.g. \"vehicle.S1.mainEngines\". The office reads design/**/*.sysml on the branch you name (default main) when you order, and tests that design.",
                          "Results are tied to the exact part design (its key). Change the part in the model and it is a new design with no test history.",
                          "design_review checks that the model compiles and could be built. cost_report prices it. Both are free and instant.",
                          "Save the results you get to program/test_results.json on main so teammates can use them. The verifier uses this ledger, not that file."],
                "status": self.status(agent)}

    def status(self, agent) -> dict:
        with self.lock:
            now = self.now()
            running = [{"facility": t["facility"], "test": t["id"], "kind": t["kind"], "agent": t["agent"],
                        "seconds_left": round(max(0.0, t["ends_at"] - now), 1)} for t in self.s["tests"] if t["status"] == "running"]
            queued = [{"test": t["id"], "kind": t["kind"], "agent": t["agent"], "facility_type": t["facility_type"],
                       "minutes": t["minutes"]} for t in sorted(self.s["tests"], key=lambda x: x["ordered_at"]) if t["status"] == "queued"]
            b = self.budget()
            c = self.committed()
            return {"rnd_budget_usd": b, "rnd_committed_usd": round(c, 0), "rnd_left_usd": round(b - c, 0),
                    "running": running, "queued": queued,
                    "your_tests": [self.public(t) for t in self.s["tests"] if t["agent"] == agent],
                    "tests_done": sum(1 for t in self.s["tests"] if t["status"] == "done"),
                    "ledger": [self.public(t) for t in self.s["tests"]][-200:]}

    def _quote(self, kind, target, params, ref) -> dict:
        return self.cli("quote", payload={"source": self.source(ref), "kind": kind, "target": target, "params": params or {}})

    def quote(self, agent, kind, target, params, ref="main") -> dict:
        return {k: v for k, v in self._quote(kind, target, params, ref).items() if k != "spec"}

    def review(self, agent, ref="main") -> dict:
        return self.cli("review", payload={"source": self.source(ref)})

    def cost(self, agent, ref="main") -> dict:
        return self.cli("cost", payload={"source": self.source(ref)})

    def order(self, agent, kind, target, params, note="", ref="main") -> dict:
        role = self.role_of(target, ref)
        if self.roles is not None:
            try:
                self.roles.require(agent, role, f"{target}")
            except R.RoleError as e:
                raise ProgramError(str(e)) from None
        q = self._quote(kind, target, params, ref)
        spec = q.pop("spec")
        with self.lock:
            left = self.budget() - self.committed()
            if q["cost_usd"] > left + 1e-6:
                raise ProgramError(f"this test costs ${q['cost_usd'] / 1e6:.2f}M but only ${left / 1e6:.2f}M of the R&D budget is left")
            tid = f"T{len(self.s['tests']) + 1:04d}"
            # the quote's kind is the test's one name (an old alias such as mass_properties orders "weighing")
            t = {"id": tid, "agent": agent, "kind": q.get("kind") or kind, "key": q["key"], "target_name": str(target)[:200], "ref": str(ref or "main"),
                 "facility_type": q["facility"], "facility": None, "spec": spec, "params": params or {},
                 "cost_usd": q["cost_usd"], "minutes": q["minutes"], "status": "queued", "ordered_at": self.now(),
                 "started_at": None, "ends_at": None, "note": str(note or "")[:300]}
            if role:
                t["role"] = role
            self.s["tests"].append(t)
            self.save()
            self.emit(agent, "test_ordered", test=tid, kind=kind, key=q["key"], cost_usd=q["cost_usd"])
        self.tick()
        t = self.find(tid)
        return {"test": tid, "status": t["status"], "cost_usd": t["cost_usd"], "minutes": t["minutes"], "key": t["key"],
                "rnd_left_usd": round(self.budget() - self.committed(), 0),
                "queue_ahead": sum(1 for x in self.s["tests"] if x["status"] == "queued" and x["facility_type"] == t["facility_type"]
                                   and x["ordered_at"] < t["ordered_at"])}

    def result(self, agent, test_id) -> dict:
        with self.lock:
            t = self.find(test_id)
            d = self.public(t)
            if t["status"] == "running":
                d["seconds_left"] = round(max(0.0, t["ends_at"] - self.now()), 1)
            if t["status"] == "done":
                if self._may_see(agent, t):
                    d["result"] = t.get("team_result") or t["outcome"]
                    if self.investigation_on and d.get("failed"):
                        d["investigation"] = "this test failed: investigate(test_id) returns telemetry around the failure for free"
                elif self.roles is not None and t.get("role"):
                    d["result"] = (f"only the owner of the {t['role']} subsystem "
                                   f"({self.roles.owner(t['role']) or 'nobody now'}) can see this result")
                else:
                    d["result"] = f"only {t['agent']}, who ordered this test, can see its result"
            if t.get("cancel_reason"):
                d["cancel_reason"] = t["cancel_reason"]
            return d

    # -- shared results -------------------------------------------

    RESULTS_BRANCH = "results"
    RESULTS_FILE = "program/test_results.json"

    def share_result(self, agent, test_id) -> dict:
        """Add a done test's result, exactly as the office gave it, to program/test_results.json on the team repo's
        results branch, which only the office writes. The file is rebuilt from the office's own list every time, so it
        holds only results the office gave and needs no records check (teams kept results out of main for fear
        that one mistyped digit there would zero the mission)."""
        with self.lock:
            t = self.find(test_id)
            if t["status"] != "done":
                raise ProgramError(f"{test_id} is {t['status']}; only a done test's result can be shared")
            if not self._may_see(agent, t):
                raise ProgramError(f"only the agent who may read {test_id}'s result can share it")
            shared = self.s.setdefault("shared", [])
            if t["id"] not in shared:
                shared.append(t["id"])
            entries = [self.find(i).get("team_result") or self.find(i)["outcome"] for i in shared]
            commit = self._commit_results(entries, f"{t['id']} shared by {agent}")
            self.save()
            self.emit(agent, "result_shared", test=t["id"])
            return {"test": t["id"], "branch": self.RESULTS_BRANCH, "file": self.RESULTS_FILE, "commit": commit,
                    "results_on_branch": len(entries),
                    "read_it": f"git fetch origin {self.RESULTS_BRANCH} && git show "
                               f"origin/{self.RESULTS_BRANCH}:{self.RESULTS_FILE}"}

    def _commit_results(self, entries, message) -> str:
        """Commit the results file onto the results branch of the bare team repo, with git plumbing (no work tree).
        The ref moves only from the tip the commit was made on; a push to the branch in between is built on."""
        repo, ref = self.cfg["repo"], "refs/heads/" + self.RESULTS_BRANCH
        env = dict(os.environ, GIT_AUTHOR_NAME="program-office", GIT_AUTHOR_EMAIL="program-office@team.local",
                   GIT_COMMITTER_NAME="program-office", GIT_COMMITTER_EMAIL="program-office@team.local")

        def git(*args, stdin=None):
            p = subprocess.run(["git", "-c", "safe.directory=*", "--git-dir", repo, *args], input=stdin,
                               capture_output=True, text=True, timeout=60, env=env)
            if p.returncode != 0:
                raise ProgramError(f"cannot write the {self.RESULTS_BRANCH} branch: {p.stderr.strip()[-300:]}")
            return p.stdout.strip()
        blob = git("hash-object", "-w", "--stdin", stdin=json.dumps(entries, indent=1) + "\n")
        sub = git("mktree", stdin=f"100644 blob {blob}\t{os.path.basename(self.RESULTS_FILE)}\n")
        tree = git("mktree", stdin=f"040000 tree {sub}\t{os.path.dirname(self.RESULTS_FILE)}\n")
        for _ in range(3):
            tip = subprocess.run(["git", "-c", "safe.directory=*", "--git-dir", repo, "rev-parse", "-q", "--verify", ref],
                                 capture_output=True, text=True).stdout.strip()
            commit = git("commit-tree", tree, *(["-p", tip] if tip else []), "-m", message)
            try:
                git("update-ref", ref, commit, tip or "0" * 40)
                return commit
            except ProgramError:
                continue
        raise ProgramError(f"cannot write the {self.RESULTS_BRANCH} branch: it kept moving")

    def cancel(self, agent, test_id) -> dict:
        with self.lock:
            t = self.find(test_id)
            if not self._may_see(agent, t):
                raise ProgramError(f"only {self.roles.owner(t['role']) if self.roles is not None and t.get('role') else t['agent']} "
                                   f"can cancel {test_id}")
            if t["status"] != "queued":
                raise ProgramError(f"{test_id} is {t['status']}; only queued tests can be cancelled")
            t["status"] = "cancelled"
            t["cancel_reason"] = "cancelled by the agent who ordered it"
            self.save()
            self.emit(agent, "test_cancelled", test=test_id)
            return {"test": test_id, "status": "cancelled", "refund_usd": t["cost_usd"],
                    "rnd_left_usd": round(self.budget() - self.committed(), 0)}


def register(board, api, mcp):
    from fastapi import Header, HTTPException
    from mcp.server.mcpserver import Context
    from mcp.server.mcpserver.exceptions import ToolError

    office = ProgramOffice(board)

    def clock():
        while True:
            time.sleep(2)
            try:
                office.tick()
            except Exception as e:  # keep the clock alive, record the failure
                office.emit(None, "clock_error", error=str(e)[:500])

    threading.Thread(target=clock, daemon=True, name="program-clock").start()

    def bearer(value):
        return value[7:].strip() if value and value.lower().startswith("bearer ") else None

    def run(ctx: Context, fn, *args):
        try:
            agent = board.authenticate(bearer((ctx.headers or {}).get("authorization")))
            office.touch(agent)
            return fn(agent, *args)
        except Exception as e:
            raise ToolError(str(e)) from None

    @mcp.tool()
    def program_info(ctx: Context) -> dict:
        """The program office: the R&D budget (the hardware cost is priced by cost_report), every test kind with
        its facility, what it needs as a target and what it reveals, the test facilities, the flight-proven
        engines you can buy, the rules, and the current status."""
        return run(ctx, office.info)

    @mcp.tool()
    def program_status(ctx: Context) -> dict:
        """R&D budget committed and left, tests running (with seconds left) and queued, your own tests, and the
        public ledger of every test the team ordered."""
        return run(ctx, office.status)

    @mcp.tool()
    def design_review(ctx: Context, ref: str = "main") -> dict:
        """Compile the SysML model (design/**/*.sysml at ref, a branch or commit of the team repository, default
        main) and check that it could be built: tank walls at proof pressure, engine envelopes, cooling, what
        fits in the fairing and the aeroshell. Lists every problem, and every part path a test can target.
        Free and instant. It reports no mass or performance; tests measure those."""
        return run(ctx, office.review, ref)

    @mcp.tool()
    def cost_report(ctx: Context, ref: str = "main") -> dict:
        """The hardware cost of the model at ref (default main): the total to the nearest $0.1M, each element's
        cost to the nearest $1M, the room left in the hardware budget, and whether it is over budget (exact). A
        design over the hardware budget scores 0. Free and instant."""
        return run(ctx, office.cost, ref)

    @mcp.tool()
    def quote_test(kind: str, target: str, ctx: Context, params: dict = None, ref: str = "main") -> dict:
        """Price a test without ordering it: its part key, cost in USD, duration in real minutes and facility.
        kind is one of the test kinds in program_info. target is a part path in the SysML model, e.g.
        "vehicle.S1.mainEngines" for an engine hot fire, "vehicle.S2" for a stage, "vehicle" for the launch
        stack in the wind tunnel (design_review lists them). ref is the branch or commit to read, default main."""
        return run(ctx, office.quote, kind, target, params or {}, ref)

    @mcp.tool()
    def order_test(kind: str, target: str, ctx: Context, params: dict = None, note: str = "", ref: str = "main") -> dict:
        """Order a test on the part at target (a part path, as in quote_test) of the model at ref (default
        main). It is paid now from the team's R&D budget, queued at its facility, and run first come first
        served. The design is read when you order. Returns the test id. Poll test_result(test_id)."""
        return run(ctx, office.order, kind, target, params or {}, note, ref)

    @mcp.tool()
    def test_result(test_id: str, ctx: Context) -> dict:
        """A test's status. When it is done, the agent who ordered it gets the full result: what was measured,
        any failure, and the part's reliability after the test."""
        return run(ctx, office.result, test_id)

    @mcp.tool()
    def share_result(test_id: str, ctx: Context) -> dict:
        """Share a done test's result with the team, exactly as the office gave it: the office adds it to
        program/test_results.json on the team repository's `results` branch, which only the office writes. Read it
        with `git fetch origin results && git show origin/results:program/test_results.json`. Only the agent who
        may read the result can share it."""
        return run(ctx, office.share_result, test_id)

    @mcp.tool()
    def cancel_test(test_id: str, ctx: Context) -> dict:
        """Cancel a test you ordered that is still queued, for a full refund."""
        return run(ctx, office.cancel, test_id)

    if office.roles is not None:
        @mcp.tool()
        def claim_role(role: str, ctx: Context) -> dict:
            """Claim a subsystem role (launcher, propulsion, entry_landing, surface, ascent_return, earth_entry).
            Only a role's owner may order or cancel tests on its parts, read their results, and push to
            design/<role>/. One agent may hold at most ceil(6 / team size) roles. A role whose owner has been
            idle 30 minutes can be claimed by anyone under the cap."""
            return run(ctx, office.claim_role, role)

        @mcp.tool()
        def release_role(role: str, ctx: Context) -> dict:
            """Give up a role you hold. Anyone may then claim it."""
            return run(ctx, office.release_role, role)

        @mcp.tool()
        def hand_over_role(role: str, to: str, ctx: Context) -> dict:
            """Hand a role you hold to another agent (who must be under the cap)."""
            return run(ctx, office.hand_over_role, role, to)

        @mcp.tool()
        def roles_status(ctx: Context, ref: str = "main") -> dict:
            """Who holds each role, the cap per agent, how long each agent has been idle, and any problem in
            the open design files at ref (an open file may place parts but not redefine an owned part)."""
            return run(ctx, office.roles_status, ref)

    if office.investigation_on:
        @mcp.tool()
        def investigate(test_id: str, ctx: Context) -> dict:
            """For a failed test whose result you may read: free synthetic telemetry from 10 s before the
            failure to 2 s after, each channel in percent of nominal, and the price of a root cause."""
            return run(ctx, office.investigate, test_id)

        @mcp.tool()
        def buy_root_cause(test_id: str, ctx: Context) -> dict:
            """Pay from the R&D budget for the root cause of a failed test: the cause class, whether it is a
            design flaw or a random failure, and what to change."""
            return run(ctx, office.buy_root_cause, test_id)

    @api.get("/program/status")
    def status(authorization: str = Header(None)):
        """program_status() over REST, so scripts can watch the queue without model calls."""
        try:
            agent = board.authenticate(bearer(authorization))
        except Exception as e:
            raise HTTPException(401, str(e)) from None
        office.touch(agent)
        return office.status(agent)

    @api.get("/program/result/{test_id}")
    def result(test_id: str, authorization: str = Header(None)):
        try:
            agent = board.authenticate(bearer(authorization))
        except Exception as e:
            raise HTTPException(401, str(e)) from None
        office.touch(agent)
        try:
            return office.result(agent, test_id)
        except ProgramError as e:
            raise HTTPException(404, str(e)) from None

    return office
