"""Launch weather: scrubs and slips from 20 real years of weather at the pad.

The weather is not modeled, it is replayed. ``data/range/lcc_<site>.npz`` holds, for every hour of 2005 to
2024, which launch weather rules the airport reports within 10 nmi of the pad broke (lightning, cumulus,
disturbed weather, thick cloud, and the old ceiling rule; built by ``results/issue-79/build/build_lcc.py`` from
public METARs). A flight draws one of the 20 years from its own random stream and lives through that year's
weather from its launch date on, so consecutive days are as correlated as real weather is.

Winds are not in the bank: the winds.earth role owns them. The peak ground wind is read from the ``winds.earth``
provider at 60 ft (18.3 m) when one is active and compared with the limit (34 kt by default, NASA KSC weather
launch commit criteria; a design may set its own). Upper-level winds stay with the vehicle's loads checks in
flight, as today.

What the airport reports cannot see (field mill readings, anvils and cumulus aloft at night when the stations
run unattended, triggered lightning, upper-level winds) is one optional number, ``unseen_pov``: the chance an
attempt is no-go for those reasons, independent from day to day. 0 (the default) replays the reports only, which
gives 10.6% no-go at the planned time at Kennedy. ``UNSEEN_POV_KSC`` = 0.15 brings the all-weather no-go to the
45th Weather Squadron's 24% of countdowns (1988 to 2017: 18% scrubbed and 6% delayed for weather; Winters 2017).

A launch attempt is "go" if, within its window, there is an hour that breaks no rule (and the ground wind is
inside its limit). A scrub recycles 24 hours. The mission is lost to weather if the launch period ends first.
"""
from __future__ import annotations

import datetime as dt
import math
import os

import numpy as np

from .safety import DATA, sites

RULES = {1: "lightning", 2: "cumulus", 4: "disturbed weather", 8: "thick cloud", 16: "ceiling"}
DEFAULT_RULES = 1 | 2 | 4 | 8
UNSEEN_POV_KSC = 0.15
J2000 = dt.datetime(2000, 1, 1, 11, 58, 55, 816000)   # TDB epoch in UTC, near enough for the hour of day

_BANKS = {}


def bank(name):
    if name not in _BANKS:
        z = np.load(os.path.join(DATA, name + ".npz"))
        _BANKS[name] = {"flags": z["flags"], "years": [int(y) for y in z["years"]]}
    return _BANKS[name]


def utc(t):
    return J2000 + dt.timedelta(seconds=float(t))


def n_bins(window_h):
    """Hourly weather samples a window of ``window_h`` hours sees: its opening hour and each hour after it
    (0 h: an instantaneous window, one sample; 2 h: three)."""
    return int(math.floor(max(0.0, window_h) + 1e-9)) + 1


def hour_flags(b, year_index, when):
    """Rule bits broken in the hour that contains ``when`` (a datetime), in the drawn year."""
    doy = min(when.timetuple().tm_yday, 366) - 1
    y = b["years"][year_index]
    if doy == 365 and not (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)):
        doy = 364
    return int(b["flags"][year_index, doy, when.hour])


def countdown(site_name, launch_utc, rng, window_h=1.0, period_days=21.0, rules=DEFAULT_RULES, winds=None,
              wind_limit_ms=None, recycle_h=None, year_index=None, max_attempts=400, unseen_pov=0.0):
    """Count down from ``launch_utc`` (datetime), day after day, until weather allows a launch or the launch
    period ends. Returns the launch time (datetime, or None if the period closed), the scrubs and the year drawn.

    ``winds``: a winds.earth provider (``wind(h, lat, lon, t) -> (east, north, up)``) or None to leave winds out.
    """
    s = sites()["sites"][site_name]
    b = bank(s["weather"])
    if year_index is None:
        year_index = int(rng.integers(len(b["years"])))
    limit = wind_limit_ms if wind_limit_ms is not None else sites()["ground_wind_limit_ms"]
    recycle = dt.timedelta(hours=recycle_h if recycle_h is not None else sites()["scrub_recycle_h"])
    hours = n_bins(window_h)
    end = launch_utc + dt.timedelta(days=period_days)
    t = launch_utc
    scrubs = []
    for _ in range(max_attempts):
        if t > end:
            return {"launch_utc": None, "scrubs": scrubs, "year": b["years"][year_index], "lost": True}
        reasons = []
        if unseen_pov > 0 and rng.random() < unseen_pov:
            scrubs.append({"utc": t.isoformat(), "reasons": ["weather the field reports do not show (field mills, "
                                                              "anvils, cumulus aloft, upper winds)"]})
            t = t + recycle
            continue
        for k in range(hours):
            when = t + dt.timedelta(hours=k)
            f = hour_flags(b, year_index, when) & rules
            why = [RULES[bit] for bit in RULES if f & bit]
            if winds is not None and not why:
                tt = (when - J2000).total_seconds()
                e, n, _u = winds.wind(18.3, math.radians(s["lat"]), math.radians(s["lon"]), tt)
                if math.hypot(e, n) > limit:
                    why = [f"ground wind {math.hypot(e, n):.1f} m/s above {limit:.1f}"]
            if not why:
                return {"launch_utc": when, "scrubs": scrubs, "year": b["years"][year_index], "lost": False}
            reasons.append(why)
        scrubs.append({"utc": t.isoformat(), "reasons": sorted({w for r in reasons for w in r})})
        t = t + recycle
    return {"launch_utc": None, "scrubs": scrubs, "year": b["years"][year_index], "lost": True}


def scrub_rate(site_name, window_h=0.0, rules=DEFAULT_RULES, months=None, hours_utc=None):
    """Share of attempts (every day and hour of the bank, or the given months and UTC hours) that would scrub
    with no winds: the climatological probability of violation for one attempt."""
    b = bank(sites()["sites"][site_name]["weather"])
    f = (b["flags"] & rules) > 0                       # (year, doy, hour)
    flat = f.reshape(f.shape[0], -1)
    allbad = flat.copy()
    for k in range(1, n_bins(window_h)):
        allbad[:, :-k] &= flat[:, k:]
    allbad = allbad.reshape(f.shape)
    sel = np.ones(f.shape, bool)
    if months is not None:
        doy_month = np.array([(dt.date(2001, 1, 1) + dt.timedelta(days=min(d, 364))).month for d in range(366)])
        sel &= np.isin(doy_month, list(months))[None, :, None]
    if hours_utc is not None:
        sel &= np.isin(np.arange(24), list(hours_utc))[None, None, :]
    return float(allbad[sel].mean())
