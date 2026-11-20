#!/usr/bin/env python3
"""Coordination metrics for a multi-agent-harness trial, or for every trial in a Harbor job dir.

    python scripts/metrics.py jobs/<job>/<trial>     # one trial
    python scripts/metrics.py jobs/<job>             # every trial, plus reward and cost across them

Reads agent/board-events.jsonl, agent/proxy-usage.jsonl, verifier/details.json (and result.json
for the reward) and, if present, the team git bundle at artifacts/logs/artifacts/multi-agent-harness/team.bundle.
Writes metrics.json into the dir it was given, or to --out. Stdlib only, plus the git binary.
Old board logs work too: a `post` event counts as a send (channel posts as broadcasts) and a
`read` as a history call.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

BUNDLE = "artifacts/logs/artifacts/multi-agent-harness/team.bundle"
BOARD_LOGS = ("agent/board-events.jsonl", "artifacts/logs/artifacts/multi-agent-harness/board-events.jsonl")
NEAR_DUP = 0.8  # Jaccard over word 3-grams
MAX_NEAR_DUP_SENDS = 4000  # the pairwise check is quadratic; skip it above this
BRANCH_RE = re.compile(r"\bbranch(?:es)?\b[^`'\"\w]{0,4}[`'\"]?([A-Za-z0-9][\w./-]*[\w-])", re.I)


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def _json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _ts(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds") if ts is not None else None


def gini(values: list[float]) -> float | None:
    """0 when everyone does the same amount, (n-1)/n when one agent does it all."""
    xs = sorted(v for v in values if v is not None)
    n, total = len(xs), sum(xs)
    if n == 0 or total == 0:
        return None
    return round(sum((2 * i - n + 1) * x for i, x in enumerate(xs)) / (n * total), 4)


def normalize(text: str, names: list[str]) -> str:
    """Lowercase, agent names masked, punctuation dropped. Numbers stay: they are often the choice."""
    t = text.lower()
    for name in sorted(names, key=len, reverse=True):
        t = t.replace(name.lower(), " @agent ")
    t = re.sub(r"[^\w@\s]", " ", t)
    return " ".join(t.split())


def _shingles(text: str) -> set:
    words = text.split()
    return {tuple(words[i:i + 3]) for i in range(max(1, len(words) - 2))}


def duplication(sends: list[dict], names: list[str]) -> dict:
    """Identical or near-identical messages written by different agents."""
    norm = [(p["agent"], normalize(p.get("text") or "", names), p.get("text") or "") for p in sends]
    groups: dict[str, set] = defaultdict(set)
    for agent, n, _ in norm:
        if n:
            groups[n].add(agent)
    exact = {n: a for n, a in groups.items() if len(a) > 1}
    in_exact = sum(1 for _, n, _ in norm if n in exact)

    near_pairs, flagged, best = None, None, None
    if len(norm) <= MAX_NEAR_DUP_SENDS:
        sh = [_shingles(n) for _, n, _ in norm]
        words = [set(n.split()) for _, n, _ in norm]
        near_pairs, flagged, best = 0, set(), [0.0] * len(norm)
        for i in range(len(norm)):
            for j in range(i + 1, len(norm)):
                if norm[i][0] == norm[j][0] or not words[i] or not words[j]:
                    continue
                w = len(words[i] & words[j]) / len(words[i] | words[j])
                best[i], best[j] = max(best[i], w), max(best[j], w)
                inter = len(sh[i] & sh[j])
                if inter and inter / len(sh[i] | sh[j]) >= NEAR_DUP:
                    near_pairs += 1
                    flagged.update((i, j))
    examples = sorted(exact.items(), key=lambda kv: -len(kv[1]))[:3]
    return {
        "exact_groups": len(exact),
        "sends_in_exact_groups": in_exact,
        "near_duplicate_pairs": near_pairs,
        "share_sends_with_cross_agent_near_duplicate":
            round(len(flagged) / len(norm), 4) if flagged is not None and norm else None,
        # Each send's closest word-set Jaccard to another agent's send, averaged. A softer signal
        # than the near-duplicate count: it rises when agents paraphrase each other.
        "mean_best_cross_agent_similarity": round(statistics.fmean(best), 4) if best else None,
        "examples": [{"agents": len(a), "text": n[:160]} for n, a in examples],
    }


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def read_bundle(bundle: Path, names: list[str]) -> dict | None:
    """Commits per agent, files touched, and branches more than one agent committed to."""
    if not bundle.exists():
        return None
    by_email = {f"{n}@".lower(): n for n in names}

    def agent_of(name: str, email: str) -> str:
        if name in names:
            return name
        return by_email.get(email.lower().split("@")[0] + "@", name)

    with tempfile.TemporaryDirectory(prefix="multi-agent-harness-metrics-") as tmp:
        repo = Path(tmp) / "team.git"
        try:
            subprocess.run(["git", "clone", "-q", "--mirror", str(bundle), str(repo)],
                           capture_output=True, text=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            return {"error": f"could not read bundle: {getattr(e, 'stderr', '') or e}".strip()}
        log = _git(repo, "log", "--all", "--no-merges", "--format=%x1e%H%x1f%an%x1f%ae%x1f%at%x1f%s",
                   "--name-only")
        commits = []
        for block in log.split("\x1e")[1:]:
            head, _, files = block.partition("\n")
            sha, an, ae, at, subject = head.split("\x1f", 4)
            commits.append({"sha": sha, "agent": agent_of(an, ae), "ts": float(at), "subject": subject,
                            "files": [f for f in files.splitlines() if f.strip()]})
        branches = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()[:500]
        try:
            default = _git(repo, "symbolic-ref", "--short", "HEAD").strip()
        except subprocess.CalledProcessError:
            default = "main"
        shared_branches = {}
        for b in branches:
            if b in (default, "main", "master"):  # everyone shares the trunk; that is not a collision
                continue
            others = [f"^{o}" for o in branches if o != b]
            authors = {agent_of(*line.split("\x1f", 1)) for line in
                       _git(repo, "log", "--no-merges", "--format=%an\x1f%ae", b, *others).splitlines() if line}
            authors &= set(names)
            if len(authors) > 1:
                shared_branches[b] = sorted(authors)
    return {"commits": commits, "branches": branches, "branches_with_several_agents": shared_branches}


def _reward(trial: Path) -> float | None:
    result = _json(trial / "result.json") or {}
    rewards = (result.get("verifier_result") or {}).get("rewards") or {}
    if isinstance(rewards.get("reward"), (int, float)):
        return float(rewards["reward"])
    try:
        return float((trial / "verifier" / "reward.txt").read_text().strip())
    except (OSError, ValueError):
        details = _json(trial / "verifier" / "details.json") or {}
        r = details.get("reward")
        return float(r) if isinstance(r, (int, float)) else None


def trial_metrics(trial: Path) -> dict:
    board_log = next((trial / p for p in BOARD_LOGS if (trial / p).exists()), trial / BOARD_LOGS[0])
    events = _jsonl(board_log)
    usage = _jsonl(trial / "agent" / "proxy-usage.jsonl")
    details = _json(trial / "verifier" / "details.json") or {}

    names = {e["agent"] for e in events if e.get("event") == "register" and e.get("agent")}
    names |= {r["agent"] for r in usage if r.get("agent")}
    names |= {a for a in details.get("agents") or [] if isinstance(a, str)}
    names = sorted(names)

    per = {n: {"sends_broadcast": 0, "sends_direct": 0, "sends_multi": 0, "received_direct": 0, "inbox_calls": 0,
               "history_calls": 0, "rate_limited": 0, "refused": 0,
               "model_usd": 0.0, "model_requests": 0, "proxy_rate_limited": 0, "commits": 0, "files_touched": 0}
           for n in names}
    sends, times, send_times = [], [], []
    subagent_ops = Counter(e.get("op") for e in events if e.get("event") == "subagent")  # async orchestration
    for e in events:
        a, kind, ts = e.get("agent"), e.get("event"), _ts(e.get("ts"))
        if a not in per:
            continue
        if kind != "register" and ts is not None:
            times.append(ts)
        if kind in ("send", "post"):  # `post` is the old board's send
            to = e.get("to")
            if to:
                per[a]["sends_multi" if isinstance(to, list) else "sends_direct"] += 1
                for r in to if isinstance(to, list) else [to]:
                    if r in per:
                        per[r]["received_direct"] += 1
            else:
                per[a]["sends_broadcast"] += 1
            sends.append(e)
            if ts is not None:
                send_times.append(ts)
        elif kind == "inbox":
            per[a]["inbox_calls"] += 1
        elif kind in ("history", "read"):
            per[a]["history_calls"] += 1
        elif kind == "rate_limited":
            per[a]["rate_limited"] += int(e.get("count") or 1)
        elif kind == "refused":
            per[a]["refused"] += 1

    statuses = Counter()
    for r in usage:
        a = r.get("agent")
        statuses[r.get("reason") or str(r.get("status"))] += 1
        if a in per:
            per[a]["model_usd"] += float(r.get("usd") or 0)
            per[a]["model_requests"] += 1
            per[a]["proxy_rate_limited"] += r.get("reason") == "rate_limit"
        if (ts := _ts(r.get("ts"))) is not None:
            times.append(ts)

    git = read_bundle(trial / BUNDLE, names)
    commit_stats = None
    if git and "commits" in git:
        agent_commits = [c for c in git["commits"] if c["agent"] in per]
        files_by_agent: dict[str, set] = defaultdict(set)
        for c in agent_commits:
            per[c["agent"]]["commits"] += 1
            files_by_agent[c["agent"]].update(c["files"])
            times.append(c["ts"])
        for a, fs in files_by_agent.items():
            per[a]["files_touched"] = len(fs)
        owners: dict[str, set] = defaultdict(set)
        for a, fs in files_by_agent.items():
            for f in fs:
                owners[f].add(a)
        sharing = [c for c in agent_commits if any(len(owners[f] - {c["agent"]}) > 0 for f in c["files"])]
        subjects: dict[str, set] = defaultdict(set)
        for c in agent_commits:
            subjects[normalize(c["subject"], names)].add(c["agent"])
        commit_stats = {
            "commits": len(agent_commits),
            "other_commits": len(git["commits"]) - len(agent_commits),
            "files": len(owners),
            "share_commits_touching_shared_files": round(len(sharing) / len(agent_commits), 4) if agent_commits else None,
            "branches": len(git["branches"]),
            "branches_with_several_agents": git["branches_with_several_agents"],
            "commit_subjects_shared_across_agents": sum(1 for a in subjects.values() if len(a) > 1),
        }
    elif git:
        commit_stats = git

    for p in per.values():
        p["model_usd"] = round(p["model_usd"], 6)

    # Branch names proposed on the board by more than one agent.
    proposed: dict[str, set] = defaultdict(set)
    for p in sends:
        for b in BRANCH_RE.findall(p.get("text") or ""):
            if b.lower() not in ("name", "names", "for", "from", "to", "and", "is", "with", "main"):
                proposed[b].add(p["agent"])

    first, last = (min(times), max(times)) if times else (None, None)
    per_minute = []
    if send_times and first is not None:
        per_minute = [0] * (int((max(send_times) - first) // 60) + 1)
        for t in send_times:
            per_minute[int((t - first) // 60)] += 1

    team = {
        "agents": len(names),
        "sends": len(sends),
        "sends_broadcast": sum(p["sends_broadcast"] for p in per.values()),
        "sends_direct": sum(p["sends_direct"] for p in per.values()),
        "sends_multi": sum(p["sends_multi"] for p in per.values()),
        "inbox_calls": sum(p["inbox_calls"] for p in per.values()),
        "history_calls": sum(p["history_calls"] for p in per.values()),
        "rate_limited": sum(p["rate_limited"] for p in per.values()),
        "refused": sum(p["refused"] for p in per.values()),
        "model_usd": round(sum(float(r.get("usd") or 0) for r in usage), 6),
        "model_requests": len(usage),
        "proxy_outcomes": dict(statuses),
        "first_activity": _iso(first),
        "last_activity": _iso(last),
        "duration_s": round(last - first, 1) if times else None,
        "sends_per_minute": per_minute,
        "subagents": {"spawns": subagent_ops["spawn"], "wakes": subagent_ops["wake"],
                      "terminations": subagent_ops["terminate"], "final_responses": subagent_ops["finish"]}
        if subagent_ops else None,
    }
    return {
        "trial": trial.name,
        "reward": _reward(trial),
        "checks": details.get("checks"),
        "team": team,
        "git": commit_stats,
        "duplication": {
            **duplication(sends, names),
            "branch_names_proposed_by_several_agents": {b: sorted(a) for b, a in proposed.items() if len(a) > 1},
        },
        "gini": {"sends": gini([p["sends_broadcast"] + p["sends_direct"] + p["sends_multi"] for p in per.values()]),
                 "commits": gini([p["commits"] for p in per.values()]) if commit_stats and "commits" in commit_stats
                 else None},
        "agents": per,
    }


def _is_trial(path: Path) -> bool:
    return (path / "agent").is_dir() or (path / "verifier").is_dir()


def _stats(values: list) -> dict:
    xs = [v for v in values if isinstance(v, (int, float))]
    if not xs:
        return {"n": 0, "mean": None, "sd": None}
    return {"n": len(xs), "mean": round(statistics.fmean(xs), 4),
            "sd": round(statistics.stdev(xs), 4) if len(xs) > 1 else None}


def job_metrics(job: Path) -> dict:
    trials = [trial_metrics(t) for t in sorted(p for p in job.iterdir() if p.is_dir() and _is_trial(p))]

    def key(t: dict) -> dict:
        return {"trial": t["trial"], "reward": t["reward"], "agents": t["team"]["agents"],
                "model_usd": t["team"]["model_usd"], "sends_broadcast": t["team"]["sends_broadcast"],
                "sends_direct": t["team"]["sends_direct"], "inbox_calls": t["team"]["inbox_calls"],
                "rate_limited": t["team"]["rate_limited"], "refused": t["team"]["refused"], "duration_s": t["team"]["duration_s"],
                "commits": (t["git"] or {}).get("commits"), "gini_sends": t["gini"]["sends"],
                "gini_commits": t["gini"]["commits"],
                "share_commits_touching_shared_files": (t["git"] or {}).get("share_commits_touching_shared_files"),
                "share_sends_near_duplicate": t["duplication"]["share_sends_with_cross_agent_near_duplicate"]}

    rows = [key(t) for t in trials]
    agg = {k: _stats([r[k] for r in rows]) for k in
           ("reward", "model_usd", "sends_broadcast", "sends_direct", "inbox_calls", "duration_s", "commits",
            "gini_sends", "gini_commits", "share_commits_touching_shared_files", "share_sends_near_duplicate")}
    return {"job": job.name, "trials": len(trials), "unscored_trials": sum(r["reward"] is None for r in rows),
            "team_sizes": sorted({r["agents"] for r in rows}),
            "total_model_usd": round(sum(r["model_usd"] for r in rows), 6),
            "aggregate": agg, "per_trial": rows, "trial_metrics": trials}


def _print_trial(m: dict) -> None:
    t, g, d = m["team"], m["git"] or {}, m["duplication"]
    print(f"{m['trial']}: reward {m['reward']}  agents {t['agents']}  ${t['model_usd']:.4f} over "
          f"{t['model_requests']} requests  {t['duration_s']}s")
    print(f"  board: {t['sends']} sends ({t['sends_broadcast']} broadcast, {t['sends_direct']} direct, {t['sends_multi']} multi), "
          f"{t['inbox_calls']} inbox calls, {t['history_calls']} history calls, {t['rate_limited']} rate-limited, "
          f"{t['refused']} refused; peak {max(t['sends_per_minute'] or [0])} sends/min")
    for name, p in m["agents"].items():
        print(f"    {name}: {p['sends_broadcast']} broadcast, {p['sends_direct']} direct, "
              f"{p['inbox_calls']} inbox, ${p['model_usd']:.4f}")
    if g.get("commits") is not None:
        print(f"  git: {g['commits']} commits on {g['files']} files, "
              f"{g['share_commits_touching_shared_files']} of commits touch files others touched, "
              f"{len(g['branches_with_several_agents'])} branches with several agents")
    print(f"  gini sends {m['gini']['sends']}, commits {m['gini']['commits']}; "
          f"duplicates: {d['exact_groups']} exact groups ({d['sends_in_exact_groups']} sends), "
          f"{d['near_duplicate_pairs']} near pairs, {d['share_sends_with_cross_agent_near_duplicate']} of sends; "
          f"mean best cross-agent similarity {d['mean_best_cross_agent_similarity']}")
    print(f"  proxy: {t['proxy_outcomes']}")
    if t.get("subagents"):
        print(f"  subagents: {t['subagents']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("path", type=Path, help="a trial dir or a job dir")
    ap.add_argument("--out", type=Path, help="where to write the JSON (default: <path>/metrics.json)")
    args = ap.parse_args(argv)
    path = args.path.resolve()
    if not path.is_dir():
        ap.error(f"{path} is not a directory")
    if _is_trial(path):
        result = trial_metrics(path)
        _print_trial(result)
    else:
        result = job_metrics(path)
        if not result["trials"]:
            ap.error(f"no trials under {path}")
        for t in result["trial_metrics"]:
            _print_trial(t)
        a = result["aggregate"]
        print(f"job {result['job']}: {result['trials']} trials ({result['unscored_trials']} unscored), team sizes "
              f"{result['team_sizes']}; reward mean {a['reward']['mean']} sd {a['reward']['sd']}; "
              f"cost ${result['total_model_usd']:.4f} total, ${a['model_usd']['mean']} mean")
    out = args.out or path / "metrics.json"
    out.write_text(json.dumps(result, indent=2) + "\n")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
