"""Entry heating and enclosed elements.

Methods of Mission, moved verbatim from sim.py. Mission inherits them from HeatingMixin."""
from __future__ import annotations

import math
import os
import time
import zlib
from dataclasses import dataclass, field
import numpy as np
from ... import data as D
from ... import vibro
from ... import terrain
from ...environment import EARTH_SOI, MARS_SOI, MARS_REF_RADIUS, Dispersions, World
from ...core.common import (  # noqa: F401
    MILESTONES, MILESTONE_WEIGHTS, ATM_TOP, EARTH_R_EQ, PARTIAL_APPROACH_D0, SCENARIO_PHASES, STATE_AFTER, FlightFailure, interp, unit, rotate_towards, newtonian_cd, parachute_peak_load, great_circle_km, ElementState,
)


class HeatingMixin:
    def _check_enclosed(self, se):
        """During entry heating only the heat shield, what rides inside its aeroshell, its backshell and
        the parachutes on them are protected. Anything else on the vehicle burns up."""
        ok = {se.id} | set(se.spec.get("_inside", []))
        if se.spec.get("_backshell"):
            ok.add(se.spec["_backshell"])
        for s in self.present():
            e = s.el
            if e.id in ok or (e.kind == "parachute" and getattr(e, "carrier", None) in ok):
                continue
            self.fail(f"{e.id} rode outside the aeroshell of {se.id} during entry and burned up (only what sits "
                      "between a heatshield and its backshell, or on a capsule, is protected)")

    def _check_heat_load(self, shield, se):
        """The heat load the shield has taken so far against what its TPS layer absorbs before the bond line gets
        too hot (its areal mass, through the TPS correlation, times the hidden truth). Its own method
       , so a test can call it."""
        mat = D.TPS[se.tps]
        tps_true = (self.truth.get("tps") or {}).get(se.tps) or {}
        Q_jcm2 = self.heat_load.get(shield, 0.0) / 1e4
        cap = (se.tps_areal / mat["k_mass"]) ** (1 / D.TPS_EXPONENT) * tps_true.get("capacity", 1.0)
        self.track.setdefault("heat_capacity_Jcm2", {})[shield] = cap
        if Q_jcm2 > cap:
            self.fail(f"heatshield {se.id} burned through: heat load {Q_jcm2:.0f} J/cm^2 exceeds the "
                      f"{cap:.0f} J/cm^2 that {se.tps_areal:.1f} kg/m^2 of {se.tps} can absorb")

    def _stag_heating(self, rho, V, aero, lead):
        """Stagnation-point heat flux (W/m^2) on whatever leads: Sutton-Graves convective, calibrated to flight
        (data.HEATING_CONVECTIVE_FACTOR), plus Tauber-Sutton radiative (Earth above 9 km/s, Mars above 6 km/s),
        times the explicit margin (data.HEATING_MARGIN) and this world's dispersion."""
        central = self.central
        rn = aero["rn"] if aero and aero.get("rn") else max(0.3, (lead.diameter if lead else 1.0) / 2)
        qc = D.SUTTON_GRAVES_K[central] * D.HEATING_CONVECTIVE_FACTOR[central] * math.sqrt(rho / rn) * V ** 3
        qr = 0.0
        if central == "Earth" and V > 9000:
            a_ = min(1.0, 1.072e6 * V ** -1.88 * rho ** -0.325)
            qr = 4.736e4 * rn ** a_ * rho ** 1.22 * interp(D.TAUBER_SUTTON_EARTH, V) * 1e4
        elif central == "Mars" and V > 6000:
            qr = 2.35e4 * rn ** 0.526 * rho ** 1.19 * interp(D.TAUBER_SUTTON_MARS, V) * 1e4
        return (qc + qr) * D.HEATING_MARGIN * self.disp.heating_factor

    def _temp_guess(self, alt):
        if self.central == "Earth":
            return 288.15 - 0.0065 * min(alt, 11000) if alt < 20000 else 216.65 + 0.001 * min(max(alt - 20000, 0), 30000)
        from ...environment import mars_temperature
        return mars_temperature(alt / 1e3)
