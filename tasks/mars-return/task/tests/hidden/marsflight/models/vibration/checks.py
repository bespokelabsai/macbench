"""Liftoff acoustics, separation shock and pogo checks.

Methods of Mission, moved verbatim from sim.py. Mission inherits them from VibrationMixin."""
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


class VibrationMixin:
    def _vibro_margin(self, eid, kind):
        """How far above its qualification level an element really survives, in dB: the hidden as-built truth
        (program.vibro_truth). Without it (local runs of untested hardware) the qualification level is the limit."""
        return float(((self.truth.get("vibro") or {}).get(eid) or {}).get(kind, 0.0))

    def _mars_air(self):
        """Pressure (Pa) and acoustic impedance rho c (Pa s/m) of the Mars air at the launch site: the simulator's
        Mars profile (environment.MARS_P0 at the datum, mars_temperature), hydrostatic near the surface, times
        the world's density factor."""
        from ...environment import MARS_GAMMA, MARS_P0, MARS_R_GAS, mars_temperature
        alt = float((getattr(self, "surface_site", None) or (0.0, 0.0, 0.0))[2])
        T = mars_temperature(alt / 1e3)
        p = MARS_P0 * math.exp(-3.727 * alt / (MARS_R_GAS * T))
        rho = p / (MARS_R_GAS * T)
        disp = getattr(self, "disp", None)
        if disp is not None and hasattr(disp, "mars_density_multiplier"):
            rho *= disp.mars_density_multiplier(alt / 1e3)
        return p, rho * math.sqrt(MARS_GAMMA * MARS_R_GAS * T)

    def check_acoustics(self):
        """Liftoff noise against each element's acoustic qualification level (and so its random vibration).
        On Mars (VIBRO_MARS_NOISE) the same sound power in the thin air of the site."""
        mars = getattr(self, "central", "Earth") == "Mars"
        if mars:
            if not vibro.switch("VIBRO_MARS_NOISE"):
                return
            p, rc = self._mars_air()
            ac = vibro.liftoff_acoustics([s.el for s in self.states], p_amb=p, rho_c=rc)
        else:
            ac = vibro.liftoff_acoustics([s.el for s in self.states])
        if not ac["levels"]:
            return
        self.event("liftoff_acoustics", oaspl_dB={k: round(v, 1) for k, v in ac["levels"].items()},
                   sound_power_dB=round(ac["Lw_dB"], 1))
        where = "Mars liftoff" if mars else "liftoff"
        for s in self.states:
            e = s.el
            L = ac["levels"].get(e.id)
            if L is None:
                continue
            if L > e.qual_acoustic_dB + self._vibro_margin(e.id, "acoustic_margin_dB"):
                self.fail(f"{e.id} broke in the {where} noise: {L:.1f} dB overall sound pressure level against its "
                          f"qualification level of {e.qual_acoustic_dB:.1f} dB (the random vibration it drives "
                          "broke the hardware; an acoustic test at the predicted level shows it)")

    def check_aero_noise(self, ts, dvh, body):
        """The boundary layer noise of flight through the air (transonic and max-q, VIBRO_AERO) against each
        payload element's acoustic qualification level. Called after each arc of the stack in the atmosphere, with
        the arc's samples (Mach in column 2, dynamic pressure in column 3, as check_atmospheric reads them)."""
        if not body.startswith("VT") or not dvh or not vibro.switch("VIBRO_AERO"):
            return
        worst = None
        for t in ts:
            d = dvh[t]
            mach, q = float(d[2]), float(d[3])
            if not (q > 0 and math.isfinite(q) and math.isfinite(mach)):
                continue
            L, ext, cq = vibro.aero_noise_dB(q, mach)
            if worst is None or L > worst[0]:
                worst = (L, ext, cq, mach, q)
        if worst is None:
            return
        L, ext, cq, mach, q = worst
        pk = self.track.setdefault("vibro_aero_peak", {"dB": -1e9})
        if L > pk["dB"]:
            pk.update({"dB": round(L, 2), "mach": round(mach, 3), "q": round(q, 1)})
        for s in self.present():
            e = s.el
            if not e.vibro_payload:
                continue
            if L > e.qual_acoustic_dB + self._vibro_margin(e.id, "acoustic_margin_dB"):
                phase = "transonic shock and separated flow" if cq == vibro.TBL_CQ_SEPARATED else "boundary layer"
                self.fail(f"{e.id} broke in the ascent aerodynamic noise: {L:.1f} dB inside the fairing ({ext:.1f} dB "
                          f"outside, {phase} at Mach {mach:.2f}, q = {q / 1e3:.1f} kPa) against its qualification "
                          f"level of {e.qual_acoustic_dB:.1f} dB")

    def check_shock(self, gone, shock):
        """Pyroshock at a separation plane against the qualification of the elements that stay."""
        for s in self.states:
            e = s.el
            g = shock.get(e.id) if e.vibro_payload else None
            if g is None or e.id == gone:
                continue
            if g > e.qual_shock_g * 10 ** (self._vibro_margin(e.id, "shock_margin_dB") / 20):
                self.fail(f"{e.id} broke in the separation shock of {gone}: {g:.0f} g shock response against its "
                          f"qualification level of {e.qual_shock_g:.0f} g")

    def check_pogo(self, groups, before, samples=6):
        """Pogo over the arc just flown: the coupled stability of each burning pump-fed stage, at points from
        the propellant it started with to what it has left (vibro.pogo)."""
        burning, F = {}, 0.0
        for gr in groups:
            st, en = gr["state"], gr["engine"]
            F += en.thrust_vac * gr["n"] * gr["throttle"] * gr["factor"]
            if en.cycle != "pressure_fed" and st.el.id not in burning:
                burning[st.el.id] = (en, gr["n"], gr["throttle"])
        if not burning:
            return
        for k in range(samples):
            u = k / (samples - 1)
            stack = []
            for s in self.states:
                f0, o0 = before.get(id(s), (s.fuel, s.ox))
                fuel, ox = f0 + (s.fuel - f0) * u, o0 + (s.ox - o0) * u
                stack.append((s.el, fuel, ox, s.dry + fuel + ox + s.rcs))
            m = sum(x[3] for x in stack)
            for sid, r in vibro.pogo(stack, burning, F / m).items():
                lo = r.get("local")
                if lo and lo["unstable"]:
                    self.fail(f"pogo on {sid}: the center engine on its crossbeam ({lo['f_local']:.1f} Hz) coupled with "
                              f"its LOX line ({lo['f_line']:.1f} Hz); the local mode needs "
                              f"{100 * lo['zeta_needed']:.1f}% structural damping to stay stable and has "
                              f"{100 * vibro.STRUCTURAL_DAMPING:.0f}%, so the engine mount oscillation grew until it "
                              "broke the crossbeam (Apollo 13's S-II; a pogo accumulator on the LOX line detunes it)")
                if r["unstable"]:
                    ff = ", ".join(f"{k} line {v:.1f} Hz" for k, v in r["f_feed"].items())
                    self.fail(f"pogo on {sid}: the stack's first longitudinal mode at {r['f_struct']:.1f} Hz coupled "
                              f"with its feed system ({ff}); it needs {100 * r['zeta_needed']:.2f}% structural "
                              f"damping to stay stable and has {100 * vibro.STRUCTURAL_DAMPING:.0f}%, so the "
                              "oscillation grew until the stack broke (a pogo accumulator detunes the feed line)")
