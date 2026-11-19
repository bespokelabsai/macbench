"""Interplanetary design helpers: Lambert arcs between planets with the simulator's ephemerides."""
from __future__ import annotations

import datetime as _dt
import math

import numpy as np

from .util import MU, body_state, kernels, unit


def epoch(s):
    from tudatpy.astro import time_representation as tr
    d = _dt.datetime.fromisoformat(s)
    return tr.julian_day_to_seconds_since_epoch(tr.DateTime.from_python_datetime(d).to_julian_day())


def date_str(t):
    return (_dt.datetime(2000, 1, 1, 12) + _dt.timedelta(seconds=float(t))).strftime("%Y-%m-%d %H:%M")


def lambert(r1, r2, tof, mu=MU["Sun"]):
    kernels()
    from tudatpy.astro import two_body_dynamics as tb
    lt = tb.LambertTargeterIzzo(np.asarray(r1, float), np.asarray(r2, float), float(tof), mu)
    return np.array(lt.get_departure_velocity()), np.array(lt.get_arrival_velocity())


def planet_vinf(dep, arr, t_dep, t_arr):
    """Departure and arrival hyperbolic excess vectors (J2000) of the Lambert arc between planet centres."""
    s1 = body_state(dep, t_dep)
    s2 = body_state(arr, t_arr)
    v1, v2 = lambert(s1[:3], s2[:3], t_arr - t_dep)
    return v1 - s1[3:], v2 - s2[3:]


def declination(v):
    v = unit(v)
    return math.degrees(math.asin(v[2]))
