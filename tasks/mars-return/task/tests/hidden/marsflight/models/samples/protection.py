"""Planetary protection for the return of Mars samples: Restricted Earth Return, "break the chain".

Two rules, from MSR's plans (data/samples/msr.json):

1. Earth avoidance. Anything carrying Mars material stays on a trajectory that misses Earth until about a week
   before closest approach; only then does it move onto an Earth-intercept trajectory, and about three days
   before arrival the carrier releases the entry capsule and diverts (Cataldo et al. 2024, the ESA Earth Return
   Orbiter's planetary protection strategy). Here: after Mars liftoff, a trajectory whose predicted closest
   approach to Earth is lower than the entry interface (120 km) more than INTERCEPT_WINDOW_S before that closest
   approach is refused.
   The prediction is a two-body conic: Earth-centred inside Earth's sphere of influence; outside it, a
   Sun-centred conic flown forward until it passes Earth (Earth from the ephemeris the core passes in), whose
   miss distance and speed relative to Earth give the hyperbolic perigee.

2. Containment assurance. MSR required the chance that containment is not assured to be below one in a million
   (Corliss et al. 2019; ESA's ERO standard). Its entry vehicle had no parachute so that no parachute failure
   could breach the sample container, and was built so the container sees less than 3,500 g at impact
   (containment) and 2,500 g (science) (Mitcheltree et al. 2001). Here: at Earth touchdown the container's
   impact deceleration must stay below 3,500 g, else containment is lost (a flight failure); above 2,500 g the
   samples' science is lost. And a capsule that lands on a parachute must still keep containment if the
   parachute fails: its container must survive the impact at the capsule's own terminal speed.
"""
from __future__ import annotations

import json
import math
import os

import numpy as np

from ... import data as D
from ...core.model import Model

_DATA = json.load(open(os.path.join(os.path.dirname(__file__), "..", "..", "data", "samples", "msr.json")))
_EA = _DATA["earth_avoidance"]
_C = _DATA["containment"]
INTERCEPT_WINDOW_S = _EA["intercept_window_days"] * 86400.0
ENTRY_INTERFACE_M = _EA["entry_interface_km"] * 1e3
G_SCIENCE = _C["impact_g_science"]
G_CONTAIN = _C["impact_g_containment"]
MU_SUN = 1.32712440018e20
MU_EARTH = 3.986004418e14
R_EARTH = 6378137.0
R_SOI_EARTH = 9.25e8


# ---------------------------------------------------------------- two-body propagation (universal variables)
def _stumpff(z):
    if z > 1e-8:
        s = math.sqrt(z)
        return (1 - math.cos(s)) / z, (s - math.sin(s)) / (s ** 3)
    if z < -1e-8:
        s = math.sqrt(-z)
        return (math.cosh(s) - 1) / (-z), (math.sinh(s) - s) / (s ** 3)
    return 0.5, 1.0 / 6.0


def kepler(r0, v0, dt, mu):
    """State after dt seconds on a two-body conic (Curtis, Algorithm 3.3 and 3.4)."""
    r0, v0 = np.asarray(r0, float), np.asarray(v0, float)
    rn = np.linalg.norm(r0)
    vr = float(np.dot(r0, v0)) / rn
    alpha = 2.0 / rn - float(np.dot(v0, v0)) / mu
    sm = math.sqrt(mu)
    x = sm * abs(alpha) * dt if abs(alpha) > 1e-12 else sm * dt / rn
    for _ in range(60):
        z = alpha * x * x
        C, S = _stumpff(z)
        F = rn * vr / sm * x * x * C + (1 - alpha * rn) * x ** 3 * S + rn * x - sm * dt
        dF = rn * vr / sm * x * (1 - alpha * x * x * S) + (1 - alpha * rn) * x * x * C + rn
        step = F / dF
        x -= step
        if abs(step) < 1e-9 * max(1.0, abs(x)):
            break
    z = alpha * x * x
    C, S = _stumpff(z)
    f = 1 - x * x / rn * C
    g = dt - x ** 3 / sm * S
    r = f * r0 + g * v0
    rr = np.linalg.norm(r)
    fd = sm / (rr * rn) * (alpha * x ** 3 * S - x)
    gd = 1 - x * x / rr * C
    return r, fd * r0 + gd * v0


def perigee_earth(r, v):
    """Perigee radius (m) and time to it (s, negative if past) of an Earth-centred conic."""
    r, v = np.asarray(r, float), np.asarray(v, float)
    h = np.cross(r, v)
    e_vec = np.cross(v, h) / MU_EARTH - r / np.linalg.norm(r)
    e = float(np.linalg.norm(e_vec))
    a_inv = 2.0 / np.linalg.norm(r) - float(np.dot(v, v)) / MU_EARTH
    hn2 = float(np.dot(h, h))
    rp = hn2 / MU_EARTH / (1.0 + e)
    rn = float(np.linalg.norm(r))
    cos_nu = float(np.clip(np.dot(e_vec, r) / (e * rn), -1, 1)) if e > 1e-12 else 1.0
    sin_sign = 1.0 if float(np.dot(r, v)) >= 0 else -1.0
    if e > 1.0:                       # hyperbola: time from perigee through the hyperbolic anomaly
        a = 1.0 / a_inv
        Hh = 2.0 * math.atanh(math.sqrt((e - 1) / (e + 1)) * math.tan(0.5 * math.acos(cos_nu))) * sin_sign
        tp = math.sqrt((-a) ** 3 / MU_EARTH) * (e * math.sinh(Hh) - Hh)
    elif e < 1.0 and a_inv > 0:       # ellipse
        a = 1.0 / a_inv
        E = 2.0 * math.atan(math.sqrt((1 - e) / (1 + e)) * math.tan(0.5 * math.acos(cos_nu))) * sin_sign
        tp = math.sqrt(a ** 3 / MU_EARTH) * (E - e * math.sin(E))
    else:
        tp = 0.0
    return rp, -tp


def predict_earth_pass(t, r_sun, v_sun, earth_state, horizon_s=500 * 86400.0, step_s=86400.0):
    """Sun-centred conic from (t, r, v) against Earth's ephemeris ``earth_state(t) -> (r, v)`` (m, m/s, Sun-centred).
    Returns (time of closest approach, perigee radius in m) or None when it does not come within the sphere of
    influence inside the horizon. The miss distance of the conic is taken as the impact parameter, and the
    hyperbolic perigee follows from it and the speed relative to Earth."""
    best = None
    k = 0
    prev = None
    while k * step_s <= horizon_s:
        tk = t + k * step_s
        r, v = kepler(r_sun, v_sun, k * step_s, MU_SUN) if k else (np.asarray(r_sun), np.asarray(v_sun))
        re, ve = earth_state(tk)
        d = float(np.linalg.norm(r - np.asarray(re)))
        if best is None or d < best[0]:
            best = (d, k * step_s)
        if prev is not None and d > prev and best[0] < 5 * R_SOI_EARTH:
            break
        prev = d
        k += 1
    if best is None:
        return None
    # refine with a golden-section search around the best step
    lo, hi = max(0.0, best[1] - step_s), best[1] + step_s

    def dist(s):
        r, v = kepler(r_sun, v_sun, s, MU_SUN)
        re, ve = earth_state(t + s)
        return float(np.linalg.norm(r - np.asarray(re))), r, v, np.asarray(re), np.asarray(ve)

    gr = (math.sqrt(5) - 1) / 2
    a, b = lo, hi
    c, d_ = b - gr * (b - a), a + gr * (b - a)
    for _ in range(80):
        if dist(c)[0] < dist(d_)[0]:
            b = d_
        else:
            a = c
        c, d_ = b - gr * (b - a), a + gr * (b - a)
        if b - a < 1.0:
            break
    s = 0.5 * (a + b)
    miss, r, v, re, ve = dist(s)
    if miss > R_SOI_EARTH:
        return None
    vinf = float(np.linalg.norm(v - ve))
    rp = MU_EARTH / vinf ** 2 * (math.sqrt(1.0 + (miss * vinf ** 2 / MU_EARTH) ** 2) - 1.0)
    return t + s, rp


def earth_avoidance(t, central, r, v, earth_state=None):
    """Check rule 1 at one moment. ``central`` is 'Earth' (r, v Earth-centred) or 'Sun' (Sun-centred, with
    ``earth_state``). Returns (seconds to closest approach, perigee altitude m, message or None)."""
    if central == "Earth":
        rp, tp = perigee_earth(r, v)
        if tp < 0:
            return tp, rp - R_EARTH, None
        t_ca = tp
    elif central == "Sun" and earth_state is not None:
        p = predict_earth_pass(t, r, v, earth_state)
        if p is None:
            return None, None, None
        t_ca, rp = p[0] - t, p[1]
    else:
        return None, None, None
    alt = rp - R_EARTH
    if alt < ENTRY_INTERFACE_M and t_ca > INTERCEPT_WINDOW_S:
        return t_ca, alt, (f"Earth return refused: {t_ca / 86400:.1f} days before closest approach the trajectory "
                           f"already hits Earth (perigee {alt / 1e3:.0f} km). Mars material must stay on an "
                           f"Earth-missing trajectory until {INTERCEPT_WINDOW_S / 86400:.0f} days before arrival")
    return t_ca, alt, None


def terminal_speed(mass, cd, area, rho):
    return math.sqrt(2.0 * mass * 9.80665 / (rho * cd * area))


def lakebed_g(diameter, speed, mass):
    """Peak deceleration in Utah's lakebed clay, the simulator's own fit (sim.py touchdown, Kellas 2017)."""
    return D.SOIL_IMPACT_G_COEF * diameter * speed * speed / max(mass, 1e-6)


def containment(impact_g, landing, mass, diameter, cd, rho_ground):
    """Check rule 2 at Earth touchdown. Returns (failure message or None, science lost message or None, detail)."""
    detail = {"impact_g": round(impact_g, 1)}
    fail = None
    if impact_g > G_CONTAIN:
        fail = (f"containment lost: the sample container took {impact_g:.0f} g at impact, more than the "
                f"{G_CONTAIN:.0f} g it is built to hold")
    if landing == "parachute":
        area = math.pi * diameter ** 2 / 4
        vt = terminal_speed(mass, cd, area, rho_ground)
        g_fail = lakebed_g(diameter, vt, mass)
        detail.update({"no_chute_speed_mps": round(vt, 1), "no_chute_g": round(g_fail, 1)})
        if g_fail > G_CONTAIN and fail is None:
            fail = (f"containment not assured: if the parachute failed the capsule would hit at {vt:.0f} m/s and its "
                    f"sample container would take {g_fail:.0f} g, more than {G_CONTAIN:.0f} g")
    sci = None
    if impact_g > G_SCIENCE:
        sci = f"the samples took {impact_g:.0f} g at impact, more than the {G_SCIENCE:.0f} g that keeps their science"
    return fail, sci, detail


class PlanetaryProtection(Model):
    """Wiring,:
    - Earth avoidance: at the end of every arc after Mars liftoff with Mars material aboard (``core/arcs.py``
      ``flight_step``, after ``_arc_end_reason`` at line 278), the core calls ``after_arc(t, central, r, v, earth_state)`` with the state
      relative to the central body ('Sun' or 'Earth') and, for 'Sun', a function giving Earth's Sun-centred state
      from the loaded ephemeris. A returned message is a flight failure.
    - Containment: in ``models/landing/surface.py`` ``touchdown`` (Earth branch from line 153), ``at_touchdown(impact_g, landing, mass,
      diameter, cd_subsonic, rho_ground)``; the first message is a flight failure, the second loses the samples.
    """
    name = "planetary_protection"
    area = "samples"
    order = 75

    def configure(self, ctx):
        self.ctx = ctx
        self.closest = None
        self.violation = None

    def after_arc(self, t, central, r, v, earth_state=None):
        t_ca, alt, msg = earth_avoidance(t, central, r, v, earth_state)
        if alt is not None:
            self.closest = (t, t_ca, alt)
        if msg and self.violation is None:
            self.violation = msg
        return msg

    def at_touchdown(self, impact_g, landing, mass, diameter, cd, rho_ground):
        return containment(impact_g, landing, mass, diameter, cd, rho_ground)

    def design_fields(self):
        return ["elements[kind=capsule].landing"]


MODELS = [PlanetaryProtection]
