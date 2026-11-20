"""Temperature of the samples from the time they are loaded on Mars to the landing on Earth.

MSR's requirement: the sample tubes stay below -20 C, with transients up to +30 C allowed (L1.5 requirement,
quoted by the MSR Temperature-Time Tiger Team, NTRS 20220010268). Above +30 C the samples are taken as lost to
science. The model is one lumped node, the Orbiting Sample container, that relaxes towards the temperature of
its surroundings with a time constant TAU_S:

- On Mars: the lander's surroundings, the world's mean surface temperature (the core passes it).
- In space (Mars orbit, cruise): a sphere in sunlight at r AU settles at 278.6 K (alpha/epsilon)^0.25 / sqrt(r).
  alpha/epsilon is the carrier's thermal finish (design field ``sample_alpha_over_eps`` on the capsule). The
  default 0.5 puts the samples at -39 C at 1 AU, the -40 C the Tiger Team expected before Earth entry (Table 1).
- Mars ascent: the Tiger Team's expected +30 C transient (Table 1) is recorded; at the limit, not over it.
- Earth entry: heat soaks back from the heat shield. MSR's entry vehicle took its bondline to at most 370 C
  (Mitcheltree et al. 2001) and its samples from -40 C to at most +25 C (Tiger Team Table 1), so the samples rise
  0.16 of the bondline's rise. The bondline's rise is taken in proportion to the capsule's heat load over its TPS
  capacity (the same correlation sim.py sizes the heat shield with). An engineering assumption, stated here.

Not modeled: the hours on the ground in Utah before recovery (the Tiger Team allows < +30 C), heaters, and the
soak-back peak after landing.
"""
from __future__ import annotations

import json
import math
import os

from ...core.model import Model

_DATA = json.load(open(os.path.join(os.path.dirname(__file__), "..", "..", "data", "samples", "msr.json")))
LIMIT_C = _DATA["temperature"]["limit_C"]
GOAL_C = _DATA["temperature"]["goal_C"]
BONDLINE_C = _DATA["temperature"]["edl_anchor"]["bondline_limit_C"]
COUPLING = _DATA["temperature"]["edl_anchor"]["coupling"]
ASCENT_C = 30.0              # Tiger Team Table 1, MAV launch, minutes
TAU_S = 86400.0              # time constant of the container in its carrier (assumption: about a day)
ALPHA_EPS_DEFAULT = 0.5
K0 = 273.15


def space_equilibrium_C(r_au, alpha_over_eps=ALPHA_EPS_DEFAULT):
    """Radiative equilibrium of a sphere in sunlight (C)."""
    return 278.6 * alpha_over_eps ** 0.25 / math.sqrt(r_au) - K0


def edl_rise_C(T_pre_C, heat_load_Jcm2, capacity_Jcm2):
    """Rise of the samples during Earth entry (C)."""
    frac = max(0.0, heat_load_Jcm2) / max(capacity_Jcm2, 1e-9)
    return COUPLING * max(0.0, BONDLINE_C - T_pre_C) * frac


class SampleTemperature:
    def __init__(self, T0_C, alpha_over_eps=ALPHA_EPS_DEFAULT):
        self.T = T0_C
        self.peak = T0_C
        self.peak_phase = "loaded"
        self.ae = alpha_over_eps
        self.lost = None

    def _note(self, phase):
        if self.T > self.peak:
            self.peak, self.peak_phase = self.T, phase
        if self.T > LIMIT_C and self.lost is None:
            self.lost = (f"the samples reached {self.T:.1f} C during {phase}, above MSR's +{LIMIT_C:.0f} C limit: "
                         "their science is lost")
        return self.lost

    def relax(self, dt, T_env_C, phase):
        self.T = T_env_C + (self.T - T_env_C) * math.exp(-max(dt, 0.0) / TAU_S)
        return self._note(phase)

    def space(self, dt, r_au, phase="cruise"):
        return self.relax(dt, space_equilibrium_C(r_au, self.ae), phase)

    def ascent(self):
        T = self.T
        self.T = max(self.T, ASCENT_C)
        msg = self._note("Mars ascent")
        self.T = T
        return msg

    def earth_entry(self, heat_load_Jcm2, capacity_Jcm2):
        self.T = self.T + edl_rise_C(self.T, heat_load_Jcm2, capacity_Jcm2)
        return self._note("Earth entry")


class SampleThermal(Model):
    """Wiring,:
    - ``loaded(T_mars_K)`` where the samples are loaded (``core/mission.py``, the ``samples_loaded`` block at line
      508), with ``self.disp.mars_ambient_T``;
    - ``surface(dt, T_mars_K)`` from ``models/power/resources.py`` ``resources(dt, 'mars_surface')``;
    - ``ascent()`` at Mars liftoff with samples aboard (``models/propulsion/engines.py`` ``liftoff``);
    - ``space(dt, r_au)`` from ``resources(dt, ...)`` off the surface (it already computes ``_sun_distance_au``);
    - ``earth_entry(Q, Q_cap)`` at Earth touchdown (``models/landing/surface.py`` ``touchdown``, Earth branch) with
      the capsule's heat shield load and capacity
      (``self.heat_load[shield] / 1e4`` and ``self.track['heat_capacity_Jcm2'][shield]``).
    Each returns a message when the samples are lost (> +30 C); the core records it as the loss of the samples.
    ``obs`` gives the container's temperature: a real container carries a thermometer."""
    name = "sample_thermal"
    area = "samples"
    order = 70

    def configure(self, ctx):
        self.ctx = ctx
        cap = next((e for e in (ctx.design or {}).get("elements", []) if e.get("kind") == "capsule"), {}) or {}
        self.ae = float(cap.get("sample_alpha_over_eps", ALPHA_EPS_DEFAULT))
        self.node = None

    def loaded(self, T_mars_K):
        self.node = SampleTemperature(T_mars_K - K0, self.ae)

    def surface(self, dt, T_mars_K):
        return self.node.relax(dt, T_mars_K - K0, "the wait on Mars") if self.node else None

    def ascent(self):
        return self.node.ascent() if self.node else None

    def space(self, dt, r_au):
        return self.node.space(dt, r_au) if self.node else None

    def earth_entry(self, Q, Q_cap):
        return self.node.earth_entry(Q, Q_cap) if self.node else None

    def obs(self, state):
        return {"sample_temperature_C": round(self.node.T, 1)} if self.node else {}

    def design_fields(self):
        return ["elements[kind=capsule].sample_alpha_over_eps"]


MODELS = [SampleThermal]
