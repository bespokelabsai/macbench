"""hello-team verifier. Runs on the hub.

Reward is the mean of four checks, so 1.0 only when all pass:
  lines   agents with exactly one `<name>: <number>` line in team.txt, over agents + stray lines
  unique  1 if the numbers on team.txt lines are all distinct (and there is at least one)
  leader  1 if LEADER is one line naming a registered agent
  sent    agents with at least one send on the board, over agents
Registered agents are the `register` events in the board log.

Paths: MULTI_AGENT_HARNESS_TEAM_GIT (default /srv/git/team.git), BOARD_LOG (/var/lib/multi-agent-harness/board-events.jsonl, as TeamEnvironment starts it),
MULTI_AGENT_HARNESS_VERIFIER_DIR (/logs/verifier).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

LINE_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*:\s*(-?\d+(?:\.\d+)?)\s*$")


def git_show(repo: str, path: str) -> str | None:
    """File contents at HEAD (main), else at the most recently committed branch."""
    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-c", "safe.directory=*", "--git-dir", repo, *args],
                              capture_output=True, text=True)
    refs = ["HEAD"] + run("for-each-ref", "--sort=-committerdate", "--format=%(refname)",
                          "refs/heads").stdout.split()
    for ref in refs:
        if run("rev-parse", "--verify", "-q", ref).returncode == 0:
            out = run("show", f"{ref}:{path}")
            return out.stdout if out.returncode == 0 else None
    return None


def read_events(path: str) -> list[dict]:
    events = []
    try:
        for line in Path(path).read_text().splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    except OSError:
        pass
    return events


def score(repo: str, board_log: str) -> dict:
    events = read_events(board_log)
    agents = sorted({e["agent"] for e in events if e.get("event") == "register" and e.get("agent")})
    senders = {e.get("agent") for e in events if e.get("event") == "send"}
    team = git_show(repo, "team.txt") or ""
    leader = (git_show(repo, "LEADER") or "").strip()

    parsed, stray = [], 0
    for line in team.splitlines():
        if not line.strip():
            continue
        m = LINE_RE.match(line)
        if m and m.group(1) in agents:
            parsed.append((m.group(1), float(m.group(2))))
        else:
            stray += 1
    per_agent = Counter(name for name, _ in parsed)
    numbers = [n for _, n in parsed]
    n = len(agents)
    checks = {
        "lines": sum(per_agent[a] == 1 for a in agents) / (n + stray) if n else 0.0,
        "unique": float(bool(numbers) and len(set(numbers)) == len(numbers)),
        "leader": float(leader in agents and "\n" not in leader),
        "sent": sum(a in senders for a in agents) / n if n else 0.0,
    }
    reward = sum(checks.values()) / len(checks)
    return {"reward": reward, "checks": checks, "agents": agents, "stray_lines": stray,
            "leader": leader}


def main() -> None:
    out_dir = Path(os.environ.get("MULTI_AGENT_HARNESS_VERIFIER_DIR", "/logs/verifier"))
    result = score(os.environ.get("MULTI_AGENT_HARNESS_TEAM_GIT", "/srv/git/team.git"),
                   os.environ.get("BOARD_LOG", "/var/lib/multi-agent-harness/board-events.jsonl"))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "details.json").write_text(json.dumps(result, indent=2))
    (out_dir / "reward.txt").write_text(f"{result['reward']:.4f}\n")
    print(json.dumps(result), file=sys.stderr)


if __name__ == "__main__":
    main()
