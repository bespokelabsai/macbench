"""The team's software as a policy for the simulator: ops/autopilot.py in a process of its own.

The verifier and the market office copy the team's ops/ folder to a fresh folder, and run it with
ops_runner.py, as an unprivileged user when they run as root, with limits it cannot raise:

- MEMORY_BYTES of address space, one BLAS thread, and at most NPROC processes.
- CALL_TIMEOUT seconds for any one call, and WORLD_SECONDS of wall time for a whole world.
- PLAYBOOK_WORLD_SECONDS for each 1,095-day playbook world, and TEST_MARKET_SECONDS for a 182-day test market:
  the same pace per day, so a test market rehearses the playbook's limit (the playbook
  ran 1,095 days on the 300 s sized for a 182-day test market, six times tighter than the documented practice).

A call that fails, times out or returns something that is not a JSON object raises SoftwareError, and
the simulator then keeps the software's last actions for the rest of the world (sim.Sim.run).
"""
import json
import os
import resource
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MEMORY_BYTES = 2 * 2 ** 30
NPROC = 64
CALL_TIMEOUT = 20.0
WORLD_SECONDS = 300.0
PLAYBOOK_WORLD_SECONDS = 600.0
TEST_MARKET_SECONDS = 100.0     # 600 s x 182 / 1,095 days, rounded
TEAM_PYTHON = os.environ.get("SEEDPROFIT_TEAM_PYTHON", "/usr/local/bin/python3")


class SoftwareError(Exception):
    pass


def links_in(root):
    """Paths under root that are not plain files or folders, e.g., symbolic links. Root copies the team's files, so a
    link to the office's secret or the simulator would hand the software its contents (a reward-hack review). The team's folders may hold only plain files."""
    bad = []
    for d, dirs, files in os.walk(root):
        for name in dirs + files:
            p = os.path.join(d, name)
            if os.path.islink(p) or not (os.path.isdir(p) or os.path.isfile(p)):
                bad.append(os.path.relpath(p, root))
    return sorted(bad)


STATE_MAX_BYTES = 50 << 20    # a live company's software state folder (v1.0)


def folder_bytes(root):
    return sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(root) for f in fs)


def copy_ops(src, owner=None, state=None):
    """A fresh copy of the team's ops/ folder and the runner, readable by the software's user, with a `state`
    folder: empty, or a copy of the live company's saved state (v1.0)."""
    bad = links_in(src)
    if bad or os.path.islink(src):
        raise SoftwareError("ops/ may hold only plain files and folders, not links: " + ", ".join(bad[:10] or ["ops"]))
    work = tempfile.mkdtemp(prefix="ops-")
    dst = os.path.join(work, "ops")
    shutil.copytree(src, dst, symlinks=False, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy(os.path.join(HERE, "ops_runner.py"), os.path.join(work, "ops_runner.py"))
    if state and os.path.isdir(state) and not links_in(state):
        shutil.copytree(state, os.path.join(work, "state"), symlinks=True)
    else:
        os.makedirs(os.path.join(work, "state"))
    home = os.path.join(work, "home")
    os.makedirs(home)
    if owner is not None:
        uid, gid = owner
        for root, dirs, files in os.walk(work):
            for name in dirs + files:
                os.lchown(os.path.join(root, name), uid, gid)
        os.chown(work, uid, gid)
    os.chmod(work, 0o700)
    return work


def copy_plain_tree(parent, name, dst, refused):
    """Copy the folder parent/name to dst as root, following no link at any level: the folder, every subfolder
    and every file are opened by descriptor with O_NOFOLLOW, so a link anywhere (the folder itself included) fails
    the copy or lands in `refused`. Files that are not plain or have another name (hard links) are refused too."""
    pfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        top = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=pfd)
    finally:
        os.close(pfd)
    try:
        for d, dirs, files, dfd in os.fwalk(".", dir_fd=top, follow_symlinks=False):
            out = os.path.normpath(os.path.join(dst, d))
            os.makedirs(out, exist_ok=True)
            for f in files:
                rel = os.path.normpath(os.path.join(d, f))
                try:
                    fd = os.open(f, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0), dir_fd=dfd)
                except OSError:
                    refused.append(rel)
                    continue
                try:
                    st = os.fstat(fd)
                    if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
                        refused.append(rel)
                        continue
                    with os.fdopen(os.dup(fd), "rb") as fin, open(os.path.join(out, f), "wb") as fout:
                        shutil.copyfileobj(fin, fout)
                finally:
                    os.close(fd)
            for sub in list(dirs):      # fwalk lists a link to a folder here and does not enter it: refuse it
                if not stat.S_ISDIR(os.stat(sub, dir_fd=dfd, follow_symlinks=False).st_mode):
                    refused.append(os.path.normpath(os.path.join(d, sub)))
                    dirs.remove(sub)
    finally:
        os.close(top)


# System calls the team's software may not make: System V IPC (shared memory, semaphores, message queues), POSIX
# message queues and sockets. The playbook runs its worlds side by side, each as its own user, and a 0666 shared memory segment or a
# socket on the loopback would let the worlds pool what they learn, while the manual says each world runs on its own
# (a reward-hack review). Containers refuse IPC and network namespaces, so a seccomp filter makes these
# calls fail with EPERM instead. The software must not use the network anyway. Pipes and socketpair() still work.
_DENY = {
    # x86_64: socket, shm*, sem*, msg*, semtimedop, mq_* (POSIX message queues), io_uring_* (it can open sockets)
    0xC000003E: (41, 29, 30, 31, 64, 65, 66, 67, 68, 69, 70, 71, 220, 240, 241, 242, 243, 244, 245, 425, 426, 427),
    0xC00000B7: (198, 186, 187, 188, 189, 190, 191, 192, 193, 194, 195, 196, 197, 180, 181, 182, 183, 184, 185,
                 425, 426, 427),
}


def deny_ipc_and_sockets():
    """Install the seccomp filter in the software's process (call it last in the child, after setuid). Linux only;
    a no-op elsewhere (macOS, local checks)."""
    if not sys.platform.startswith("linux"):
        return
    import ctypes
    import struct
    ld_w_abs, jeq_k, jge_k, ret_k = 0x20, 0x15, 0x35, 0x06
    allow, deny = 0x7FFF0000, 0x00050000 | 1       # SECCOMP_RET_ALLOW, SECCOMP_RET_ERRNO | EPERM
    prog = [(ld_w_abs, 0, 0, 4)]                   # the architecture
    blocks = []
    for arch, nrs in _DENY.items():
        body = [(ld_w_abs, 0, 0, 0)]               # the system call number
        if arch == 0xC000003E:
            body.append((jge_k, len(nrs) + 1, 0, 0x40000000))   # x32 calls: deny
        for i, nr in enumerate(nrs):
            body.append((jeq_k, len(nrs) - i, 0, nr))
        body += [(ret_k, 0, 0, allow), (ret_k, 0, 0, deny)]
        blocks.append((arch, body))
    # dispatch on the architecture: jump to its block, or allow an architecture we do not know
    head = []
    offsets, at = [], 0
    for _, body in blocks:
        offsets.append(at)
        at += len(body)
    n = len(blocks)
    for i, (arch, _) in enumerate(blocks):
        head.append((jeq_k, (n - i - 1) + 1 + offsets[i], 0, arch))
    head.append((ret_k, 0, 0, allow))
    prog += head
    for _, body in blocks:
        prog += body
    raw = b"".join(struct.pack("HBBI", *f) for f in prog)
    buf = ctypes.create_string_buffer(raw)

    class Fprog(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]

    fp = Fprog(len(prog), ctypes.addressof(buf))
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0:           # PR_SET_NO_NEW_PRIVS
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_NO_NEW_PRIVS) failed")
    if libc.prctl(22, 2, ctypes.byref(fp), 0, 0) != 0:   # PR_SET_SECCOMP, SECCOMP_MODE_FILTER
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_SECCOMP) failed")


class ExternalPolicy:
    def __init__(self, ops_dir, owner=None, python=None, world_seconds=WORLD_SECONDS, call_timeout=CALL_TIMEOUT,
                 state=None):
        self.owner = owner
        self.state = state       # a live company's state folder, kept between months (v1.0)
        self.work = copy_ops(ops_dir, owner, state)
        self.state_dir = os.path.join(self.work, "state")
        self.python = python or (TEAM_PYTHON if os.path.exists(TEAM_PYTHON) else sys.executable)
        self.deadline = None
        self.world_seconds = world_seconds
        self.call_timeout = call_timeout
        self.proc = None
        self.err = None          # the software's stderr: an unnamed root-owned file outside the team's folder
        self.calls = 0

    def _start(self):
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": os.path.join(self.work, "home"),
               "TMPDIR": os.path.join(self.work, "home"), "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
               "MKL_NUM_THREADS": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0"}
        owner = self.owner

        def limits():
            os.setsid()
            for lim, val in ((resource.RLIMIT_AS, MEMORY_BYTES), (resource.RLIMIT_CORE, 0)):
                try:
                    resource.setrlimit(lim, (val, val))
                except (ValueError, OSError):   # macOS, for local checks
                    pass
            if owner is not None:
                resource.setrlimit(resource.RLIMIT_NPROC, (NPROC, NPROC))
                os.setgroups([])
                os.setgid(owner[1])
                os.setuid(owner[0])
            try:
                deny_ipc_and_sockets()
            except OSError as e:     # a kernel without seccomp filters (or an emulator): run, and say so on stderr
                os.write(2, f"note: IPC and socket blocking unavailable here ({e})\n".encode())

        # Not a file in the team's folder: the software owns that folder and could swap the file for a link to a
        # root-only file, which _tail would then read back to it as root (a reward-hack review).
        if self.err is None:
            self.err = tempfile.TemporaryFile()
        # -s -P, not -I: -I also ignores PYTHON* variables, and PYTHONHASHSEED=0 above must hold so that the live
        # company and its replay run the same software the same way. The environment
        # is built from nothing above, so no other PYTHON* variable reaches the software.
        self.proc = subprocess.Popen([self.python, "-s", "-P", os.path.join(self.work, "ops_runner.py"),
                                      os.path.join(self.work, "ops")],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.err, env=env,
                                     cwd=os.path.join(self.work, "ops"), preexec_fn=limits)
        self.deadline = time.monotonic() + self.world_seconds

    def _call(self, cmd, data):
        if self.proc is None:
            self._start()
        if self.proc.poll() is not None:
            raise SoftwareError(f"the software's process ended (exit code {self.proc.returncode}){self._tail()}")
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise SoftwareError(f"the software used up its {self.world_seconds:.0f} s for this world")
        try:
            self.proc.stdin.write((json.dumps({"cmd": cmd, "data": data}) + "\n").encode())
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            raise SoftwareError(f"the software's process ended{self._tail()}") from None
        wait = min(self.call_timeout, left)
        line = b""
        end = time.monotonic() + wait
        while not line.endswith(b"\n"):
            r, _, _ = select.select([self.proc.stdout], [], [], max(0.0, end - time.monotonic()))
            if not r:
                raise SoftwareError(f"the software did not answer within {wait:.0f} s")
            chunk = os.read(self.proc.stdout.fileno(), 1 << 20)
            if not chunk:
                raise SoftwareError(f"the software's process ended{self._tail()}")
            line += chunk
            if len(line) > 4 << 20:
                raise SoftwareError("the software's answer is larger than 4 MB")
        self.calls += 1
        try:
            res = json.loads(line.decode(errors="replace").splitlines()[0])
        except ValueError:
            raise SoftwareError("the software's answer is not JSON") from None
        if not res.get("ok"):
            raise SoftwareError(str(res.get("error", "unknown error"))[:1500])
        return res.get("data")

    def _tail(self):
        """The last 1,500 bytes the software wrote to stderr, read from the open file, never by a path."""
        try:
            if self.err is None:
                return ""
            self.err.flush()
            fd = self.err.fileno()
            size = os.fstat(fd).st_size
            t = os.pread(fd, 1500, max(0, size - 1500)).decode(errors="replace").strip()
            return f": {t}" if t else ""
        except OSError:
            return ""

    def init(self, setup):
        self._call("init", dict(setup, state_dir=self.state_dir))

    def save_state(self):
        """Keep the software's state folder for the live company's next month: plain files only, at most
        STATE_MAX_BYTES. The software's process must be stopped first (stop()), so it cannot change the folder while
        root copies it; the copy follows no link, and a copy that holds any link, special file or hard link is not
        kept (a red-team check: a link swapped in between the check and the copy handed root's files to the
        software; a reward-hack review: a link put in place of the state folder itself was followed).
        Returns a note when the folder could not be kept."""
        if not self.state:
            return None
        if self.proc is not None and self.proc.poll() is None:
            raise RuntimeError("save_state needs the software's process stopped")
        tmp = self.state + ".new"
        shutil.rmtree(tmp, ignore_errors=True)
        refused = []

        try:
            copy_plain_tree(self.work, "state", tmp, refused)
        except (OSError, shutil.Error) as e:
            shutil.rmtree(tmp, ignore_errors=True)
            return f"the state folder could not be copied ({type(e).__name__}); it was not kept"
        bad = links_in(tmp) + refused
        if bad:
            shutil.rmtree(tmp, ignore_errors=True)
            return "the state folder held links (" + ", ".join(bad[:5]) + "); it was not kept"
        if folder_bytes(tmp) > STATE_MAX_BYTES:
            shutil.rmtree(tmp, ignore_errors=True)
            return f"the state folder is larger than {STATE_MAX_BYTES >> 20} MB; it was not kept"
        if os.geteuid() == 0:       # kept as root's, out of the team's reach between months
            for d, dirs, files in os.walk(tmp):
                for name in dirs + files:
                    os.lchown(os.path.join(d, name), 0, 0)
        os.chmod(tmp, 0o700)
        shutil.rmtree(self.state, ignore_errors=True)
        os.replace(tmp, self.state)
        return None

    def stop(self):
        """End the software's process (and every process it started), keeping its folders for save_state."""
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.stdin.write(b'{"cmd": "close"}\n')
                self.proc.stdin.flush()
                self.proc.wait(timeout=2)
            except Exception:
                pass
        if self.proc is not None:
            try:
                os.killpg(self.proc.pid, 9)       # its children too: none may outlive the month
            except Exception:
                pass
            try:
                self.proc.wait(timeout=5)
            except Exception:
                pass
        if self.owner is not None:
            kill_user(self.owner[0])              # and any process that left the group: the user is this run's own

    def step(self, obs):
        act = self._call("step", obs)
        return act if isinstance(act, dict) else {}

    def close(self):
        if self.proc is not None:
            try:
                self.proc.stdin.write(b'{"cmd": "close"}\n')
                self.proc.stdin.flush()
                self.proc.wait(timeout=2)
            except Exception:
                pass
            if self.proc.poll() is None:
                try:
                    os.killpg(self.proc.pid, 9)
                except Exception:
                    self.proc.kill()
            try:
                self.err.close()
            except Exception:
                pass
        shutil.rmtree(self.work, ignore_errors=True)

    def stderr_tail(self, n=2000):
        try:
            fd = self.err.fileno()
            return os.pread(fd, n, max(0, os.fstat(fd).st_size - n)).decode(errors="replace")
        except (AttributeError, OSError, ValueError):
            return ""


def kill_user(uid, rounds=20):
    """Kill every process of uid (a user made for one run of team software), until none is left."""
    for _ in range(rounds):
        pids = []
        for d in os.listdir("/proc") if os.path.isdir("/proc") else []:
            if d.isdigit():
                try:
                    if os.stat(f"/proc/{d}").st_uid == uid:
                        pids.append(int(d))
                except OSError:
                    pass
        if not pids:
            return
        for pid in pids:
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        time.sleep(0.05)


def id_range():
    """The uids and gids to pick team users from, [low, high): 10^6 to 2 * 10^6 where this process's user namespace
    maps them, else the top half of what it maps (some sandboxes map only 0 to 65535, and setgid to an id
    outside the map fails with EINVAL), below 65534 (nobody)."""
    top = 2 ** 32 - 1
    for p in ("/proc/self/uid_map", "/proc/self/gid_map"):
        try:
            rows = [line.split() for line in open(p) if line.strip()]
            top = min(top, max(int(inside) + int(count) for inside, _, count in rows))
        except (OSError, ValueError):
            pass
    if top >= 2_000_000:
        return 1_000_000, 2_000_000
    return top // 2, min(top, 65534)


def make_user(prefix="stp-ops"):
    """An unprivileged user for running team software, when we are root: a random uid and gid (id_range) that no
    account and no process has. It gets no /etc/passwd entry (the software runs by number, with its own HOME), so
    nothing here writes /etc/passwd: a useradd killed part way leaves /etc/passwd.lock behind, and every useradd
    after it fails (the live company of an earlier run stopped at month 7 that way, and its final
    grading failed). Returns (uid, gid, name), or None when not root."""
    if os.geteuid() != 0:
        return None
    import grp
    import pwd
    import secrets
    low, high = id_range()
    for _ in range(50):
        uid = low + secrets.randbelow(high - low)
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
        if uid_running(uid):
            continue
        return uid, uid, f"{prefix}{uid}"
    raise RuntimeError("could not pick a user for the team's software")


def uid_running(uid):
    """Whether any process runs as uid (Linux /proc)."""
    try:
        pids = [d for d in os.listdir("/proc") if d.isdigit()]
    except OSError:
        return False
    for pid in pids:
        try:
            if os.stat(f"/proc/{pid}").st_uid == uid:
                return True
        except OSError:
            pass
    return False


def remove_user(user):
    if user is None:
        return
    uid, gid, name = user
    subprocess.run(["pkill", "-9", "-u", str(uid)], capture_output=True)
    for d in SHARED_TMP:      # anything the user left in shared folders
        subprocess.run(["find", d, "-xdev", "-uid", str(uid), "-delete"], capture_output=True)


SHARED_TMP = ("/tmp", "/var/tmp", "/dev/shm", "/run/lock", "/var/lock", "/dev/mqueue")   # every folder others may write


def close_shared_tmp():
    """Team software gets its own TMPDIR. Close the shared temp folders to it (root keeps full access, others may
    only pass through), so two processes of team software cannot leave files for each other."""
    if os.geteuid() != 0:
        return
    for p in SHARED_TMP:
        if os.path.isdir(p):
            os.chmod(p, 0o1711)


class grading_lock:
    """While the verifier runs graded worlds, the market office runs no team software (plan_review, test_market),
    so graded-world software and the agents' software are never alive at the same time. The office writes
    STATE/office-busy-<pid> while it runs team software; the verifier waits for those to end first."""

    def __init__(self, state, wait_s=1200):
        self.path = os.path.join(state, "grading.lock")
        self.state = state
        self.wait_s = wait_s

    def __enter__(self):
        if not os.path.isdir(self.state):
            return self
        with open(self.path, "w") as f:
            f.write(str(os.getpid()))
        end = time.time() + self.wait_s
        while time.time() < end and office_busy(self.state):
            time.sleep(2)
        return self

    def __exit__(self, *exc):
        try:
            os.remove(self.path)
        except OSError:
            pass


def office_busy(state):
    busy = False
    for name in os.listdir(state) if os.path.isdir(state) else []:
        if name.startswith("office-busy-"):
            tail = name.rsplit("-", 1)[1]
            pid = int(tail) if tail.isdigit() else 0
            try:
                os.kill(pid, 0)
                busy = True
            except OSError:
                try:
                    os.remove(os.path.join(state, name))
                except OSError:
                    pass
    return busy


def grading_now(state):
    """True while a grading runs, unless its lock is stale (its process is gone)."""
    p = os.path.join(state, "grading.lock")
    try:
        pid = int(open(p).read().strip() or 0)
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class office_busy_marker:
    """Marks that the market office is running team software now (see grading_lock)."""

    def __init__(self, state):
        self.path = os.path.join(state, f"office-busy-{os.getpid()}") if state and os.path.isdir(state) else None

    def __enter__(self):
        if self.path:
            open(self.path, "w").close()
        return self

    def __exit__(self, *exc):
        if self.path:
            try:
                os.remove(self.path)
            except OSError:
                pass
