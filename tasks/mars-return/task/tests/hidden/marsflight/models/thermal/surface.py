"""The cold a lander's tanks and heaters work against on the Mars surface, hour by hour and season by season (gate G18).

The thermal sink is the radiative environment of a surface that sees half sky and half ground at Jezero, as
Perseverance's MEDA Thermal Infrared Sensor measured it (PDS Atmospheres node, bundle mars2020_meda, DER_TIRS files;
2,967 hour bins of 155 sols over two Mars years, with the Sun out of the sensors' view and the ground out of the rover's
shadow; results/issue-13/data/jezero_meda_hourly.csv). A sum of harmonics: rows 1, cos x, sin x, cos 2x, sin 2x, cos 3x,
sin 3x of the local true solar time x (360 deg a sol), times columns 1, cos Ls, sin Ls, cos 2Ls, sin 2Ls. It runs
from about 170 to 178 K before dawn to 229 to 244 K early in the afternoon. In every 30 deg season its hourly means are
within 6.9 K of MEDA's (4.2 K rms for single hours); fitted to the first year alone it is within 5.3 K rms of the
second. Each world's dispersed ambient temperature (environment.Dispersions.mars_ambient_T, mean 205 K) shifts it.

Tank heaters hold storable propellant 10 K above its freezing point against this sink through the insulation's
conductance (vehicle field ``surface_insulation_W_per_K_m2``), step by step through the sol (models/power/arrays.HOURS),
so their need peaks before dawn, when the battery carries it.
"""
from __future__ import annotations

import math

from ...core.model import Model
from ..power.arrays import HOURS

JEZERO_SINK_COEF = [
    [198.289, -3.609, 3.434, 1.307, 0.418],
    [-24.960, 0.201, -1.883, -1.280, -0.048],
    [-11.871, 0.442, -0.170, -0.722, 0.213],
    [9.325, 0.039, -0.931, 0.762, 0.017],
    [3.263, -0.462, -0.709, 0.359, 0.183],
    [-1.147, -0.073, 1.730, -0.050, -0.008],
    [0.281, 0.224, 0.666, -0.008, -0.151],
]
V3_SINK_K = 205.0                  # Dispersions.mars_ambient_T's mean, used all day on v3
HEATER_MARGIN_K = 10.0             # kept above the freezing point (sim.py resources, unchanged)


def surface_sink_K(ls_deg: float, ltst_h: float) -> float:
    """The sink (K) at Jezero at solar longitude Ls and local true solar time (h)."""
    x, y = math.radians(15.0 * ltst_h), math.radians(ls_deg)
    day = [1.0, math.cos(x), math.sin(x), math.cos(2 * x), math.sin(2 * x), math.cos(3 * x), math.sin(3 * x)]
    year = [1.0, math.cos(y), math.sin(y), math.cos(2 * y), math.sin(2 * y)]
    return sum(a * sum(c * b for c, b in zip(row, year)) for a, row in zip(day, JEZERO_SINK_COEF))


def sink_by_hour(ls_deg: float, shift_K: float = 0.0) -> list:
    """The sink (K) at each step of a sol (arrays.HOURS), shifted by shift_K."""
    return [surface_sink_K(ls_deg, h) + shift_K for h in HOURS]


def heaters_by_hour(tanks, sinks) -> list:
    """Heater power (W) at each step: tanks is a list of (conductance W/K, temperature to keep K)."""
    return [sum(ua * max(0.0, keep - T) for ua, keep in tanks) for T in sinks]


class SurfaceThermal(Model):
    """The surface sink and tank heaters, for the core's surface power balance (sim.py ``resources``)."""
    name = "surface_thermal"
    area = "thermal"
    order = 56

    def configure(self, ctx):
        self.ctx = ctx
        self.shift_K = float(getattr(ctx.world, "mars_ambient_T", V3_SINK_K)) - V3_SINK_K if ctx.world else 0.0

    def design_fields(self):
        return ["elements[].surface_insulation_W_per_K_m2"]

    def sinks(self, ls_deg):
        return sink_by_hour(ls_deg, self.shift_K)


MODELS = [SurfaceThermal]
