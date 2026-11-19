"""Simulation result, flight log and deterministic JSON output."""

from __future__ import annotations

import json
from bisect import bisect_right

LOG_INTERVAL_S = 5


class SimResult:
    """Outcome of one simulated day.

    Attributes
    ----------
    points : float
        The day's points.
    per_drone : dict
        drone id -> ``{"lost", "reason", "t_lost", "photos_taken", "photos_banked",
        "points", "energy_J", "rows", "scans_taken", "scans_banked", "sorties"}``.
        ``points`` is the drone's photo points only (landmark and show points belong to
        the fleet). ``sorties`` lists, per sortie (takeoff to landing, the first from
        t = 0), ``[captures recorded, captures card_full]``.
    photos : list[dict]
        Every photo taken, sorted by (t, drone): ``{"t", "drone", "target", "valid",
        "reason", "banked", "accepted", "value", "pos", "light", "quality", "synced"}``.
        ``reason`` is None for an accepted photo; otherwise the invalidity reason
        (``absent``, ``range``, ``cone``, ``elevation``, ``los``), ``card_full`` (over
        the sortie's memory card, never recorded; ``valid`` is False), ``void`` (valid
        but the drone was lost before landing), ``noise`` (banked in an over-budget
        noise zone-hour), ``unsynced`` (a banked moving photo
        with no partner photo, see ``scoring``), or ``repeat_angle`` /
        ``repeat_limit`` (banked, not accepted). ``value`` is the points credited.
        ``light`` is the sun-light factor L(target, t) for a static target and 1.0
        for a moving one. ``quality`` is the photo quality Q from the drone's
        distance and viewing angle (:func:`skyline.scoring.quality`; None when
        the target has no position at the photo time). ``synced`` is None for a static target; for a moving one
        it is True when the photo is valid, banked and synced, else False.
    events : list[dict]
        ``{"t", "drone", "type", "detail"}`` in processing (time) order; type in
        takeoff | land | queue | lost | photo; detail is a string (pad id, target id,
        or for ``lost`` the reason optionally followed by ``": text"``).
    flightlog : dict or None
        Viewer input (only when simulated with ``log=True``).
    day : str
        Wind day label (``"forecast"``, ``"day1"``, ...).

    v2b: ``points`` = max(0, ``photo_points`` + ``landmark_points`` + ``show_points``
    - ``loss_fine_points``), where ``loss_fine_points`` = ``scoring.loss_fine`` x ``lost_drones``
    (drones lost that day, any reason).

    scans : list[dict]
        ``{"t", "drone", "landmark", "pos", "banked", "reason", "seen"}``, sorted by (t, drone);
        reason None (counts), ``card_full``, ``void`` or ``noise``; ``seen`` the patch ids the scan
        sees (empty for ``card_full``).
    show_holds : list[dict]
        ``{"show", "slot", "drone", "t_arrive", "late", "t_hold_end", "pos", "held", "banked",
        "reason"}``; reason None (slot filled), ``late``, ``void``, ``same_drone``, ``slot_taken``.
    landmarks : dict
        landmark id -> ``{"c", "mapped", "total", "points", "mapped_patches", "views", "spread_deg",
        "rig_drones", "scans"}`` (``views``, ``spread_deg`` and ``rig_drones`` per patch, in file order;
        see :func:`skyline.scoring.landmark_detail`).
    shows : dict
        show id -> ``{"value", "slots", "filled", "points", "slot_drones", "failed"}``.
    noise : dict
        zone id -> ``{"budget_s", "usage" (12 hourly seconds), "hours_over", "max_usage"}``.
    noise_usage : dict
        zone id -> 12 hourly usage counts U(z, h).
    """

    def __init__(self, points, per_drone, photos, events, flightlog, day, extra=None):
        self.points = points
        self.per_drone = per_drone
        self.photos = photos
        self.events = events
        self.flightlog = flightlog
        self.day = day
        extra = extra or {}
        self.photo_points = extra.get("photo_points", points)
        self.landmark_points = extra.get("landmark_points", 0.0)
        self.show_points = extra.get("show_points", 0.0)
        self.scans = extra.get("scans", [])
        self.show_holds = extra.get("show_holds", [])
        self.landmarks = extra.get("landmarks", {})
        self.shows = extra.get("shows", {})
        self.noise = extra.get("noise", {})
        self.noise_usage = extra.get("noise_usage", {})
        self.lost_drones = extra.get("lost_drones", 0)
        self.loss_fine_points = extra.get("loss_fine_points", 0.0)

    def to_dict(self) -> dict:
        """Plain dict of the result (without the flight log). v2b fields follow the v2 ones."""
        return {"day": self.day, "points": self.points, "per_drone": self.per_drone,
                "photos": self.photos, "events": self.events,
                "photo_points": self.photo_points, "landmark_points": self.landmark_points,
                "show_points": self.show_points, "lost_drones": self.lost_drones,
                "loss_fine_points": self.loss_fine_points, "scans": self.scans, "show_holds": self.show_holds,
                "landmarks": _json_keys(self.landmarks), "shows": _json_keys(self.shows), "noise": self.noise}

    def __repr__(self):
        lost = sum(1 for v in self.per_drone.values() if v["lost"])
        return f"SimResult(day={self.day!r}, points={self.points:.3f}, lost={lost})"


def _json_keys(d):
    """Copy of a per-landmark/per-show dict with int keys (failed slots) as strings, for JSON."""
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            v = {kk: ({str(a): b for a, b in vv.items()} if isinstance(vv, dict) else vv) for kk, vv in v.items()}
        out[k] = v
    return out


def _round(obj, nd=6):
    if isinstance(obj, float):
        v = round(obj, nd)
        return 0.0 if v == 0.0 else v  # no "-0.0"
    if isinstance(obj, dict):
        return {k: _round(v, nd) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round(v, nd) for v in obj]
    return obj


def to_json(obj, indent=None) -> str:
    """Deterministic JSON text: floats rounded to 6 decimals, keys in insertion order."""
    return json.dumps(_round(obj), indent=indent, allow_nan=False, separators=(",", ":") if indent is None else None)


def write_json(obj, path: str, indent=None) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(to_json(obj, indent))
        f.write("\n")


def build_flightlog(sim, photos, events, points, day, extra=None) -> dict:
    """Flight log: per drone, samples every 5 s plus at its event times.

    ``photos`` are the result's photo dicts, so each carries ``light``,
    ``quality`` and ``synced`` (added after the v1 fields; nothing else changed).

    Sample = ``[t, x, y, z, soc, state]`` with soc a battery fraction in [0, 1]
    and state ``"air"``, ``"pad"`` or ``"lost"``. A lost drone's samples stop at
    its loss time (the last sample has state ``"lost"``).
    """
    B = sim.B
    day_end = sim.day_end
    ev_times: dict = {}
    for e in events:
        ev_times.setdefault(e["drone"], []).append(e["t"])
    drones_out = []
    for d in sim.drones:
        t_end = d.lost_t if d.lost_t is not None else day_end
        times = set(float(k) for k in range(0, int(t_end) + 1, LOG_INTERVAL_S))
        times.update(ev_times.get(d.id, []))
        times.add(float(t_end))
        smp = d.samples
        ts = [s[0] for s in smp]
        out = []
        for t in sorted(times):
            if t > t_end:
                continue
            i = bisect_right(ts, t) - 1
            if i < 0:
                i = 0
            s0 = smp[i]
            if i + 1 < len(smp) and smp[i + 1][0] > s0[0] and t > s0[0]:
                s1 = smp[i + 1]
                f = (t - s0[0]) / (s1[0] - s0[0])
                x = s0[1] + (s1[1] - s0[1]) * f
                y = s0[2] + (s1[2] - s0[2]) * f
                z = s0[3] + (s1[3] - s0[3]) * f
                E = s0[4] + (s1[4] - s0[4]) * f
            else:
                x, y, z, E = s0[1], s0[2], s0[3], s0[4]
            soc = min(1.0, max(0.0, E / B))
            out.append([t, x, y, z, soc, s0[5]])
        drones_out.append({"id": d.id, "samples": out})
    out = {"day": day, "t_end": day_end, "drones": drones_out, "photos": photos,
           "events": events, "points": points}
    if extra:
        # v2b, additive only: the viewer reads the keys above and ignores these.
        out["photo_points"] = extra["photo_points"]
        out["landmark_points"] = extra["landmark_points"]
        out["show_points"] = extra["show_points"]
        out["lost_drones"] = extra["lost_drones"]
        out["loss_fine_points"] = extra["loss_fine_points"]
        out["scans"] = extra["scans"]
        out["show_holds"] = extra["show_holds"]
        out["sorties"] = {d.id: [list(c) for c in d.sorties] for d in sim.drones}
        out["landmarks"] = {k: {kk: v[kk] for kk in ("c", "mapped", "total", "points", "scans")}
                            for k, v in extra["landmarks"].items()}
        out["shows"] = _json_keys(extra["shows"])
        out["noise"] = extra["noise"]
    return out
