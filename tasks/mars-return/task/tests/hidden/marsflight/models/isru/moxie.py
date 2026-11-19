"""ISRU oxygen fitted to MOXIE on Perseverance.

MOXIE made 122 g of oxygen in 16 runs on Mars (NASA, September 2023). Integrated here from the 1 Hz derived data in the
PDS MOXIE bundle (atmos.nmsu.edu/PDS/data/PDS4/Mars2020/moxie_bundle, data_derived; results/issue-13/data/
moxie_runs.csv and moxie_power_vs_rate.csv): the plant draws 149.5 W + 14.56 W per g/h, so 47% of its full power
(320 W at 12 g/h) keeps the 800 C stack hot before any oxygen comes, and a cold start takes about 2 hours and 376 Wh of
heating, 1.18 hours at full power. Runs 1 to 7 give 49.9 g as in Hoffman et al. (2022), Science Advances 8, eabp8636,
Table 1; the 4 A supply limit (12 g/h) and the 17.8 kg mass are from Hecht et al. (2021), Space Science Reviews 217, 9.
Liquefaction uses the simulator's cryocooler model. The compressor's intake follows the air density, so a thin season
caps the output.
"""
from __future__ import annotations

from ... import data as D
from ...core.model import Model
from ..power import arrays as A
from ..power import eclipse as E

ISRU = {"idle_fraction": 0.467, "marginal_Wh_per_kg": 14560.0, "warmup_hours": 1.175}
O2_GAS_CP = 918.0            # J/kg/K, oxygen gas near 300 K
V3_KG_PER_SOL_PER_KW = 0.75  # data.ISRU_KG_PER_SOL_PER_KW on v3, before this change


def wh_per_kg(ambient_K: float) -> float:
    """Energy (Wh) per kg of oxygen beyond the plant's idle draw: electrolysis and compression at MOXIE's measured rate,
    plus cooling the gas from ambient and condensing it with the cryocooler model."""
    lox = D.PROPELLANTS["LOX"]
    liquefy_J = (O2_GAS_CP * max(0.0, ambient_K - lox["T"]) + lox["hvap"]) * D.CRYOCOOLER["LOX"]["w_per_w"]
    return ISRU["marginal_Wh_per_kg"] + liquefy_J / 3600.0


def oxygen_kg(power_W, rated_W, hours, ambient_K, density_factor, heat_left_Wh=None):
    """Oxygen (kg) a MOXIE-class plant rated at rated_W makes in `hours` on power_W, and the heat-up energy (Wh) still
    needed after that time. Below its idle draw the plant cannot hold its stack hot and stops; then it is cold (None)
    and must heat up again."""
    idle = ISRU["idle_fraction"] * rated_W
    if rated_W <= 0 or power_W <= 0 or power_W < idle:
        return 0.0, None
    heat = ISRU["warmup_hours"] * rated_W if heat_left_Wh is None else heat_left_Wh
    energy = power_W * hours
    if energy <= 0:
        return 0.0, heat
    used = min(heat, energy)
    per_kg = wh_per_kg(ambient_K)
    full = (1.0 - ISRU["idle_fraction"]) * rated_W / per_kg
    rate = min((min(power_W, rated_W) - idle) / per_kg, full * min(1.0, density_factor))
    return rate * hours * (1.0 - used / energy), heat - used


def plant_power_W(rated_W, spare_W, load_W, normal_W, lat_deg, ls_deg, usable_Wh, night_Wh):
    """Power (W) the plant can draw around the clock: no more than its rating or the power to spare, with the battery
    able to carry it through the night and its night energy losing the round trip."""
    loads = load_W if isinstance(load_W, (list, tuple)) else [load_W] * A.STEPS

    def fits(p):
        night = A.night_deficit_Wh([w + p for w in loads], normal_W, lat_deg, ls_deg)
        return night <= usable_Wh and p + (night - night_Wh) * (1.0 / E.BATTERY_ROUND_TRIP - 1.0) / A.SOL_H <= spare_W

    hi = min(rated_W, spare_W)
    if hi <= 0 or fits(hi):
        return max(0.0, hi)
    lo = 0.0
    for _ in range(20):
        mid = 0.5 * (lo + hi)
        lo, hi = (mid, hi) if fits(mid) else (lo, mid)
    return lo


class MoxieIsru(Model):
    """Holds the plant's heat-up state between surface steps; the core's surface power balance calls ``step``."""
    name = "isru_moxie"
    area = "isru"
    order = 62

    def configure(self, ctx):
        self.ctx = ctx
        self.heat_Wh = None

    def design_fields(self):
        return ["elements[].isru_kW"]

    def step(self, power_W, rated_W, hours, ambient_K, density_factor):
        kg, self.heat_Wh = oxygen_kg(power_W, rated_W, hours, ambient_K, density_factor, self.heat_Wh)
        return kg

    def stop(self):
        self.heat_Wh = None


MODELS = [MoxieIsru]
