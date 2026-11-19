"""Live progress of graded flights, for a flight viewer.

While tests/verify.py flies, each flight appends short JSON lines to its own file in a live folder
(MARSFLIGHT_LIVE_DIR, by default /logs/verifier/live; an empty value turns it off):

  {"k": "start", "label": "world-03", "kind": "world", "index": 3, "seed": ..., "wall0": <unix time>, ...}
  {"k": "s", "w": <wall s>, "met": <mission s>, "c": "Earth"|"Sun"|"Mars", "ph": <phase>, "r": [x, y, z] km from the
   central body (J2000 axes, null on the ground), "u": [vx, vy, vz] km/s, "hs": [x, y] Sun-centred ecliptic position
   in 1000 km, "alt": km, "v": km/s, "m": kg, "p": progress, "pl": {"Earth": [x, y], "Mars": [x, y]} (in cruise only), "el": [element ids]
   (when they change), "ll": [lat, lon] (on the ground)}
  {"k": "e", "w": ..., "met": ..., "type": "milestone", "name": "liftoff"}       one per event as it happens
  {"k": "end", "w": ..., "met": ..., "failure": null | "<text>", "milestones": [...], "progress": ..., "live_s": ...}

A state line is written at most once a second of wall time, unless the vehicle changed central body or phase or
moved more than 4% of its distance from the central body since the last one. Each file stays under MAX_BYTES (state
lines stop there; events and the end line are always written). Lines are flushed as they are written.

What goes in: the flight's own trajectory, phase, milestones and the outcome, all of which details.json holds too.
What stays out: the secret, the hidden as-built truth, hidden reliability factors and fault draws. Events are
copied only for the types in EVENT_TYPES and only with the fields in EVENT_FIELDS, so the in-flight fault model's
events (fault, string_switched, ...) and model internals carried on events (parachute opening constants, ...)
are never written.

The folder is the grader's, never the team's: tests/verify.py makes it readable by root (and the owner of the
output folder on the host) only, so flight software, which runs as another user, can't read the true state.
Writing never raises into the flight: any error turns the log off for that flight.
"""
from __future__ import annotations

import json
import math
import os
import time

MAX_BYTES = 4_000_000
SAMPLE_WALL_S = 1.0
SAMPLE_MOVE = 0.04
MAX_ARC_POINTS = 24
OBLIQUITY = math.radians(23.4392911)   # J2000 equator to ecliptic
EVENT_TYPES = {
    "milestone", "failure", "liftoff", "ignition", "ignition_failed", "shutdown", "burnout", "engine_out",
    "separation", "touchdown", "landing_error_km", "landed_outside_range", "soi_enter", "soi_exit", "central_body",
    "atmosphere_interface", "parachute_deployed", "parachute_opened", "parachute_failed", "samples_loaded",
    "propellant_depleted", "ballast_jettisoned", "launch_scrubbed", "mission_ended_by_guidance", "legs_deployed",
    "scenario_start",
}
EVENT_FIELDS = ("name", "element", "engine", "body", "frm", "to", "km", "reason", "phase")


def live_dir(default=None):
    """The live folder: MARSFLIGHT_LIVE_DIR when set (empty turns it off), else default."""
    d = os.environ.get("MARSFLIGHT_LIVE_DIR")
    if d is None:
        d = default
    return d or None


def file_name(kind, index):
    return f"{kind}-{int(index):02d}.jsonl"


class LiveLog:
    """One flight's live file. tag: {"dir", "kind", "index", "label", ...}; extra keys go into the start line."""

    def __init__(self, tag, seed=None):
        self.f = None
        self.cost = 0.0
        self.bytes = 0
        self.last_w = -1e30
        self.last = None          # (central, phase, r) of the last state line
        self.last_el = None
        t0 = time.perf_counter()
        try:
            d = tag["dir"]
            kind, index = tag.get("kind", "world"), int(tag.get("index", 0))
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, file_name(kind, index))
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            st = os.stat(d)
            if st.st_uid != os.geteuid():   # the folder's owner (the host user of a mounted output folder) reads it
                try:
                    os.fchown(fd, st.st_uid, st.st_gid)
                except OSError:
                    pass
            self.f = os.fdopen(fd, "w", buffering=1)
            self.wall0 = time.time()
            start = {"k": "start", "kind": kind, "index": index, "label": tag.get("label") or f"{kind}-{index:02d}",
                     "seed": seed, "wall0": round(self.wall0, 3), "pid": os.getpid()}
            for k in ("engine_out", "failure_check", "max_wall_s"):
                if tag.get(k) is not None:
                    start[k] = tag[k]
            self._write(start)
        except Exception:  # noqa: BLE001
            self.f = None
        self.cost += time.perf_counter() - t0

    def _write(self, row):
        if self.f is None:
            return
        try:
            s = json.dumps(row, separators=(",", ":"), default=_plain) + "\n"
            self.f.write(s)
            self.bytes += len(s)
        except Exception:  # noqa: BLE001
            self.close()

    def _w(self):
        return round(time.time() - self.wall0, 2)

    # ------------------------------------------------------------------ hooks called by Mission
    def event(self, ev):
        if self.f is None or ev.get("type") not in EVENT_TYPES:
            return
        t0 = time.perf_counter()
        row = {"k": "e", "w": self._w(), "met": ev.get("met_s"), "type": ev["type"]}
        for k in EVENT_FIELDS:
            v = ev.get(k)
            if v is not None and (k != "reason" or ev["type"] in ("failure", "ignition_failed")):
                row[k] = v if isinstance(v, (str, int, float, bool)) else str(v)
        self._write(row)
        self.cost += time.perf_counter() - t0

    def sample(self, m, force=False):
        """After each arc: a state line if a second of wall time passed or the vehicle moved on."""
        if self.f is None:
            return
        t0 = time.perf_counter()
        try:
            w = time.time() - self.wall0
            x = m.x
            ph = m.phase()
            due = force or w - self.last_w >= SAMPLE_WALL_S
            if not due and self.last is not None:
                c0, ph0, r0 = self.last
                if c0 != m.central or ph0 != ph:
                    due = True
                elif x is not None and r0 is not None:
                    d = math.dist(x[:3], r0)
                    due = d > SAMPLE_MOVE * max(1.0, math.hypot(*r0))
                elif (x is None) != (r0 is None):
                    due = True
            if due and (self.bytes < MAX_BYTES or force):
                self._write(self._state(m, w, ph))
                self.last_w = w
                self.last = (m.central, ph, None if x is None else [float(v) for v in x[:3]])
        except Exception:  # noqa: BLE001
            pass
        self.cost += time.perf_counter() - t0

    def arc(self, m, central, ts, sh):
        """Points along an arc the vehicle just flew, where it moved on (the same 4% rule as state lines), at most
        MAX_ARC_POINTS: one line {"k": "a", "c": body, "w": ..., "met": [...], "p": [[x, y, z] km, ...]} ([x, y] in
        1000 km on the Sun-centred ecliptic in cruise). Long coasts then draw as curves, not chords."""
        if self.f is None or self.bytes >= MAX_BYTES:
            return
        t0 = time.perf_counter()
        try:
            last = self.last[2] if self.last is not None and self.last[0] == central else None
            pts = []
            for t in ts[:-1]:
                r = [float(v) for v in sh[t][:3]]
                if last is None or math.dist(r, last) > SAMPLE_MOVE * max(1.0, math.hypot(*last)):
                    pts.append((t, r))
                    last = r
            if len(pts) > MAX_ARC_POINTS:
                step = len(pts) / MAX_ARC_POINTS
                pts = [pts[int(i * step)] for i in range(MAX_ARC_POINTS)]
            if pts:
                if central == "Sun":
                    p = [_ecl(r) for _, r in pts]
                else:
                    p = [[round(v / 1e3, 1) for v in r] for _, r in pts]
                self._write({"k": "a", "c": central, "w": self._w(), "met": [round(t - m.t0, 1) for t, _ in pts], "p": p})
                self.last = (central, self.last[1] if self.last else None, pts[-1][1])
        except Exception:  # noqa: BLE001
            pass
        self.cost += time.perf_counter() - t0

    def _state(self, m, w, ph):
        c = m.central
        row = {"k": "s", "w": round(w, 2), "met": round(m.t - m.t0, 1), "c": c, "ph": ph}
        x = m.x
        sun = None
        if x is not None:
            r = [float(v) for v in x[:3]]
            row["r"] = [round(v / 1e3, 1) for v in r]
            u = [float(v) / 1e3 for v in x[3:6]]
            row["u"] = [round(v, 4) for v in u]
            row["v"] = round(math.hypot(*u), 3)
            if c != "Sun":
                row["alt"] = round(m.altitude(x[:3], m.t) / 1e3, 2)
            try:
                sun = m.central_state(c, m.t)[:3] - m.central_state("Sun", m.t)[:3] if c != "Sun" else None
                h = r if c == "Sun" else [r[i] + float(sun[i]) for i in range(3)]
                row["hs"] = _ecl(h)
            except Exception:  # noqa: BLE001
                pass
        else:
            row["r"] = None
            lat, lon, alt = m.surface_site
            row["ll"] = [round(float(lat), 3), round(float(lon), 3)]
            row["alt"] = round(float(alt) / 1e3, 3)
            row["v"] = 0.0
        if c == "Sun":
            try:
                s = m.central_state("Sun", m.t)[:3]
                row["pl"] = {b: _ecl(m.central_state(b, m.t)[:3] - s) for b in ("Earth", "Mars")}
            except Exception:  # noqa: BLE001
                pass
        row["m"] = round(float(m.total_mass()), 1)
        row["p"] = round(float(m.progress()), 4)
        el = [s.el.id for s in m.states]
        if el != self.last_el:
            row["el"] = el
            self.last_el = el
        return row

    def finish(self, res):
        """The end line, from fly_one's result."""
        if self.f is None:
            return
        t0 = time.perf_counter()
        ms = res.get("milestones") or {}
        t_end, t0m = res.get("end_t"), res.get("t0")
        met = round(t_end - t0m, 1) if isinstance(t_end, (int, float)) and isinstance(t0m, (int, float)) else None
        self.cost += time.perf_counter() - t0
        self._write({"k": "end", "w": self._w(), "met": met, "failure": res.get("failure"),
                     "milestones": list(ms.keys()), "progress": res.get("progress"), "wall_s": res.get("wall_s"),
                     "live_s": round(self.cost, 4), "live_bytes": self.bytes})
        self.close()

    def close(self):
        try:
            if self.f is not None:
                self.f.close()
        except Exception:  # noqa: BLE001
            pass
        self.f = None


def _ecl(h):
    """Sun-centred J2000 equatorial (m) to ecliptic x, y in 1000 km."""
    x, y, z = (float(v) for v in h[:3])
    return [round(x / 1e6, 1), round((y * math.cos(OBLIQUITY) + z * math.sin(OBLIQUITY)) / 1e6, 1)]


def _plain(o):
    try:
        return float(o)
    except Exception:  # noqa: BLE001
        return str(o)


def write_run(d, info):
    """The run's own file (run.json) in the live folder: what is flying, and at the end the outcome per world.
    Written whole each time through a temporary file, so a reader never sees half of it."""
    if not d:
        return
    try:
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, ".run.json.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        st = os.stat(d)
        if st.st_uid != os.geteuid():
            try:
                os.fchown(fd, st.st_uid, st.st_gid)
            except OSError:
                pass
        with os.fdopen(fd, "w") as f:
            json.dump(info, f, default=_plain)
        os.replace(tmp, os.path.join(d, "run.json"))
    except Exception:  # noqa: BLE001
        pass
