"""Verifier for mars-return. Runs on the hub, where /app holds the team's final main and
/var/lib/marsflight-hub holds the program office's ledger and secret (written by program_plugin.py).

0. Compile the team's SysML model (design/**/*.sysml) with the hidden compiler into the simulator's
   internal design (vehicle.json). A model that does not compile scores 0. Without a model, it reads
   design/vehicle.json as before (old runs).

1. Cost the design (marsflight.economics). A design over the hardware budget cannot be built: 0.
2. Build the hidden as-built truth of every part (program.flight_truth) and its failure chances
   given the tests the team ran (program.hazards), with each part's hidden reliability factor
   (program.reliability_factors). Tank designs the team qualified may use the tested factor of safety.
3. Fly MARSFLIGHT_VERIFY_SEEDS (default 48) dispersed worlds: the fixed seeds of MARSFLIGHT_VERIFY_SEED_FILE
   (tests/test.sh sets it to tests/seeds.json), else fresh seeds. The flight software runs as
   an unprivileged flight user made for this run (make_flight_user: a random uid above 10^6 that nothing
   on the hub uses or owns), removed at the end. It runs with limits it cannot raise (guidance_io): 2 GiB
   of data per process (RLIMIT_DATA), 32 tasks for all flight software together, 10 a flight with more than 3
   flights at once (RLIMIT_NPROC, flight_nproc), and one
   BLAS thread. It cannot read the simulator or the hidden compiler: their folders are closed to all but
   root (hide_simulator), and it runs from a copy of the standard-library runner in isolated mode. /app, the
   whole team repo, is closed to it too while flights run (hide_app): it sees only its copy of flight/. Each
   flight's flight software gets an empty home and TMPDIR of its own, and before the
   flights every process and file of the flight user is removed. As many flights fly at once as the hub's
   CPU quota allows and its memory holds with every flight software at its limit (scoring.default_workers:
   3 in 8 GB, all 48 at 48 CPUs and 123 GB). MARSFLIGHT_VERIFY_WORKERS asks for a number instead
   (verify_workers): set it to fly all 48 worlds at once on a hub sized for typical flights
   (about 1 GB each), which takes minutes instead of the hour or more 3 at a time take. That trades the
   worst-case memory bound for speed: it is capped at what the hub's memory holds at TYPICAL_FLIGHT_BYTES per
   flight, and if flight software near its 2 GiB limit then fills the hub, the out-of-memory killer ends a
   process and the rules below apply (its flight fails; a lost simulator process's flights fly again).
   A flight whose flight software dies (a MemoryError, the out-of-memory killer, any signal) has failed
   and is not flown again. fly_one's killed_by_kernel and flight_software_exit are diagnostics only.
   If the kernel kills a simulator process (a pool worker), the flights already finished keep their
   results, and only the flights the broken pool lost are flown again, one at a time, after the flight
   user's processes and files are removed.
   Everything must end within MARSFLIGHT_VERIFY_BUDGET_S (default 5400 s, inside task.toml's 6000 s
   timeout): a flight that starts too late for its full 1500 s gets only the time left.
4. Engine-out checks (grading.engine_out_cases, realism gate G4): for each group of two or more engines of one
   kind, fly two of the graded worlds again with one of those engines shut down, at the element's first
   ignition and halfway through the group's burning. The share each kept of the value its graded twin earned
   after the shutdown sets how much of the group's failure chance the mission survives;
   program.CATASTROPHIC_FRACTION of failures always destroy the stage. The checks fly only if ENGINE_OUT_MIN_S
   of the budget is left and grading.ENGINE_OUT_CHECKS is on; without them every engine failure counts as lost.
   Then the failure checks (grading.failure_check_cases): each parachute with a backup on its carrier flown
   failing to open, and each stage whose engines can restart flown with its first ignition a no-light. What
   each keeps sets how much of that part's failure chance the mission survives, up to the share a backup or a
   retry can recover (program.CHUTE_BACKUP_FRACTION, IGNITION_RETRY_FRACTION). grading.FAILURE_CHECKS turns
   them off.
5. Score: the mean physical progress over the worlds (scoring.expected_progress's physical_progress: the
   milestone weights and partial credit each flight earned, not weighted by hardware survival), times G.

       score = G * mean physical progress

   G is the correctness gate: the deliverables are on main, the design is valid and within the hardware
   budget, and every test result the team saved in program/test_results.json matches the program office's
   ledger. The weights add up to 1, so a mission that reaches every milestone in every world scores 1. This is
   the progress measure the published results report. The program acceptance grade (marsflight.grading: world values against the success criteria, survival against the
   0.90 requirement, the forecast and net value) stays in details.json as cliff_score with its grade, and the
   v2 score (scoring.score_flights) as v2_score; neither changes the score.
"""
from __future__ import annotations

import atexit
import glob
import grp
import json
import os
import pwd
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool

T_START = time.time()
# The verifier grades 6-DOF flights: a MARSFLIGHT_DYNAMICS=3dof left in its environment must not change
# that. Its Monte Carlo workers inherit this setting.
os.environ["MARSFLIGHT_DYNAMICS"] = "6dof"

sys.path.insert(0, os.environ.get("MARSFLIGHT_DIR", "/opt/marsflight"))
sys.path.insert(0, os.environ.get("MARSFLIGHT_DESIGNLANG_DIR", "/opt/designlang"))
from designlang.hidden.compile import CompileError, compile_files  # noqa: E402
from marsflight import economics as E  # noqa: E402
from marsflight import grading as G  # noqa: E402
from marsflight import program as P  # noqa: E402
from marsflight.guidance_io import FS_DATA_BYTES, NPROC_ENV  # noqa: E402
from marsflight.scoring import (RESERVE_BYTES, default_workers, fly_one, monte_carlo, score_flights,  # noqa: E402
                                usable_cpus, usable_memory)
from marsflight.vehicle import build_vehicle  # noqa: E402

APP = os.environ.get("MARSFLIGHT_APP", "/app")
OUT = os.environ.get("MARSFLIGHT_VERIFIER_OUT", "/logs/verifier")
STATE = os.environ.get("MARSFLIGHT_PROGRAM_STATE", "/var/lib/marsflight-hub")
N = int(os.environ.get("MARSFLIGHT_VERIFY_SEEDS", "48"))
MAX_WALL = float(os.environ.get("MARSFLIGHT_VERIFY_MAX_WALL_S", "1500"))
BUDGET_S = float(os.environ.get("MARSFLIGHT_VERIFY_BUDGET_S", "5400"))
# where the flight user can write, besides its home (/dev/mqueue: POSIX message queues)
SHARED_DIRS = ("/tmp", "/var/tmp", "/run/lock", "/dev/shm", "/dev/mqueue")
RUN_UID_MIN = 10**6   # the run's flight user gets a uid above this, unlike any user of the image or the host
FLIGHT_NPROC = 32     # tasks for all flight software together: one flight software has 2 (python, jemalloc)
FLIGHT_NPROC_EACH = 10   # with more than 3 flights at once, this many tasks per flight (flight_nproc)
ENGINE_OUT_MIN_S = 600.0   # the engine-out checks fly only if at least this much of the budget is left
WORKERS_ENV = "MARSFLIGHT_VERIFY_WORKERS"
# One flight, simulator and flight software together, as flown: 0.94 GB at most for one flight alone and
# about 1 GB a flight with 48 at once on a 32-core host (the 48-world checks), plus a margin.
TYPICAL_FLIGHT_BYTES = 1.2e9


def verify_workers(n):
    """How many flights fly at once, and how that was chosen. By default scoring.default_workers: no more than
    the hub's CPU quota, and few enough that the hub's memory holds every flight software at its FS_DATA_BYTES
    limit plus its simulator, so flight software that keeps to its limit can't bring in the out-of-memory killer.
    MARSFLIGHT_VERIFY_WORKERS=k asks for k flights at once instead, up to n. It may go past the CPU quota (the
    flights then share CPUs and each runs slower against its MAX_WALL), but never past what the hub's memory
    holds at TYPICAL_FLIGHT_BYTES a flight, so a hub that ended up smaller than planned still gets a safe pool."""
    default = default_workers(n)
    info = {"default": default, "cpus": usable_cpus(), "memory_bytes": usable_memory(), "asked": None}
    raw = os.environ.get(WORKERS_ENV, "").strip()
    if not raw:
        return default, info
    try:
        asked = int(raw)
    except ValueError:
        info["asked"] = raw
        info["note"] = f"{WORKERS_ENV}={raw!r} is not a whole number; using the default"
        return default, info
    info["asked"] = asked
    if asked <= 0:   # 0 asks for the default
        return default, info
    w = min(n, asked)
    mem = info["memory_bytes"]
    if mem:
        fits = max(1, int((mem - RESERVE_BYTES) // TYPICAL_FLIGHT_BYTES))
        if w > fits:
            info["note"] = f"capped at {fits}: the hub's {mem / 1e9:.1f} GB holds no more typical flights"
            w = fits
    return w, info


def fly_side_by_side(design_in, guidance_in, user, truth, qualified, deadline, *kinds, live_dir=""):
    """Fly several kinds of check flights in one pool, all at once: each kind is a list of (seed, max_wall_s,
    engine_out, failure_check). Returns one result list per kind, as scoring.monte_carlo would; a kind that lost a
    flight to a broken pool gets [] (no checks), as monte_carlo's callers do on BrokenProcessPool. live_dir: each
    flight's live progress (marsflight.live), engine_out-NN or failure_check-NN by what it checks."""
    from marsflight.scoring import _fly_star
    jobs = [(k, i, (design_in, guidance_in, seed, user, wall, False, truth, qualified, deadline, eo, fcheck,
                    {"dir": live_dir, "kind": "engine_out" if eo else "failure_check", "index": i} if live_dir else None))
            for k, kind in enumerate(kinds) for i, (seed, wall, eo, fcheck) in enumerate(kind)]
    out = [[None] * len(kind) for kind in kinds]
    with ProcessPoolExecutor(max_workers=max(1, len(jobs))) as ex:
        futures = {ex.submit(_fly_star, args): (k, i) for k, i, args in jobs}
        for f in as_completed(futures):
            k, i = futures[f]
            try:
                out[k][i] = f.result()
            except BrokenProcessPool:
                pass
    return [r if all(x is not None for x in r) else [] for r in out]


def flight_nproc(workers):
    """RLIMIT_NPROC for the run's flight user: the tasks all its flight software may have at once. The kernel counts
    them per uid, across every flight flying at the time, so the limit grows with the pool: FLIGHT_NPROC up to 3
    flights at once, FLIGHT_NPROC_EACH a flight beyond. A fixed 32 left 48 flights at once unable to start 15 of
    their flight software (runuser: Resource temporarily unavailable), each counted as a failed flight."""
    return max(FLIGHT_NPROC, FLIGHT_NPROC_EACH * workers)


def owned_by(uid):
    """Whether any process or file on the hub belongs to uid."""
    for pid in os.listdir("/proc"):
        try:
            if pid.isdigit() and os.stat(f"/proc/{pid}").st_uid == uid:
                return True
        except OSError:
            pass
    return bool(files_of(uid, first=True))


def files_of(uid, first=False):
    """The files on the hub that uid owns (only the first one found, if first)."""
    found = subprocess.run(["find", "/", "(", "-path", "/proc", "-o", "-path", "/sys", ")", "-prune", "-o",
                            "-uid", str(uid), "-print0"] + (["-quit"] if first else []), capture_output=True, text=True)
    return [p for p in found.stdout.split("\0") if p]


def remove_ipc(user):
    """Remove the SysV shared memory, message queues and semaphores the flight user made: they outlive its
    processes (kernel.shm_rmid_forced is 0), so a flight could leave a note in one. ipcrm runs as the user, which
    can always remove what it made; root in the container cannot (no CAP_IPC_OWNER)."""
    subprocess.run(["runuser", "-u", user, "--", "ipcrm", "--all"], check=False, capture_output=True)


def mapped_ids(path):
    """The ids this process's user namespace maps, as (first, end) ranges, from /proc/self/uid_map or gid_map.
    Without the file (no namespaces) every id is mapped."""
    try:
        with open(path) as f:
            rows = [line.split() for line in f if line.strip()]
        return [(int(r[0]), int(r[0]) + int(r[2])) for r in rows if len(r) == 3]
    except (OSError, ValueError):
        return [(0, 2**32 - 1)]


def run_uid_ranges():
    """Where the run's flight uid may come from: ids that both the uid and the gid map cover, up to 2^31 - 2. Above
    RUN_UID_MIN where the maps reach that far (Docker maps every id). A sandbox in a user namespace may map only
    0-65535 (some sandbox providers do), and a file can't be given an unmapped uid (chown fails with EINVAL, so every flight
    crashed there): then from 20000 up, past the image's own users, leaving out 65534 (nobody)."""
    def overlap(lo, hi):
        out = []
        for a, b in mapped_ids("/proc/self/uid_map"):
            for c, d in mapped_ids("/proc/self/gid_map"):
                s, e = max(a, c, lo), min(b, d, hi)
                if s < e:
                    out.append((s, e))
        return out

    return overlap(RUN_UID_MIN, 2**31 - 1) or overlap(20000, 65534)


def make_flight_user():
    """A flight user of the verifier's own for this run. Its uid is random, above 10^6 where the user namespace
    maps it (else in the mapped range, run_uid_ranges), and neither used nor owning anything on the hub, so
    RLIMIT_NPROC, which the kernel counts per uid across the whole machine, counts only this run's flight software.
    Returns its name, or None when it can't be made."""
    if not shutil.which("useradd"):
        return None
    ranges = run_uid_ranges()
    if not ranges:
        return None
    for _ in range(10):
        s, e = ranges[secrets.randbelow(len(ranges))]
        uid = s + secrets.randbelow(e - s)
        name = f"flight{uid}"
        try:
            pwd.getpwuid(uid)
            continue
        except KeyError:
            pass
        try:
            grp.getgrgid(uid)
            continue
        except KeyError:
            pass
        if owned_by(uid):
            continue
        made = subprocess.run(["useradd", "--uid", str(uid), "--user-group", "--no-create-home", "--home-dir",
                               "/nonexistent", "--shell", "/usr/sbin/nologin", name], capture_output=True)
        if made.returncode == 0:
            return name
    return None


def remove_flight_user(user):
    """Kill the run's flight user's processes, remove its files, then the user and its group."""
    clear_flight_user(user)
    subprocess.run(["userdel", user], check=False, capture_output=True)
    subprocess.run(["groupdel", user], check=False, capture_output=True)


def hide_simulator(user):
    """The flight software must not import the simulator or the hidden compiler (the task hides both from the team;
    the hub bundle unpacks them readable by all, owned by root). Close their folders to everyone but root, which
    flies the vehicle, and check as the flight user that the packages can no longer be read. The flight software
    runs from a copy of the standard-library runner (guidance_io), so it needs neither."""
    if not user:
        return None
    import designlang
    import marsflight
    hidden = {}
    for mod in (marsflight, designlang):
        pkg = os.path.abspath(list(mod.__path__)[0])   # designlang is a namespace package, with no __file__
        root = os.path.dirname(pkg)
        if root in ("/", "/opt", sys.prefix):
            hidden[root] = False
            continue
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass
        probe = subprocess.run(["runuser", "-u", user, "--", "test", "-r", pkg], capture_output=True)
        hidden[root] = probe.returncode != 0
    return hidden


def hide_app(user):
    """Close /app to everyone but root while the flight user's software runs: it holds the whole team repo
    (program/test_results.json among it), and flight software may read only flight/. Returns how to undo it."""
    if not user:
        return None
    try:
        st = os.stat(APP)
    except OSError:
        return None
    try:
        os.chown(APP, 0, 0)   # an owner could open it again
        os.chmod(APP, 0o700)
    except OSError:
        pass
    atexit.register(restore_app, st)   # also when the verifier dies with /app hidden
    return st


def restore_app(st):
    if st is not None:
        try:
            os.chown(APP, st.st_uid, st.st_gid)
            os.chmod(APP, st.st_mode & 0o7777)
        except OSError:
            pass


def clear_flight_user(user):
    """Kill every process of the flight user and remove the files it owns where it can write (its home keeps
    what the image put there, such as the .tudat link), and its SysV IPC objects. The run's own user owned nothing
    before its flights, so every file it owns is removed, wherever it is. Tudat's data in the image came out owned by uid 1000,
    which is the flight user: give it to root, so no flight can change it or leave anything in it."""
    if not user:
        return
    uid, home = pwd.getpwnam(user).pw_uid, pwd.getpwnam(user).pw_dir
    subprocess.run(["pkill", "-KILL", "-u", user], check=False)
    reap(uid)
    subprocess.run(["chown", "-R", "root:root", os.path.realpath(os.path.expanduser("~/.tudat"))], check=False,
                   capture_output=True)
    remove_ipc(user)
    paths = [os.path.join(d, name) for d in SHARED_DIRS + (home,) for name in listdir(d)]
    if user == f"flight{uid}":   # the run's own user owned nothing before its flights: remove every file it owns
        paths += files_of(uid)
    for p in paths:
        try:
            if os.lstat(p).st_uid != uid:
                continue
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                os.unlink(p)
        except OSError:
            pass


def listdir(d):
    try:
        return os.listdir(d)
    except OSError:
        return []


def become_subreaper():
    """Orphans of the flight software (children it leaves behind) come to this process instead of the container's
    init, so reap() can wait for them: the kernel counts them against RLIMIT_NPROC until then."""
    try:
        import ctypes
        ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0)   # PR_SET_CHILD_SUBREAPER
    except (OSError, AttributeError):
        pass


def reap(uid, wait_s=2.0):
    """Wait for the processes of uid that are this process's children and have died (after a kill)."""
    t_end = time.time() + wait_s
    while True:
        alive = False
        for pid in os.listdir("/proc"):
            try:
                if pid.isdigit() and os.stat(f"/proc/{pid}").st_uid == uid:
                    alive = True
                    os.waitpid(int(pid), os.WNOHANG)
            except (OSError, ChildProcessError):
                pass
        if not alive or time.time() > t_end:
            return
        time.sleep(0.05)


LIVE = {"dir": None, "info": {}}   # the live folder of this run (prepare_live) and its run.json


def prepare_live(user):
    """The live folder (marsflight.live): MARSFLIGHT_LIVE_DIR, by default OUT/live; empty turns it off. Emptied of an
    earlier run's files, and closed to all but root and the owner of OUT (the host user, when OUT is a folder mounted
    from the host), never the flight user: flight software must not read the true state of its own flight."""
    from marsflight.live import live_dir
    d = live_dir(os.path.join(OUT, "live"))
    LIVE["dir"], LIVE["info"] = d, {}
    if not d:
        return None
    try:
        os.makedirs(d, exist_ok=True)
        for name in listdir(d):
            if name.endswith(".jsonl") or name in ("run.json", ".run.json.tmp"):
                os.unlink(os.path.join(d, name))
        os.chmod(d, 0o700)
        owner = os.stat(os.path.dirname(os.path.abspath(d)))
        flight_uid = pwd.getpwnam(user).pw_uid if user else None
        if os.geteuid() == 0 and owner.st_uid not in (0, flight_uid):
            os.chown(d, owner.st_uid, owner.st_gid)
    except OSError:
        pass
    return d


def live_run(**info):
    """Update run.json in the live folder: the run's phase, its size and, at the end, the outcome per world."""
    if LIVE["dir"]:
        from marsflight.live import write_run
        LIVE["info"].update(info)
        write_run(LIVE["dir"], LIVE["info"])


def write(reward, details):
    live_run(phase="done", score=reward, error=details.get("error"), finished=round(time.time(), 1))
    os.makedirs(OUT, exist_ok=True)
    details.setdefault("score", reward)   # also on the early rejections, which return before grading
    with open(os.path.join(OUT, "details.json"), "w") as f:
        json.dump(details, f, indent=1, default=float)
    with open(os.path.join(OUT, "reward.txt"), "w") as f:
        f.write(f"{reward:.6f}\n")
    print(json.dumps({k: v for k, v in details.items() if k not in ("flights", "cost", "ledger", "truth", "program_secret",
                                                                   "compiled_design")},
                     default=float))


def load_program():
    """The ledger of tests, the paid failure investigations and the secret. Without the plugin (no state on the
    hub), nothing was tested."""
    tests, investigations, secret, found = [], [], None, False
    try:
        with open(os.path.join(STATE, "program.json")) as f:
            state = json.load(f)
        tests, investigations = state.get("tests", []), state.get("investigations", [])
        found = True
    except (OSError, ValueError):
        pass
    try:
        with open(os.path.join(STATE, "secret.json")) as f:
            secret = json.load(f)["secret"]
    except (OSError, ValueError, KeyError):
        secret = secrets.token_hex(32)
    return tests, investigations, secret, found


def rnd_spent_usd(tests, investigations):
    """R&D spent, as the program office counts it: the tests not cancelled, and the root causes bought. The
    net value charges it, so nothing the office sells is free."""
    return (sum(float(t.get("cost_usd", 0.0)) for t in tests if t.get("status") != "cancelled")
            + sum(float(i.get("cost_usd", 0.0)) for i in investigations))


def main():
    made = []   # the flight user run() makes, removed even when it fails
    try:
        run(made)
    finally:
        os.environ.pop(NPROC_ENV, None)
        for user in made:
            remove_flight_user(user)


def run(made):
    LIVE["dir"], LIVE["info"] = None, {}
    design_path = os.path.join(APP, "design", "vehicle.json")
    # Every .sysml file under design/, subsystem folders included, as the program office compiles it.
    models = sorted(glob.glob(os.path.join(APP, "design", "**", "*.sysml"), recursive=True))
    flight = os.path.join(APP, "flight")
    if not (models or os.path.isfile(design_path)) or not os.path.isfile(os.path.join(flight, "guidance.py")):
        write(0.0, {"error": "design/**/*.sysml or flight/guidance.py missing on main"})
        return
    compiled_from = None
    if models:
        try:
            design, report = compile_files(models)
        except CompileError as ex:
            write(0.0, {"error": "the SysML model does not compile", "compile_errors": ex.errors,
                        "models": [os.path.relpath(m, APP) for m in models]})
            return
        compiled_from = [os.path.relpath(m, APP) for m in models]
        design_path = os.path.join(tempfile.mkdtemp(prefix="compiled-"), "vehicle.json")
        with open(design_path, "w") as f:
            json.dump(design, f, indent=1)
    else:
        try:
            with open(design_path) as f:
                design = json.load(f)
        except ValueError as ex:
            write(0.0, {"error": f"design/vehicle.json is not valid JSON: {ex}"})
            return
    tests, investigations, secret, plugin_state = load_program()
    record = P.record_from_ledger(tests)
    rnd_spent = rnd_spent_usd(tests, investigations)
    qualified = P.qualified_stages(design, record)
    vehicle = build_vehicle(design, qualified=qualified)
    cost = E.cost_report(vehicle, design)
    program = {"plugin_state_found": plugin_state, "tests_ordered": len(tests),
               "tests_done": sum(1 for t in tests if t.get("status") == "done"), "rnd_spent_usd": rnd_spent,
               "rnd_budget_usd": E.RND_BUDGET_USD, "qualified_tank_stages": qualified}
    ledger = [{k: t.get(k) for k in ("id", "agent", "kind", "key", "target_name", "cost_usd", "minutes", "status",
                                     "ordered_at", "started_at", "ends_at", "note", "outcome", "team_result")} for t in tests]
    # The run's secret goes into the verifier output (never seen by agents) so replays can recompute the
    # hidden truth of any earlier design: marsflight.program.flight_truth(secret, design, vehicle, record).
    base = {"n_flights": N, "program": program, "cost": cost, "ledger": ledger, "program_secret": secret,
            "compiled_from": compiled_from, "compiled_design": design if compiled_from else None}
    if vehicle.errors:
        write(0.0, {**base, "error": "design invalid", "design_errors": vehicle.errors})
        return
    if cost["over_budget"]:
        write(0.0, {**base, "error": f"hardware costs ${cost['hardware_usd'] / 1e6:.0f}M, over the "
                                     f"${cost['hardware_budget_usd'] / 1e6:.0f}M budget"})
        return
    # Gate G3: the test results the team saved must be ones the program office gave it. Read as root, before
    # any flight runs, like the forecast.
    records = G.records_honest(G.load_json(os.path.join(APP, "program", "test_results.json")), tests)
    base["gate"] = {"deliverables": True, "valid_design": True, "within_budget": True, "honest_records": records}
    if not records["ok"]:
        write(0.0, {**base, "error": "program/test_results.json holds results the program office never gave the team"})
        return
    forecast = G.load_json(os.path.join(APP, G.FORECAST_FILE))
    truth = P.flight_truth(secret, design, vehicle, record)
    factors = P.reliability_factors(secret, design)
    hz = P.hazards(design, record, factors)
    # Private copies the flight user can read but not change.
    work = "/var/tmp/marsflight-verify"
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    shutil.copy(design_path, os.path.join(work, "vehicle.json"))
    shutil.copytree(flight, os.path.join(work, "flight"))
    subprocess.run(["chmod", "-R", "a+rX,go-w", work], check=False)
    user, own_user = None, False
    if os.geteuid() == 0 and shutil.which("runuser"):
        user = make_flight_user()
        own_user = user is not None
        if not own_user:   # the image's user, with no RLIMIT_NPROC: its uid may be shared with the host
            try:
                subprocess.run(["id", "flight"], check=True, capture_output=True)
                user = "flight"
            except subprocess.CalledProcessError:
                user = None
    if user:
        become_subreaper()
    workers, base["workers_chosen"] = verify_workers(N)
    nproc = flight_nproc(workers)
    if own_user:
        made.append(user)
        os.environ[NPROC_ENV] = str(nproc)   # read by guidance_io in the Monte Carlo workers
    clear_flight_user(user)
    base["simulator_hidden"] = hide_simulator(user)
    app_mode = hide_app(user)   # undone below, once no flight software runs
    base["app_hidden"] = app_mode is not None and os.stat(APP).st_uid == 0 and not os.stat(APP).st_mode & 0o077
    # The 48 fixed worlds of the published scores (tests/seeds.json, MARSFLIGHT_VERIFY_SEED_FILE), the first N of them;
    # fresh random worlds when no seed file is given.
    seeds = (json.load(open(os.environ['MARSFLIGHT_VERIFY_SEED_FILE']))[:N] if os.environ.get('MARSFLIGHT_VERIFY_SEED_FILE')
             else [secrets.randbelow(2**31 - 1) for _ in range(N)])
    ld = prepare_live(user) or ""   # each flight's live progress, for the flight viewer (not part of this task)
    live_run(phase="flights", started=round(T_START, 1), flights_started=round(time.time(), 1), n_worlds=N,
             workers=workers, max_wall_s=MAX_WALL, budget_s=BUDGET_S)
    phase_s = base["phase_s"] = {}
    t_phase = time.time()
    deadline = T_START + BUDGET_S
    cost_in = {"hardware_usd": cost["hardware_usd"], "hardware_budget_usd": cost["hardware_budget_usd"],
               "rnd_budget_usd": cost["rnd_budget_usd"], "rnd_spent_usd": rnd_spent}
    design_in, guidance_in = os.path.join(work, "vehicle.json"), os.path.join(work, "flight", "guidance.py")
    lost = []
    try:
        results, summary = monte_carlo(design_in, guidance_in, seeds, workers=workers, user=user, max_wall_s=MAX_WALL,
                                       truth=truth, qualified=qualified, hz=hz, cost=cost_in, deadline=deadline,
                                       live_dir=ld)
    except BrokenProcessPool as ex:   # the kernel killed a simulator process: keep the flights that finished
        results = getattr(ex, "results", None) or [None] * len(seeds)
        lost = [i for i, r in enumerate(results) if r is None]
        clear_flight_user(user)   # the dead pool's flight software and files must not reach the flights flown again
        for i in lost:
            results[i] = fly_one(design_in, guidance_in, seeds[i], user, MAX_WALL, False, truth, qualified, deadline,
                                 live={"dir": ld, "kind": "world", "index": i} if ld else None)
            results[i]["flown_again_after_broken_pool"] = True
    phase_s["flights"], t_phase = round(time.time() - t_phase, 1), time.time()
    # Engine-out checks, then failure checks (grading.failure_check_cases): a parachute with a backup that fails to
    # open, a restartable stage whose first ignition does not light. Each failure check gets at most three times its
    # twin's wall time. Both kinds are chosen from the graded flights alone, so when the pool has room for all of them
    # at once they fly side by side in one pool (fly_side_by_side; each is a full mission, so this saves the time of
    # the shorter kind); otherwise one kind after the other, as before. Never more than `workers` flights at once.
    cases, eo, eo_results = [], {}, []
    fcases, fc, fc_results = [], {}, []
    if G.ENGINE_OUT_CHECKS and deadline - time.time() >= ENGINE_OUT_MIN_S:
        cases = G.engine_out_cases(design, results, hz)
    if G.FAILURE_CHECKS and cases and deadline - time.time() >= ENGINE_OUT_MIN_S:
        fcases = G.failure_check_cases(design, results)
    fc_wall = lambda: min(MAX_WALL, 3 * max((r.get("wall_s") or 0.0) for r in results  # noqa: E731
                                            if r["seed"] in {c[0] for c in fcases}) + 120)
    if cases or fcases:
        live_run(phase="checks", checks_started=round(time.time(), 1), n_engine_out=len(cases),
                 n_failure_check=len(fcases))
    if cases and fcases and len(cases) + len(fcases) <= workers:
        eo_results, fc_results = fly_side_by_side(design_in, guidance_in, user, truth, qualified, deadline,
                                                  [(c[0], MAX_WALL, c[1], None) for c in cases],
                                                  [(c[0], fc_wall(), None, c[1]) for c in fcases], live_dir=ld)
        phase_s["checks_side_by_side"] = True
    else:
        if cases:
            try:
                eo_results, _ = monte_carlo(design_in, guidance_in, [c[0] for c in cases], workers=workers, user=user,
                                            max_wall_s=MAX_WALL, truth=truth, qualified=qualified, deadline=deadline,
                                            engine_outs=[c[1] for c in cases], live_dir=ld, live_kind="engine_out")
            except BrokenProcessPool:   # no checks: every engine failure counts as lost
                eo_results = []
        phase_s["engine_out_checks"], t_phase = round(time.time() - t_phase, 1), time.time()
        fcases = []
        if G.FAILURE_CHECKS and deadline - time.time() >= ENGINE_OUT_MIN_S:
            fcases = G.failure_check_cases(design, results)
        if fcases:
            live_run(phase="checks", n_failure_check=len(fcases))
            try:
                fc_results, _ = monte_carlo(design_in, guidance_in, [c[0] for c in fcases], workers=workers, user=user,
                                            max_wall_s=fc_wall(), truth=truth, qualified=qualified, deadline=deadline,
                                            failure_checks=[c[1] for c in fcases], live_dir=ld,
                                            live_kind="failure_check")
            except BrokenProcessPool:   # no checks: those failures count as lost
                fc_results = []
    if cases:
        eo = G.engine_out_survival(cases, eo_results, results)
        hz = P.hazards(design, record, factors, eo)
    if fcases:
        fc = G.failure_check_survival(fcases, fc_results, results)
        hz = P.hazards(design, record, factors, eo, fc)
    phase_s["failure_checks" if "engine_out_checks" in phase_s else "checks"] = round(time.time() - t_phase, 1)
    if lost or cases or fcases:
        summary = score_flights(results, hz, cost_in)
    restore_app(app_mode)
    keep = ("seed", "expected_progress", "physical_progress", "p_full_success", "credit", "milestones", "partial",
            "failure", "failure_t", "end_t", "t0", "wall_s", "max_wall_s", "peaks", "guidance_calls", "trajectory",
            "killed_by_kernel", "flight_software_exit", "flown_again_after_broken_pool", "world", "landing_errors_km")
    flights = [{k: r.get(k) for k in keep} for r in results]
    containers = next((e.sample_containers for e in vehicle.elements if e.kind == "capsule"), 1)
    grade = G.grade(results, hz, cost["hardware_usd"] + rnd_spent, forecast, containers)
    for f, v, o in zip(flights, grade.pop("world_values"), grade.pop("world_outcomes")):
        f["grade_value"], f["grade_outcome"] = v, o
    LIVE["info"]["worlds"] = [{"index": i, "grade_value": f["grade_value"], "grade_outcome": f["grade_outcome"],
                               "failure": f["failure"], "wall_s": f["wall_s"]} for i, f in enumerate(flights)]
    v2 ={("v2_score" if k == "score" else k): v for k, v in summary.items()}
    progress = round(float(summary["mean_physical_progress"]), 6)
    write(progress, {"score": progress, "score_kind": "physical_progress", "cliff_score": grade["score"],
                     "grade": grade, **v2, **base, "flight_user": user,
                     "flight_uid": pwd.getpwnam(user).pw_uid if user else None,
                     "flight_limits": {"data_bytes": FS_DATA_BYTES, "nproc": nproc if own_user else None,
                                       "blas_threads": 1} if user else None,
                     "workers": workers, "flown_again_after_broken_pool": lost,
                     "budget_s": BUDGET_S, "verifier_s": round(time.time() - T_START, 1),
                     "flights_cut_short": sum(1 for r in results if (r.get("max_wall_s") or MAX_WALL) < MAX_WALL),
                     "reliability": hz["detail"], "reliability_factors": factors,
                     "hazards": {k: v for k, v in hz.items() if k not in ("detail", "factors")},
                     "engine_out_checks": [{"seed": c[0], **c[1], "weight": round(c[2], 6),
                                            "shut_down_at_met_s": (r.get("engine_out") or {}).get("at_met_s"),
                                            "failure": r.get("failure"),
                                            "kept": round(G.engine_out_kept(results[seeds.index(c[0])], r), 6)}
                                           for c, r in zip(cases, eo_results)],
                     "engine_out_survival": eo,
                     "failure_checks": [{"seed": c[0], **c[1], "at_met_s": (r.get("failure_check") or {}).get("at_met_s"),
                                         "failure": r.get("failure"),
                                         "kept": round(G.failure_check_kept(results[seeds.index(c[0])], r), 6)}
                                        for c, r in zip(fcases, fc_results)],
                     "failure_check_survival": fc, "truth": truth, "flights": flights})


if __name__ == "__main__":
    main()
