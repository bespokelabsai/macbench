"""Graded marks: grade the team's main at set times while the agents keep working.

    --ak grade_at=1h,2h,4h,8h,16h      (also minutes and seconds: 2m,4m,8m or 90s; a bare number is seconds)

Times are agent time, measured from when the agents start (on a resumed run, counting the time the
earlier run already used). At each mark, without stopping or pausing the agents, TeamAgent

1. pins the team repo's main commit,
2. takes a checkpoint labelled with the mark (kept by retention, like the final one),
3. queues the mark's grading. One grading runs at a time; a mark that comes while the last one is
   still grading waits for it. TeamEnvironment.grade_mark runs the task's own verifier (the tests dir,
   the [verifier] env, user and timeout of task.toml) on the hub, against a fresh checkout of the
   pinned commit, with its output in a folder of its own. The task reads two environment variables:

       MULTI_AGENT_HARNESS_GRADE_APP   the checkout of the pinned commit (the final verifier's is the workdir)
       MULTI_AGENT_HARNESS_GRADE_OUT   where to write reward.txt or reward.json and anything else
                                       (the final verifier's is /logs/verifier)

   plus MULTI_AGENT_HARNESS_GRADE_MARK (the label, e.g. h04) and MULTI_AGENT_HARNESS_GRADE_COMMIT.
   A task that ignores them grades the workdir and writes /logs/verifier, so it must honour them.
4. downloads the mark's output into <trial>/marks/<label>/ and records
   {mark, mark_s, elapsed_s, commit, reward, verifier_s, status, ...} in the ledger and in
   result.json's agent metadata under "marks".

A mark at the end of the run (within END_SLACK_SEC of the agent timeout) is not graded separately:
Harbor's normal final verifier is that mark, and the trial's reward stays the final verifier's. Its
record is filled in when the environment stops, after the verifier, and its files are copied from
<trial>/verifier into <trial>/marks/<label>/. A grading that fails or times out is recorded as failed
and the run goes on. Checkpoint manifests carry the records, so a resumed run keeps the marks already
graded and grades again a mark whose grading had not finished.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

GRADE_APP_VAR = "MULTI_AGENT_HARNESS_GRADE_APP"
GRADE_OUT_VAR = "MULTI_AGENT_HARNESS_GRADE_OUT"
GRADE_MARK_VAR = "MULTI_AGENT_HARNESS_GRADE_MARK"
GRADE_COMMIT_VAR = "MULTI_AGENT_HARNESS_GRADE_COMMIT"

_UNITS = {"h": 3600.0, "m": 60.0, "s": 1.0, "": 1.0}
_ITEM = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*(h|m|s|)\s*$", re.I)
END_SLACK_SEC = 60.0   # a mark this close to the agent timeout (or past it by less) is the final verifier's
MARKS_DIR = "marks"    # <trial>/marks/<label>/


def parse(spec: Any) -> list[float]:
    """'1h,2h,4h' -> [3600, 7200, 14400]. Sorted, positive, no repeats. Raises ValueError."""
    if spec in (None, ""):
        return []
    items = spec if isinstance(spec, (list, tuple)) else str(spec).split(",")
    out: list[float] = []
    for item in items:
        if isinstance(item, (int, float)):
            sec = float(item)
        else:
            m = _ITEM.match(str(item))
            if not m:
                raise ValueError(f"grade_at: {item!r} is not a time like 2h, 30m or 90s")
            sec = float(m.group(1)) * _UNITS[m.group(2).lower()]
        if sec <= 0:
            raise ValueError(f"grade_at: {item!r} must be after the start")
        out.append(sec)
    if len(set(out)) != len(out):
        raise ValueError(f"grade_at: {spec!r} names a time twice")
    return sorted(out)


def label(sec: float) -> str:
    """h01, h16 for whole hours; m02 for whole minutes; s090 otherwise. Sorts by time within a schedule
    of one unit."""
    if sec % 3600 == 0:
        return f"h{int(sec // 3600):02d}"
    if sec % 60 == 0:
        return f"m{int(sec // 60):02d}"
    return f"s{int(sec):03d}" if sec == int(sec) else f"s{sec:g}"


def words(sec: float) -> str:
    """'hour 4', 'minute 8', 'second 90': how a mark is named to people."""
    if sec % 3600 == 0:
        return f"hour {int(sec // 3600)}"
    if sec % 60 == 0:
        return f"minute {int(sec // 60)}"
    return f"second {sec:g}"


def is_final(sec: float, timeout: float | None) -> bool:
    """The mark is the end of the run: within END_SLACK_SEC (at most 2% of the run) of the agent timeout."""
    return bool(timeout) and abs(sec - float(timeout)) <= min(END_SLACK_SEC, 0.02 * float(timeout))


def read_reward(folder: Path) -> float | None:
    """The reward in a verifier output folder, as Harbor reads it: reward.json first (its "reward" key,
    or its only value), else reward.txt. None when there is none or it is not a finite number."""
    folder = Path(folder)
    try:
        if (folder / "reward.json").is_file():
            data = json.loads((folder / "reward.json").read_text())
            if isinstance(data, dict):
                data = data.get("reward", next(iter(data.values()), None) if len(data) == 1 else None)
            value = float(data)
        elif (folder / "reward.txt").is_file():
            value = float((folder / "reward.txt").read_text().strip())
        else:
            return None
    except (OSError, ValueError, TypeError):
        return None
    return value if math.isfinite(value) else None


def schedule(marks: list[float], timeout: float | None) -> list[dict[str, Any]]:
    """[{mark, mark_s, final}] for the marks the run reaches; a mark past the end is dropped."""
    out = []
    for sec in marks:
        final = is_final(sec, timeout)
        if timeout and sec > float(timeout) and not final:
            continue
        out.append({"mark": label(sec), "mark_s": sec, "final": final})
        if final:
            break
    return out
