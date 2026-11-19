"""Earth's atmosphere from NRLMSIS 2.1, the ``atmosphere.earth`` role.

NRLMSIS 2.1 is the US Naval Research Laboratory's empirical model of the neutral atmosphere from the ground
to the exosphere (Emmert et al. 2021, Earth and Space Science 8, e2020EA001321, for MSIS 2.0; Emmert et al.
2022, JGR Space Physics 127, e2022JA030896, for 2.1, which adds nitric oxide). It replaced NRLMSISE-00
(Picone et al. 2002), which is what Tudat has built in. Below about 100 km, 2.0 and 2.1 were refitted to many
more temperature profiles (TIMED/SABER, Aura/MLS, radiosondes), so the troposphere and stratosphere follow
measurements rather than the old model's climatology.

It runs through ``pymsis`` (MIT license, pinned in ``environment/requirements/earth.txt``), which wraps NRL's
own Fortran. NRL's license for the model allows non-commercial research use, which this task is.

Space weather comes from ``earth_space_weather`` (the frozen CelesTrak file, one record drawn per world), so
pymsis never downloads anything. The world's density factor (``Dispersions.earth_density``, 1 sigma 5%) still
multiplies the density, as it does for NRLMSISE-00 today.

Speed: a single pymsis call costs about 0.1 ms, so the provider evaluates a whole altitude profile at once
(about 0.5 ms for 401 heights) and keeps it for a cell of 0.25 deg in latitude and longitude and 15 minutes.
A point is then an interpolation in that profile (log density, linear in height): a few microseconds.

How the core can put it on the Tudat body: Tudat 1.0's custom atmosphere takes a density function but
holds the temperature constant, which would make the Mach number wrong. ``tudat_settings`` instead gives a
tabulated atmosphere (density, pressure, temperature, gas constant and ratio of specific heats against height)
for the launch site and date, scaled at each call so that the density is this model's at the vehicle's real
position and time. The core gives the position with ``note_position(alt, lat, lon)`` from the Earth wind
callback, which Tudat calls just before the density at each update (the same trick ``environment.World`` uses
for the Mars dust factor). The temperature, and so the speed of sound, is the launch site's profile; for Earth
entry over Utah that is a few percent off (``docs/reference/atmosphere-earth-msis21.md``).
"""
from __future__ import annotations

import bisect
import math
import os
import tempfile

import numpy as np

from ...core.model import Provider
from .earth_space_weather import SpaceWeather, tdb_to_utc

K_BOLTZMANN = 1.380649e-23
# Height grid of the cached profiles, m: 0.5 km to 120 km, then coarser. Geodetic height, as MSIS takes it.
ALT_GRID = np.concatenate([np.arange(0.0, 120e3, 500.0), np.arange(120e3, 300e3, 2e3), np.arange(300e3, 1000.1e3, 10e3)])
_GRID = ALT_GRID.tolist()
_TOP = _GRID[-1]
CELL_DEG = 0.25
CELL_S = 900.0
DIATOMIC = (1, 2, 9)          # N2, O2, NO in the pymsis output; the others (O, He, H, Ar, N, anomalous O) are atoms
SPECIES = range(1, 10)


def msis_profile(utc, lat, lon, alts_m, f107, f107a, ap, version=2.1):
    """One profile: arrays of density (kg/m3), temperature (K), pressure (Pa), gas constant (J/kg/K) and ratio
    of specific heats, from pymsis at one time and place for heights ``alts_m``."""
    import pymsis
    n = len(alts_m)
    out = pymsis.calculate(np.repeat(np.datetime64(utc, "ms"), n), np.full(n, lon), np.full(n, lat),
                           np.asarray(alts_m) / 1e3, f107s=np.full(n, f107), f107as=np.full(n, f107a),
                           aps=np.repeat(np.atleast_2d(ap), n, axis=0), version=version)
    out = np.asarray(out).reshape(n, 11)
    rho, T = out[:, 0], out[:, 10]
    num = np.nan_to_num(out[:, 1:10])
    ntot = num.sum(axis=1)
    p = ntot * K_BOLTZMANN * T
    R = p / (rho * T)
    x_di = num[:, [d - 1 for d in DIATOMIC]].sum(axis=1) / ntot
    cv = 2.5 * x_di + 1.5 * (1.0 - x_di)          # in units of k per particle
    gamma = (cv + 1.0) / cv
    return rho, T, p, R, gamma


class Msis21Atmosphere:
    """NRLMSIS at any point and time, with profiles cached per cell. ``version`` 2.1 (default) or 0
    (NRLMSISE-00 in pymsis, for comparisons)."""

    def __init__(self, space_weather: SpaceWeather, factor=1.0, version=2.1):
        self.sw = space_weather
        self.factor = float(factor)
        self.version = version
        self._cache = {}

    def _cell(self, lat, lon, t):
        key = (int(math.floor(lat / CELL_DEG)), int(math.floor(((lon + 180.0) % 360.0) / CELL_DEG)),
               int(math.floor(t / CELL_S)))
        prof = self._cache.get(key)
        if prof is None:
            if len(self._cache) > 20000:
                self._cache.clear()
            clat = (key[0] + 0.5) * CELL_DEG
            clon = (key[1] + 0.5) * CELL_DEG - 180.0
            tc = (key[2] + 0.5) * CELL_S
            f107, f107a, ap = self.sw.indices(tc)
            rho, T, p, R, g = msis_profile(tdb_to_utc(tc), clat, clon, ALT_GRID, f107[0], f107a[0], ap[0],
                                           self.version)
            prof = np.column_stack([np.log(rho), T, np.log(p), R, g]).tolist()
            self._cache[key] = prof
        return prof

    def state(self, h, lat, lon, t):
        """(density, temperature, pressure, gas constant, ratio of specific heats) at a point; angles in
        degrees here. Below 0 m the 0 m values; above 1000 km the density keeps falling with the top scale
        height."""
        prof = self._cell(lat, lon, t)
        hh = min(max(float(h), 0.0), _TOP)
        i = min(bisect.bisect_right(_GRID, hh), len(_GRID) - 1)
        w = (hh - _GRID[i - 1]) / (_GRID[i] - _GRID[i - 1])
        a, b = prof[i - 1], prof[i]
        lr, T, lp, R, g = (a[k] + w * (b[k] - a[k]) for k in range(5))
        if h > _TOP:
            lr -= (h - _TOP) * (prof[-2][0] - prof[-1][0]) / (_GRID[-1] - _GRID[-2])
        return self.factor * math.exp(lr), T, math.exp(lp), R, g

    def density(self, h, lat, lon, t):
        return self.state(h, lat, lon, t)[0]

    def temperature(self, h, lat, lon, t):
        return self.state(h, lat, lon, t)[1]

    def speed_of_sound(self, h, lat, lon, t):
        _, T, _, R, g = self.state(h, lat, lon, t)
        return math.sqrt(g * R * T)


class EarthMsis21(Provider):
    """``atmosphere.earth`` from NRLMSIS 2.1, the default wired the providers. The original
    NRLMSISE-00 stays selectable with ``MARSFLIGHT_ATMOSPHERE_EARTH=earth_nrlmsise00``."""
    role = "atmosphere.earth"
    name = "earth_msis21"
    default = True
    version = 2.1

    def configure(self, ctx):
        self.ctx = ctx
        factor = getattr(ctx.world, "earth_density", 1.0) if ctx.world is not None else 1.0
        self.atm = Msis21Atmosphere(SpaceWeather(ctx.rng("earth_space_weather")), factor, self.version)
        self._pos = None

    # The role's methods. Angles in radians, as in core.model.ROLES.
    def density(self, h, lat, lon, t):
        return self.atm.density(h, math.degrees(lat), math.degrees(lon), t)

    def temperature(self, h, lat, lon, t):
        return self.atm.temperature(h, math.degrees(lat), math.degrees(lon), t)

    def speed_of_sound(self, h, lat, lon, t):
        return self.atm.speed_of_sound(h, math.degrees(lat), math.degrees(lon), t)

    # Tudat wiring
    def note_position(self, alt, lat, lon):
        """The vehicle's height (m) and geodetic latitude and longitude (rad), from the Earth wind callback."""
        if math.isfinite(alt):
            self._pos = (alt, lat, lon)

    def write_table(self, path, lat_deg, lon_deg, t):
        """A Tudat tabulated atmosphere file for one place and time: height, density, pressure, temperature,
        gas constant, ratio of specific heats."""
        f107, f107a, ap = self.atm.sw.indices(t)
        rho, T, p, R, g = msis_profile(tdb_to_utc(t), lat_deg, lon_deg, ALT_GRID, f107[0], f107a[0], ap[0],
                                       self.version)
        self._table_h, self._table_lrho = ALT_GRID, np.log(rho)
        rows = list(zip(ALT_GRID, rho, p, T, R, g))
        # Tudat evaluates the atmosphere on every Earth-centered arc and at every integrator stage, so the
        # table covers any height a trial step can reach (as environment.write_mars_table does): a near vacuum
        # far above, the surface values below the ground.
        top = rows[-1]
        rows += [(h, min(top[1], 1e-18), min(top[2], 1e-12)) + top[3:] for h in
                 (1.5e6, 3e6, 1e7, 3e7, 1e8, 3e8, 1e9, 1e10, 1e12, 1e15, 1e20)]
        rows[:0] = [(h,) + rows[0][1:] for h in (-4.0e6, -1.0e6, -1.0e5, -2.0e4)]
        with open(path, "w") as f:
            for row in rows:
                f.write("%.1f %.6e %.6e %.3f %.4f %.5f\n" % row)
        return path

    def scale(self, t):
        """Density over the table's density at the last noted position, times the world's factor."""
        if self._pos is None:
            return self.atm.factor
        h, lat, lon = self._pos
        if h > _TOP:
            return self.atm.factor    # far above: the table's own near vacuum
        base = math.exp(float(np.interp(min(max(h, 0.0), _TOP), self._table_h, self._table_lrho)))
        return self.atm.density(h, math.degrees(lat), math.degrees(lon), t) / base

    def base_settings(self, epoch, site=None):
        """Tudat's tabulated atmosphere for the launch site (or ``site``) at ``epoch``, unscaled."""
        from tudatpy.dynamics import environment_setup as es
        from ... import data as D
        site = site or D.SITES["earth_launch"]
        path = self.write_table(os.path.join(tempfile.mkdtemp(prefix="msis21_"), "earth_atm.dat"),
                                site["lat"], site["lon"], epoch)
        A = es.atmosphere.AtmosphereDependentVariables
        return es.atmosphere.tabulated(path, [A.tabulated_density, A.tabulated_pressure, A.tabulated_temperature,
                                              A.tabulated_gas_constant, A.tabulated_specific_heat_ratio])

    def tudat_settings(self, epoch, site=None):
        """The tabulated atmosphere scaled to this model's density at the noted position (``scale``)."""
        from tudatpy.dynamics import environment_setup as es
        return es.atmosphere.scaled_by_function(self.base_settings(epoch, site), self.scale)


PROVIDERS = [EarthMsis21]
