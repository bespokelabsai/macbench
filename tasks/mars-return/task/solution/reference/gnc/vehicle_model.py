"""What the flight software knows about its own vehicle, from the check report passed to reset()."""
from __future__ import annotations

import math

from .util import G0


class VehicleModel:
    def __init__(self, info):
        rep = info["vehicle"]
        self.engines = rep["engines"]
        self.elements = {e["id"]: e for e in rep["elements"]}
        self.order = [e["id"] for e in rep["elements"]]
        # what the capsule brings home: its Orbiting Sample containers (SampleContainmentAssembly.sampleContainers)
        self.sample_kg = next((float(e.get("sample_mass_kg", 11.0)) for e in rep["elements"] if e.get("kind") == "capsule"), 11.0)

    def engine_of(self, eid):
        e = self.elements[eid]
        if not e.get("engines"):
            return None, 0
        en = e["engines"][0]
        return self.engines[en["engine"]], en["count"]

    def thrust_vac(self, eid, n=None, throttle=1.0):
        en, cnt = self.engine_of(eid)
        n = cnt if n is None else n
        return en["thrust_vac_kN"] * 1e3 * n * throttle

    def mdot(self, eid, n=None, throttle=1.0):
        en, cnt = self.engine_of(eid)
        n = cnt if n is None else n
        return en["mdot_kg_s"] * n * throttle

    def isp(self, eid):
        en, _ = self.engine_of(eid)
        return en["isp_vac_s"]

    def exit_area(self, eid):
        en, _ = self.engine_of(eid)
        return math.pi * en["exit_diameter_m"] ** 2 / 4

    def thrust_at(self, eid, p_amb, n=None, throttle=1.0):
        en, cnt = self.engine_of(eid)
        n = cnt if n is None else n
        return max(0.0, n * (throttle * en["thrust_vac_kN"] * 1e3 - p_amb * math.pi * en["exit_diameter_m"] ** 2 / 4))

    def min_throttle(self, eid):
        en, _ = self.engine_of(eid)
        return en["min_throttle"]

    def dry(self, eid):
        return self.elements[eid]["dry_mass_kg"]


def stack_mass(obs, model):
    """Mass from the propellant reported per element and the nominal dry masses (dry mass dispersion
    is 1%); RCS propellant included."""
    m = 0.0
    for e in obs["elements"]:
        m += model.dry(e["id"]) + e.get("fuel_kg", 0.0) + e.get("ox_kg", 0.0) + e.get("rcs_kg", 0.0)
    if obs.get("samples_loaded"):
        m += model.sample_kg
    return m


def present(obs):
    return [e["id"] for e in obs["elements"]]


def element(obs, eid):
    for e in obs["elements"]:
        if e["id"] == eid:
            return e
    return None


def prop_left(obs, eid):
    e = element(obs, eid)
    if e is None:
        return 0.0
    return e.get("fuel_kg", 0.0) + e.get("ox_kg", 0.0)


def accel_meas(obs):
    sa = obs.get("sensed_accel")
    if not sa:
        return 0.0
    return math.sqrt(sum(c * c for c in sa))


def rocket_tgo(dv, m, mdot, isp):
    """Burn time for dv with constant mass flow."""
    ve = isp * G0
    return m / mdot * (1 - math.exp(-dv / ve))
