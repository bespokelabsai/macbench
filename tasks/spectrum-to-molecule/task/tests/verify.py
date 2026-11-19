"""Run the team's `predict` on the held-out spectra, in batches, and score its rankings.

Flow:
  1. Keep a root-only copy of the team's tree (from --app). Any data/ the team committed is
     dropped from it.
  2. Split the held-out spectra into random batches of about 1,300 (fixed seed; 6,299 spectra
     -> 5 batches). For each batch: restore /app from the kept tree, put a fresh copy of the
     agent data at /app/data (the shipped files only, never the labels), hand all of /app to
     the unprivileged run user (so the program may write next to its code and data, as it
     could while the team worked), clear the user's /tmp files and HOME, and run
     `./predict <batch.jsonl> <out.jsonl>` from /app as uid 65534 with a clean environment,
     no network (the verifier sandbox has none), a wall-clock limit per batch (default 3600 s)
     and an OOM-kill preference. Every process of the run user is killed after each batch.
  3. Parse whatever valid output each batch wrote (a crash or timeout still counts what was
     written) and pool it. Labels stay in /tests (mode 700) and are read only after the last
     batch. Each SMILES is standardised with the dataset's own code and matched on
     InChIKey14. Invalid SMILES and repeats of an earlier InChIKey14 take no rank; only the
     first 20 entries of a list count.
  4. Per spectrum: hit@1/5/20 and reciprocal rank. Mean per molecule, then over molecules.
     reward = hit@1. details.json adds hit@5/20, MRR, the split by Tanimoto to training
     (>= 0.7 vs < 0.7), bootstrap CIs over molecules, per-batch runtimes and diagnostics.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))  # python -I leaves the script dir off sys.path

import numpy as np  # noqa: E402

from standardize import ik14  # noqa: E402

DATA = TESTS / "data"
PRISTINE = Path("/opt/task/data")
APP = Path("/app")
TEAM = Path("/srv/team-tree")  # root-only copy of the team's tree, restored before every batch
RUN = Path("/srv/predict-run")
HOME = RUN / "home"
RUN_UID = 65534
TIME_LIMIT = int(os.environ.get("MS2_PREDICT_TIMEOUT_SEC", "3600"))  # per batch
BATCH_TARGET = int(os.environ.get("MS2_BATCH_TARGET", "1300"))
BATCH_SEED = 20261104
MAX_K = 20
MAX_OUT_BYTES = 200 * 1024 * 1024
MAX_SMILES_LEN = 2000
LOG_TAIL = 200_000
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def sh(*cmd: str) -> None:
    subprocess.run(list(cmd), check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def mkdir_owned(d: Path, owner: int, mode: int) -> None:
    d.mkdir(parents=True, exist_ok=True)
    os.chown(d, owner, owner)
    os.chmod(d, mode)


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
    # Empty /app in place rather than deleting it: on a remote sandbox backend the harness's later
    # shell calls run with /app as their working directory, and a deleted cwd breaks them.
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
    sh("find", "/tmp", "-mindepth", "1", "-user", str(RUN_UID), "-delete")
    shutil.rmtree(HOME, ignore_errors=True)
    mkdir_owned(HOME, RUN_UID, 0o700)
    bdir = RUN / f"b{i}"
    mkdir_owned(bdir, 0, 0o755)
    mkdir_owned(bdir / "out", RUN_UID, 0o700)
    (bdir / "spectra.jsonl").write_text("".join(lines))
    os.chmod(bdir / "spectra.jsonl", 0o644)
    return bdir


# ----------------------------------------------------------------------------------- run

def kill_run_user() -> int:
    """SIGKILL every process of the run user, including any that escaped the process group."""
    for _ in range(20):
        left = subprocess.run(["pgrep", "-u", str(RUN_UID)], capture_output=True, text=True).stdout.split()
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
    some sandbox backends the verifier sees every host core but gets an 8-CPU quota."""
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
           "MKL_NUM_THREADS": n, "NUMEXPR_NUM_THREADS": n}
    log_path = bdir / "predict.log"

    def prefer_oom_kill():
        Path("/proc/self/oom_score_adj").write_text("1000")

    cmd = ["timeout", "-k", "30", str(TIME_LIMIT),
           "setpriv", f"--reuid={RUN_UID}", f"--regid={RUN_UID}", "--clear-groups", "--",
           "./predict", str(bdir / "spectra.jsonl"), str(bdir / "out" / "predictions.jsonl")]
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
        (out_dir / f"predict-log-b{i}.txt").write_bytes(f.read())
    return res


# ------------------------------------------------------------------------------- scoring

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


def parse(lines: list[str], ids: set[str]) -> tuple[dict[str, list[str]], dict]:
    ranked: dict[str, list[str]] = {}
    st = {"lines": 0, "bad_lines": 0, "unknown_ids": 0, "duplicate_ids": 0, "lists_over_20": 0}
    for line in lines:
        if not line.strip():
            continue
        st["lines"] += 1
        try:
            d = json.loads(line)
            sid, smiles = d["spectrum_id"], d["smiles"]
            if not isinstance(sid, str) or not isinstance(smiles, list):
                raise TypeError
        except Exception:  # noqa: BLE001 - any malformed line is skipped
            st["bad_lines"] += 1
            continue
        if sid not in ids:
            st["unknown_ids"] += 1
            continue
        if sid in ranked:
            st["duplicate_ids"] += 1
            continue
        if len(smiles) > MAX_K:
            st["lists_over_20"] += 1
        ranked[sid] = smiles[:MAX_K]
    st["spectra_answered"] = len(ranked)
    return ranked, st


def rank_of_truth(smiles: list, truth: str, cache: dict, st: dict) -> int | None:
    seen: list[str] = []
    for s in smiles:
        st["smiles_total"] += 1
        if not isinstance(s, str) or not s or len(s) > MAX_SMILES_LEN:
            st["smiles_invalid"] += 1
            continue
        if s not in cache:
            cache[s] = ik14(s)
        k = cache[s]
        if k is None:
            st["smiles_invalid"] += 1
            continue
        if k in seen:
            st["smiles_duplicate"] += 1
            continue
        seen.append(k)
        if k == truth:
            return len(seen)
    return None


KEYS = ("hit@1", "hit@5", "hit@20", "mrr")


def aggregate(per: list[dict], mols: list[str], seed: int = 0, n_boot: int = 2000) -> dict:
    if not per:
        return {"n_molecules": 0, "n_spectra": 0}
    by: dict[str, list[dict]] = {}
    for p, m in zip(per, mols):
        by.setdefault(m, []).append(p)
    X = np.array([[np.mean([p[k] for p in ps]) for k in KEYS] for ps in by.values()])
    rng = np.random.default_rng(seed)
    boots = X[rng.integers(0, len(X), size=(n_boot, len(X)))].mean(axis=1)
    out = {k: round(float(X[:, j].mean()), 6) for j, k in enumerate(KEYS)}
    out["ci95"] = {k: [round(float(np.quantile(boots[:, j], q)), 6) for q in (0.025, 0.975)]
                   for j, k in enumerate(KEYS)}
    out.update(n_molecules=len(X), n_spectra=len(per))
    return out


def score(ranked: dict[str, list[str]], labels: list[dict]) -> tuple[dict, dict, list[dict]]:
    cache: dict[str, str | None] = {}
    st = {"smiles_total": 0, "smiles_invalid": 0, "smiles_duplicate": 0}
    per, rows = [], []
    for lab in labels:
        r = rank_of_truth(ranked.get(lab["spectrum_id"], []), lab["inchikey14"], cache, st)
        p = {"hit@1": float(r == 1), "hit@5": float(r is not None and r <= 5),
             "hit@20": float(r is not None and r <= 20), "mrr": 1.0 / r if r else 0.0}
        per.append(p)
        rows.append({"spectrum_id": lab["spectrum_id"], "molecule_id": lab["molecule_id"], "rank": r})
    mols = [lab["molecule_id"] for lab in labels]
    hi = [bool(lab["tanimoto_ge_0.7"]) for lab in labels]
    metrics = {
        "all": aggregate(per, mols),
        "tanimoto_ge_0.7": aggregate([p for p, h in zip(per, hi) if h], [m for m, h in zip(mols, hi) if h]),
        "tanimoto_lt_0.7": aggregate([p for p, h in zip(per, hi) if not h], [m for m, h in zip(mols, hi) if not h]),
        "per_spectrum_micro": {k: round(float(np.mean([p[k] for p in per])), 6) for k in KEYS},
    }
    return metrics, st, rows


# ---------------------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default="/app")
    ap.add_argument("--out", default="/logs/verifier")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "reward.txt").write_text("0\n")
    details: dict = {"reward": 0.0, "mark": os.environ.get("MULTI_AGENT_HARNESS_GRADE_MARK"),
                     "commit": os.environ.get("MULTI_AGENT_HARNESS_GRADE_COMMIT")}
    try:
        details["data"] = (DATA / "SOURCE").read_text().splitlines()[0]
    except OSError:
        pass
    t0 = time.monotonic()
    details["staging"] = keep_team_tree(Path(a.app))
    n_files = sum(1 for p in TEAM.rglob("*") if p.is_file() and ".git" not in p.relative_to(TEAM).parts)
    has_predict = (TEAM / "predict").is_file() or (TEAM / "predict").is_symlink()
    details["staging"].update(team_files=n_files, predict=has_predict)
    print(f"team tree: {n_files} files outside .git and data/, predict {'present' if has_predict else 'missing'}",
          flush=True)

    spectra = [line if line.endswith("\n") else line + "\n"
               for line in (DATA / "hidden_spectra.jsonl").open() if line.strip()]
    spectra_ids = [json.loads(line)["spectrum_id"] for line in spectra]
    n_batches = max(1, round(len(spectra) / BATCH_TARGET))
    perm = np.random.default_rng(BATCH_SEED).permutation(len(spectra))
    batches, ranked = [], {}
    totals: dict[str, int] = {}
    for i, idx in enumerate(np.array_split(perm, n_batches)):
        idx = sorted(idx.tolist())
        bdir = stage_batch(i, [spectra[j] for j in idx])
        res = run_predict(i, bdir, out)
        res["n_spectra"] = len(idx)
        lines, res["output_file"] = read_output(bdir / "out" / "predictions.jsonl")
        got, st = parse(lines, {spectra_ids[j] for j in idx})
        ranked.update(got)  # batches are disjoint
        res["answered"] = st["spectra_answered"]
        for k, v in st.items():
            totals[k] = totals.get(k, 0) + v
        batches.append(res)
        print(f"batch {i}: {res['status']} {res.get('wall_sec')}s answered {res['answered']}/{len(idx)}", flush=True)
    kill_run_user()
    walls = [b.get("wall_sec", 0.0) for b in batches]
    statuses: dict[str, int] = {}
    for b_ in batches:
        statuses[b_["status"]] = statuses.get(b_["status"], 0) + 1
    details["run"] = {"n_batches": n_batches, "batch_seed": BATCH_SEED, "time_limit_sec_per_batch": TIME_LIMIT,
                      "cpus": len(os.sched_getaffinity(0)), "cpu_max": cgroup_value("cpu.max"),
                      "memory_max": cgroup_value("memory.max"), "statuses": statuses,
                      "total_wall_sec": round(sum(walls), 1), "max_wall_sec": max(walls) if walls else 0.0,
                      "batches": batches}
    details["output"] = totals

    # Labels are read only now, after every process of the run user has been killed.
    labels = [json.loads(line) for line in (DATA / "hidden_labels.jsonl").open()]
    metrics, smiles_stats, rows = score(ranked, labels)
    details["output"].update(smiles_stats)
    details["metrics"] = metrics
    details["reward"] = metrics["all"].get("hit@1", 0.0)
    details["grade_sec"] = round(time.monotonic() - t0, 1)

    (out / "reward.txt").write_text(f"{details['reward']:.4f}\n")
    (out / "details.json").write_text(json.dumps(details, indent=2))
    with open(out / "per_spectrum.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    m = metrics["all"]
    print(f"batches={details['run']['statuses']} max_wall={details['run']['max_wall_sec']}s "
          f"answered={details['output']['spectra_answered']}/{len(spectra_ids)} "
          f"hit@1={m.get('hit@1')} hit@5={m.get('hit@5')} hit@20={m.get('hit@20')} mrr={m.get('mrr')}")


if __name__ == "__main__":
    main()
