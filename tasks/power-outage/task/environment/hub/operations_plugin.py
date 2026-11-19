"""National grid operations board plugin: control-room seats, each seat's private data, and practice restorations.

Load it with `--ek board_plugins=<this file>,keepalive_plugin.py`. It runs inside the board on the hub, so it uses
only the standard library and the board's own packages. The simulator (gridsim) is in the image at /opt/blackstart,
root-only: the office on the hub runs it, and agents, who work as an unprivileged user, cannot read it.

Seats: the grid has 16 control areas, a01 to a16, and every run has the same 16 seats whatever the team size. Seat
a is held by the agent at position (a - 1) mod N in the team's names sorted alphabetically, so a team of 16 has one
area each and a team of one holds all 16. Holding an area means reading its private data (area_data) and its part of
every practice log. Anyone may write any area's controller.

Drills: practice_run plays the team's controllers (the repository at a ref) on a practice scenario through the same
runner the grader uses: one process per area, isolated from each other and from the simulator. Practice scenarios
come from the same distribution as the graded ones but never coincide with them. The office runs
BLACKSTART_PRACTICE_SLOTS drills at once, first come first served, the same for a team of any size. The team
summary of a run goes to everyone; each area's event log goes only to the agent holding that area.

The drill pool: the true simulator is rationed. The whole team has one pool of BLACKSTART_PRACTICE_POOL drills for
the whole run, the same at every team size, and it never refills. A drill is priced by the area-hours it simulates:
all 16 areas for the full 16 hours cost 1.0, `only_areas` and `hours` cost proportionally less (two areas for the
first 4 hours: 2 x 4 / 256). The price is charged when the drill is ordered; an order the pool cannot cover is
refused whole; a drill the office fails to run (a simulator error, or the hub restarting under it) is refunded. A
drill also holds its slot for at least its price times BLACKSTART_DRILL_MINUTES, so turnaround follows what is
simulated and cheap drills cannot buy speed.

State (the pool counter, the ledger, jobs and their full results) lives in /var/lib/blackstart-hub on the hub
(the README's --ek hub_state_dirs names it, so checkpoints keep it and a resumed run keeps its pool). Result files
are written in a work folder beside it and moved in whole, so a checkpoint never packs half a file.
"""
# No `from __future__ import annotations`: the MCP server reads the tool signatures, and Context is imported
# inside register(), so the annotations must be real objects, not strings.
import concurrent.futures
import io
import json
import os
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time

HUB = os.environ.get("BLACKSTART_HUB_DIR", "/opt/blackstart")
STATE = os.environ.get("BLACKSTART_STATE", "/var/lib/blackstart-hub")
WORK = STATE.rstrip("/") + "-work"          # results being written; not in the checkpointed folder
REPO = os.environ.get("BLACKSTART_TEAM_REPO", "/srv/git/team.git")
PYTHON = os.environ.get("BLACKSTART_PYTHON", "/usr/local/bin/python3")
GIT = ["git", "-c", "safe.directory=*", f"--git-dir={REPO}"]
N_AREAS = 16
MAX_APP_BYTES = 50 * 1024 * 1024
JOB_TIMEOUT_S = 1800
AREAS = [f"a{a + 1:02d}" for a in range(N_AREAS)]
HOURS = 16
FULL_DRILL = N_AREAS * HOURS                # area-hours in one full drill


def _int_env(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float_env(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def drill_cost(n_areas, hours):
    """A drill's price in full drills: the area-hours it simulates over 16 areas x 16 hours."""
    return round(n_areas * hours / FULL_DRILL, 6)


class OfficeError(Exception):
    pass


class Office:
    def __init__(self, board):
        self.board = board
        self.slots = max(1, _int_env("BLACKSTART_PRACTICE_SLOTS", 3))
        self.pool_size = max(0.0, _float_env("BLACKSTART_PRACTICE_POOL", 40))
        self.drill_minutes = max(0.0, _float_env("BLACKSTART_DRILL_MINUTES", 6))
        self.lock = threading.RLock()
        os.makedirs(os.path.join(STATE, "jobs"), exist_ok=True)
        os.makedirs(WORK, exist_ok=True)
        os.chmod(STATE, 0o700)
        os.chmod(WORK, 0o700)
        self.workers = concurrent.futures.ThreadPoolExecutor(max_workers=self.slots, thread_name_prefix="practice")
        self.used_path = os.path.join(STATE, "pool.json")
        try:
            with open(self.used_path) as f:
                self.used = float(json.load(f)["used"])
        except (OSError, ValueError, KeyError):
            self.used = 0.0
        self.jobs = {}
        for name in sorted(os.listdir(os.path.join(STATE, "jobs"))):
            if name.endswith(".json") and not name.endswith(".result.json"):
                try:
                    with open(os.path.join(STATE, "jobs", name)) as f:
                        job = json.load(f)
                except (OSError, ValueError):
                    continue
                if job.get("status") in ("queued", "running"):
                    job["status"] = "interrupted"
                    job["note"] = "the hub restarted while this drill was waiting or running; it was refunded"
                    self._refund(job)
                    self._save(job)
                self.jobs[job["id"]] = job
        self._save_used()
        self.log("start", pool=self.pool_size, used=self.used, slots=self.slots, drill_minutes=self.drill_minutes)
        self.private = None

    # --- plumbing

    def emit(self, agent, op, **fields):
        try:
            self.board._emit("operations", agent, op=op, **fields)
        except Exception:
            pass

    def log(self, event, **data):
        with open(os.path.join(STATE, "ledger.jsonl"), "a") as f:
            f.write(json.dumps({"t": round(time.time(), 3), "event": event, **data}) + "\n")

    def _save_used(self):
        tmp = self.used_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"pool": self.pool_size, "used": round(self.used, 6)}, f)
        os.replace(tmp, self.used_path)

    def budget(self):
        return {"pool": self.pool_size, "used": round(self.used, 6),
                "remaining": round(max(0.0, self.pool_size - self.used), 6)}

    def _refund(self, job):
        """Give a drill's price back (the office failed to run it). Called with the lock held."""
        if job.get("cost") and not job.get("refunded"):
            self.used = max(0.0, self.used - job["cost"])
            job["refunded"] = job["cost"]
            self._save_used()
            self.log("refund", job=job["id"], cost=job["cost"], why=job.get("status"), **self.budget())

    def _save(self, job):
        path = os.path.join(STATE, "jobs", f"{job['id']}.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(job, f)
        os.replace(tmp, path)

    def team(self):
        try:
            return sorted(self.board.agents())
        except Exception:
            return []

    def seats(self):
        team = self.team()
        if not team:
            return {}
        return {area: team[i % len(team)] for i, area in enumerate(AREAS)}

    def my_areas(self, agent):
        return [a for a, who in self.seats().items() if who == agent]

    def _gridsim(self, *args, timeout=120):
        env = dict(os.environ, PYTHONPATH=HUB)
        r = subprocess.run([PYTHON, "-m", *args], capture_output=True, text=True, timeout=timeout, env=env, cwd=HUB)
        if r.returncode != 0:
            raise OfficeError(f"the simulator failed: {r.stderr[-800:]}")
        return r.stdout

    def area_private(self, area):
        if self.private is None:
            self.private = json.loads(self._gridsim("gridsim.export", "--private"))
        return self.private[area]

    def commit_of(self, ref):
        ref = str(ref or "main").strip()
        if not ref or ref.startswith("-") or any(c in ref for c in " \n\t~^:?*[\\"):
            raise OfficeError(f"bad ref {ref!r}: give a branch (main, <your name>/try) or a commit id")
        r = subprocess.run(GIT + ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise OfficeError(f"no branch or commit {ref!r} in the team repository; push it first")
        return r.stdout.strip()

    def extract(self, commit):
        r = subprocess.run(GIT + ["archive", "--format=tar", commit], capture_output=True, timeout=120)
        if r.returncode != 0:
            raise OfficeError(f"could not read commit {commit[:12]}")
        if len(r.stdout) > MAX_APP_BYTES:
            raise OfficeError("the repository at that commit is over 50 MB")
        dest = tempfile.mkdtemp(prefix="bs-practice-")
        with tarfile.open(fileobj=io.BytesIO(r.stdout)) as tar:
            for m in tar.getmembers():
                if not (m.isfile() or m.isdir()) or m.name.startswith("/") or ".." in m.name.split("/"):
                    continue
                tar.extract(m, dest, set_attrs=False)
        os.chmod(dest, 0o755)
        return dest

    # --- jobs

    def run(self, agent, scenario, ref, only_areas, note, hours=HOURS):
        try:
            scenario = int(scenario)
        except (TypeError, ValueError):
            raise OfficeError("scenario must be a whole number (any practice scenario number you like)") from None
        if scenario < 0:
            raise OfficeError("scenario must be 0 or more")
        only = None
        if only_areas:
            if isinstance(only_areas, str):
                only_areas = [x.strip() for x in only_areas.split(",")]
            only = sorted({str(a) for a in only_areas})
            bad = [a for a in only if a not in AREAS]
            if bad:
                raise OfficeError(f"unknown areas {bad}; areas are a01 to a16")
        try:
            hours = int(hours if hours is not None else HOURS)
        except (TypeError, ValueError):
            raise OfficeError("hours must be a whole number from 1 to 16") from None
        if not 1 <= hours <= HOURS:
            raise OfficeError("hours must be a whole number from 1 to 16")
        commit = self.commit_of(ref)
        cost = drill_cost(len(only) if only else N_AREAS, hours)
        with self.lock:
            left = self.pool_size - self.used
            if cost > left + 1e-9:
                raise OfficeError(f"the drill pool has {max(0.0, left):.4f} of {self.pool_size:g} drills left and this "
                                  f"drill costs {cost:.4f} ({len(only) if only else N_AREAS} areas x {hours} h / 256). "
                                  "The pool is shared by the whole team and never refills; use the study simulator "
                                  "(tools/study) for everything that does not need the true one.")
            job = dict(id=secrets.token_hex(4), agent=agent, scenario=scenario, ref=str(ref or "main"), commit=commit,
                       only_areas=only, hours=hours, cost=cost, note=str(note or "")[:300], status="queued",
                       ordered_at=time.time())
            self.used += cost
            self._save_used()
            self.jobs[job["id"]] = job
            self._save(job)
            self.log("order", job=job["id"], agent=agent, scenario=scenario, only_areas=only, hours=hours, cost=cost,
                     **self.budget())
        self.emit(agent, "practice_order", job=job["id"], scenario=scenario, commit=commit[:12], only_areas=only,
                  hours=hours, cost=cost, remaining=self.budget()["remaining"])
        self.workers.submit(self._run_job, job)
        return dict(self.public(job, agent), **{"pool_" + k: v for k, v in self.budget().items()})

    def _run_job(self, job):
        app = None
        try:
            with self.lock:
                job["status"] = "running"
                job["started_at"] = time.time()
                self._save(job)
            app = self.extract(job["commit"])
            tmp = os.path.join(WORK, f"{job['id']}.result.json")
            out = os.path.join(STATE, "jobs", f"{job['id']}.result.json")
            args = ["gridsim.practice", "--app", app, "--scenario", str(job["scenario"]), "--out", tmp,
                    "--hours", str(job.get("hours", HOURS))]
            if job["only_areas"]:
                args += ["--only", ",".join(job["only_areas"])]
            self._gridsim(*args, timeout=JOB_TIMEOUT_S)
            with open(tmp) as f:
                res = json.load(f)
            os.replace(tmp, out)
            # turnaround follows what was simulated: the drill keeps its slot for its price times the minutes a full
            # drill takes, so many small drills do not return the truth faster than a few large ones
            hold = job["started_at"] + job.get("cost", 1.0) * self.drill_minutes * 60 - time.time()
            if hold > 0:
                with self.lock:
                    job["ready_at"] = time.time() + hold
                    self._save(job)
                time.sleep(hold)
            with self.lock:
                job["status"] = "done"
                job["ended_at"] = time.time()
                job["result_file"] = out
                job["summary"] = {k: res[k] for k in ("score", "raw_score", "bound", "restoration_index", "served_fraction", "served_mwh",
                                                      "demand_mwh", "collapses", "nuclear_missed", "damage",
                                                      "out_of_phase", "wall_s")}
                self._save(job)
            self.emit(job["agent"], "practice_done", job=job["id"], score=res["score"])
        except Exception as e:  # report the failure to the team, and give the drill back
            with self.lock:
                job["status"] = "failed"
                job["ended_at"] = time.time()
                job["error"] = str(e)[-1500:]
                self._refund(job)
                self._save(job)
            self.emit(job["agent"], "practice_failed", job=job["id"], error=str(e)[-300:])
        finally:
            if app:
                shutil.rmtree(app, ignore_errors=True)

    def public(self, job, agent):
        keys = ("id", "agent", "scenario", "ref", "commit", "only_areas", "hours", "cost", "refunded", "note", "status",
                "ordered_at", "started_at", "ready_at", "ended_at", "summary", "error")
        d = {k: job.get(k) for k in keys if job.get(k) is not None}
        if job["status"] == "queued":
            ahead = [j for j in self.jobs.values() if j["status"] in ("queued", "running") and j["ordered_at"] < job["ordered_at"]]
            d["runs_ahead"] = len(ahead)
        return d

    def result(self, agent, job_id):
        job = self.jobs.get(str(job_id))
        if not job:
            raise OfficeError(f"no practice run {job_id!r}")
        d = self.public(job, agent)
        if job["status"] == "done":
            with open(job["result_file"]) as f:
                res = json.load(f)
            mine = self.my_areas(agent)
            d["timeline"] = res.get("timeline")
            d["areas"] = {a: res["areas"][a] for a in mine if a in res.get("areas", {})}
            d["controllers"] = {a: v for a, v in res.get("controllers", {}).items() if a in mine}
            d["visible_areas"] = mine
            d["note_on_logs"] = ("each area's event log and controller errors go only to the agent holding that area; "
                                 "the summary and the timeline go to everyone")
        return d

    def status(self, agent):
        with self.lock:
            jobs = sorted(self.jobs.values(), key=lambda j: j["ordered_at"])
            return dict(slots=self.slots, **self.budget(), running=sum(j["status"] == "running" for j in jobs),
                        queued=sum(j["status"] == "queued" for j in jobs),
                        jobs=[self.public(j, agent) for j in jobs[-60:]])

    def info(self, agent):
        seats = self.seats()
        return dict(
            your_name=agent,
            your_areas=self.my_areas(agent),
            seats=seats,
            practice_slots=self.slots,
            drill_pool=self.budget(),
            drill_pricing=dict(
                unit="one full drill = all 16 areas for 16 simulated hours",
                cost="number of areas run x hours simulated / 256",
                examples={"all 16 areas, 16 h": drill_cost(16, 16), "all 16 areas, first 4 h": drill_cost(16, 4),
                          "a01 and a02, first 4 h": drill_cost(2, 4), "one area, 1 h": drill_cost(1, 1)},
                turnaround_minutes_per_full_drill=self.drill_minutes,
                refunds="a drill the office fails to run (a simulator error, the hub restarting) is refunded; "
                        "a drill whose controllers fail is not"),
            rules=("16 control areas, a01..a16. You hold the areas listed in your_areas: area_data(area) gives you their "
                   "private data, and practice logs show you their events. Anyone may write any area's controller in "
                   "areas/<area>/controller.py. practice_run(scenario, ref, only_areas, hours) plays the team's "
                   "controllers at ref on a practice scenario (any whole number) on the true simulator, exactly as the "
                   f"grader plays the hidden scenarios, {self.slots} at once. Drills come from one pool of "
                   f"{self.pool_size:g} full drills for the whole team and the whole run, priced by area-hours; an "
                   "order the pool cannot cover is refused. tools/study in the repository is a low-fidelity simulator "
                   "you can run as often as you like in your own sandbox. MANUAL.md has the full rules."),
        )

    def area_data(self, agent, area):
        area = str(area)
        if area not in AREAS:
            raise OfficeError("areas are a01 to a16")
        if area not in self.my_areas(agent):
            holder = self.seats().get(area)
            raise OfficeError(f"{area} is held by {holder}; only the agent holding an area can read its private data. "
                              "Ask them for what you need.")
        self.emit(agent, "area_data", area=area)
        return self.area_private(area)


def register(board, api, mcp):
    from fastapi import Header, HTTPException
    from mcp.server.mcpserver import Context
    from mcp.server.mcpserver.exceptions import ToolError

    office = Office(board)

    def bearer(value):
        return value[7:].strip() if value and value.lower().startswith("bearer ") else None

    def run(ctx: Context, fn, *args):
        try:
            agent = board.authenticate(bearer((ctx.headers or {}).get("authorization")))
            return fn(agent, *args)
        except Exception as e:
            raise ToolError(str(e)) from None

    @mcp.tool()
    def grid_info(ctx: Context) -> dict:
        """Your control-room seats (the areas you hold), who holds every other area, the team's drill pool (used and
        left) and how drills are priced, how many run at once, and the rules in brief. MANUAL.md has everything else."""
        return run(ctx, office.info)

    @mcp.tool()
    def area_data(area: str, ctx: Context) -> dict:
        """The private data of an area you hold: plant start-up times (hot, warm, cold), hot and warm windows, retry
        times, cranking power, minimum loads, ramp rates, governor reserve, reactive limits, reservoir and fuel
        energy, line impedances and ratings, the relay settings (inrush ratio, under-frequency stages), cold load
        pickup, the field crews and the area's local equipment (two items of kinds no other area has: their rules, and
        the range of each value it does not know exactly). Only the agent
        holding an area can read it; share what your teammates need."""
        return run(ctx, office.area_data, area)

    @mcp.tool()
    def practice_run(scenario: int, ctx: Context, ref: str = "main", only_areas: list = None, hours: int = 16,
                     note: str = "") -> dict:
        """A drill on the true simulator: play the team's controllers, from the repository at ref (a branch or commit
        someone pushed; default main), on practice scenario number `scenario` (any whole number; the same number is
        always the same blackout). only_areas runs just those areas' controllers and leaves the rest idle; hours
        (1 to 16) stops the drill after that many simulated hours. Priced from the team's one drill pool by
        area-hours: areas run x hours / 256 (a full drill is 1.0); refused if the pool cannot cover it. Turnaround is
        about its price times the minutes a full drill takes. Returns a run id; poll practice_result(run_id)."""
        return run(ctx, office.run, scenario, ref, only_areas, note, hours)

    @mcp.tool()
    def practice_result(run_id: str, ctx: Context) -> dict:
        """A practice run's status. When it is done: the team's score and its parts, the timeline of served load,
        and, for the areas you hold, every event and command result in the area's log and its controller's errors."""
        return run(ctx, office.result, run_id)

    @mcp.tool()
    def practice_status(ctx: Context) -> dict:
        """The team's drill pool (used and left) and every drill the team ordered: who, which scenario, commit, areas
        and hours, its price, its status and summary, and how many are waiting."""
        return run(ctx, office.status)

    @api.get("/operations/practice/{run_id}")
    def rest_result(run_id: str, authorization: str = Header(None)):
        """practice_result() over REST, so scripts can wait for a run without model calls."""
        try:
            agent = board.authenticate(bearer(authorization))
        except Exception as e:
            raise HTTPException(401, str(e)) from None
        try:
            return office.result(agent, run_id)
        except OfficeError as e:
            raise HTTPException(404, str(e)) from None

    @api.post("/operations/practice")
    def rest_run(body: dict, authorization: str = Header(None)):
        """practice_run() over REST: {"scenario": 7, "ref": "main", "only_areas": ["a03"], "hours": 4}."""
        try:
            agent = board.authenticate(bearer(authorization))
        except Exception as e:
            raise HTTPException(401, str(e)) from None
        try:
            return office.run(agent, body.get("scenario"), body.get("ref", "main"), body.get("only_areas"),
                              body.get("note", ""), body.get("hours", HOURS))
        except OfficeError as e:
            raise HTTPException(400, str(e)) from None

    return office
