"""Rule constants, read from ``rules.json``.

Every rule constant used by the checker comes from this file; nothing is
hard-coded elsewhere. :class:`Rules` exposes the values as flat attributes
(``rules.alt_min_m``, ``rules.battery_J``, ...) and keeps the parsed JSON in
``rules.raw``.

Photo rules, all in the ``photo`` block:

* ``light = {"min", "max"}`` -> ``light_min``, ``light_max``: the sun-light factor
  of static photos (see :func:`skyline.scoring.light`; needs ``sun.json``).
* ``moving_sync_window_s``, ``moving_sync_min_angle_deg``: a moving photo
  scores only when synced with another drone's photo (see
  :func:`skyline.scoring.score_photos`).
* ``repeat_multipliers_moving``: multipliers for accepted moving photos
  (static photos use ``repeat_multipliers``).

All four keys are required: a rules file without any of them is an error
(ValueError).

Photo quality: ``photo.quality = {"far_factor", "edge_factor"}`` ->
``quality_far_factor``, ``quality_edge_factor``, the factors of the photo
quality Q at the far end of ``range_m`` and on the edge of the static cone (see
:func:`skyline.scoring.quality`). Optional: without the block both are None and
Q = 1.0. With it, both keys are required, each a finite number in [0, 1]
(ValueError otherwise).

Minimum time on the pad: ``pads.min_turnaround_s`` ->
``min_turnaround_s``; after every landing the next takeoff is at least that long
after the landing. Required (ValueError).

Memory card: ``drone.card_capacity`` -> ``card_capacity``,
the most captures (photos and scans) one sortie records. Required (ValueError).

Mapping and show blocks: ``mapping`` (landmark scans),
``shows`` (light shows) and ``scoring`` (``loss_fine``, points fined per lost
drone). All three are required, with every key (ValueError otherwise; there is
no fallback), and ``load_world`` requires ``landmarks.json``, ``noise.json``
and ``shows.json``. Flat attributes: ``scan_hover_s``,
``scan_range_m``, ``scan_fov_half_deg``, ``max_incidence_deg``,
``mapping_min_views``, ``mapping_min_spread_deg``, ``mapping_bonus`` (tuple of
(threshold, bonus) pairs), ``mapping_min_drones``, ``mapping_sync_window_s``,
``mapping_coverage_exponent``, ``show_slot_tol_m`` and ``loss_fine``; the flag
``v2b`` (mapping and shows present) is always True.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Rules:
    """Flat, read-only view of ``rules.json``."""

    day_length_s: float
    time_step_s: float
    fleet: tuple[str, ...]
    # airspace
    alt_min_m: float
    alt_max_m: float
    building_clearance_m: float
    separation_m: float
    pad_zone_radius_m: float
    # drone
    airspeed_max_mps: float
    airspeed_min_mps: float
    climb_rate_max_mps: float
    descent_rate_max_mps: float
    battery_J: float
    power_params: dict
    climb_efficiency: float
    card_capacity: int
    # pads
    slots_per_pad: int
    charge_time_full_s: float
    min_turnaround_s: float
    # photo
    photo_hover_s: float
    photo_range_m: tuple[float, float]
    static_cone_deg: float
    moving_min_elevation_deg: float
    repeat_min_angle_deg: float
    repeat_multipliers: tuple[float, ...]
    # plans
    max_rows_per_drone: int
    # photo rules (None or defaults when absent from rules.json)
    light_min: float | None = None
    light_max: float | None = None
    moving_sync_window_s: float | None = None
    moving_sync_min_angle_deg: float | None = None
    repeat_multipliers_moving: tuple[float, ...] = ()
    # photo quality Q (None when rules.json has no photo.quality: Q = 1.0)
    quality_far_factor: float | None = None
    quality_edge_factor: float | None = None
    # mapping and shows: None when the rules have no mapping/shows blocks
    v2b: bool = False
    scan_hover_s: float | None = None
    scan_range_m: tuple[float, float] | None = None
    scan_fov_half_deg: float | None = None
    max_incidence_deg: float | None = None
    mapping_min_views: int | None = None
    mapping_min_spread_deg: float | None = None
    mapping_bonus: tuple[tuple[float, float], ...] = ()
    mapping_min_drones: int | None = None
    mapping_sync_window_s: float | None = None
    mapping_coverage_exponent: float | None = None
    show_slot_tol_m: float | None = None
    # points fined per drone lost in the day
    loss_fine: float | None = None
    raw: dict = field(default=None, repr=False, compare=False)

    @property
    def charge_rate_Jps(self) -> float:
        """Battery charge rate on a pad, in J/s (full charge in ``charge_time_full_s``)."""
        return self.battery_J / self.charge_time_full_s


def rules_from_dict(d: dict) -> Rules:
    """Build :class:`Rules` from the parsed ``rules.json`` dictionary."""
    air, drone, pads, photo = d["airspace"], d["drone"], d["pads"], d["photo"]
    if drone.get("power_model", "rotary_wing_zeng2019") != "rotary_wing_zeng2019":
        raise ValueError(f"unsupported power model {drone['power_model']!r}")
    missing = [k for k in ("light", "moving_sync_window_s", "moving_sync_min_angle_deg",
                           "repeat_multipliers_moving") if k not in photo]
    if missing:
        raise ValueError("rules.json: photo block is missing " + ", ".join(missing))
    if "card_capacity" not in drone:
        raise ValueError("rules.json: drone block is missing card_capacity")
    cap = drone["card_capacity"]
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 0:
        raise ValueError("rules.json: drone.card_capacity must be an integer >= 0")
    light = photo["light"]
    lmin, lmax = float(light["min"]), float(light["max"])
    has_win = has_ang = True
    mults = tuple(float(m) for m in photo["repeat_multipliers"])
    mults_moving = tuple(float(m) for m in photo["repeat_multipliers_moving"])
    v2b = _v2b_fields(d)
    q_far, q_edge = _quality(photo)
    return Rules(
        day_length_s=float(d["day_length_s"]),
        time_step_s=float(d["time_step_s"]),
        fleet=tuple(d["fleet"]),
        alt_min_m=float(air["alt_min_m"]),
        alt_max_m=float(air["alt_max_m"]),
        building_clearance_m=float(air["building_clearance_m"]),
        separation_m=float(air["separation_m"]),
        pad_zone_radius_m=float(air["pad_zone_radius_m"]),
        airspeed_max_mps=float(drone["airspeed_max_mps"]),
        airspeed_min_mps=float(drone["airspeed_min_mps"]),
        climb_rate_max_mps=float(drone["climb_rate_max_mps"]),
        descent_rate_max_mps=float(drone["descent_rate_max_mps"]),
        battery_J=float(drone["battery_J"]),
        power_params={k: float(v) for k, v in drone["power_params"].items()},
        climb_efficiency=float(drone["climb_efficiency"]),
        card_capacity=int(cap),
        slots_per_pad=int(pads["slots_per_pad"]),
        charge_time_full_s=float(pads["charge_time_full_s"]),
        min_turnaround_s=_turnaround(pads),
        photo_hover_s=float(photo["hover_s"]),
        photo_range_m=(float(photo["range_m"][0]), float(photo["range_m"][1])),
        static_cone_deg=float(photo["static_cone_deg"]),
        moving_min_elevation_deg=float(photo["moving_min_elevation_deg"]),
        repeat_min_angle_deg=float(photo["repeat_min_angle_deg"]),
        repeat_multipliers=mults,
        max_rows_per_drone=int(d["plan_limits"]["max_rows_per_drone"]),
        light_min=lmin,
        light_max=lmax,
        moving_sync_window_s=float(photo["moving_sync_window_s"]) if has_win else None,
        moving_sync_min_angle_deg=float(photo["moving_sync_min_angle_deg"]) if has_ang else None,
        repeat_multipliers_moving=mults_moving,
        quality_far_factor=q_far,
        quality_edge_factor=q_edge,
        **v2b,
        raw=d,
    )


MAPPING_KEYS = ("scan_hover_s", "scan_range_m", "scan_fov_half_deg", "max_incidence_deg", "min_views",
                "min_spread_deg", "bonus", "min_drones", "sync_window_s", "coverage_exponent")
SHOWS_KEYS = ("slot_tol_m",)
SCORING_KEYS = ("loss_fine",)


def _v2b_fields(d: dict) -> dict:
    """The mapping and show fields of :class:`Rules`. Every block is required."""
    missing = [b for b in ("mapping", "shows", "scoring") if b not in d]
    if missing:
        raise ValueError("rules.json: block missing: " + ", ".join(missing) + " (all are required)")
    m, s = d["mapping"], d["shows"]
    miss = ([f"mapping.{k}" for k in MAPPING_KEYS if k not in m] + [f"shows.{k}" for k in SHOWS_KEYS if k not in s]
            + [f"scoring.{k}" for k in SCORING_KEYS if k not in d["scoring"]])
    if miss:
        raise ValueError("rules.json: missing " + ", ".join(miss))
    rng = tuple(float(v) for v in m["scan_range_m"])
    if len(rng) != 2 or not rng[0] <= rng[1]:
        raise ValueError("rules.json: mapping.scan_range_m must be [min, max]")
    bonus = tuple((float(t), float(b)) for t, b in m["bonus"])
    md = m["min_drones"]
    if isinstance(md, bool) or not isinstance(md, int) or md < 1:
        raise ValueError("rules.json: mapping.min_drones must be an integer >= 1")
    win, expo, fine = float(m["sync_window_s"]), float(m["coverage_exponent"]), float(d["scoring"]["loss_fine"])
    if not (win >= 0.0 and expo > 0.0 and fine >= 0.0) or not all(map(math.isfinite, (win, expo, fine))):
        raise ValueError("rules.json: mapping.sync_window_s >= 0, mapping.coverage_exponent > 0 and "
                         "scoring.loss_fine >= 0 must be finite numbers")
    return dict(
        v2b=True,
        scan_hover_s=float(m["scan_hover_s"]),
        scan_range_m=rng,
        scan_fov_half_deg=float(m["scan_fov_half_deg"]),
        max_incidence_deg=float(m["max_incidence_deg"]),
        mapping_min_views=int(m["min_views"]),
        mapping_min_spread_deg=float(m["min_spread_deg"]),
        mapping_bonus=bonus,
        mapping_min_drones=md,
        mapping_sync_window_s=win,
        mapping_coverage_exponent=expo,
        show_slot_tol_m=float(s["slot_tol_m"]),
        loss_fine=fine,
    )


def _quality(photo: dict) -> tuple:
    """``photo.quality``: (far_factor, edge_factor), or (None, None) when the block is absent."""
    if "quality" not in photo:
        return None, None
    q = photo["quality"]
    if not isinstance(q, dict):
        raise ValueError("rules.json: photo.quality must be an object")
    out = []
    for k in ("far_factor", "edge_factor"):
        if k not in q:
            raise ValueError(f"rules.json: photo.quality is missing {k}")
        v = q[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0.0 <= v <= 1.0:
            raise ValueError(f"rules.json: photo.quality.{k} must be a finite number in [0, 1]")
        out.append(float(v))
    return out[0], out[1]


def _turnaround(pads: dict) -> float:
    """``pads.min_turnaround_s``: required, a finite number >= 0."""
    if "min_turnaround_s" not in pads:
        raise ValueError("rules.json: pads block is missing min_turnaround_s")
    v = pads["min_turnaround_s"]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
        raise ValueError("rules.json: pads.min_turnaround_s must be a finite number >= 0")
    return float(v)


def load_rules(path: str) -> Rules:
    """Load ``rules.json`` from a file path or from a directory containing it."""
    if os.path.isdir(path):
        path = os.path.join(path, "rules.json")
    with open(path, encoding="utf-8") as f:
        return rules_from_dict(json.load(f))
