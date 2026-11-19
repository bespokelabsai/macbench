"""Skyline Survey checker.

Typical use::

    from skyline import load_world, load_wind, load_plans, simulate, points_to_score

    world = load_world("data")
    wind = load_wind("data/wind_forecast.json")
    plans, errors = load_plans("plans", world)   # errors: drone id -> message
    res = simulate(world, plans, wind, log=True)
    res.points, res.per_drone, res.photos, res.events, res.flightlog

``skyline.scoring.light(target, t)`` gives a static target's sun-light factor and
``skyline.scoring.quality(world, target, D, t)`` a photo's quality factor Q.
v2b helpers: ``scan_sees(world, D, landmark)``,
``landmark_points(world, landmark_id, scans)``, ``landmark_detail(...)`` and
``noise_usage(world, res_or_samples)``; ``res.photo_points``,
``res.landmark_points``, ``res.show_points``, ``res.scans``, ``res.show_holds``,
``res.landmarks``, ``res.shows``, ``res.noise`` and ``res.noise_usage``.
Geometry helpers for planning live in :mod:`skyline.geometry`
(``segment_clear``, ``los_clear``, ``viewpoint_ok``, ``viewpoint_reason``).
The simulation rules, including how each rule is applied, are
documented at the top of :mod:`skyline.sim`.

Command line: ``python -m skyline check --help``.
"""

from __future__ import annotations

from .plan import Row, format_plan, load_plans, parse_plan_text
from .result import SimResult, build_flightlog, to_json, write_json
from .scoring import (landmark_detail, landmark_points, light, noise_usage, points_to_score, scan_sees,
                      score_day, score_photos)
from .sim import Simulator
from .wind import Wind
from .world import World, load_wind, load_world

__all__ = [
    "load_world", "load_wind", "load_plans", "simulate", "points_to_score", "light",
    "scan_sees", "landmark_points", "landmark_detail", "noise_usage",
    "Row", "SimResult", "World", "Wind", "parse_plan_text", "format_plan", "to_json", "write_json",
]


def simulate(world, plans, wind, log: bool = True) -> SimResult:
    """Fly all plans for one day on ``wind`` and score the result.

    ``plans`` maps drone id -> list of :class:`Row` (missing drones get an empty
    plan and stay on their start pad). Deterministic: the same inputs give the
    same result, and ``to_json`` of it is byte-identical.
    """
    sim = Simulator(world, plans, wind, log=log).run()
    photos = sorted(sim.photos, key=lambda p: (p["t"], world.rules.fleet.index(p["drone"])))
    scans = sorted(sim.scans, key=lambda p: (p["t"], world.rules.fleet.index(p["drone"])))
    for sc in scans:
        sc["seen"] = scan_sees(world, sc["pos"], sc["landmark"]) if sc["reason"] != "card_full" else []
    holds = sorted(sim.holds, key=lambda h: (h["t_arrive"], world.rules.fleet.index(h["drone"])))
    day_score = score_day(world, photos, scans, holds, sim.noise_usage)
    # a fine per drone lost (any reason), the day floored at 0
    lost_drones = sum(1 for d in sim.drones if d.lost_reason is not None)
    fine = (world.rules.loss_fine or 0.0) * lost_drones
    points = max(0.0, day_score["points"] - fine)
    per_drone = {}
    for d in sim.drones:
        pts = sum((p["value"] for p in photos if p["drone"] == d.id), 0.0)
        per_drone[d.id] = {
            "lost": d.lost_reason is not None,
            "reason": d.lost_reason,
            "t_lost": d.lost_t,
            "photos_taken": d.photos_taken,
            "photos_banked": d.photos_banked,
            "points": pts,
            "energy_J": d.energy,
            "rows": len(d.rows),
            "scans_taken": d.scans_taken,
            "scans_banked": d.scans_banked,
            "sorties": [list(c) for c in d.sorties],
        }
    day = getattr(wind, "label", "") or "day"
    extra = {k: day_score[k] for k in ("photo_points", "landmark_points", "show_points", "landmarks", "shows", "noise")}
    extra.update(lost_drones=lost_drones, loss_fine_points=fine)
    extra.update(scans=scans, show_holds=holds, noise_usage=sim.noise_usage)
    flightlog = build_flightlog(sim, photos, sim.events, points, day, extra) if log else None
    return SimResult(points, per_drone, photos, sim.events, flightlog, day, extra)
