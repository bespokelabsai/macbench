"""Terrain at the landing sites: ground height, and the slope and rocks a lander meets at touchdown.

Each site has a map in marsflight/terrain_data/ (our validation notes, not included, say how they were made):
- jezero.npz: Jezero crater, Mars.
- utah.npz: the Utah Test and Training Range, Earth.

A map is a regular latitude and longitude grid of ground height above the body's datum (Mars: the MOLA
areoid, which the simulator places at its 3389.5 km reference sphere; Earth: the WGS84 ellipsoid), and,
where it has them, two hazard layers per cell:
- rough_deg: the scatter of the lander-scale slope about the slope the height grid resolves (deg, one
  sigma per horizontal component).
- rock_k: the fraction of the ground covered by rocks (Golombek and Rapp 1997).
Outside every map the ground is flat at the site altitude (data.SITES). At Utah each spot is also either
the soft dry lakebed or hard ground (`surface`).

What a lander touches at one spot is drawn from these statistics with a hash of the spot itself, so the
same spot always has the same slope and the same rocks, in every world: the rocks do not move.
"""
from __future__ import annotations

import hashlib
import math
import os

import numpy as np

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "terrain_data")
FILES = {"Mars": "jezero.npz", "Earth": "utah.npz"}
R_BODY = {"Mars": 3396190.0, "Earth": 6378137.0}    # the spheres the maps' degrees are measured on
ROCK_HEIGHT_PER_DIAMETER = 0.5      # rock height is about half its diameter (Golombek et al. 2003)
SPOT_DEG = 1e-5                     # the hash grid of touchdown spots: about 0.6 m at Mars, 1.1 m at Earth
# Utah: the range's dry lakebed is sandy clay that a capsule sinks into, which takes up its impact; a
# passive (crushable) capsule is designed for it (Kellas, "Passive Earth Entry Vehicle Landing Test",
# NTRS 20170002221, p. 2; Mitcheltree et al., NTRS 20010059026). The lakebed is where the map's ground is
# below LAKEBED_MAX_ALT_M (above the WGS84 ellipsoid) and flatter than LAKEBED_MAX_SLOPE_DEG at its cell
# scale. Above it are the alluvial fans and ranges, gravel and rock; within it, the steep spots are bedrock
# outcrops and old islands, while gentle rises are sand and low ridges of the lake floor. The cutoffs are
# this model's: the lakebed floor lies at 1,267 to 1,290 m on the map and the fans rise from there.
LAKEBED_MAX_ALT_M = 1300.0
LAKEBED_MAX_SLOPE_DEG = 5.0
_MAPS = {}


class Map:
    def __init__(self, path):
        with np.load(path) as z:
            def layer(name):
                if name not in z:
                    return None
                scale = float(z[name.split("_")[0] + "_scale"]) if name.split("_")[0] + "_scale" in z else 1.0
                return np.asarray(z[name], dtype=float) * scale
            self.lat0, self.lon0 = float(z["lat0"]), float(z["lon0"])
            self.dlat, self.dlon = float(z["dlat"]), float(z["dlon"])
            self.elev = layer("elev")
            self.rough = layer("rough_deg")
            self.rock = layer("rock_k")
            self.source = str(z["source"]) if "source" in z else ""
        self.n_lat, self.n_lon = self.elev.shape
        self.lo, self.hi = float(self.elev.min()), float(self.elev.max())

    def index(self, lat, lon):
        """Fractional grid indices of (lat, lon), or None outside the map."""
        i = (lat - self.lat0) / self.dlat
        j = ((lon - self.lon0 + 180.0) % 360.0 - 180.0) / self.dlon
        if not (0.0 <= i <= self.n_lat - 1 and 0.0 <= j <= self.n_lon - 1):
            return None
        return i, j

    def bilinear(self, grid, i, j):
        i0, j0 = min(int(i), self.n_lat - 2), min(int(j), self.n_lon - 2)
        fi, fj = i - i0, j - j0
        g = grid[i0:i0 + 2, j0:j0 + 2]
        return float((g[0, 0] * (1 - fj) + g[0, 1] * fj) * (1 - fi) + (g[1, 0] * (1 - fj) + g[1, 1] * fj) * fi)

    def cell(self, grid, i, j):
        return float(grid[int(round(i)), int(round(j))])

    def gradient(self, i, j, lat, body):
        """Ground slope the height grid resolves: (east, north) rise per meter."""
        i0, j0 = min(int(i), self.n_lat - 2), min(int(j), self.n_lon - 2)
        fi, fj = i - i0, j - j0
        g = self.elev[i0:i0 + 2, j0:j0 + 2]
        m_lat = math.radians(self.dlat) * R_BODY[body]
        m_lon = math.radians(self.dlon) * R_BODY[body] * math.cos(math.radians(lat))
        de = ((g[0, 1] - g[0, 0]) * (1 - fi) + (g[1, 1] - g[1, 0]) * fi) / m_lon
        dn = ((g[1, 0] - g[0, 0]) * (1 - fj) + (g[1, 1] - g[0, 1]) * fj) / m_lat
        return float(de), float(dn)


def get_map(body):
    """The terrain map of a body's landing site, or None when there is none."""
    if body not in _MAPS:
        path = os.path.join(HERE, FILES.get(body, ""))
        _MAPS[body] = Map(path) if body in FILES and os.path.isfile(path) else None
    return _MAPS[body]


def height(body, lat, lon):
    """Ground height (m above the datum) at (lat, lon), or None outside the map."""
    m = get_map(body)
    ij = m.index(lat, lon) if m is not None else None
    return None if ij is None else m.bilinear(m.elev, *ij)


def lowest(body):
    """The lowest ground on the map, or None when there is no map."""
    m = get_map(body)
    return None if m is None else m.lo


def highest(body):
    m = get_map(body)
    return None if m is None else m.hi


def _uniforms(body, lat, lon, n):
    """n uniform numbers in (0, 1) fixed by the touchdown spot (about 1 m), the same in every world."""
    key = f"{body}:{round(lat / SPOT_DEG)}:{round(lon / SPOT_DEG)}".encode()
    raw = hashlib.blake2b(key, digest_size=8 * n).digest()
    return [(int.from_bytes(raw[8 * k:8 * k + 8], "little") + 0.5) / 2.0 ** 64 for k in range(n)]


def rocks_per_m2(k, d):
    """Golombek and Rapp (1997): rocks per m^2 with diameter at least d (m), for rock abundance k.
    The cumulative area they cover is F(D) = k exp(-q D), q = 1.79 + 0.152 / k, so the number density
    is n(x) = 4 k q exp(-q x) / (pi x^2), and its integral from d up is the expression below."""
    if k <= 0:
        return 0.0
    from scipy.special import exp1
    q = 1.79 + 0.152 / k
    return 4.0 * k * q / math.pi * (math.exp(-q * d) / d - q * float(exp1(q * d)))


def largest_rock(k, area_m2, u):
    """Height (m) of the tallest rock under a footprint of area_m2, at cumulative probability u: the
    number of rocks larger than D under the footprint is Poisson with mean rocks_per_m2(k, D) * area."""
    if k <= 0 or area_m2 <= 0:
        return 0.0
    target = -math.log(u) / area_m2            # P(none larger than D) = u  =>  N(D) * A = -ln u
    lo, hi = 0.02, 20.0
    if rocks_per_m2(k, lo) <= target:
        return 0.0
    for _ in range(60):
        mid = math.sqrt(lo * hi)
        if rocks_per_m2(k, mid) > target:
            lo = mid
        else:
            hi = mid
    return ROCK_HEIGHT_PER_DIAMETER * math.sqrt(lo * hi)


def touchdown(body, lat, lon, footprint_m):
    """What a lander of diameter footprint_m meets at (lat, lon): the ground slope at its own scale and
    the tallest rock under it. Returns None outside the map."""
    m = get_map(body)
    ij = m.index(lat, lon) if m is not None else None
    if ij is None:
        return None
    u = _uniforms(body, lat, lon, 3)
    de, dn = m.gradient(*ij, lat, body)
    rough = m.cell(m.rough, *ij) if m.rough is not None else 0.0
    # lander-scale slope: the resolved slope plus the sub-cell part, a normal draw per horizontal component
    z = math.sqrt(-2.0 * math.log(u[0]))
    t = math.tan(math.radians(rough))
    se, sn = de + t * z * math.cos(2 * math.pi * u[1]), dn + t * z * math.sin(2 * math.pi * u[1])
    slope = math.degrees(math.atan(math.hypot(se, sn)))
    k = m.cell(m.rock, *ij) if m.rock is not None else 0.0
    rock = largest_rock(k, math.pi * footprint_m ** 2 / 4.0, u[2])
    out = {"ground_alt_m": m.bilinear(m.elev, *ij), "slope_deg": slope,
           "resolved_slope_deg": math.degrees(math.atan(math.hypot(de, dn))), "rough_deg": rough,
           "rock_abundance": k, "rock_m": rock}
    if body == "Earth":
        out["surface"] = ("lakebed" if out["ground_alt_m"] < LAKEBED_MAX_ALT_M and out["resolved_slope_deg"] < LAKEBED_MAX_SLOPE_DEG
                          else "hard")
    return out
