"""B-plane targeting with the numerical predictor: encounter geometry, aim points and impulsive
corrections. Used for the TMI aim and every trajectory correction maneuver on both legs."""
from __future__ import annotations

import math
import os

import numpy as np

from . import predict as P
from .util import MU, rodrigues, rot_inertial_to_fixed, unit

# The solves stop on their iteration counts only, so a flight is the same however busy the machine is.
# With wall-clock stops, a loaded machine got fewer iterations and Mars landings moved by meters between runs
# of the same commit. REF_WALL_BUDGETS=1 restores the old stops.
WALL_BUDGETS = os.environ.get("REF_WALL_BUDGETS") == "1"


def hyper_tp(r, v, mu):
    """Time from the state (r, v) to periapsis of a hyperbola (seconds, positive if before periapsis)."""
    r = np.asarray(r, float)
    v = np.asarray(v, float)
    rn = np.linalg.norm(r)
    v2 = float(v @ v)
    a = 1.0 / (2.0 / rn - v2 / mu)          # negative for hyperbola
    e_vec = ((v2 - mu / rn) * r - float(r @ v) * v) / mu
    e = np.linalg.norm(e_vec)
    cosnu = float(np.clip(e_vec @ r / (e * rn), -1, 1))
    nu = math.acos(cosnu)
    if float(r @ v) < 0:
        nu = -nu
    F = 2 * math.atanh(math.sqrt((e - 1) / (e + 1)) * math.tan(nu / 2))
    M = e * math.sinh(F) - F
    n = math.sqrt(mu / (-a) ** 3)
    return -M / n


def closest_approach(t, x, central, body, t_lo, t_hi):
    """Closest approach to body between t_lo and t_hi for a trajectory that stays outside its sphere
    of influence. Returns (t_ca, state relative to body)."""
    t1, x1, c1, why = P.coast(t, x, central, t_lo, stop_soi=body)
    if c1 == body:
        return None, None
    t2, x2, why, hist = P._segment(t1, x1, c1, t_hi, dense=True)
    pts = []
    for tt, xx in hist:
        rel = P.to_central(xx, c1, body, tt)
        pts.append((float(np.linalg.norm(rel[:3])), tt, xx))
    k = int(np.argmin([p_[0] for p_ in pts]))
    lo = pts[max(0, k - 1)][1]
    hi = pts[min(len(pts) - 1, k + 1)][1]
    x_lo = pts[max(0, k - 1)][2]

    def dist(tq):
        t3, x3, c3, _ = P.coast(lo, x_lo, c1, tq)
        rel = P.to_central(x3, c3, body, t3)
        return float(np.linalg.norm(rel[:3])), rel

    g = (math.sqrt(5) - 1) / 2
    a, b = lo, hi
    c1_, c2_ = b - g * (b - a), a + g * (b - a)
    f1, _ = dist(c1_)
    f2, _ = dist(c2_)
    for _ in range(40):
        if f1 < f2:
            b, c2_, f2 = c2_, c1_, f1
            c1_ = b - g * (b - a)
            f1, _ = dist(c1_)
        else:
            a, c1_, f1 = c1_, c2_, f2
            c2_ = a + g * (b - a)
            f2, _ = dist(c2_)
        if b - a < 10.0:
            break
    tca = 0.5 * (a + b)
    _, rel = dist(tca)
    return tca, rel


def encounter(t, x, central, body="Mars", t_max=None, t_guess=None):
    """B-plane coordinates of the encounter with body and the periapsis (or closest approach) time.
    Inside the sphere of influence the B-plane is that of the osculating hyperbola; a trajectory
    that misses the sphere uses the straight-line closest approach (the two agree for large B)."""
    t_max = t_max if t_max is not None else t + 400 * 86400.0
    mu = MU[body]
    if central == body:
        x1 = np.asarray(x, float)
        bt, br, S, vinf = P.bplane(x1[:3], x1[3:], mu)
        return {"bt": bt, "br": br, "tp": t + hyper_tp(x1[:3], x1[3:], mu), "S": S, "vinf": vinf, "t_soi": t,
                "x_soi": x1, "soi": True}
    t1, x1, c1, why = P.coast(t, x, central, t_max, stop_soi=body)
    if c1 == body:
        bt, br, S, vinf = P.bplane(x1[:3], x1[3:], mu)
        tp = t1 + hyper_tp(x1[:3], x1[3:], mu)
        return {"bt": bt, "br": br, "tp": tp, "S": S, "vinf": vinf, "t_soi": t1, "x_soi": x1, "soi": True}
    tg = t_guess if t_guess is not None else (t_max - 30 * 86400.0)
    tca, rel = closest_approach(t, x, central, body, max(t, tg - 40 * 86400.0), min(t_max, tg + 40 * 86400.0))
    if tca is None:
        return None
    S = unit(rel[3:])
    T = unit(np.cross(S, [0.0, 0.0, 1.0]))
    R = np.cross(S, T)
    B = rel[:3] - float(rel[:3] @ S) * S
    return {"bt": float(B @ T), "br": float(B @ R), "tp": tca, "S": S, "vinf": float(np.linalg.norm(rel[3:])),
            "soi": False}


def solve_dv(t, x, central, aim, dv0=None, body="Mars", w_time=1.0, iters=6, tol_km=2.0, t_max=None, t_guess=None,
             budget_s=6.0, h_fd=0.01):
    """Impulsive velocity change at (t, x) that hits aim = (bt, br, tp). w_time scales the time
    constraint (seconds are weighed as w_time km); h_fd is the finite-difference step (m/s) of the Jacobian.
    Returns (dv, encounter)."""
    import time as _time
    t_wall0 = _time.time()
    dv = np.zeros(3) if dv0 is None else np.array(dv0, float)

    def f(d):
        xx = np.array(x, float)
        xx[3:] += d
        e = encounter(t, xx, central, body, t_max, t_guess if t_guess is not None else aim[2])
        if e is None:
            return None, None
        return np.array([(e["bt"] - aim[0]) / 1e3, (e["br"] - aim[1]) / 1e3, (e["tp"] - aim[2]) * w_time]), e

    g, e = f(dv)
    for _ in range(iters):
        if g is None:
            return None, None
        if np.max(np.abs(g)) < tol_km:
            break
        if (WALL_BUDGETS and _time.time() - t_wall0 > budget_s):
            break
        J = np.zeros((3, 3))
        h = h_fd
        for k in range(3):
            d2 = dv.copy()
            d2[k] += h
            gk, _ = f(d2)
            if gk is None:
                return None, None
            J[:, k] = (gk - g) / h
        step = np.linalg.lstsq(J, -g, rcond=None)[0]
        lim = 50.0
        if np.linalg.norm(step) > lim:
            step *= lim / np.linalg.norm(step)
        # Backtracking: take the step only where it lowers the miss. A full Newton step from a B-plane far from the
        # linear region diverged in some worlds (rtcm3 asked 74 m/s instead of about 0.2 and the capsule missed
        # Earth, world 1000 ref-b).
        for _ in range(6):
            g2, e2 = f(dv + step)
            if g2 is not None and np.linalg.norm(g2) < np.linalg.norm(g):
                break
            step = 0.5 * step
        else:
            break
        dv = dv + step
        g, e = g2, e2
    return dv, e


def b_for_fpa(vinf, r_ei, fpa_deg, mu):
    """Impact parameter giving flight path angle fpa (negative) at radius r_ei."""
    v_ei = math.sqrt(vinf * vinf + 2 * mu / r_ei)
    return r_ei * v_ei * math.cos(math.radians(fpa_deg)) / vinf


def ei_from_b(S, vinf, bvec, r_ei, mu):
    """Entry-interface position direction and the in-plane basis for a hyperbola given S and B."""
    b = np.linalg.norm(bvec)
    h_hat = unit(np.cross(bvec, S))     # h = vinf * (B x S)
    e = math.sqrt(1 + (b * vinf * vinf / mu) ** 2)
    p = (b * vinf) ** 2 / mu
    nu_inf = math.acos(-1 / e)
    # periapsis direction: rotate -S... incoming asymptote direction (position at -nu_inf) is -S
    # position at true anomaly nu: rotate p_hat by nu about h
    p_hat = unit(rodrigues(-S, h_hat, nu_inf))
    cos_nu = (p / r_ei - 1) / e
    nu_ei = -math.acos(max(-1.0, min(1.0, cos_nu)))   # before periapsis
    r_hat = unit(rodrigues(p_hat, h_hat, nu_ei))
    return r_hat, h_hat, p_hat, e, nu_ei


def aim_point(S, vinf, t_near, site_lat, site_lon, fpa_deg, range_deg, t_edl, body="Mars", r_ei=None):
    """B-plane aim (bt, br, tp) so that an entry at fpa_deg, flying range_deg downrange in t_edl
    seconds, ends over the site. Searches the B-plane angle and the arrival time near t_near."""
    mu = MU[body]
    R = 3389.5e3 if body == "Mars" else 6378137.0
    r_ei = r_ei or (R + (130e3 if body == "Mars" else 120e3))
    b = b_for_fpa(vinf, r_ei, fpa_deg, mu)
    T = unit(np.cross(S, [0.0, 0.0, 1.0]))
    Rv = np.cross(S, T)
    la, lo = math.radians(site_lat), math.radians(site_lon)
    site_f = np.array([math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)])

    def landing_dir(theta):
        bvec = b * (math.cos(theta) * T + math.sin(theta) * Rv)
        r_hat, h_hat, p_hat, e, nu_ei = ei_from_b(S, vinf, bvec, r_ei, mu)
        return unit(rodrigues(r_hat, h_hat, math.radians(range_deg))), bvec, e, nu_ei

    # time from EI to periapsis on the hyperbola
    def t_ei_to_p(e, nu_ei):
        a = -mu / (vinf * vinf)
        F = 2 * math.atanh(math.sqrt((e - 1) / (e + 1)) * math.tan(nu_ei / 2))
        M = e * math.sinh(F) - F
        return -M / math.sqrt(mu / (-a) ** 3)

    best = None
    times = [t_near - 12 * 3600 + j * 900.0 for j in range(97)]
    sds = np.array([rot_inertial_to_fixed(body, tt).T @ site_f for tt in times])
    for k in range(720):
        th = 2 * math.pi * k / 720
        ld, bvec, e, nu_ei = landing_dir(th)
        cs = sds @ ld
        j = int(np.argmax(cs))
        if best is None or cs[j] > best[0]:
            best = (float(cs[j]), th, times[j])
    c, th, tt = best
    # refine with a local search
    from scipy.optimize import minimize

    def cost(z):
        ld, _, _, _ = landing_dir(z[0])
        sd = rot_inertial_to_fixed(body, tt + 1000.0 * z[1]).T @ site_f
        return 1.0 - float(ld @ sd)

    sol = minimize(cost, [th, 0.0], method="Nelder-Mead",
                   options={"xatol": 1e-8, "fatol": 1e-15, "maxiter": 600, "initial_simplex": [[th, 0], [th + 0.01, 0], [th, 0.5]]})
    th, t_land = sol.x[0], tt + 1000.0 * sol.x[1]
    ld, bvec, e, nu_ei = landing_dir(th)
    t_ei = t_land - t_edl
    tp = t_ei + t_ei_to_p(e, nu_ei)
    return {"bt": float(bvec @ T), "br": float(bvec @ Rv), "tp": tp, "t_ei": t_ei, "theta": th, "b": b,
            "miss_km": math.acos(max(-1, min(1, 1 - sol.fun))) * R / 1e3}


def landing_of(t, x, central, cap_mass, cap_cdA, ei_alt=120e3, stop_alt=1300.0, push=None):
    """Coast to the entry interface, then fly the ballistic capsule to the ground with the NRLMSISE-00
    predictor. Returns (geodetic lat, lon, fpa_ei_deg, t_land) or None: the predictor stops at stop_alt above the
    WGS84 ellipsoid, and the landing point is read on the ellipsoid, as the simulator scores it.
    push: (seconds before the 160 km crossing, speed m/s): the capsule's separation push along its velocity then."""
    from .util import geodetic_latlon_earth
    if push is not None:
        lead, dv_push = push
        tc, xc, cc, why = P.coast(t, x, central, t + 20 * 86400.0, stop_alt=160e3, stop_body="Earth")
        if why != "alt" or cc != "Earth":
            return None
        t_sep = tc - lead
        if t_sep > t:
            t, x, central, _ = P.coast(t, x, central, t_sep)
        # (already inside the separation window: the stack separates at once, a minute or two after this)
        x = np.array(x, float)
        x[3:] += dv_push * x[3:] / np.linalg.norm(x[3:])
    t1, x1, c1, why = P.coast(t, x, central, t + 20 * 86400.0, stop_alt=ei_alt, stop_body="Earth")
    if why != "alt" or c1 != "Earth":
        return None
    r, v = x1[:3], x1[3:]
    fpa = math.degrees(math.asin(float(r @ v) / np.linalg.norm(r) / np.linalg.norm(v)))
    tl, xl, info = P.capsule_entry(t1, x1, cap_mass, cap_cdA, stop_alt=stop_alt, dt=1.0)
    la, lo, _ = geodetic_latlon_earth(xl[:3], tl)
    return la, lo, fpa, tl


def solve_landing(t, x, central, site, fpa_target, cap_mass, cap_cdA, iters=4, budget_s=5.0, stop_alt=1300.0, push=None):
    """Impulsive correction that lands the capsule on site (lat, lon) with entry flight path angle
    fpa_target at 120 km. stop_alt: the predicted point is where the capsule passes this height above the
    ellipsoid (the ground, or the parachute deployment height). Returns (dv, (lat, lon, fpa)) or (None, None)."""
    import time as _time
    t_w = _time.time()
    lat0, lon0 = site

    def g_of(d):
        xx = np.array(x, float)
        xx[3:] += d
        res = landing_of(t, xx, central, cap_mass, cap_cdA, stop_alt=stop_alt, push=push)
        if res is None:
            return None, None
        la, lo, fpa, tl = res
        dlo = (lo - lon0 + 180.0) % 360.0 - 180.0
        return np.array([(la - lat0) * 111.0, dlo * 111.0 * math.cos(math.radians(lat0)), (fpa - fpa_target) * 50.0]), res

    dv = np.zeros(3)
    g, res = g_of(dv)
    for _ in range(iters):
        if g is None or np.max(np.abs(g)) < 0.5 or (WALL_BUDGETS and _time.time() - t_w > budget_s):
            break
        J = np.zeros((3, 3))
        h = 0.02
        for k in range(3):
            d2 = dv.copy()
            d2[k] += h
            gk, _ = g_of(d2)
            if gk is None:
                return None, None
            J[:, k] = (gk - g) / h
        step = np.linalg.lstsq(J, -g, rcond=None)[0]
        if np.linalg.norm(step) > 5.0:
            step *= 5.0 / np.linalg.norm(step)
        dv = dv + step
        g, res = g_of(dv)
    return dv, res



def mars_landing_of(edl, t, x, central):
    """Coast to Mars entry interface (130 km), then predict the descent to the powered-descent point with
    the entry guidance's reference profile. Returns (lat, lon, fpa_ei_deg) or None."""
    from .util import latlon
    t1, x1, c1, why = P.coast(t, x, central, t + 20 * 86400.0, stop_alt=130e3, stop_body="Mars")
    if why != "alt" or c1 != "Mars":
        return None
    r, v = x1[:3], x1[3:]
    fpa = math.degrees(math.asin(float(r @ v) / np.linalg.norm(r) / np.linalg.norm(v)))
    from .edl import predict_reference
    te, xe, info = predict_reference(edl, t1, x1)
    la, lo = latlon("Mars", xe[:3], te)
    return la, lo, fpa


def solve_mars_landing(edl, t, x, central, site, fpa_target, iters=4, budget_s=6.0):
    import time as _time
    t_w = _time.time()
    lat0, lon0 = site
    km = 3389.5 * math.pi / 180.0

    def g_of(d):
        xx = np.array(x, float)
        xx[3:] += d
        res = mars_landing_of(edl, t, xx, central)
        if res is None:
            return None, None
        la, lo, fpa = res
        dlo = (lo - lon0 + 180.0) % 360.0 - 180.0
        return np.array([(la - lat0) * km, dlo * km * math.cos(math.radians(lat0)), (fpa - fpa_target) * 50.0]), res

    dv = np.zeros(3)
    g, res = g_of(dv)
    for _ in range(iters):
        if g is None or np.max(np.abs(g)) < 0.3 or (WALL_BUDGETS and _time.time() - t_w > budget_s):
            break
        J = np.zeros((3, 3))
        h = 0.01
        for k in range(3):
            d2 = dv.copy()
            d2[k] += h
            gk, _ = g_of(d2)
            if gk is None:
                return None, None
            J[:, k] = (gk - g) / h
        step = np.linalg.lstsq(J, -g, rcond=None)[0]
        if np.linalg.norm(step) > 5.0:
            step *= 5.0 / np.linalg.norm(step)
        dv = dv + step
        g, res = g_of(dv)
    return dv, res


def ei_state(S, vinf, theta, b, r_ei, mu):
    """Entry-interface position and velocity (planet-centred, inertial) on the approach hyperbola with
    incoming asymptote S, hyperbolic speed vinf and B-vector of length b at B-plane angle theta."""
    T = unit(np.cross(S, [0.0, 0.0, 1.0]))
    R = np.cross(S, T)
    bvec = b * (math.cos(theta) * T + math.sin(theta) * R)
    r_hat, h_hat, p_hat, e, nu_ei = ei_from_b(S, vinf, bvec, r_ei, mu)
    p = (b * vinf) ** 2 / mu
    f = math.sqrt(mu / p)
    t_hat = np.cross(h_hat, r_hat)
    v = f * (e * math.sin(nu_ei) * r_hat + (1 + e * math.cos(nu_ei)) * t_hat)
    return r_ei * r_hat, v, bvec, e, nu_ei


def aim_point_edl(edl, S, vinf, t_near, site, fpa_deg, iters=6):
    """B-plane aim for a Mars landing: like aim_point, but the descent from the entry interface is flown
    with the onboard EDL predictor (reference lift profile) instead of an inertial range angle."""
    from .edl import predict_reference
    from .util import latlon
    mu = MU["Mars"]
    r_ei = 3389.5e3 + 130e3
    a0 = aim_point(S, vinf, t_near, site[0], site[1], fpa_deg, 17.0, 442.0)
    b = a0["b"]
    th, t_ei = a0["theta"], a0["t_ei"]
    km = 3389.5 * math.pi / 180.0

    def g(th_, t_):
        r, v, _, _, _ = ei_state(S, vinf, th_, b, r_ei, mu)
        te, xe, info = predict_reference(edl, t_, np.concatenate([r, v]))
        la, lo = latlon("Mars", xe[:3], te)
        dlo = (lo - site[1] + 180.0) % 360.0 - 180.0
        return np.array([(la - site[0]) * km, dlo * km * math.cos(math.radians(site[0]))])

    gv = g(th, t_ei)
    for _ in range(iters):
        if np.max(np.abs(gv)) < 0.2:
            break
        J = np.zeros((2, 2))
        g1 = g(th + 1e-3, t_ei)
        g2 = g(th, t_ei + 20.0)
        J[:, 0] = (g1 - gv) / 1e-3
        J[:, 1] = (g2 - gv) / 20.0
        step = np.linalg.lstsq(J, -gv, rcond=None)[0]
        step[0] = max(-0.05, min(0.05, step[0]))
        step[1] = max(-1800.0, min(1800.0, step[1]))
        th, t_ei = th + step[0], t_ei + step[1]
        gv = g(th, t_ei)
    r, v, bvec, e, nu_ei = ei_state(S, vinf, th, b, r_ei, mu)
    T = unit(np.cross(S, [0.0, 0.0, 1.0]))
    Rv = np.cross(S, T)
    a_ = -mu / (vinf * vinf)
    F = 2 * math.atanh(math.sqrt((e - 1) / (e + 1)) * math.tan(nu_ei / 2))
    Mh = e * math.sinh(F) - F
    tp = t_ei - Mh / math.sqrt(mu / (-a_) ** 3)
    return {"bt": float(bvec @ T), "br": float(bvec @ Rv), "tp": tp, "t_ei": t_ei, "theta": th, "b": b,
            "miss_km": float(np.linalg.norm(gv)), "edl": True}


def safe_miss_aim(aim, vinf, rp_alt_m, body="Earth"):
    """The same B-plane aim turned into a miss: same angle and arrival time, with |B| for a perigee rp_alt_m above
    the ground (planetary protection: Mars material stays on an Earth-missing course until the last week)."""
    R = 3389.5e3 if body == "Mars" else 6378137.0
    rp = R + rp_alt_m
    vinf = float(np.linalg.norm(vinf)) if np.ndim(vinf) else float(vinf)
    b_safe = rp * math.sqrt(1.0 + 2.0 * MU[body] / (rp * vinf * vinf))
    k = b_safe / math.hypot(aim["bt"], aim["br"])
    return dict(aim, bt=aim["bt"] * k, br=aim["br"] * k, b_safe=b_safe)
