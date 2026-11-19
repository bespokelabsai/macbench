"""Earth winds from measured profiles, the ``winds.earth`` role.

Each world flies real wind profiles: for every day of the flight and each launch or landing site, one profile
drawn from a bank of measured ones taken within 15 days of the same day of the year. The bank is
``data/earth/winds_igra.npz``: balloon soundings from NOAA's Integrated Global Radiosonde Archive (IGRA 2,
public domain) at Cape Canaveral (station USM00074794, for Kennedy), Vandenberg (USM00072393) and Salt Lake
City (USM00072572, for the Utah landing), 2005 to 2025, 0 to 30 km every 250 m. ``results/issue-112/
build_wind_bank.py`` builds it. An ERA5 bank (Copernicus, CC-BY 4.0) in the same format can replace it once the
Climate Data Store key is set up.

- The draw uses the world's own stream (the seed and this provider's name), and the day, so it does not depend
  on the order of calls and moves no other draw.
- The nominal world (no seed) flies the mean profile of its 31-day window.
- A point uses the nearest site's profile. Above the bank's top (30 km) the top wind is held to 35 km and then
  falls linearly to nothing at 60 km, where the old jet also stopped (a known limit: ERA5 reaches 48 km).
- No gusts here; the NASA discrete gust can be added on top (see ``docs/reference/winds-earth.md``).
"""
from __future__ import annotations

import bisect
import math
import os
import zlib

import numpy as np

from ...core.model import Provider

BANK = os.path.join(os.path.dirname(__file__), "..", "..", "data", "earth", "winds_igra.npz")
WINDOW_DAYS = 15
HOLD_TO = 35000.0
ZERO_AT = 60000.0
_BANKS = {}


def load_bank(path=BANK):
    """{site: dict(u, v (n x levels, m/s), doy, hour, year, lat, lon, elev)}, plus 'z', decoded once."""
    if path not in _BANKS:
        d = np.load(path)
        z = d["z"].astype(float)
        q = float(d["q"])
        sites = {}
        for name in sorted({k.split("_")[0] for k in d.files if k.endswith("_doy")}):
            rec = {}
            for c in ("u", "v"):
                steps = np.concatenate([d[f"{name}_{c}0"][:, None].astype(np.int32), d[f"{name}_d{c}"].astype(np.int32)], axis=1)
                rec[c] = q * np.cumsum(steps, axis=1)
            rec["doy"] = d[f"{name}_doy"].astype(int)
            rec["hour"] = d[f"{name}_hour"].astype(int)
            rec["year"] = d[f"{name}_year"].astype(int)
            rec["lat"], rec["lon"], rec["elev"] = (float(x) for x in d[f"{name}_site"])
            sites[name] = rec
        _BANKS[path] = (z, sites)
    return _BANKS[path]


def day_of_year(day):
    """Day of year (0 = Jan 1) of a day counted from 2000-01-01."""
    d = np.datetime64("2000-01-01") + np.timedelta64(int(day), "D")
    return int((d - d.astype("datetime64[Y]")).astype(int))


class EarthSoundings(Provider):
    """Measured wind profiles per world and day. Not the default yet: select it with
    ``MARSFLIGHT_WINDS_EARTH=earth_soundings``; the swap moves ``default`` here from ``earth_jet``."""
    role = "winds.earth"
    name = "earth_soundings"
    default = False
    bank_path = BANK

    def configure(self, ctx):
        self.seed = ctx.seed
        self.z, self.sites = load_bank(self.bank_path)
        self._z = self.z.tolist()
        self._names = sorted(self.sites)
        self._profiles = {}
        self.drawn = {}          # (site, day) -> (index in the bank or None for the mean, year, day of year)

    def nearest_site(self, lat_deg, lon_deg):
        best, dmin = None, 1e9
        for n in self._names:
            s = self.sites[n]
            c = math.cos(math.radians(lat_deg))
            d = math.hypot(lat_deg - s["lat"], ((lon_deg - s["lon"] + 180) % 360 - 180) * c)
            if d < dmin:
                best, dmin = n, d
        return best

    def profile(self, site, day):
        """(u, v) lists over the bank heights for a site and a day (days since 2000-01-01, TDB)."""
        key = (site, day)
        p = self._profiles.get(key)
        if p is None:
            s = self.sites[site]
            doy = day_of_year(day)
            dd = np.abs(s["doy"] - doy)
            near = np.nonzero(np.minimum(dd, 365 - dd) <= WINDOW_DAYS)[0]
            if self.seed is None:
                u, v = s["u"][near].mean(axis=0), s["v"][near].mean(axis=0)
                self.drawn[key] = (None, None, doy)
            else:
                r = np.random.default_rng([int(self.seed) & 0xFFFFFFFF, zlib.crc32(self.name.encode()),
                                           self._names.index(site), int(day) & 0xFFFFFFFF])
                i = int(near[r.integers(len(near))])
                u, v = s["u"][i], s["v"][i]
                self.drawn[key] = (i, int(s["year"][i]), int(s["doy"][i]))
            p = (u.tolist(), v.tolist())
            self._profiles[key] = p
        return p

    def _uv(self, h, lat_deg, lon_deg, t):
        site = self.nearest_site(lat_deg, lon_deg)
        u, v = self.profile(site, int(math.floor(t / 86400.0 + 0.5)))   # J2000 is noon; days start at midnight
        z = self._z
        top = z[-1]
        hh = min(max(h, z[0]), top)
        i = min(max(bisect.bisect_right(z, hh), 1), len(z) - 1)
        w = (hh - z[i - 1]) / (z[i] - z[i - 1])
        e, n = u[i - 1] + w * (u[i] - u[i - 1]), v[i - 1] + w * (v[i] - v[i - 1])
        if h > HOLD_TO:
            f = max(0.0, (ZERO_AT - h) / (ZERO_AT - HOLD_TO))
            e, n = e * f, n * f
        return e, n

    def wind(self, h, lat, lon, t):
        """(east, north, up) m/s; angles in radians."""
        e, n = self._uv(h, math.degrees(lat), math.degrees(lon), t)
        return (e, n, 0.0)

    def tudat_wind(self, alt, lon, lat, t):
        """For ``custom_wind_model`` (Tudat's vertical frame: north, east, down), as ``World._earth_wind``."""
        e, n = self._uv(alt, math.degrees(lat), math.degrees(lon), t)
        return np.array([[n], [e], [0.0]])


PROVIDERS = [EarthSoundings]
