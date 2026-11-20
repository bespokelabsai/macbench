"""Drag above the atmosphere interface, so that orbits decay during coasts and long waits.

Today ``sim.py`` adds drag only on arcs that start below ``ATM_TOP`` (130 km at Mars, 150 km at Earth). Above
that a sample container or an orbiter can wait as long as it likes in a very low orbit. This model gives the drag
on every Earth or Mars centred arc between ``ATM_TOP`` and ``CEILING`` (1000 km), where it matters for weeks to
years, and nothing else changes: the flight in the air below ``ATM_TOP`` keeps its own aerodynamics.

Where the numbers come from:
- Mars density: Mars-DTM (Bruinsma and Lemoine 2002), as tabulated from Tudat's own model in
  ``mars_thermosphere.py``, by height, latitude, local solar time, season and solar flux at Mars.
- Earth density: whatever atmosphere the core put on Tudat's Earth body (today NRLMSISE-00 with the world's
  space weather; the NRLMSIS 2.1 once it is the ``atmosphere.earth`` default). NRLMSISE-00 and MSIS 2.1 both
  reach 1000 km, so nothing new is needed, only the drag.
- Solar flux for Mars: the 81-day mean F10.7 from the ``space_weather`` provider when one is active, else from
  Tudat's CelesTrak file (observed to July 2025, NOAA's monthly forecast to October 2041, then the same phase of
  the 11-year cycle earlier). Scaled to Mars by 1/r^2 with the Mars24 Sun distance.
- Drag coefficient 2.2, the usual free-molecular value for satellites (Vallado 2013, Fundamentals of
  Astrodynamics and Applications, 4th ed., sec. 9.6; Moe and Moe 2005, Planet. Space Sci. 53, 793, measured
  2.0 to 2.3 for compact bodies at 200 to 400 km).
- Area: the stack is a cylinder of its widest diameter and its total length, and each solar array a flat plate.
  A stack that holds an attitude (the core passes its long axis) shows the cylinder's projected area along the
  air-relative velocity; the arrays count half their area (they turn to the Sun, not to the flow). A body without
  attitude control tumbles, and the mean projected area of a convex body over all orientations is a quarter of
  its surface (Cauchy's formula); a tumbling flat plate shows half its area.
- The air turns with the planet (velocity relative to the air is v - omega x r).

How the core uses it (see the PR for the exact lines): ``tudat_acceleration(body, central)`` returns a Tudat
custom acceleration for the arc's body, added in ``Mission._accels`` on arcs outside the atmosphere. The core
calls ``set_stack(elements, mass)`` at the start of each arc. The force is computed in the inertial frame; it is a
pure drag along the air-relative velocity, so 3-DOF and 6-DOF use it the same way (no torque).
"""
from __future__ import annotations

import math
import os

import numpy as np

from ...core.model import Model
from .mars_thermosphere import AU, MarsDtmTable, lat_lst, mars24, mars_pole

CD_FREE_MOLECULAR = 2.2
CEILING = {"Earth": 1000e3, "Mars": 1000e3}
FLOOR = {"Earth": 150e3, "Mars": 130e3}           # sim.ATM_TOP: below this the flight in air has its own drag
OMEGA = {"Earth": 7.292115e-5, "Mars": 7.088218e-5}  # rad/s (IERS; Mars rotation 350.891983 deg/day, pck00010)
R_EQ = {"Earth": 6378137.0, "Mars": 3396190.0}
EARTH_FLAT = 1.0 / 298.257223563
F_MARS_RANGE = (25.0, 100.0)                       # the table's range; Tudat's model also stops at 100
SOLAR_CYCLE_S = 11.0 * 365.25 * 86400.0


def stack_area(elements):
    """Mean projected area (m2) of a tumbling stack: elements are objects or dicts with ``diameter``, ``length``
    and optionally ``solar_m2`` (one-sided array area)."""
    def g(e, k):
        return float(e.get(k, 0.0) if isinstance(e, dict) else getattr(e, k, 0.0) or 0.0)
    if not elements:
        return 0.0
    d = max(g(e, "diameter") for e in elements)
    length = sum(g(e, "length") for e in elements)
    cyl = math.pi * d * length + 0.5 * math.pi * d * d
    return 0.25 * cyl + 0.5 * sum(g(e, "solar_m2") for e in elements)


def cylinder(elements):
    """(widest diameter, total length, one-sided solar array area) of a stack."""
    def g(e, k):
        return float(e.get(k, 0.0) if isinstance(e, dict) else getattr(e, k, 0.0) or 0.0)
    if not elements:
        return 0.0, 0.0, 0.0
    return (max(g(e, "diameter") for e in elements), sum(g(e, "length") for e in elements),
            sum(g(e, "solar_m2") for e in elements))


class SolarFlux:
    """81-day centred F10.7 (observed, at 1 AU) by time, from a ``space_weather`` provider or Tudat's file."""

    def __init__(self, provider=None, path=None):
        self.provider = provider
        self.t, self.f = None, None
        if provider is None:
            self._read(path)

    def _read(self, path):
        import datetime as _dt
        if path is None:
            import tudatpy.data
            path = os.path.join(tudatpy.data.get_space_weather_path(), "sw19571001.txt")
        j2000 = _dt.datetime(2000, 1, 1, 12)
        ts, fs, sec = [], [], None
        with open(path) as fh:
            for line in fh:
                if line.startswith("BEGIN "):
                    sec = line.split()[1]
                    continue
                if line.startswith("END "):
                    sec = None
                    continue
                p = line.split()
                if sec in ("OBSERVED", "DAILY_PREDICTED") and len(p) >= 32:
                    f = float(p[-2])                  # F10.7 OBS CENTER81
                elif sec == "MONTHLY_PREDICTED" and len(p) >= 10:
                    f = float(p[-2])
                else:
                    continue
                d = _dt.datetime(int(p[0]), int(p[1]), int(p[2]))
                t = (d - j2000).total_seconds()
                if ts and t <= ts[-1]:
                    continue
                ts.append(t)
                fs.append(f)
        self.t, self.f = np.array(ts), np.array(fs)

    def f107a(self, t):
        if self.provider is not None:
            ind = self.provider.indices(t)
            v = ind["f107a"] if isinstance(ind, dict) else ind[1]
            return float(np.atleast_1d(v)[0])
        while t > self.t[-1]:
            t -= SOLAR_CYCLE_S
        return float(np.interp(t, self.t, self.f))


class OrbitDecay(Model):
    """Drag between the atmosphere interface and 1000 km on Earth and Mars centred arcs."""
    name = "orbit_decay"
    area = "orbit_decay"
    order = 40

    def __init__(self, bodies=None, flux: SolarFlux | None = None, cd=CD_FREE_MOLECULAR):
        self.bodies = bodies
        self.flux = flux
        self.cd = cd
        self.cda = 0.0
        self.mass = 1.0
        self.axis = None
        self.cyl = (0.0, 0.0, 0.0)
        self.enabled = True
        self._mars = None
        self._sun = (None, None)
        self._pole = mars_pole(0.0)
        self._fkey = (None, None)

    # -- the Model hooks ---------------------------------------------------------------------------
    def configure(self, ctx):
        self.bodies = ctx.bodies
        provider = None
        provs = getattr(ctx, "providers", None)
        if isinstance(provs, dict):
            provider = provs.get("space_weather")
        self.flux = SolarFlux(provider)

    def design_fields(self):
        return ["elements[].diameter", "elements[].length", "elements[].solar_array_m2"]

    def set_stack(self, elements, mass, axis=None):
        """The flown stack for the next arc: its elements (for the area), its mass (kg) and, when the stack holds
        an attitude, the unit vector of its long axis in J2000. Without an axis the stack tumbles."""
        self.cda = self.cd * stack_area(elements)
        self.mass = max(float(mass), 1e-6)
        self.axis = None
        if axis is not None and np.linalg.norm(axis) > 0:
            g = cylinder(elements)
            n = float(np.linalg.norm(axis))
            self.axis = tuple(float(x) / n for x in axis)
            self.cyl = g

    # -- the environment ---------------------------------------------------------------------------
    def mars_table(self):
        if self._mars is None:
            self._mars = MarsDtmTable()
        return self._mars

    def f_mars(self, t):
        if self.flux is None:
            self.flux = SolarFlux()
        r_au = mars24(t)[3]
        return min(max(self.flux.f107a(t) / (r_au * r_au), F_MARS_RANGE[0]), F_MARS_RANGE[1])

    def _sun_from_mars(self, t):
        key = int(t // 600.0)
        if self._sun[0] != key:
            if self.bodies is not None:
                eph = (self.bodies.get(b).ephemeris.cartesian_state(t)[:3] for b in ("Sun", "Mars"))
                s = np.asarray(next(eph)) - np.asarray(next(eph))
            else:
                from tudatpy.interface import spice
                s = np.asarray(spice.get_body_cartesian_position_at_epoch("Sun", "Mars", "J2000", "none", t))
            self._sun = (key, s)
        return self._sun[1]

    def mars_density(self, t, r):
        """Mars-DTM density (kg/m3) at a Mars-centred J2000 position, any height from 120 km."""
        h = math.sqrt(r[0] * r[0] + r[1] * r[1] + r[2] * r[2]) - R_EQ["Mars"]
        lat, lst = lat_lst(r, self._sun_from_mars(t), self._pole)
        key = int(t // 21600.0)
        if self._fkey[0] != key:
            self._fkey = (key, (mars24(t)[0], self.f_mars(t)))
        ls, f = self._fkey[1]
        return self.mars_table().density(h, lat, lst, ls, f, cache_key=key)

    def earth_density(self, t, r):
        """Density (kg/m3) of the atmosphere on Tudat's Earth body at an Earth-centred J2000 position."""
        rot = np.asarray(self.bodies.get("Earth").rotation_model.inertial_to_body_fixed_rotation(t))
        x = rot @ np.asarray(r)
        rho_xy = math.hypot(x[0], x[1])
        lat = math.atan2(x[2], rho_xy)
        lon = math.atan2(x[1], x[0])
        h = math.sqrt(x @ x) - R_EQ["Earth"] * (1.0 - EARTH_FLAT * math.sin(lat) ** 2)
        if h > CEILING["Earth"]:
            return 0.0
        return float(self.bodies.get("Earth").atmosphere_model.get_density(h, lon, lat, t))

    def density(self, central, t, r):
        h = math.sqrt(r[0] * r[0] + r[1] * r[1] + r[2] * r[2]) - R_EQ.get(central, 0.0)
        if central not in CEILING or h > CEILING[central] or h < FLOOR[central] - 20e3:
            return 0.0
        if central == "Mars":
            return self.mars_density(t, r)
        return self.earth_density(t, r)

    # -- the force ------------------------------------------------------------------------------------
    def acceleration(self, central, t, r, v, cda_over_m=None):
        """Drag acceleration (m/s2, J2000) on a body at position r and velocity v relative to ``central``."""
        if not self.enabled:
            return np.zeros(3)
        b = self.cda / self.mass if cda_over_m is None else cda_over_m
        if b <= 0.0:
            return np.zeros(3)
        rho = self.density(central, t, r)
        if rho <= 0.0:
            return np.zeros(3)
        if central == "Mars":
            w0, w1, w2 = (OMEGA["Mars"] * float(x) for x in self._pole)
        else:
            w0, w1, w2 = 0.0, 0.0, OMEGA["Earth"]           # J2000 z is the Earth's pole to 0.3 deg over 2030s
        r0, r1, r2 = float(r[0]), float(r[1]), float(r[2])
        # air-relative velocity v - w x r, in plain floats (this runs inside the integrator)
        v0 = float(v[0]) - (w1 * r2 - w2 * r1)
        v1 = float(v[1]) - (w2 * r0 - w0 * r2)
        v2 = float(v[2]) - (w0 * r1 - w1 * r0)
        speed = math.sqrt(v0 * v0 + v1 * v1 + v2 * v2)
        if cda_over_m is None and self.axis is not None and speed > 0:
            # a held attitude: the cylinder's area seen along the air-relative velocity, arrays edge-on on average
            d, length, arrays = self.cyl
            ax = self.axis
            c = abs(ax[0] * v0 + ax[1] * v1 + ax[2] * v2) / speed
            area = 0.25 * math.pi * d * d * c + d * length * math.sqrt(max(0.0, 1.0 - c * c)) + 0.5 * arrays
            b = self.cd * area / self.mass
        k = -0.5 * rho * b * speed
        return np.array([k * v0, k * v1, k * v2])

    def tudat_acceleration(self, body, central):
        """A Tudat custom acceleration giving this drag on ``body`` about ``central`` (for ``Mission._accels``)."""
        from tudatpy.dynamics import propagation_setup as ps
        bodies = self.bodies

        def acc(t):
            x = np.asarray(bodies.get(body).state) - np.asarray(bodies.get(central).state)
            return self.acceleration(central, t, x[:3], x[3:])

        return ps.acceleration.custom_acceleration(acc)


def scale_height_km(table: MarsDtmTable, h_km, ls=90.0, f_mars=55.0):
    """Density scale height of the day-and-night mean Mars-DTM profile at h_km (for the docs and the figures)."""
    lst = np.arange(0.0, 24.0, 2.0)
    r = [np.mean([table.density(h * 1e3, 0.0, x, ls, f_mars) for x in lst]) for h in (h_km - 5.0, h_km + 5.0)]
    return 10.0 / math.log(r[0] / r[1])


MODELS = [OrbitDecay]
