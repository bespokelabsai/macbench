"""Engine dry mass from a correlation fitted to 36 flown engines (open item 1).

Switch: MARSFLIGHT_ENGINE_MASS_FIT=1 gives every engine a design builds itself (not a catalog engine) this mass in
vehicle.build_vehicle. Unset or 0 (the default), engines keep the v3 engine model's mass.

Form. For an engine of vacuum thrust F, chamber pressure pc and cycle c:

    tw = tw_at_pc_ref[c] * (F / 1 MN)^0.08 * (pc / pc_ref[c])^pc_exponent[c]
    m  = F / (g0 * tw), times the hydrogen factor k[c] when the fuel is LH2
    m  = m + 15 kg/m^2 * (Ae - 40 At) when the expansion ratio is above 40
    m  = m * 1.10 with a pintle injector, plus the igniter mass (data.IGNITERS)

The nozzle term, the pintle factor and the igniter are the same as in models/propulsion/engine.py. What is new is
the hydrogen factor for each cycle (gas generator 1.31, expander 1.26, staged fuel-rich 1.28; other cycles use the
pooled 1.28) and the chamber pressure term, with one exponent per cycle. pc_ref[c] is 0.6 times the cycle's pc
limit. The coefficients are in data/structures/engine_mass.json.

Fit. Each flown engine was built with the v3 engine model from its published chamber pressure, mixture ratio,
expansion ratio and thrust, the way the engine fit builds them. The coefficients were then fitted by least squares on the
log of the mass. Each tw is pulled toward its old value with the weight of one engine, each cycle's hydrogen factor
toward the pooled one, and each cycle's pressure exponent toward a common one.

Known outlier. The Merlin 1D is held out of the fit. It is a production-scale, low-margin design, and no other
gas-generator engine in the data is nearly as light for its thrust. The fit to the other engines puts it at
1,308 kg against its published 470 kg (+178%); every form tried gives +139% to +185%. A design that wants a Merlin
buys it from the catalog (economics.CATALOG_ENGINES "Merlin 1D"), which carries the published 470 kg, and catalog
engines never use this correlation.

Error, on the 36 engines. Leave-one-out mean absolute error is 21.3% (median 19.6%, max 88%), against 26.1% (median
18.5%, max 108%) for the v3 model and 21.0% (median 14.5%, max 98%) for the refit. So the gain over the earlier fit is
in the worst engines, not the typical one. A design year term in place of the pressure term fits about as well
(21.0%, median 12.7%), but a design has no year, so it is not used. Leave-one-out: F-1 -8%, J-2 -21%, RS-68 -30%,
RL10B-2 -13%, RS-25 +13%.

Sources. The engine data are in our validation notes (not included), each value with its
own source link. The fit is results/issue-10/engine_mass_fit/fit_engine_mass.py and its results file is
results_engine_mass.json in the same folder.
"""
from __future__ import annotations

import functools
import json
import math
import os

from ... import data as D

COEF_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "structures", "engine_mass.json")


def fit_enabled() -> bool:
    """MARSFLIGHT_ENGINE_MASS_FIT=1 turns the fitted engine mass on; off by default."""
    return os.environ.get("MARSFLIGHT_ENGINE_MASS_FIT", "1") == "1"


@functools.lru_cache(maxsize=1)
def coefficients() -> dict:
    with open(COEF_PATH) as f:
        return json.load(f)


def engine_mass_kg(en) -> float:
    """Dry mass in kg of a built marsflight Engine, from the fitted correlation in engine_mass.json."""
    c = coefficients()
    cyc = en.cycle
    thrust = float(en.thrust_vac)
    pc_bar = max(float(en.pc) / 1e5, 1.0)
    c_pc = c["pc_exponent"][cyc] if isinstance(c["pc_exponent"], dict) else c["pc_exponent"]
    tw = (c["tw_at_pc_ref"][cyc] * (max(thrust, 1e3) / 1e6) ** c["thrust_exponent"]
          * (pc_bar / c["pc_ref_bar"][cyc]) ** c_pc)
    mass = thrust / (D.G0 * tw)
    if en.fuel == "LH2":
        mass *= c["hydrogen_factor"].get(cyc, c["hydrogen_factor_pooled"])
    if en.eps > 40:
        mass += c["nozzle_kg_per_m2"] * (en.Ae - 40 * en.At)
    spec = en.spec or {}
    if spec.get("injector", "standard") == "pintle":
        mass *= c["pintle_factor"]
    hypergolic = D.PAIRS.get((en.fuel, en.ox), {}).get("hypergolic", False)
    igniter = spec.get("igniter", "hypergolic" if hypergolic else "spark_torch")
    if igniter not in D.IGNITERS:
        igniter = "spark_torch"
    units = 1
    if igniter == "tea_teb":
        try:
            units = min(max(int(float(spec.get("igniter_units", 1))), 1), 50)
        except (TypeError, ValueError):
            units = 1
    return float(mass + D.IGNITERS[igniter]["mass_kg"] * units)
