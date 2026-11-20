"""Small forces in cruise: outgassing, thermal radiation and attitude-control leaks.

Each is a force of micronewtons, given once in the body frame through the Model ``forces`` hook, on coasts outside
an atmosphere only. Their sizes are drawn per flight from the model's own random stream (``ctx.rng``), so adding
them moves no other draw. The navigation filter does not know them exactly: what it does not know is its process
noise (``process_noise``), on top of the floor in filter.NAV.

Sizes. These are order of magnitude values for a spacecraft of about a tonne, not fits to one mission:
- Outgassing: water and solvents leaving blankets and composites after launch push the vehicle with a force that
  decays over weeks. F0 is log-normal with median 20 uN and a factor 3 spread, e-folding time 30 days (navigation
  teams fit such a decaying term in early cruise; Rosetta and Juno both needed one).
- Thermal radiation: heat leaving one side more than the other. F = asymmetry * P / c, with P the heat the vehicle
  sheds (the design's ``thermal_power_W``, default 1,000 W, plus the sunlight it absorbs) and an asymmetry drawn
  between 0.02 and 0.15. A 2 kW RTG with 10% asymmetry gives 0.7 uN.
- Attitude-control leaks: the steady seep past closed thruster valves (not the failed-valve leaks, which
  MARSFLIGHT_FAILURES models). With probability 0.3 a flight has one, of 1 to 20 uN, along a drawn body direction.

MARSFLIGHT_NAV switches it (switch.py). Off, ``forces`` returns None, and today the core does not yet call the
``forces`` hook at all (core/arcs.py; see the PR's "Not done").
"""
from __future__ import annotations

import math

import numpy as np

from ...core.model import Model
from . import switch

C = 299792458.0
SOLAR_FLUX_1AU = 1361.0
AU = 1.495978707e11
DAY = 86400.0

SMALL_FORCES = {
    "outgas_median_N": 20e-6, "outgas_spread": 3.0, "outgas_efold_s": 30 * DAY,
    "thermal_power_W": 1000.0, "thermal_asym": (0.02, 0.15), "absorptivity": 0.3,
    "leak_prob": 0.3, "leak_N": (1e-6, 20e-6),
    # the filter's knowledge: the leak and thermal terms to 30%, outgassing to 50%, over a 1 day correlation time
    "know_frac": {"outgas": 0.5, "thermal": 0.3, "leak": 0.3}, "corr_s": DAY,
}


def _unit(rng):
    v = rng.normal(0.0, 1.0, 3)
    return v / np.linalg.norm(v)


class SmallForces(Model):
    name = "small_forces"
    area = "navigation"
    order = 40

    def configure(self, ctx):
        p = SMALL_FORCES
        rng = ctx.rng(self.name)
        self.t_launch = float(getattr(ctx, "t_launch", 0.0) or 0.0)
        self.outgas_F0 = p["outgas_median_N"] * math.exp(math.log(p["outgas_spread"]) * rng.normal())
        self.outgas_dir = _unit(rng)
        design = ctx.design or {}
        self.thermal_P = float(design.get("thermal_power_W", p["thermal_power_W"]))
        self.thermal_asym = float(rng.uniform(*p["thermal_asym"]))
        self.thermal_dir = _unit(rng)
        self.area_m2 = float(design.get("sun_area_m2", 5.0))
        leak = rng.uniform() < p["leak_prob"]
        self.leak_F = float(rng.uniform(*p["leak_N"])) if leak else 0.0
        self.leak_dir = _unit(rng)
        return self

    def parts(self, t, sun_dist_m=AU):
        """Each force (N, body frame) at time t, with the vehicle sun_dist_m from the Sun."""
        age = max(0.0, t - self.t_launch)
        outgas = self.outgas_F0 * math.exp(-age / SMALL_FORCES["outgas_efold_s"]) * self.outgas_dir
        absorbed = SMALL_FORCES["absorptivity"] * SOLAR_FLUX_1AU * (AU / max(sun_dist_m, 1e9)) ** 2 * self.area_m2
        thermal = self.thermal_asym * (self.thermal_P + absorbed) / C * self.thermal_dir
        leak = self.leak_F * self.leak_dir
        return {"outgas": outgas, "thermal": thermal, "leak": leak}

    def forces(self, t, state, arc):
        if not switch.on() or arc.in_atmosphere or arc.thrusting:
            return None
        sun = (state or {}).get("sun_dist_m", AU) if isinstance(state, dict) else AU
        return sum(self.parts(t, sun).values()), None

    def process_noise(self, mass_kg, t, sun_dist_m=AU):
        """Velocity random walk (m^2/s^3) for what the filter does not know of these forces: sigma_a^2 * tau."""
        k = SMALL_FORCES["know_frac"]
        parts = self.parts(t, sun_dist_m)
        var = sum((k[n] * float(np.linalg.norm(f)) / max(mass_kg, 1.0)) ** 2 for n, f in parts.items())
        return var * SMALL_FORCES["corr_s"]


MODELS = [SmallForces]
