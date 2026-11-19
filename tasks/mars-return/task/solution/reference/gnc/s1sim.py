"""First-stage ascent predictor: a fast point-mass simulation of the gravity turn over a rotating
spherical Earth with a US76-like atmosphere and the slender-body drag model. Used to choose the pitch
kick so the ascent keeps max-q in bounds and stages at a good flight path angle."""
from __future__ import annotations

import math

import numpy as np

from .util import MU

US76 = [(0.0, 288.15, -6.5e-3, 101325.0), (11e3, 216.65, 0.0, 22632.06), (20e3, 216.65, 1.0e-3, 5474.889),
        (32e3, 228.65, 2.8e-3, 868.0187), (47e3, 270.65, 0.0, 110.9063), (51e3, 270.65, -2.8e-3, 66.93887),
        (71e3, 214.65, -2.0e-3, 3.956420), (84.852e3, 186.946, 0.0, 0.3733836)]
G0 = 9.80665
R_AIR = 287.053
SLENDER_CA_MACH = [(0.0, 0.30), (0.8, 0.35), (1.05, 0.55), (1.2, 0.55), (1.5, 0.48), (2.0, 0.40),
                   (3.0, 0.32), (5.0, 0.27), (10.0, 0.25), (50.0, 0.25)]


def atmos(h):
    """(rho, p, T) at geometric altitude h (m)."""
    if h < 0:
        h = 0.0
    hg = 6356766.0 * h / (6356766.0 + h)
    if hg > 86e3:
        T = 186.9
        p = 0.3734 * math.exp(-(hg - 86e3) / 6300.0)
        return p / (R_AIR * T), p, T
    for k in range(len(US76) - 1, -1, -1):
        hb, Tb, L, pb = US76[k]
        if hg >= hb:
            break
    T = Tb + L * (hg - hb)
    if L == 0.0:
        p = pb * math.exp(-G0 * (hg - hb) / (R_AIR * Tb))
    else:
        p = pb * (T / Tb) ** (-G0 / (R_AIR * L))
    return p / (R_AIR * T), p, T


def interp(tab, x):
    if x <= tab[0][0]:
        return tab[0][1]
    for (a, fa), (b, fb) in zip(tab, tab[1:]):
        if a <= x <= b:
            return fa + (fb - fa) * (x - a) / (b - a)
    return tab[-1][1]


def simulate(r0, pz, az_dir, F_vac, Ae, mdot, m0, m_burnout, S, kick_deg, t_vert, t_kick, h=0.5, omega=7.2921159e-5,
             density_scale=1.0, record=False):
    """Gravity turn from the pad position r0 (inertial). Returns dict with max q, burnout state, and with record the
    path below 60 km as (altitude, dynamic pressure, Earth-relative velocity, position) every 2 s."""
    path = []
    mu = MU["Earth"]
    w = omega * np.asarray(pz, float)
    r = np.array(r0, float)
    v = np.cross(w, r)
    m = m0
    t = 0.0
    up0 = r / np.linalg.norm(r)
    kick = math.radians(kick_deg)
    qmax = 0.0
    hmax_q = 0.0
    while m > m_burnout and t < 600.0:
        rn = np.linalg.norm(r)
        up = r / rn
        alt = rn - 6371000.0
        rho, p, T = atmos(alt)
        rho *= density_scale
        vrel = v - np.cross(w, r)
        V = np.linalg.norm(vrel)
        if t < t_vert:
            d = up
        elif t < t_vert + t_kick:
            d = math.cos(kick) * up + math.sin(kick) * az_dir
        else:
            d = vrel / V
            if math.acos(max(-1, min(1, float(np.dot(d, up))))) < kick and t < t_vert + t_kick + 20:
                d = math.cos(kick) * up + math.sin(kick) * az_dir
        Fth = max(0.0, F_vac - p * Ae)
        q = 0.5 * rho * V * V
        if q > qmax:
            qmax, hmax_q = q, alt
        if record and alt < 60e3 and abs(t / 2.0 - round(t / 2.0)) < 1e-6:
            path.append((alt, q, vrel.copy(), r.copy()))
        mach = V / math.sqrt(1.4 * R_AIR * T)
        Dacc = q * interp(SLENDER_CA_MACH, mach) * S / m
        a = -mu * r / rn ** 3 + Fth / m * d - (Dacc * vrel / V if V > 1 else 0.0)
        # semi-implicit Euler is enough for planning at this step
        v = v + a * h
        r = r + v * h
        m -= mdot * h
        t += h
        # az_dir follows the local frame (keeps heading roughly constant)
        east = np.cross(pz, up)
        east /= np.linalg.norm(east)
        north = np.cross(up, east)
        az_dir = az_dir - np.dot(az_dir, up) * up
        az_dir /= np.linalg.norm(az_dir)
    rn = np.linalg.norm(r)
    up = r / rn
    vrel = v - np.cross(w, r)
    fpa = math.degrees(math.asin(float(np.dot(vrel, up)) / np.linalg.norm(vrel)))
    out = {"qmax": qmax, "alt_qmax": hmax_q, "t": t, "alt": rn - 6371000.0, "v_inertial": float(np.linalg.norm(v)),
           "fpa_rel": fpa, "v_rel": float(np.linalg.norm(vrel))}
    if record:
        out["path"] = path
    return out


def choose_kick(r0, pz, az_dir, F_vac, Ae, mdot, m0, m_burnout, S, t_vert, t_kick, q_target=38e3, fpa_min=18.0):
    """Largest kick (flattest, least gravity loss) whose max-q stays under q_target and whose burnout
    flight path angle stays above fpa_min."""
    best = None
    lo, hi = 0.2, 8.0
    res_lo = simulate(r0, pz, az_dir, F_vac, Ae, mdot, m0, m_burnout, S, lo, t_vert, t_kick)
    if res_lo["qmax"] > q_target or res_lo["fpa_rel"] < fpa_min:
        return lo, res_lo
    for _ in range(18):
        mid = 0.5 * (lo + hi)
        res = simulate(r0, pz, az_dir, F_vac, Ae, mdot, m0, m_burnout, S, mid, t_vert, t_kick)
        ok = res["qmax"] <= q_target and res["fpa_rel"] >= fpa_min
        if ok:
            lo, best = mid, res
        else:
            hi = mid
    if best is None:
        best = res_lo
    return lo, best
