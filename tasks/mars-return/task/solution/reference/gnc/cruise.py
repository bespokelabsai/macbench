"""Interplanetary cruise with trajectory correction maneuvers (TCMs) on RCS, and the approach to the
entry interface. Generic for both legs: Earth to Mars (RCS on the backshell) and Mars to Earth (RCS on
the Earth return stage)."""
from __future__ import annotations

import math
import os

import numpy as np

from . import mission as M
from . import target as TG
from .log import log
from .util import DAY, G0, MU, unit
from .vehicle_model import element, present, stack_mass

RCS_ISP = 225.0
BURN_ALIGN_DEG = 2.0       # a correction fires once the stack points within this of its direction ...
BURN_RATE_MAX = 0.1        # deg/s ... and has stopped turning
BURN_ALIGN_WAIT_S = 1800.0 # s: fire anyway after this long


class Cruise:
    def __init__(self, fl, leg="out"):
        self.fl = fl
        self.leg = leg
        self.done = False
        self.sep_wait_t0 = None      # when the stack started waiting for the separation axis
        if leg == "out":
            self.body = "Mars"
            self.t_arr = M.T_ARR
            self.rcs_el = M.CRUISE_RCS
            self.sched = [("tcm1", None), ("tcm2", M.T_ARR - 60 * DAY), ("tcm3", M.T_ARR - 12 * DAY),
                          ("tcm4", M.T_ARR - 2.2 * DAY), ("tcm5", M.T_ARR - 0.55 * DAY)]
        else:
            self.body = "Earth"
            self.t_arr = M.T_EARR
            self.rcs_el = M.RETURN_RCS
            # Earth avoidance (planetary protection, ESA ERO practice): rtcm1 to rtcm3 aim at a miss of
            # EARTH_SAFE_PERIGEE_M; rtcm4, inside the last week, puts the vehicle on course for entry.
            self.sched = [("rtcm1", None), ("rtcm2", M.T_EARR - 60 * DAY), ("rtcm3", M.T_EARR - 12 * DAY),
                          ("rtcm4", M.T_EARR - M.EARTH_INTERCEPT_DAYS * DAY), ("rtcm5", M.T_EARR - 2.5 * DAY),
                          ("rtcm6", M.T_EARR - 0.45 * DAY)]
        self.k = 0
        self.burn = None
        self.t_first = None
        self.aim = getattr(fl, "mars_aim" if leg == "out" else "earth_aim", None)
        self.log_tcm = []

    # ------------------------------------------------------------------ aim
    def current_aim(self, enc):
        if self.leg == "out":
            from .edl import EDL
            if not hasattr(self, "_edl"):
                self._edl = EDL(self.fl)
            return TG.aim_point_edl(self._edl, enc["S"], enc["vinf"], self.t_arr, M.JEZERO, M.EI_FPA)
        return TG.aim_point(enc["S"], enc["vinf"], self.t_arr, M.UTTR_GC[0], M.UTTR_GC[1], M.EARTH_EI_FPA, M.EARTH_RANGE_DEG,
                            M.EARTH_ENTRY_TIME, body="Earth", r_ei=6378137.0 + 120e3)

    def tcm_time(self, obs):
        name, t = self.sched[self.k]
        if t is None:
            if self.t_first is None:
                self.t_first = obs["t"] + 1.0 * DAY
            t = self.t_first
        return name, t

    # ------------------------------------------------------------------ step
    def step(self, obs):
        if os.environ.get("MF_DIAG") and not getattr(self, "_diag", False):
            # development aid: the encounter as the cruise starts
            self._diag = True
            x = np.array(obs["r"] + obs["v"])
            enc = TG.encounter(obs["t"], x, obs["body"], self.body, self.t_arr + 60 * DAY, self.t_arr)
            log("diag_cruise_start", t=obs["t"], body=obs["body"], r=obs["r"], v=obs["v"], nav=obs.get("nav_sigma"),
                upd=obs.get("nav_update"), enc=None if enc is None else {k: enc[k] for k in ("bt", "br", "tp")})
        if self.burn is not None:
            return self.burn_step(obs)
        # skip maneuvers whose slot has passed (scenario starts part way through the cruise)
        if self.k == 0 and self.fl.scenario == ("mars_approach" if self.leg == "out" else "earth_approach") \
                and not getattr(self, "_skipped", False):
            self.k = 1
        self._skipped = True
        while self.k < len(self.sched) and self.sched[self.k][1] is not None and obs["t"] > self.sched[self.k][1] + 600.0:
            self.k += 1
        if self.k >= len(self.sched):
            return self.approach(obs)
        name, t_tcm = self.tcm_time(obs)
        if obs["t"] < t_tcm - 120.0:
            nxt = t_tcm - 60.0 - obs["t"]
            return {"next": min(nxt, 50 * DAY)}
        return self.plan_tcm(obs, name)

    def predict_stop_alt(self):
        """Height above the ellipsoid where the landing prediction ends: the main parachute's deployment height
        when the capsule has a parachute (below it the capsule comes down nearly vertically, drifting with the
        wind, which is not known in advance), else the Utah ground."""
        from .earth_entry import CHUTE_DEPLOY_ALT, UTAH_GROUND_M, capsule_chute
        return CHUTE_DEPLOY_ALT if capsule_chute(self.fl.info) else UTAH_GROUND_M

    def capsule_model(self, obs=None):
        """The entry capsule's mass and hypersonic drag area for the landing predictor. The mass is the measured
        total (obs "mass") less what separates before entry. The build report's dry mass leaves out the spare
        avionics string and the cross strap (40.6 kg on CAP) and the hidden as-built growth, so it put the capsule
        at 158 + 11 kg against about 212 kg flown, 20% too light, and the predicted landing short."""
        import math as _m
        from . import edl_model as E
        from .earth_entry import capsule_chute
        des = {e["id"]: e for e in self.fl.info["design"]["elements"]}
        c = des[M.CAPSULE]
        rn = c.get("nose_radius_m", 0.25 * c["diameter_m"])
        cd = E.newtonian_cd(c.get("half_angle_deg", 70), rn, c["diameter_m"] / 2) * (1 - 0.5 * c.get("ld_trim", 0.0))
        ch = capsule_chute(self.fl.info)
        keep = {M.CAPSULE} | ({ch[0]} if ch is not None else set())
        m = self.dry_full(M.CAPSULE) + self.fl.model.sample_kg + (self.dry_full(ch[0]) if ch is not None else 0.0)
        if obs is not None and obs.get("mass"):
            # Scale the modelled stack to the measured mass (the hidden as-built growth), and give the capsule its share
            other = sum(self.dry_full(e["id"]) + e.get("fuel_kg", 0.0) + e.get("ox_kg", 0.0) + e.get("rcs_kg", 0.0)
                        for e in obs["elements"] if e["id"] not in keep)
            k = float(obs["mass"]) / (m + other)
            if 0.9 < k < 1.2:
                m *= k
        return m, cd * _m.pi * c["diameter_m"] ** 2 / 4

    def dry_full(self, eid):
        """An element's dry mass with the spares its redundancy adds, which the build report's dry mass leaves out:
        a second avionics string as heavy as the first (dual or cross-strapped), 6 kg of cross strapping, and 2 kg
        for a second RCS branch (manual: "Redundancy"; the simulator's redundancy mass items)."""
        m = self.fl.model.dry(eid)
        el = self.fl.model.elements.get(eid, {})
        des = next((e for e in self.fl.info["design"]["elements"] if e.get("id") == eid), {})
        red = des.get("redundancy") or {}
        av = (el.get("breakdown_kg") or {}).get("avionics", 0.0)
        if red.get("avionics") in ("dual", "cross_strapped"):
            m += av
        if red.get("avionics") == "cross_strapped":
            m += 6.0
        if int(red.get("rcs_branches", 1) or 1) == 2:
            m += 2.0
        return m

    def sep_push(self, obs):
        """6-DOF only: the capsule's velocity change when the return stage separates from below it, along the
        planned separation axis (along the velocity), from the masses now. None in 3-DOF, which has no push."""
        if obs.get("dynamics") != "6dof" or M.RETURN_RCS not in present(obs):
            return None
        md = mv = 0.0
        for e in obs["elements"]:
            mass = self.dry_full(e["id"]) + e.get("fuel_kg", 0.0) + e.get("ox_kg", 0.0) + e.get("rcs_kg", 0.0)
            if e["id"] == M.RETURN_RCS:
                md += mass
            else:
                mv += mass
        mv += self.fl.model.sample_kg if obs.get("samples_loaded") else 0.0
        axis = unit(np.array(obs["v"]))
        return axis, M.SEP_PUSH * md / (md + mv) * axis

    def plan_landing_tcm(self, obs, name):
        x = np.array(obs["r"] + obs["v"])
        m, cdA = self.capsule_model(obs)
        push = self.sep_push(obs) if name == M.LANDING_TCMS[-1] else None
        push_plan = None
        if push is not None:
            # Aim for the trajectory with the separation push, applied where it happens: along the velocity, at
            # CAP_SEP_BEFORE_EI before the 160 km crossing (gnc/cruise.py approach). Applied at the correction itself
            # (7 hours earlier, as before), it moved the predicted landing about 3 times as far as the real push does,
            # and the capsules came down 4 km south-west of the post-separation prediction in every world.
            self.sep_axis = push[0]
            push_plan = (M.CAP_SEP_BEFORE_EI, float(np.linalg.norm(push[1])))
            log("sep_push_planned", name=name, dv=push[1].tolist())
        dv, res = TG.solve_landing(obs["t"], x, obs["body"], M.UTTR, M.EARTH_EI_FPA, m, cdA, stop_alt=self.predict_stop_alt(),
                                   push=push_plan)
        self.k += 1
        if dv is None:
            log("ltcm_fail", name=name, t=obs["t"])
            return {"next": 600.0}
        mag = float(np.linalg.norm(dv))
        log("ltcm_plan", name=name, t=obs["t"], dv=dv.tolist(), mag=mag, pred=res)
        if mag < 0.005:
            return {"next": 60.0}
        if mag > M.TCM_MAX:
            dv = dv * (M.TCM_MAX / mag)
            mag = M.TCM_MAX
        m_all = stack_mass(obs, self.fl.model)
        self.burn = {"name": name, "dir": unit(dv), "dv_left": mag, "m": m_all, "F": self.rcs_thrust(), "oriented": False}
        return {"dir": self.burn["dir"].tolist(), "next": 90.0}

    def plan_intercept(self, obs, name):
        """The first correction inside the last week (planetary protection): from the safe miss onto the entry
        corridor. A B-plane correction to the Utah aim of the last correction (not a new aim: from the miss, the aim
        search can pick another pass), then the landing solver from that state, as one burn."""
        t, x = obs["t"], np.array(obs["r"] + obs["v"])
        aim = (self.aim["bt"], self.aim["br"], self.aim["tp"])
        # Continuation: the move from the safe miss to the entry corridor is about 9,500 km in the B-plane, too far
        # for one Newton solve from zero (on v3 it returned 0 or 16 m/s instead of about 20, and the
        # capsule missed Earth). Solve to aims part of the way from the current encounter, each from the last answer.
        enc = TG.encounter(t, x, obs["body"], self.body, self.t_arr + 60 * DAY, self.t_arr)
        dv0 = None
        if enc is not None:
            for frac in (0.34, 0.67):
                sub = (enc["bt"] + frac * (aim[0] - enc["bt"]), enc["br"] + frac * (aim[1] - enc["br"]),
                       enc["tp"] + frac * (aim[2] - enc["tp"]))
                d, _ = TG.solve_dv(t, x, obs["body"], sub, dv0=dv0, body=self.body, w_time=M.TCM_W_TIME,
                                   t_max=self.t_arr + 60 * DAY, h_fd=M.INTERCEPT_FD_MPS)
                if d is not None:
                    dv0 = d
        dv0, enc2 = TG.solve_dv(t, x, obs["body"], aim, dv0=dv0, body=self.body, w_time=M.TCM_W_TIME,
                                t_max=self.t_arr + 60 * DAY, iters=10, h_fd=M.INTERCEPT_FD_MPS)
        log("intercept_solve", name=name, enc=None if enc is None else {k: enc[k] for k in ("bt", "br", "tp")},
            after=None if enc2 is None else {k: enc2[k] for k in ("bt", "br", "tp")},
            dv=None if dv0 is None else [float(v) for v in dv0])
        if dv0 is None:
            log("tcm_fail", name=name, t=t)
            self.k += 1
            return {"next": 600.0}
        m, cdA = self.capsule_model(obs)
        x1 = x.copy()
        x1[3:] += dv0
        dv = dv0
        if TG.landing_of(t, x1, obs["body"], m, cdA) is not None:
            dv1, res = TG.solve_landing(t, x1, obs["body"], M.UTTR, M.EARTH_EI_FPA, m, cdA, stop_alt=self.predict_stop_alt())
            if dv1 is not None:
                dv = dv0 + dv1
                log("intercept_landing", name=name, dv_bplane=float(np.linalg.norm(dv0)), dv_landing=float(np.linalg.norm(dv1)),
                    pred=res)
        self.k += 1
        mag = float(np.linalg.norm(dv))
        log("tcm_plan", name=name, t=t, dv=dv.tolist(), mag=mag, intercept=True)
        if mag > M.INTERCEPT_TCM_MAX:
            dv, mag = dv * (M.INTERCEPT_TCM_MAX / mag), M.INTERCEPT_TCM_MAX
        self.burn = {"name": name, "dir": unit(dv), "dv_left": mag, "m": stack_mass(obs, self.fl.model), "F": self.rcs_thrust(),
                     "oriented": False}
        return {"dir": self.burn["dir"].tolist(), "next": 90.0}

    def plan_tcm(self, obs, name):
        x = np.array(obs["r"] + obs["v"])
        t = obs["t"]
        if self.leg == "out" and name in M.MARS_LANDING_TCMS:
            from .edl import EDL
            if not hasattr(self, "_edl"):
                self._edl = EDL(self.fl)
            if TG.mars_landing_of(self._edl, t, x, obs["body"]) is not None:
                dv, res = TG.solve_mars_landing(self._edl, t, x, obs["body"], M.JEZERO, M.EI_FPA)
                self.k += 1
                if dv is None:
                    log("mtcm_fail", name=name, t=t)
                    return {"next": 600.0}
                mag = float(np.linalg.norm(dv))
                log("mtcm_plan", name=name, t=t, dv=dv.tolist(), mag=mag, pred=res)
                if mag < 0.005:
                    return {"next": 60.0}
                if mag > M.TCM_MAX:
                    dv = dv * (M.TCM_MAX / mag)
                    mag = M.TCM_MAX
                m_all = stack_mass(obs, self.fl.model)
                self.burn = {"name": name, "dir": unit(dv), "dv_left": mag, "m": m_all, "F": self.rcs_thrust(), "oriented": False}
                return {"dir": self.burn["dir"].tolist(), "next": 90.0}
        if self.leg == "back" and name in M.LANDING_TCMS:
            m, cdA = self.capsule_model(obs)
            if TG.landing_of(t, x, obs["body"], m, cdA) is not None:
                return self.plan_landing_tcm(obs, name)
            if name == M.LANDING_TCMS[0] and self.aim is not None:
                return self.plan_intercept(obs, name)
        if obs["body"] in ("Earth", "Mars") and obs["body"] != self.body:
            en = float(np.dot(x[3:], x[3:])) / 2 - MU[obs["body"]] / np.linalg.norm(x[:3])
            if en < 0:
                # still captured by the departure planet: no correction can help from here
                log("tcm_bound", name=name, t=t, energy=en)
                self.k += 1
                return {"next": 5 * DAY}
        import time as _time
        tw = _time.time()
        log("tcm_start", name=name, t=t, body=obs["body"])
        enc = TG.encounter(t, x, obs["body"], self.body, self.t_arr + 60 * DAY, self.t_arr)
        log("tcm_enc", name=name, dt=_time.time() - tw, enc=None if enc is None else {k: enc[k] for k in ("bt", "br", "tp", "soi")})
        if enc is None:
            log("tcm_no_encounter", name=name, t=t)
            # retarget from a Lambert-like guess: aim straight at the planet with a coarse shot
            enc = None
        if enc is not None:
            self.aim = self.current_aim(enc)
        log("tcm_aim", name=name, dt=_time.time() - tw, aim=self.aim)
        aim = (self.aim["bt"], self.aim["br"], self.aim["tp"])
        if self.leg == "back" and enc is not None and self.aim["tp"] - t > M.EARTH_AVOID_S:
            # more than a week out: aim at the same B-plane angle, but at a perigee EARTH_SAFE_PERIGEE_M up
            safe = TG.safe_miss_aim(self.aim, enc["vinf"], M.EARTH_SAFE_PERIGEE_M)
            aim = (safe["bt"], safe["br"], safe["tp"])
            log("tcm_safe_aim", name=name, b_safe=safe["b_safe"], days_to_perigee=(self.aim["tp"] - t) / DAY)
        dv, enc2 = TG.solve_dv(t, x, obs["body"], aim, body=self.body, w_time=M.TCM_W_TIME, t_max=self.t_arr + 60 * DAY)
        self.k += 1
        if dv is None:
            log("tcm_fail", name=name, t=t)
            return {"next": 600.0}
        mag = float(np.linalg.norm(dv))
        log("tcm_plan", name=name, t=t, dv=dv.tolist(), mag=mag, enc=None if enc is None else
            {k: enc[k] for k in ("bt", "br", "tp")}, aim=self.aim, after=None if enc2 is None else
            {k: enc2[k] for k in ("bt", "br", "tp")})
        self.log_tcm.append((name, mag))
        if mag < 0.01:
            return {"next": 60.0}
        if mag > M.TCM_MAX:
            log("tcm_clipped", name=name, mag=mag)
            dv = dv * (M.TCM_MAX / mag)
            mag = M.TCM_MAX
        m = stack_mass(obs, self.fl.model)
        F = self.fl.model.elements[self.rcs_el].get("rcs_thrust_N") if False else self.rcs_thrust()
        self.burn = {"name": name, "dir": unit(dv), "dv_left": mag, "m": m, "F": F, "oriented": False}
        # orient first: the attitude slews during this arc
        return {"dir": self.burn["dir"].tolist(), "next": 90.0}

    def rcs_thrust(self):
        d = self.fl.info["design"]
        for e in d["elements"]:
            if e["id"] == self.rcs_el:
                return float(e.get("rcs_thrust_N", 0.0))
        return 0.0

    def uplink_wait(self, obs):
        """A correction burn in solar orbit must be uplinked (manual, "Correction burns need the ground"): it is
        refused during the solar conjunction moratorium or with no DSN uplink in the 7 days before. Returns a wait
        command while the burn cannot go, else None."""
        b = self.burn
        refused = any("not uplinked" in str(e) for e in obs.get("command_errors") or [])
        if refused and b.get("firing"):
            # the last arc did not burn: book no velocity change for it
            b["firing"] = False
            b["refused"] = b.get("refused", 0) + 1
            log("tcm_refused", name=b["name"], t=obs["t"], errors=obs.get("command_errors"), comms=obs.get("comms"))
        if obs.get("body") != "Sun":
            return None
        c = obs.get("comms") or {}
        if c.get("moratorium") or (b.get("refused") and not c.get("dsn_contact")):
            b.setdefault("wait_t0", obs["t"])
            return {"dir": b["dir"].tolist(), "next": 3600.0}
        return None

    def burn_step(self, obs):
        b = self.burn
        wait = self.uplink_wait(obs)
        if wait is not None:
            return wait
        a = b.get("a_true") or b["F"] / b["m"]
        if b["oriented"] and b.get("firing"):
            sa = obs.get("sensed_accel")
            # the velocity change along the burn direction (the accelerometers' mean over the last second)
            am = float(np.dot(np.array(sa), b["dir"])) if sa else 0.0
            a_true = am if am > 1e-6 else b["last_a"]
            b["dv_left"] -= b["last_dt"] * a_true
            log("tcm_burn_arc", name=b["name"], t=obs["t"], a=a_true, dt=b["last_dt"], dv_left=b["dv_left"], sa=sa,
                v=obs["v"], att=obs.get("attitude"))
            b["a_true"] = a = a_true
        if not b.get("firing"):
            # 6-DOF: the stack turns on its RCS, and a reversal (a retrograde correction) takes minutes on a small
            # RCS. Fire only once it points within BURN_ALIGN_DEG and has stopped turning: rtcm6 used to fire 90 s after
            # its command, still turning, and delivered about a third of its 0.46 m/s, 17 km at Utah.
            att = obs.get("attitude")
            rates = obs.get("body_rates_deg_s") or [0.0, 0.0, 0.0]
            b.setdefault("t_cmd", obs["t"])
            if att is not None and obs["t"] - b["t_cmd"] < BURN_ALIGN_WAIT_S and (
                    float(np.dot(unit(np.array(att)), b["dir"])) < math.cos(math.radians(BURN_ALIGN_DEG))
                    or max(abs(w) for w in rates) > BURN_RATE_MAX):
                return {"dir": b["dir"].tolist(), "next": 20.0}
        b["oriented"] = True
        if b["dv_left"] <= 1e-4:
            log("tcm_done", name=b["name"], t=obs["t"])
            self.burn = None
            return {"dir": b["dir"].tolist(), "next": 30.0}
        dt = b["dv_left"] / a
        dt = min(dt, 100.0)
        b["last_dt"], b["last_a"] = dt, a
        b["firing"] = True
        return {"rcs": {"element": self.rcs_el, "throttle": 1.0}, "dir": b["dir"].tolist(), "next": dt}

    def approach(self, obs):
        """Coast to entry. Arcs end on time a few seconds above the atmosphere (the simulator's
        altitude terminations overshoot by an integrator step), then short arcs into it."""
        from . import predict as P
        if self.leg == "out":
            if obs["body"] == "Mars" and obs.get("alt", 1e9) < 135e3:
                self.done = True
                return self.heatshield_first({"next": 1.0}, obs["t"], np.array(obs["r"] + obs["v"]))
            if obs["body"] == "Mars" and obs.get("alt", 1e9) < 160e3:
                return self.heatshield_first({"next": 2.0}, obs["t"], np.array(obs["r"] + obs["v"]))
            x = np.array(obs["r"] + obs["v"])
            t1, x1, c1, why = P.coast(obs["t"], x, obs["body"], obs["t"] + 20 * DAY, stop_alt=140e3, stop_body="Mars")
            if why == "alt":
                dt = t1 - obs["t"] - 2.0
                log("approach", t=obs["t"], t_ei140=t1, dt=dt)
                if dt > 1.0:
                    return self.heatshield_first({"next": dt}, t1, x1)
                return self.heatshield_first({"next": 2.0}, t1, x1)
            return {"next": 20 * DAY}
        # Earth: drop the return stage a few hours before entry, then coast on time to 160 km
        if obs["body"] == "Earth" and os.environ.get("MF_DIAG"):
            key = (M.RETURN_RCS in present(obs), self.k)
            if getattr(self, "_diag_key", None) != key:
                # development aid: the predicted landing as the approach goes on (before and after the separation)
                self._diag_key = key
                m, cdA = self.capsule_model(obs)
                res = TG.landing_of(obs["t"], np.array(obs["r"] + obs["v"]), "Earth", m, cdA, stop_alt=self.predict_stop_alt())
                log("diag_approach_pred", t=obs["t"], attached=key[0], pred=res, att=obs.get("attitude"), v=obs["v"],
                    r=obs["r"], mass=obs.get("mass"), m_cap=m)
        if obs["body"] == "Earth":
            if M.RETURN_RCS in present(obs):
                x = np.array(obs["r"] + obs["v"])
                t1, x1, c1, why = P.coast(obs["t"], x, "Earth", obs["t"] + 20 * DAY, stop_alt=160e3, stop_body="Earth")
                axis = getattr(self, "sep_axis", None)
                if why == "alt" and t1 - obs["t"] < M.CAP_SEP_BEFORE_EI:
                    att = obs.get("attitude")
                    rates = obs.get("body_rates_deg_s") or [0.0, 0.0, 0.0]
                    if self.sep_wait_t0 is None:
                        self.sep_wait_t0 = obs["t"]
                        if axis is not None:
                            # separate along the velocity now, as the last correction planned the push
                            self.sep_axis = axis = unit(np.array(obs["v"]))
                    rcs_kg = (element(obs, M.RETURN_RCS) or {}).get("rcs_kg")
                    # Never wait past SEP_WAIT_MAX_S or with the return stage's RCS empty: the stack can then no
                    # longer turn, and a return stage still attached at entry burns up with the samples (world 1034
                    # under the RCS ran dry in the last corrections and the stack waited to entry).
                    give_up = obs["t"] - self.sep_wait_t0 > M.SEP_WAIT_MAX_S or (rcs_kg is not None and rcs_kg < M.SEP_RCS_MIN_KG)
                    if axis is not None and att is not None and not give_up and (
                            float(np.dot(unit(np.array(att)), axis)) < math.cos(math.radians(M.SEP_ALIGN_DEG))
                            or max(abs(w) for w in rates) > M.SEP_RATE_MAX):
                        # still turning to the separation axis: a stack that turns while it separates swings the
                        # spent stage back into the capsule
                        return {"dir": axis.tolist(), "next": 5.0}
                    log("capsule_sep", t=obs["t"], t_ei=t1, waited_s=obs["t"] - self.sep_wait_t0, rcs_kg=rcs_kg,
                        aligned=not give_up, r=obs["r"], v=obs["v"], att=obs.get("attitude"), mass=obs.get("mass"))
                    return {"actions": [f"separate:{M.RETURN_RCS}"], "next": 60.0}
                if axis is not None:
                    return {"dir": axis.tolist(), "next": max(1.0, min(60.0, t1 - obs["t"] - M.CAP_SEP_BEFORE_EI + 1.0))}
                if why == "alt":
                    return {"next": max(1.0, t1 - obs["t"] - M.CAP_SEP_BEFORE_EI + 1.0)}
                return {"next": 20 * DAY}
            if obs.get("alt", 1e9) < 155e3:
                self.done = True
                return {"next": 1.0}
            if obs.get("alt", 1e9) < 175e3:
                return {"next": 2.0}
            x = np.array(obs["r"] + obs["v"])
            t1, x1, c1, why = P.coast(obs["t"], x, "Earth", obs["t"] + 20 * DAY, stop_alt=160e3, stop_body="Earth")
            if why == "alt":
                return {"next": max(1.0, t1 - obs["t"] - 2.0)}
        return {"next": 20 * DAY}

    def heatshield_first(self, cmd, t, x):
        """6-DOF only: turn the heatshield (the bottom of the stack) into the airflow it will meet at
        time t and state x, as a real entry vehicle does before the entry interface. In 6-DOF the entry starts with
        the attitude the vehicle has, and this aeroshell would also fly backshell first. In 3-DOF the simulator
        turns the heatshield into the airflow itself, so the command stays as it was."""
        if self.fl.info.get("dynamics") != "6dof":
            return cmd
        from .edl import EDL
        if not hasattr(self, "_edl"):
            self._edl = EDL(self.fl)
        _, w = self._edl.frames(t)
        air = x[3:] - np.cross(w, x[:3])
        return dict(cmd, dir=unit(-air).tolist())

    def next_handler(self):
        if self.leg == "out":
            from .edl import EDL
            return EDL(self.fl)
        try:
            from .earth_entry import EarthEntry
            return EarthEntry(self.fl)
        except ImportError:
            return End(self.fl)


class End:
    def __init__(self, fl):
        self.fl = fl
        self.done = False

    def step(self, obs):
        return {"actions": ["end:mission"]}
