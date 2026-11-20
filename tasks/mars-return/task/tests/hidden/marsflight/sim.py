"""End-to-end mission simulator: Earth launch, Mars landing, surface stay, Mars ascent, Earth return.

The team's flight software (guidance) is called at the start of every arc. It sees navigation
estimates (with realistic errors), not the true state, and returns engine, attitude and event
commands plus how long until it wants to be called again. Each arc is propagated by Tudat with
piecewise-constant commands. After every arc the simulator checks what real hardware would suffer:
structural loads, heating and thermal protection, parachute envelopes, nozzle flow separation,
propellant, batteries, power, boiloff and freezing. A failure ends the flight.

The code lives in core/ (the loop) and models/<area>/ (one file per area). This module
keeps the old import path: `from marsflight.sim import Mission, FlightFailure, ...` still works."""
from .core.common import *  # noqa: F401,F403
from .core.common import (  # noqa: F401
    MILESTONES, MILESTONE_WEIGHTS, ATM_TOP, EARTH_R_EQ, PARTIAL_APPROACH_D0, SCENARIO_PHASES, STATE_AFTER, FlightFailure, interp, unit, rotate_towards, newtonian_cd, parachute_peak_load, great_circle_km, ElementState, clamp01,
)
from .core.mission import Mission  # noqa: F401
