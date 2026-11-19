"""Practice office board plugin: the only place a team can play a match against Petra.

Load it with `--ek board_plugins=<legion_hub_plugin.py>,<this file>`. It runs inside the board on the hub, so it
uses only the standard library and the board's own packages. The agents' sandboxes and the hub share one image,
from which Petra is removed. Petra and the trusted match harness reach the hub only through the hub bundle
(make_hub_plugin.py writes legion_hub_plugin.py, which the board loads first and which unpacks them into
/opt/legion-hub). A team never sees either.

Rules:
- One pool of practice matches for the whole team (LEGION_PRACTICE_POOL, the same for a team of any size). A
  match uses one from the pool when it is ordered, whatever its length or result. An order is refused whole if
  the pool cannot cover it, and the pool never refills.
- A match the office itself fails to run (not a loss by the AI) is refunded. The reproducibility re-run of a
  match the AI does not lose is free: it re-plays a match already paid for.
- The office plays LEGION_PRACTICE_SLOTS games at once, first come first served.
- The AI is read from the team repository at a ref (a branch or commit someone pushed) and a path (default
  legion/), so a teammate can test a candidate without touching main.
- Everyone sees every job and its records: practice results belong to the team.

The pool counter, the ledger and the jobs live in /var/lib/legion-hub on the hub (pass it in
--ek hub_state_dirs, so checkpoints keep it and a resumed run keeps its pool). Jobs that were still running when
a run stopped are marked interrupted on resume, and their unplayed matches refunded.
"""
# No `from __future__ import annotations`: the MCP server reads the tool signatures, and Context is imported
# inside register(), so the annotations must be real objects, not strings.
import concurrent.futures
import importlib.util
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

HUB = os.environ.get("LEGION_HUB_DIR", "/opt/legion-hub")
STATE = os.environ.get("LEGION_PRACTICE_STATE", "/var/lib/legion-hub")
REPO = os.environ.get("LEGION_TEAM_REPO", "/srv/git/team.git")
PYTHON = os.environ.get("LEGION_PRACTICE_PYTHON", "/usr/bin/python3")
MAX_BOT_BYTES = 20 * 1024 * 1024
MATCH_TIMEOUT_SEC = 1800   # the harness's own wall limit is 600 s a game, twice with the reproducibility re-run
MATCH_KEYS = ("map", "seed", "aiseed", "slot", "civ", "opp_civ", "opponent", "behavior", "biome", "placement")
GIT = ["git", "-c", "safe.directory=*", f"--git-dir={REPO}"]


def _int_env(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _load_legion():
    """The trusted harness (hub bundle), for validating match specs and scoring results; games run in a subprocess."""
    path = os.path.join(HUB, "harness", "legion.py")
    spec = importlib.util.spec_from_file_location("legion_trusted", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class PracticeError(Exception):
    pass


class PracticeOffice:
    def __init__(self, board):
        self.board = board
        self.legion = _load_legion()
        # The game's archive for checking map names (the hub's own; LEGION_PUBLIC_ZIP points it at a copy for tests).
        self.public_zip = os.environ.get("LEGION_PUBLIC_ZIP") or str(self.legion.PUBLIC_ZIP)
        self.pool = _int_env("LEGION_PRACTICE_POOL", 30)
        self.slots = max(1, _int_env("LEGION_PRACTICE_SLOTS", 3))
        self.lock = threading.Lock()
        self.games = concurrent.futures.ThreadPoolExecutor(max_workers=self.slots, thread_name_prefix="practice")
        os.makedirs(os.path.join(STATE, "jobs"), mode=0o700, exist_ok=True)
        os.chmod(STATE, 0o700)
        self.used_path = os.path.join(STATE, "used")
        self.used = int(open(self.used_path).read()) if os.path.exists(self.used_path) else 0
        self.jobs = {}
        for name in sorted(os.listdir(os.path.join(STATE, "jobs"))):
            try:
                job = json.load(open(os.path.join(STATE, "jobs", name)))
            except (OSError, ValueError):
                continue
            if job["status"] in ("queued", "running"):   # the run stopped (checkpoint, resume) while it played
                unplayed = sum(1 for r in job["records"] if r is None)
                job["status"], job["refunded"] = "interrupted", job.get("refunded", 0) + unplayed
                self.used = max(0, self.used - unplayed)
                self._save(job)
            self.jobs[job["id"]] = job
        self._save_used()
        dev = os.path.join(HUB, "harness", "dev_gauntlet.json")
        self.dev = json.load(open(dev))["matches"] if os.path.exists(dev) else []
        self.log("start", pool=self.pool, slots=self.slots, used=self.used)

    # --- bookkeeping

    def log(self, event, **data):
        with open(os.path.join(STATE, "ledger.jsonl"), "a") as f:
            f.write(json.dumps({"t": round(time.time(), 3), "event": event, **data}) + "\n")

    def emit(self, agent, op, **fields):
        try:
            self.board._emit("practice", agent, op=op, **fields)
        except Exception:
            pass

    def _save_used(self):
        with open(self.used_path, "w") as f:
            f.write(str(self.used))

    def _save(self, job):
        path = os.path.join(STATE, "jobs", f"{job['id']}.json")
        with open(path + ".tmp", "w") as f:
            json.dump(job, f)
        os.replace(path + ".tmp", path)

    def budget(self):
        return {"pool": self.pool, "used": self.used, "remaining": max(0, self.pool - self.used)}

    # --- inputs

    def clean_match(self, m):
        L = self.legion
        if not isinstance(m, dict):
            raise PracticeError("each match must be an object with map, seed, slot, civ, opp_civ and opponent")
        extra = set(m) - set(MATCH_KEYS)
        if extra:
            raise PracticeError(f"unknown match keys: {sorted(extra)}; allowed: {list(MATCH_KEYS)}")
        out = {k: m[k] for k in MATCH_KEYS if k in m}
        for k in ("map", "seed", "slot", "civ", "opp_civ", "opponent"):
            if k not in out:
                raise PracticeError(f"match is missing {k!r}")
        try:
            out["seed"], out["slot"] = int(out["seed"]), int(out["slot"])
            opp = out["opponent"]
            out["opponent"] = L.LEVELS[opp] if isinstance(opp, str) and opp in L.LEVELS else int(opp)
            out["aiseed"] = int(out.get("aiseed") or out["seed"])
        except (TypeError, ValueError):
            raise PracticeError("seed, aiseed, slot and opponent must be integers (opponent may be a level name)") from None
        if out["slot"] not in (1, 2):
            raise PracticeError("slot must be 1 or 2")
        if not 1 <= out["opponent"] <= 12:
            raise PracticeError("opponent must be a Petra level from 1 (very_easy) to 12 (elite_4)")
        if out["civ"] not in L.CIVS or out["opp_civ"] not in L.CIVS:
            raise PracticeError(f"civilizations must be among {L.CIVS}")
        out["behavior"] = out.get("behavior") or L.DEFAULT_BEHAVIOR
        if out["behavior"] not in L.BEHAVIORS:
            raise PracticeError(f"behavior must be one of {list(L.BEHAVIORS)}")
        if not isinstance(out["map"], str) or not out["map"].replace("_", "").isalnum():
            raise PracticeError("map must be a random-map name such as mainland")
        try:
            L.map_options(out["map"], self.public_zip)
        except KeyError:
            raise PracticeError(f"unknown map {out['map']!r}") from None
        return out

    def commit_of(self, ref):
        ref = str(ref or "main").strip()
        if not ref or ref.startswith("-") or any(c in ref for c in " \n\t~^:?*[\\"):
            raise PracticeError(f"bad ref {ref!r}: give a branch (main, <your name>/try) or a commit id")
        r = subprocess.run(GIT + ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise PracticeError(f"no branch or commit {ref!r} in the team repository; push it first")
        return r.stdout.strip()

    def extract_bot(self, commit, path):
        path = str(path or "legion").strip().strip("/")
        if not path or ".." in path.split("/") or path.startswith("-"):
            raise PracticeError(f"bad path {path!r}: give a folder of the repository, such as legion")
        r = subprocess.run(GIT + ["archive", "--format=tar", commit, "--", path], capture_output=True, timeout=60)
        if r.returncode != 0:
            raise PracticeError(f"{path}/ is not in commit {commit[:12]}")
        dest = tempfile.mkdtemp(prefix="practice-bot-")
        size = 0
        with tarfile.open(fileobj=io.BytesIO(r.stdout)) as tar:
            for member in tar.getmembers():
                rel = os.path.relpath(member.name, path)
                if (not member.isfile() or rel.startswith("..") or os.path.isabs(rel)
                        or os.path.splitext(rel)[1] not in (".js", ".json")):
                    continue
                size += member.size
                if size > MAX_BOT_BYTES:
                    shutil.rmtree(dest, ignore_errors=True)
                    raise PracticeError(f"{path}/ holds more than 20 MB of .js and .json files")
                target = os.path.join(dest, rel)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as f:
                    f.write(tar.extractfile(member).read())
        if not os.path.isfile(os.path.join(dest, "main.js")):
            shutil.rmtree(dest, ignore_errors=True)
            raise PracticeError(f"{path}/main.js is not in commit {commit[:12]}")
        for root, dirs, files in os.walk(dest):
            os.chmod(root, 0o755)
            for f in files:
                os.chmod(os.path.join(root, f), 0o644)
        return dest

    # --- jobs

    def play(self, agent, matches, dev_first, ref, path, verify, time_limit_min, note):
        if matches and dev_first:
            raise PracticeError("give matches or dev_first, not both")
        if dev_first:
            if not 1 <= int(dev_first) <= len(self.dev):
                raise PracticeError(f"dev_first must be between 1 and {len(self.dev)}")
            matches = [{k: m[k] for k in MATCH_KEYS if k in m} for m in self.dev[:int(dev_first)]]
        if not matches or not isinstance(matches, list):
            raise PracticeError("no matches given: pass matches=[{...}, ...] (legion.py match / gauntlet print them) "
                                "or dev_first=N for the first N matches of the dev gauntlet")
        clean = [self.clean_match(m) for m in matches]
        try:
            limit = float(time_limit_min)
        except (TypeError, ValueError):
            raise PracticeError("time_limit_min must be a number") from None
        if not 1 <= limit <= self.legion.DEFAULT_TIME_LIMIT_MIN:
            raise PracticeError(f"time_limit_min must be between 1 and {self.legion.DEFAULT_TIME_LIMIT_MIN}")
        commit = self.commit_of(ref)
        bot = self.extract_bot(commit, path)
        with self.lock:
            left = self.pool - self.used
            if len(clean) > left:
                shutil.rmtree(bot, ignore_errors=True)
                raise PracticeError(f"the practice pool has {max(0, left)} of {self.pool} matches left; this order "
                                    f"needs {len(clean)}. The pool is shared by the whole team and does not refill.")
            self.used += len(clean)
            self._save_used()
            job = {"id": "p" + secrets.token_hex(4), "agent": agent, "ref": str(ref or "main"), "commit": commit,
                   "path": str(path or "legion"), "verify": bool(verify), "time_limit_min": limit,
                   "note": str(note or "")[:500], "ordered": round(time.time(), 1), "status": "queued",
                   "matches": clean, "records": [None] * len(clean), "refunded": 0}
            self.jobs[job["id"]] = job
            self._save(job)
        self.log("order", job=job["id"], agent=agent, n=len(clean), commit=commit, used=self.used)
        self.emit(agent, "order", job=job["id"], n=len(clean), commit=commit[:12], remaining=self.pool - self.used)
        pending = {"n": len(clean)}
        for i in range(len(clean)):
            self.games.submit(self._run_one, job, i, bot, pending)
        return {"job_id": job["id"], "matches": len(clean), "commit": commit, **self.budget(),
                "note": "Poll practice_result(job_id). A match takes about 1-3 minutes of wall time."}

    def _run_one(self, job, i, bot, pending):
        with self.lock:
            if job["status"] == "queued":
                job["status"], job["started"] = "running", round(time.time(), 1)
        req = {"match": job["matches"][i], "bot_dir": bot, "verify": job["verify"], "time_limit_min": job["time_limit_min"]}
        try:
            r = subprocess.run([PYTHON, "-I", "-S", os.path.join(HUB, "practice_run.py")], input=json.dumps(req),
                               capture_output=True, text=True, timeout=MATCH_TIMEOUT_SEC,
                               env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root",
                                    "LEGION_PETRA_ZIP": os.path.join(HUB, "petra.zip")})
            lines = [l for l in r.stdout.splitlines() if l.startswith("{")]
            if r.returncode != 0 or not lines:
                raise RuntimeError((r.stderr or r.stdout or f"exit {r.returncode}").strip()[-400:])
            rec = json.loads(lines[-1])
        except Exception as e:   # the office failed, not the AI: refund this match
            rec = {**job["matches"][i], "result": None, "reason": f"practice office error, refunded: {str(e)[:300]}"}
            with self.lock:
                self.used = max(0, self.used - 1)
                job["refunded"] += 1
                self._save_used()
        with self.lock:
            job["records"][i] = rec
            pending["n"] -= 1
            done = pending["n"] == 0
            if done:
                job["status"], job["finished"] = "done", round(time.time(), 1)
                scored = [r for r in job["records"] if r and r.get("result") in self.legion.SCORE_OF]
                if scored:
                    try:
                        ladder = json.load(open(os.path.join(HUB, "harness", "ladder.json")))
                        s = self.legion.score_results(scored, ladder, bootstrap=200)
                        job["estimate"] = {k: s[k] for k in ("score", "score_ci95", "elo", "elo_ci95", "points", "record", "per_level")
                                           if k in s}
                    except Exception as e:
                        job["estimate"] = {"error": str(e)[:200]}
            self._save(job)
        if done:
            shutil.rmtree(bot, ignore_errors=True)
            results = [(r or {}).get("result") for r in job["records"]]
            self.log("done", job=job["id"], refunded=job["refunded"], used=self.used, results=results)
            self.emit(job["agent"], "done", job=job["id"], results=results, refunded=job["refunded"])

    def summary(self, job):
        out = {k: job.get(k) for k in ("id", "agent", "ref", "commit", "path", "verify", "note", "ordered",
                                       "started", "finished", "status", "refunded", "estimate")}
        recs = job["records"]
        out["matches"] = len(recs)
        out["played"] = sum(r is not None for r in recs)
        out["results"] = {k: sum(1 for r in recs if r and r.get("result") == k) for k in ("win", "draw", "loss")}
        return out

    def result(self, agent, job_id):
        job = self.jobs.get(str(job_id))
        if not job:
            raise PracticeError(f"no practice job {job_id!r}")
        out = self.summary(job)
        out["records"] = [r if r is not None else {**m, "result": None, "reason": "not played yet"}
                          for m, r in zip(job["matches"], job["records"])]
        return out

    def status(self, agent):
        with self.lock:
            jobs = [self.summary(j) for j in sorted(self.jobs.values(), key=lambda j: j["ordered"])]
        return {**self.budget(), "slots": self.slots, "jobs": jobs}

    def info(self, agent):
        L = self.legion
        return {
            **self.budget(), "slots": self.slots,
            "rules": ["One pool of practice matches against Petra for the whole team; it never refills.",
                      "A match uses one from the pool when ordered, whatever its result; an order the pool cannot "
                      "cover is refused whole.",
                      "Matches the office fails to run are refunded; reproducibility re-runs (verify) are free.",
                      f"{self.slots} games play at once, first come first served.",
                      "The AI is read from the team repository at ref (a branch or commit you pushed) and path."],
            "match_keys": {"map": "a random-map name, e.g. mainland (see /app/harness/dev_gauntlet.json)",
                           "seed": "map seed", "aiseed": "AI random seed (default: seed)", "slot": "1 or 2",
                           "civ": "your civilization", "opp_civ": "Petra's civilization",
                           "opponent": f"Petra level 1-12 or its name: {[k for k in L.LEVELS if k != 'sandbox']}",
                           "behavior": f"Petra's personality: {list(L.BEHAVIORS)} (default {L.DEFAULT_BEHAVIOR})",
                           "biome": "optional", "placement": "optional"},
            "civilizations": L.CIVS, "dev_gauntlet_matches": len(self.dev),
            "time_limit_min": L.DEFAULT_TIME_LIMIT_MIN, "wall_limit_sec": L.DEFAULT_WALL_LIMIT_SEC,
        }


def register(board, api, mcp):
    from fastapi import Header
    from mcp.server.mcpserver import Context
    from mcp.server.mcpserver.exceptions import ToolError

    office = PracticeOffice(board)

    def bearer(value):
        return value[7:].strip() if value and value.lower().startswith("bearer ") else None

    def run(ctx: Context, fn, *args):
        try:
            agent = board.authenticate(bearer((ctx.headers or {}).get("authorization")))
            return fn(agent, *args)
        except Exception as e:
            raise ToolError(str(e)) from None

    @mcp.tool()
    def practice_info(ctx: Context) -> dict:
        """The practice office: the team's pool of matches against Petra (how many are left), the rules, and the
        format of a match spec for practice_play."""
        return run(ctx, office.info)

    @mcp.tool()
    def practice_play(ctx: Context, matches: list = None, dev_first: int = 0, ref: str = "main", path: str = "legion",
                      verify: bool = True, time_limit_min: float = 60.0, note: str = "") -> dict:
        """Play your AI against Petra on the hub. Each match uses one from the team's shared pool, which never
        refills; an order the pool cannot cover is refused whole. matches is a list of match specs, e.g.
        [{"map": "mainland", "seed": 3, "slot": 1, "civ": "athen", "opp_civ": "brit", "opponent": "hard",
        "behavior": "aggressive"}] (python3 /app/harness/legion.py match / gauntlet print them), or dev_first=N
        plays the first N matches of the dev gauntlet. The AI is read from the team repository: the folder path
        (default legion) at ref, a branch or commit that has been pushed (default main). verify also runs the
        reproducibility check on matches you do not lose, free. Returns a job id; poll practice_result(job_id)."""
        return run(ctx, office.play, matches, dev_first, ref, path, verify, time_limit_min, note)

    @mcp.tool()
    def practice_result(ctx: Context, job_id: str) -> dict:
        """A practice job: its status, every match record so far (result, reason, game and wall time, bot errors,
        reproducibility check) and, when it is done, an estimated score and Elo from its matches."""
        return run(ctx, office.result, job_id)

    @mcp.tool()
    def practice_status(ctx: Context) -> dict:
        """The team's practice pool (used and left) and every job anyone on the team ordered, with its results."""
        return run(ctx, office.status)

    @api.get("/admin/practice")
    def admin_status(authorization: str | None = Header(None)):
        """The pool and every job with its records, for the people running the eval (admin token)."""
        api.state.require_admin(authorization)
        with office.lock:
            return {**office.budget(), "slots": office.slots,
                    "jobs": [office.result(None, j) for j in sorted(office.jobs, key=lambda j: office.jobs[j]["ordered"])]}
