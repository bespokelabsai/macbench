"""Propellant gauging, loading spread, low level cutoff and running dry.

Today ``sim.py:obs`` reports each tank's exact propellant, the loads have no spread, and a stage that burns to
depletion just stops. Real stages are loaded within about 0.3% of the plan, read their tanks with a gauge whose
error depends on its type, stop their engines on low level sensors, and carry a flight performance reserve so
that guidance, not depletion, ends the burn. All numbers and their sources are in
``data/propellant/gauging.json``.

What this model does, per stage tank (fuel, oxidizer) and per RCS supply:

- **Load spread** (hidden): the tank holds its planned load times a draw of normal(1, 0.27%), clipped at 3 sigma.
- **Gauge** (a design choice, field ``gauge`` on the stage): the reading the flight software gets in place of the
  true mass.
  - ``capacitance`` (default): a bias per tank of 1 sigma 0.05% + 0.25% x fill of the full load. Reads only with
    the liquid settled (axial acceleration of at least 0.01 m/s^2 for 15 s, or on the pad); otherwise it holds its
    last reading, so boiloff in a coast shows up only at the next burn. Blind below 3% of load.
  - ``point_sensors``: exact to 0.03% of load when the level passes a sensor, flowmeter integration (0.5% of the
    mass used) in between.
  - ``pvt``: helium pressure, volume and temperature. Works in zero g, but its error is large and grows as the tank
    empties: 1 sigma 1.0% to 1.8% of load (Shuttle RCS design analysis). RCS supplies always use it.
- **Low level cutoff** (field ``low_level_cutoff``, default true): the engines stop when either tank's usable
  propellant falls to 0.15% of its load. The core treats that as the stage's empty point.
- **Running dry** (only with ``low_level_cutoff: false``): what the pump does when its liquid runs out, by cycle.
  Staged combustion and full flow overspeed and come apart (SSME); a gas generator burns its turbine if fuel runs
  out first and stops cleanly if oxidizer runs out first (Atlas); expander and electric pumps stop but cannot
  restart; pressure-fed engines just stop.

The flight performance reserve is not a rule here: a team carries it by loading more than its burns need, as
Saturn V did (MSFC-MAN-503, 1% of the S-IVB load).

Hooks the core calls (see the PR for the exact places in today's sim.py):
- ``load_factor(eid, tank)``: multiply the loaded mass by it when the element states are built.
- ``cutoff_floor(eid)``: (fuel_kg, ox_kg) left when the low level sensors stop the engines; ``time_to_depletion``
  and the burnout check treat the tank as empty there.
- ``on_depletion(eid, tank, engines)``: called once when a lit stage reaches its empty point; returns what
  happened, and a failure message when the engine was destroyed.
- ``gauged(eid, fuel, ox, rcs, t, settled)``: the readings that replace ``fuel_kg``, ``ox_kg`` and ``rcs_kg`` in
  obs. ``obs(state)`` does the same for a whole state object (``state.t``, ``state.present()``,
  ``state.settled``).

Hidden values (load factors, gauge biases) never go into obs.
"""
from __future__ import annotations

import json
import math
import os

import numpy as np

from ...core.model import Model

_DATA = json.load(open(os.path.join(os.path.dirname(__file__), "..", "..", "data", "propellant", "gauging.json")))
GAUGES = _DATA["gauges"]
LOAD_SD = _DATA["load_spread_sd"]["value"]
LOAD_CLIP = _DATA["load_spread_sd"]["clip_sd"]
DEFAULT_GAUGE = _DATA["default_gauge"]["value"]
LLCO_DEFAULT = _DATA["low_level_cutoff"]["default"]
LLCO_TRIP = _DATA["low_level_cutoff"]["trip_fill"]
RUNNING_DRY = _DATA["running_dry"]
A_SETTLED = 0.01          # m/s^2: the slosh model's settled threshold (data/basilisk/slosh_sp106.json)
N_LEVEL_DRAWS = 16        # draws reserved per tank for point sensor readings, used or not


def _interp(x, xs, ys):
    """Linear interpolation on a table whose xs fall from 1 to 0."""
    pts = sorted(zip(xs, ys))
    if x <= pts[0][0]:
        return pts[0][1]
    for (a, fa), (b, fb) in zip(pts, pts[1:]):
        if x <= b:
            return fa + (fb - fa) * (x - a) / (b - a)
    return pts[-1][1]


def gauge_sd(kind, fill, tank="fuel"):
    """1 sigma gauge error as a fraction of the full load, at a fill fraction (published accuracies)."""
    g = GAUGES[kind]
    fill = min(max(fill, 0.0), 1.0)
    if kind == "capacitance":
        return g["sd_floor"] + g["sd_per_fill"] * fill
    if kind == "pvt":
        return _interp(fill, g["fill"], g["sd_ox" if tank == "ox" else "sd_fuel"])
    if kind == "point_sensors":
        return g["sd_at_level"]
    raise ValueError(f"unknown gauge {kind!r}")


class TankGauge:
    """One tank's hidden truth (load factor, gauge draws) and its gauge's memory."""

    def __init__(self, kind, load_kg, tank, draws):
        self.kind, self.load, self.tank = kind, float(load_kg), tank
        self.load_factor, self.z_bias, self.z_flow = draws[0], draws[1], draws[2]
        self.z_levels = draws[3:]
        self.reading = None           # last valid reading (kg)
        self.below = False
        self.anchor = None            # point sensors: (true kg, read kg) at the last sensor passed
        self.level_i = 0

    def error_kg(self, true_kg):
        fill = true_kg / self.load if self.load > 0 else 0.0
        return self.z_bias * gauge_sd(self.kind, fill, self.tank) * self.load

    def read(self, true_kg, settled):
        """The gauge reading for a true mass. Settled: liquid held at the tank bottom long enough to read."""
        if self.load <= 0:
            return 0.0
        g = GAUGES[self.kind]
        valid = settled or not g.get("needs_settling", False)
        if self.kind == "capacitance":
            if valid:
                bottom = g["probe_bottom_fill"] * self.load
                self.below = true_kg < bottom
                self.reading = max(bottom, true_kg + self.error_kg(true_kg)) if not self.below else bottom
        elif self.kind == "pvt":
            self.reading = max(0.0, true_kg + self.error_kg(true_kg))
        elif self.kind == "point_sensors":
            levels = g["levels"]
            if self.anchor is None:                       # loaded on the pad, in 1 g: the top sensor reads it
                self.anchor = (true_kg, true_kg + self.z_levels[0] * g["sd_at_level"] * self.load)
            if valid:
                while self.level_i < len(levels) and true_kg <= levels[self.level_i] * self.load:
                    k = min(self.level_i + 1, len(self.z_levels) - 1)
                    self.anchor = (true_kg, true_kg + self.z_levels[k] * g["sd_at_level"] * self.load)
                    self.level_i += 1
            used = self.anchor[0] - true_kg
            self.reading = max(0.0, self.anchor[1] - used * (1.0 + self.z_flow * g["sd_per_consumed"]))
        if self.reading is None:
            self.reading = max(0.0, true_kg + self.error_kg(true_kg))
        return self.reading


class PropellantGauging(Model):
    name = "propellant_gauging"
    area = "propellant"
    order = 40

    def __init__(self):
        self.tanks = {}        # (eid, "fuel" | "ox" | "rcs") -> TankGauge
        self.llco = {}         # eid -> bool
        self.cycles = {}       # eid -> [(engine name, cycle)]
        self.depleted = {}     # eid -> outcome dict
        self.burn_t0 = {}      # eid -> time the current burn started (for settling)

    def design_fields(self):
        return ["gauge", "low_level_cutoff"]

    def configure(self, ctx):
        spec = {str(e.get("id")): e for e in (ctx.design or {}).get("elements") or [] if isinstance(e, dict)}
        nominal = ctx.seed is None
        for el in ctx.vehicle.elements:
            s = spec.get(el.id, {})
            kind = s.get("gauge", DEFAULT_GAUGE)
            if kind not in GAUGES:
                raise ValueError(f"element {el.id}: gauge must be one of {sorted(GAUGES)}")
            self.llco[el.id] = bool(s.get("low_level_cutoff", LLCO_DEFAULT))
            self.cycles[el.id] = [(en.name, getattr(en, "cycle", "gas_generator")) for en, _ in (el.engines or [])]
            loaded = getattr(el, "loaded", {}) or {}
            for tank, load in (("fuel", loaded.get("fuel", 0.0)), ("ox", loaded.get("ox", 0.0)),
                               ("rcs", getattr(el, "rcs_prop", 0.0) or 0.0)):
                if load <= 0:
                    continue
                # a stream per tank (named after the model, the element and the tank), so a change to one
                # element never moves another tank's draws
                z = ctx.rng(f"{self.name}:{el.id}:{tank}").standard_normal(3 + N_LEVEL_DRAWS)
                lf = 1.0 + LOAD_SD * float(np.clip(z[0], -LOAD_CLIP, LOAD_CLIP))
                draws = [1.0 if (nominal or tank == "rcs") else lf] + [0.0 if nominal else float(v) for v in z[1:]]
                self.tanks[(el.id, tank)] = TankGauge("pvt" if tank == "rcs" else kind, load, tank, draws)

    # ---------------------------------------------------------------- hidden truth for the core
    def load_factor(self, eid, tank):
        g = self.tanks.get((eid, tank))
        return g.load_factor if g else 1.0

    def cutoff_floor(self, eid):
        """Propellant (fuel_kg, ox_kg) still in the tanks when the low level sensors stop the engines."""
        if not self.llco.get(eid, LLCO_DEFAULT):
            return 0.0, 0.0
        return tuple(LLCO_TRIP * (self.tanks[(eid, t)].load if (eid, t) in self.tanks else 0.0) for t in ("fuel", "ox"))

    def on_depletion(self, eid, tank):
        """A lit stage reached its empty point; ``tank`` ("fuel" or "ox") is the one that ran out. Returns
        {"cutoff": "low_level" | "depletion", "restart": bool, "failure": message or None, "engines": {...}}."""
        if eid in self.depleted:
            return self.depleted[eid]
        out = {"cutoff": "low_level" if self.llco.get(eid, LLCO_DEFAULT) else "depletion", "restart": True,
               "failure": None, "engines": {}}
        if out["cutoff"] == "depletion":
            for name, cycle in self.cycles.get(eid, []):
                rule = RUNNING_DRY.get(cycle, RUNNING_DRY["gas_generator"])
                what = rule.get("outcome") or rule["ox_first" if tank == "ox" else "fuel_first"]
                out["engines"][name] = what
                out["restart"] = out["restart"] and rule.get("restart", False) and what != "destroyed"
                if what == "destroyed" and out["failure"] is None:
                    why = {"gas_generator": "the gas generator ran oxidizer-rich when the fuel ran out and burned "
                                            "the turbine"}.get(cycle, "its turbopump ran dry, unloaded and oversped")
                    out["failure"] = (f"engine {name} on {eid}: the {'oxidizer' if tank == 'ox' else 'fuel'} ran out "
                                      f"with the engine running and {why}; the stage had no low level cutoff sensors")
        self.depleted[eid] = out
        return out

    # ---------------------------------------------------------------- what the flight software sees
    def settled(self, eid, t, axial_accel, thrusting, on_pad=False):
        """Liquid held at the tank bottom for long enough to read: on the pad, or 15 s into a burn with at least
        0.01 m/s^2 of axial acceleration."""
        if on_pad:
            return True
        if not thrusting or axial_accel < A_SETTLED:
            self.burn_t0.pop(eid, None)
            return False
        t0 = self.burn_t0.setdefault(eid, t)
        return t - t0 >= GAUGES["capacitance"]["settle_s"]

    def gauged(self, eid, fuel, ox, rcs, settled):
        """Readings for one element: {"fuel_kg", "ox_kg", "rcs_kg", "gauge", ...} for the keys it has."""
        out = {}
        for tank, true_kg, key in (("fuel", fuel, "fuel_kg"), ("ox", ox, "ox_kg"), ("rcs", rcs, "rcs_kg")):
            g = self.tanks.get((eid, tank))
            if g is None:
                continue
            out[key] = round(g.read(true_kg, settled), 3)
            if g.kind == "capacitance" and g.below:
                out[tank + "_below_gauge"] = True
        if (eid, "fuel") in self.tanks:
            out["gauge"] = self.tanks[(eid, "fuel")].kind
            out["low_level_cutoff"] = self.llco.get(eid, LLCO_DEFAULT)
        return out

    def obs(self, state):
        """For a core state with ``t``, ``present()`` (element states with ``el``, ``fuel``, ``ox``, ``rcs``,
        ``lit``) and ``settled_by_element`` (eid -> bool): readings by element id."""
        settled = getattr(state, "settled_by_element", {}) or {}
        return {"gauged": {s.el.id: self.gauged(s.el.id, s.fuel, s.ox, getattr(s, "rcs", 0.0), settled.get(s.el.id, False))
                           for s in state.present()}}


MODELS = [PropellantGauging]
