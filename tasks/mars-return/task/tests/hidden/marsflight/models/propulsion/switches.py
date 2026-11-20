"""On and off switches for the propulsion models wired in by the propulsion wiring PR.

Each switch is read from the environment as MARSFLIGHT_<NAME> = 1 or 0 (also true/false, on/off). When it is not
set, the default below applies. With every switch at 0 the simulator flies exactly as v3 did before the wiring.

- ENGINE_TRANSIENTS: engine start and shutdown curves, the cutoff impulse scatter, chilldown that depends on
  the engine and its cold time, the mixture ratio command, and the Isp loss at low throttle. OFF: it needs the
  core and 6-DOF hunks listed in the PR ("Needs from other owners") and the reference fixes (staging
  after a commanded shutdown, the 0.2 g floor in the descent, cutoff lead by shutdown_impulse_s).
- TURBOPUMP: the turbopump power balance of each cycle (pump and turbine data of flown engines), the
  combustion stability margin and a start-by-start draw of instability, and (with ENGINE_TRANSIENTS) the spin-up
  time from the turbine power.
- NOZZLE_SEPARATION_PRESSURE (a cross-check finding): the nozzle flow separation check takes the ambient pressure of
  the active atmosphere model, not rho R T with a fixed lapse rate.
- PROPELLANT_GAUGING: the load spread of each tank, gauge readings in place of the true masses, the low level
  cutoff, and what each engine cycle does when a tank runs dry. OFF: it needs the reference's burnout latch
  (branch v3-reference-fixes).
- STORAGE_AGEING: a stage's propulsion that has aged out in storage fails at its next engine start.
- COMMS_CONJUNCTION: a heliocentric correction burn needs an uplink in the last 7 days and none during the
  solar conjunction moratorium.
- ENGINE_FIT: the engine model refits against flown engines (engine_fit.py): vacuum Isp
  calibrated as a line in ln(expansion ratio), engine mass per cycle with a hydrogen factor, the MMH regen heat
  load factor that lets Aestus pass, and the expander thrust caps of data.EXPANDER_FUELS enforced. OFF: it changes
  engine masses and Isp, so every design and the reference move; turn it on with a reference refit.
"""
from __future__ import annotations

import os

DEFAULTS = {
    "ENGINE_TRANSIENTS": True,
    "TURBOPUMP": True,
    "NOZZLE_SEPARATION_PRESSURE": True,
    "PROPELLANT_GAUGING": True,
    "STORAGE_AGEING": True,
    "COMMS_CONJUNCTION": True,
    "ENGINE_FIT": True,
}


def on(name: str) -> bool:
    """Whether the switch MARSFLIGHT_<name> is on."""
    v = os.environ.get("MARSFLIGHT_" + name)
    if v is None or v.strip() == "":
        return DEFAULTS[name]
    return v.strip().lower() not in ("0", "false", "off", "no")


def all_switches() -> dict:
    return {k: on(k) for k in DEFAULTS}
