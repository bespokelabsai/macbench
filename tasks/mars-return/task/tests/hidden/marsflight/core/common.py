"""End-to-end mission simulator: Earth launch, Mars landing, surface stay, Mars ascent, Earth return.

The team's flight software (guidance) is called at the start of every arc. It sees navigation
estimates (with realistic errors), not the true state, and returns engine, attitude and event
commands plus how long until it wants to be called again. Each arc is propagated by Tudat with
piecewise-constant commands. After every arc the simulator checks what real hardware would suffer:
structural loads, heating and thermal protection, parachute envelopes, nozzle flow separation,
propellant, batteries, power, boiloff and freezing. A failure ends the flight.

Moved from sim.py: module-level names shared by the core and the models.
"""
from __future__ import annotations

import math
import os
import time
import zlib
from dataclasses import dataclass, field

import numpy as np

from .. import data as D
from .. import vibro
from .. import terrain
from ..environment import EARTH_SOI, MARS_SOI, MARS_REF_RADIUS, Dispersions, World

MILESTONES = [
    ("design_valid", 0.017, False),
    ("liftoff", 0.017, False),
    ("max_q_passed", 0.025, False),
    ("earth_orbit", 0.042, False),
    ("earth_departure", 0.042, False),
    ("mars_arrival", 0.051, False),
    ("mars_entry", 0.068, False),
    ("mars_descent", 0.042, False),
    ("mars_touchdown", 0.084, False),
    ("mars_target", 0.12, True),       # True: optional, does not block later milestones
    ("samples_loaded", 0.042, False),
    ("mars_orbit", 0.068, False),
    ("mars_departure", 0.051, False),
    ("earth_arrival", 0.051, False),
    ("earth_entry", 0.076, False),
    ("earth_touchdown", 0.084, False),
    ("earth_target", 0.12, True),
]
MILESTONE_WEIGHTS = {k: w for k, w, _ in MILESTONES}
# Landing precision: each landing target earns target_credit(miss) of its weight, from the landing's distance to the
# site, so only a landing close to the site earns it in full (half at the scale). The milestones mars_target and
# earth_target still mark a landing inside the site's radius_km (data.SITES); the score uses the distance.
TARGET_GATES = {"mars_target": ("mars_touchdown", "Mars"), "earth_target": ("earth_touchdown", "Earth")}
TARGET_SCALE_KM = {"mars_target": 1.0, "earth_target": 5.0}
# The sample cache at Jezero: a lander that comes down farther than this from the site cannot reach the samples,
# and the mission ends at touchdown.
SAMPLE_RANGE_KM = 10.0


def target_credit(miss_km, scale_km):
    """Share of a landing target's weight a landing miss_km from the site earns: 1 / (1 + (miss / scale)^2)."""
    return 1.0 / (1.0 + (float(miss_km) / scale_km) ** 2)


ATM_TOP = {"Earth": 150e3, "Mars": 130e3}
# Calls to the flight software in one flight. Flown missions take 650 to 1,600 and the most seen in any saved run
# that finished is 9,545; flight software stuck in a loop (asking again for what the vehicle refuses, or for tiny
# arcs) made 360,000 to 380,000 in 1,500 s. Past the cap the flight fails where it is, as at the wall-clock
# limit, but at the same point on any machine and without using up the verifier's time for the other worlds.
MAX_GUIDANCE_CALLS = 20000
EARTH_R_EQ = 6378137.0
PARTIAL_APPROACH_D0 = 1.495978707e11   # m: an approach no closer than this earns no partial credit
# Scenario starts (development only): the phase a saved state starts in, and the last milestone
# reached before it. `marsflight fly --save-states DIR` writes one file per phase.
SCENARIO_PHASES = {
    "earth_orbit": "earth_orbit",
    "cruise_to_mars": "earth_departure",
    "mars_approach": "mars_arrival",
    "mars_surface": "mars_touchdown",
    "mars_orbit": "mars_orbit",
    "cruise_to_earth": "mars_departure",
    "earth_approach": "earth_arrival",
}
STATE_AFTER = {v: k for k, v in SCENARIO_PHASES.items()}


class FlightFailure(Exception):
    pass


def interp(table, x):
    if x <= table[0][0]:
        return table[0][1]
    for (a, fa), (b, fb) in zip(table, table[1:]):
        if a <= x <= b:
            return fa + (fb - fa) * (x - a) / (b - a)
    return table[-1][1]


def unit(v):
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def rotate_towards(a, b, max_angle):
    """Rotate unit vector a towards b by at most max_angle (rad)."""
    a, b = unit(a), unit(b)
    c = float(np.clip(np.dot(a, b), -1.0, 1.0))
    ang = math.acos(c)
    if ang <= max_angle or ang < 1e-9:
        return b
    axis = np.cross(a, b)
    if np.linalg.norm(axis) < 1e-12:
        axis = np.cross(a, [1.0, 0.0, 0.0])
        if np.linalg.norm(axis) < 1e-12:
            axis = np.cross(a, [0.0, 1.0, 0.0])
    axis = unit(axis)
    th = max_angle
    return a * math.cos(th) + np.cross(axis, a) * math.sin(th) + axis * np.dot(axis, a) * (1 - math.cos(th))


def newtonian_cd(half_angle_deg, rn, Rb):
    th = math.radians(half_angle_deg)
    s2, c2 = math.sin(th) ** 2, math.cos(th) ** 2
    k = min(1.0, (rn / Rb)) ** 2
    return 1.85 * ((1 - s2 * s2) * k / 2 + s2 * (1 - k * c2))


def parachute_peak_load(chute_type, diameter, q, kp=None):
    """Peak opening load (N) of a parachute at full inflation: F = 2 kP q S_proj (Way 2018, data.CHUTE_KP)."""
    kp = D.CHUTE_KP["mean"] if kp is None else kp
    return 2.0 * kp * q * D.PARACHUTES[chute_type]["proj_area_ratio"] * math.pi * (diameter / 2) ** 2


def clamp01(f):
    """f clamped to [0, 1], with a non-finite or non-numeric f as 0 (min(1.0, nan) would be 1.0)."""
    try:
        f = float(f)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, f)) if math.isfinite(f) else 0.0


def great_circle_km(lat1, lon1, lat2, lon2, R):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    c = math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(dl)
    return R * math.acos(max(-1.0, min(1.0, c))) / 1e3


@dataclass
class ElementState:
    el: object
    dry: float
    fuel: float
    ox: float
    rcs: float
    battery_Wh: float
    starts_used: dict = field(default_factory=dict)
    engine_starts: dict = field(default_factory=dict)  # engine name -> starts used by each installed engine
    burn_s: dict = field(default_factory=dict)
    lit: dict = field(default_factory=dict)          # engine name -> time thrust begins
    running: dict = field(default_factory=dict)      # engine name -> what it was last commanded
    last_cold: dict = field(default_factory=dict)    # engine name -> when it was last cold, its last cutoff

    @property
    def mass(self):
        return self.dry + self.fuel + self.ox + self.rcs
