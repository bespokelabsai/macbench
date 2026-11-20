"""Range safety at the launch site: launch azimuth limits, the instantaneous impact point (IIP) in
powered flight, and where spent stages fall.

Data, not physics of our own:
- azimuth limits per pad from the FAA's published spaceport data (``data/range/sites.json``);
- land from Natural Earth 1:10m land and minor islands (public domain), as a 0.05 deg grid
  (``data/range/landmask_0p05.npz``, built by ``results/issue-79/build/build_landmask.py``).

The IIP is the vacuum two-body impact point, the quantity range safety watches in powered flight
(SSCMAN 91-710): where the vehicle would land if thrust stopped now. Air drag would make the real impact
shorter; range safety uses the vacuum IIP too. Earth is a sphere of 6,371 km for the impact, rotating under the
falling body.
"""
from __future__ import annotations

import json
import math
import os

import numpy as np

GM_EARTH = 3.986004418e14
R_EARTH = 6371000.0
OMEGA_EARTH = 7.2921159e-5
DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "range")

_CACHE = {}


def sites():
    if "sites" not in _CACHE:
        with open(os.path.join(DATA, "sites.json")) as f:
            _CACHE["sites"] = json.load(f)
    return _CACHE["sites"]


def landmask():
    if "land" not in _CACHE:
        z = np.load(os.path.join(DATA, "landmask_0p05.npz"))
        _CACHE["land"] = (np.unpackbits(z["land"], axis=1)[:, :int(z["nlon"])].astype(bool), float(z["res_deg"]))
    return _CACHE["land"]


def is_land(lat_deg, lon_deg):
    m, res = landmask()
    i = min(m.shape[0] - 1, max(0, int((lat_deg + 90.0) / res)))
    j = int(((lon_deg + 180.0) % 360.0) / res) % m.shape[1]
    return bool(m[i, j])


def great_circle_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    c = math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(dl)
    return R_EARTH / 1000.0 * math.acos(max(-1.0, min(1.0, c)))


def check_azimuth(site_name, azimuth_deg):
    """None if the range allows this launch azimuth from the pad, else the reason it is refused."""
    s = sites()["sites"][site_name]
    lo, hi = s["azimuth_deg"]
    az = azimuth_deg % 360.0
    if lo <= az <= hi:
        return None
    return (f"launch azimuth {az:.1f} deg is outside the {s['range']}'s limits for {s['name']} "
            f"({lo:.0f} to {hi:.0f} deg): range safety refuses the flight")


def heading_deg(lat_deg, lon_deg, v_fixed):
    """Azimuth (deg from north, clockwise) of an Earth-fixed velocity at a place."""
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    east = np.array([-math.sin(lon), math.cos(lon), 0.0])
    north = np.array([-math.sin(lat) * math.cos(lon), -math.sin(lat) * math.sin(lon), math.cos(lat)])
    return math.degrees(math.atan2(float(np.dot(v_fixed, east)), float(np.dot(v_fixed, north)))) % 360.0


def fixed_from_inertial(r, v, R):
    """Earth-fixed position and velocity from J2000 ones, with R the fixed-to-J2000 rotation at that time."""
    rf = R.T @ np.asarray(r, float)
    vf = R.T @ (np.asarray(v, float) - np.cross([0.0, 0.0, OMEGA_EARTH], np.asarray(r, float)))
    return rf, vf


WGS84_A = 6378137.0
WGS84_F = 1.0 / 298.257223563


def latlon(rf):
    """Geodetic (WGS84) latitude and longitude (deg) of an Earth-fixed position, as maps, pad surveys and the
    Natural Earth coastlines give them. Bowring's formula with two refinements: well under a millimetre of
    latitude error at any height below the Moon. A planetocentric latitude is up to 0.19 deg off."""
    x, y, z = float(rf[0]), float(rf[1]), float(rf[2])
    a, f = WGS84_A, WGS84_F
    b = a * (1.0 - f)
    e2 = f * (2.0 - f)
    ep2 = e2 / (1.0 - e2)
    p = math.hypot(x, y)
    lon = math.degrees(math.atan2(y, x))
    if p < 1e-9:
        return (90.0 if z >= 0 else -90.0), lon
    beta = math.atan2(z * a, p * b)
    for _ in range(3):
        lat = math.atan2(z + ep2 * b * math.sin(beta) ** 3, p - e2 * a * math.cos(beta) ** 3)
        beta = math.atan2((1.0 - f) * math.sin(lat), math.cos(lat))
    return math.degrees(lat), lon


def _kepler(r0, v0, dt):
    from ..comms.edl import kepler_propagate
    return kepler_propagate(r0, v0, dt, GM_EARTH)


def iip(r, v, R0, max_s=3600.0):
    """Vacuum instantaneous impact point of a J2000 state (from Earth's centre), with R0 the fixed-to-J2000
    rotation now. Returns (lat, lon, time to impact s) or None if the orbit does not reach the ground within
    ``max_s`` (an orbit or an escape)."""
    r, v = np.asarray(r, float), np.asarray(v, float)
    if np.linalg.norm(r) <= R_EARTH:
        rf = R0.T @ r
        return (*latlon(rf), 0.0)
    h = np.cross(r, v)
    en = 0.5 * float(v @ v) - GM_EARTH / np.linalg.norm(r)
    if en < 0:
        a = -GM_EARTH / (2.0 * en)
        e = math.sqrt(max(0.0, 1.0 + 2.0 * en * float(h @ h) / GM_EARTH ** 2))
        if a * (1.0 - e) > R_EARTH:
            return None
    t_lo, t_hi, step = 0.0, None, 5.0
    t = step
    while t <= max_s:
        if np.linalg.norm(_kepler(r, v, t)) <= R_EARTH:
            t_hi = t
            break
        t_lo = t
        t += step if t < 120 else 30.0
    if t_hi is None:
        return None
    for _ in range(40):
        tm = 0.5 * (t_lo + t_hi)
        if np.linalg.norm(_kepler(r, v, tm)) <= R_EARTH:
            t_hi = tm
        else:
            t_lo = tm
    ri = _kepler(r, v, t_hi)
    th = OMEGA_EARTH * t_hi                     # Earth turns under the falling body
    Rz = np.array([[math.cos(th), math.sin(th), 0.0], [-math.sin(th), math.cos(th), 0.0], [0.0, 0.0, 1.0]])
    rf = Rz @ (R0.T @ ri)
    return (*latlon(rf), t_hi)


def check_impact(site_name, point, what="instantaneous impact point"):
    """None if an impact point is at sea (or far downrange), else the reason."""
    if point is None:
        return None
    lat, lon = point[0], point[1]
    s = sites()["sites"][site_name]
    d = great_circle_km(s["lat"], s["lon"], lat, lon)
    if d <= s["launch_area_km"] or d > sites()["iip_check_downrange_km"]:
        return None
    if is_land(lat, lon):
        return (f"{what} at {lat:.2f}, {lon:.2f} ({d:.0f} km from {s['name']}) is over land: "
                f"range safety does not allow it")
    return None
