"""Attitude sensors and air data for the 6-DOF mode (sixdof.py): what the flight software and the onboard
autopilot know, instead of the true attitude, body rates, Mach number and dynamic pressure.

- Gyros: the LN-200 family of fiber optic IMUs (the LN-200S is its space version): bias repeatability 1.0 deg/h
  and angle random walk 0.07 deg/sqrt(h), each per axis (NovAtel, "LN-200 IMU performance"). The bias is drawn
  once per flight; nothing calibrates it.
- Star tracker: two Rocket Lab ST-16RT2 heads at right angles. Each is 5 arcsec across its boresight and 55 arcsec
  about it (RMS), so the pair fixes every axis to about 5 arcsec. It works up to 3 deg/s (ST-16RT2 data sheet,
  version 3.1, 2024), and it is blind inside the atmosphere (fairing, heatshield, daylight sky).
- Air data: from the navigated position and velocity, the planet's rotation and an onboard atmosphere, which is the
  world's own model with the day's dispersions taken out: the nominal density (Earth: NRLMSISE-00 with the day's
  space weather; Mars: the mean profile and the seasonal cycle, no dust storm) and the nominal winds.

Every error is zero in the nominal world (seed None), like the navigation errors in sim.py.
"""
from __future__ import annotations

import math

import numpy as np

from ...environment import MARS_REF_RADIUS, Dispersions, World, dust_density_factor

GYRO_BIAS_DEG_H = 1.0          # 1 sigma per axis, bias repeatability
GYRO_ARW_DEG_RTH = 0.07        # angle random walk
TRACKER_ARCSEC = 5.0           # 1 sigma per axis, two heads
TRACKER_MAX_RATE_DEG_S = 3.0
SPIN = {"Earth": 7.2921159e-5, "Mars": 7.088218e-5}
EARTH_A, EARTH_F = 6378137.0, 1 / 298.257223563


def rotation(e):
    """The rotation matrix of a rotation vector e (rad)."""
    th = float(np.linalg.norm(e))
    if th < 1e-15:
        return np.eye(3)
    k = np.asarray(e, float) / th
    K = np.array([[0.0, -k[2], k[1]], [k[2], 0.0, -k[0]], [-k[1], k[0], 0.0]])
    return np.eye(3) + math.sin(th) * K + (1 - math.cos(th)) * (K @ K)


class AttitudeSensors:
    """The onboard attitude estimate. Its error is a small rotation e in body axes: estimate = truth x rotation(e).
    Over each arc e moves in a straight line between its values at the start and the end of the arc, so Tudat's
    callbacks can ask for it at any moment of the arc. In an arc the star tracker can see through, e is a fresh
    draw at the tracker's accuracy. In any other arc it grows with the gyro bias and angle random walk."""

    def __init__(self, seed, scale):
        self.rng = np.random.default_rng([int(seed), 2108, 8]) if seed is not None else None
        self.scale = float(scale) if seed is not None else 0.0
        self.bias = self._draw(math.radians(GYRO_BIAS_DEG_H) / 3600.0)
        self.err = np.zeros(3)
        self.arc = None
        self.tracker = True

    def _draw(self, sigma):
        if not self.scale:
            return np.zeros(3)
        return self.scale * self.rng.normal(0.0, sigma, 3)

    def align(self):
        """Before liftoff the estimate is aligned to the launch site's reference, as well as the star tracker
        would."""
        self.err, self.arc = self._draw(math.radians(TRACKER_ARCSEC / 3600.0)), None

    def begin(self, t0, dt, tracker):
        if tracker:
            e0 = e1 = self._draw(math.radians(TRACKER_ARCSEC / 3600.0))
        else:
            e0 = self.err
            e1 = e0 + self.bias * dt + self._draw(math.radians(GYRO_ARW_DEG_RTH) / 60.0 * math.sqrt(max(dt, 0.0)))
        self.arc = (t0, t0 + dt, e0, e1)

    def error(self, t):
        if self.arc is None:
            return self.err
        t0, t1, e0, e1 = self.arc
        f = min(1.0, max(0.0, (t - t0) / (t1 - t0))) if t1 > t0 else 1.0
        return e0 + f * (e1 - e0)

    def end(self, t):
        self.err, self.arc = self.error(t), None

    def estimate(self, R, t):
        """The estimated body-to-inertial rotation, from the true one."""
        e = self.error(t)
        return R if not e.any() else R @ rotation(e)

    def rates(self, w):
        """What the gyros read, from the true body rates (rad/s)."""
        return np.ravel(w) + self.bias

    def tracker_sees(self, in_atm, w):
        """Whether the star tracker fixes the attitude in an arc that starts with body rates w."""
        return self.tracker and not in_atm and float(np.linalg.norm(w)) < math.radians(TRACKER_MAX_RATE_DEG_S)


class _NominalWorld:
    """Stands in for World in its wind functions: the winds of the nominal world, which the onboard model assumes."""
    disp = Dispersions.draw(None, [])
    _mars_alt_km = 0.0


def _geodetic(rf):
    """Geodetic latitude (rad) and height above the Earth's ellipsoid of a body-fixed position."""
    e2 = EARTH_F * (2 - EARTH_F)
    x, y, z = (float(v) for v in rf)
    p = math.hypot(x, y)
    lat, h = math.atan2(z, p * (1 - e2)), 0.0
    for _ in range(5):
        n = EARTH_A / math.sqrt(1 - e2 * math.sin(lat) ** 2)
        h = p / math.cos(lat) - n
        lat = math.atan2(z, p * (1 - e2 * n / (n + h)))
    return lat, h


def air_data(m, r, v):
    """Mach number and dynamic pressure of a navigated position r and velocity v (J2000), with the onboard
    atmosphere: the world's model at that point, the day's density dispersion divided out, and the nominal winds.
    Evaluated the way Tudat evaluates the truth (height above the shape, geocentric latitude, winds north, east and
    down), so with no navigation error the nominal world gets the true values."""
    c, t = m.central, m.t
    Rbf = np.array(m.bodies.get(c).rotation_model.inertial_to_body_fixed_rotation(t))
    rf = Rbf @ np.asarray(r, float)
    vf = Rbf @ np.asarray(v, float) - np.cross([0.0, 0.0, SPIN[c]], rf)
    lat, lon = math.asin(rf[2] / np.linalg.norm(rf)), math.atan2(rf[1], rf[0])
    atm = m.bodies.get(c).atmosphere_model
    if c == "Earth":
        h = _geodetic(rf)[1]
        rho = atm.get_density(h, lon, lat, t) / m.disp.earth_density
        wind = World._earth_wind(_NominalWorld, h, lon, lat, t)
    else:
        h = float(np.linalg.norm(rf)) - MARS_REF_RADIUS
        m.world._mars_alt_km = h / 1e3   # the height the density scale's dust factor reads (World._mars_wind)
        rho = atm.get_density(h, lon, lat, t) / m.disp.mars_density_multiplier(h / 1e3)
        tau = m.disp.mars_tau(m.world.mars_sol(t))
        if tau > 0.5:
            rho /= dust_density_factor(tau, h / 1e3)
        wind = World._mars_wind(_NominalWorld, h, lon, lat, t)
    sl, cl, so, co = math.sin(lat), math.cos(lat), math.sin(lon), math.cos(lon)
    wn, we, wd = np.ravel(wind).tolist()
    wf = wn * np.array([-sl * co, -sl * so, cl]) + we * np.array([-so, co, 0.0]) - wd * np.array([cl * co, cl * so, sl])
    V = float(np.linalg.norm(vf - wf))
    return V / atm.get_speed_of_sound(h, lon, lat, t), 0.5 * rho * V * V
