"""Mars atmosphere as marsflight flies it today: the ``atmosphere.mars`` role's default.

A hydrostatic CO2 table built from one mean temperature profile, times the world's random density field by
height, the seasonal pressure cycle (Viking Lander 1) and the dust term. All of it is in ``environment.py``
(``write_mars_table``, ``mars_seasonal_pressure_factor``, ``dust_density_factor``, ``World._mars_density_scale``);
this provider calls that code and changes nothing, so the default keeps every world as it is. the move then
brings the code here, as ``docs/LAYOUT.md`` maps it.

What replaces it: the Mars Climate Database v6.1 (``mars_mcd``) or Mars-GRAM (``mars_gram``), once the access
forms are through. The swap is one line: the new provider gets ``default = True`` and this one
loses it. ``dev/test_mars_env.py`` checks that this provider gives the same density as ``environment.World``.

Where the dust comes from: the active ``dust.mars`` provider (``mars_dust.py``). With ``storm_draw`` it is today's
storm draw from ``Dispersions``; with ``montabone`` it is the observed dust of a real Mars year.
"""
from __future__ import annotations

import math
import os
import tempfile

import numpy as np

from ...core.model import Provider
from ... import environment as E

SECONDS_PER_SOL = 88775.244


class MarsTable(Provider):
    role = "atmosphere.mars"
    name = "mars_table"
    default = True

    def __init__(self, dust=None):
        self.dust = dust          # a configured dust.mars provider, or None for the world's own storm draw
        self.disp = None
        self.table = None
        self.path = None

    def configure(self, ctx, dust=None):
        """Builds the world's table. ``ctx.world`` is the drawn ``Dispersions``."""
        if dust is not None:
            self.dust = dust
        self.disp = ctx.world
        self.epoch0 = float(getattr(ctx, "epoch0", 0.0) or 0.0)
        self.path = os.path.join(tempfile.mkdtemp(prefix="mars_table_"), "mars_atm.dat")
        E.write_mars_table(self.path, self.disp)
        self.table = np.loadtxt(self.path)

    # -- the role's methods ------------------------------------------------------------------
    def tau(self, lat, lon, t):
        if self.dust is not None:
            return self.dust.optical_depth(lat, lon, t)
        return self.disp.mars_tau((t - self.epoch0) / SECONDS_PER_SOL)

    def scale(self, h, lat, lon, t):
        """The factor on the table's density: season, then dust (as ``World._mars_density_scale``)."""
        f = E.mars_seasonal_pressure_factor(E.mars_ls(t))
        tau = self.tau(lat, lon, t)
        if tau > 0.5:
            f *= E.dust_density_factor(tau, h / 1e3)
        return f

    def density(self, h, lat, lon, t):
        return float(np.interp(h, self.table[:, 0], self.table[:, 1])) * self.scale(h, lat, lon, t)

    def temperature(self, h, lat, lon, t):
        return float(np.interp(h, self.table[:, 0], self.table[:, 3]))

    def pressure(self, h, lat, lon, t):
        return float(np.interp(h, self.table[:, 0], self.table[:, 2])) * self.scale(h, lat, lon, t)

    def speed_of_sound(self, h, lat, lon, t):
        return math.sqrt(E.MARS_GAMMA * E.MARS_R_GAS * self.temperature(h, lat, lon, t))

    def tudat_settings(self, density_scale=None):
        """Tudat's tabulated atmosphere on the world's table, scaled as the live code does. ``density_scale`` is
        the core's callback of time (Tudat passes the scale only the time; the core supplies the height)."""
        from tudatpy.dynamics import environment_setup as es
        atm = es.atmosphere.tabulated(self.path, specific_gas_constant=E.MARS_R_GAS,
                                      ratio_of_specific_heats=E.MARS_GAMMA)
        if density_scale is None:
            return atm
        return es.atmosphere.scaled_by_function(atm, density_scale)


PROVIDERS = [MarsTable]
