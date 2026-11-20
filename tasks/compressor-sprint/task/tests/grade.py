"""Grades the team's compressor on hidden files. Submission code runs only as uid 65534.

Per hidden file t (about 1 MiB each: one of each example kind, cut from a file that is in neither the
public data nor the practice samples, plus kinds the team has no example of at all):
  s_t    = on a percentage (log) scale, where every 1% smaller counts the same: 0 at the zero point,
           1.0 at the state of the art, and 0.01 more for every 1% smaller than the state of the art:
             team_t >= sota_t:  clamp(ln(zero_t / team_t) / ln(zero_t / sota_t), 0, 1)
             team_t <  sota_t:  1 + ln(sota_t / team_t)
           or 0 if the round trip fails or breaks a limit.
           zero_t is the smaller of lpaq1's size and baseline_t, the best of xz, zstd, brotli and bzip2 at
           their strongest settings; sota_t is the smaller of paq8px v217 at -9LT and, for image files, JPEG XL lossless
           (cjxl -d 0 -e 9, or -e 9 -I 100 -g 3 if smaller) on that same file. Both are precomputed
           (targets.json), since paq8px takes minutes per file.
  reward = the mean over kinds of the mean of s_t over each kind's files, at most 1.0. (The geometric mean of
           team_t / sota_t is reported too, for information only.)

Whatever the score, report.md and details.json record the team's sizes and times next to every tool's,
each hidden file's sha256 and the sha256 of the submitted source, and the grader's output ends with a
RESULTS line (JSON) holding every file's numbers, so a run can be re-scored under another formula.
A submission over the size cap scores 0, but it is still built and graded, and details.json records
what it would have scored ("score_if_allowed"). A grader crash still writes all three files, with the
traceback in details.json ("grader_error"). If a process of the submission survives being killed between
phases, the run scores 0, since what crossed the phases cannot be trusted.
"""
import hashlib, io, json, lzma, math, os, resource, shutil, signal, subprocess, sys, tarfile, threading, time, traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TESTS = Path(__file__).resolve().parent
APP = Path(os.environ.get("MULTI_AGENT_HARNESS_GRADE_APP") or "/app")
OUT = Path(os.environ.get("MULTI_AGENT_HARNESS_GRADE_OUT") or "/logs/verifier")
TARGETS = {t: v for t, v in json.loads((TESTS / "targets.json").read_text()).items() if not t.startswith("_")}
for _t, _v in TARGETS.items():  # a type whose SOTA does not beat the standard tools has no scale to score on
    assert _v["sota_bytes"] and _v["sota_bytes"] < _v["baseline_bytes"], f"targets.json: no valid SOTA for {_t}"
HIDDEN = TESTS / "hidden"
SEALED = Path("/var/lib/compressor-sprint/sealed")
SEALED_TYPE = "json"
USER = 65534             # build.sh and the read probes run as this one uid
RUN_USERS = tuple(range(65470, 65534))  # a distinct uid per concurrent run: a 0700 run dir then really isolates
ALL_USERS = (*RUN_USERS, USER)          # one file cannot read or corrupt another's run directory, or hide CPU in it
RUN_POOL = RUN_USERS                    # the uids really used, settled per grading: a user namespace may map only some
CPU_LIMIT_S = 900.0      # per file, per direction (user + sys, every process of the run, reaped or not)
WALL_LIMIT_S = 1200.0
MEM_LIMIT = 8 << 30      # address space per process
OUT_LIMIT = 16 << 20
SIZE_CAP = 1 << 20       # xz -9e size of everything in compressor/ but bin/
RAW_CAP = 4 << 30        # measuring stops past 4 GiB of uncompressed source (no real submission comes near)
BUILD_TIMEOUT_S = 900
WORK = Path("/var/tmp/cs-grade")


class Escaped(Exception):
    """A process of the submission survived being killed, so nothing graded after it can be trusted."""


def source_files(src: Path):
    """The submitted files: everything in compressor/ but .git and bin/, which is build output and is
    deleted before the build (build.sh must make all of it). Symlinks are followed, so whatever a link
    points to counts as submitted (system pseudo-files under /proc, /sys and /dev are skipped). A generator,
    so measuring can stop part way through a huge tree."""
    seen = set()
    for root, dirs, files in os.walk(src, followlinks=True):
        real = os.path.realpath(root)
        if real in seen or real.startswith(("/proc", "/sys", "/dev")):
            dirs[:] = []
            continue
        seen.add(real)
        top = Path(root) == src
        dirs[:] = sorted(d for d in dirs if d != ".git" and not (top and d == "bin"))
        for f in sorted(files):
            p = Path(root) / f
            if f != ".git" and p.is_file() and not os.path.realpath(p).startswith(("/proc", "/sys", "/dev")):
                yield p


class _OverCap(Exception):
    pass


class _XzCounter(io.RawIOBase):
    """Counts the xz -9e size and the sha256 of what is written to it, without keeping it. Raises _OverCap
    (once) as soon as the compressed size passes SIZE_CAP or the raw size passes RAW_CAP."""
    def __init__(self):
        self.xz, self.h = lzma.LZMACompressor(preset=9 | lzma.PRESET_EXTREME), hashlib.sha256()
        self.n = self.raw = 0
        self.over = False

    def writable(self):
        return True

    def write(self, b):
        if not self.over:
            self.h.update(b)
            self.raw += len(b)
            self.n += len(self.xz.compress(b))
            if self.n > SIZE_CAP or self.raw > RAW_CAP:
                self.over = True
                raise _OverCap
        return len(b)


def program_bytes(src: Path) -> dict:
    """xz -9e size of a deterministic tar (PAX, so paths of any length) of the submitted source tree, before
    building, and the tar's sha256 (which identifies the code graded). It is measured as it streams, so a huge
    tree cannot exhaust the grader: measuring stops once the size is over the cap ("complete": False)."""
    out = _XzCounter()
    try:
        with tarfile.open(fileobj=out, mode="w|", format=tarfile.PAX_FORMAT) as tar:
            for p in source_files(src):
                info = tarfile.TarInfo(str(p.relative_to(src)))
                info.size, info.mode = p.stat().st_size, 0o644
                with open(p, "rb") as f:
                    tar.addfile(info, f)
        out.n += len(out.xz.flush())
        return {"bytes": out.n, "complete": True, "sha256": out.h.hexdigest()}
    except _OverCap:
        return {"bytes": out.n, "complete": False, "sha256": None,
                "raw_bytes_read": out.raw, "stopped": "over the cap" if out.n > SIZE_CAP else "over 4 GiB uncompressed"}


def drop_privileges(mem: int, uid: int = USER):
    os.setsid()
    os.setgroups([])
    os.setgid(uid)
    os.setuid(uid)
    resource.setrlimit(resource.RLIMIT_AS, (mem, mem))


def run_limits():
    resource.setrlimit(resource.RLIMIT_CPU, (int(CPU_LIMIT_S * 1.5), int(CPU_LIMIT_S * 1.5)))
    resource.setrlimit(resource.RLIMIT_FSIZE, (OUT_LIMIT, OUT_LIMIT))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def run_preexec(uid: int):
    """preexec for a graded run: a fresh session as `uid` with the per-run limits."""
    def f():
        drop_privileges(MEM_LIMIT, uid)
        run_limits()
    return f


def as_user():  # the read probes, as one non-root uid (any run uid is denied the same way)
    drop_privileges(MEM_LIMIT, USER)
    run_limits()


def as_builder():
    drop_privileges(max(MEM_LIMIT, 4 << 30), USER)


_CLK = os.sysconf("SC_CLK_TCK")
_PAGE = resource.getpagesize()


def proc_usage(uid: int) -> tuple:
    """(CPU seconds, RSS bytes) summed over every live, non-zombie process of `uid` right now. Summing the
    whole set, sampled often, counts forked workers whether or not the submission reaps them, so CPU and
    memory cannot be hidden by never calling wait()."""
    cpu, rss = 0.0, 0
    try:
        names = os.listdir("/proc")
    except OSError:
        return 0.0, 0
    for d in names:
        if not d.isdigit():
            continue
        try:
            fields = {}
            with open(f"/proc/{d}/status") as fh:
                for line in fh:
                    k, _, v = line.partition(":")
                    fields[k] = v
            if str(uid) not in fields.get("Uid", "").split()[:2] or fields.get("State", " ").strip()[:1] == "Z":
                continue
            with open(f"/proc/{d}/stat") as fh:
                after = fh.read().rpartition(") ")[2].split()  # fields from 'state' on, past a comm with spaces
            cpu += (int(after[11]) + int(after[12])) / _CLK   # utime + stime
            rss += int(after[21]) * _PAGE                      # resident pages
        except (OSError, ValueError, IndexError):
            continue
    return cpu, rss


def run_user(argv, cwd: Path, wall: float, uid: int):
    """Run as `uid` in cwd. Returns (returncode, cpu s over every process of the run, wall s, peak RSS MB,
    timed out). A sampler watches every process of `uid`, so forked workers count even if never reaped, and
    a run that passes the CPU or memory limit is killed at once so it cannot steal from a run beside it."""
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(cwd), "TMPDIR": str(cwd), "LANG": "C.UTF-8"}
    began = time.monotonic()
    p = subprocess.Popen(argv, cwd=cwd, env=env, preexec_fn=run_preexec(uid), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    timed_out = threading.Event()
    done = threading.Event()
    peak = {"cpu": 0.0, "rss": 0}

    def killpg():
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def on_wall():
        timed_out.set()
        killpg()
    timer = threading.Timer(wall, on_wall)
    timer.start()

    def sample():
        while not done.wait(0.3):
            cpu, rss = proc_usage(uid)
            peak["cpu"] = max(peak["cpu"], cpu)
            peak["rss"] = max(peak["rss"], rss)
            if cpu > CPU_LIMIT_S or rss > MEM_LIMIT:  # over its own budget: stop it before it crowds a neighbour
                killpg()
    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    _, status, ru = os.wait4(p.pid, 0)
    timer.cancel()
    done.set()
    sampler.join(timeout=2)
    p.returncode = os.waitstatus_to_exitcode(status)
    killpg()  # nothing of the run may outlive it
    cpu = max(ru.ru_utime + ru.ru_stime, peak["cpu"])
    rss_mb = max(round(ru.ru_maxrss / 1024), round(peak["rss"] / (1 << 20)))
    return (p.returncode, cpu, time.monotonic() - began, rss_mb, timed_out.is_set())


def user_dir(path: Path, uid: int = USER) -> Path:
    path.mkdir(parents=True)
    os.chown(path, uid, uid)
    path.chmod(0o700)
    return path


def usable_run_users() -> tuple:
    """RUN_USERS if this host lets the grader own files as them and run as them, else just (USER,). A sandbox
    with a narrow user namespace may map only some ids; grading then still works, on one shared uid, and
    details.json says so rather than every file failing."""
    probe = WORK / ".uidprobe"
    try:
        probe.mkdir(parents=True, exist_ok=True)
        for uid in (RUN_USERS[0], RUN_USERS[-1]):
            os.chown(probe, uid, uid)
            if subprocess.run(["true"], preexec_fn=run_preexec(uid), stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
                return (USER,)
        return RUN_USERS
    except Exception:  # noqa: BLE001 - any refusal means fall back to the one uid that is known to work
        return (USER,)
    finally:
        shutil.rmtree(probe, ignore_errors=True)


def live_user_pids() -> list[int]:
    """Processes running as USER (by real or effective uid), zombies aside: they run no code."""
    pids = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            status = dict(line.split(":", 1) for line in (d / "status").read_text().splitlines() if ":" in line)
        except (OSError, ValueError):
            continue
        if not status.get("State", "").strip().startswith("Z") and (
                set(status.get("Uid", "").split()[:2]) & {str(u) for u in ALL_USERS}):
            pids.append(int(d.name))
    return pids


KILL_ALL = "import os, signal\ntry:\n    os.kill(-1, signal.SIGKILL)\nexcept OSError:\n    pass\n"


def killer_preexec(uid: int):
    def f():
        os.setgroups([])
        os.setgid(uid)
        os.setuid(uid)
    return f


def kill_user_processes() -> list[int]:
    """SIGKILL every process of USER; returns any still alive. kill(-1) sent as USER signals all of USER's
    processes in one kernel pass that a process forking in a loop cannot outrun (no new process can appear
    while it runs, and a fork caught mid-way is cancelled), unlike pkill, which lists processes and then kills
    them one by one."""
    # Always kill first: a process that forks in a loop changes pid faster than /proc can be listed, so
    # looking before killing can miss it. The kill reaches it whatever its pid; the look afterwards checks.
    for i in range(25):
        for uid in ALL_USERS:
            subprocess.run([sys.executable, "-I", "-S", "-c", KILL_ALL], preexec_fn=killer_preexec(uid),
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            for flag in ("-u", "-U"):  # and as root, by effective and real uid, for what the uid may not signal itself
                subprocess.run(["pkill", "-KILL", flag, str(uid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        time.sleep(0.2)
        alive = live_user_pids()
        if not alive and i >= 1:  # two clean passes at least
            return []
    return alive


def user_ipc() -> list[tuple]:
    """System V shared memory, message queues and semaphores of USER, as (ipcrm flag, id). They outlive
    the processes that made them, so they could carry data from compressing to decompressing."""
    found = []
    for kind, flag in (("shm", "-m"), ("msg", "-q"), ("sem", "-s")):
        try:
            head, *rows = Path(f"/proc/sysvipc/{kind}").read_text().splitlines()
        except (OSError, ValueError):
            continue
        cols = head.split()
        for row in rows:
            f = dict(zip(cols, row.split()))
            owner = next((int(f[k]) for k in ("uid", "cuid") if f.get(k) and int(f[k]) in ALL_USERS), None)
            if owner is not None:
                found.append((flag, f[cols[1]], owner))
    return found


VIRTUAL_FS = {"proc", "sysfs", "cgroup", "cgroup2", "devpts", "securityfs", "debugfs", "tracefs", "pstore", "bpf",
              "configfs", "fusectl", "binfmt_misc", "autofs", "hugetlbfs", "nsfs", "efivarfs", "selinuxfs",
              "rpc_pipefs"}


def sweep_roots() -> list[str]:
    """/ and every other mounted directory outside /dev: find -xdev stays on one filesystem, and Harbor
    bind-mounts /logs/verifier (0777) and its siblings, which a sweep of / alone never enters. /dev is left
    out: in user-namespaced sandboxes its device nodes show as uid 65534."""
    roots = ["/", "/dev/shm", "/dev/mqueue", "/tmp", "/var/tmp", "/run"]
    try:
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            left, _, right = line.partition(" - ")
            point = left.split()[4].encode().decode("unicode_escape")  # mountinfo writes a space as \040
            fstype = right.split()[0] if right else ""
            if (fstype not in VIRTUAL_FS and fstype != "devtmpfs" and not point.startswith(("/proc", "/sys", "/dev"))
                    and point not in roots and os.path.isdir(point)):  # /dev/shm and /dev/mqueue are listed above
                roots.append(point)
    except (OSError, IndexError, UnicodeDecodeError):
        pass
    return roots


def sweep_user_files():
    """Kill every process of the submission's user and delete everything it owns (files, POSIX shared memory
    and message queues, System V IPC), so no state crosses phases. Raises Escaped if anything survives."""
    alive = kill_user_processes()
    if alive:
        raise Escaped(f"{len(alive)} process(es) of the submission survived being killed")
    owned = []  # ( -uid N ) over every uid the submission may run as
    for u in ALL_USERS:
        owned += ["-uid", str(u), "-o"]
    owned = ["(", *owned[:-1], ")"]
    for root in sweep_roots():
        if os.path.exists(root):
            subprocess.run(["find", root, "-xdev", *owned, "-not", "-path", "/proc/*", "-not", "-type", "c",
                            "-not", "-type", "b", "-delete"], check=False, stderr=subprocess.DEVNULL)  # never a device
    for flag, ident, owner in user_ipc():  # as their owner: root in a container usually lacks CAP_IPC_OWNER
        subprocess.run(["ipcrm", flag, ident], preexec_fn=killer_preexec(owner), check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if user_ipc():
        raise Escaped("System V IPC objects of the submission could not be removed")


def lock_hidden() -> bool:
    """Make the tests folder root-only; True if the submission's user can still read a hidden file."""
    for p in (TESTS, HIDDEN):
        try:
            p.chmod(0o700)
        except OSError:
            pass
    probe = HIDDEN / sorted(TARGETS)[0]
    rc = subprocess.run(["head", "-c", "1", str(probe)], preexec_fn=as_user, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL).returncode
    return rc == 0


def lock_private() -> list[str]:
    """Make the public data and the repo root-only, so the submission's build and runs read nothing but
    their own copy of compressor/ (taken before this) and the system's files. Returns what uid 65534
    can still read."""
    still = []
    for d in (Path("/data"), APP):
        if not d.is_dir():
            continue
        try:
            d.chmod(0o700)
        except OSError:
            pass
        probe = next((p for p in sorted(d.rglob("*")) if p.is_file()), None)
        if probe and subprocess.run(["head", "-c", "1", str(probe)], preexec_fn=as_user, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL).returncode == 0:
            still.append(str(d))
    return still


def user_can_run(path: Path) -> bool:
    """Whether the submission's user, which runs the programs, may execute path (root's view would not tell)."""
    return path.is_file() and subprocess.run(["test", "-x", str(path)], preexec_fn=as_user, stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL).returncode == 0


def build(src: Path, dst: Path) -> dict:
    def skip(d, names):  # .git anywhere, and the top-level bin/: build.sh must make the programs itself
        return [n for n in names if n == ".git" or (n == "bin" and Path(d) == src)]
    shutil.copytree(src, dst, symlinks=True, ignore=skip)
    for p in [dst, *dst.rglob("*")]:
        os.lchown(p, USER, USER)
    script = dst / "build.sh"
    if not script.is_file():
        return {"ok": False, "detail": "missing compressor/build.sh"}
    home = user_dir(WORK / "build-home")
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(home), "TMPDIR": str(home), "LANG": "C.UTF-8"}
    began = time.monotonic()
    try:
        proc = subprocess.run(["/bin/bash", str(script)], cwd=dst, env=env, preexec_fn=as_builder,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
                              timeout=BUILD_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        sweep_user_files()
        return {"ok": False, "detail": f"build.sh exceeded {BUILD_TIMEOUT_S} s"}
    # From here on the built submission is read-only to the submission's own code.
    for p in [dst, *dst.rglob("*")]:
        os.lchown(p, 0, 0)
        if not p.is_symlink():  # readable by all; a file executable by anyone (a 0700 build) becomes executable by all
            m = p.stat().st_mode
            p.chmod((m | 0o555) & 0o755 if p.is_dir() else (m | 0o444 | (0o111 if m & 0o111 else 0)) & 0o755)
    sweep_user_files()  # the build may not leave anything behind outside the submission
    ok = proc.returncode == 0 and all(user_can_run(dst / "bin" / b) for b in ("compress", "decompress"))
    return {"ok": ok, "returncode": proc.returncode, "seconds": round(time.monotonic() - began, 1),
            "detail": "" if ok else (proc.stdout[-1500:] + proc.stderr[-1500:]).strip() or "bin/compress or bin/decompress missing or not executable"}


def workers() -> int:
    """Files graded at once: half the CPUs, at most 4, and no more than memory allows at the cap."""
    try:
        mem = int(Path("/sys/fs/cgroup/memory.max").read_text())
    except (OSError, ValueError):
        mem = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    return max(1, min(4, (os.cpu_count() or 1) // 2, mem // MEM_LIMIT))


def phase(sub: Path, kind: str, inputs: dict) -> dict:
    """Run bin/<kind> on every input in its own fresh directory; return per-type results."""
    tool = str(sub / "bin" / kind)

    order = sorted(inputs)
    uid_of = {t: RUN_POOL[i % len(RUN_POOL)] for i, t in enumerate(order)}  # its own uid, so a sibling dir is unreadable

    def one(t):
        uid = uid_of[t]
        d = user_dir(WORK / kind / t, uid)
        shutil.copyfile(inputs[t], d / "input")
        os.chown(d / "input", uid, uid)
        rc, cpu, wall, rss, timed_out = run_user([tool, "input", "output"], d, WALL_LIMIT_S, uid)
        res = {"rc": rc, "cpu_s": round(cpu, 2), "wall_s": round(wall, 2), "peak_rss_mb": rss}
        out = d / "output"
        if timed_out:
            res["error"] = f"wall time over {WALL_LIMIT_S:.0f} s"
        elif cpu > CPU_LIMIT_S:
            res["error"] = f"CPU time {cpu:.1f} s over {CPU_LIMIT_S:.0f} s"
        elif rss > MEM_LIMIT >> 20:
            res["error"] = f"memory {rss} MB over {MEM_LIMIT >> 20} MB"
        elif rc != 0:
            res["error"] = f"exit code {rc}"
        elif not out.is_file() or out.is_symlink():
            res["error"] = "no output file"
        else:
            keep = WORK / "store" / f"{kind}-{t}"
            shutil.copyfile(out, keep)
            keep.chmod(0o600)
            res["output"] = str(keep)
        return t, res

    with ThreadPoolExecutor(workers()) as pool:
        results = dict(pool.map(one, order))
    sweep_user_files()
    shutil.rmtree(WORK / kind, ignore_errors=True)
    return results


TOOL_NAMES = ["xz -9e", "zstd --ultra -22 --long=27", "brotli -q 11 -w 24", "bzip2 -9"]
REFERENCE_NAMES = ["lpaq1", "paq8px-9LT", "jpegxl-lossless", "paq8px-12LT", "cmix"]


def cell(r: dict | None) -> str:
    if not r or r.get("bytes") is None:
        return "-"
    return f"{r['bytes']} ({r.get('compress_cpu_s', '?')}/{r.get('decompress_cpu_s', '?')})"


def render_report(details: dict, types: list) -> str:
    """Readable comparison of the team's compressor with each standard tool and the strongest known
    compressors, written whatever the score, so a zero still shows how close the team got."""
    b = details.get("build") or {"ok": False, "detail": "not reached"}
    lines = [f"# Compressor grade: {details['reward']:.4f}", ""]
    if details.get("grader_error"):
        lines.append("GRADER ERROR, scored 0 (the traceback is in details.json):  "
                     + details["grader_error"].strip().splitlines()[-1][:300])
    for w in details.get("grader_warnings", []):
        lines.append(f"GRADER WARNING: {w}")
    if details.get("readable_during_grading"):
        lines.append(f"GRADER WARNING: the submission could still read {', '.join(details['readable_during_grading'])} while graded.")
    if details.get("disqualified"):
        lines.append(f"DISQUALIFIED, scored 0: {details['disqualified'][:300]}")
        if details.get("score_if_allowed") is not None:
            lines.append(f"Graded anyway for the record: it would have scored {details['score_if_allowed']:.4f} "
                         "(per type in brackets below).")
    lines.append(f"Build: {'ok' if b['ok'] else 'FAILED'}" + (f" in {b.get('seconds')} s" if b.get("seconds") is not None else "")
                 + (f" ({b['detail'][:300]})" if b.get("detail") else ""))
    if "program_bytes" in details:
        pb = details["program_bytes"]
        lines.append(f"Submitted source, xz -9e: {pb} bytes (cap {SIZE_CAP})" if details.get("program_complete", True)
                     else f"Submitted source, xz -9e: over {pb} bytes, measuring stopped {details.get('program_stopped')} (cap {SIZE_CAP})")
        if details.get("program_sha256"):
            lines.append(f"Submitted source tar sha256: {details['program_sha256']}")
    if details.get("vs_sota_overall") is not None:
        lines.append(f"Overall vs SOTA (geometric mean of size / SOTA over {len(details['kinds'])} kinds, for information): "
                     f"{details['vs_sota_overall']:+.2%}; beats the state of the art overall: "
                     + ("yes" if details["beats_sota_overall"] else "no"))
    lines += ["", "Bytes are compressed sizes of each hidden file (about 1 MiB); (a/b) are CPU seconds to compress / decompress. "
              "A file scores 0 at the smaller of lpaq1 and the best standard tool, and 1.0 at the state of the art (SOTA), on a percentage scale "
              "where every 1% smaller counts the same, and 0.01 more for every 1% smaller than SOTA. A file that fails "
              "scores 0. A kind scores the mean of its files, and the run scores the mean of its kinds.", "",
              "| type | kind | score | team bytes | team bits/byte | team cpu | team peak MB | round trip | vs SOTA | best standard | SOTA | "
              + " | ".join(TOOL_NAMES) + " | " + " | ".join(REFERENCE_NAMES) + " |",
              "|" + "---|" * (11 + len(TOOL_NAMES) + len(REFERENCE_NAMES))]
    tot = {"team": 0, "best": 0, "sota": 0, **{n: 0 for n in TOOL_NAMES}}
    team_complete = True
    for t in types:
        r = details["types"][t]
        c, d = r.get("compress", {}), r.get("decompress", {})
        size = r.get("compressed_bytes")
        if size is not None:
            tot["team"] += size
        else:
            team_complete = False
        err = c.get("error") or d.get("error")
        rt = "yes" if r.get("roundtrip_ok") else (err or ("no" if "roundtrip_ok" in r else "-"))
        vs = f"{size / r['sota_bytes'] - 1:+.1%}" if size is not None and r.get("roundtrip_ok") else "-"
        tot["best"] += r["baseline_bytes"]
        tot["sota"] += r["sota_bytes"]
        for n in TOOL_NAMES:
            tot[n] += r["tools"][n]["bytes"]
        team = f"{size} | {8 * size / r['size']:.3f}" if size is not None else "- | -"
        peak = max(c.get("peak_rss_mb", 0), d.get("peak_rss_mb", 0)) or "-"
        sc = f"{r['score']:.3f}" + (f" ({r['score_if_allowed']:.3f})" if "score_if_allowed" in r else "")
        lines.append(f"| {t} | {r['kind']} | {sc} | {team} | {c.get('cpu_s', '-')} / {d.get('cpu_s', '-')} | {peak} | "
                     f"{rt} | {vs} | {r['baseline_bytes']} ({r['baseline_tool'].split()[0]}) | "
                     f"{r['sota_bytes']} ({r['sota_tool']}) | "
                     + " | ".join(cell(r["tools"][n]) for n in TOOL_NAMES) + " | "
                     + " | ".join(cell(r["references"].get(n)) for n in REFERENCE_NAMES) + " |")
    orig = sum(details["types"][t]["size"] for t in types)

    def pct(x):
        return f"{x} ({x / orig:.1%} of original, {8 * x / orig:.3f} bits/byte)"
    lines += ["", f"Totals over {len(types)} files ({orig} bytes):", ""]
    if team_complete:
        lines.append(f"- team: {pct(tot['team'])}")
        lines.append(f"  - vs best standard per type: {tot['team'] / tot['best'] - 1:+.1%}; vs SOTA per type: {tot['team'] / tot['sota'] - 1:+.1%}")
    else:
        lines.append(f"- team: some files have no output; {tot['team']} bytes over the others")
    lines.append(f"- best standard tool per type: {pct(tot['best'])}")
    lines.append(f"- SOTA per type: {pct(tot['sota'])}")
    for n in TOOL_NAMES:
        lines.append(f"- {n}: {pct(tot[n])}")
    for n in REFERENCE_NAMES:
        sizes = [(details["types"][t]["references"].get(n) or {}).get("bytes") for t in types]
        if all(s is not None for s in sizes):
            lines.append(f"- {n}: {pct(sum(sizes))}")
    lines += ["", "Raw results, to re-score under another formula: each hidden file's sha256, its size, the team's "
              "compressed size and the two reference points (also in details.json and on the RESULTS line of the "
              "grader's output).", "",
              "| type | hidden file sha256 | original bytes | team bytes | round trip | best standard bytes | zero bytes | SOTA bytes |",
              "|---|---|---|---|---|---|---|---|"]
    for t in types:
        r = details["types"][t]
        lines.append(f"| {t} | {r.get('sha256', '-')} | {r['size']} | {r.get('compressed_bytes', '-')} | "
                     f"{'yes' if r.get('roundtrip_ok') else 'no'} | {r['baseline_bytes']} | {r['zero_bytes']} | {r['sota_bytes']} |")
    return "\n".join(lines) + "\n"


def results_line(details: dict, types: list) -> dict:
    """The per-file numbers needed to re-score a run, in one JSON object."""
    files = {}
    for t in types:
        r = details["types"][t]
        files[t] = {"kind": r["kind"], "sha256": r.get("sha256"), "size": r["size"], "compressed_bytes": r.get("compressed_bytes"),
                    "roundtrip_ok": bool(r.get("roundtrip_ok")),
                    "compress_cpu_s": (r.get("compress") or {}).get("cpu_s"),
                    "decompress_cpu_s": (r.get("decompress") or {}).get("cpu_s"),
                    "baseline_bytes": r["baseline_bytes"], "zero_bytes": r["zero_bytes"], "sota_bytes": r["sota_bytes"], "score": r["score"]}
    return {"reward": details["reward"], "vs_sota_overall": details.get("vs_sota_overall"),
            "program_sha256": details.get("program_sha256"), "files": files}


def zero_bytes(t: str) -> int:
    """The size a file scores 0 at: the smaller of lpaq1's and the best standard tool's."""
    lpaq = (TARGETS[t]["references"].get("lpaq1") or {}).get("bytes")
    return min(TARGETS[t]["baseline_bytes"], lpaq) if lpaq else TARGETS[t]["baseline_bytes"]


def file_score(base: int, sota: int, team: int) -> float:
    """A file's score on a percentage (log) scale: 0 at the zero point's size, 1.0 at the state of the art's,
    and 0.01 more for every 1% smaller than the state of the art."""
    team = max(team, 1)
    if team >= sota:
        return min(1.0, max(0.0, math.log(base / team) / math.log(base / sota)))
    return 1.0 + math.log(sota / team)


def kind_of(t: str) -> str:
    return TARGETS[t].get("kind", t)


def kind_mean(values: dict) -> float:
    """The mean over kinds of the mean over each kind's files."""
    groups = {}
    for t, v in values.items():
        groups.setdefault(kind_of(t), []).append(v)
    return sum(sum(g) / len(g) for g in groups.values()) / len(groups)


def overall_ratio(details: dict, types: list) -> float:
    """Geometric mean of the team's size divided by the state of the art's, within each kind and then over
    kinds. A file that failed counts at its original size. Below 1 means the team beats the state of the art
    overall."""
    logs = {}
    for t in types:
        r = details["types"].get(t, {})
        size = r["compressed_bytes"] if r.get("roundtrip_ok") else TARGETS[t]["size"]
        logs[t] = math.log(size / TARGETS[t]["sota_bytes"])
    return math.exp(kind_mean(logs))


def grade(details: dict, types: list) -> tuple[float, dict]:
    """Build the submission and run it on every hidden file, filling in details. Returns (reward, scores)."""
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    WORK.chmod(0o711)
    (WORK / "store").mkdir(mode=0o700)
    global RUN_POOL
    RUN_POOL = usable_run_users()
    details["run_isolation"] = ("a separate uid per graded file" if RUN_POOL != (USER,)
                                else "one shared uid: this host does not allow a uid per run")
    sweep_user_files()
    hidden_readable = lock_hidden()
    src = APP / "compressor"
    scores = {t: 0.0 for t in types}
    broken = []  # rules the submission breaks: it scores 0, but is still built and graded for the record
    if hidden_readable:
        details["build"] = b = {"ok": False, "detail": "grader error: hidden files readable by uid 65534"}
    elif not src.is_dir():
        details["build"] = b = {"ok": False, "detail": "missing compressor/ directory"}
    else:
        pb = program_bytes(src)
        details["program_bytes"], details["program_complete"], details["program_sha256"] = pb["bytes"], pb["complete"], pb["sha256"]
        if not pb["complete"]:
            details["program_stopped"] = pb["stopped"]
            broken.append(f"compressor/ is too big: measuring stopped {pb['stopped']} ({pb['bytes']} bytes after xz -9e "
                          f"so far, cap {SIZE_CAP})")
        elif pb["bytes"] > SIZE_CAP:
            broken.append(f"compressor/ is {pb['bytes']} bytes after xz -9e, over the {SIZE_CAP} cap")
        unlocked = lock_private()
        if unlocked:  # a grader problem, not the team's: recorded, not scored
            details["readable_during_grading"] = unlocked
        details["build"] = b = build(src, WORK / "sub")
    if broken:
        details["disqualified"] = "; ".join(broken)
    if b["ok"]:
        details["workers"] = workers()
        comp = phase(WORK / "sub", "compress", {t: HIDDEN / t for t in types})
        decomp = phase(WORK / "sub", "decompress", {t: Path(r["output"]) for t, r in comp.items() if "output" in r})
        for t in types:
            tg, c, d = TARGETS[t], comp[t], decomp.get(t, {"error": "not decompressed"})
            row = {"compress": c, "decompress": d}
            if "output" in c:
                row["compressed_bytes"] = Path(c["output"]).stat().st_size
            if "output" in c and "output" in d:
                original = (HIDDEN / t).read_bytes()
                restored = Path(d["output"]).read_bytes()
                row["roundtrip_ok"] = hashlib.sha256(restored).digest() == hashlib.sha256(original).digest()
                if row["roundtrip_ok"]:
                    scores[t] = round(file_score(zero_bytes(t), tg["sota_bytes"], row["compressed_bytes"]), 4)
            for r in (c, d):
                r.pop("output", None)
            details["types"][t] = row
    allowed = dict(scores)  # what each file would score if the submission broke no rule
    ratio = overall_ratio(details, types) if b["ok"] else None
    details["vs_sota_overall"] = round(ratio - 1, 4) if ratio is not None else None
    details["beats_sota_overall"] = ratio is not None and ratio < 1
    allowed_reward = min(1.0, round(kind_mean(allowed), 4))
    if not broken:
        return allowed_reward, scores
    details["score_if_allowed"] = allowed_reward if b["ok"] else None
    for t in types:
        details["types"].setdefault(t, {})["score_if_allowed"] = allowed[t]
    return 0.0, {t: 0.0 for t in types}


RESULT_FILES = ("reward.json", "reward.txt", "details.json", "report.md")


def lock_out() -> list:
    """While submission code runs, only root may write the output folder or its parent: Harbor makes
    /logs/verifier 0777, and a reward.json there takes precedence over reward.txt. Returns what to restore."""
    saved = []
    for d in (OUT, OUT.parent):
        try:
            st = d.lstat()
            saved.append((d, st.st_uid, st.st_gid, st.st_mode & 0o7777))
            if st.st_uid == USER:
                os.chown(d, 0, 0)
            if st.st_mode & 0o022:
                d.chmod(st.st_mode & 0o7755)
        except OSError:
            pass
    return saved


def unlock_out(saved: list):
    """Back to Harbor's own modes once no submission process is left, so it can clean the folder up."""
    for d, uid, gid, mode in saved:
        try:
            os.chown(d, uid, gid)
            d.chmod(mode)
        except OSError:
            pass


def write_result(name: str, text: str):
    """Replace OUT/name with a fresh root-written file: never write through a link someone left there."""
    path = OUT / name
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    path.write_text(text)


def unseal():
    dst, want = HIDDEN / SEALED_TYPE, TARGETS[SEALED_TYPE]["sha256"]
    if dst.exists():
        return
    try:
        sealed = SEALED.read_bytes()
    except OSError as e:
        raise RuntimeError(f"hidden file {SEALED_TYPE} is not in tests/hidden and {SEALED} cannot be read ({e}): "
                           f"rebuild the image from environment/Dockerfile") from e
    n = len(sealed)
    pad = b"".join(hashlib.sha256(bytes.fromhex(want) + i.to_bytes(8, "big")).digest() for i in range((n + 31) // 32))[:n]
    plain = (int.from_bytes(sealed, "big") ^ int.from_bytes(pad, "big")).to_bytes(n, "big")
    data = plain[8:8 + int.from_bytes(plain[:8], "big")]
    if hashlib.sha256(data).hexdigest() != want:
        raise RuntimeError(f"{SEALED} does not open to hidden file {SEALED_TYPE}: rebuild the image from environment/Dockerfile")
    dst.write_bytes(data)


def main():
    """Grade, and whatever happens (a crash included) write details.json, reward.json, reward.txt and report.md."""
    OUT.mkdir(parents=True, exist_ok=True)
    saved = lock_out()
    for name in RESULT_FILES:  # nothing from before this grading may stand in for its result
        try:
            (OUT / name).unlink()
        except FileNotFoundError:
            pass
    types = sorted(TARGETS)
    details = {"types": {}, "limits": {"cpu_s": CPU_LIMIT_S, "wall_s": WALL_LIMIT_S, "mem_bytes": MEM_LIMIT,
                                       "size_cap_xz_bytes": SIZE_CAP}}
    try:
        unseal()
        reward, scores = grade(details, types)
    except Escaped as e:
        details["disqualified"] = f"{e}; nothing it produced can be trusted, so the run scores 0"
        details.pop("score_if_allowed", None)
        for row in details["types"].values():
            row.pop("score_if_allowed", None)
        reward, scores = 0.0, {t: 0.0 for t in types}
    except Exception:  # noqa: BLE001 - a crash still leaves a record
        details["grader_error"] = traceback.format_exc()[-4000:]
        reward, scores = 0.0, {t: 0.0 for t in types}
    finally:
        try:
            sweep_user_files()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(WORK, ignore_errors=True)
    details["reward"] = reward
    details["kinds"] = {}
    for t in types:
        details["kinds"].setdefault(kind_of(t), []).append(t)
    for t in types:
        row = details["types"].setdefault(t, {})
        row["score"] = scores[t]
        row["kind"] = kind_of(t)
        try:
            row["sha256"] = hashlib.sha256((HIDDEN / t).read_bytes()).hexdigest()
        except OSError:
            row["sha256"] = None
        if row["sha256"] != TARGETS[t].get("sha256"):
            details.setdefault("grader_warnings", []).append(f"hidden file {t} does not match targets.json")
        for k in ("size", "baseline_bytes", "baseline_tool", "sota_bytes", "sota_tool", "tools", "references"):
            row[k] = TARGETS[t][k]
        row["zero_bytes"] = zero_bytes(t)
    write_result("details.json", json.dumps(details, indent=2) + "\n")
    write_result("reward.txt", f"{reward}\n")
    write_result("reward.json", json.dumps({"reward": reward}) + "\n")  # Harbor reads this one first
    try:
        report = render_report(details, types)
    except Exception:  # noqa: BLE001
        report = f"# Compressor grade: {reward:.4f}\n\nThe report failed; details.json has the numbers.\n\n{traceback.format_exc()}"
    write_result("report.md", report)
    unlock_out(saved)
    print(report)
    print("RESULTS " + json.dumps(results_line(details, types), separators=(",", ":")))
    print(json.dumps(scores), "reward", reward)


if __name__ == "__main__":
    main()
