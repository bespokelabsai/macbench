#!/usr/bin/env python3
"""Intermediate scores of a finished team run: what was on `main` at given agent times, graded by the verifier.
Called by grade_checkpoints.sh (same arguments), which builds the verifier image first if it is missing.

    python3 tests/checkpoints/grade_checkpoints.py <job_or_trial_dir> [--marks 0.5h,1h,2h,3h,4h]
        [--image spectrum-verifier] [--out <dir>] [--cpus 8] [--memory 16g] [--dry-run]

Needs the run's checkpoints, copied as they were written by tests/checkpoints/archive_checkpoints.sh into
<job dir>.ckpt-archive/<trial>/ (the harness itself keeps only the last few in <job dir>.checkpoints/, which
are read too), the trial's final team.bundle, and keepalive.jsonl (its first line is the agents' start time,
the zero of every mark).

1. The team repository is rebuilt from the final team.bundle and every checkpoint's team.bundle.
2. main at each mark. Main is known exactly at every checkpoint (its bundle). Between two checkpoints a < b,
   main only fast-forwards through the commits on b's first-parent chain that descend from main at a. Each
   such commit c reached main no earlier than L(c) = max(its committer time, a) and no later than b. The
   estimate E(c) is the time of the first `git push` to main in the agents' Claude Code session logs (by c's
   author if possible, else by anyone) inside [L(c), b]; without one, E(c) = b. Main at a mark T is then the
   newest commit with E <= T (`est`, what is graded), between `lo` (main at the last checkpoint before T: a
   true state of main) and `hi` (the newest commit with L <= T). lo == hi means the mark's commit is certain.
3. Each mark's commit is checked out and graded with the verifier image, exactly as the run's final verifier
   (no network, 8 CPUs, 16 GB): <out>/<mark>/result/reward.txt, details.json, per_spectrum.jsonl.
A mark with no ./predict on main yet scores 0 (every batch `missing`); before the first checkpoint, main starts at
the repository's seed commit (the task's starting files). Prints one row per mark and writes
<out>/checkpoints.json. Standard library only; needs git and docker on the host."""
import argparse, glob, io, json, os, re, subprocess, sys, tarfile
from datetime import datetime
from pathlib import Path

PUSH = re.compile(r"git\b[^|;&]*\bpush\b")


def dur(s):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(h|m|s)?", s.strip())
    if not m:
        raise SystemExit(f"bad mark {s!r}: use 30m, 1h, 90s, ...")
    return float(m.group(1)) * {"h": 3600, "m": 60, "s": 1, None: 1}[m.group(2)]


def label(sec):
    if sec % 1800 == 0:
        return f"{sec / 3600:g}h"
    return f"{sec / 60:g}m" if sec % 60 == 0 else f"{sec:g}s"


def git(gd, *a, check=False):
    return subprocess.run(["git", "--git-dir", str(gd), *a], capture_output=True, text=True, check=check).stdout.strip()


def is_anc(gd, a, b):
    return subprocess.run(["git", "--git-dir", str(gd), "merge-base", "--is-ancestor", a, b]).returncode == 0


def bundle_main(bundle):
    heads = subprocess.run(["git", "bundle", "list-heads", str(bundle)], capture_output=True, text=True).stdout
    m = re.search(r"^([0-9a-f]{40}) refs/heads/main$", heads, re.M)
    return m.group(1) if m else None


def ts_of(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def targets_main(cmd):
    """A push to main: "origin main", "HEAD:main", "<x>:main", or a bare "git push" (agents work on main)."""
    for m in PUSH.finditer(cmd):
        seg = cmd[m.start():].split("&&")[0].split(";")[0].split("|")[0]
        if re.search(r"(\s|:)main\b", seg) or re.fullmatch(r"git\s+push(\s+-q)?(\s+origin)?\s*", seg.strip() + " "):
            return True
    return False


def checkpoints(job, trial):
    """Every checkpoint dir of the trial (archive first, then whatever the harness kept), by number."""
    seen = {}
    for root in (Path(f"{job}.ckpt-archive") / trial.name, Path(f"{job}.checkpoints") / trial.name):
        for m in glob.glob(f"{root}/*/manifest.json"):
            d = Path(m).parent
            if d.name.endswith(".tmp"):
                continue
            man = json.load(open(m))
            seen.setdefault(man["n"], {"n": man["n"], "elapsed": float(man["elapsed_agent_sec"]), "dir": d,
                                       "main": bundle_main(d / "team.bundle"), "names": man.get("agent_names") or {}})
    return [seen[n] for n in sorted(seen)]


def push_times(cks, t0):
    """Agent seconds of every `git push` to main in the Claude Code session logs, per agent slot."""
    found = {}
    for c in cks:
        for f in glob.glob(f"{c['dir']}/agents/*/session/**/*.jsonl", recursive=True):
            ag = f.split("/agents/")[1].split("/")[0]
            cmds = {}
            for line in open(f, errors="replace"):
                if "push" not in line and "tool_result" not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                msg = d.get("message") or {}
                cont = msg.get("content") if isinstance(msg, dict) else None
                if not isinstance(cont, list):
                    continue
                for it in cont:
                    if not isinstance(it, dict):
                        continue
                    if it.get("type") == "tool_use" and it.get("name") == "Bash":
                        if targets_main((it.get("input") or {}).get("command") or ""):
                            cmds[it.get("id")] = True
                    elif it.get("type") == "tool_result" and it.get("tool_use_id") in cmds and d.get("timestamp"):
                        found.setdefault((ag, it["tool_use_id"]), ts_of(d["timestamp"]) - t0)
    per = {}
    for (ag, _), t in found.items():
        per.setdefault(ag, []).append(t)
    return {k: sorted(v) for k, v in per.items()}, sorted(t for v in per.values() for t in v)


def main_at(gd, cks, final, t0, marks):
    names = cks[-1]["names"] if cks else {}
    slot_of = {v: k for k, v in names.items()} if isinstance(names, dict) else {}
    per, allpush = push_times(cks, t0)
    states = [(c["elapsed"], c["main"]) for c in cks if c["main"]]
    if states:   # before the first checkpoint, main starts at the repo's seed commit (the task's starting files)
        seed = git(gd, "rev-list", "--max-parents=0", "--first-parent", states[0][1]).splitlines()
        if seed and states[0][0] > 0:
            states.insert(0, (0.0, seed[-1]))
    if final and (not states or states[-1][1] != final):
        states.append((max([s[0] for s in states] + marks), final))

    def candidates(a, b, a_t, b_t):
        out = []
        for line in git(gd, "log", "--first-parent", "--format=%H %ct %an", f"{a}..{b}").splitlines():
            h, ct, an = line.split(" ", 2)
            if not is_anc(gd, a, h):
                continue
            L = max(int(ct) - t0, a_t)
            ag = slot_of.get(an)
            cand = [t for t in per.get(ag, []) if L - 5 <= t <= b_t + 5] if ag else []
            if not cand:
                cand = [t for t in allpush if L - 5 <= t <= b_t + 5]
            E = min(max(cand[0], L), b_t) if cand else b_t
            out.append({"sha": h, "L": L, "E": E})
        out.reverse()
        for k in ("L", "E"):
            for i in range(1, len(out)):
                out[i][k] = max(out[i][k], out[i - 1][k])
        return out

    ivs = [(a_t, a, b_t, candidates(a, b, a_t, b_t) if a != b else [])
           for (a_t, a), (b_t, b) in zip(states, states[1:])]
    points = []
    for T in marks:
        before = [s for s in states if s[0] <= T]
        if not before:
            points.append({"T": T, "lo": None, "est": None, "hi": None, "certain": False})
            continue
        a_t, a = before[-1]
        lo = est = hi = a
        for iv_a_t, iv_a, b_t, cands in ivs:
            if iv_a_t == a_t and iv_a == a and b_t > T:
                for c in cands:
                    if c["E"] <= T:
                        est = c["sha"]
                    if c["L"] <= T:
                        hi = c["sha"]
        points.append({"T": T, "lo": lo, "est": est, "hi": hi, "certain": lo == hi})
    return points


def grade(gd, sha, out, a):
    app = out / "app"
    if app.exists():
        subprocess.run(["rm", "-rf", str(app)], check=True)
    app.mkdir(parents=True)
    tar = subprocess.run(["git", "--git-dir", str(gd), "archive", sha], capture_output=True, check=True).stdout
    tarfile.open(fileobj=io.BytesIO(tar)).extractall(app, filter="tar")
    res = out / "result"
    res.mkdir(exist_ok=True)
    r = subprocess.run(["docker", "run", "--rm", "--network", "none", "--cpus", str(a.cpus), "--memory", a.memory,
                        "-v", f"{app}:/grade/app:ro", "-v", f"{res}:/grade/out",
                        "-e", "MULTI_AGENT_HARNESS_GRADE_APP=/grade/app", "-e", "MULTI_AGENT_HARNESS_GRADE_OUT=/grade/out",
                        # optional: a shorter limit per graded batch for a quick test (verify.py's default is 3600 s)
                        *(["-e", f"MS2_PREDICT_TIMEOUT_SEC={os.environ['MS2_PREDICT_TIMEOUT_SEC']}"]
                          if os.environ.get("MS2_PREDICT_TIMEOUT_SEC") else []),
                        a.image, "bash", "-c", f"bash /tests/test.sh; chown -R {os.getuid()}:{os.getgid()} /grade/out"],
                       capture_output=True, text=True)
    (out / "test-stdout.txt").write_text(r.stdout + r.stderr)
    try:
        return float((res / "reward.txt").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run", help="the Harbor job dir of the team run (holds one trial dir), or the trial dir")
    ap.add_argument("--marks", default="0.5h,1h,2h,3h,4h", help="agent times, comma-separated (default: %(default)s)")
    ap.add_argument("--image", default="spectrum-verifier", help="verifier image (docker build -t spectrum-verifier task/tests)")
    ap.add_argument("--out", help="output dir (default: <job dir>.graded-checkpoints)")
    ap.add_argument("--cpus", default="8"); ap.add_argument("--memory", default="16g")
    ap.add_argument("--dry-run", action="store_true", help="find the commits only, do not grade")
    a = ap.parse_args()
    run = Path(a.run).resolve()
    if (run / "keepalive.jsonl").is_file():          # a trial dir
        job, trial = run.parent, run
    else:
        trials = sorted(p for p in run.iterdir() if p.is_dir() and (p / "keepalive.jsonl").is_file())
        if len(trials) != 1:
            sys.exit(f"{run}: expected a trial dir or a job dir with one trial, found {len(trials)} trials")
        job, trial = run, trials[0]
    out = Path(a.out or f"{job}.graded-checkpoints").resolve()
    out.mkdir(parents=True, exist_ok=True)
    marks = [dur(x) for x in a.marks.split(",") if x.strip()]
    t0 = json.loads(open(trial / "keepalive.jsonl").readline())["ts"]
    final = bundle_main(trial / "team.bundle")
    cks = checkpoints(job, trial)
    gd = out / "repo.git"
    if not gd.exists():
        subprocess.run(["git", "init", "-q", "--bare", str(gd)], check=True)
    for i, b in enumerate([trial / "team.bundle"] + [c["dir"] / "team.bundle" for c in cks]):
        if b.is_file():
            subprocess.run(["git", "--git-dir", str(gd), "fetch", "-q", str(b), f"+refs/heads/*:refs/b{i}/*"], check=False)
    times = ", ".join("%.0fm" % (c["elapsed"] / 60) for c in cks)
    print(f"{trial.name}: {len(cks)} checkpoints ({times}), final main {final[:10] if final else None}", flush=True)
    points = main_at(gd, cks, final, t0, marks)
    for p in points:
        name = label(p["T"])
        if not p["est"]:
            p["reward"] = 0.0
            print(f"{name:>5}  -           no checkpoint at all: nothing to grade  reward 0.0", flush=True)
            continue
        tag = "certain" if p["certain"] else f"between {p['lo'][:10]} and {p['hi'][:10]}"
        if a.dry_run:
            print(f"{name:>5}  {p['est'][:10]} ({tag})", flush=True)
            continue
        p["reward"] = grade(gd, p["est"], out / name, a)
        print(f"{name:>5}  {p['est'][:10]} ({tag})  reward {p['reward']}", flush=True)
    off = trial / "verifier" / "reward.txt"
    (out / "checkpoints.json").write_text(json.dumps({"trial": trial.name, "t0": t0, "final_main": final,
                                                "official_reward": off.read_text().strip() if off.is_file() else None,
                                                "marks": points}, indent=1))


if __name__ == "__main__":
    main()
