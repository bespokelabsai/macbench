"""World and wind loaders.

``load_world(data_dir)`` reads ``rules.json``, ``city.json``, ``targets.json``,
``pads.json``, ``nofly.json``, ``sun.json`` and, when the rules have the
v2b ``mapping``/``shows`` blocks, ``landmarks.json``, ``noise.json`` and
``shows.json`` (formats in docs/MANUAL.md
section 3) and returns a :class:`World`. ``load_wind(path)`` reads one wind day (see ``wind.py``).

All positions are in the local metric frame: x east, y north, z up, metres.
Times are seconds from 06:00 local.
"""

from __future__ import annotations

import json
import math
import os
from bisect import bisect_right
from dataclasses import dataclass, field

import numpy as np
import shapely

from .geometry import BuildingIndex
from .rules import Rules, load_rules
from .wind import Wind, load_wind  # re-exported

__all__ = ["World", "Building", "Target", "Pad", "NoFlyZone", "Sun", "Landmark", "NoiseZone", "Show",
           "load_world", "load_sun", "load_wind", "Wind"]


@dataclass
class Building:
    """A vertical prism from z = 0 to ``height`` over ``footprint`` (CCW polygon)."""

    id: int
    footprint: np.ndarray  # shape (n, 2)
    height: float
    polygon: shapely.Polygon = field(repr=False)


@dataclass
class Target:
    """A photo target.

    Static targets (roof, facade, bridge) have ``pos`` and a unit ``normal``.
    Moving targets (train, boat) have a piecewise-linear ``track`` of
    ``(t, x, y, z)`` rows and exist only for ``t_first <= t <= t_last``.
    """

    id: str
    kind: str
    value: float
    moving: bool
    pos: tuple[float, float, float] | None = None
    normal: tuple[float, float, float] | None = None
    building_id: int | None = None
    track: list[tuple[float, float, float, float]] | None = field(default=None, repr=False)
    _track_t: list[float] | None = field(default=None, repr=False)
    #: (Sun, light_min, light_max), set by load_world when the rules have a light factor.
    _light: tuple | None = field(default=None, repr=False, compare=False)

    @property
    def t_first(self) -> float | None:
        return self.track[0][0] if self.moving else None

    @property
    def t_last(self) -> float | None:
        return self.track[-1][0] if self.moving else None

    def position_at(self, t: float | None = None) -> tuple[float, float, float] | None:
        """Target position at time ``t``.

        Static targets ignore ``t``. Moving targets are linearly interpolated
        along their track and return ``None`` when ``t`` is outside
        ``[t_first, t_last]`` (or when ``t`` is None).
        """
        if not self.moving:
            return self.pos
        if t is None:
            return None
        tr, ts = self.track, self._track_t
        if t < ts[0] or t > ts[-1]:
            return None
        i = bisect_right(ts, t) - 1
        if i >= len(ts) - 1:
            return (tr[-1][1], tr[-1][2], tr[-1][3])
        t0, x0, y0, z0 = tr[i]
        t1, x1, y1, z1 = tr[i + 1]
        if t1 <= t0:
            return (x1, y1, z1)
        f = (t - t0) / (t1 - t0)
        return (x0 + (x1 - x0) * f, y0 + (y1 - y0) * f, z0 + (z1 - z0) * f)


class Sun:
    """Unit vectors toward the sun from ``sun.json``.

    ``vectors[k]`` is the direction at t = t0_s + k * step_s (x east, y north,
    z up; z < 0 when the sun is below the horizon).
    """

    def __init__(self, doc: dict):
        self.t0 = float(doc["t0_s"])
        self.step = float(doc["step_s"])
        self.vectors = [tuple(float(c) for c in v) for v in doc["vectors"]]
        if len(self.vectors) < 2 or self.step <= 0.0:
            raise ValueError("sun.json: need at least 2 vectors and step_s > 0")
        for v in self.vectors:
            if len(v) != 3:
                raise ValueError("sun.json: every vector needs 3 components")
        self.date = doc.get("date")

    def at(self, t: float) -> tuple[float, float, float]:
        """s(t): the unit vector toward the sun at time t.

        With k = floor((t - t0) / step) (clamped to the table, so times outside
        it use the first or last interval's end) and f = (t - t0 - k * step) / step
        clamped to [0, 1], the raw vector is v[k] + f * (v[k+1] - v[k]),
        component by component, and s(t) is that vector normalised.
        """
        n = len(self.vectors)
        x = (float(t) - self.t0) / self.step
        k = math.floor(x)
        if k < 0:
            k = 0
        elif k > n - 2:
            k = n - 2
        f = (float(t) - self.t0 - k * self.step) / self.step
        f = 0.0 if f < 0.0 else (1.0 if f > 1.0 else f)
        a, b = self.vectors[k], self.vectors[k + 1]
        v0 = a[0] + f * (b[0] - a[0])
        v1 = a[1] + f * (b[1] - a[1])
        v2 = a[2] + f * (b[2] - a[2])
        m = math.sqrt(v0 * v0 + v1 * v1 + v2 * v2)
        if m == 0.0:
            raise ValueError(f"sun.json: zero sun vector at t = {t}")
        return (v0 / m, v1 / m, v2 / m)


def load_sun(path: str) -> Sun:
    """Load ``sun.json`` from a file path or from a directory containing it."""
    if os.path.isdir(path):
        path = os.path.join(path, "sun.json")
    with open(path, encoding="utf-8") as f:
        return Sun(json.load(f))


@dataclass
class Pad:
    """A landing pad on the ground."""

    id: str
    pos: tuple[float, float, float]


@dataclass
class NoFlyZone:
    """A no-fly polygon, active at all altitudes during any ``[t0, t1]`` interval.

    A point on the polygon boundary counts as inside.
    """

    id: str
    name: str
    polygon_xy: np.ndarray
    active: list[tuple[float, float]]
    polygon: shapely.Polygon = field(repr=False)
    bounds: tuple[float, float, float, float] = field(repr=False, default=(0, 0, 0, 0))

    def active_at(self, t: float) -> bool:
        for t0, t1 in self.active:
            if t0 <= t <= t1:
                return True
        return False

    def contains_xy(self, x: float, y: float) -> bool:
        """True if (x, y) is inside the polygon or on its boundary."""
        b = self.bounds
        if x < b[0] or x > b[2] or y < b[1] or y > b[3]:
            return False
        return bool(shapely.intersects_xy(self.polygon, x, y))


class Landmark:
    """A landmark to map (``landmarks.json``).

    ``patch_ids`` (list of str), ``C`` (n x 3 patch centres), ``N`` (n x 3 unit
    normals, normalised on load) and ``area`` (n,) are in file order;
    ``total_area`` is their sum. ``centroid`` is the footprint centroid at
    half the building height, as written in the file.
    """

    def __init__(self, d: dict):
        self.id = str(d["id"])
        self.name = str(d["name"])
        self.building_id = int(d["building_id"])
        self.value = float(d["value"])
        c = [float(v) for v in d["centroid"]]
        if len(c) != 3:
            raise ValueError(f"landmarks.json: {self.id}: centroid needs 3 numbers")
        self.centroid = tuple(c)
        patches = d["patches"]
        if not patches:
            raise ValueError(f"landmarks.json: {self.id}: no patches")
        self.patch_ids = [str(p["id"]) for p in patches]
        if len(set(self.patch_ids)) != len(self.patch_ids):
            raise ValueError(f"landmarks.json: {self.id}: duplicate patch ids")
        self.C = np.array([[float(v) for v in p["c"]] for p in patches], dtype=np.float64).reshape(-1, 3)
        N = np.array([[float(v) for v in p["n"]] for p in patches], dtype=np.float64).reshape(-1, 3)
        nn = np.sqrt((N * N).sum(axis=1))
        if (nn <= 0.0).any() or len(N) != len(patches) or len(self.C) != len(patches):
            raise ValueError(f"landmarks.json: {self.id}: every patch needs c [x,y,z] and a non-zero n [nx,ny,nz]")
        self.N = N / nn[:, None]
        self.area = np.array([float(p["area"]) for p in patches], dtype=np.float64)
        if (self.area < 0.0).any() or not np.isfinite(self.area).all():
            raise ValueError(f"landmarks.json: {self.id}: patch areas must be finite and >= 0")
        self.total_area = float(self.area.sum())
        if self.total_area <= 0.0:
            raise ValueError(f"landmarks.json: {self.id}: total patch area must be > 0")
        self.index_of = {pid: i for i, pid in enumerate(self.patch_ids)}

    def __repr__(self):
        return f"Landmark({self.id!r}, building {self.building_id}, {len(self.patch_ids)} patches)"


@dataclass
class NoiseZone:
    """A quiet zone with a noise budget (``noise.json``). Boundary counts as inside."""

    id: str
    name: str
    polygon_xy: np.ndarray
    budget_s: float
    polygon: shapely.Polygon = field(repr=False)
    bounds: tuple[float, float, float, float] = field(repr=False, default=(0, 0, 0, 0))

    def contains_xy(self, x: float, y: float) -> bool:
        """True if (x, y) is inside the polygon or on its boundary."""
        b = self.bounds
        if x < b[0] or x > b[2] or y < b[1] or y > b[3]:
            return False
        return bool(shapely.intersects_xy(self.polygon, x, y))


@dataclass
class Show:
    """A light show (``shows.json``); ``slots`` are (x, y, z), counted from 0."""

    id: str
    name: str
    t0: float
    hold_s: float
    value: float
    slots: list


@dataclass
class World:
    """Everything static about the task: rules, city, targets, pads, no-fly zones.

    Attributes
    ----------
    rules : Rules
    name, origin, bbox : city metadata; ``bbox = (xmin, ymin, xmax, ymax)``
    buildings : list[Building], in file order
    building_by_id : dict[int, Building]
    targets : dict[str, Target], in file order
    pads : dict[str, Pad], in file order
    start : dict[str, str], drone id -> start pad id
    nofly : list[NoFlyZone]
    index : BuildingIndex, uniform-grid spatial index used by ``geometry``
    sun : Sun or None, the sun table (None when the rules have no light factor)
    landmarks : dict[str, Landmark], in file order (empty without the v2b rules)
    noise_alt_m : float or None, the noise ceiling from noise.json
    noise_zones : list[NoiseZone] (empty without the v2b rules)
    shows : dict[str, Show], in file order (empty without the v2b rules)
    """

    rules: Rules
    name: str
    origin: dict
    bbox: tuple[float, float, float, float]
    buildings: list[Building]
    building_by_id: dict
    targets: dict
    pads: dict
    start: dict
    nofly: list
    index: BuildingIndex = field(repr=False)
    data_dir: str | None = None
    sun: Sun | None = field(default=None, repr=False)
    landmarks: dict = field(default_factory=dict, repr=False)
    noise_alt_m: float | None = None
    noise_zones: list = field(default_factory=list, repr=False)
    shows: dict = field(default_factory=dict, repr=False)

    def noise_zones_at(self, x: float, y: float, z: float) -> list:
        """The noise zones (in file order) that count a drone at (x, y, z): inside the polygon
        (boundary included) and z strictly below ``noise_alt_m``."""
        if self.noise_alt_m is None or not z < self.noise_alt_m:
            return []
        return [nz for nz in self.noise_zones if nz.contains_xy(x, y)]

    def in_pad_zone(self, x: float, y: float) -> bool:
        """True if (x, y) is within ``pad_zone_radius_m`` (inclusive) of any pad."""
        r2 = self.rules.pad_zone_radius_m ** 2
        for p in self._pad_xy:
            dx, dy = x - p[0], y - p[1]
            if dx * dx + dy * dy <= r2:
                return True
        return False

    def __post_init__(self):
        self._pad_xy = [(p.pos[0], p.pos[1]) for p in self.pads.values()]


def _read_json(data_dir: str, name: str):
    with open(os.path.join(data_dir, name), encoding="utf-8") as f:
        return json.load(f)


def _parse_target(d: dict) -> Target:
    kind = d["kind"]
    if "track" in d and d["track"] is not None:
        track = [tuple(float(v) for v in row) for row in d["track"]]
        if not track:
            raise ValueError(f"target {d['id']}: empty track")
        return Target(id=str(d["id"]), kind=kind, value=float(d["value"]), moving=True,
                      track=track, _track_t=[r[0] for r in track])
    bid = d.get("building_id")
    return Target(id=str(d["id"]), kind=kind, value=float(d["value"]), moving=False,
                  pos=tuple(float(v) for v in d["pos"]),
                  normal=tuple(float(v) for v in d["normal"]),
                  building_id=None if bid is None else int(bid))


def load_world(data_dir: str) -> World:
    """Load the world from ``data_dir`` (rules, city, targets, pads, nofly JSON files)."""
    rules: Rules = load_rules(os.path.join(data_dir, "rules.json"))
    city = _read_json(data_dir, "city.json")
    buildings = []
    for b in city["buildings"]:
        fp = np.asarray(b["footprint"], dtype=np.float64).reshape(-1, 2)
        buildings.append(Building(id=int(b["id"]), footprint=fp, height=float(b["height"]),
                                  polygon=shapely.Polygon(fp)))
    building_by_id = {b.id: b for b in buildings}
    if len(building_by_id) != len(buildings):
        raise ValueError("city.json: duplicate building ids")

    targets = {}
    for d in _read_json(data_dir, "targets.json"):
        t = _parse_target(d)
        if t.id in targets:
            raise ValueError(f"targets.json: duplicate target id {t.id}")
        targets[t.id] = t

    pads_json = _read_json(data_dir, "pads.json")
    pads = {str(p["id"]): Pad(id=str(p["id"]), pos=tuple(float(v) for v in p["pos"]))
            for p in pads_json["pads"]}
    start = {str(k): str(v) for k, v in pads_json["start"].items()}
    for drone in rules.fleet:
        if start.get(drone) not in pads:
            raise ValueError(f"pads.json: drone {drone} has no valid start pad")

    nofly = []
    nofly_path = os.path.join(data_dir, "nofly.json")
    if os.path.exists(nofly_path):
        for z in _read_json(data_dir, "nofly.json"):
            xy = np.asarray(z["polygon"], dtype=np.float64).reshape(-1, 2)
            poly = shapely.Polygon(xy)
            shapely.prepare(poly)
            nofly.append(NoFlyZone(id=str(z["id"]), name=str(z.get("name", z["id"])), polygon_xy=xy,
                                   active=[(float(a), float(b)) for a, b in z["active"]],
                                   polygon=poly, bounds=tuple(float(v) for v in poly.bounds)))

    sun = None
    if rules.light_min is not None:
        sun_path = os.path.join(data_dir, "sun.json")
        if not os.path.exists(sun_path):
            raise FileNotFoundError(f"rules.json has photo.light but {sun_path} is missing")
        sun = load_sun(sun_path)
        for t in targets.values():
            if not t.moving:
                t._light = (sun, rules.light_min, rules.light_max)

    landmarks, noise_alt, zones, shows = {}, None, [], {}
    if rules.v2b:
        for name in ("landmarks.json", "noise.json", "shows.json"):
            if not os.path.exists(os.path.join(data_dir, name)):
                raise FileNotFoundError(f"rules.json has mapping/shows but {os.path.join(data_dir, name)} is missing")
        for d in _read_json(data_dir, "landmarks.json")["landmarks"]:
            lm = Landmark(d)
            if lm.id in landmarks:
                raise ValueError(f"landmarks.json: duplicate landmark id {lm.id}")
            if lm.building_id not in building_by_id:
                raise ValueError(f"landmarks.json: {lm.id}: unknown building_id {lm.building_id}")
            landmarks[lm.id] = lm
        nd = _read_json(data_dir, "noise.json")
        noise_alt = float(nd["alt_m"])
        for z in nd["zones"]:
            xy = np.asarray(z["polygon"], dtype=np.float64).reshape(-1, 2)
            poly = shapely.Polygon(xy)
            shapely.prepare(poly)
            nz = NoiseZone(id=str(z["id"]), name=str(z["name"]), polygon_xy=xy, budget_s=float(z["budget_s"]),
                           polygon=poly, bounds=tuple(float(v) for v in poly.bounds))
            if any(o.id == nz.id for o in zones):
                raise ValueError(f"noise.json: duplicate zone id {nz.id}")
            zones.append(nz)
        for d in _read_json(data_dir, "shows.json")["shows"]:
            slots = [tuple(float(v) for v in s) for s in d["slots"]]
            if not slots or any(len(s) != 3 for s in slots):
                raise ValueError(f"shows.json: {d['id']}: slots must be a non-empty list of [x, y, z]")
            sh = Show(id=str(d["id"]), name=str(d["name"]), t0=float(d["t0"]), hold_s=float(d["hold_s"]),
                      value=float(d["value"]), slots=slots)
            if sh.id in shows:
                raise ValueError(f"shows.json: duplicate show id {sh.id}")
            if ":" in sh.id:
                raise ValueError(f"shows.json: show id {sh.id!r} may not contain ':'")
            shows[sh.id] = sh

    index = BuildingIndex(buildings, rules.building_clearance_m)
    return World(rules=rules, name=city.get("name", ""), origin=city.get("origin", {}),
                 bbox=tuple(float(v) for v in city["bbox"]), buildings=buildings,
                 building_by_id=building_by_id, targets=targets, pads=pads, start=start,
                 nofly=nofly, index=index, data_dir=os.path.abspath(data_dir), sun=sun,
                 landmarks=landmarks, noise_alt_m=noise_alt, noise_zones=zones, shows=shows)
