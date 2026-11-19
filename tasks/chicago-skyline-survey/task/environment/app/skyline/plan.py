"""Plan files: ``plans/D1.csv`` ... ``plans/D8.csv``.

Header, exact: ``x,y,z,speed,action,arg,soc,depart_after``.

Each row is the next waypoint; the first leg starts at the drone's start pad.

* ``fly``   - arg and soc empty; on arrival hover until t >= depart_after.
* ``photo`` - arg = target id; on arrival wait until t >= depart_after, hover
  ``hover_s``, take the photo, continue.
* ``pad``   - arg = pad id; soc optional in [0, 1] (default 1.0); the
  waypoint must be the pad position (horizontal within 1 m, z within 0.5 m);
  land, charge, leave when t >= depart_after and battery >= soc * battery_J.
* ``scan``  - (mapping and show rules) arg = landmark id; like ``photo``: on arrival wait
  until t >= depart_after, hover ``mapping.scan_hover_s``, scan, continue.
* ``show``  - (mapping and show rules) arg = ``<show id>:<slot>`` (slot counted from 0);
  the waypoint must be that slot's position within ``shows.slot_tol_m`` in each
  of x, y and z; hold until max(t0 + hold_s, depart_after), then continue.

A malformed file gives the drone an empty plan (it stays on its start pad all
day) and an error message. Malformed means: wrong header; a wrong number of
fields; a non-numeric (or non-finite) numeric field; an unknown action,
target id, pad id, landmark id or show id; a show slot out of range or a
show row not at its slot; more than ``max_rows_per_drone`` rows; x or y outside the
bbox; speed outside [airspeed_min, airspeed_max]; soc outside [0, 1]; a pad
row not at its pad; not readable as CSV. A symlinked file or one over 2,000,000 bytes is ignored
(reported as an error), as the grader ignores it. A missing file is not an error: the drone stays on its pad.

Parsing notes: fields are stripped of surrounding whitespace; completely
blank lines are skipped; ``arg`` is ignored on ``fly`` rows; ``soc`` is only
used on ``pad`` rows (on other rows it must still be empty or a number in
[0, 1]); an empty ``depart_after`` means 0.
"""

from __future__ import annotations

import csv
import io
import math
import os
from dataclasses import dataclass

from .geometry import EPS

HEADER = ["x", "y", "z", "speed", "action", "arg", "soc", "depart_after"]
HEADER_LINE = ",".join(HEADER)
ACTIONS = ("fly", "photo", "pad")
ACTIONS_V2B = ("fly", "photo", "pad", "scan", "show")
PAD_XY_TOL_M = 1.0
PAD_Z_TOL_M = 0.5
MAX_PLAN_BYTES = 2_000_000  # same cap as the grader


@dataclass(frozen=True)
class Row:
    """One plan row (a waypoint and what to do on arrival).

    ``soc`` is a float for pad rows (1.0 when empty) and None otherwise.
    ``line`` is the 1-based line number in the CSV file (0 if built in code).
    """

    x: float
    y: float
    z: float
    speed: float
    action: str
    arg: str = ""
    soc: float | None = None
    depart_after: float = 0.0
    line: int = 0

    @property
    def pos(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.z)


class PlanError(ValueError):
    """A malformed plan file."""


#: Longest accepted numeric field (characters). Longer fields are malformed, so no parse is ever unbounded.
MAX_NUM_CHARS = 64
#: Longest accepted show slot number (digits).
MAX_SLOT_DIGITS = 4


def _short(s: str) -> str:
    return repr(s) if len(s) <= 40 else repr(s[:20]) + f"... ({len(s)} characters)"


def _num(s: str, name: str, line: int) -> float:
    if len(s) > MAX_NUM_CHARS:
        raise PlanError(f"line {line}: {name} {_short(s)} is longer than {MAX_NUM_CHARS} characters")
    try:
        v = float(s)
    except (ValueError, OverflowError):
        raise PlanError(f"line {line}: {name} {s!r} is not a number") from None
    if not math.isfinite(v):
        raise PlanError(f"line {line}: {name} {s!r} is not a finite number")
    return v


def parse_plan_text(text: str, world) -> list[Row]:
    """Parse one plan CSV. Raises :class:`PlanError` with a message if malformed."""
    try:
        return _parse_rows(text, world)
    except csv.Error as e:  # e.g. a lone carriage return inside a row, or a field over the csv size limit
        raise PlanError(f"not a readable CSV file: {e}") from None
    except (ValueError, OverflowError) as e:  # any other bad value is a malformed file, never a crash
        raise PlanError(f"malformed value: {str(e)[:200]}") from None


def _parse_rows(text: str, world) -> list[Row]:
    r = world.rules
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise PlanError(f"empty file; expected header {HEADER_LINE!r}") from None
    if header != HEADER:
        raise PlanError(f"line 1: header must be exactly {HEADER_LINE!r}, got {','.join(header)!r}")
    xmin, ymin, xmax, ymax = world.bbox
    actions = ACTIONS_V2B if r.v2b else ACTIONS
    rows: list[Row] = []
    for fields in reader:
        line = reader.line_num
        if not fields or all(not f.strip() for f in fields):
            continue
        if len(fields) != len(HEADER):
            raise PlanError(f"line {line}: expected {len(HEADER)} fields, got {len(fields)}")
        if len(rows) >= r.max_rows_per_drone:
            raise PlanError(f"more than {r.max_rows_per_drone} rows")
        fx, fy, fz, fspeed, action, arg, fsoc, fdep = (f.strip() for f in fields)
        x, y, z = _num(fx, "x", line), _num(fy, "y", line), _num(fz, "z", line)
        speed = _num(fspeed, "speed", line)
        soc = None if fsoc == "" else _num(fsoc, "soc", line)
        dep = 0.0 if fdep == "" else _num(fdep, "depart_after", line)
        if action not in actions:
            raise PlanError(f"line {line}: unknown action {action!r} (use {', '.join(actions[:-1])} or {actions[-1]})")
        if not (xmin <= x <= xmax and ymin <= y <= ymax):
            raise PlanError(f"line {line}: ({x}, {y}) is outside the bbox {list(world.bbox)}")
        if not (r.airspeed_min_mps <= speed <= r.airspeed_max_mps):
            raise PlanError(f"line {line}: speed {speed} outside [{r.airspeed_min_mps}, {r.airspeed_max_mps}]")
        if soc is not None and not (0.0 <= soc <= 1.0):
            raise PlanError(f"line {line}: soc {soc} outside [0, 1]")
        if action == "photo":
            if arg not in world.targets:
                raise PlanError(f"line {line}: unknown target id {arg!r}")
            soc = None
        elif action == "pad":
            pad = world.pads.get(arg)
            if pad is None:
                raise PlanError(f"line {line}: unknown pad id {arg!r}")
            px, py, pz = pad.pos
            if math.hypot(x - px, y - py) > PAD_XY_TOL_M or abs(z - pz) > PAD_Z_TOL_M or z < 0.0:
                raise PlanError(f"line {line}: pad row ({x}, {y}, {z}) is not at pad {arg} {list(pad.pos)}"
                                f" (tolerance {PAD_XY_TOL_M} m horizontal, {PAD_Z_TOL_M} m vertical)")
            soc = 1.0 if soc is None else soc
        elif action == "scan":
            if arg not in world.landmarks:
                raise PlanError(f"line {line}: unknown landmark id {arg!r}")
            soc = None
        elif action == "show":
            show, slot = parse_show_arg(arg, world, line)
            sx, sy, sz = world.shows[show].slots[slot]
            tol = r.show_slot_tol_m
            if abs(x - sx) > tol + EPS or abs(y - sy) > tol + EPS or abs(z - sz) > tol + EPS:
                raise PlanError(f"line {line}: show row ({x}, {y}, {z}) is not at slot {arg} {[sx, sy, sz]}"
                                f" (tolerance {tol} m in each of x, y and z)")
            soc = None
        else:
            arg, soc = "", None
        rows.append(Row(x, y, z, speed, action, arg, soc, dep, line))
    return rows


def parse_show_arg(arg: str, world, line: int = 0) -> tuple[str, int]:
    """Split a show row's arg ``<show id>:<slot>`` into (show id, slot index); PlanError if invalid."""
    sid, sep, slot_s = arg.rpartition(":")
    if not sep or not slot_s.isascii() or not slot_s.isdigit():
        raise PlanError(f"line {line}: show arg {_short(arg)} must be <show id>:<slot index>, e.g. S2:3")
    if len(slot_s) > MAX_SLOT_DIGITS:
        raise PlanError(f"line {line}: show arg {_short(arg)}: slot number longer than {MAX_SLOT_DIGITS} digits")
    if sid not in world.shows:
        raise PlanError(f"line {line}: unknown show id {_short(sid)}")
    slot = int(slot_s)
    n = len(world.shows[sid].slots)
    if slot >= n:
        raise PlanError(f"line {line}: show {sid} has slots 0 to {n - 1}, not {slot}")
    return sid, slot


def load_plan_file(path: str, world) -> list[Row]:
    """Parse one plan file. Raises :class:`PlanError` if malformed."""
    try:
        with open(path, encoding="utf-8", newline="") as f:
            text = f.read()
    except UnicodeDecodeError:
        raise PlanError("file is not valid UTF-8 text") from None
    return parse_plan_text(text, world)


def load_plans(plans_dir: str, world) -> tuple[dict, dict]:
    """Load ``<plans_dir>/<drone>.csv`` for every drone in the fleet.

    Returns ``(plans, errors)``: ``plans`` maps every drone id to a list of
    :class:`Row` (empty when the file is missing or malformed); ``errors``
    maps drone id -> message for malformed files only.
    """
    plans: dict = {}
    errors: dict = {}
    for drone in world.rules.fleet:
        path = os.path.join(plans_dir, f"{drone}.csv")
        if os.path.islink(path):  # the grader copies regular files only, so a symlinked plan is never flown
            plans[drone] = []
            errors[drone] = f"{drone}.csv: is a symlink; plan files must be regular files (ignored)"
            continue
        if not os.path.isfile(path):
            plans[drone] = []
            continue
        if os.path.getsize(path) > MAX_PLAN_BYTES:
            plans[drone] = []
            errors[drone] = f"{drone}.csv: larger than {MAX_PLAN_BYTES} bytes (ignored)"
            continue
        try:
            plans[drone] = load_plan_file(path, world)
        except (PlanError, OSError) as e:
            plans[drone] = []
            errors[drone] = f"{drone}.csv: {e}"
    return plans, errors


def _fmt(v: float) -> str:
    return repr(float(v))


def format_plan(rows) -> str:
    """Write rows back as CSV text (header included), round-trippable through :func:`parse_plan_text`."""
    out = [HEADER_LINE]
    for r in rows:
        soc = "" if (r.soc is None or r.action != "pad") else _fmt(r.soc)
        arg = r.arg if r.action != "fly" else ""
        out.append(",".join([_fmt(r.x), _fmt(r.y), _fmt(r.z), _fmt(r.speed), r.action, arg, soc,
                             _fmt(r.depart_after)]))
    return "\n".join(out) + "\n"
