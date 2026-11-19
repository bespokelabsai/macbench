"""Loads in the atmosphere and in burns: g limits and stage structure.

Methods of Mission, moved verbatim from sim.py. Mission inherits them from LoadsMixin."""
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
from . import materials
from ...environment import EARTH_SOI, MARS_SOI, MARS_REF_RADIUS, Dispersions, World
from ...core.common import (  # noqa: F401
    MILESTONES, MILESTONE_WEIGHTS, ATM_TOP, EARTH_R_EQ, PARTIAL_APPROACH_D0, SCENARIO_PHASES, STATE_AFTER, FlightFailure, interp, unit, rotate_towards, newtonian_cd, parachute_peak_load, great_circle_km, ElementState,
)


class LoadsMixin:
    # ------------------------------------------------------------------ checks
    def check_atmospheric(self, ts, dvh, body, aero, groups, thrust_dir):
        central = self.central
        pres = self.present()
        lead = pres[0].el if pres else None
        shield = aero.get("shield") if aero else None
        prev_t, prev_q = None, 0.0   # heat load: trapezoid over this arc's samples (ts[0] closed the last arc)
        q_last = None                # (t, dynamic pressure) of the sample before, for a parachute's full inflation
        if materials.enabled() and central == "Mars" and self.mars_lifted and not getattr(self, "_cracks_checked", False):
            # the first arc in the air after Mars liftoff; the tanks waited on Mars until then
            self._cracks_checked = True
            self._check_cracks()
        psi = self._plume_surface() if central == "Mars" and body.startswith("VT") and groups else None
        burnt = self._burn_through(groups, ts[-1]) if groups and ts else None
        for t in ts:
            if burnt and t > burnt[0]:   # nothing after the burn-through counts
                self.fail(burnt[1])
            d = dvh[t]
            alt, mach, q, rho, V, a_aero = d[0], d[2], d[3], d[4], d[5], d[6]
            aoa = 0.0
            if body.startswith("VT"):
                # the total angle between the body axis and the airflow, from angle of attack and sideslip: the
                # normal force and the bending follow it
                c = math.cos(d[7]) * math.cos(d[8])
                aoa = math.acos(max(-1.0, min(1.0, c))) if math.isfinite(c) else 0.0
                if self.ctl.get("entry"):
                    aoa = math.pi - aoa   # a 6-DOF blunt entry: the angle to the heatshield's axis, down the body x axis
            a_thr = d[-1] if body.startswith("VT") else 0.0
            self.last = {"mach": mach, "q": q, "rho": rho, "airspeed": V, "alt": alt}
            # Thrust acts along the body axis and drag against the airflow, at the angle of attack to the axis.
            # So the stack feels thrust minus the axial drag (thrust plus drag when it flies tail first).
            a_ax = a_aero * math.cos(aoa)
            g_load = math.sqrt(max(0.0, a_thr * a_thr + a_aero * a_aero - 2.0 * a_thr * a_ax)) / D.G0
            self.peak["q"] = max(self.peak["q"], q)
            self.peak["g"] = max(self.peak["g"], g_load)
            qt = 0.0
            if self.telemetry is not None and t - self.track.get("tele_atm_t", -1e30) >= 1.0:
                self.track["tele_atm_t"] = t
                self._tele({"kind": "atm", "met": round(t - self.t0, 3), "body": central, "alt": alt,
                            "mach": mach, "q": q, "rho": rho, "airspeed": V, "g": g_load,
                            "bank_deg": (math.degrees(self.bank_at(t)) + 180.0) % 360.0 - 180.0,
                            "p_amb": self.nozzle_ambient_pressure(alt, rho, t),
                            "accel_thrust": a_thr, "accel_aero": a_aero,
                            "aoa_deg": math.degrees(aoa), "aero": aero.get("kind") if aero else None,
                            "heat_flux": self._stag_heating(rho, V, aero, lead) if V > 1500 and rho > 0 else 0.0})
            # payload g limits
            self.check_g_limits(g_load, f"at {alt / 1e3:.1f} km")
            for eid, op in self.chute_open.items():   # parachutes that reach full inflation by this sample
                if not op.get("open") and t >= op["t_fi"]:
                    st = self.find(eid)
                    if st is None:
                        op["open"] = True
                        continue
                    q_fi = q   # the dynamic pressure at the moment of full inflation, between the two samples
                    if q_last is not None and q_last[0] < op["t_fi"] < t:
                        q_fi = q_last[1] + (q - q_last[1]) * (op["t_fi"] - q_last[0]) / (t - q_last[0])
                    self._chute_opened(st.el, op, q_fi, mach, op["t_fi"])
            q_last = (t, q)
            # heating at the stagnation point of whatever leads
            if V > 1500 and rho > 0:
                qt = self._stag_heating(rho, V, aero, lead)
                self.peak["heat_flux"] = max(self.peak["heat_flux"], qt)
                if shield is None:
                    if qt > 3e5:
                        self.fail(f"unshielded hardware ({lead.id if lead else '?'}) burned up: stagnation heating "
                                  f"{qt / 1e6:.2f} MW/m^2 at {alt / 1e3:.1f} km with no heatshield leading")
                else:
                    se = self.find(shield).el
                    if qt > 3e5:
                        self._check_enclosed(se)
                    mat = D.TPS[se.tps]
                    tps_true = (self.truth.get("tps") or {}).get(se.tps) or {}
                    q_rated = mat["q_max"] * tps_true.get("q_max", 1.0)
                    if qt > q_rated:
                        self.fail(f"heat flux {qt / 1e6:.2f} MW/m^2 exceeds what {se.tps} on {se.id} survives "
                                  f"({q_rated / 1e6:.1f} MW/m^2)")
                    if prev_t is not None:
                        self.heat_load[shield] = self.heat_load.get(shield, 0.0) + 0.5 * (prev_q + qt) * (t - prev_t)
                    self._check_heat_load(shield, se)
                    if materials.enabled():
                        self._check_bond_lines()
                    self._heat_backshell(se, t, qt)
            # nozzle flow separation for engines firing in the atmosphere
            if body.startswith("VT") and groups:
                self.check_nozzle_separation(groups, alt, rho, t)   # models/propulsion/engines.py
            if psi is not None and prev_t is not None:
                self._fire_plumes(psi, groups, alt, t, t - prev_t)
            # structural loads on stage tanks in the launch stack. The drag acts on the leading element.
            if body.startswith("VT") and not self.ctl.get("entry"):   # a blunt entry has no stage tanks in the airflow
                self.check_structure(q, aoa, (a_thr - a_ax) / D.G0, aero, max(a_ax, 0.0) * self.total_mass())
            self._track_entry(alt, V, t)   # credit only for a sample the vehicle survived
            prev_t, prev_q = t, qt

    def check_g_limits(self, g_load, where):
        """The capsule's g limit, and 150 g for anything. Checked in the atmosphere and in every powered arc."""
        for s in self.present():
            if s.el.kind == "capsule" and g_load > s.el.g_limit:
                self.fail(f"{g_load:.1f} g exceeds the capsule's {s.el.g_limit:.0f} g limit {where}")
        if g_load > 150:
            self.fail(f"{g_load:.0f} g: the vehicle breaks up")

    def check_structure(self, q, aoa, n_axial, aero, drag_lead=0.0):
        """Buckling of every stage tank that carries something. The load at a joint is the mass above it
        times the axial load factor the stack feels (n_axial, thrust minus axial drag, in g), plus the drag
        on the leading element above it (drag_lead, N), plus the bending from the normal force."""
        pres = self.present()
        S = aero["S"] if aero else 1.0
        inc = min(abs(aoa), math.pi - abs(aoa))   # angle between the body axis and the airflow, either end first
        # A fairing carries the aerodynamic bending, and its own drag, around the payload it covers.
        shielded = set()
        for s in pres:
            if s.el.kind == "fairing":
                shielded.update(s.el.spec.get("_covers", []))
        for i, s in enumerate(pres):
            e = s.el
            if not e.is_stage or not e.tanks:
                continue
            above = pres[i + 1:]
            if not above:
                continue
            m_above = sum(x.mass for x in above)
            L_above = sum(x.el.length for x in above)
            R = e.diameter / 2
            N = m_above * n_axial * D.G0 + (0.0 if e.id in shielded else drag_lead)
            M = 0.0 if e.id in shielded else 0.5 * q * S * D.SLENDER_CN_ALPHA * inc * (L_above / 2)
            N_eq = N + 2 * M / R
            mat = D.MATERIALS[e.material]
            # The design must keep its factor of safety on the as-built strength: 1.25 for a tank design
            # qualified by a structural test, 1.6 for one that was never tested (NASA-STD-5001 no-test factor).
            tk_true = (self.truth.get("tanks") or {}).get(e.id) or {}
            sf = D.BUCKLING_SF if tk_true.get("qualified") else D.BUCKLING_SF_UNTESTED
            kd = tk_true.get("knockdown", 1.0)
            V_sh = 0.0 if e.id in shielded else 0.5 * q * S * D.SLENDER_CN_ALPHA * inc
            allow = float("inf")
            for tk in ([] if e.balloon else e.tanks.values()):
                if e.stiffeners:
                    # SP-8007 section 4.3.1, worked out when the vehicle was built. SP-8007 gives no pressure
                    # stabilization for stiffened cylinders, so the pressure adds only its end load.
                    allow = min(allow, 2 * math.pi * R * tk.n_cr + e.tank_pressure * math.pi * R * R)
                    continue
                if materials.enabled():
                    # An unstiffened wall: axial load, bending and transverse shear each against its own
                    # SP-8007 buckling load, added as in its eq. 32, with the L-67 pressure credit in shear.
                    ra, rb, rs = materials.shell_ratios(R, tk.wall_t, max(tk.cyl_length, 0.5 * R), mat,
                                                        e.tank_pressure, N, M, V_sh, sf, r_tank=tk.radius)
                    r_all = materials.interaction(ra, rb, rs)
                    if r_all > kd:
                        self.fail(f"stage {e.id} buckled: axial, bending and shear load {r_all:.2f} of what the "
                                  f"tank wall holds (x{sf} factor of safety) at q={q / 1e3:.1f} kPa, angle of attack "
                                  f"{math.degrees(inc):.1f} deg, axial load factor {n_axial:.2f} g")
                    continue
                t = tk.wall_t
                phi = math.sqrt(R / t) / 16
                gamma = 1 - 0.901 * (1 - math.exp(-phi))
                # Internal pressure also stiffens the shell against buckling: NASA SP-8007 eq. 34 with Delta gamma
                # from its Figure 6, as a closed form fitted to the digitized curve (within 3.5% for pbar 0.016 to 10).
                pbar = e.tank_pressure / mat["E"] * (R / t) ** 2
                dgamma = 1.194 * pbar ** 0.945 / (1 + 4.363 * pbar ** 0.945)
                s_cr = min((gamma * 0.605 + dgamma) * mat["E"] * t / R, mat["sy"])
                allow = min(allow, 2 * math.pi * R * t * s_cr + e.tank_pressure * math.pi * R * R)
            if e.balloon:
                # A balloon tank's skin takes no compression: its pressure must carry the whole load.
                allow = e.tank_pressure * math.pi * R * R
            allow *= kd
            if N_eq * sf > allow:
                self.fail(f"stage {e.id} buckled: equivalent axial load {N_eq / 1e6:.2f} MN (x{sf} factor of safety) exceeds "
                          f"{allow / 1e6:.2f} MN at q={q / 1e3:.1f} kPa, angle of attack {math.degrees(inc):.1f} deg, "
                          f"axial load factor {n_axial:.2f} g")

    # ------------------------------------------------------------------ material behavior
    def _rng(self, tag):
        """A generator of its own for one hidden draw of this world, so the other draws do not move. None in
        the nominal world."""
        if self.seed is None:
            return None
        return np.random.default_rng([abs(int(self.seed)) % 2 ** 32, zlib.crc32(tag.encode())])

    def _check_bond_lines(self):
        """An ablator bonded to aluminum, soaked at the cold limit before entry, must take the structure's
        contraction with the bonded joint factor of safety (materials.bond_strain). Checked once per element, at
        the first heated sample of its entry."""
        done = self.__dict__.setdefault("_bond_checked", [])
        for s in self.present():
            tps = getattr(s.el, "tps", None)
            need = materials.bond_strain(tps) if s.el.kind in ("heatshield", "capsule") and tps else None
            if need is None or s.el.id in done:
                continue
            done.append(s.el.id)
            lo, hi = materials.TPS_STRAIN_TO_FAILURE[tps]
            g = self._rng("bond:" + s.el.id)
            have = (lo + hi) / 2 if g is None else float(g.uniform(lo, hi))
            if need > have:
                self.fail(f"{s.el.kind} {s.el.id} heat shield cracked in the cold before entry: the {tps} layer "
                          f"could not take the contraction of the structure under it")

    def _check_cracks(self):
        """A heated, pressurized tank that waited on Mars saw one pressure cycle each sol, as its ullage warmed by
        day. A flaw the acceptance proof let through must not grow through the wall in four times as many cycles
        (materials.cycles_to_leak)."""
        if self.landed_t is None:
            return
        sols = (self.t - self.landed_t) / 88775.244
        for s in self.present():
            e = s.el
            if not (e.is_stage and e.tanks) or e.material not in materials.NIST_CONTRACTION or sols < 1:
                continue
            mat = D.MATERIALS[e.material]
            for role, tk in e.tanks.items():
                P = D.PROPELLANTS[e.fuel if role == "fuel" else e.ox]
                if P["cryo"] or (s.fuel if role == "fuel" else s.ox) <= 0:
                    continue
                sigma = e.tank_pressure * tk.radius / tk.wall_t
                n, _, how = materials.cycles_to_leak(tk.wall_t, sigma, materials.diurnal_ratio(P["freeze"] + 10.0), mat["sy"])
                if n < materials.SERVICE_LIFE_FACTOR * sols:
                    self.fail(f"stage {e.id} {role} tank cracked while it waited on Mars: a flaw grew through "
                              f"the wall in the daily pressure cycles" + (" and the tank burst" if how == "burst" else ""))

    def _check_clearance(self, st):
        """The stage leaving keeps the interstage, and the nozzles of the stage above must come out of it without
        touching it while that stage turns at its tip-off rate (materials.separation_clearance). Called by
        models/separation/separate.py before the element is removed."""
        if not materials.enabled():
            return
        pres = self.present()
        if self.mode != "flight" or len(pres) < 2 or pres[0] is not st or "interstage_above" not in st.el.mass_breakdown:
            return
        up = pres[1].el
        if not (up.is_stage and up.engines):
            return
        n = sum(k for _, k in up.engines)
        De = max(math.sqrt(4 * en.Ae / math.pi) for en, _ in up.engines)
        Dt = max(math.sqrt(4 * en.At / math.pi) for en, _ in up.engines)
        nozzle = min(0.8 * (De - Dt) / 2 / math.tan(math.radians(15)) + 2.0 * Dt, 0.5 * up.diameter)
        arm = materials.nozzle_arm([(x.mass, x.el.length) for x in pres[1:] if x.el.kind != "fairing"])
        g = self._rng("tipoff:" + up.id)
        sig = materials.TIPOFF_3SIGMA_DEG_S / 3
        rate = sig * math.sqrt(math.pi / 2) if g is None else float(np.hypot(*g.normal(0.0, sig, 2)))
        c, gap, _ = materials.separation_clearance(up.diameter / 2, n, De, nozzle, arm, rate)
        if c < 0:
            self.fail(f"stage {up.id} engines struck the interstage at separation from {st.el.id}"
                      + (": the engine cluster is wider than the interstage" if gap < 0 else ""))

    # ------------------------------------------------------------------ the backshell and plume models
    def _flight_ctx(self):
        from ...core.model import FlightContext
        return FlightContext(seed=self.seed, design=self.design, vehicle=self.vehicle, world=self.disp,
                             truth=self.truth, data_dir="", six_dof=self.rb is not None)

    def _heat_backshell(self, se, t, qt):
        """the backshell of the leading heat shield takes a fraction of its stagnation heating
        (models/plumes/backshell.py). MARSFLIGHT_BACKSHELL_HEATING=1 turns it on; it stays off until the
        reference's backshell is thickened to about 1.5 kg/m^2 (its 0.5 kg/m^2 burns through 71 s after entry)."""
        if os.environ.get("MARSFLIGHT_BACKSHELL_HEATING", "1") != "1":
            return
        bs_id = se.spec.get("_backshell")
        bs = self.find(bs_id) if bs_id else None
        if bs is None:
            return
        m = getattr(self, "_backshell_model", None)
        if m is None:
            from ..plumes.backshell import BackshellHeating
            m = BackshellHeating()
            m.configure(self._flight_ctx())
            self._backshell_model = m
        why = m.heat(bs.el.id, bs.el.tps, bs.el.tps_areal, self.central, t, qt)
        if why:
            self.fail(why)

    def _plume_surface(self):
        """the plume surface interaction model of this flight (models/plumes/surface.py), or None when
        MARSFLIGHT_PLUME_SURFACE is not 1. Off by default until models/landing/surface.py calls touchdown_plumes
        (the tip-over check); firing alone only digs."""
        if os.environ.get("MARSFLIGHT_PLUME_SURFACE", "1") != "1":
            return None
        m = getattr(self, "_psi_model", None)
        if m is None:
            from ..plumes.surface import PlumeSurfaceInteraction
            m = PlumeSurfaceInteraction()
            m.configure(self._flight_ctx())
            self._psi_model = m
        return m

    def _fire_plumes(self, psi, groups, alt, t, dt):
        h = alt - self.surface_alt()
        if not (0.0 <= h < psi.PSI_HEIGHT_M) or dt <= 0:
            return
        by_el = {}
        for gr in groups:
            if t < gr["t_on"] or gr["throttle"] <= 0:
                continue
            en, e = gr["engine"], gr["state"].el
            r_exit = math.sqrt(en.Ae / math.pi)
            by_el.setdefault(e.id, []).append((en.thrust(gr["throttle"], 0.0), r_exit, gr["n"], e.diameter,
                                               bool((en.spec or {}).get("pulsed", False))))
        for eid, gs in by_el.items():
            psi.fire(eid, gs, h, dt)

    def touchdown_plumes(self, lead):
        """At a Mars touchdown: does a plume crater under a footpad tip the lander over? Returns a failure
        message or None. models/landing/surface.py calls it (see the PR's needs)."""
        psi = getattr(self, "_psi_model", None)
        if psi is None or not psi.craters:
            return None
        return psi.touchdown(lead.diameter / 2, D.LANDING_SLOPE_MAX_DEG)
