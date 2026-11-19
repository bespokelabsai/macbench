"""The simulator's nominal Mars atmosphere, rebuilt in memory (hydrostatic CO2 over the Viking/MSL-like
temperature profile), with its seasonal pressure cycle. The flight software scales it by a density
factor estimated from the measured drag."""
from __future__ import annotations

import bisect
import math

import numpy as np

from . import mission as M  # noqa: E402
from .marsenv import (MARS_GAMMA, MARS_P0, MARS_R_GAS, MARS_REF_RADIUS, dust_density_factor, mars_ls,  # noqa: E402
                      mars_seasonal_pressure_factor, mars_temperature)

MU = 4.282837581575610e13


def _build():
    hs = np.arange(-10e3, 300e3 + 1, 500.0)

    def g(h):
        return MU / (MARS_REF_RADIUS + h) ** 2

    pvals = {0.0: MARS_P0}
    p = MARS_P0
    up = [h for h in hs if h >= 0]
    for a, b in zip(up, up[1:]):
        Tm = 0.5 * (mars_temperature(a / 1e3) + mars_temperature(b / 1e3))
        p = p * math.exp(-g(0.5 * (a + b)) * (b - a) / (MARS_R_GAS * Tm))
        pvals[b] = p
    p = MARS_P0
    down = [h for h in hs if h <= 0][::-1]
    for a, b in zip(down, down[1:]):
        Tm = 0.5 * (mars_temperature(a / 1e3) + mars_temperature(b / 1e3))
        p = p * math.exp(-g(0.5 * (a + b)) * (b - a) / (MARS_R_GAS * Tm))
        pvals[b] = p
    H, RHO, P, T = [], [], [], []
    for h in hs:
        Tt = mars_temperature(h / 1e3)
        Pp = max(pvals[h], 1e-12)
        H.append(float(h))
        RHO.append(max(Pp / (MARS_R_GAS * Tt), 1e-15))
        P.append(Pp)
        T.append(Tt)
    return H, RHO, P, T


_H, _RHO, _P, _T = _build()
# Dust forecast: dust heats the air and puffs it up, so a dusty atmosphere is denser high up (30% to 50% at
# 50 km for optical depth 0.8 to 1, 7% to 9% at 27 km). The onboard profile carries the forecast optical depth for
# the arrival season, as MSL flew Mars-GRAM with a dust forecast; the measured-drag density factor then corrects
# what the forecast misses.
_DUST_TAU = M.MARS_DUST_TAU_FORECAST
_RHO = [r * dust_density_factor(_DUST_TAU, h / 1e3) for h, r in zip(_H, _RHO)]
_LNRHO = [math.log(x) for x in _RHO]


def season_factor(t):
    return mars_seasonal_pressure_factor(mars_ls(t))


def density(h):
    """Nominal density (kg/m^3) at altitude h above the 3389.5 km reference sphere (no season)."""
    if h <= _H[0]:
        return _RHO[0]
    if h >= _H[-1]:
        return _RHO[-1]
    k = int((h - _H[0]) // 500.0)
    f = (h - _H[k]) / 500.0
    return _RHO[k] + f * (_RHO[k + 1] - _RHO[k])


def temperature(h):
    return mars_temperature(h / 1e3)


def sound_speed(h):
    return math.sqrt(MARS_GAMMA * MARS_R_GAS * mars_temperature(h / 1e3))


def pressure(h):
    if h <= _H[0]:
        return _P[0]
    if h >= _H[-1]:
        return _P[-1]
    k = int((h - _H[0]) // 500.0)
    f = (h - _H[k]) / 500.0
    return _P[k] + f * (_P[k + 1] - _P[k])
