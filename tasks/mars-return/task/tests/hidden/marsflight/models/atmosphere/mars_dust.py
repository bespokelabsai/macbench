"""Dust over Mars, the ``dust.mars`` role: the column dust optical depth at a place and time.

``montabone`` (the default): the observed dust of real Mars years. Each world flies in one of Mars years 24 to 36
(1998 to 2023), picked with the world's own random stream; the time of the flight maps to that year by the
season (solar longitude Ls), so storms come in the season they really do (Ls 180 to 330). Three of the
thirteen years had a global storm (MY25, MY28, MY34).
  Data: the daily kriged column dust optical depth maps, version 2 (Montabone et al. 2015, Icarus 251, 65-95,
  doi:10.1016/j.icarus.2014.12.034; Montabone et al. 2020, JGR Planets 125, doi:10.1029/2019JE006111), built into
  ``data/mars/dust_montabone.npz`` by ``results/issue-113/build/make_dust.py``. License: Creative Commons
  Attribution-ShareAlike 3.0 Unported; the derived file keeps that license and this credit.
  Units: the visible optical depth of the whole column, the quantity landers measure (the maps' 9.3 micron
  absorption depth times 2.6, as the data page says), down to the 6 degree cell's mean ground, or down to a given
  spot's ground when its surface pressure is passed.
  A world drawn with an arrival storm (``Dispersions.arrival_storm``, a harder-world knob) gets one of the three
  real global storm years, with the storm's arrival at Jezero placed the drawn number of sols (0 to 30) before the
  vehicle reached Mars, instead of in its season.

``storm_draw``: today's ad hoc draw (``Dispersions.mars_tau``): optical depth 0.5 everywhere, plus 2 to 5 during
a storm drawn in one run in three. Kept for before-and-after runs.
"""
from __future__ import annotations

import math
import os

import numpy as np

from ...core.model import Provider
from ... import environment as E

SECONDS_PER_SOL = 88775.244
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "mars", "dust_montabone.npz")
STORM_YEARS = (25, 28, 34)
_DATA = None


def _load():
    global _DATA
    if _DATA is None:
        with np.load(DATA) as z:
            _DATA = {"years": [int(y) for y in z["years"]], "tau": z["tau"], "tau610": z["tau610"], "ls": z["ls"].astype(float),
                     "lat": z["lat"].astype(float), "lon": z["lon"].astype(float), "step": float(z["step"]),
                     "source": str(z["source"])}
            # Ls is not monotonic across the year boundary in a few files: unwrap it for the search
            _DATA["ls_unwrapped"] = np.unwrap(np.radians(_DATA["ls"]), axis=1) * 180.0 / math.pi
    return _DATA


def global_mean(year_index):
    """Area-weighted global mean optical depth per sol of one year (for finding storm onsets and for plots)."""
    d = _load()
    w = np.cos(np.radians(d["lat"]))[None, :, None]
    t = d["tau"][year_index].astype(float) * d["step"]
    return (t * w).sum(axis=(1, 2)) / (w.sum() * t.shape[2])


JEZERO = (18.4447, 77.4508)


def local_series(year_index, lat, lon):
    """Optical depth per sol of one year at one place (nearest 6 degree cell)."""
    d = _load()
    i = int(np.argmin(abs(d["lat"] - lat)))
    j = int(np.argmin(abs(((d["lon"] - lon + 180.0) % 360.0) - 180.0)))
    return d["tau"][year_index, :, i, j].astype(float) * d["step"]


def storm_onset(year_index, lat=JEZERO[0], lon=JEZERO[1]):
    """The sol a storm year's dust arrives at a place (Jezero by default): the first sol the local optical depth
    passes half way from the year's median to its peak there."""
    g = local_series(year_index, lat, lon)
    thr = np.median(g) + 0.5 * (g.max() - np.median(g))
    return int(np.argmax(g > thr))


class MontaboneDust(Provider):
    role = "dust.mars"
    name = "montabone"
    default = True

    def __init__(self, year=None):
        d = _load()
        self.d = d
        self.year = year if year is not None else d["years"][-1]
        self.k = d["years"].index(self.year)
        self.epoch0 = 0.0
        self.world = None
        self.storm = None        # (onset sol in the storm year, sols before arrival the storm began)
        self._cache = {}

    def configure(self, ctx):
        rng = ctx.rng(self.name)
        years = self.d["years"]
        self.year = int(years[int(rng.integers(len(years)))])
        storm_year = int(STORM_YEARS[int(rng.integers(len(STORM_YEARS)))])   # drawn always, used only in storm worlds
        self.world = ctx.world
        self.epoch0 = float(getattr(ctx, "epoch0", 0.0) or 0.0)
        arrival = getattr(ctx.world, "arrival_storm", None)
        if arrival:
            self.year = storm_year
            self.k = years.index(self.year)
            self.storm = (storm_onset(self.k), float(arrival[0]))
        self.k = years.index(self.year)

    def sol_of_year(self, t):
        """Fractional sol (0 to 668) in the world's dust year for TDB time t (s since J2000)."""
        if self.storm is not None and self.world is not None and getattr(self.world, "arrival_sol", None) is not None:
            onset, began = self.storm
            sol = (t - self.epoch0) / SECONDS_PER_SOL - (self.world.arrival_sol - began)
            if sol >= 0:
                return float(np.clip(onset + sol, 0, self.d["tau"].shape[1] - 1))
        ls = E.mars_ls(t)
        lsu = self.d["ls_unwrapped"][self.k]
        ls = ls + 360.0 * round((lsu[0] - ls) / 360.0) if ls < lsu[0] else ls
        return float(np.interp(ls, lsu, np.arange(lsu.size)))

    def optical_depth(self, lat, lon, t, surface_pressure=None):
        """Visible column dust optical depth at (lat, lon) in degrees (east longitude), TDB time t. Down to the grid
        cell's mean ground by default; with ``surface_pressure`` (Pa) of the spot, down to that spot's ground
        (the data page's rule: the 610 Pa value times surface pressure / 610)."""
        d = self.d
        if surface_pressure is not None:
            return self._interp(d["tau610"], lat, lon, t) * surface_pressure / 610.0
        # The flight asks at every integrator stage; dust changes over sols and hundreds of km, so the answer is
        # kept per 0.05 deg and 60 s: the value at the bin's centre, so the answer does not depend on call order.
        key = (round(lat * 20.0), round(lon * 20.0), round(t / 60.0), self.k)
        v = self._cache.get(key)
        if v is None:
            if len(self._cache) > 100000:
                self._cache.clear()
            v = self._cache[key] = self._interp(d["tau"], key[0] / 20.0, key[1] / 20.0, key[2] * 60.0)
        return v

    def _interp(self, grid, lat, lon, t):
        d = self.d
        s = self.sol_of_year(t)
        s0 = min(int(s), d["tau"].shape[1] - 2)
        fs = s - s0
        # grid: lat 87 .. -87 (descending), lon -178.5 .. 175.5, both 6 deg
        fi = (d["lat"][0] - lat) / 6.0
        fi = min(max(fi, 0.0), d["lat"].size - 1.000001)
        i0 = int(fi)
        a = fi - i0
        fj = ((lon - d["lon"][0]) % 360.0) / 6.0
        j0 = int(fj) % d["lon"].size
        j1 = (j0 + 1) % d["lon"].size
        b = fj - int(fj)
        out = 0.0
        for ss, ws in ((s0, 1.0 - fs), (s0 + 1, fs)):
            m = grid[self.k, ss]
            v = ((1 - a) * ((1 - b) * m[i0, j0] + b * m[i0, j1]) + a * ((1 - b) * m[i0 + 1, j0] + b * m[i0 + 1, j1]))
            out += ws * float(v)
        return out * d["step"]


class StormDrawDust(Provider):
    role = "dust.mars"
    name = "storm_draw"
    default = False

    def configure(self, ctx):
        self.disp = ctx.world
        self.epoch0 = float(getattr(ctx, "epoch0", 0.0) or 0.0)

    def optical_depth(self, lat, lon, t, surface_pressure=None):
        return self.disp.mars_tau((t - self.epoch0) / SECONDS_PER_SOL)


def mars_year(t):
    """The Mars year (Clancy et al. 2000 numbering, MY1 began 1955-04-11) at TDB time t, for checks against dated
    observations."""
    p = 686.9726 * 86400.0
    t24 = -46281600.0          # 1998-07-14 20:00 TDB, Ls 0 of MY24 by environment.mars_ls
    n = math.floor((t - t24) / p)
    start = t24 + n * p
    ls = E.mars_ls(t)
    frac = (t - start) / p
    if frac < 0.1 and ls > 300:
        n -= 1
    elif frac > 0.9 and ls < 60:
        n += 1
    return 24 + n


PROVIDERS = [MontaboneDust, StormDrawDust]
