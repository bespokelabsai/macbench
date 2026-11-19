"""Space weather for the Earth atmosphere models, from CelesTrak's file frozen in ``data/earth/``.

Nothing is downloaded at run time. ``data/earth/SW-All.csv.gz`` is CelesTrak's ``SW-All.csv`` as fetched on
Oct 27, 2026: observed daily values from Oct 1957 to Oct 26, 2026, then NOAA SWPC's monthly forecast of the
solar flux to Oct 2041 (rows marked PRM). The geomagnetic indices (Kp, ap) come from GFZ Potsdam (CC BY 4.0);
the solar flux F10.7 from the Dominion Radio Astrophysical Observatory, Penticton (NRC Canada, open data).

The mission flies in 2029 to 2041, after the observed record ends. So each world gets a daily record of its
own, built from real days:
- The 81-day mean solar flux follows the forecast for the date (``F10.7_OBS_CENTER81`` of the PRM rows).
- The day-to-day flux and the 3-hourly geomagnetic index ``ap`` are copied from 27-day blocks (one solar
  rotation) of the observed record, each drawn at random from the blocks whose 81-day mean flux was within 15%
  of the forecast level. The day's flux is the block day's flux over its own 81-day mean, times the forecast
  level, so storms and quiet spells keep their real shape and size.
This is the method ``environment.write_space_weather`` uses for NRLMSISE-00 today, applied to the newer
CelesTrak file. That one reads Tudat's copy, whose forecast was made in mid 2025.

Times are seconds TDB since J2000, as Tudat passes them.
"""
from __future__ import annotations

import gzip
import os

import numpy as np

from ...core.model import Provider

DATA = os.path.join(os.path.dirname(__file__), "..", "..", "data", "earth", "SW-All.csv.gz")
FIRST_DAY = np.datetime64("2026-11-01")    # the built record covers this day ...
LAST_DAY = np.datetime64("2041-10-01")     # ... to this one; outside it the nearest day is used
BLOCK = 27
LEVEL_TOLERANCE = 0.15
J2000_UTC = np.datetime64("2000-01-01T11:58:55.816", "ms")
TT_MINUS_UTC_SINCE_2017 = 69.184 - 64.184   # leap seconds added since 2000 (5, the last at the end of 2016)

_RECORD = None


def tdb_to_utc(t):
    """Seconds TDB since J2000 to numpy datetime64 (UTC), good to well under a second after 2017."""
    return J2000_UTC + np.asarray(np.round((np.asarray(t, float) - TT_MINUS_UTC_SINCE_2017) * 1e3), "timedelta64[ms]")


def record(path=DATA):
    """The CelesTrak file, parsed once: observed days and the monthly forecast."""
    global _RECORD
    if _RECORD is None or _RECORD["path"] != path:
        obs = {k: [] for k in ("date", "ap", "Ap", "f107", "c81")}
        fc_date, fc_c81 = [], []
        with gzip.open(path, "rt") as f:
            next(f)
            for line in f:
                p = line.rstrip("\n").split(",")
                kind = p[26]
                if kind == "OBS" and p[12] and p[24] and p[27]:
                    obs["date"].append(p[0])
                    obs["ap"].append([int(x) for x in p[12:20]])
                    obs["Ap"].append(int(p[20]))
                    obs["f107"].append(float(p[24]))
                    obs["c81"].append(float(p[27]))
                elif kind in ("PRD", "PRM") and p[27]:
                    fc_date.append(p[0])
                    fc_c81.append(float(p[27]))
        _RECORD = {"path": path, "date": np.array(obs["date"], "datetime64[D]"),
                   "ap": np.array(obs["ap"], float), "Ap": np.array(obs["Ap"], float),
                   "f107": np.array(obs["f107"]), "c81": np.array(obs["c81"]),
                   "fc_date": np.array(fc_date, "datetime64[D]"), "fc_c81": np.array(fc_c81)}
    return _RECORD


class SpaceWeather:
    """One world's daily solar flux and 3-hourly ap for the mission years, drawn with ``rng``."""

    def __init__(self, rng, first=FIRST_DAY, last=LAST_DAY, path=DATA):
        rec = record(path)
        self.days = np.arange(first, last + np.timedelta64(1, "D"))
        x = self.days.astype(float)
        self.level = np.interp(x, rec["fc_date"].astype(float), rec["fc_c81"])
        n = len(self.days)
        # Blocks start late enough that the block's ap history (57 h) is observed too, and end in the record.
        o_c81 = rec["c81"]
        starts = np.arange(3, len(o_c81) - BLOCK)
        f107 = np.empty(n)
        ap3 = np.empty((n, 8))
        Ap = np.empty(n)
        self.source = []
        i = 0
        while i < n:
            lev = self.level[i]
            near = starts[np.abs(o_c81[starts] / lev - 1.0) < LEVEL_TOLERANCE]
            if len(near) < BLOCK:
                near = starts[np.argsort(np.abs(o_c81[starts] - lev))[:200]]
            j0 = int(rng.choice(near))
            k = min(BLOCK, n - i)
            src = slice(j0, j0 + k)
            f107[i:i + k] = self.level[i:i + k] * rec["f107"][src] / o_c81[src]
            ap3[i:i + k] = rec["ap"][src]
            Ap[i:i + k] = rec["Ap"][src]
            self.source.append((str(self.days[i]), str(rec["date"][j0])))
            i += k
        self.f107, self.Ap, self.ap3 = f107, Ap, ap3
        self._ap_flat = ap3.reshape(-1)

    def _day(self, d):
        return np.clip((d - self.days[0]).astype("timedelta64[D]").astype(int), 0, len(self.days) - 1)

    def indices(self, t):
        """MSIS inputs at TDB time(s) ``t``: (f107 of the day before, 81-day mean f107 of the day,
        ap array n x 7 as MSIS takes it: daily Ap, the 3-hour ap now, 3, 6 and 9 h before, and the means over
        12 to 33 h and 36 to 57 h before)."""
        utc = np.atleast_1d(tdb_to_utc(t))
        day = self._day(utc.astype("datetime64[D]"))
        f107 = self.f107[np.maximum(day - 1, 0)]
        f107a = self.level[day]
        hours = (utc - self.days[0]).astype("timedelta64[h]").astype(int)
        slot = np.clip(hours // 3, 0, len(self._ap_flat) - 1)
        a = self._ap_flat
        get = lambda k: a[np.clip(slot - k, 0, None)]  # noqa: E731
        ap = np.column_stack([self.Ap[day], get(0), get(1), get(2), get(3),
                              np.mean([get(k) for k in range(4, 12)], axis=0),
                              np.mean([get(k) for k in range(12, 20)], axis=0)])
        return f107, f107a, ap


class CelestrakSpaceWeather(Provider):
    """The ``space_weather`` role: daily indices for the mission years from the frozen CelesTrak file."""
    role = "space_weather"
    name = "celestrak_frozen"
    default = True

    def configure(self, ctx):
        self.sw = SpaceWeather(ctx.rng("earth_space_weather"))

    def indices(self, t):
        f107, f107a, ap = self.sw.indices(t)
        return {"f107": float(f107[0]), "f107a": float(f107a[0]), "ap": float(ap[0, 0]), "ap_array": ap[0].tolist()}


PROVIDERS = [CelestrakSpaceWeather]
