"""Separating elements from the stack.

Methods of Mission, moved verbatim from sim.py. Mission inherits them from SeparationMixin."""
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


class SeparationMixin:
    def separate(self, st):
        e = st.el
        if self.mode == "flight" and self.central in ("Earth", "Mars"):
            alt = self.altitude(self.x[:3], self.t)
            if e.kind == "fairing" and alt < ATM_TOP[self.central]:
                rho = self.last.get("rho", 0.0)
                v = self.last.get("airspeed", 0.0)
                fmh = 0.5 * rho * v ** 3
                if fmh > D.FREE_MOLECULAR_HEATING_MAX:
                    self.fail(f"fairing jettisoned at {fmh:.0f} W/m^2 free-molecular heating "
                              f"(limit {D.FREE_MOLECULAR_HEATING_MAX:.0f}): payload overheated")
            if e.kind == "heatshield" and self.last.get("mach", 0.0) > 0.9 and alt < ATM_TOP[self.central]:
                self.fail(f"heatshield separated at Mach {self.last.get('mach', 0):.2f}; it must be subsonic (below Mach 0.9)")
        shock = vibro.separation_shock([s.el for s in self.states], e.id)
        stack = self.rb.layout_now() if self.rb is not None else None
        self.states.remove(st)
        self.exposures.append((self.t, "separation", e.id, 1))
        self.check_shock(e.id, shock)
        removed = [e.id]
        # A parachute rides on the nearest backshell or capsule below it: separating that backshell
        # takes its parachutes along, and no others.
        if e.kind == "backshell":
            for s in list(self.states):
                if s.el.kind == "parachute" and getattr(s.el, "carrier", None) == e.id:
                    self.states.remove(s)
                    removed.append(s.el.id)
        if self.w and e.kind in ("stage", "fairing"):
            self._w_range_stage(e)   # a spent stage or fairing must not fall on land near the pad (range safety)
        self.event("separation", element=e.id, removed=removed)
        if not self.states:
            self.fail("everything was separated")
        if self.rb is not None:
            self.rb.separated(stack, removed)
