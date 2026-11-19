"""Earth launch planning: launch time and parking-orbit plane for a given departure asymptote."""
from __future__ import annotations

import math

import numpy as np

from .util import J2, MU, RREF, pole, rodrigues, site_inertial, unit


def nodal_rate(a, inc, body="Earth"):
    n = math.sqrt(MU[body] / a ** 3)
    return -1.5 * n * J2[body] * (RREF[body] / a) ** 2 * math.cos(inc)


def plane_through(r_site, s_hat, pz):
    """Prograde plane normal containing the site and the asymptote direction."""
    n = unit(np.cross(r_site, s_hat))
    if np.dot(n, pz) < 0:
        n = -n
    return n


def perigee_dir(s_hat, nhat, vinf, rp, mu):
    e = 1 + rp * vinf * vinf / mu
    nu_inf = math.acos(-1.0 / e)
    return unit(rodrigues(s_hat, nhat, -nu_inf)), nu_inf


def arg_lat(r, nhat, pz):
    """Angle of r in the plane measured from the ascending node, in the direction of motion."""
    node = unit(np.cross(pz, nhat))
    y = np.cross(nhat, node)
    return math.atan2(float(np.dot(r, y)), float(np.dot(r, node))) % (2 * math.pi)


def plane_at(T, rs, s_hat, vinf, park_alt=200e3, coast=1800.0, body="Earth", lat=None):
    """The insertion plane for a launch at T from the inertial site position rs (at T): the plane through the
    site that contains s_hat at the departure burn, after J2 nodal regression over the coast. Used for a launch
    that does not go at the planned time (a weather hold), and to aim the plan at where the pad really is."""
    R0 = 6378137.0 if body == "Earth" else 3389.5e3
    a = R0 + park_alt
    pz = pole(body, T)
    la = math.asin(float(np.dot(unit(rs), pz))) if lat is None else math.radians(lat)
    dOm = nodal_rate(a, la, body) * (coast + 500.0)
    s_ins = rodrigues(s_hat, pz, -dOm)
    n_ins = plane_through(rs, s_ins, pz)
    n_tmi = unit(rodrigues(n_ins, pz, dOm))
    inc = math.acos(float(np.clip(np.dot(n_ins, pz), -1, 1)))
    pdir, nu_inf = perigee_dir(s_hat, n_tmi, vinf, a, MU[body])
    u_site = arg_lat(rs, n_ins, pz)
    u_p = arg_lat(pdir, n_tmi, pz)
    ang = (u_p - u_site) % (2 * math.pi)
    return {"t_launch": T, "n_ins": n_ins, "n_tmi": n_tmi, "inc_deg": math.degrees(inc), "perigee_dir": pdir,
            "angle_site_to_perigee_deg": math.degrees(ang), "nu_inf_deg": math.degrees(nu_inf)}


def plan(t_day, s_hat, vinf, site, park_alt=200e3, coast_guess=1800.0, downrange_deg=24.0, body="Earth", span=86400.0):
    """Find the launch time in [t_day, t_day + span] for a near-due-east launch into a plane that will
    contain s_hat at the departure burn (after J2 nodal regression during the coast). Returns a dict."""
    lat, lon, alt = site["lat"], site["lon"], site["alt"]
    if body == "Earth":
        # The simulator places Earth sites at their geodetic latitude on WGS84; site_inertial takes a
        # geocentric one. Taken as geocentric, LC-39A's 28.61 deg put the planned plane 18 km south of the pad,
        # and S1 flew a 6 deg dogleg north from the pitch-over to close it (seeds 1000 to 1011, ref sprint).
        from .util import geocentric_lat_earth
        lat = geocentric_lat_earth(lat, alt)
    R0 = 6378137.0 if body == "Earth" else 3389.5e3
    a = R0 + park_alt
    best = []
    pz = pole(body, t_day + span / 2)
    coast = coast_guess
    for _ in range(3):
        cands = []
        ts = np.arange(t_day + 600.0, t_day + span + 600.0, 60.0)
        for T in ts:
            rs = site_inertial(body, lat, lon, alt, T)
            # plane at TMI contains s_hat; at insertion it is rotated back by the nodal regression
            n_tmi = plane_through(rs, s_hat, pz)
            inc = math.acos(float(np.clip(np.dot(n_tmi, pz), -1, 1)))
            cands.append((T, inc))
        incs = np.array([c[1] for c in cands])
        sols = []
        for k in range(1, len(cands) - 1):
            if incs[k] <= incs[k - 1] and incs[k] <= incs[k + 1]:
                # refine by parabola
                T0 = cands[k][0]
                y0, y1, y2 = incs[k - 1], incs[k], incs[k + 1]
                den = (y0 - 2 * y1 + y2)
                off = 0.5 * (y0 - y2) / den * 60.0 if abs(den) > 1e-15 else 0.0
                sols.append(T0 + off)
        out = []
        for T in sols:
            dOm = nodal_rate(a, math.radians(lat), body) * (coast + 500.0)
            s_ins = rodrigues(s_hat, pz, -dOm)
            rs = site_inertial(body, lat, lon, alt, T)
            n_ins = plane_through(rs, s_ins, pz)
            n_tmi = unit(rodrigues(n_ins, pz, dOm))
            inc = math.acos(float(np.clip(np.dot(n_ins, pz), -1, 1)))
            pdir, nu_inf = perigee_dir(s_hat, n_tmi, vinf, a, MU[body])
            u_site = arg_lat(rs, n_ins, pz)
            u_p = arg_lat(pdir, n_tmi, pz)
            ang = (u_p - u_site) % (2 * math.pi)
            out.append({"t_launch": T, "n_ins": n_ins, "n_tmi": n_tmi, "inc_deg": math.degrees(inc), "perigee_dir": pdir,
                        "angle_site_to_perigee_deg": math.degrees(ang), "nu_inf_deg": math.degrees(nu_inf)})
        # choose the solution whose TMI point comes soonest after insertion
        good = [o for o in out if o["angle_site_to_perigee_deg"] > downrange_deg + 40]
        if not good:
            good = out
        good.sort(key=lambda o: o["angle_site_to_perigee_deg"])
        best = good[0]
        period = 2 * math.pi * math.sqrt(a ** 3 / MU[body])
        coast = max(300.0, (best["angle_site_to_perigee_deg"] - downrange_deg - 15.0) / 360.0 * period)
        best["coast_s"] = coast
    best["all"] = out
    return best
