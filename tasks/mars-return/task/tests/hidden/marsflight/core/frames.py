"""Reference frames, positions over the planets and orbit elements.

Methods of Mission, moved verbatim from sim.py. Mission inherits them from FramesMixin."""
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
from ..core.common import (  # noqa: F401
    MILESTONES, MILESTONE_WEIGHTS, ATM_TOP, EARTH_R_EQ, PARTIAL_APPROACH_D0, SCENARIO_PHASES, STATE_AFTER, FlightFailure, interp, unit, rotate_towards, newtonian_cd, parachute_peak_load, great_circle_km, ElementState,
)


# Earth's shape: WGS84 (the same ellipsoid Tudat's Earth shape uses, environment.World). Sites, maps and everything
# reported to the team give Earth latitudes as geodetic (map) latitudes and heights above this ellipsoid.
EARTH_F = 1 / 298.257223563
EARTH_E2 = EARTH_F * (2.0 - EARTH_F)


def geodetic_to_fixed(lat_deg, lon_deg, h):
    """Earth-fixed position (m) of geodetic latitude and longitude (deg) and height h (m) above the WGS84
    ellipsoid: the standard closed form, the position on the ellipsoid moved h along its normal."""
    la, lo = math.radians(lat_deg), math.radians(lon_deg)
    s = math.sin(la)
    n = EARTH_R_EQ / math.sqrt(1.0 - EARTH_E2 * s * s)
    return np.array([(n + h) * math.cos(la) * math.cos(lo), (n + h) * math.cos(la) * math.sin(lo),
                     (n * (1.0 - EARTH_E2) + h) * s])


def fixed_to_geodetic(rf):
    """Geodetic latitude and longitude (deg) and height above the WGS84 ellipsoid (m) of an Earth-fixed position.
    Fixed-point iteration from Bowring's start; five passes converge to well under a millimetre from the ground to
    beyond the Moon. The height uses the form that stays exact at the poles."""
    x, y, z = (float(c) for c in rf)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1.0 - EARTH_E2))
    h = 0.0
    for _ in range(5):
        s, c = math.sin(lat), math.cos(lat)
        n = EARTH_R_EQ / math.sqrt(1.0 - EARTH_E2 * s * s)
        h = p * c + (z + EARTH_E2 * n * s) * s - n
        lat = math.atan2(z, p * (1.0 - EARTH_E2 * n / (n + h)))
    return math.degrees(lat), math.degrees(math.atan2(y, x)), h


class FramesMixin:
    # ------------------------------------------------------------------ geometry helpers
    def central_state(self, body, t):
        return np.array(self.bodies.get(body).ephemeris.cartesian_state(t))

    def switch_central(self, new):
        if new == self.central:
            return
        rel = self.central_state(self.central, self.t) - self.central_state(new, self.t)
        self.x = self.x + rel
        self.event("central_body", frm=self.central, to=new)
        self.central = new

    def body_fixed(self, r, t, body=None):
        body = body or self.central
        R = np.array(self.bodies.get(body).rotation_model.inertial_to_body_fixed_rotation(t))
        return R @ r

    def latlon(self, r, t):
        """Planetocentric latitude and longitude (deg) and the body-fixed position. Internal geometry only (the
        frame Tudat evaluates winds in); at Earth, what is reported or compared with a site is geodetic
        (map_latlon)."""
        rf = self.body_fixed(r, t)
        lat = math.degrees(math.asin(rf[2] / np.linalg.norm(rf)))
        lon = math.degrees(math.atan2(rf[1], rf[0]))
        return lat, lon, rf

    def altitude(self, r, t):
        """Height above the central body's shape: above the WGS84 ellipsoid at Earth (the height Tudat's oblate
        Earth shape and its atmosphere use), above the reference sphere at Mars."""
        if self.central == "Mars":
            return float(np.linalg.norm(r)) - MARS_REF_RADIUS
        if self.central == "Earth":
            return fixed_to_geodetic(self.body_fixed(r, t))[2]
        return float("inf")

    def surface_alt(self):
        if self.central == "Mars":
            return D.SITES["mars_landing"]["alt"]
        if self.central == "Earth":
            return D.SITES["earth_landing"]["alt"] if "mars_departure" in self.milestones else 0.0
        return -1e30

    def omega_cross(self, r):
        w = {"Earth": 7.2921159e-5, "Mars": 7.088218e-5}.get(self.central, 0.0)
        R = np.array(self.bodies.get(self.central).rotation_model.inertial_to_body_fixed_rotation(self.t)) if w else np.eye(3)
        axis = R.T @ np.array([0, 0, 1.0])
        return np.cross(w * axis, r)

    def gm(self, body):
        return self.bodies.get(body).gravitational_parameter

    def orbit_elements(self):
        r, v = self.x[:3], self.x[3:]
        mu = self.gm(self.central)
        rn, vn = np.linalg.norm(r), np.linalg.norm(v)
        energy = vn * vn / 2 - mu / rn
        h = np.cross(r, v)
        e_vec = np.cross(v, h) / mu - r / rn
        e = float(np.linalg.norm(e_vec))
        if abs(energy) < 1e-12:
            return energy, e, rn
        a = -mu / (2 * energy)
        rp = a * (1 - e) if energy < 0 else float(np.linalg.norm(h)) ** 2 / mu / (1 + e)
        return energy, e, rp

    def _sun_distance_au(self):
        if self.x is None:
            r = self.central_state(self.central, self.t)[:3] - self.central_state("Sun", self.t)[:3]
        elif self.central == "Sun":
            r = self.x[:3]
        else:
            r = self.x[:3] + self.central_state(self.central, self.t)[:3] - self.central_state("Sun", self.t)[:3]
        return max(0.3, float(np.linalg.norm(r)) / D.AU)
