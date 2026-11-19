"""Shared constants, frames and small helpers for the reference flight software."""
from __future__ import annotations

import math

import numpy as np

G0 = 9.80665
MU = {"Earth": 3.986004415e14, "Mars": 4.28283758157561e13, "Sun": 1.32712440041279e20}
# Zonal J2 and reference radius of the gravity fields the simulator uses (GGM02C, JGMRO-120D).
J2 = {"Earth": 4.841694588430318e-4 * math.sqrt(5.0), "Mars": 8.750220924537e-4 * math.sqrt(5.0)}
RREF = {"Earth": 6378136.3, "Mars": 3396000.0}
OMEGA = {"Earth": 7.2921159e-5, "Mars": 7.088218e-5}
R_EQ_EARTH = 6378137.0
F_EARTH = 1 / 298.257223563
MARS_R = 3389.5e3
DAY = 86400.0
SOL = 88775.244

_KERNELS = False


def kernels():
    """Load the simulator's SPICE kernels (DE440 and friends) once."""
    global _KERNELS
    if _KERNELS:
        return
    from .marsenv import load_kernels
    load_kernels()
    _KERNELS = True


def body_state(body, t, center="Sun"):
    kernels()
    from tudatpy.interface import spice
    return np.array(spice.get_body_cartesian_state_at_epoch(body, center, "J2000", "NONE", t))


def rot_inertial_to_fixed(body, t):
    """J2000 -> body-fixed rotation matrix (the simulator's rotation models are SPICE IAU frames)."""
    kernels()
    from tudatpy.interface import spice
    return np.array(spice.compute_rotation_matrix_between_frames("J2000", "IAU_EARTH" if body == "Earth" else "IAU_MARS", t))


def pole(body, t):
    return rot_inertial_to_fixed(body, t).T @ np.array([0.0, 0.0, 1.0])


def unit(v):
    v = np.asarray(v, dtype=float)
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def rodrigues(v, axis, ang):
    k = unit(axis)
    return v * math.cos(ang) + np.cross(k, v) * math.sin(ang) + k * np.dot(k, v) * (1 - math.cos(ang))


def angle(a, b):
    return math.acos(float(np.clip(np.dot(unit(a), unit(b)), -1.0, 1.0)))


def site_inertial(body, lat_deg, lon_deg, alt, t):
    """Inertial position of a surface point, the way the simulator places sites."""
    if body == "Earth":
        Rs = R_EQ_EARTH * (1 - F_EARTH * math.sin(math.radians(lat_deg)) ** 2)
    else:
        Rs = MARS_R
    la, lo = math.radians(lat_deg), math.radians(lon_deg)
    rf = (Rs + alt) * np.array([math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)])
    return rot_inertial_to_fixed(body, t).T @ rf


def latlon(body, r, t):
    rf = rot_inertial_to_fixed(body, t) @ np.asarray(r, dtype=float)
    return math.degrees(math.asin(rf[2] / np.linalg.norm(rf))), math.degrees(math.atan2(rf[1], rf[0]))


def geodetic_latlon_earth(r, t):
    """Geodetic latitude and longitude (deg) of an Earth position on the WGS84 ellipsoid, and the height above it
    (m), as the simulator places Earth sites (Bowring's iteration)."""
    rf = rot_inertial_to_fixed("Earth", t) @ np.asarray(r, dtype=float)
    e2 = F_EARTH * (2 - F_EARTH)
    x, y, z = (float(c) for c in rf)
    p = math.hypot(x, y)
    lat, h = math.atan2(z, p * (1 - e2)), 0.0
    for _ in range(6):
        n = R_EQ_EARTH / math.sqrt(1 - e2 * math.sin(lat) ** 2)
        h = p / math.cos(lat) - n
        lat = math.atan2(z, p * (1 - e2 * n / (n + h)))
    return math.degrees(lat), math.degrees(math.atan2(y, x)), h


def geocentric_lat_earth(lat_geodetic_deg, h=0.0):
    """Geocentric latitude (deg) of a point at geodetic latitude and height h on the WGS84 ellipsoid."""
    e2 = F_EARTH * (2 - F_EARTH)
    phi = math.radians(lat_geodetic_deg)
    n = R_EQ_EARTH / math.sqrt(1 - e2 * math.sin(phi) ** 2)
    return math.degrees(math.atan2((n * (1 - e2) + h) * math.sin(phi), (n + h) * math.cos(phi)))


def great_circle(lat1, lon1, lat2, lon2, R):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    c = math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(math.radians(lon2 - lon1))
    return R * math.acos(max(-1.0, min(1.0, c)))


def grav(body, r):
    """Point mass plus J2 gravity acceleration, J2000 axes, pole along the IAU pole (passed in r frame)."""
    mu = MU[body]
    rn = np.linalg.norm(r)
    return -mu * r / rn ** 3


def grav_j2(body, r, pz):
    """Point mass + J2 with the body's pole unit vector pz (inertial)."""
    mu, j2, R = MU[body], J2[body], RREF[body]
    rn = np.linalg.norm(r)
    z = float(np.dot(r, pz))
    k = 1.5 * j2 * mu * R * R / rn ** 5
    s = 5.0 * z * z / (rn * rn)
    return -mu * r / rn ** 3 - k * ((1 - s) * r + 2 * z * pz)


def kepler_vinf(r, v, mu):
    """Outgoing hyperbolic excess velocity vector of a two-body hyperbola (None if bound)."""
    r = np.asarray(r, float)
    v = np.asarray(v, float)
    rn = np.linalg.norm(r)
    v2 = float(np.dot(v, v))
    en = v2 / 2 - mu / rn
    if en <= 0:
        return None
    vinf = math.sqrt(2 * en)
    h = np.cross(r, v)
    e_vec = np.cross(v, h) / mu - r / rn
    e = np.linalg.norm(e_vec)
    p_hat = e_vec / e
    q_hat = unit(np.cross(h, p_hat))
    nu = math.acos(-1.0 / e)
    return vinf * (math.cos(nu) * p_hat + math.sin(nu) * q_hat)


def orbit_rp_ra(r, v, mu):
    rn = np.linalg.norm(r)
    v2 = float(np.dot(v, v))
    en = v2 / 2 - mu / rn
    h = np.linalg.norm(np.cross(r, v))
    e_vec = np.cross(v, np.cross(r, v)) / mu - np.asarray(r) / rn
    e = float(np.linalg.norm(e_vec))
    p = h * h / mu
    rp = p / (1 + e)
    ra = p / (1 - e) if e < 1 else float("inf")
    return rp, ra, e, en


def hyperbola_velocity(r, s_hat, vinf, mu, n_hat=None):
    """Velocity at position r that puts the vehicle on a two-body hyperbola whose outgoing asymptote is
    s_hat * vinf. The orbit plane contains r and s_hat (or uses n_hat if given, for the in-plane basis)."""
    r = np.asarray(r, float)
    rn = np.linalg.norm(r)
    rh = r / rn
    if n_hat is None:
        n_hat = unit(np.cross(rh, s_hat))
    th = unit(np.cross(n_hat, rh))
    # angle phi from r to s measured in the direction of motion
    s_in = s_hat - np.dot(s_hat, n_hat) * n_hat
    s_in = unit(s_in)
    phi = math.atan2(float(np.dot(s_in, th)), float(np.dot(s_in, rh)))
    if phi < 0:
        phi += 2 * math.pi
    k = mu / (vinf * vinf)
    a_ = k
    b_ = -rn * math.sin(phi)
    c_ = -rn * (1 - math.cos(phi))
    x = (-b_ + math.sqrt(b_ * b_ - 4 * a_ * c_)) / (2 * a_)
    e = math.sqrt(1 + x * x)
    p = k * x * x
    nu_inf = math.acos(-1.0 / e)
    nu = nu_inf - phi
    f = math.sqrt(mu / p)
    return f * (e * math.sin(nu) * rh + (1 + e * math.cos(nu)) * th)
