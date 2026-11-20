"""Stagnation-point convective heating from tables built offline. The live code only reads.

``data/heating/stagnation.json`` holds, for CO2 and for air, the heating coefficient
K(V, rho) = q / (sqrt(rho / R_eff) V^3) on a grid of velocity and free-stream density, from the
Fay-Riddell relation with Mutation++ equilibrium shock-layer and wall properties (fully catalytic wall at
1500 K), checked against SU2-NEMO and Eilmer runs and MSL's MEDLI plugs. Sutton-Graves is the special
case of one constant K per gas.

R_eff is the effective nose radius: a sphere-cone's stagnation-point velocity gradient is set by the whole
forebody, not only its nose cap, so R_eff = f(half-angle, rn / rb) x rn, with f from SU2 Euler solutions
of the stagnation region (table "r_eff").

See ``docs/reference/heating-tables.md`` for the sources, limits and checks.
"""
from __future__ import annotations

import functools
import json
import math
import os

from ..aero.tables import _interp_nd

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "heating")


@functools.lru_cache(maxsize=None)
def _load(name):
    with open(os.path.join(DATA, name)) as f:
        return json.load(f)


class StagnationHeatingTable:
    def __init__(self, name="stagnation.json"):
        self.t = _load(name)

    def k(self, gas, V, rho, catalytic=True):
        """Heating coefficient; catalytic=False gives the non-catalytic wall (no recombination at the wall)."""
        g = self.t["gases"][gas]
        key = "K" if catalytic else "K_noncatalytic"
        return _interp_nd([g["velocity_m_s"], g["log10_density"]], g[key], (V, math.log10(max(rho, 1e-30))))

    def r_eff(self, half_angle_deg, rn, rb):
        f = self.t["r_eff"]
        ratio = _interp_nd([f["half_angle_deg"], f["rn_over_rb"]], f["factor"], (half_angle_deg, rn / rb))
        return ratio * rn

    def convective(self, gas, V, rho, rn, rb=None, half_angle_deg=None, catalytic=True):
        """W/m^2 at the stagnation point. With rb and half_angle_deg the effective radius is used; without
        them rn is taken as it is (a sphere). catalytic=True is the fully catalytic wall Sutton-Graves assumes;
        False is a non-catalytic wall (closer to PICA in CO2, which MSL's reconstruction treats as nearly so)."""
        R = self.r_eff(half_angle_deg, rn, rb) if (rb and half_angle_deg is not None) else rn
        return self.k(gas, V, rho, catalytic) * math.sqrt(rho / R) * V ** 3


try:
    from ...core.model import Model

    class HeatingTables(Model):
        """Holds the stagnation heating table reader. The core wires it into models/entry/heating.py."""
        name = "heating_tables"
        area = "entry"
        order = 20

        def configure(self, ctx):
            self.table = StagnationHeatingTable()

    MODELS = [HeatingTables]
except ImportError:
    MODELS = []
