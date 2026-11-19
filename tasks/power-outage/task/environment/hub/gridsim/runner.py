"""Run a team's controllers on one scenario, each area in its own process.

Each area's controller runs under ctrlhost.py in a process of its own, from its own copy of the team's code (the
shared code and its own area folder). Run as root (the verifier, and the practice service when the board runs as
root), each area gets its own unprivileged user (ctl01..ctl16), a private home and temp dir, and resource limits, so
controllers cannot read the simulator, the scenario or each other. They talk only through the simulator's message
channel, and each is told only its own area's private data for the day (scenario.apply_day).

    python3 -m gridsim.runner --app DIR --seed N [--practice] [--only a01,a02] [--out result.json]

The grader passes the seed and the output path in BLACKSTART_SEED and BLACKSTART_OUT instead: a controller can read
its parent's command line in /proc, but not its environment.
"""
import argparse
import json
import os
import pwd
import resource
import selectors
import shutil
import stat
import subprocess
import sys
import tempfile
import time

from . import NUMERIC_ENV
from . import grid as G
from . import scenario as SC
from .sim import Sim

HOST = os.environ.get("BLACKSTART_HOST", "/opt/blackstart-host/ctrlhost.py")
PYTHON = os.environ.get("BLACKSTART_CTRL_PYTHON", sys.executable)
# A step's limit is the controller's own CPU time, not wall time: on a busy grading host a controller that needs 1.5 s
# of CPU can take far longer than 2 s of wall time, so a wall-time limit made the score depend on the host's load
# (one regrade of the same code timed out 118 steps, another 1). The wall-time limit is only a backstop for a
# controller that blocks without computing.
STEP_TIMEOUT = float(os.environ.get("BLACKSTART_STEP_TIMEOUT", "2.0"))
STEP_WALL_TIMEOUT = float(os.environ.get("BLACKSTART_STEP_WALL_TIMEOUT", "20.0"))
CLK_TCK = os.sysconf("SC_CLK_TCK")
INIT_TIMEOUT = 60.0
CPU_LIMIT_S = int(os.environ.get("BLACKSTART_CPU_LIMIT_S", "1800"))
MEM_LIMIT = int(os.environ.get("BLACKSTART_MEM_LIMIT_MB", "1536")) * 2 ** 20
MAX_ERRORS = 50


SKIP_DIRS = {".git", "__pycache__", "logs", "results"}


def _copy_app(src, area=None):
    """A read-only copy of the team's code for the controllers: areas/, lib/ and any other Python packages.
    Regular files and folders only. This runs as root, so a symbolic link (or a device or fifo) in the team's
    repository is left out rather than followed: a link to /opt/blackstart would hand the simulator to the
    controllers. With `area`, the copy is that area's: of areas/ it holds only areas/<area>/, so a controller
    has the team's shared code and its own folder, and nothing another area's folder holds."""
    root = tempfile.mkdtemp(prefix="bs-app-")
    dst = os.path.join(root, "app")
    for d, dirs, files in os.walk(src):
        rel = os.path.relpath(d, src)
        dirs[:] = [n for n in dirs if n not in SKIP_DIRS and not os.path.islink(os.path.join(d, n))
                   and not (area and rel == "areas" and n != area)]
        os.makedirs(os.path.join(dst, rel), mode=0o755, exist_ok=True)
        for f in files:
            p = os.path.join(d, f)
            if f.endswith(".pyc") or not stat.S_ISREG(os.lstat(p).st_mode):
                continue
            shutil.copyfile(p, os.path.join(dst, rel, f), follow_symlinks=False)
    os.chmod(root, 0o755)
    for d, dirs, files in os.walk(dst):
        os.chmod(d, 0o755)
        for f in files:
            os.chmod(os.path.join(d, f), 0o644)
    return root, dst


class Controller:
    def __init__(self, app, area, grid_pub, private, workdir):
        self.area = area
        self.errors = 0
        self.timeouts = 0
        self.disabled = False
        self.last_error = None
        self.buf = b""
        uid = None
        if os.geteuid() == 0:
            try:
                uid = pwd.getpwnam(f"ctl{area[1:]}").pw_uid
            except KeyError:
                uid = None
        # A home the controller cannot write to: there is no file it could leave for another area.
        home = os.path.join(workdir, area)
        os.makedirs(home, exist_ok=True)
        os.chmod(home, 0o711)
        self.uid = uid
        # its log is root-owned 0600: the controller writes it through the inherited stderr handle, and no other
        # area can open it (a world-readable log in the shared workdir was a free channel between areas)
        self.log = os.fdopen(os.open(os.path.join(workdir, f"{area}.log"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
                             "wb")
        os.fchmod(self.log.fileno(), 0o600)

        def demote():
            os.setsid()
            resource.setrlimit(resource.RLIMIT_CPU, (CPU_LIMIT_S, CPU_LIMIT_S + 5))
            resource.setrlimit(resource.RLIMIT_AS, (MEM_LIMIT, MEM_LIMIT))
            # its log (stderr) is the only file it writes; the audit hook in ctrlhost.py refuses opening any other
            resource.setrlimit(resource.RLIMIT_FSIZE, (50 * 2 ** 20, 50 * 2 ** 20))
            resource.setrlimit(resource.RLIMIT_NPROC, (64, 64)) if uid is not None else None
            if uid is not None:
                os.setgroups([])
                os.setgid(uid)
                os.setuid(uid)

        # The environment is the runner's own, nothing from the team. A fixed hash seed makes a controller that iterates
        # over a set of strings behave the same in every grading (Python randomises string hashing per process). So
        # Python must read this environment: -s -P (no user site, no script directory on sys.path) instead of -I,
        # whose -E would ignore it. The team's repository is not on sys.path at startup; the host adds it after
        # installing its guard.
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": home, "TMPDIR": home, "PYTHONHASHSEED": "0",
               "PYTHONDONTWRITEBYTECODE": "1", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
               "MKL_NUM_THREADS": "1", **NUMERIC_ENV}
        self.p = subprocess.Popen([PYTHON, "-s", "-P", "-B", HOST, app, area], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=self.log, cwd=home, env=env, preexec_fn=demote)
        os.set_blocking(self.p.stdout.fileno(), False)
        self.send({"type": "init", "area": area, "grid": grid_pub, "private": private, "step_s": SC.STEP_S,
                   "horizon_min": SC.HORIZON_MIN})
        if self.read_line(INIT_TIMEOUT) is None:
            self.disabled = True
            self.last_error = "controller did not start (see its log)"

    def send(self, msg):
        try:
            self.p.stdin.write((json.dumps(msg) + "\n").encode())
            self.p.stdin.flush()
        except (BrokenPipeError, OSError):
            self.disabled = True
            self.last_error = "controller process exited"

    def read_line(self, timeout):
        end = time.time() + timeout
        sel = selectors.DefaultSelector()
        sel.register(self.p.stdout, selectors.EVENT_READ)
        try:
            while True:
                if b"\n" in self.buf:
                    line, self.buf = self.buf.split(b"\n", 1)
                    return line
                # an answer that is already waiting is read even after the deadline: only a controller that has
                # not answered yet is late
                left = max(0.0, end - time.time())
                if not sel.select(left):
                    return None
                chunk = self.p.stdout.read(1 << 20)
                if not chunk:
                    if self.p.poll() is not None:
                        self.disabled = True
                        self.last_error = f"controller exited with {self.p.returncode}"
                        return None
                    continue
                self.buf += chunk
        finally:
            sel.close()

    def pop_line(self):
        """A complete line already read, or None."""
        if b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            return line
        return None

    def cpu_s(self):
        """The controller process's CPU time so far (user and system, all its threads), as the kernel counts it, or
        None when /proc cannot tell. ctrlhost.py's audit hook refuses new processes, so this is all the CPU a
        controller can use, and the controller cannot change what the kernel counts."""
        try:
            with open(f"/proc/{self.p.pid}/stat") as f:
                fields = f.read().rsplit(")", 1)[1].split()
            return (int(fields[11]) + int(fields[12])) / CLK_TCK
        except (OSError, IndexError, ValueError):
            return None

    def pump(self):
        """Read what the controller has written so far (it is ready to read)."""
        try:
            chunk = self.p.stdout.read(1 << 20)
        except (BlockingIOError, InterruptedError):
            return
        if chunk:
            self.buf += chunk
        elif chunk == b"":      # its output is closed: it can no longer answer
            self.disabled = True
            code = self.p.poll()
            self.last_error = f"controller exited with {code}" if code is not None else "controller closed its output"

    def close(self):
        try:
            self.send({"type": "stop"})
            self.p.wait(timeout=2)
        except Exception:
            pass
        try:
            os.killpg(self.p.pid, 9)
        except Exception:
            pass
        self.log.close()


def _scan_leftovers(uids):
    """Files the controllers' users left in the shared writable folders (there should be none)."""
    if not uids:
        return []
    found = []
    for d in ("/tmp", "/var/tmp", "/dev/shm"):
        try:
            for name in os.listdir(d):
                p = os.path.join(d, name)
                try:
                    if os.lstat(p).st_uid in uids:
                        found.append(p)
                except OSError:
                    pass
        except OSError:
            pass
    return found[:20]


CPU_POLL_S = 0.25   # how often a pending controller's CPU time is checked while it computes


def _collect(ctls, sent, step, cpu0):
    """Each controller's reply to this step: its parsed reply, "bad" (not JSON) or None (no reply in time).
    Every controller has STEP_TIMEOUT seconds of its own CPU time from the moment its own step was sent (cpu0: its
    CPU time then), and at most STEP_WALL_TIMEOUT seconds of wall time; all of them are read at once, so a reply
    counts exactly when it arrives within its controller's own limits, whatever the others do and however busy the
    host is. Where /proc cannot tell a controller's CPU time, STEP_TIMEOUT is wall time, as before. A late answer
    to an earlier step is skipped."""
    pending = {a for a in sent if not ctls[a].disabled}
    out = {}
    sel = selectors.DefaultSelector()
    for a in pending:
        sel.register(ctls[a].p.stdout, selectors.EVENT_READ, a)

    def wall_limit(a):
        return sent[a] + (STEP_WALL_TIMEOUT if cpu0.get(a) is not None else STEP_TIMEOUT)

    def over_cpu(a):
        now = ctls[a].cpu_s()
        return cpu0.get(a) is not None and now is not None and now - cpu0[a] > STEP_TIMEOUT

    def finish(a, rep):
        out[a] = rep
        pending.discard(a)
        sel.unregister(ctls[a].p.stdout)
    try:
        while pending:
            for a in list(pending):     # complete lines read so far
                while a in pending:
                    line = ctls[a].pop_line()
                    if line is None:
                        if ctls[a].disabled:
                            finish(a, None)
                        break
                    try:
                        rep = json.loads(line)
                    except ValueError:
                        finish(a, "bad")
                        break
                    if isinstance(rep, dict) and rep.get("step") != step:
                        continue        # a late answer to an earlier step
                    # an answer that took more than its CPU time is late, however soon it arrived
                    finish(a, None if over_cpu(a) else rep if isinstance(rep, dict) else "bad")
            now = time.time()
            for a in [a for a in pending if wall_limit(a) <= now or over_cpu(a)]:
                finish(a, None)
            if not pending:
                break
            # wait until the next reply arrives, the earliest pending controller's wall time is up, or it is time to
            # look at the pending controllers' CPU time again
            wait = min(wall_limit(a) for a in pending) - now
            if any(cpu0.get(a) is not None for a in pending):
                wait = min(wait, CPU_POLL_S)
            for key, _ in sel.select(max(0.0, wait)):
                ctls[key.data].pump()
    finally:
        sel.close()
    return out


def run(app, seed, only=None, keep_logs=None, collect_events=False, progress=None, hours=None):
    """Run one scenario. `only`: area names that run the team's controllers (the rest do nothing). `hours`: stop
    after that many simulated hours (a partial practice drill); the score covers the hours simulated."""
    g = G.build()
    scen = SC.build(g, seed)
    sim = Sim(g, scen, horizon_min=60.0 * hours if hours else SC.HORIZON_MIN)
    pub = G.public_view(g)
    roots = []
    work = tempfile.mkdtemp(prefix="bs-run-")
    os.chmod(work, 0o711)
    ctls = {}
    t0 = time.time()
    try:
        for a in range(G.N_AREAS):
            name = G.area_name(a)
            if only and name not in only:
                continue
            # each area's controller gets its own copy of the code (its own area folder only) and today's private
            # data of its own area (scenario.apply_day)
            root, app_copy = _copy_app(app, area=name)
            roots.append(root)
            if not os.path.exists(os.path.join(app_copy, "areas", name, "controller.py")):
                continue
            ctls[a] = Controller(app_copy, name, pub, G.area_private(g, a), work)
        inbox = [[] for _ in range(G.N_AREAS)]
        logs = {G.area_name(a): [] for a in range(G.N_AREAS)}
        while not sim.done():
            obs = [sim.observe(a) for a in range(G.N_AREAS)]
            if collect_events:
                for a in range(G.N_AREAS):
                    if obs[a]["events"] and len(logs[G.area_name(a)]) < 4000:
                        logs[G.area_name(a)].extend(obs[a]["events"])
            sent, cpu0 = {}, {}
            for a, c in ctls.items():
                if not c.disabled:
                    cpu0[a] = c.cpu_s()
                    c.send({"type": "step", "step": sim.step_i, "obs": obs[a], "inbox": inbox[a]})
                    sent[a] = time.time()
            actions = [[] for _ in range(G.N_AREAS)]
            outbox = [[] for _ in range(G.N_AREAS)]
            for a, rep in _collect(ctls, sent, sim.step_i, cpu0).items():
                c = ctls[a]
                if rep is None:
                    c.timeouts += 1
                elif rep == "bad":
                    c.errors += 1
                    c.last_error = "reply is not JSON"
                else:
                    if rep.get("error"):
                        c.errors += 1
                        c.last_error = rep["error"]
                    actions[a] = rep.get("actions") if isinstance(rep.get("actions"), list) else []
                    outbox[a] = rep.get("outbox") if isinstance(rep.get("outbox"), list) else []
            for a, c in ctls.items():
                if c.errors >= MAX_ERRORS and not c.disabled:
                    c.disabled = True
                    sim.ev(a, f"controller disabled after {MAX_ERRORS} errors: {str(c.last_error)[-300:]}")
            sim.step(actions)
            inbox = sim.route_messages(outbox)
            if progress and sim.step_i % 120 == 0:
                progress(sim)
        res = sim.score()
        res["isolation"] = _scan_leftovers([c.uid for c in ctls.values() if c.uid is not None])
        if collect_events:
            for a in range(G.N_AREAS):
                logs[G.area_name(a)].extend(sim.events[a])
            res["areas"] = {name: dict(events=ev) for name, ev in logs.items()}
        res.update(seed=seed, wall_s=round(time.time() - t0, 1), timeline=sim.timeline,
                   controllers={c.area: dict(errors=c.errors, timeouts=c.timeouts, disabled=c.disabled,
                                             last_error=(str(c.last_error)[-1500:] if c.last_error else None))
                                for c in ctls.values()})
        if keep_logs:
            os.makedirs(keep_logs, exist_ok=True)
            for c in ctls.values():
                c.close()
                try:
                    shutil.copy(os.path.join(work, f"{c.area}.log"), os.path.join(keep_logs, f"{c.area}.log"))
                except OSError:
                    pass
        return res, sim
    finally:
        for c in ctls.values():
            c.close()
        for root in roots:
            shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", required=True)
    ap.add_argument("--seed", type=int, default=None, help="default: $BLACKSTART_SEED")
    ap.add_argument("--practice", action="store_true", help="seed is a practice scenario number")
    ap.add_argument("--only", default="")
    ap.add_argument("--out", default=os.environ.get("BLACKSTART_OUT"))
    a = ap.parse_args()
    if a.seed is None:
        a.seed = int(os.environ["BLACKSTART_SEED"])
    seed = SC.practice_seed(a.seed) if a.practice else a.seed
    res, _ = run(a.app, seed, only=set(x for x in a.only.split(",") if x) or None)
    text = json.dumps(res, indent=1)
    if a.out:
        with open(a.out, "w") as f:
            f.write(text)
    else:
        print(text)


if __name__ == "__main__":
    main()
