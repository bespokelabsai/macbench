"""Onboard hazard map of Jezero for the landing divert.

gnc/jezero_hazard.npz holds, for every 20 m cell of the simulator's Jezero map (the HiRISE map), the chance that the lander
tips over on a slope or breaks on a rock when it touches down at a random spot in the cell
(tools/hazard_map.py builds it). After the terrain-relative navigation fix the navigated position is good
to about 13 m, so the powered descent can aim at a chosen cell. It keeps its natural stopping point when that
cell is safe, and otherwise moves to the nearest safe cell within the divert limit.
"""
from __future__ import annotations

import math
import os

import numpy as np

from . import mission as M

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jezero_hazard.npz")


class HazardMap:
    def __init__(self, path=PATH):
        with np.load(path) as z:
            self.lat0, self.lon0 = float(z["lat0"]), float(z["lon0"])
            self.dlat, self.dlon = float(z["dlat"]), float(z["dlon"])
            self.R = float(z["r_body"])
            self.p = 10.0 ** (z["p_code"] / float(z["codes_per_decade"]) + float(z["log_p_min"]))
        self.n_lat, self.n_lon = self.p.shape
        # The touchdown scatters by LAND_SIGMA_M per axis about the aim: the share of it in the cell d cells away
        # along an axis, out to 2.5 sigma (one neighbor each side on the old 50 m map, two on the 20 m one).
        s = M.LAND_SIGMA_M / (self.dlat * math.pi / 180 * self.R)
        self.k = max(1, int(math.ceil(2.5 * s)))

        def phi(x):
            return 0.5 * math.erfc(-x / (s * math.sqrt(2.0)))
        self.w = np.array([phi(d + 0.5) - phi(d - 0.5) for d in range(-self.k, self.k + 1)])

    def p_aim(self, i, j):
        """Failure chance of a touchdown aimed at the center of cell (i, j), with the touchdown scattered
        by LAND_SIGMA_M per axis: the cells within self.k of it, weighted by where the lander comes down."""
        k = self.k
        i0, i1, j0, j1 = max(i - k, 0), min(i + k + 1, self.n_lat), max(j - k, 0), min(j + k + 1, self.n_lon)
        w = np.outer(self.w[i0 - i + k:i1 - i + k], self.w[j0 - j + k:j1 - j + k])
        return float((w * self.p[i0:i1, j0:j1]).sum() / w.sum())

    def ground_m(self, lat, lon, lat_ref, lon_ref):
        """(north, east) ground distance in m from (lat_ref, lon_ref) to (lat, lon)."""
        dn = math.radians(lat - lat_ref) * self.R
        de = math.radians((lon - lon_ref + 180.0) % 360.0 - 180.0) * self.R * math.cos(math.radians(lat_ref))
        return dn, de

    def choose(self, lat, lon, radius_m):
        """The landing cell for the natural stopping point (lat, lon), among the cells whose centers lie within
        radius_m of it: the nearest one whose failure chance is below P_SAFE, or if none is, the one with the
        lowest chance. So the descent moves only when it has to, and only as far as it has to. Returns None
        when (lat, lon) is off the map."""
        i_c = (lat - self.lat0) / self.dlat
        j_c = ((lon - self.lon0 + 180.0) % 360.0 - 180.0) / self.dlon
        if not (0.0 <= i_c <= self.n_lat - 1 and 0.0 <= j_c <= self.n_lon - 1):
            return None
        i_n, j_n = int(round(i_c)), int(round(j_c))
        k = int(radius_m / (self.dlat * math.pi / 180 * self.R)) + 2
        safe = least = None
        for i in range(max(i_n - k, 0), min(i_n + k + 1, self.n_lat)):
            for j in range(max(j_n - k, 0), min(j_n + k + 1, self.n_lon)):
                la, lo = self.lat0 + i * self.dlat, self.lon0 + j * self.dlon
                d = math.hypot(*self.ground_m(la, lo, lat, lon))
                if d > radius_m:
                    continue
                c = (d, self.p_aim(i, j), i, j, la, lo)
                if c[1] < M.P_SAFE and (safe is None or d < safe[0]):
                    safe = c
                if least is None or c[1] < least[1]:
                    least = c
        best = safe or least
        if best is None:
            return None
        d, p, i, j, la, lo = best
        return {"lat": la, "lon": lo, "cell": (i, j), "p_aim": p, "p_cell": float(self.p[i, j]),
                "p_natural": self.p_aim(i_n, j_n), "divert_m": d}
