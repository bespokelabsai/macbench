"""Mars entry, descent and landing.

Entry: numerical predictor-corrector. Every call it predicts the rest of the descent with the onboard
model (entry at the current bank, ballast drop, DGB deployment, heat shield separation, chute descent
to the powered-descent start) and solves for the bank magnitude that puts that point over Jezero;
bank reversals keep the crossrange inside a corridor that shrinks with speed. The atmosphere is the
simulator's nominal model scaled by a density factor measured from the drag.

Descent: DGB at Mach <= 2.05 and q <= 760 Pa, heat shield off below Mach 0.75, backshell off and
the two lander engines lit at the powered-descent height, then a gravity-turn braking phase to a gate
above the target and a one-engine terminal descent to touchdown at about 1.5 m/s. Once the
terrain-relative navigation fix (event trn_fix) has corrected the position, the descent aims at a safe
cell of the onboard hazard map within the divert limit of its natural stopping point (gnc/hazard.py).
"""
from __future__ import annotations

import math

import numpy as np

from . import edl_model as E
from . import marsatm as A
from . import mission as M
from .log import log
from .util import MARS_R, OMEGA, latlon, pole, rot_inertial_to_fixed, unit
from .vehicle_model import element, present, prop_left, stack_mass

SITE_ALT = -2550.0
# km: range control aims this far short of the target. The range trigger takes out a short miss by holding the
# parachute; nothing takes out a long one, since the parachute cannot open above its Mach limit.
M_DR_AIM_KM = -1.0
LATE_SIGN_HYST_KM = 0.3
# m: deploy_floor keeps the predicted parachute opening at least this high above the ground. 6 km (tried) let range
# control fly the lift down so late that the parachute opened 4.7 km up and the heat shield came off right above
# the powered-descent height (seeds 1012 and 1023).
DEPLOY_FLOOR_AGL_M = 8000.0
TD_FLOOR_H = 15.0         # m: the terminal descent reaches its touchdown speed this high above the ground
HS_SEP_COAST_S = 3.0     # s from heat shield separation to the powered-descent start (and backshell separation)
TRIM_AOA_DEG = 9.2      # deg: trim angle of attack of the aeroshell at its ld_trim of 0.14


class EDL:
    def __init__(self, fl):
        self.fl = fl
        self.model = fl.model
        self.done = False
        self.phase = "entry"
        des = {e["id"]: e for e in fl.info["design"]["elements"]}
        rep = self.model.elements
        hs = des["HS"]
        self.hs_id = "HS"
        self.S = math.pi * hs["diameter_m"] ** 2 / 4
        self.rn = hs.get("nose_radius_m", 0.25 * hs["diameter_m"])
        self.cd_h = E.newtonian_cd(hs.get("half_angle_deg", 70), self.rn, hs["diameter_m"] / 2)
        self.ld = hs.get("ld_trim", 0.0)
        self.ld_design = self.ld
        self.ld_scale = 1.0          # real L/D over the model, estimated in flight
        self.prev_nav = None
        self.ballast = rep["HS"]["breakdown_kg"].get("trim_ballast_jettisoned_before_parachute", 0.0)
        self.hs_dry = rep["HS"]["dry_mass_kg"] - self.ballast
        ch = next(e for e in fl.info["design"]["elements"] if e["kind"] == "parachute" and e["id"] == M.MARS_CHUTE)
        self.chute_id = ch["id"]
        self.chute_A = math.pi * (ch["diameter_m"] / 2) ** 2
        self.chute_D = ch["diameter_m"]
        self.t_deploy, self.V_deploy = None, None
        self.chute_q = ch["design_q_Pa"]
        self.bs_dry = rep["BS"]["dry_mass_kg"]
        ids = [e["id"] for e in fl.info["vehicle"]["elements"]]
        i_hs = ids.index("HS")
        def wet(e):
            return e["dry_mass_kg"] + sum((e.get("loaded_kg") or {}).values()) + e.get("rcs_prop_kg", 0.0)
        self.entry_mass = sum(wet(e) for e in fl.info["vehicle"]["elements"][i_hs:] if e["kind"] != "fairing")
        self.k_rho = None
        self.q_sensed = None         # dynamic pressure from the accelerometers and the drag model (Pa)
        self.sign = 1.0
        self.bank = math.radians(60.0)
        self.t_last = None
        self.n_rev = 0
        self.bank_cmd = None          # 6-DOF: the bank last commanded (deg), for slow reversals
        self.t_last_cmd = None
        self.six = fl.info.get("dynamics") == "6dof"
        hs_rep = next((e for e in fl.info["vehicle"]["elements"] if e["id"] == "HS"), {})
        # The angle of attack the RCS holds in the 6-DOF entry. The build report has no 6-DOF trim row, so this
        # read 0 and the RCS held the heat shield straight into the flow: no lift at all, and every world landed
        # 150 to 220 km short. The trim comes from our own modified Newtonian model of the 70 deg
        # sphere-cone, as the ballast is placed for it: L/D 0.14 at 9.2 deg, 0.153 at 10 deg (Mach 30).
        self.trim_deg = (hs_rep.get("entry_trim_6dof") or {}).get("alpha_deg") or TRIM_AOA_DEG
        self.ballast_dropped = False
        self.site = (M.JEZERO[0], M.JEZERO[1])
        self.pd = None
        self.trn_t = None            # time of the terrain-relative navigation fix
        self.hazard = None           # onboard hazard map, loaded at the first divert
        self.trn_cell = None         # landing cell chosen on the parachute after the TRN fix
        self.wind_fixed = None       # measured wind at full strength, Mars-fixed (m/s), see update_wind
        self.n_wind = 0

    # ------------------------------------------------------------------ helpers
    def frames(self, t):
        pz = pole("Mars", t)
        w = OMEGA["Mars"] * pz
        return pz, w

    def site_inertial(self, t):
        la, lo = math.radians(self.site[0]), math.radians(self.site[1])
        f = (MARS_R + SITE_ALT) * np.array([math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)])
        return rot_inertial_to_fixed("Mars", t).T @ f

    def mass(self, obs):
        m = stack_mass(obs, self.model)
        return m

    def update_density(self, obs, m):
        sa = obs.get("sensed_accel")
        q = obs.get("q") or 0.0
        mach = obs.get("mach") or 0.0
        if not sa or q < 30.0:
            return
        a = math.sqrt(sum(c * c for c in sa))
        r = np.array(obs["r"])
        h = np.linalg.norm(r) - MARS_R
        V = obs.get("airspeed") or 0.0
        if V < 100:
            return
        ld = 0.0 if self.ballast_dropped else self.ld * self.ld_scale * E.interp(E.CAPSULE_LD_MACH, mach)
        ld_d = 0.0 if self.ballast_dropped else self.ld_design
        chute = self.chute_A * E.interp(E.DGB_CD, mach) if self.phase != "entry" else 0.0
        cdA = self.cd_h * (1.0 - 0.5 * ld_d) * E.interp(E.CAPSULE_CD_MACH, mach) * self.S
        # total aero accel = q (cdA sqrt(1+ld^2) + chute) / m for the entry body
        rho_meas = 2 * m * a / (V * V * (cdA * math.sqrt(1 + ld * ld) + chute))
        self.q_sensed = 0.5 * rho_meas * V * V
        k = rho_meas / max(A.density(h), 1e-12)
        self.k_rho = k if self.k_rho is None else 0.6 * self.k_rho + 0.4 * k

    def predict(self, obs, bank, sign, m, k_rho, t_rel_stop=None, lateral=True):
        """Predict to the powered-descent start. Returns (t_end_abs, x_end, info). lateral=False flies the
        reference path: lift kept vertical with L/D scaled by cos(bank), no sideways lift."""
        t0 = obs["t"]
        pz, w = self.frames(t0)
        x0 = np.array(obs["r"] + obs["v"])
        ld = 0.0 if self.ballast_dropped else self.ld
        if not lateral:
            ld = ld * math.cos(bank)
            bank = 0.0
        state = {"ph": "entry" if self.phase == "entry" else self.phase, "m": m, "ld": ld}
        if self.phase == "chute" and self.t_deploy is not None:
            state["t_dep"], state["V_dep"] = self.t_deploy - t0, self.V_deploy
        ld_design = 0.0 if self.ballast_dropped else self.ld_design
        veh = E.Vehicle(m, self.S, self.cd_h, ld * self.ld_scale, chute_area=self.chute_area_at(state, 0.0),
                        ld_drag=ld_design)
        if self.wind_fixed is not None:
            veh.wind = tuple(rot_inertial_to_fixed("Mars", t0).T @ self.wind_fixed)
            veh.site_alt = SITE_ALT
        h_stop = SITE_ALT + M.PD_PREDICT_AGL
        events = {}

        def bank_fn(t, x):
            return sign * bank if state["ph"] == "entry" and veh.ld else 0.0

        def stop(t, x, h, V, mach, q):
            if state["ph"] == "entry":
                if not state.get("ballast") and mach < M.BALLAST_MACH and veh.ld:
                    state["ballast"] = True
                    veh.ld = 0.0
                    veh.m -= self.ballast
                if mach <= self.chute_mach() and q <= M.CHUTE_Q:
                    state["ph"] = "chute"
                    state["t_dep"], state["V_dep"] = t, V
                    events["deploy"] = (t, h, V, mach, q)
            if state["ph"] == "chute":
                veh.chute_area = self.chute_area_at(state, t)
            if state["ph"] == "chute" and not state.get("hs_off") and mach < M.HS_SEP_MACH:
                state["hs_off"] = True
                veh.m -= self.hs_dry
                veh.cd_h = 0.30 / 0.62
                events["hs"] = (t, h, V)
            return h < h_stop

        tt, xe, info = E.fly(x0, veh, bank, k_rho, w, pz, stop, dt=M.NPC_DT, bank_fn=bank_fn)
        info["events"] = events
        return t0 + tt, xe, info

    def chute_area_at(self, state, t):
        """Drag area (as pi D^2/4) of the parachute t s into a prediction: none before deploy or line stretch,
        then growing as the fourth power of the time over the filling, then the whole canopy (the manual's opening:
        lines stretch at 0.053 s per meter of diameter, the canopy fills over 12.5 diameters). Assuming the whole
        canopy at deploy put the 60 m parachute's predicted landing about 2 km short."""
        if state["ph"] != "chute":
            return 0.0
        if "t_dep" not in state:
            return self.chute_A
        e = t - state["t_dep"]
        t_ls = 0.053 * self.chute_D
        t_fill = 12.5 * self.chute_D / max(state["V_dep"] or 1.0, 1.0)
        if e <= t_ls:
            return 0.0
        return self.chute_A * min(1.0, ((e - t_ls) / t_fill) ** 4)

    def chute_mach(self):
        """The Mach number the parachute is deployed at or below. The predictor uses it too: otherwise in 6-DOF it
        plans for a deploy 0.15 in Mach earlier than the flight software makes, and Jezero within 3 km falls
        from 21 to 9 of 48 worlds (ref-a, seeds 1000 to 1047)."""
        if self.fl.info.get("dynamics") == "6dof":
            return M.CHUTE_MACH - M.CHUTE_MACH_MARGIN_6DOF
        return M.CHUTE_MACH

    def errors(self, t_end, x_end, x_now):
        """Downrange and crossrange of the predicted end point from the target (km), in the frame of the
        current horizontal velocity."""
        tgt = self.site_inertial(t_end)
        r = x_now[:3]
        pz, w = self.frames(t_end)
        v = x_now[3:] - np.cross(w, r)
        up = unit(r)
        h_dir = unit(v - np.dot(v, up) * up)
        side = np.cross(up, h_dir)
        d = x_end[:3] - tgt
        d = d - np.dot(d, unit(tgt)) * unit(tgt)
        return float(np.dot(d, h_dir)) / 1e3, float(np.dot(d, side)) / 1e3

    # ------------------------------------------------------------------ step
    def step(self, obs):
        if "trn_fix" in (obs.get("events") or []):
            self.trn_t = obs["t"]
            log("trn_fix", t=obs["t"], hagl=obs.get("height_above_surface"), phase=self.phase)
            if self.pd is not None:
                self.pd.pop("aim", None)    # chosen on the old position: choose it again
            elif self.phase == "chute":
                self.choose_on_chute(obs)
        if self.phase == "entry":
            return self.entry(obs)
        if self.phase == "chute":
            return self.chute(obs)
        if self.phase == "powered":
            return self.powered(obs)
        if self.phase == "landed":
            self.done = True
            return {"next": 60.0}
        raise RuntimeError(self.phase)

    def update_wind(self, obs):
        """The wind from the air data: the navigated velocity relative to the ground less the airspeed vector,
        which the air data take from the same navigated state, so the navigation error cancels. Below 40 km above
        the ground it blows from one direction (the manual), so each reading, scaled by the profile, measures the
        whole profile; the predictor then flies the parachute's drift with it. Before, a 30 m/s wind carried the
        lander 3 to 4 km from the predicted landing (seed 1006)."""
        av = obs.get("airspeed_vector")
        if not av:
            return
        r = np.array(obs["r"])
        h = float(np.linalg.norm(r)) - MARS_R
        f = E.wind_factor(h, SITE_ALT)
        if f <= 0.0 or h - SITE_ALT > E.WIND_TOP_AGL - 2e3:
            return
        pz, w = self.frames(obs["t"])
        wi = (np.array(obs["v"]) - np.cross(w, r) - np.array(av)) / f
        wf = rot_inertial_to_fixed("Mars", obs["t"]) @ wi
        wf -= np.dot(wf, unit(rot_inertial_to_fixed("Mars", obs["t"]) @ r)) * unit(rot_inertial_to_fixed("Mars", obs["t"]) @ r)
        self.n_wind += 1
        a = 1.0 / min(self.n_wind, 5)
        self.wind_fixed = wf if self.wind_fixed is None else (1 - a) * self.wind_fixed + a * wf
        if self.n_wind in (1, 5):
            log("wind", t=obs["t"], h=h, n=self.n_wind, speed=float(np.linalg.norm(self.wind_fixed)))

    def update_ld(self, obs):
        """Real lift-to-drag ratio from the change of the navigated velocity over the last arc: the
        aerodynamic acceleration is the velocity change minus gravity; its part along the air-relative
        velocity is drag, the rest is lift. Used only high up, above the winds, at useful dynamic pressure."""
        from .util import grav_j2
        t = obs["t"]
        r = np.array(obs["r"])
        v = np.array(obs["v"])
        prev, self.prev_nav = self.prev_nav, (t, r, v)
        q = obs.get("q") or 0.0
        if prev is None or self.ballast_dropped or not self.ld:
            return
        tp, rp, vp = prev
        dt = t - tp
        hagl = obs.get("height_above_surface", 0.0)
        if dt < 1.0 or dt > 12.0 or q < 300.0 or hagl < 42e3:
            return
        pz, w = self.frames(t)
        rm = 0.5 * (r + rp)
        a = (v - vp) / dt - grav_j2("Mars", rm, pz)
        va = unit(0.5 * (v + vp) - np.cross(w, rm))
        drag = -float(np.dot(a, va))
        lift = float(np.linalg.norm(a + drag * va))
        if drag <= 0.5:
            return
        f = (lift / drag) / (self.ld * E.interp(E.CAPSULE_LD_MACH, obs.get("mach") or 30.0))
        f = min(1.4, max(0.6, f))
        self.ld_scale = 0.5 * self.ld_scale + 0.5 * f

    def entry(self, obs):
        m = self.mass(obs)
        self.update_ld(obs)
        self.update_density(obs, m)
        self.update_wind(obs)
        k = self.k_rho if self.k_rho is not None else A.season_factor(obs["t"])
        mach = obs.get("mach") or 30.0
        q = obs.get("q") or 0.0
        acts = []
        x_now = np.array(obs["r"] + obs["v"])
        # chute deployment
        mach_max = self.chute_mach()
        if self.fl.info.get("dynamics") == "6dof":
            # In 6-DOF, mach and q come from an onboard atmosphere with nominal winds and no dust storm. The
            # dynamic pressure the vehicle feels comes from its accelerometers instead, and the Mach number
            # gets a margin for the wind the onboard model does not know.
            if self.q_sensed is not None:
                q = self.q_sensed
        if mach <= mach_max and q <= M.CHUTE_Q and obs.get("alt", 1e9) < 60e3 and not self.range_wait(obs, m, k, x_now, mach):
            if not self.ballast_dropped and self.ballast > 0:
                acts.append(f"jettison_ballast:{self.hs_id}")
                self.ballast_dropped = True
            acts.append(f"deploy:{self.chute_id}")
            self.phase = "chute"
            self.t_deploy, self.V_deploy = obs["t"], obs.get("airspeed") or float(np.linalg.norm(obs["v"]))
            if self.fl.info.get("debug_predict") or __import__("os").environ.get("MF_LOG"):
                t_e, x_e, _ = self.predict(obs, 0.0, 1.0, m, k)
                log("chute_deploy_pred", t_end=t_e, latlon=latlon("Mars", x_e[:3], t_e))
            log("chute_deploy", t=obs["t"], mach=mach, q=q, alt=obs.get("alt"))
            return self.hold({"actions": acts, "bank_deg": 0.0, "dir": unit(np.array(obs.get("airspeed_vector") or obs["v"])).tolist(),
                              "next": 1.0}, None)
        if mach < M.BALLAST_MACH and not self.ballast_dropped and self.ballast > 0:
            acts.append(f"jettison_ballast:{self.hs_id}")
            self.ballast_dropped = True
            log("ballast", t=obs["t"], mach=mach)
        if self.ballast_dropped:
            # no lift left: just wait for the deployment window
            nxt = 1.0 if mach < M.CHUTE_MACH + 0.25 else 2.0
            return self.hold({"actions": acts, "bank_deg": 0.0, "next": nxt}, 0.0)
        # predictor-corrector on the bank magnitude (active above the guidance cutoff speed)
        V = obs.get("airspeed") or float(np.linalg.norm(obs["v"]))
        # Range control all the way to the ballast drop. MSL's heading alignment below 900 m/s (kept here before)
        # held the lift mostly up and gave up the downrange: worlds whose range control was still burning range with
        # the lift down then landed 3 to 6 km long (seeds 1003, 1012, 1023). Below REV_V_MIN the
        # bank sign is instead the one whose sideways lift leaves the smaller predicted crossrange.
        b = self.solve_bank(obs, m, k, x_now)
        self.bank = b
        if V < M.DEPLOY_FLOOR_V:
            self.bank = self.deploy_floor(obs, m, k, self.bank)
        if V <= M.REV_V_MIN:
            self.sign = self.late_sign(obs, m, k, x_now)
        # crossrange: judged on the reference path (no sideways lift from here on). The current bank sign
        # pushes the path towards +sign * side; reverse when that push has carried it past the corridor.
        t_end, x_end, info = self.predict(obs, self.bank, self.sign, m, k, lateral=False)
        dr, cr = self.errors(t_end, x_end, x_now)
        corridor = M.CR_MIN_KM + M.CR_K * (V / 6000.0) ** 2
        if V > M.REV_V_MIN and self.sign * cr > corridor:
            self.sign = -self.sign
            self.n_rev += 1
        ev = info["events"].get("deploy")
        log("entry", t=obs["t"], V=V, mach=mach, q=q, k=k, lds=self.ld_scale, bank=math.degrees(self.bank) * self.sign, dr=dr, cr=cr,
            deploy=ev, alt=obs.get("alt"), nrev=self.n_rev)
        nxt = M.ENTRY_ARC if mach > 4.0 else 2.0
        bank = math.degrees(self.bank) * self.sign
        if self.six:
            bank, nxt = self.slow_reversal(obs["t"], bank, nxt)
        return self.hold({"actions": acts, "bank_deg": bank, "next": nxt}, self.trim_deg)

    def hold(self, cmd, aoa_deg):
        """6-DOF only: the RCS holds the angle of attack at aoa_deg (the trim, then 0 once the ballast
        is gone; None: rate damping again). This aeroshell's center of mass is far aft: thin air at the top of the
        atmosphere turns it away from the airflow, and its aerodynamics turn it back only up to about 18 deg. The
        simulator reads it only with its 6-DOF entry on (MARSFLIGHT_ENTRY6DOF)."""
        if self.six:
            cmd["attitude"] = {"aoa_deg": aoa_deg}
        return cmd

    def slow_reversal(self, t, bank, nxt):
        """6-DOF only: move the commanded bank no faster than M.REV_RATE_6DOF. This aeroshell is only
        weakly stable in pitch, and a fast roll with the RCS can throw it past the angle its aerodynamics can turn
        back. In 3-DOF the command is as before."""
        if self.bank_cmd is None or self.t_last_cmd is None:
            self.bank_cmd, self.t_last_cmd = bank, t
            return bank, nxt
        step = M.REV_RATE_6DOF * max(0.0, t - self.t_last_cmd)
        self.t_last_cmd = t
        d = bank - self.bank_cmd
        if abs(d) > step:
            self.bank_cmd += math.copysign(step, d)
            return self.bank_cmd, 1.0
        self.bank_cmd = bank
        return bank, nxt

    def range_wait(self, obs, m, k, x_now, mach):
        """A range trigger, as Mars 2020 flew (NTRS 20240015538): inside the deploy window, hold the parachute
        while deploying now would land short of the target, since every second of waiting flies further. Never
        below M.CHUTE_MACH_MIN or M.DEPLOY_AGL_MIN above the ground."""
        if mach <= M.CHUTE_MACH_MIN or obs.get("height_above_surface", 0.0) <= M.DEPLOY_AGL_MIN:
            return False
        phase, self.phase = self.phase, "entry"
        try:
            t_e, x_e, _ = self.predict(obs, 0.0, 1.0, m, k)
        finally:
            self.phase = phase
        dr, _ = self.errors(t_e, x_e, x_now)
        if dr < 0:
            log("range_wait", t=obs["t"], mach=mach, dr=dr)
            return True
        return False

    def late_sign(self, obs, m, k, x_now):
        """Below REV_V_MIN: the bank sign whose sideways lift (at the bank magnitude range control asks for) leaves
        the smaller predicted crossrange. It changes only for a gain of LATE_SIGN_HYST_KM, so the vehicle does not
        roll back and forth, and not for a bank within 15 deg of lift up or down, where the side does not matter."""
        if min(self.bank, math.pi - self.bank) < math.radians(15.0):
            return self.sign

        def cr(s):
            t_end, x_end, _ = self.predict(obs, self.bank, s, m, k, lateral=True)
            return abs(self.errors(t_end, x_end, x_now)[1])
        now, other = cr(self.sign), cr(-self.sign)
        if other < now - LATE_SIGN_HYST_KM:
            self.n_rev += 1
            log("late_reversal", t=obs["t"], cr_now=now, cr_other=other, bank=math.degrees(self.bank))
            return -self.sign
        return self.sign

    def heading_align(self, obs, m, k, x_now):
        """The signed bank within M.HA_BANK_MAX that brings the predicted crossrange (with the sideways lift)
        to zero. Below REV_V_MIN the bank sign was held, and with the trim L/D growing as the capsule slows
        (1.4 times at Mach 2) that left 2 to 4 km of crossrange at Jezero."""
        def cr(b):
            t_end, x_end, _ = self.predict(obs, abs(b), 1 if b >= 0 else -1, m, k, lateral=True)
            return self.errors(t_end, x_end, x_now)[1]
        hi = math.radians(M.HA_BANK_MAX)
        lo, f_lo, f_hi = -hi, cr(-hi), cr(hi)
        if f_lo * f_hi > 0:
            return -hi if abs(f_lo) < abs(f_hi) else hi
        for _ in range(5):
            mid = 0.5 * (lo + hi)
            f_mid = cr(mid)
            if f_mid * f_lo > 0:
                lo, f_lo = mid, f_mid
            else:
                hi = mid
        b = 0.5 * (lo + hi)
        log("heading_align", t=obs["t"], bank=math.degrees(b), cr=f_lo)
        return b

    def deploy_floor(self, obs, m, k, bank):
        """The bank magnitude, lowered toward lift up if needed so that the predicted parachute opening stays at
        least M.DEPLOY_AGL_MIN above the ground. Near the end the downrange solver can ask for lift down, and the
        trim L/D grows as the capsule slows (1.4 times at Mach 2), so a held lift-down bank can carry the vehicle
        so deep that the parachute opens too low to slow it before the engines must stop it."""
        def agl(b):
            _, _, info = self.predict(obs, b, self.sign, m, k, lateral=False)
            dep = info["events"].get("deploy")
            return (dep[1] - SITE_ALT) if dep else -1e9
        if self.ballast_dropped or bank <= 1e-3 or agl(bank) >= DEPLOY_FLOOR_AGL_M:
            return bank
        if agl(0.0) < DEPLOY_FLOOR_AGL_M:
            return 0.0
        lo, hi = 0.0, bank            # agl(lo) meets the floor, agl(hi) does not
        for _ in range(6):
            mid = 0.5 * (lo + hi)
            lo, hi = (mid, hi) if agl(mid) >= DEPLOY_FLOOR_AGL_M else (lo, mid)
        log("deploy_floor", t=obs["t"], bank_in=math.degrees(bank), bank=math.degrees(lo))
        return lo

    def solve_bank(self, obs, m, k, x_now):
        """Secant search on the bank magnitude for zero downrange error."""
        def f(b):
            t_end, x_end, info = self.predict(obs, b, self.sign, m, k, lateral=False)
            dr, cr = self.errors(t_end, x_end, x_now)
            return dr - M_DR_AIM_KM
        lo, hi = math.radians(M.BANK_MIN), math.radians(M.BANK_MAX)
        b0 = min(max(self.bank, lo), hi)
        f0 = f(b0)
        db = math.radians(8.0) if f0 > 0 else -math.radians(8.0)
        b1 = min(max(b0 + db, lo), hi)
        if abs(b1 - b0) < 1e-6:
            return b0
        f1 = f(b1)
        for _ in range(4):
            if abs(f1) < 0.3 or abs(f1 - f0) < 1e-6:
                break
            b2 = b1 - f1 * (b1 - b0) / (f1 - f0)
            b2 = min(max(b2, lo), hi)
            b0, f0 = b1, f1
            b1 = b2
            f1 = f(b1)
        return b1 if abs(f1) <= abs(f0) else b0

    # ------------------------------------------------------------------ chute phase
    def chute(self, obs):
        self.update_wind(obs)
        acts = []
        mach = obs.get("mach") or 0.0
        hs_on = self.hs_id in present(obs)
        if hs_on and 0 < mach < M.HS_SEP_MACH:
            acts.append(f"separate:{self.hs_id}")
            hs_on = False
            self.t_hs_sep = obs["t"]
            log("hs_sep", t=obs["t"], mach=mach, alt=obs.get("alt"))
        hagl = obs.get("height_above_surface", 1e9)
        vrel = self.vrel(obs)
        t_sep = getattr(self, "t_hs_sep", None)
        if t_sep is not None and obs["t"] - t_sep < HS_SEP_COAST_S - 1e-6:
            # let the heat shield fall clear before the backshell goes and the engines light: lit in the same call,
            # the lander flew into it (recontact, seeds 1012 and 1023 with a low parachute opening)
            return {"actions": acts, "dir": unit(vrel).tolist(), "next": HS_SEP_COAST_S - (obs["t"] - t_sep)}
        if not hs_on:
            h_pd = self.pd_height(obs, hs_gone=True)
            if hagl <= h_pd + 5.0:
                return self.start_powered(obs, extra=acts)
            # time to the powered-descent height at the current descent rate (short arcs near it)
            vz = -float(np.dot(vrel, unit(np.array(obs["r"]))))
            dt = (hagl - h_pd) / max(vz, 1.0)
            nxt = max(0.5, min(10.0, 0.5 * dt))
            return {"actions": acts, "dir": unit(vrel).tolist(), "next": nxt}
        return {"actions": acts, "dir": unit(vrel).tolist(), "next": 1.0}

    def vrel(self, obs):
        r = np.array(obs["r"])
        v = np.array(obs["v"])
        pz, w = self.frames(obs["t"])
        return v - np.cross(w, r)

    def pd_height(self, obs, hs_gone=True):
        """Height to light the engines: braking distance for the descent rate with all engines at the
        braking throttle, plus margin. Sideways drift (the parachute rides the wind) is cancelled by the
        horizontal channel, so it does not count here."""
        m = self.mass(obs)
        for eid in ("BS", self.chute_id, self.hs_id):
            if eid in present(obs):
                m -= self.model.dry(eid) + (element(obs, eid) or {}).get("rcs_kg", 0.0)
        F = self.model.thrust_at("DL", 700.0, n=self.model.engine_of("DL")[1], throttle=M.PD_THROTTLE)
        a = F / m - 3.71
        vz = -float(np.dot(self.vrel(obs), unit(np.array(obs["r"]))))
        return max(vz * vz / (2 * max(a, 0.5)) + M.PD_MARGIN_M, self.divert_height(vz))

    def zem_extra(self):
        """Seconds by which the ZEM/ZEV phase ends earlier than in 3-DOF (it hands over to the hold loop at
        ZEM_MIN_S_6DOF in 6-DOF, 2 s in 3-DOF)."""
        return (M.ZEM_MIN_S_6DOF if self.fl.info.get("dynamics") == "6dof" else 2.0) - 2.0

    def divert_decel(self, vz):
        """Braking deceleration of the powered descent: PD_DECEL, or less, so that the ZEM/ZEV phase that starts
        PD_AIM_DELAY_S after ignition still has the sqrt(6 DIVERT_MAX_M / DIVERT_ACCEL) s that a DIVERT_MAX_M divert
        needs. The descent then lights higher (about 2 km instead of about 1 km above the ground, as Mars 2020)."""
        t_need = math.sqrt(6.0 * M.DIVERT_MAX_M / M.DIVERT_ACCEL) + M.H_LEAD_S + self.zem_extra() + M.PD_AIM_DELAY_S
        return max(M.PD_DECEL_MIN, min(M.PD_DECEL, (max(vz, M.GATE_V) - M.GATE_V) / t_need))

    def divert_height(self, vz):
        """Height to light the engines for the braking profile of divert_decel."""
        return M.GATE_H + (vz * vz - M.GATE_V ** 2) / (2 * self.divert_decel(vz))

    def vertical_ref(self, h, vz):
        """Descent-rate reference (constant deceleration pd["decel"] to GATE_V at the gate) and time to the gate."""
        a = self.pd.get("decel", M.PD_DECEL)
        vref = -math.sqrt(M.GATE_V ** 2 + 2 * a * (h - M.GATE_H))
        return vref, 2 * (h - M.GATE_H) / max(1.0, abs(vz) + M.GATE_V)

    def choose_on_chute(self, obs):
        """Right after the TRN fix, on the parachute: predict the natural stopping point (the drift under the
        parachute down to the powered-descent start, then half the drift speed over the powered descent) and
        choose its landing cell within DIVERT_MAX_M, as Mars 2020 did. The powered descent keeps this cell when
        it can still reach it (choose_aim)."""
        r = np.array(obs["r"])
        up = unit(r)
        vrel = self.vrel(obs)
        vz = -float(np.dot(vrel, up))
        vh = vrel + vz * up
        h = obs.get("height_above_surface", 0.0)
        h_pd = self.pd_height(obs, hs_gone=True)
        t_chute = max(h - h_pd, 0.0) / max(vz, 1.0)
        t_pd = 2 * (h_pd - M.GATE_H) / max(1.0, vz + M.GATE_V)
        p_nat = r + vh * (t_chute + 0.5 * t_pd)
        if self.hazard is None:
            from .hazard import HazardMap
            self.hazard = HazardMap()
        lat, lon = latlon("Mars", p_nat, obs["t"])
        c = self.hazard.choose(lat, lon, M.DIVERT_MAX_M)
        self.trn_cell = c
        if c is not None:
            log("trn_cell", t=obs["t"], hagl=h, h_pd=h_pd, t_chute=t_chute, t_pd=t_pd, divert_m=c["divert_m"],
                p_natural=c["p_natural"], p_aim=c["p_aim"], cell=c["cell"], lat=c["lat"], lon=c["lon"])

    def start_powered(self, obs, extra=()):
        self.phase = "powered"
        n_all = self.model.engine_of("DL")[1]
        vrel = self.vrel(obs)
        vz0 = -float(np.dot(vrel, unit(np.array(obs["r"]))))
        self.pd = {"t0": obs["t"], "n": n_all, "one_engine": False, "decel": self.divert_decel(vz0)}
        log("pd_start", t=obs["t"], hagl=obs.get("height_above_surface"), v=float(np.linalg.norm(vrel)), decel=self.pd["decel"],
            latlon=latlon("Mars", np.array(obs["r"]), obs["t"]))
        return {"actions": list(extra) + ["separate:BS"], "engines": {"DL": {"throttle": M.PD_THROTTLE, "count": n_all}},
                "dir": unit(-vrel).tolist(), "next": 6.0}

    # ------------------------------------------------------------------ powered descent
    def powered(self, obs):
        if obs["mode"] == "landed" or any(e == "touchdown" for e in obs["events"]):
            self.phase = "landed"
            self.done = True
            log("touchdown", t=obs["t"], events=obs["events"])
            return {"next": 60.0}
        r = np.array(obs["r"])
        up = unit(r)
        vrel = self.vrel(obs)
        vz = float(np.dot(vrel, up))
        vh = vrel - vz * up
        h = obs.get("height_above_surface", 0.0)
        m = self.mass(obs)
        g = 3.711 * (MARS_R / np.linalg.norm(r)) ** 2
        pd = self.pd
        # --- vertical channel: descent-rate reference (constant deceleration to the gate)
        vhn = float(np.linalg.norm(vh))
        if h > M.GATE_H:
            vref, tgo = self.vertical_ref(h, vz)
        else:
            # touchdown speed already TD_FLOOR_H above the ground: the radar height can be 10 m or more off over
            # rough ground, and with the old ramp to 0 m seed 1000 touched down at 2.95 m/s (limit 3.125)
            # Without a landing radar (radar_valid false) the height comes from the navigated position, 20 m or more
            # off (seed 1000 lost its only radar in entry and touched down at 3.2 m/s reading 23 m): reach the
            # touchdown speed higher, by three times the filter's own position error.
            fl = TD_FLOOR_H
            if obs.get("radar_valid") is False:
                fl = min(TD_FLOOR_H + 3.0 * float((obs.get("nav_sigma") or {}).get("position_m") or 30.0), M.GATE_H - 5.0)
            vref = -max(M.TD_V, min(M.GATE_V, M.TD_V + (M.GATE_V - M.TD_V) * (h - fl) / (M.GATE_H - fl)))
            # do not touch down while still sliding sideways: slow the descent until vh is small
            if vhn > M.TD_VH_OK:
                vref = -min(abs(vref), M.TD_V * max(0.25, 1.0 - (vhn - M.TD_VH_OK) / 1.5))
            tgo = None
        # at least 0.2 g up: a vertical command below zero would flip the thrust direction (a 180 deg slew in 6-DOF)
        az = max((vref - vz) / M.TAU_V + g, 0.2 * g)
        # --- horizontal channel: zero-effort-miss law that brings the horizontal speed to zero some seconds
        # before the gate, at an aim point within DIVERT_MAX_M of the natural stopping point
        if "aim" not in pd:
            t_nat = tgo if tgo is not None else 0.0
            # p_nat is where the ground point under the natural stop is now, so compare it with Jezero now
            p_nat = r + vh * (0.5 * t_nat)
            pd["aim"] = self.choose_aim(obs["t"], p_nat, up, t_nat)
        aim = rot_inertial_to_fixed("Mars", obs["t"]).T @ pd["aim"]
        dp = aim - r
        dp -= np.dot(dp, up) * up
        tgo_h = None if tgo is None else tgo - M.H_LEAD_S
        # In 6-DOF the lander's attitude lags the command by a second or two (6 deg gimbals on a tall stack), and
        # the ZEM law's gains grow as tgo shrinks, so it hands over to the speed loop earlier.
        zem_min = M.ZEM_MIN_S_6DOF if self.fl.info.get("dynamics") == "6dof" else 2.0
        # The ZEM/ZEV law aims to stop zem_min - 2 s before tgo_h, so it hands over nearly at rest, as in 3-DOF.
        # Before, in 6-DOF it handed over 8 s short of its end still moving 15 to 30 m/s sideways, and the hold
        # loop then coasted 50 to 75 m past the aim (seed 1040).
        t_z = None if tgo_h is None else tgo_h - (zem_min - 2.0)
        if t_z is not None and t_z > 2.0:
            # ZEM/ZEV with zero final speed: a = 6 ZEM / t^2 - 2 ZEV / t = 6 dp / t^2 - 4 v / t
            ah = 6.0 * dp / t_z ** 2 - 4.0 * vh / t_z
        else:
            # Hold the aim point: a critically damped loop on position and speed (time constant TAU_H), moving
            # towards the aim at most HOLD_V_MAX. The ZEM law hands over still moving 2 to 10 m/s sideways (its
            # last seconds hit the tilt limit), and a loop on speed alone let the lander coast 6 to 25 m past the
            # aim, often into the next map cell.
            v_cmd = dp / (2.0 * M.TAU_H)
            vc = float(np.linalg.norm(v_cmd))
            if vc > M.HOLD_V_MAX:
                v_cmd *= M.HOLD_V_MAX / vc
            ah = 2.0 * (v_cmd - vh) / M.TAU_H
        an = np.linalg.norm(ah)
        if h > M.GATE_H:
            tilt_max = math.radians(M.TILT_MAX)
        elif h > 3.0:
            tilt_max = math.radians(M.TILT_TERMINAL)
        else:
            tilt_max = math.radians(5.0)
        if an > az * math.tan(tilt_max):
            ah *= az * math.tan(tilt_max) / an
        # --- the vertical channel comes first: when the engines cannot give both, cut the horizontal command
        F1 = self.model.thrust_at("DL", 700.0, n=1, throttle=1.0)
        a_max = pd["n"] * F1 / m
        an = float(np.linalg.norm(ah))
        if az ** 2 + an ** 2 > a_max ** 2 and an > 0:
            ah *= math.sqrt(max(a_max ** 2 - az ** 2, 0.0)) / an
        acc = az * up + ah
        F_req = m * np.linalg.norm(acc)
        # --- engines: all of them while braking; one shuts down in the gate region
        thr_min = self.model.min_throttle("DL")
        n = pd["n"]
        n_all = self.model.engine_of("DL")[1]
        n_low = n_all - 1
        # (at any height: on the slower braking profile of divert_decel all engines at their lowest throttle brake
        # harder than the profile asks, and the lander would stop and climb)
        if n > n_low >= 1 and F_req < 0.9 * n_low * F1:
            n = n_low
            pd["n"] = n_low
        elif n < n_all and F_req > 0.97 * n * F1:
            # light the engine again when the others cannot give what the profile asks: with the 1.3 t heavier
            # surface payload two engines at full thrust fell behind the braking profile and every world
            # crashed at 8 to 25 m/s
            n = n_all
            pd["n"] = n_all
        thr = min(1.0, max(thr_min, F_req / (n * F1)))
        d = unit(acc)
        # never a one-step arc near the ground (the simulator misses a touchdown on an arc's last step)
        nxt = 1.0 if h > 30 else 0.6
        log("pd", t=obs["t"], h=h, vz=vz, vh=float(np.linalg.norm(vh)), n=n, thr=thr, miss=float(np.linalg.norm(dp)),
            m=m, prop=prop_left(obs, "DL"), tgo=tgo)
        return {"engines": {"DL": {"throttle": thr, "count": n}}, "dir": d.tolist(), "next": nxt}

    def choose_aim(self, t, p_nat, up, t_nat):
        """Aim point of the powered descent, Mars-fixed. The divert from the natural stopping point p_nat
        (inertial) is at most DIVERT_MAX_M, and less when little time is left (DIVERT_ACCEL). After the TRN
        fix the aim is the center of the map cell that HazardMap.choose picks within that divert. Before it,
        or off the map, the navigated position can be about 1 km off, so the map is no use, and p_nat moves
        towards Jezero."""
        t_div = max(t_nat - M.H_LEAD_S - self.zem_extra(), 0.0)
        radius = min(M.DIVERT_MAX_M, M.DIVERT_ACCEL * t_div ** 2 / 6.0)
        d = self.site_inertial(t) - p_nat
        d -= np.dot(d, up) * up
        dn = np.linalg.norm(d)
        if self.trn_t is not None:
            if self.hazard is None:
                from .hazard import HazardMap
                self.hazard = HazardMap()
            lat, lon = latlon("Mars", p_nat, t)
            c, mode = None, "hazard"
            tc = getattr(self, "trn_cell", None)
            if tc is not None:
                # keep the cell chosen on the parachute when it is still within reach
                reach = math.hypot(*self.hazard.ground_m(tc["lat"], tc["lon"], lat, lon))
                if reach <= radius:
                    c, mode = dict(tc, divert_m=reach), "trn_cell"
                else:
                    log("trn_cell_out_of_reach", t=t, reach_m=reach, radius_m=radius)
            if c is None:
                c = self.hazard.choose(lat, lon, radius)
            if c is not None:
                la, lo = math.radians(c["lat"]), math.radians(c["lon"])
                log("pd_aim", t=t, mode=mode, dist_to_jezero_km=dn / 1e3, t_nat=t_nat, radius_m=radius,
                    divert_m=c["divert_m"], p_natural=c["p_natural"], p_aim=c["p_aim"], cell=c["cell"], lat=c["lat"],
                    lon=c["lon"])
                return np.linalg.norm(p_nat) * np.array([math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)])
        nudge = d * min(1.0, radius / dn) if dn > 1 else np.zeros(3)
        log("pd_aim", t=t, mode="nudge", dist_to_jezero_km=dn / 1e3, nudge_m=float(np.linalg.norm(nudge)), t_nat=t_nat,
            radius_m=radius)
        return rot_inertial_to_fixed("Mars", t) @ (p_nat + nudge)

    def next_handler(self):
        from .mars_ascent import MarsSurface
        return MarsSurface(self.fl)


def predict_reference(edl, t, x, bank_ref_deg=None):
    """Predict the descent from an entry-interface state (t, x Mars-centered inertial) with the reference
    profile the entry guidance flies around: lift kept vertical (no sideways lift), L/D scaled by
    cos(reference bank). Returns (t_end, x_end, info)."""
    bank_ref = math.radians(bank_ref_deg if bank_ref_deg is not None else M.BANK_REF)
    obs = {"t": t, "r": list(x[:3]), "v": list(x[3:])}
    saved = (edl.ld, edl.phase, edl.ballast_dropped)
    try:
        edl.phase = "entry"
        edl.ballast_dropped = False
        ld0 = edl.ld
        edl.ld = ld0 * math.cos(bank_ref)
        edl.ld_scale = 1.0
        m = edl.entry_mass
        k = A.season_factor(t)
        return edl.predict(obs, 0.0, 1.0, m, k)
    finally:
        edl.ld, edl.phase, edl.ballast_dropped = saved
