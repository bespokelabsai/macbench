"""Relative navigation sensors and ground tracking of other crafts (ported sim.py
``_relnav`` and ``_ground_track_error``). Pure functions: the caller passes its own random stream."""
from __future__ import annotations

import math

import numpy as np

from . import data as D


def _unit(v):
    v = np.asarray(v, float)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def sensor_sigmas(rho, off_axis_deg):
    """(sensor, one sigma of range m, range rate m/s, angle deg) for a target at range rho and off_axis_deg from the
    body axis, or None out of reach. Within 150 m and inside 20 deg of the body axis, the laser docking sensor
    (data.LASER); else, out to 400 nmi, the rendezvous radar (data.RADAR). One sigma is a third of each published
    accuracy."""
    las, rad, k = D.LASER, D.RADAR, D.SENSOR_SIGMA
    if las["range_m"][0] <= rho <= las["range_m"][1] and off_axis_deg <= las["fov_deg"]:
        return "laser", las["range_acc_m"] / k, las["rate_mps"] / k, las["angle_deg"] / k
    if rad["range_m"][0] <= rho <= rad["range_m"][1]:
        near = rho <= rad["far_from_m"]
        s_r = (max(rad["range_frac"] * rho, rad["range_floor_m"]) if near else rad["range_far_m"]) / k
        a0, a1 = rad["angle_deg"]
        f = 0.0 if near else math.log(rho / rad["far_from_m"]) / math.log(rad["range_m"][1] / rad["far_from_m"])
        return "radar", s_r, rad["rate_mps"] / k, (a0 + (a1 - a0) * f) / k
    return None


def relnav(x_obs, x_tgt, axis, rng, nav_scale=1.0):
    """Range, range rate and line of sight from one craft (state x_obs) to another (x_tgt), as its relative
    navigation sensors measure them, or None out of their reach. axis is the observer's body axis (None: the
    laser is not pointed). Every measurement gets its own error, scaled by nav_scale (0: perfect)."""
    rel = np.asarray(x_tgt, float) - np.asarray(x_obs, float)
    rho = float(np.linalg.norm(rel[:3]))
    if rho <= 0:
        return None
    u = rel[:3] / rho
    rdot = float(np.dot(rel[3:], u))
    off_axis = 180.0 if axis is None else math.degrees(math.acos(float(np.clip(np.dot(_unit(axis), u), -1, 1))))
    s = sensor_sigmas(rho, off_axis)
    if s is None:
        return None
    sensor, s_r, s_v, s_a = s
    if nav_scale:
        rho_m = rho + rng.normal(0, nav_scale * s_r)
        rdot_m = rdot + rng.normal(0, nav_scale * s_v)
        e = rng.normal(0, nav_scale * math.radians(s_a), 3)
        u_m = _unit(u + e - np.dot(e, u) * u)
    else:
        rho_m, rdot_m, u_m = rho, rdot, u
    return {"sensor": sensor, "range": rho_m, "range_rate": rdot_m, "los": u_m.tolist()}


class GroundTracking:
    """The error of another craft's state from ground tracking, one sigma 200 m and 2 cm/s per axis, redrawn each
    day per (observer, target) pair."""

    def __init__(self, rng, nav_scale=1.0):
        self.rng = rng
        self.scale = nav_scale
        self._bias = {}

    def error(self, observer, target, t):
        key = (observer, target)
        b = self._bias.get(key)
        if b is None or t - b[0] > D.GROUND_TRACK_HOLD_S:
            sc = self.scale
            b = (t, self.rng.normal(0, sc * D.GROUND_TRACK_POS_M, 3) if sc else np.zeros(3),
                 self.rng.normal(0, sc * D.GROUND_TRACK_VEL_MPS, 3) if sc else np.zeros(3))
            self._bias[key] = b
        return b[1], b[2]
