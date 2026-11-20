"""Backshell heating during entry.

Today only the heat shield is heated (sim.py ``check_atmospheric``); a backshell's ``tps`` and
``areal_mass_kg_m2`` are free choices. Real backshells see a few to ten percent of the nose heating
(data/plumes/backshell.json):

- Heat flux on the backshell = FRACTION x the stagnation-point heat flux the simulator already computes for the
  heat shield (``_stag_heating``, with its dispersion). Mars: 0.10, from Mars 2020's MEDLI2 backshell sensors
  (4.5 to 6.0 W/cm2 at peak) over the simulator's 54.5 W/cm2 stagnation heating on Mars 2020's reconstructed
  trajectory; CO2 radiation in the wake carries most of it. Earth: 0.03 (laminar convective, Edquist 2005).
- The backshell absorbs the heat load (its integral). What its TPS can take scales like the heat shield's
  correlation (areal mass ~ load^0.51575), pinned to MSL: 0.32 in of SLA-561V (2.11 kg/m2) for a design heat load
  of 377 J/cm2 (Edquist et al. 2009). Other materials scale with their k_mass in data.TPS.
- A backshell whose load passes that capacity burns through, and so does one whose peak flux passes the
  material's q_max (data.TPS).

This uses the same flow regime split as the heat shield (Sutton-Graves convective, Tauber-Sutton radiative);
turbulence on the backshell and RCS plume interference (MSL: up to 76 W/cm2 locally) are not modeled.
"""
from __future__ import annotations

import json
import os

from ... import data as D
from ...core.model import Model

_DATA = json.load(open(os.path.join(os.path.dirname(__file__), "..", "..", "data", "plumes", "backshell.json")))
FRACTION = {k: v["value"] for k, v in _DATA["fraction"].items()}
_S = _DATA["sizing"]
MSL_AREAL = _S["thickness_m"] * _S["rho_kg_m3"]     # 2.11 kg/m2
MSL_LOAD = _S["heat_load_Jcm2"]                    # 377 J/cm2


def backshell_flux(q_stag, central):
    """Backshell heat flux (W/m^2) from the stagnation-point heat flux of the leading heat shield."""
    return FRACTION.get(central, FRACTION["Earth"]) * q_stag


def capacity_Jcm2(tps, areal_kg_m2):
    """Heat load (J/cm^2) a backshell TPS of this material and areal mass takes before its bondline overheats."""
    k_ratio = D.TPS[tps]["k_mass"] / D.TPS[_S["material"]]["k_mass"]
    return MSL_LOAD * (areal_kg_m2 / (MSL_AREAL * k_ratio)) ** (1.0 / D.TPS_EXPONENT)


def needed_areal(tps, load_Jcm2):
    """Areal mass (kg/m^2) of a backshell TPS that just takes this heat load."""
    k_ratio = D.TPS[tps]["k_mass"] / D.TPS[_S["material"]]["k_mass"]
    return MSL_AREAL * k_ratio * (load_Jcm2 / MSL_LOAD) ** D.TPS_EXPONENT


class BackshellHeat:
    """One backshell's heating over an entry."""

    def __init__(self, element_id, tps, areal, central, q_max_factor=1.0, capacity_factor=1.0):
        self.id, self.tps, self.areal, self.central = element_id, tps, areal, central
        self.q_rated = D.TPS[tps]["q_max"] * q_max_factor
        self.cap = capacity_Jcm2(tps, areal) * capacity_factor
        self.load = 0.0    # J/m^2
        self.peak = 0.0
        self._prev = None

    def add(self, t, q_stag):
        """Stagnation heat flux q_stag (W/m^2) at time t; returns a failure message or None."""
        q = backshell_flux(q_stag, self.central)
        self.peak = max(self.peak, q)
        if self._prev is not None:
            t0, q0 = self._prev
            self.load += 0.5 * (q0 + q) * (t - t0)
        self._prev = (t, q)
        if q > self.q_rated:
            return (f"backshell {self.id} burned through: {q / 1e4:.1f} W/cm^2 is more than {self.tps} survives "
                    f"({self.q_rated / 1e4:.0f} W/cm^2)")
        if self.load / 1e4 > self.cap:
            return (f"backshell {self.id} burned through: heat load {self.load / 1e4:.0f} J/cm^2 is more than the "
                    f"{self.cap:.0f} J/cm^2 that {self.areal:.2f} kg/m^2 of {self.tps} takes")
        return None

    def summary(self):
        return {"id": self.id, "tps": self.tps, "areal_kg_m2": self.areal, "peak_Wcm2": round(self.peak / 1e4, 3),
                "load_Jcm2": round(self.load / 1e4, 2), "capacity_Jcm2": round(self.cap, 1)}


class BackshellHeating(Model):
    """Wiring: in ``models/structures/loads.py`` ``check_atmospheric``, the block
    ``if V > 1500 and rho > 0`` (lines 72 to 95), for every sample with a heat shield
    leading, the core calls ``heat(backshell_id, tps, areal, central, t, qt)`` with the heat shield's backshell
    (``se.spec['_backshell']``) and the same ``qt`` the heat shield gets; a returned message is a flight failure.
    The hidden TPS truth (``truth['tps'][material]``) scales ``q_max`` and ``capacity`` as for the heat shield."""
    name = "backshell_heating"
    area = "plumes"
    order = 45

    def configure(self, ctx):
        self.ctx = ctx
        self.truth = (ctx.truth or {}).get("tps") or {}
        self.shells = {}

    def heat(self, backshell_id, tps, areal, central, t, q_stag):
        b = self.shells.get(backshell_id)
        if b is None:
            tr = self.truth.get(tps) or {}
            b = self.shells[backshell_id] = BackshellHeat(backshell_id, tps, areal, central, tr.get("q_max", 1.0),
                                                          tr.get("capacity", 1.0))
        return b.add(t, q_stag)

    def obs(self, state):
        # the team sees the heat load its backshells took (as it does the heat shield's), not their capacity
        return {"backshell_heat_load_Jcm2": {k: round(b.load / 1e4, 2) for k, b in self.shells.items()}} if self.shells else {}

    def design_fields(self):
        return ["elements[kind=backshell].tps", "elements[kind=backshell].areal_mass_kg_m2"]


MODELS = [BackshellHeating]
