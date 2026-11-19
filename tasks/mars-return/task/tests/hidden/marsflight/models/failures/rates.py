"""Failure rates from data/failures/rates.json, and the arithmetic of drawing a failure time from them.

Every rate in the JSON file names its source. This file only does the sums:
- a Weibull unit with shape beta and scale theta (years) survives to t with R(t) = exp(-(t/theta)^beta);
- a unit that already ran ``age`` years (a burn-in test) fails at the first t > 0 with
  (age + t) = theta * ((age/theta)^beta - ln U)^(1/beta), U uniform on (0, 1];
- a string of units in series fails at the first of its units' failure times.
"""
from __future__ import annotations

import json
import math
import os

YEAR_S = 365.25 * 86400.0
_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "failures")
_CACHE = {}


def load(name="rates.json"):
    if name not in _CACHE:
        with open(os.path.join(_DATA, name)) as f:
            _CACHE[name] = json.load(f)
    return _CACHE[name]


def weibull(unit):
    w = load()["weibull_subsystems"][unit]
    return float(w["beta"]), float(w["theta_years"])


def reliability(unit, t_years, age_years=0.0):
    """Chance that a Weibull unit that already ran ``age_years`` survives ``t_years`` more."""
    beta, theta = weibull(unit)
    return math.exp((age_years / theta) ** beta - ((age_years + t_years) / theta) ** beta)


def failure_time_s(unit, u, age_years=0.0):
    """Seconds from launch to the failure of a Weibull unit, given a uniform draw ``u`` in (0, 1) and the hours
    it already ran on the ground (as years). Inverse of the conditional Weibull law."""
    beta, theta = weibull(unit)
    u = min(max(float(u), 1e-300), 1.0)
    total = theta * ((age_years / theta) ** beta - math.log(u)) ** (1.0 / beta)
    return (total - age_years) * YEAR_S


def p_fail(units, t_years, age_years=0.0):
    """Chance that a series string of these units fails within t_years."""
    r = 1.0
    for u in units:
        r *= reliability(u, t_years, age_years)
    return 1.0 - r


def part(name):
    return load()["part_classes"][name]


def redundancy(name):
    return load()["redundancy"][name]


def prior(name):
    return float(load()["latent_flaw_prior"][name])


def landing_radar_record():
    """Counts by class in data/failures/landing_sensors.json: a sensor failed, b integration error, c software error,
    d no anomaly, u unknown."""
    cnt = {}
    for r in load("landing_sensors.json")["landings"]:
        cnt[r["class"]] = cnt.get(r["class"], 0) + int(r.get("count", 1))
    return cnt


def landing_radar_p(prior=(0.5, 0.5)):
    """Posterior mean chance that the landing sensor fails in one landing: sensor failures (class a) over every
    landing with a sensor record (a, b, c, d), with a Beta prior (Jeffreys by default)."""
    c = landing_radar_record()
    n = sum(v for k, v in c.items() if k != "u")
    return (c.get("a", 0) + prior[0]) / (n + prior[0] + prior[1])
