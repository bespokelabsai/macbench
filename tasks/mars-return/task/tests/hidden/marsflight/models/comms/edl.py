"""Who hears the vehicle during Mars entry, descent and landing.

Since Mars Polar Lander (1999) NASA requires EDL data to reach the ground, either direct to Earth (X-band MFSK
tones, which pass through the plasma) or through an orbiter (UHF, which the plasma of peak heating cuts for
25 to 100 s; MSL telecom, DESCANSO 14, Table 1-6). Here each sample of the entry is covered:

- direct to Earth when Earth is above the vehicle's horizon, a DSN complex sees Mars above 6 deg, and the
  Sun-Earth-probe angle is above the X-band limit (2.3 deg);
- by relay when an orbiter is above the vehicle's horizon, within range, the UHF link at the relay rate closes
  with margin (``link.uhf_relay``), and the vehicle is not in the UHF plasma blackout.

An entry with no coverage at all reports ``entry_data_lost``. Orbiters are given as ``Orbiter`` objects (a
two-body orbit about Mars from a state), so any orbiter the team leaves at Mars can relay.
"""
from __future__ import annotations

import math

import numpy as np

from . import dsn
from .link import table, uhf_relay

GM_MARS = 4.282837e13


def kepler_propagate(r0, v0, dt, mu=GM_MARS):
    """Two-body propagation by universal variables (Vallado, Algorithm 8), r0 and v0 in m and m/s."""
    r0, v0 = np.asarray(r0, float), np.asarray(v0, float)
    rn, vn = np.linalg.norm(r0), np.linalg.norm(v0)
    alpha = 2.0 / rn - vn * vn / mu
    sq = math.sqrt(mu)
    x = sq * dt * alpha if alpha > 1e-12 else sq * dt / rn
    rv = float(np.dot(r0, v0))
    for _ in range(60):
        z = alpha * x * x
        if z > 1e-6:
            s = math.sqrt(z)
            c2, c3 = (1 - math.cos(s)) / z, (s - math.sin(s)) / (s ** 3)
        elif z < -1e-6:
            s = math.sqrt(-z)
            c2, c3 = (1 - math.cosh(s)) / z, (math.sinh(s) - s) / (s ** 3)
        else:
            c2, c3 = 0.5, 1.0 / 6.0
        r = x * x * c2 + rv / sq * x * (1 - z * c3) + rn * (1 - z * c2)
        f_ = sq * dt - x ** 3 * c3 - rv / sq * x * x * c2 - rn * x * (1 - z * c3)
        dx = f_ / r
        x += dx
        if abs(dx) < 1e-9:
            break
    z = alpha * x * x
    if z > 1e-6:
        s = math.sqrt(z)
        c2, c3 = (1 - math.cos(s)) / z, (s - math.sin(s)) / (s ** 3)
    elif z < -1e-6:
        s = math.sqrt(-z)
        c2, c3 = (1 - math.cosh(s)) / z, (math.sinh(s) - s) / (s ** 3)
    else:
        c2, c3 = 0.5, 1.0 / 6.0
    f = 1 - x * x / rn * c2
    g = dt - x ** 3 / sq * c3
    r1 = f * r0 + g * v0
    return r1


class Orbiter:
    """A relay orbiter on a two-body orbit about Mars, from its J2000 state at ``t0`` (m, m/s, from Mars)."""

    def __init__(self, name, r0, v0, t0, uhf=None):
        self.name, self.r0, self.v0, self.t0 = name, np.asarray(r0, float), np.asarray(v0, float), float(t0)
        self.uhf = uhf or {}

    @classmethod
    def circular(cls, name, alt_m, inc_deg, raan_deg, u0_deg, t0, R=3389500.0):
        a = R + alt_m
        v = math.sqrt(GM_MARS / a)
        i, O, u = (math.radians(x) for x in (inc_deg, raan_deg, u0_deg))
        # position and velocity in the J2000 equator (an approximation of Mars's own equator is enough here)
        p = np.array([math.cos(u), math.sin(u), 0.0])
        q = np.array([-math.sin(u), math.cos(u), 0.0])
        Rx = np.array([[1, 0, 0], [0, math.cos(i), -math.sin(i)], [0, math.sin(i), math.cos(i)]])
        Rz = np.array([[math.cos(O), -math.sin(O), 0], [math.sin(O), math.cos(O), 0], [0, 0, 1]])
        M = Rz @ Rx
        return cls(name, a * (M @ p), v * (M @ q), t0)

    def pos(self, t):
        return kepler_propagate(self.r0, self.v0, float(t) - self.t0)


def above(r_from, r_to, min_deg=0.0):
    """Elevation (deg) of ``r_to`` seen from ``r_from`` on a sphere (local up = radial)."""
    d = np.asarray(r_to) - np.asarray(r_from)
    up = np.asarray(r_from) / np.linalg.norm(r_from)
    return math.degrees(math.asin(float(np.dot(d, up)) / float(np.linalg.norm(d))))


def in_uhf_blackout(airspeed, alt):
    b = table("hardware")["edl"]["uhf_plasma_blackout"]
    return airspeed > b["speed_ms"] and b["alt_min_m"] <= alt <= b["alt_max_m"]


def coverage(samples, orbiters=(), rate_bps=None):
    """Coverage of EDL samples ``[(t, r_mars_J2000, airspeed, alt), ...]``. Returns per-sample flags and totals."""
    e = table("hardware")["edl"]
    rate = rate_bps or e["relay_rate_bps"]
    rows = []
    for t, r, vair, alt in samples:
        r = np.asarray(r, float)
        earth = -dsn.body_position("Mars", "Earth", t)            # Earth from Mars
        earth_el = above(r, r + earth)
        re = r - earth                                             # vehicle from Earth
        R = dsn.rotation("IAU_Earth", "J2000", t)
        dsn_el = max(dsn.elevation(c, re, t, R) for c in dsn.COMPLEXES)
        sep = dsn.sep_deg(re, t)
        dte = (earth_el >= e["dte_min_earth_elevation_deg"] and dsn_el >= 6.0
               and sep >= table("dsn")["solar_corona"]["X"]["telemetry_min_sep_deg"])
        blackout = in_uhf_blackout(vair, alt)
        relay = []
        for o in orbiters:
            p = o.pos(t)
            rng = float(np.linalg.norm(p - r))
            if rng > e["relay_max_range_m"] or above(r, p) < 0.0:
                continue
            if blackout:
                continue
            if uhf_relay(rng, **o.uhf).margin_dB(rate, code="uhf_conv_7_1/2", mod_index_deg=60.0) >= 0.0:
                relay.append(o.name)
        rows.append({"t": float(t), "dte": bool(dte), "relay": relay, "uhf_blackout": blackout,
                     "earth_elevation_deg": earth_el, "dsn_elevation_deg": dsn_el, "sep_deg": sep})
    n = max(1, len(rows))
    dte_f = sum(r["dte"] for r in rows) / n
    rel_f = sum(bool(r["relay"]) for r in rows) / n
    any_f = sum(r["dte"] or bool(r["relay"]) for r in rows) / n
    bl = [r["t"] for r in rows if r["uhf_blackout"]]
    return {"samples": rows, "dte_fraction": dte_f, "relay_fraction": rel_f, "covered_fraction": any_f,
            "uhf_blackout_s": (max(bl) - min(bl)) if bl else 0.0, "lost": any_f == 0.0}
