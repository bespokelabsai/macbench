"""Ageing in space: the chance that a propulsion system (valves, seals, regulators, engines) has failed by the time
it is used again after long storage in space.

Source: J. H. Saleh, F. Geng, M. Ku and M. L. R. Walker, "Electric propulsion reliability: statistical analysis of
on-orbit anomalies and comparative analysis of electric versus chemical propulsion failure rates", Acta Astronautica
139 (2017), doi:10.1016/j.actaastro.2017.06.034 (arXiv 1706.10129), Fig. 15: chemical propulsion anomalies per
spacecraft-day on orbit. Fatal ones: 6.71e-6 a day for spacecraft launched 1997 to 2004 and 3.50e-6 a day for 2005 to
2015, that is 2.4e-3 and 1.3e-3 a year. These are operating GEO spacecraft, not dormant stages; no published rate for
dormant valves and seals was found (results/issue-73/README.md), so the recent fatal rate is used as a constant hazard
from launch for each stage that carries propellant, and a stage's engines fail at their first start after its
drawn failure time.

The draw comes from the model's own stream (ctx.rng("storage_ageing")), one failure time per stage, so it never moves
another model's draws, and the same seed gives the same world. Over a 2.6 year mission the chance per stage is 0.3%.
"""
from __future__ import annotations

import math

from ...core.model import Model

FATAL_PER_YEAR = 3.50e-6 * 365.25        # chemical propulsion, spacecraft launched 2005 to 2015 (Saleh et al. 2017)
FATAL_PER_YEAR_OLDER = 6.71e-6 * 365.25  # 1997 to 2004
YEAR_S = 365.25 * 86400.0


def failure_probability(years: float, rate_per_year: float = FATAL_PER_YEAR) -> float:
    """Chance that a stage's propulsion has failed after `years` in space."""
    return 1.0 - math.exp(-rate_per_year * max(0.0, years))


class StorageAgeing(Model):
    """Draws one failure time per propulsion stage at configure; the core calls ``on_event`` at each engine start."""
    name = "storage_ageing"
    area = "thermal"
    order = 58

    def configure(self, ctx):
        self.ctx = ctx
        rng = ctx.rng(self.name)
        self.fail_after_s = {}
        els = getattr(ctx.vehicle, "elements", None) or []
        for e in els:
            if getattr(e, "is_stage", False) and getattr(e, "tanks", None):
                self.fail_after_s[e.id] = float(rng.exponential(YEAR_S / FATAL_PER_YEAR))

    def on_event(self, event, state):
        if event.kind not in ("ignition", "engine_start") or event.element not in self.fail_after_s:
            return
        stored = event.t - state.t0
        if stored > self.fail_after_s[event.element]:
            from ...core.common import FlightFailure
            raise FlightFailure(f"{event.element}'s propulsion failed to start after {stored / YEAR_S:.1f} years in "
                                "space (a valve, seal or regulator aged out in storage)")


MODELS = [StorageAgeing]
