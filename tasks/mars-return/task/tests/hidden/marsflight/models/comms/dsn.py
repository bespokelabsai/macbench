"""Where the Deep Space Network can see the vehicle, and when the Sun is in the way.

Geometry only, from SPICE through Tudat (``tudatpy.interface.spice``, the kernels the simulator already loads:
DE440 for the planets, the IAU PCK for Earth's and Mars's rotation). Station positions are the 810-005 module
301 WGS 84 coordinates (``data/comms/dsn.json``). Times are seconds TDB since J2000 (the simulator's ``t``),
vectors are in J2000.

- ``elevation(complex, r, t)``: elevation of the vehicle above a complex's horizon, with ``r`` the vehicle's
  position from Earth's centre.
- ``sep_deg(r, t)``: the Sun-Earth-probe angle. Below 2 deg no commands are sent (NASA's Mars conjunction
  practice); below 2.3 deg (X) or 1 deg (Ka) telemetry is lost (810-005 106 sec. 2.3).
- ``Contact``: per-time contact state for one flight, with a booking rule (passes per day) and a cache.
"""
from __future__ import annotations

import math

import numpy as np

from .link import table

WGS84_A = 6378137.0
WGS84_F = 1.0 / 298.257223563
COMPLEXES = ("goldstone", "canberra", "madrid")
DAY = 86400.0


def _spice():
    from tudatpy.interface import spice
    from ...environment import load_kernels
    load_kernels()
    return spice


def body_position(target, observer, t):
    """Position of ``target`` from ``observer`` (m, J2000, geometric) at ``t``."""
    from ...environment import spice_name   # DE440 alone has no Mars centre: Mars flies on its barycentre
    sp = _spice()
    return np.asarray(sp.get_body_cartesian_position_at_epoch(spice_name(target), spice_name(observer), "J2000", "None",
                                                              float(t)))


def rotation(frame_from, frame_to, t):
    return np.asarray(_spice().compute_rotation_matrix_between_frames(frame_from, frame_to, float(t)))


def geodetic_to_ecef(lat_deg, lon_deg, h_m):
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    e2 = WGS84_F * (2.0 - WGS84_F)
    n = WGS84_A / math.sqrt(1.0 - e2 * math.sin(lat) ** 2)
    pos = np.array([(n + h_m) * math.cos(lat) * math.cos(lon), (n + h_m) * math.cos(lat) * math.sin(lon),
                    (n * (1.0 - e2) + h_m) * math.sin(lat)])
    up = np.array([math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat)])
    return pos, up


_SITES = {}


def site(name):
    if name not in _SITES:
        c = table("dsn")["complexes"][name]
        _SITES[name] = geodetic_to_ecef(c["lat"], c["lon"], c["h_m"])
    return _SITES[name]


def elevation(name, r_earth, t, R=None):
    """Elevation (deg) of a point ``r_earth`` (m from Earth's centre, J2000) above complex ``name``."""
    R = rotation("IAU_Earth", "J2000", t) if R is None else R
    p, up = site(name)
    d = np.asarray(r_earth) - R @ p
    return math.degrees(math.asin(float(np.dot(d, R @ up)) / float(np.linalg.norm(d))))


def sep_deg(r_earth, t, sun=None):
    """Sun-Earth-probe angle (deg) of a vehicle at ``r_earth`` (m from Earth's centre, J2000)."""
    s = body_position("Sun", "Earth", t) if sun is None else sun
    c = float(np.dot(s, r_earth) / (np.linalg.norm(s) * np.linalg.norm(r_earth)))
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def scintillation_index(sep, band="X"):
    """810-005 106 eq. 7 (see data/comms/dsn.json for how it is read). A diagnostic only."""
    k = table("dsn")["solar_corona"][band]
    d = sep - k["theta_t_deg"]
    if d < 0:
        return 1.0
    return min(1.0, math.exp(-k["a1"] * d + k["a2"] + k["a3"] * d + k["a4"] * d * d))


def to_earth(r, central, t):
    """A position relative to ``central`` ("Earth", "Mars" or "Sun") as a position from Earth's centre."""
    if central == "Earth":
        return np.asarray(r, dtype=float)
    return np.asarray(r, dtype=float) + body_position(central, "Earth", t)


class Contact:
    """Contact state for one flight.

    ``passes_per_day`` is the booking: 3 books every complex's view (near continuous coverage, as a mission gets
    around critical events), 1 books Goldstone's pass only, 2 Goldstone and Canberra. Each booked pass is the
    complex's whole view period above the receive limit (6 deg) for downlink and the low-power transmit limit
    (10 deg) for uplink (810-005 301 sec. 2.2.2).
    """

    def __init__(self, passes_per_day=3, band="X"):
        self.booked = COMPLEXES[:max(0, min(3, int(passes_per_day)))]
        self.band = band
        d = table("dsn")
        self.lim = d["elevation_limit_deg"]
        self.cor = d["solar_corona"]
        self._cache = {}

    def state(self, r, central, t):
        """Contact at time ``t`` for a vehicle at ``r`` (m from ``central``, J2000)."""
        key = (round(float(t), 0), central)
        if key in self._cache:
            return self._cache[key]
        re = to_earth(r, central, t)
        R = rotation("IAU_Earth", "J2000", t)
        el = {c: elevation(c, re, t, R) for c in COMPLEXES}
        sep = sep_deg(re, t)
        down = [c for c in self.booked if el[c] >= self.lim["receive"]]
        up = [c for c in self.booked if el[c] >= self.lim["transmit_low_power"]]
        tlm_min = self.cor[self.band]["telemetry_min_sep_deg"]
        out = {"t": float(t), "elevation_deg": el, "sep_deg": sep, "range_m": float(np.linalg.norm(re)),
               "owlt_s": float(np.linalg.norm(re)) / 299792458.0,
               "moratorium": sep < self.cor["command_moratorium_sep_deg"],
               "downlink": bool(down) and sep >= tlm_min, "uplink": bool(up) and sep >= self.cor["command_moratorium_sep_deg"],
               "stations_down": down, "stations_up": up, "scintillation": scintillation_index(sep, self.band)}
        if len(self._cache) > 20000:
            self._cache.clear()
        self._cache[key] = out
        return out

    def windows(self, t0, t1, pos, step=600.0, key="uplink"):
        """Intervals in [t0, t1] where ``key`` holds, for a trajectory ``pos(t) -> (r, central)``."""
        out, start = [], None
        t = t0
        while t <= t1:
            r, c = pos(t)
            ok = self.state(r, c, t)[key]
            if ok and start is None:
                start = t
            if not ok and start is not None:
                out.append((start, t))
                start = None
            t += step
        if start is not None:
            out.append((start, t1))
        return out
