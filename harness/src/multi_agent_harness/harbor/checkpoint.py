"""Checkpoints of a running team, and resuming a new run from one. Host side only: layout,
manifest, retention, validation and the resume note. TeamAgent collects the contents (agent
session files from the sandboxes) and TeamEnvironment the hub's part (team repo, board and
proxy logs); see docs/architecture.md, Checkpoint and resume.

A trial's checkpoints live next to the job dir, never inside it, because `harbor jobs resume`
deletes trial dirs it considers unfinished:

    <job dir>.checkpoints/<trial name>/<n>-<utc stamp>/
        manifest.json  team.bundle  board-events.jsonl  proxy-usage.jsonl
        hub-state.tgz  (with --ek hub_state_dirs: the task's own hub folders)
        agents/<name>/session/  agents/<name>/session-id
    <job dir>.checkpoints/<trial name>/mark-<label> -> the checkpoint taken at that graded mark
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

FORMAT = 2  # allocation-aware; read legacy format 1 as CPU-only when the plan is absent
KEEP = 3                          # periodic checkpoints kept per trial, besides the final one
MIN_FREE_BYTES = 2 << 30          # skip a checkpoint that would leave the host disk under this
SESSION_CAP_BYTES = 100 << 20     # an agent's session files over this are not copied
MIN_LEFT_SEC = 120                # refuse to resume with less agent time than this
RESUME_QUICK_SEC = 60             # a resumed CLI failing within this falls back to a fresh session
MANIFEST = "manifest.json"
HUB_STATE = "hub-state.tgz"       # the task's own hub folders (--ek hub_state_dirs), restored on resume
# File names that hold tokens in a CLI home: never copied, and removed again on the host.
TOKEN_FILES = ("auth.json", ".claude.json", ".claude.json.backup", ".credentials.json", "mcp_config.json",
               "agent.env", "git-credentials")
_DIR = re.compile(r"^(\d+)-(\d{8}T\d{6}Z)$")


def root_for(trial_dir: Path) -> Path:
    """<job dir>.checkpoints/<trial name>, beside the job dir."""
    trial_dir = Path(trial_dir).resolve()
    job = trial_dir.parent
    return job.parent / f"{job.name}.checkpoints" / trial_dir.name


def listing(root: Path) -> list[tuple[int, Path]]:
    """Finished checkpoints under root, oldest first."""
    if not root.is_dir():
        return []
    found = [(int(m.group(1)), p) for p in root.iterdir() if p.is_dir() and (m := _DIR.match(p.name))]
    return sorted(found)


def next_n(root: Path) -> int:
    done = listing(root)
    return done[-1][0] + 1 if done else 1


def begin(root: Path, n: int) -> Path:
    """A private temp dir inside root, renamed into place by commit()."""
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = root / f".tmp-{n}-{secrets.token_hex(4)}"
    tmp.mkdir(mode=0o700)
    return tmp


def lock_down(path: Path) -> None:
    """0700 dirs, 0600 files."""
    os.chmod(path, 0o700)
    for d, dirs, files in os.walk(path):
        for x in dirs:
            p = os.path.join(d, x)
            if not os.path.islink(p):
                os.chmod(p, 0o700)
        for x in files:
            p = os.path.join(d, x)
            if not os.path.islink(p):
                os.chmod(p, 0o600)


def drop_token_files(path: Path) -> int:
    """Remove any token-bearing file names a sandbox copy let through; returns how many."""
    hits = 0
    for p in list(path.rglob("*")):
        if p.is_file() and (p.name in TOKEN_FILES or p.name.startswith(".claude.json")):
            p.unlink()
            hits += 1
    return hits


def redact_tree(path: Path, values: list[str], skip: tuple[str, ...] = ("team.bundle", "hub-state.tgz")) -> int:
    """Replace each secret value in every file under path; returns how many files had one."""
    needles = [v.encode() for v in values if v and len(v) >= 8]
    hits = 0
    for p in path.rglob("*"):
        if not p.is_file() or p.is_symlink() or p.name in skip:
            continue
        data = p.read_bytes()
        new = data
        for n in needles:
            new = new.replace(n, b"[REDACTED]")
        if new != data:
            p.write_bytes(new)
            hits += 1
    return hits


def commit(tmp: Path, root: Path, n: int, manifest: dict[str, Any]) -> Path:
    """Write the manifest, lock permissions, rename the temp dir to <n>-<stamp>."""
    (tmp / MANIFEST).write_text(json.dumps(manifest, indent=1))
    lock_down(tmp)
    final = root / f"{n}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    tmp.rename(final)
    return final


def link_mark(root: Path, path: Path, label: str) -> Path:
    """root/mark-<label> -> the checkpoint taken at that graded mark, for --ek resume_from=.../mark-h04."""
    link = root / f"mark-{label}"
    if link.is_symlink():
        link.unlink()
    link.symlink_to(path.name)
    return link


def update_mark(path: Path, rec: dict[str, Any]) -> None:
    """Write a mark's graded record into a checkpoint's manifest that carries the mark, so resuming from that
    checkpoint keeps the grade instead of grading the mark again. A manifest without it is left alone."""
    m = _manifest(path)
    marks = m.get("marks") or []
    new = [dict(rec) if x.get("mark") == rec.get("mark") else x for x in marks]
    if new == marks:
        return
    m["marks"] = new
    tmp = path / f".{MANIFEST}.tmp"
    tmp.write_text(json.dumps(m, indent=1))
    os.chmod(tmp, 0o600)
    tmp.replace(path / MANIFEST)


def _manifest(path: Path) -> dict[str, Any]:
    try:
        return json.loads((path / MANIFEST).read_text())
    except (OSError, ValueError):
        return {}


def prune(root: Path, keep: int = KEEP) -> list[Path]:
    """Delete all but the last `keep` periodic checkpoints (a final one and one taken at a graded
    mark always stay) and any leftover temp dirs. Only checkpoint dirs under root are touched. Call
    with no checkpoint in progress."""
    periodic = [p for _, p in listing(root) if not (m := _manifest(p)).get("final") and not m.get("mark")]
    gone = periodic[:-keep] if keep else periodic
    gone += [p for p in root.glob(".tmp-*") if p.is_dir()]
    for p in gone:
        shutil.rmtree(p, ignore_errors=True)
    return gone


def spend(proxy_log: Path) -> float:
    """USD in a proxy usage log (one JSON record per model call)."""
    total = 0.0
    try:
        for line in proxy_log.read_text().splitlines():
            try:
                total += float(json.loads(line).get("usd") or 0)
            except (ValueError, TypeError, AttributeError):
                continue
    except OSError:
        pass
    return round(total, 6)


def spawned(board_log: Path) -> list[str]:
    """Subagent names the lead spawned, from a board event log."""
    names: list[str] = []
    try:
        lines = board_log.read_text().splitlines()
    except OSError:
        return names
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("event") == "subagent" and e.get("op") == "spawn" and e.get("name") not in names:
            names.append(e["name"])
    return names


def time_left(manifest: dict[str, Any]) -> float | None:
    total = manifest.get("agent_timeout_sec")
    if not total:
        return None
    return float(total) - float(manifest.get("elapsed_agent_sec") or 0)


def load(path: str | Path, *, task: str | None, harness: str, n_agents: int,
         gpu_allocation: dict[str, Any] | None = None, harnesses: dict[str, str] | None = None) -> dict[str, Any]:
    """The checkpoint at `path`, checked against this run's config. `harness` is the team's one
    harness or "mixed"; `harnesses`, each agent's, is checked against the agents the checkpoint
    records one for, since a CLI session only resumes in its own CLI. Raises ValueError with every
    mismatch, or when too little agent time is left. Returns {"path", "manifest"}."""
    path = Path(path).expanduser().resolve()
    m = _manifest(path)
    if not m:
        raise ValueError(f"resume_from={path}: no readable {MANIFEST}; pass a checkpoint dir "
                         f"(<job>.checkpoints/<trial>/<n>-<stamp>)")
    if m.get("format") not in (1, FORMAT):
        raise ValueError(f"resume_from={path}: checkpoint format {m.get('format')!r}, this harness reads 1 and {FORMAT}")
    if m.get("format") == FORMAT and not isinstance(m.get("gpu_allocation"), dict):
        raise ValueError(f"resume_from={path}: format {FORMAT} requires a GPU allocation plan")
    if not (path / "team.bundle").is_file():
        raise ValueError(f"resume_from={path}: team.bundle is missing")
    wrong = [f"{k} is {got!r} in the checkpoint but {want!r} in this run"
             for k, got, want in (("task", m.get("task"), task), ("harness", m.get("harness"), harness),
                                  ("n_agents", m.get("n_agents"), n_agents))
             if got != want and not (k == "task" and task is None)]
    saved = {a: v.get("harness") for a, v in (m.get("agents") or {}).items() if isinstance(v, dict) and v.get("harness")}
    wrong += [f"{a} runs {h!r} in the checkpoint but {harnesses[a]!r} in this run"
              for a, h in sorted(saved.items()) if harnesses and a in harnesses and harnesses[a] != h]
    from .resources import gpu_plan
    want = gpu_allocation if gpu_allocation is not None else gpu_plan(n_agents)
    got = m.get("gpu_allocation", gpu_plan(n_agents))  # legacy checkpoints are CPU-only
    if got != want:
        wrong.append(f"GPU allocation is {got!r} in the checkpoint but {want!r} in this run")
    if wrong:
        raise ValueError(f"resume_from={path} does not match this run: " + "; ".join(wrong))
    left = time_left(m)
    if left is not None and left < MIN_LEFT_SEC:
        raise ValueError(f"resume_from={path}: only {max(0, int(left))}s of the {int(m['agent_timeout_sec'])}s "
                         f"agent timeout is left (minimum {MIN_LEFT_SEC}s); not resuming")
    return {"path": path, "manifest": m}


def _duration(sec: float) -> str:
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def note(manifest: dict[str, Any], board_path: str, left: float | None, subagents: list[str] | None = None) -> str:
    """The resume note for an agent's prompt."""
    text = (f"Note: this run was interrupted and resumed from a checkpoint taken "
            f"{_duration(manifest.get('elapsed_agent_sec') or 0)} into it. The team's work so far is on main "
            f"(your working directory is a fresh clone of it; anything that was not pushed is gone). "
            f"The board starts empty: earlier board messages are in {board_path}, read it before going on.")
    if left is not None:
        text += f" {_duration(left)} of agent time remains."
    if subagents:
        text += (f" Subagents spawned before the interruption ({', '.join(subagents)}) are gone; spawn new ones "
                 f"if you still need help.")
    return text
