"""Run the team's flight software in its own process and talk to it over JSON lines.

The flight software is a Python file defining `reset(info)` and `step(obs) -> dict`. It runs in a
separate process (as an unprivileged user in the verifier), so it can't reach into the simulator, the
dispersions or the verifier's output. A crash, a hang or a bad reply is a flight software failure,
which ends the flight.
"""
from __future__ import annotations

import collections
import json
import math
import os
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time


# Under a flight user (the verifier), the flight software and every process it starts may use at most this much
# data each: heap and private memory, RLIMIT_DATA. The largest seen is 0.64 GB (the reference, with one BLAS
# thread). Past it an allocation fails, a MemoryError in Python: the flight software's own failure.
FS_DATA_BYTES = 2 << 30
# RLIMIT_NPROC: the tasks (processes and threads) all flight software may have at once, set when the verifier made
# a flight user of its own for the run (it puts the bound in this variable). The kernel counts them per uid across
# the whole machine, so on a uid shared with the host, such as the image's "flight" (1000), it is not set.
NPROC_ENV = "MARSFLIGHT_FLIGHT_NPROC"


class GuidanceError(Exception):
    pass


def _clean(o):
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if hasattr(o, "tolist"):
        return _clean(o.tolist())
    return o


def _non_finite(token):
    raise GuidanceError(f"flight software replied with the non-finite number {token}; every number in a reply "
                        "must be finite")


def _finite_float(token):
    v = float(token)
    if not math.isfinite(v):
        _non_finite(token)
    return v


def parse_reply(line):
    """Parse one reply line. A NaN or infinite number (Python's json writes them as NaN and Infinity, and a
    literal like 1e999 overflows) raises GuidanceError: no command or score may be computed from one."""
    return json.loads(line, parse_constant=_non_finite, parse_float=_finite_float)


class GuidanceHost:
    def __init__(self, path, call_timeout_s=20.0, total_cpu_s=1800.0, user=None, extra_env=None):
        self.path = os.path.abspath(path)
        self.call_timeout_s = call_timeout_s
        self.total_cpu_s = total_cpu_s
        self.spent = 0.0
        self.user = user
        self.exit_code = None   # set when the flight software is found to have ended by itself
        self.home = None
        env = {k: v for k, v in os.environ.items() if not k.startswith("MARSFLIGHT_SECRET")}
        env["PYTHONUNBUFFERED"] = "1"
        if user:
            # An empty home and temporary directory of its own, removed at close, so that flights can't leave
            # anything for each other or for a flight flown again. Tudat finds its data at ~/.tudat.
            self.home = tempfile.mkdtemp(prefix="flight-")
            shutil.chown(self.home, user)
            tudat = os.path.expanduser("~/.tudat")
            if os.path.exists(tudat):
                os.symlink(os.path.realpath(tudat), os.path.join(self.home, ".tudat"))
            env["TMPDIR"] = self.home
            # One BLAS thread: numpy and scipy each start one per CPU of the host otherwise, and each thread's
            # buffers count against the data limit (5.7 to 6.0 GB on a 30-CPU host instead of about 0.6 GB).
            env.update(OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1")
        nproc = env.pop(NPROC_ENV, None)
        env.update(extra_env or {})
        if user:
            # The flight software must not reach the simulator (it is hidden from the team, and the verifier
            # hides its files from the flight user). The runner needs only the standard library, so it runs from
            # a copy in the flight's own home, in isolated mode: no PYTHONPATH, no user site, and not the
            # directory it came from. It puts the flight software's own folder on the path itself.
            runner = os.path.join(self.home, "guidance_runner.py")
            shutil.copyfile(os.path.join(os.path.dirname(os.path.abspath(__file__)), "guidance_runner.py"), runner)
            os.chmod(runner, 0o644)
            # RLIMIT_NPROC is set by the flight user itself, right after runuser: prlimit sets the hard limit
            # too, which an unprivileged process can't raise again. Set on root before runuser, it also bounded
            # runuser's own fork, and where root in the container is not the host's root (a user namespace, as on
            # some sandbox providers) the kernel counts every process of the container's root against it, so runuser could
            # not start the flight software at all ("cannot create child process").
            cmd = ["runuser", "-u", user, "--"] + (["prlimit", f"--nproc={nproc}", "--"] if nproc else []) + [
                "env", f"HOME={self.home}",   # runuser sets HOME itself
                sys.executable, "-I", runner, self.path]
            env.pop("PYTHONPATH", None)
            # set as root before runuser, so neither the flight software nor its children can raise it
            cmd = ["prlimit", f"--data={FS_DATA_BYTES}", "--"] + cmd
        else:
            cmd = [sys.executable, "-m", "marsflight.guidance_runner", self.path]
            pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            env["PYTHONPATH"] = pkg_root + os.pathsep + os.path.dirname(self.path) + os.pathsep + env.get("PYTHONPATH", "")
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     cwd=os.path.dirname(self.path), env=env, text=True, bufsize=1)
        # Drain the child's stderr all the time. Prints from the flight software and Tudat's warnings go
        # there, and an undrained pipe (8 to 64 KB) would block the child mid-flight. Keep the tail for errors.
        self._tail = collections.deque(maxlen=40)
        threading.Thread(target=self._drain, args=(self.proc.stderr, self._tail), daemon=True).start()

    @staticmethod
    def _drain(f, tail):
        try:
            for line in f:
                tail.append(line[-400:])
        except Exception:  # noqa: BLE001
            pass

    def _call(self, kind, payload):
        if self.proc.poll() is not None:
            raise self._exited()
        line = json.dumps({"kind": kind, "data": _clean(payload)})
        t0 = time.time()
        try:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()
        except BrokenPipeError:
            raise self._exited()
        r, _, _ = select.select([self.proc.stdout], [], [], self.call_timeout_s)
        if not r:
            self.close()
            raise GuidanceError(f"flight software did not answer within {self.call_timeout_s:.0f} s")
        out = self.proc.stdout.readline()
        self.spent += time.time() - t0
        if self.spent > self.total_cpu_s:
            self.close()
            raise GuidanceError(f"flight software used more than {self.total_cpu_s:.0f} s in total")
        if not out:
            raise self._exited()
        try:
            msg = parse_reply(out)
        except json.JSONDecodeError:
            raise GuidanceError(f"flight software wrote a non-JSON line: {out[:200]!r}")
        if msg.get("error"):
            raise GuidanceError(f"flight software raised: {msg['error'][-1500:]}")
        return msg.get("data")

    def _exited(self):
        """The flight software ended by itself: keep its exit status for the verifier's kill check."""
        try:
            self.exit_code = self.proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass
        return GuidanceError(f"flight software exited: {self._stderr()}")

    @property
    def sigkilled(self):
        """The flight software ended from SIGKILL before the host stopped it. runuser reports a child killed by
        a signal as 128 + the signal (and prints the signal's name, "Killed", to stderr); -9 is runuser itself
        killed. A flight software can also exit with 137 or kill itself, so this alone is not an OOM kill."""
        return self.exit_code in ((-9, 128 + 9) if self.user else (-9,))

    def _stderr(self):
        time.sleep(0.05)   # let the drain thread catch the last lines
        return "".join(self._tail)[-1500:]

    def reset(self, info):
        return self._call("reset", info)

    def step(self, obs):
        return self._call("step", obs)

    def close(self):
        """Stop the flight software and remove its home."""
        self._stop()
        if self.home:
            shutil.rmtree(self.home, ignore_errors=True)

    def _stop(self):
        """Stop the flight software. A kill would stop only runuser and leave the flight software running (about
        0.5 GB for each flight flown), so close its input first, at the end of which it stops, then send SIGTERM,
        which runuser passes on to it, and kill only as a last resort."""
        if self.proc.poll() is not None:
            return
        try:
            self.proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        for stop, wait_s in ((None, 2.0), (self.proc.terminate, 5.0), (self.proc.kill, 5.0)):
            try:
                if stop:
                    stop()
                self.proc.wait(timeout=wait_s)
                return
            except Exception:  # noqa: BLE001
                continue


class InProcessGuidance:
    """For development only: import the flight software into this process (fast, no isolation)."""

    def __init__(self, module):
        self.m = module

    def reset(self, info):
        if hasattr(self.m, "reset"):
            return self.m.reset(json.loads(json.dumps(_clean(info))))

    def step(self, obs):
        return self.m.step(json.loads(json.dumps(_clean(obs))))

    def close(self):
        pass
