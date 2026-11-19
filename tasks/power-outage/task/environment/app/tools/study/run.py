"""The study simulator: play the team's controllers on a blackout the team defines, in your own sandbox, as often as
you like. It is a low-fidelity stand-in for the true simulator (see README.md for what it leaves out).

    python3 tools/study/run.py tools/study/example_blackout.json
    python3 tools/study/run.py my_blackout.json --private private.json --only a01,a02 --hours 4 --out result.json

Each area's controller runs in its own process through the same host the grader uses (/opt/blackstart-host/
ctrlhost.py, with its audit hook), sees only its own observation and talks only through the message channel, as in
grading. --inprocess runs them all in this process instead (faster, no isolation: module globals are shared).

Memory: each controller process has the grader's address-space limit (BLACKSTART_MEM_LIMIT_MB, 1536) and is the
first thing the kernel kills if the sandbox runs out of memory, never your own tools. At most a few runs play their
16 processes at once in a sandbox (BLACKSTART_STUDY_SLOTS, by default one per 1.5 GB of the sandbox's memory beyond
3 GB: 3 in 8 GB); a run started while every slot is busy waits for one.
"""
import argparse
import contextlib
import fcntl
import importlib.util
import json
import os
import resource
import selectors
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
from engine import grid as G  # noqa: E402
from engine import scenario as SC  # noqa: E402
from engine.sim import Sim  # noqa: E402

HOST = os.environ.get("BLACKSTART_HOST", "/opt/blackstart-host/ctrlhost.py")
MEM_LIMIT = int(os.environ.get("BLACKSTART_MEM_LIMIT_MB", "1536")) * 2 ** 20   # as the grader's runner
SLOT_DIR = "/tmp/.blackstart-study-slots"


def sandbox_memory():
    """The sandbox's memory limit in bytes (cgroup v2, then v1), else the machine's memory."""
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            v = open(path).read().strip()
            if v != "max" and int(v) < 1 << 60:
                return int(v)
        except (OSError, ValueError):
            pass
    return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")


def study_slots():
    try:
        return max(1, int(os.environ["BLACKSTART_STUDY_SLOTS"]))
    except (KeyError, ValueError):
        return max(1, int((sandbox_memory() - 3 * 2 ** 30) // (1.5 * 2 ** 30)))


@contextlib.contextmanager
def study_slot():
    """Hold one of study_slots() lock files while the run's controller processes live; wait while all are taken."""
    n = study_slots()
    os.makedirs(SLOT_DIR, exist_ok=True)
    try:
        os.chmod(SLOT_DIR, 0o1777)
    except OSError:
        pass
    waited = False
    while True:
        for i in range(n):
            f = open(os.path.join(SLOT_DIR, f"slot{i}.lock"), "a")
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                f.close()
                continue
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
                f.close()
            return
        if not waited:
            print(f"study: all {n} study slots of this sandbox are busy; waiting for one", file=sys.stderr, flush=True)
            waited = True
        time.sleep(1)


def controller_limits():
    """In each controller process: the grader's memory limit, and first in line for the out-of-memory killer."""
    try:
        resource.setrlimit(resource.RLIMIT_AS, (MEM_LIMIT, MEM_LIMIT))
    except (ValueError, OSError):
        pass
    try:
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write("1000")
    except OSError:
        pass


class Proc:
    """One area's controller in its own process (the grader's protocol: one JSON line each way per step)."""

    def __init__(self, app, area, pub, private, log):
        self.area, self.buf, self.disabled, self.errors, self.timeouts, self.last_error = area, b"", False, 0, 0, None
        self.log = open(log, "wb")
        # as grading: a fixed hash seed and no other PYTHON* variables (the host puts the repository on sys.path)
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        env.update(PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1", OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1",
                   MKL_NUM_THREADS="1")
        self.p = subprocess.Popen([sys.executable, "-s", "-P", "-B", HOST, app, area], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=self.log, env=env, preexec_fn=controller_limits)
        os.set_blocking(self.p.stdout.fileno(), False)
        self.send({"type": "init", "area": area, "grid": pub, "private": private, "step_s": SC.STEP_S,
                   "horizon_min": SC.HORIZON_MIN})
        if self.read(60) is None:
            self.disabled, self.last_error = True, "controller did not start (see its log)"

    def send(self, msg):
        try:
            self.p.stdin.write((json.dumps(msg) + "\n").encode())
            self.p.stdin.flush()
        except OSError:
            self.disabled, self.last_error = True, "controller process exited"

    def read(self, timeout):
        end = time.time() + timeout
        sel = selectors.DefaultSelector()
        sel.register(self.p.stdout, selectors.EVENT_READ)
        try:
            while True:
                if b"\n" in self.buf:
                    line, self.buf = self.buf.split(b"\n", 1)
                    return line
                if not sel.select(max(0.0, end - time.time())):
                    return None
                chunk = self.p.stdout.read(1 << 20)
                if not chunk:
                    if self.p.poll() is not None:
                        self.disabled, self.last_error = True, f"controller exited with {self.p.returncode}"
                        return None
                    continue
                self.buf += chunk
        finally:
            sel.close()

    def step(self, step, obs, inbox, timeout):
        self.send({"type": "step", "step": step, "obs": obs, "inbox": inbox})
        end = time.time() + timeout
        while True:
            line = self.read(max(0.0, end - time.time()))
            if line is None:
                self.timeouts += 1
                return [], []
            try:
                rep = json.loads(line)
            except ValueError:
                self.errors += 1
                return [], []
            if rep.get("step") != step:
                continue
            if rep.get("error"):
                self.errors += 1
                self.last_error = rep["error"]
            return (rep.get("actions") if isinstance(rep.get("actions"), list) else [],
                    rep.get("outbox") if isinstance(rep.get("outbox"), list) else [])

    def close(self):
        try:
            self.send({"type": "stop"})
            self.p.wait(timeout=2)
        except Exception:
            self.p.kill()
        self.log.close()


class InProc:
    def __init__(self, app, area, pub, private):
        spec = importlib.util.spec_from_file_location(f"study_{area}", os.path.join(app, "areas", area, "controller.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.ctl, self.area, self.disabled, self.errors, self.timeouts, self.last_error = (
            mod.make_controller(area, pub, private), area, False, 0, 0, None)

    def step(self, step, obs, inbox, timeout):
        try:
            return self.ctl.step(obs, inbox)
        except Exception as e:
            self.errors += 1
            self.last_error = repr(e)
            return [], []

    def close(self):
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("blackout", help="the blackout spec (JSON); see README.md")
    ap.add_argument("--app", default=APP, help="the repository whose areas/ to play (default: this one)")
    ap.add_argument("--private", help="JSON {area: area_data(area)} for the areas you have data for")
    ap.add_argument("--day", type=int, help="rehearse a day: vary the --private data within the grader's day-to-day "
                    "ranges (README.md, Today's values); each controller is given its own area's varied data")
    ap.add_argument("--only", default="", help="areas whose controllers run (the rest stay idle)")
    ap.add_argument("--hours", type=float, default=16)
    ap.add_argument("--step-timeout", type=float, default=2.0, help="seconds per step, as in grading")
    ap.add_argument("--inprocess", action="store_true")
    ap.add_argument("--out", help="write the result (score, timeline, every area's events) here")
    ap.add_argument("--logs", help="keep each area's controller log (print output) in this folder")
    a = ap.parse_args()
    pub = json.load(open(os.path.join(a.app, "grid", "public_grid.json")))
    private = json.load(open(a.private)) if a.private else {}
    if a.day is not None:
        private = SC.day_private(private, pub, a.day)
    g = G.build(pub, private)
    scen = SC.build(g, json.load(open(a.blackout)))
    sim = Sim(g, scen, horizon_min=60 * a.hours)
    only = {x for x in a.only.split(",") if x}
    logs = a.logs or tempfile.mkdtemp(prefix="study-logs-")
    os.makedirs(logs, exist_ok=True)
    inproc = a.inprocess or not os.path.exists(HOST)
    if inproc:
        sys.path.insert(0, a.app)       # as the controller host does: shared code imports as lib
    slot = contextlib.ExitStack()
    if not inproc:
        slot.enter_context(study_slot())
    ctls = {}
    for k in range(G.N_AREAS):
        name = G.area_name(k)
        if (only and name not in only) or not os.path.exists(os.path.join(a.app, "areas", name, "controller.py")):
            continue
        priv = private.get(name, {})
        ctls[k] = InProc(a.app, name, pub, priv) if inproc else Proc(a.app, name, pub, priv,
                                                                   os.path.join(logs, f"{name}.log"))
    events = {G.area_name(k): [] for k in range(G.N_AREAS)}
    inbox = [[] for _ in range(G.N_AREAS)]
    t0 = time.time()
    try:
        while not sim.done():
            obs = [sim.observe(k) for k in range(G.N_AREAS)]
            for k in range(G.N_AREAS):
                events[G.area_name(k)].extend(obs[k]["events"])
            actions = [[] for _ in range(G.N_AREAS)]
            outbox = [[] for _ in range(G.N_AREAS)]
            for k, c in ctls.items():
                if not c.disabled:
                    actions[k], outbox[k] = c.step(sim.step_i, obs[k], inbox[k], a.step_timeout)
            sim.step(actions)
            inbox = sim.route_messages(outbox)
    finally:
        for c in ctls.values():
            c.close()
        slot.close()
    res = sim.score()
    res.update(hours=a.hours, wall_s=round(time.time() - t0, 1), timeline=sim.timeline, areas=events,
               controllers={c.area: dict(errors=c.errors, timeouts=c.timeouts, last_error=c.last_error)
                            for c in ctls.values()})
    if a.out:
        with open(a.out, "w") as f:
            json.dump(res, f)
    print(json.dumps({k: res[k] for k in ("score", "raw_score", "bound", "restoration_index", "served_fraction", "collapses",
                                          "nuclear_missed", "damage", "out_of_phase", "wall_s")}))
    for row in sim.timeline[::6]:
        print(f"  {row['t']:6.0f} min  served {row['served_mw']:7.0f} of {row['demand_mw']:7.0f} MW, "
              f"{row['islands']} islands")
    if not inproc:
        print(f"controller logs: {logs}")


if __name__ == "__main__":
    main()
