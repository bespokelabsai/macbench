"""The ground at Jezero, the ``terrain.mars`` role.

``jezero_hirise`` (the default): ``data/mars/jezero_20m.npz``, 20 m cells over the same 32 by 30 km as today, built
by ``results/issue-113/build/make_jezero_terrain.py`` from three USGS products (U.S. public domain, CC0):
- Mars 2020 TRN HiRISE DTM mosaic, 1 m (Fergason et al. 2020, doi:10.5066/P9REJ9JN),
- Mars Sample Return TRN HiRISE DTM mosaic, 1 m (Bland et al. 2024, doi:10.5066/P13CPYYU), which adds the delta
  and the western rim,
- Mars 2020 TRN CTX DTM mosaic, 20 m, where neither HiRISE mosaic reaches.
Layers per cell: ``elev`` (m above the MOLA areoid), ``rough_deg`` (scatter of the lander-scale slope about the
slope the 20 m grid resolves), ``rock_k`` (rock cover, a model calibrated to Mars 2020's landing statistics; no
public rock map of Jezero exists), ``steep15`` (measured share of 1 m pixels steeper than 15 deg) and ``src``.
A touchdown is drawn the same way as today (``terrain.touchdown``): the resolved slope plus a normal draw with the
cell's scatter, and the tallest rock under the footprint from the Golombek and Rapp (1997) law, both fixed by a hash
of the touchdown spot, so the same spot always gives the same ground in every world.

``jezero_50m``: today's map (``terrain_data/jezero.npz`` through ``terrain.py``), for before-and-after runs.

Angles in degrees here, as ``terrain.py`` (the role's table in core/model.py says radians for ``slope``;
``slope_rad`` gives that).
"""
from __future__ import annotations

import math
import os

from ...core.model import Provider
from ... import terrain as T

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "mars", "jezero_20m.npz")


class JezeroHirise(Provider):
    role = "terrain.mars"
    name = "jezero_hirise"
    default = True
    path = DATA
    _map = None

    @classmethod
    def map(cls):
        if cls._map is None or cls._map_path != cls.path:
            m = T.Map(cls.path)
            import numpy as np
            with np.load(cls.path) as z:
                m.steep = z["steep15"].astype(float) * float(z["steep15_scale"]) if "steep15" in z else None
                m.src = z["src"] if "src" in z else None
            cls._map, cls._map_path = m, cls.path
        return cls._map

    def elevation(self, lat, lon):
        m = self.map()
        ij = m.index(lat, lon)
        return None if ij is None else m.bilinear(m.elev, *ij)

    def lowest(self):
        return self.map().lo

    def highest(self):
        return self.map().hi

    def slope(self, lat, lon):
        """Slope the 20 m grid resolves, degrees."""
        m = self.map()
        ij = m.index(lat, lon)
        if ij is None:
            return None
        de, dn = m.gradient(*ij, lat, "Mars")
        return math.degrees(math.atan(math.hypot(de, dn)))

    def slope_rad(self, lat, lon):
        s = self.slope(lat, lon)
        return None if s is None else math.radians(s)

    def steep_share(self, lat, lon):
        """Measured share of 1 m pixels steeper than 15 deg in the cell, or None off HiRISE."""
        m = self.map()
        ij = m.index(lat, lon)
        if ij is None or m.steep is None:
            return None
        v = m.cell(m.steep, *ij)
        return None if v > 1.0 else v

    def rock_height(self, lat, lon, footprint_m=3.0):
        g = self.touchdown(lat, lon, footprint_m)
        return None if g is None else g["rock_m"]

    def touchdown(self, lat, lon, footprint_m):
        """What a lander of diameter footprint_m meets at (lat, lon), as ``terrain.touchdown`` does for today's map."""
        m = self.map()
        ij = m.index(lat, lon)
        if ij is None:
            return None
        u = T._uniforms("Mars", lat, lon, 3)
        de, dn = m.gradient(*ij, lat, "Mars")
        rough = m.cell(m.rough, *ij)
        z = math.sqrt(-2.0 * math.log(u[0]))
        t = math.tan(math.radians(rough))
        se, sn = de + t * z * math.cos(2 * math.pi * u[1]), dn + t * z * math.sin(2 * math.pi * u[1])
        k = m.cell(m.rock, *ij)
        return {"ground_alt_m": m.bilinear(m.elev, *ij), "slope_deg": math.degrees(math.atan(math.hypot(se, sn))),
                "resolved_slope_deg": math.degrees(math.atan(math.hypot(de, dn))), "rough_deg": rough,
                "rock_abundance": k, "rock_m": T.largest_rock(k, math.pi * footprint_m ** 2 / 4.0, u[2])}


class Jezero50m(Provider):
    role = "terrain.mars"
    name = "jezero_50m"
    default = False

    def elevation(self, lat, lon):
        return T.height("Mars", lat, lon)

    def lowest(self):
        return T.lowest("Mars")

    def highest(self):
        return T.highest("Mars")

    def slope(self, lat, lon):
        g = T.touchdown("Mars", lat, lon, 1.0)
        return None if g is None else g["resolved_slope_deg"]

    def slope_rad(self, lat, lon):
        s = self.slope(lat, lon)
        return None if s is None else math.radians(s)

    def rock_height(self, lat, lon, footprint_m=3.0):
        g = T.touchdown("Mars", lat, lon, footprint_m)
        return None if g is None else g["rock_m"]

    def touchdown(self, lat, lon, footprint_m):
        return T.touchdown("Mars", lat, lon, footprint_m)


PROVIDERS = [JezeroHirise, Jezero50m]
