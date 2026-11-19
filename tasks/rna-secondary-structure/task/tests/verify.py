"""Run the team's `predict` on the held-out RNA sequences, in batches holding at most one sequence per
family, and score the predicted secondary structures (rna_score.py; its docstring gives the conventions).

Flow (function names are kept stable because external grading tools hook into them):
  1. Keep a root-only copy of the team's tree (from --app). Any data/ the team committed is dropped
     from it (info flag team_data_dir_replaced).
  2. Split the held-out sequences into batches with AT MOST ONE SEQUENCE PER FAMILY (family from the
     labels; fixed seed): n_batches = the size of the largest held-out family, each family's members
     go to distinct batches, the least-filled batches first, so batch sizes differ by at most a few.
     For each batch: empty /app in place and restore it from the kept tree, put a fresh copy of the
     agent data at /app/data, hand all of /app to the unprivileged run user, clear the user's files
     everywhere else (/tmp, /var/tmp, /dev/shm, ...), its SysV IPC objects and HOME, and run
     `./predict <batch.jsonl> <out.jsonl>` from /app as uid 65534 with a clean environment, thread
     variables sized to the CPU budget (affinity capped by the cgroup quota), no network (the
     verifier sandbox has none), an OOM-kill preference and a generous safety cap per batch
     (RNA_PREDICT_TIMEOUT_SEC, default 3600 s; not promised to the team). Every process of the run
     user is killed after each batch, then the batch's input and output dirs are deleted, so no
     batch can see another batch's sequences. The output dir (/logs/verifier) is root-only (0700)
     while batches run, because it holds the earlier batches' program logs.
  3. Parse whatever valid output each batch wrote (a crash or timeout still counts what was written)
     and pool it. The structures in the labels are read only after the last batch.

Input line:  {"id": str, "sequence": str}.
Output line: {"id": str, "structure": str}. Lines that are not JSON objects with a string id are
skipped; so are ids not in the batch and repeats of an id (the first line counts). Only the
"structure" field is read (the instruction asks for dot-bracket); a structure that is not a string
is passed on as None, which the scorer counts as invalid (no pairs); strings longer than
MAX_STRUCT_LEN are dropped the same way. Everything else (length, characters, balance, sharp pairs)
is the scorer's business: an invalid structure scores as no pairs.

reward.txt = rna_score.score(...)["score"] (family-macro exact base-pair F1), clamped to [0, 1]
(NaN -> 0). details.json keeps the scorer's summary under "metrics" (family-macro, sequence-mean
and micro precision/recall/F1/exact and the one-nucleotide-slippage variants, counters, per-family
means) plus a bootstrap CI of the reward over families, the batching and per-batch run
diagnostics; per_sequence.jsonl has the scorer's per-sequence rows plus the batch each one ran in.
details.json "checks" (diagnostics only, never used in the reward): the build tools on the program's
PATH (gcc, g++, cc, c++, make, cmake, node; whether gcc/g++ compile), each batch's program-log tail,
and the batches whose log has lines that look like a failed build (a program that compiles itself on
first run and silently falls back when the compiler is missing scores lower without any error).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))  # python -I leaves the script dir off sys.path

import numpy as np  # noqa: E402

from rna_score import load_labels, score  # noqa: E402

DATA = TESTS / "hidden"
PRISTINE = Path("/opt/rna/data")
APP = Path("/app")
TEAM = Path("/srv/rna-team")  # root-only copy of the team's tree, restored before every batch
RUN = Path("/srv/rnarun")
HOME = RUN / "home"
RUN_UID = 65534
TIME_LIMIT = int(os.environ.get("RNA_PREDICT_TIMEOUT_SEC", "3600"))  # per batch; a safety cap only
MAX_BATCHES_SIZED = 8  # task.toml's [verifier] timeout_sec assumes at most this many batches (the dataset needs 8)
BATCH_SEED = 20261105
MAX_OUT_BYTES = 200 * 1024 * 1024
MAX_STRUCT_LEN = 100_000
LOG_TAIL = 200_000
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
# Places the run user could leave files in besides /app and its HOME (all scrubbed between batches).
SCRUB_ROOTS = ("/", "/tmp", "/var/tmp", "/dev/shm", "/run", "/logs")
SCRUB_PRUNE = ("/proc", "/sys", "/dev", "/app", str(RUN), str(TEAM))


TOOLS = ("gcc", "g++", "cc", "c++", "make", "cmake", "node")  # recorded in details.json "checks"
LOG_TAIL_CHECK = 2000  # chars of each batch's program log kept in details.json
# Program-log lines that suggest a build step failed (a missing compiler, a failed compile or link).
# Informational only: the reward never depends on them.
BUILD_FAILURE = re.compile(
    r"No such file or directory: '[^']*\b(?:g\+\+|gcc|cc|c\+\+|clang\+*|make|cmake|nvcc|gfortran)'"
    r"|\b(?:g\+\+|gcc|cc|c\+\+|clang|make|cmake|nvcc|gfortran)(?![\w+])[^\n]{0,20}: (?:command )?not found"
    r"|compilation terminated|fatal error: [^\n]{0,120}No such file|make(?:\[\d+\])?: \*\*\*"
    r"|undefined reference to|ld returned \d+ exit status|CompileError|DistutilsExecError"
    r"|(?:build|compil)\w* failed", re.IGNORECASE)


def sh(*cmd: str) -> None:
    subprocess.run(list(cmd), check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def toolchain() -> dict:
    """Which build tools this machine has on the program's PATH (and whether gcc/g++ really
    compile), so a program that builds itself at first run and silently falls back is visible."""
    info: dict = {}
    for t in TOOLS:
        p = shutil.which(t, path=SAFE_PATH)
        if not p:
            info[t] = None
            continue
        try:
            r = subprocess.run([p, "--version"], capture_output=True, text=True, timeout=20)
            info[t] = {"path": p, "version": ((r.stdout or r.stderr).strip().splitlines() or [""])[0]}
        except (OSError, subprocess.SubprocessError) as e:
            info[t] = {"path": p, "version": None, "error": type(e).__name__}
    for t, src in (("gcc", "int main(void){return 0;}\n"), ("g++", "#include <vector>\nint main(){std::vector<int> v;return 0;}\n")):
        if not info.get(t):
            continue
        d = Path(tempfile.mkdtemp(prefix="tc-", dir="/srv"))
        try:
            (d / "t.c").write_text(src)
            r = subprocess.run([info[t]["path"], "-x", "c" if t == "gcc" else "c++", str(d / "t.c"), "-o", str(d / "t")],
                               capture_output=True, text=True, timeout=60)
            info[t]["compiles"] = r.returncode == 0 and subprocess.run([str(d / "t")]).returncode == 0
        except (OSError, subprocess.SubprocessError):
            info[t]["compiles"] = False
        finally:
            shutil.rmtree(d, ignore_errors=True)
    return info


def mkdir_owned(d: Path, owner: int, mode: int) -> None:
    d.mkdir(parents=True, exist_ok=True)
    os.chown(d, owner, owner)
    os.chmod(d, mode)


# ------------------------------------------------------------------------------- batching

def family_batches(ids: list[str], fam_of: dict[str, str], seed: int = BATCH_SEED) -> list[list[str]]:
    """Batches with at most one id per family. n_batches = largest family; each family's members
    (shuffled) go to the currently least-filled distinct batches; each batch is then shuffled."""
    rng = np.random.default_rng(seed)
    fams: dict[str, list[str]] = {}
    for i in ids:
        fams.setdefault(fam_of.get(i, f"__nofamily__{i}"), []).append(i)
    if not fams:
        return []
    n_batches = max(len(v) for v in fams.values())
    batches: list[list[str]] = [[] for _ in range(n_batches)]
    for f in sorted(fams, key=lambda f: (-len(fams[f]), f)):
        members = [fams[f][j] for j in rng.permutation(len(fams[f]))]
        tie = rng.permutation(n_batches)
        order = sorted(range(n_batches), key=lambda b: (len(batches[b]), tie[b]))
        for b, m in zip(order, members):
            batches[b].append(m)
    return [[b[j] for j in rng.permutation(len(b))] for b in batches]


# ------------------------------------------------------------------------------- staging

def keep_team_tree(app_src: Path) -> dict:
    info: dict = {}
    shutil.rmtree(TEAM, ignore_errors=True)
    shutil.copytree(app_src, TEAM, symlinks=True)
    os.chmod(TEAM, 0o700)
    data = TEAM / "data"
    if data.exists() or data.is_symlink():
        info["team_data_dir_replaced"] = True
        if data.is_dir() and not data.is_symlink():
            shutil.rmtree(data)
        else:
            data.unlink()
    shutil.rmtree(RUN, ignore_errors=True)
    mkdir_owned(RUN, 0, 0o755)
    return info


def stage_batch(i: int, lines: list[str]) -> Path:
    """Fresh /app (team tree + agent data, all owned by the run user), fresh HOME and /tmp."""
    # Empty /app in place rather than deleting it: on a remote sandbox the harness's later shell calls
    # run with /app as their working directory, and a deleted cwd breaks them.
    APP.mkdir(exist_ok=True)
    for child in APP.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
    shutil.copytree(TEAM, APP, symlinks=True, dirs_exist_ok=True)
    shutil.copytree(PRISTINE, APP / "data")
    # -h with -R: symlinks themselves change owner, their targets never do.
    sh("chown", "-hR", f"{RUN_UID}:{RUN_UID}", str(APP))
    sh("chmod", "-R", "u+rwX", str(APP))
    os.chmod(APP, 0o755)
    os.chmod("/tmp", 0o1777)
    scrub_run_user_files()
    shutil.rmtree(HOME, ignore_errors=True)
    mkdir_owned(HOME, RUN_UID, 0o700)
    bdir = RUN / f"b{i}"
    mkdir_owned(bdir, 0, 0o755)
    mkdir_owned(bdir / "out", RUN_UID, 0o700)
    (bdir / "sequences.jsonl").write_text("".join(lines))
    os.chmod(bdir / "sequences.jsonl", 0o644)
    return bdir


def scrub_run_user_files() -> int:
    """Delete every file the run user owns outside /app and the grader's own dirs, and its SysV IPC
    objects, so nothing carries over from one batch to the next. Returns the number of paths."""
    found: set[str] = set()
    for root in SCRUB_ROOTS:
        if not os.path.isdir(root):
            continue
        prune: list[str] = []
        for p in SCRUB_PRUNE:
            if p != root:
                prune += ["-path", p, "-o"]
        cmd = ["find", root, "-xdev", "(", *prune[:-1], ")", "-prune", "-o",
               "-user", str(RUN_UID), "-print0"]
        r = subprocess.run(cmd, capture_output=True, check=False)
        found.update(x.decode("utf-8", "surrogateescape") for x in r.stdout.split(b"\0") if x)
    for p in sorted(found, key=len, reverse=True):  # children before parents
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                os.unlink(p)
        except OSError:
            pass
    return len(found) + remove_run_user_ipc()


def remove_run_user_ipc() -> int:
    n = 0
    for kind, flag in (("shm", "-m"), ("sem", "-s"), ("msg", "-q")):
        try:
            rows = Path(f"/proc/sysvipc/{kind}").read_text().splitlines()
        except OSError:
            continue
        if not rows:
            continue
        head = rows[0].split()
        try:
            ci, cu = 1, head.index("uid")
        except ValueError:
            continue
        for row in rows[1:]:
            f = row.split()
            if len(f) > cu and f[cu] == str(RUN_UID):
                # as the owner: root in a container usually lacks CAP_IPC_OWNER
                sh("setpriv", f"--reuid={RUN_UID}", f"--regid={RUN_UID}", "--clear-groups", "--",
                   "ipcrm", flag, f[ci])
                n += 1
    return n


def finish_batch(bdir: Path) -> int:
    """After the run user is gone and the output is read: delete the batch's dir and its files."""
    shutil.rmtree(bdir, ignore_errors=True)
    shutil.rmtree(HOME, ignore_errors=True)
    return scrub_run_user_files()


# ----------------------------------------------------------------------------------- run

def live_run_user_pids() -> list[str]:
    """The run user's processes, zombies excluded: an orphan that was killed stays a zombie when PID 1
    does not reap (a container whose PID 1 is `sleep infinity`); it holds no memory or files."""
    out = subprocess.run(["ps", "-e", "-o", "pid=,uid=,stat="], capture_output=True, text=True).stdout
    return [f[0] for f in (line.split() for line in out.splitlines())
            if len(f) >= 3 and f[1] == str(RUN_UID) and not f[2].startswith("Z")]


def kill_run_user() -> int:
    """SIGKILL every process of the run user, including any that escaped the process group."""
    for _ in range(20):
        left = live_run_user_pids()
        if not left:
            return 0
        sh("pkill", "-KILL", "-u", str(RUN_UID))
        time.sleep(0.5)
    return len(left)


def cgroup_value(name: str) -> str | None:
    try:
        return Path(f"/sys/fs/cgroup/{name}").read_text().strip()
    except OSError:
        return None


def cpu_budget() -> int:
    """CPUs the program may really use: the affinity mask, capped by the cgroup quota. On
    a remote sandbox the program may see every host core but get a quota of the configured size."""
    n = len(os.sched_getaffinity(0))
    quota, _, period = (cgroup_value("cpu.max") or "max").partition(" ")
    if quota != "max" and period:
        n = min(n, max(1, -(-int(quota) // int(period))))
    return n


def oom_kills() -> int:
    events = cgroup_value("memory.events") or ""
    for line in events.splitlines():
        k, _, v = line.partition(" ")
        if k == "oom_kill":
            return int(v)
    return 0


def run_predict(i: int, bdir: Path, out_dir: Path) -> dict:
    predict = APP / "predict"
    res: dict = {"batch": i}
    if not (predict.is_file() or predict.is_symlink()):
        res["status"] = "missing"
        return res
    if predict.is_file() and not predict.is_symlink():
        predict.chmod(predict.stat().st_mode | 0o111)
    n = str(cpu_budget())
    env = {"PATH": SAFE_PATH, "HOME": str(HOME), "LANG": "C.UTF-8",
           "PYTHON_CPU_COUNT": n, "OMP_NUM_THREADS": n, "OPENBLAS_NUM_THREADS": n,
           "MKL_NUM_THREADS": n, "NUMEXPR_NUM_THREADS": n, "NUMBA_NUM_THREADS": n}
    log_path = bdir / "predict.log"

    def prefer_oom_kill():
        Path("/proc/self/oom_score_adj").write_text("1000")

    cmd = ["timeout", "-k", "30", str(TIME_LIMIT),
           "setpriv", f"--reuid={RUN_UID}", f"--regid={RUN_UID}", "--clear-groups", "--",
           "./predict", str(bdir / "sequences.jsonl"), str(bdir / "out" / "predictions.jsonl")]
    ooms = oom_kills()
    t0 = time.monotonic()
    with open(log_path, "wb") as log:
        p = subprocess.Popen(cmd, cwd=APP, env=env, stdin=subprocess.DEVNULL, stdout=log,
                             stderr=subprocess.STDOUT, preexec_fn=prefer_oom_kill)
        try:
            rc = p.wait(timeout=TIME_LIMIT + 90)
        except subprocess.TimeoutExpired:
            p.kill()
            rc = p.wait()
    res["wall_sec"] = round(time.monotonic() - t0, 1)
    res["exit_code"] = rc
    res["stragglers_left"] = kill_run_user()
    res["oom_kills"] = oom_kills() - ooms
    if rc == 0:
        res["status"] = "ok"
    elif rc in (124, 137) and res["wall_sec"] >= TIME_LIMIT - 1:
        res["status"] = "timeout"
    elif rc == 137 or rc < 0:
        res["status"] = "killed"  # SIGKILL before the limit: usually out of memory
    else:
        res["status"] = "crashed"
    with open(log_path, "rb") as f:
        f.seek(max(0, log_path.stat().st_size - LOG_TAIL))
        tail = f.read()
    (out_dir / f"predict-log-b{i}.txt").write_bytes(tail)
    text = tail.decode("utf-8", "replace")
    res["log_bytes"] = log_path.stat().st_size
    res["log_tail"] = text[-LOG_TAIL_CHECK:]
    hints = list(dict.fromkeys(m.group(0)[:160] for m in BUILD_FAILURE.finditer(text)))
    if hints:
        res["build_failure_hints"] = hints[:5]
    return res


# ------------------------------------------------------------------------------- parsing

def read_output(path: Path) -> tuple[list[str], dict]:
    """The run user's output file, opened without following links; regular files only."""
    info = {"present": False}
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        info["error"] = type(e).__name__
        return [], info
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            info["error"] = "not a regular file"
            return [], info
        raw = f.read(MAX_OUT_BYTES + 1)
    info.update(present=True, bytes=len(raw), truncated=len(raw) > MAX_OUT_BYTES)
    return raw[:MAX_OUT_BYTES].decode("utf-8", "replace").splitlines(), info


def parse(lines: list[str], ids: set[str]) -> tuple[dict[str, str | None], dict]:
    got: dict[str, str | None] = {}
    st = {"lines": 0, "bad_lines": 0, "unknown_ids": 0, "duplicate_ids": 0,
          "structure_not_string": 0, "structure_too_long": 0}
    for line in lines:
        if not line.strip():
            continue
        st["lines"] += 1
        try:
            d = json.loads(line)
            sid = d["id"]
            if not isinstance(sid, str):
                raise TypeError
        except Exception:  # noqa: BLE001 - any malformed line is skipped
            st["bad_lines"] += 1
            continue
        if sid not in ids:
            st["unknown_ids"] += 1
            continue
        if sid in got:
            st["duplicate_ids"] += 1
            continue
        s = d.get("structure")
        if not isinstance(s, str):
            st["structure_not_string"] += 1
            s = None
        elif len(s) > MAX_STRUCT_LEN:
            st["structure_too_long"] += 1
            s = None
        got[sid] = s
    st["sequences_returned"] = sum(v is not None for v in got.values())
    return got, st


# ---------------------------------------------------------------------------------- main

def clamp01(x) -> float:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if not math.isfinite(x) else min(1.0, max(0.0, x))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default="/app")
    ap.add_argument("--out", default="/logs/verifier")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)  # earlier batches' logs must not be readable by later batches
    try:
        grade(Path(a.app), out)
    finally:
        os.chmod(out, 0o755)


def grade(app_src: Path, out: Path) -> None:
    (out / "reward.txt").write_text("0\n")
    details: dict = {"reward": 0.0, "mark": os.environ.get("MULTI_AGENT_HARNESS_GRADE_MARK"),
                     "commit": os.environ.get("MULTI_AGENT_HARNESS_GRADE_COMMIT")}
    try:
        details["data"] = (DATA / "SOURCE").read_text().splitlines()[0]
    except OSError:
        pass
    t0 = time.monotonic()
    details["staging"] = keep_team_tree(app_src)

    seqs: dict[str, str] = {}
    for line in (DATA / "hidden_sequences.jsonl").open():
        if line.strip():
            d = json.loads(line)
            seqs[d["id"]] = d["sequence"]
    # Only the family of each label now; the structures are read after the last batch.
    fam_of = {}
    for line in (DATA / "hidden_labels.jsonl").open():
        if line.strip():
            d = json.loads(line)
            fam_of[d["id"]] = d["family"]
    plan = family_batches(list(seqs), fam_of)
    batch_of = {sid: i for i, b in enumerate(plan) for sid in b}

    batches, preds = [], {}
    totals: dict[str, int] = {}
    for i, ids in enumerate(plan):
        bdir = stage_batch(i, [json.dumps({"id": s, "sequence": seqs[s]}) + "\n" for s in ids])
        res = run_predict(i, bdir, out)
        res["n_sequences"] = len(ids)
        lines, res["output_file"] = read_output(bdir / "out" / "predictions.jsonl")
        got, st = parse(lines, set(ids))
        preds.update(got)  # batches are disjoint
        res["returned"] = st["sequences_returned"]
        for k, v in st.items():
            totals[k] = totals.get(k, 0) + v
        res["scrubbed_paths"] = finish_batch(bdir)
        batches.append(res)
        print(f"batch {i}: {res['status']} {res.get('wall_sec')}s returned {res['returned']}/{len(ids)}"
              f" scrubbed {res['scrubbed_paths']}", flush=True)
    kill_run_user()
    walls = [b.get("wall_sec", 0.0) for b in batches]
    statuses: dict[str, int] = {}
    for b_ in batches:
        statuses[b_["status"]] = statuses.get(b_["status"], 0) + 1
    fam_sizes: dict[str, int] = {}
    for s in seqs:
        fam_sizes[fam_of.get(s, "?")] = fam_sizes.get(fam_of.get(s, "?"), 0) + 1
    details["batching"] = {"rule": "at most one sequence per family per batch", "seed": BATCH_SEED,
                           "n_batches": len(plan), "n_families": len(fam_sizes),
                           "largest_family": max(fam_sizes.values()) if fam_sizes else 0,
                           "batch_sizes": [len(b) for b in plan],
                           "over_sized_for_verifier_timeout": len(plan) > MAX_BATCHES_SIZED,
                           "max_per_family_in_any_batch": int(max(
                               (max(np.unique([fam_of.get(s, s) for s in b], return_counts=True)[1])
                                for b in plan if b), default=0))}
    details["run"] = {"n_batches": len(plan), "time_limit_sec_per_batch": TIME_LIMIT,
                      "cpus": len(os.sched_getaffinity(0)), "cpu_budget": cpu_budget(),
                      "cpu_max": cgroup_value("cpu.max"), "memory_max": cgroup_value("memory.max"),
                      "statuses": statuses, "total_wall_sec": round(sum(walls), 1),
                      "max_wall_sec": max(walls) if walls else 0.0, "batches": batches}
    details["output"] = totals
    # Diagnostics only (the reward never depends on them): the build tools this machine has, and each
    # batch's program-log tail (stdout and stderr, interleaved) with any lines that look like a failed build.
    tools = toolchain()
    details["checks"] = {
        "toolchain": tools,
        "missing_tools": [t for t, v in tools.items() if not v],
        "batches_with_build_failure_hints": [b_["batch"] for b_ in batches if b_.get("build_failure_hints")],
        "predict_log_tails": {str(b_["batch"]): b_.get("log_tail", "") for b_ in batches},
    }
    for b_ in batches:
        b_.pop("log_tail", None)  # kept once, under checks

    # Labels (structures) are read only now, after every process of the run user has been killed.
    labels = load_labels(str(DATA / "hidden_labels.jsonl"), str(DATA / "hidden_sequences.jsonl"))
    metrics = score(labels, preds)
    per_seq = metrics.pop("per_sequence", {}) or {}
    rows = []
    for rid, m in per_seq.items():
        r = {"id": rid, **m}
        if rid in batch_of:
            r["batch"] = batch_of[rid]
            r["batch_status"] = batches[batch_of[rid]]["status"]
        rows.append(r)
    reward = clamp01(metrics.get("score"))
    fam_f1 = np.array([fm["f1"] for fm in metrics.get("per_family", {}).values()])
    if len(fam_f1):
        boots = fam_f1[np.random.default_rng(0).integers(0, len(fam_f1), (2000, len(fam_f1)))].mean(axis=1)
        metrics["reward_ci95_over_families"] = [round(float(np.quantile(boots, q)), 6) for q in (0.025, 0.975)]
    details["metrics"] = metrics
    details["reward"] = reward
    details["grade_sec"] = round(time.monotonic() - t0, 1)

    (out / "reward.txt").write_text(f"{reward:.6f}\n")
    (out / "details.json").write_text(json.dumps(details, indent=2, default=str))
    with open(out / "per_sequence.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + "\n")
    print(f"batches={statuses} n_batches={len(plan)} max_wall={details['run']['max_wall_sec']}s "
          f"returned={totals.get('sequences_returned', 0)}/{len(seqs)} reward={reward:.6f} "
          f"cpu_budget={details['run']['cpu_budget']}")
    ck = details["checks"]
    print(f"checks: missing_tools={ck['missing_tools']} "
          f"batches_with_build_failure_hints={ck['batches_with_build_failure_hints']}")


if __name__ == "__main__":
    main()
