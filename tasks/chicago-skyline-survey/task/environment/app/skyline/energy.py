"""Rotary-wing power model (Zeng, Xu and Zhang 2019).

Forward flight at horizontal airspeed V:

    P(V) = P0 * (1 + 3 V^2 / U_tip^2)
         + Pi * sqrt( sqrt(1 + V^4 / (4 v0^4)) - V^2 / (2 v0^2) )
         + 0.5 * d0 * rho * s * A * V^3

Climbing at vertical rate vz > 0 adds W * vz / climb_efficiency; descent adds
nothing. Hovering in wind w is forward flight at V = |w|.
"""

from __future__ import annotations

import math

from .rules import Rules


class PowerModel:
    """Power draw in watts, parameterised from ``rules.json``."""

    def __init__(self, rules: Rules):
        p = rules.power_params
        self.P0 = p["P0_W"]
        self.Pi = p["Pi_W"]
        self._inv_utip2 = 1.0 / (p["U_tip_mps"] ** 2)
        v0 = p["v0_mps"]
        self._inv_2v02 = 1.0 / (2.0 * v0 * v0)
        self._inv_4v04 = 1.0 / (4.0 * v0 ** 4)
        self._k_par = 0.5 * p["d0"] * p["rho_kgpm3"] * p["s"] * p["A_m2"]
        self._climb_k = p["weight_N"] / rules.climb_efficiency

    def power(self, V: float) -> float:
        """Level-flight power P(V) at horizontal airspeed V (m/s)."""
        V2 = V * V
        induced = math.sqrt(math.sqrt(1.0 + V2 * V2 * self._inv_4v04) - V2 * self._inv_2v02)
        return self.P0 * (1.0 + 3.0 * V2 * self._inv_utip2) + self.Pi * induced + self._k_par * V2 * V

    def climb_power(self, vz: float) -> float:
        """Extra power for climbing at vz m/s (zero for vz <= 0)."""
        return self._climb_k * vz if vz > 0.0 else 0.0

    def total(self, V: float, vz: float = 0.0) -> float:
        """P(V) plus the climb term."""
        return self.power(V) + self.climb_power(vz)
