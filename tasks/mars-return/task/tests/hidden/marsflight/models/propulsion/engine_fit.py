"""The engine model refits, used by engine.py when MARSFLIGHT_ENGINE_FIT is on.

The engine fit was written against the old layout (marsflight/data.py and marsflight/engine.py). Its engine model changes are
ported here, behind a switch, so that the old values in data.py stay in force until the switch is turned on. The fits,
their leave-one-out errors and the flown engine data are in our validation notes (not included) on
the branch (v3-issue-1-catalog).
"""
from __future__ import annotations

import math

# A second calibration of vacuum Isp against flown regen-cooled engines: one factor for every propellant pair, a
# straight line in ln(expansion ratio), clamped to the 16:1 to 285:1 the engines span. v2 used one factor per pair
# (data.PAIRS isp_cal), which left small sea-level nozzles high (Raptor 2 +1.9%, F-1 +2.2%) and big vacuum nozzles
# low (Raptor Vacuum and Merlin 1D Vacuum -1.9%). Leave-one-out error on 20 engines: 0.59% with this line, 0.89% with
# one factor and 0.93% with one factor per pair.
ISP_CAL = {"at_eps_1": 0.94316, "per_ln_eps": 0.011618, "eps_min": 16.0, "eps_max": 285.0}

# Thrust to weight at 1 MN by cycle, fitted to the dry masses of 37 flown engines, with an engine that burns hydrogen
# ENGINE_MASS_FUEL_FACTOR times heavier (hydrogen turbopumps move far more volume). Leave-one-out mass error 25% mean
# and 16% median, against 29% and 20% for the hand-set v2 values in data.CYCLES.
CYCLE_TW = {"pressure_fed": 35.0, "electric_pump": 55.0, "gas_generator": 96.0, "expander": 83.0,
            "staged_fuel": 84.0, "staged_ox": 92.0, "full_flow": 127.0}
ENGINE_MASS_FUEL_FACTOR = {"LH2": 1.42}

# The wall heat load of a coolant's engines against the Bartz scaling in engine.py, calibrated on flown regen engines.
# The scaling from RS-25 put the flown Aestus at 0.998 of its cooling capacity at MR 1.9 and 11 bar, and 0.94 at MR 2.05
# and 10 bar (both published), so the check rejected it. With 0.92 it passes at both, and small (3 to 5 kN) or hot
# (MR 2.4) MMH regen chambers still fail.
COOLANT_Q_CAL = {"MMH": 0.92}


def isp_cal(eps: float) -> float:
    """The vacuum Isp calibration against flown engines at this expansion ratio."""
    c = ISP_CAL
    return c["at_eps_1"] + c["per_ln_eps"] * math.log(min(max(eps, c["eps_min"]), c["eps_max"]))
