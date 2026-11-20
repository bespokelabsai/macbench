"""Mars upper atmosphere for orbit decay: Mars-DTM, tabulated from Tudat's own implementation.

Mars-DTM is the Mars thermosphere model of Bruinsma and Lemoine (2002, J. Geophys. Res. 107(E10), 5085,
doi:10.1029/2001JE001508): total density from about 120 km up as a function of height, latitude, local solar
time, season (Ls) and solar activity (F10.7 at Mars), fitted to Mars Global Surveyor and Mars Odyssey drag
and accelerometer data. Tudat 1.0 ships it as ``environment_setup.atmosphere.mars_dtm()`` (BSD 3-clause) with its
coefficients in ``tudat-resources`` ``resource/atmosphere_tables/dtm_mars.dat`` (commit 007223c4, 70 x 25 numbers).

Why a table and not Tudat's model at run time:
- one call costs about 150 us, too slow inside a 60-day orbit coast (about 400,000 calls);
- Tudat 1.0 counts the season from a list of Mars year start dates that ends on 2028-08-17, so for every date
  after mid 2030 its season phase drifts by about 18 sols per Mars year (``marsDate::findNearestDate``);
- it subtracts the longitude in radians where Mars24 wants degrees (``computeLocalSolarTime``), so its local
  time is nearly the prime meridian's everywhere;
- it reads F10.7 from Tudat's space weather file and divides by a fixed 2.25.
``results/issue-80/build_mars_dtm_table.py`` therefore runs Tudat's unmodified model only inside the Mars year
its date list handles correctly (MY 39, 2026-09-30 to 2028-08-17), at chosen F10.7, and at the times of day that
give each local time at the prime meridian, and stores log10 density on a grid. This file reads the grid and
supplies the season, local time and Sun distance itself, with the Mars24 algorithm (Allison and McEwen 2000,
Planet. Space Sci. 48, 215) that Tudat's model also uses.

Grid: F10.7 at Mars 25 to 100 (6), Ls 0 to 345 deg (24), local solar time 0 to 22 h (12), latitude -90 to 90 deg
(13), height 120 to 1000 km (55); rows below 140 km are not used (see ``H_MIN``). Values are log10(kg/m3) x 1000 as int16 (0.23% resolution).
"""
from __future__ import annotations

import bisect
import math
import os

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
TABLE = os.path.join(HERE, "mars_dtm_table.npz")

# Mars24 (Allison and McEwen 2000; https://www.giss.nasa.gov/tools/mars24/help/algorithm.html), table 5
_PBS = ((0.0071, 2.2353, 49.409), (0.0057, 2.7543, 168.173), (0.0039, 1.1177, 191.837), (0.0037, 15.7866, 21.736),
        (0.0021, 2.1354, 15.704), (0.0020, 2.4694, 95.528), (0.0018, 32.8493, 49.095))
# Mars north pole in J2000, IAU 2009 (pck00010.tpc, as loaded by the simulator): RA, Dec and their rates per century
POLE_RA0, POLE_RA1, POLE_DEC0, POLE_DEC1 = 317.68143, -0.1061, 52.88650, -0.0609
AU = 1.495978707e11
H_MIN = 140e3      # Mars-DTM starts at 138 km (its temperature profile is referred to 138 km); Tudat's model gives
                   # no density or a negative temperature below about 135 km, so the table rows under 140 km are
                   # not used


def mars24(t):
    """Season and clock of Mars at ``t`` (s TDB since J2000): (Ls deg, equation of time deg, mean solar time at
    the prime meridian in hours, Sun distance in AU)."""
    d = t / 86400.0                                   # days since J2000 (TT, close enough to TDB)
    M = math.radians(19.3871 + 0.52402073 * d)
    a_fms = 270.3871 + 0.524038496 * d
    pbs = sum(A * math.cos(math.radians(0.985626 * d / tau + phi)) for A, tau, phi in _PBS)
    eoc = ((10.691 + 3.0e-7 * d) * math.sin(M) + 0.623 * math.sin(2 * M) + 0.050 * math.sin(3 * M)
           + 0.005 * math.sin(4 * M) + 0.0005 * math.sin(5 * M) + pbs)
    ls = (a_fms + eoc) % 360.0
    L = math.radians(ls)
    eot = 2.861 * math.sin(2 * L) - 0.071 * math.sin(4 * L) + 0.002 * math.sin(6 * L) - eoc
    mst = (24.0 * ((d + 2451545.0 - 2451549.5) / 1.0274912517 + 44796.0 - 0.0009626)) % 24.0
    r = 1.52367934 * (1.00436 - 0.09309 * math.cos(M) - 0.004336 * math.cos(2 * M) - 0.00031 * math.cos(3 * M)
                      - 0.00003 * math.cos(4 * M))
    return ls, eot, mst, r


def local_true_solar_time(t, east_lon_deg):
    """Local true solar time (hours) at an east longitude, Mars24."""
    _, eot, mst, _ = mars24(t)
    return (mst + east_lon_deg / 15.0 + eot / 15.0) % 24.0


def mars_pole(t):
    """Unit vector of Mars's north pole in J2000."""
    T = t / (86400.0 * 36525.0)
    ra, dec = math.radians(POLE_RA0 + POLE_RA1 * T), math.radians(POLE_DEC0 + POLE_DEC1 * T)
    return np.array([math.cos(dec) * math.cos(ra), math.cos(dec) * math.sin(ra), math.sin(dec)])


def lat_lst(r, sun, pole):
    """Areocentric latitude (deg) and local true solar time (h) of a Mars-centred position ``r`` (J2000), from
    the direction to the Sun ``sun`` (Mars-centred, J2000) and the pole: the hour angle from the Sun's meridian."""
    # plain floats: this runs inside the integrator, where numpy's per-call cost on 3-vectors dominates
    r0, r1, r2 = float(r[0]), float(r[1]), float(r[2])
    s0, s1, s2 = float(sun[0]), float(sun[1]), float(sun[2])
    p0, p1, p2 = float(pole[0]), float(pole[1]), float(pole[2])
    rp = r0 * p0 + r1 * p1 + r2 * p2
    rn = math.sqrt(r0 * r0 + r1 * r1 + r2 * r2)
    lat = math.degrees(math.asin(max(-1.0, min(1.0, rp / rn))))
    sp = s0 * p0 + s1 * p1 + s2 * p2
    # hour angle: atan2((s x r) . p, s . r - (s . p)(r . p)), the projections on the equator
    cross_p = (s1 * r2 - s2 * r1) * p0 + (s2 * r0 - s0 * r2) * p1 + (s0 * r1 - s1 * r0) * p2
    dot = s0 * r0 + s1 * r1 + s2 * r2 - sp * rp
    h = math.degrees(math.atan2(cross_p, dot))
    return lat, (12.0 + h / 15.0) % 24.0


class MarsDtmTable:
    """Mars-DTM total density from the stored grid. ``density(h, lat, lst, ls, f_mars)``, SI and degrees."""

    def __init__(self, path=TABLE):
        z = np.load(path)
        self.F = z["f_mars"].astype(float)
        self.Ls = z["ls"].astype(float)
        self.LT = z["lst"].astype(float)
        self.lat = z["lat"].astype(float)
        self.h = z["alt_m"].astype(float)
        self.logrho = z["log10_rho_milli"].astype(float) / 1000.0     # (F, Ls, LT, lat, h)
        self.top_slope = (self.logrho[..., -1] - self.logrho[..., -2]) / (self.h[-1] - self.h[-2])
        self._slice_key = None
        self._slice = None
        self._hl = self.h.tolist()
        self._ltl = self.LT.tolist()
        self._latl = self.lat.tolist()

    @staticmethod
    def _bracket(grid, x, periodic=None):
        """Index and weight for linear interpolation; ``periodic`` is the period for a wrapping axis."""
        n = len(grid)
        if periodic:
            step = grid[1] - grid[0]
            u = (x % periodic) / step
            i = int(math.floor(u)) % n
            return i, (i + 1) % n, u - math.floor(u)
        if x <= grid[0]:
            return 0, 1, 0.0
        if x >= grid[-1]:
            return n - 2, n - 1, 1.0
        i = bisect.bisect_left(grid, x) - 1
        return i, i + 1, (x - grid[i]) / (grid[i + 1] - grid[i])

    def season_slice(self, ls, f_mars):
        """log10 density on (LT, lat, h) for one season and solar flux, interpolated linearly."""
        f0, f1, wf = self._bracket(self.F, f_mars)
        l0, l1, wl = self._bracket(self.Ls, ls, periodic=360.0)
        a = (1 - wf) * self.logrho[f0] + wf * self.logrho[f1]
        return (1 - wl) * a[l0] + wl * a[l1]

    def log10_density(self, h, lat, lst, ls, f_mars, cache_key=None):
        if cache_key is None or cache_key != self._slice_key:
            self._slice = self.season_slice(ls, f_mars).tolist()     # nested lists: fast to index in Python
            self._slice_key = cache_key
        s = self._slice
        t0, t1, wt = self._bracket(self._ltl, lst, periodic=24.0)
        a0, a1, wa = self._bracket(self._latl, lat)
        hl = self._hl
        if h >= hl[-1]:
            k0, k1, wh, extra = len(hl) - 2, len(hl) - 1, 1.0, h - hl[-1]
        else:
            hh = max(h, hl[0])
            k1 = max(1, bisect.bisect_left(hl, hh))
            k0 = k1 - 1
            wh, extra = (hh - hl[k0]) / (hl[k1] - hl[k0]), 0.0
        v = 0.0
        for ti, wti in ((t0, 1 - wt), (t1, wt)):
            for ai, wai in ((a0, 1 - wa), (a1, wa)):
                row = s[ti][ai]
                v += wti * wai * (row[k0] + wh * (row[k1] - row[k0]))
        if extra:
            # above 1000 km: keep the top scale height (density is far below anything that matters)
            v += extra * (s[t0][a0][-1] - s[t0][a0][-2]) / (hl[-1] - hl[-2])
        return v

    def density(self, h, lat, lst, ls, f_mars, cache_key=None):
        if h < H_MIN:
            # below Mars-DTM's base (138 km): keep the scale height of 140 to 150 km
            v0 = self.log10_density(H_MIN, lat, lst, ls, f_mars, cache_key)
            v1 = self.log10_density(H_MIN + 10e3, lat, lst, ls, f_mars, cache_key)
            return 10.0 ** (v0 + (v0 - v1) * (H_MIN - h) / 10e3)
        return 10.0 ** self.log10_density(h, lat, lst, ls, f_mars, cache_key)
