"""Aerodynamic coefficients, the aerodynamic configuration and bank angle plans.

Methods of Mission, moved verbatim from sim.py. Mission inherits them from AeroMixin."""
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


class AeroMixin:
    def _coeffs_thrust(self, iv):
        """Force coefficients times area in body axes (Tudat's negative body-fixed frame: axial, side, normal),
        from Mach, angle of attack and sideslip, which Tudat takes from the airflow with the wind. A slender
        stack gets its axial force along the body axis and a normal force SLENDER_CN_ALPHA x sin(total angle
        of attack) across it, against the cross flow: a wind or an attitude off the airflow gives it lift and
        side force. Other shapes, and deployed parachutes, get drag against the airflow."""
        a = self.ctl["aero"]
        if a is None:
            return [0.0, 0.0, 0.0]
        mach = float(iv[0])
        al = float(iv[1]) if len(iv) > 1 else 0.0
        be = float(iv[2]) if len(iv) > 2 else 0.0
        t = float(iv[3]) if len(iv) > 3 else None   # for a parachute that is still opening
        if not (math.isfinite(al) and math.isfinite(be)):
            al = be = 0.0   # no airspeed, no direction
        # the vehicle's motion through the air, in body axes
        ux, uy, uz = math.cos(al) * math.cos(be), math.sin(be), math.sin(al) * math.cos(be)
        if a["kind"] == "slender":
            true_cd = (self.truth.get("aero") or {}).get("stack", {}).get("cd", 1.0)
            ca = interp(D.SLENDER_CA_MACH, mach) * a["S"] * self.disp.aero_cd_factor * true_cd
            cn = D.SLENDER_CN_ALPHA * a["S"]
            chute = self._chute_area(mach, t)
            return [(ca + chute) * ux, (cn + chute) * uy, (cn + chute) * uz]
        else:
            cd = D.BLUFF_CD * interp(D.CAPSULE_CD_MACH, mach) / 0.85
            F = self.ctl.get("F_now", 0.0)
            if F > 0:
                q = 1.0
                try:
                    q = max(1.0, float(self.bodies.get(self.ctl.get("vt", "VTEarth")).flight_conditions.dynamic_pressure))
                except Exception:  # noqa: BLE001
                    pass
                srp = self.w.get("srp") if getattr(self, "w", None) else None
                if srp is not None and mach >= 1.0:
                    # wind tunnel data for the lit engines' layout; below Mach 1 it keeps the rule below
                    n_eng = sum(g["n"] for g in self.ctl.get("thrust_groups") or [])
                    cd *= srp.cd_factor(F, q, a["S"], mach, max(n_eng, 1))
                else:
                    cd *= math.exp(-D.SRP_DRAG_DECAY * F / (q * a["S"]))
        drag_area = cd * a["S"] * self.disp.aero_cd_factor + self._chute_area(mach, t)
        return [drag_area * ux, drag_area * uy, drag_area * uz]

    def _coeffs_entry(self, iv):
        a = self.ctl["aero"]
        if a is None:
            return [0.0, 0.0, 0.0]
        mach = float(iv[0])
        tr = (self.truth.get("aero") or {}).get(a.get("shield")) or {}
        cd = a["cd_h"] * interp(D.CAPSULE_CD_MACH, mach) * self.disp.aero_cd_factor * tr.get("cd", 1.0)
        cl = cd * a["ld"] * interp(D.CAPSULE_LD_MACH, mach) * self.disp.aero_ld_factor * tr.get("ld", 1.0)
        return [cd * a["S"] + self._chute_area(mach, float(iv[1])), 0.0, cl * a["S"]]

    # ------------------------------------------------------------------ bank angle
    def _plan_bank(self, dt):
        """The bank angle over the next arc: the capsule rolls from its bank and roll rate now towards the
        commanded bank, the short way round, at no more than data.BANK_RATE_MAX_DEG_S and with no more than
        data.BANK_ACCEL_MAX_DEG_S2, slowing down in time to stop on it. The stopping rate is the one for steps of
        h, so the roll does not overshoot."""
        wmax, amax, h = math.radians(D.BANK_RATE_MAX_DEG_S), math.radians(D.BANK_ACCEL_MAX_DEG_S2), 0.02
        th, om = self.bank, self.bank_rate
        ts, ths, oms = [0.0], [th], [om]
        for _ in range(int(min(dt, 60.0) / h) + 1):
            e = (self.bank_cmd - th + math.pi) % (2 * math.pi) - math.pi
            done = abs(e) < 1e-4 and abs(om) < 1e-3
            if done:
                th, om = th + e, 0.0
            else:
                stop = math.sqrt((amax * h / 2) ** 2 + 2.0 * amax * abs(e)) - amax * h / 2
                w_want = math.copysign(min(wmax, stop), e)
                om += min(amax, max(-amax, (w_want - om) / h)) * h
                th += om * h
            ts.append(ts[-1] + h)
            ths.append(th)
            oms.append(om)
            if done:
                break
        self.bank_plan = (self.t, np.array(ts), np.array(ths), np.array(oms))

    def bank_at(self, t):
        if self.bank_plan is None:
            return self.bank
        t0, ts, ths, _ = self.bank_plan
        return float(np.interp(t - t0, ts, ths))

    def _end_bank(self, t1):
        if self.bank_plan is not None:
            t0, ts, ths, oms = self.bank_plan
            self.bank = float(np.interp(t1 - t0, ts, ths))
            self.bank_rate = float(np.interp(t1 - t0, ts, oms))
            self.bank_plan = None

    # ------------------------------------------------------------------ aero configuration
    def aero_config(self, retrograde):
        pres = self.present()
        if not pres:
            return None
        lead = pres[0].el
        fair = any(s.el.kind == "fairing" for s in pres)
        dmax = max([s.el.diameter for s in pres if s.el.kind != "parachute"] + [0.5])
        if fair:
            return {"kind": "slender", "S": math.pi * dmax ** 2 / 4, "D": dmax}
        if lead.kind in ("heatshield", "capsule"):
            ld = 0.0 if lead.id in self.ballast_dropped else lead.ld_trim
            # Trimmed for lift, a capsule flies at an angle of attack, where its drag is below the
            # zero-angle Newtonian value: x (1 - 0.5 L/D), calibrated to MSL's flown ballistic coefficient.
            return {"kind": "blunt", "S": math.pi * lead.diameter ** 2 / 4, "D": lead.diameter,
                    "cd_h": newtonian_cd(lead.half_angle, lead.nose_radius, lead.diameter / 2) * (1.0 - 0.5 * ld), "ld": ld,
                    "rn": lead.nose_radius, "shield": lead.id}
        if lead.kind == "stage" and not retrograde and any(s.el.kind == "stage" for s in pres):
            return {"kind": "slender", "S": math.pi * dmax ** 2 / 4, "D": dmax}
        return {"kind": "bluff", "S": math.pi * dmax ** 2 / 4, "D": dmax}
