"""Earth phases: pad wait, S1 gravity turn, S2 closed-loop insertion, parking orbit, TMI.

Mission constants for the reference flight live in mission.py; this module only flies.
"""
from __future__ import annotations

import math

import numpy as np

from . import mission as M
from .interplanetary import planet_vinf
from .launch import plan, perigee_dir
from .log import log
from .powered import Insertion, _rk4
from .util import G0, MU, J2, RREF, angle, hyperbola_velocity, kepler_vinf, orbit_rp_ra, pole, rodrigues, unit
from .vehicle_model import accel_meas, element, present, prop_left, stack_mass


# Upper-wind launch commit limit (Pa deg): the largest q-alpha left after the wind biasing that the launch commits
# to. To be calibrated on the IGRA Cape Canaveral bank with the structural load rule (every sounding under the limit must keep S1's load ratio below 1 with a 2 sigma balloon error). Still the
# placeholder of mission.py (150e3) until that test's nominal path is fixed. MARSFLIGHT_REF_UPPER_WIND_QALPHA
# overrides it for a calibration sweep.
UPPER_WIND_QALPHA_MAX = 150e3

def _tune(name, default):
    """Development aid: MF_TUNE_<name> in the environment overrides a constant of this module for a sweep."""
    import os
    v = os.environ.get("MF_TUNE_" + name)
    return float(v) if v else default


# S1/S2 staging (see EarthAscent.staging)
S1_TAILOFF_COAST_S = 2.5   # s from S1 cutoff to separation: the tail-off is down to 5% after 0.75 s (manual, "Separation")
S1_SEP_COAST_S = 1.5       # s from separation to the S2 start command, with ullage thrust, while the gap opens
S2_HOLD_S = 8.0            # s: S2 burns on the held attitude at most this long before turning, if S1 is not yet clear
# Load relief on the measured airflow (EarthAscent.load_relief): it fades in over this dynamic pressure (Pa) ...
RELIEF_Q = (_tune("RELIEF_Q0", 4e3), _tune("RELIEF_Q1", 12e3))
RELIEF_MAX_DEG = _tune("RELIEF_MAX_DEG", 10.0)   # ... and turns the steering towards the airflow by up to this
S1_VFOLLOW_MIN = 60.0   # m/s: S1 follows its Earth-relative velocity only from this speed on
S2_LOADED_MR = 3.55               # S2's loaded oxidizer to fuel ratio (vehicle.json loaded_mixture_ratio)
PU_MR_RANGE = (0.78 * 3.6, 3.6)   # the Raptor Vacuum's commandable mixture ratios (manual, "Mixture ratio command")
S2_RESERVE = 0.02   # S2 flight performance reserve for the trans-Mars burn, as a share of its load (EarthAscent.tmi_margin)
S1_G_MAX = _tune("S1_G_MAX", 4.8)   # g: S1 throttles down late in its burn to stay under this (EarthAscent.s1_throttle)
# Parking orbit altitude (m). 170 km gave S2 no more propellant for the TMI than 200 km (runs/v8), so it stays.
PARK_ALT = _tune("PARK_ALT", 200e3)
# The chosen pitch kick times this in 6-DOF. With the plane through the real pad (launch.plan), 1.4 flew flat
# (max q 44 kPa, S1 burnout at 50 km); 1.1 gives max q 33 to 36 kPa, S1 burnout near 80 km and 77 t of S2
# propellant in the parking orbit (0.5: 72 t, 0.8: 72 to 74 t; seeds 1000 and 1005).
KICK_SCALE = _tune("KICK_SCALE", 1.1)
S1_BANDWIDTH = _tune("S1_BANDWIDTH", M.S1_BANDWIDTH)   # rad/s: the 6-DOF attitude loop through the S1 burn
# deg added to the S1 flight path angle profile, ramped in from 0.3 to 1 km/s. -3 deg stages S1 near 95 km instead
# of 100 to 104 km and gives S2 about 1.8 t more in the parking orbit (0.7 t more after the larger TMI it then
# plans), added 1.3 t on the Mars side (seeds 1000, 1008, 1043; -6 deg gave the same, -9 deg less).
FPA_OFFSET_DEG = _tune("FPA_OFFSET_DEG", -3.0)
FPA_BIAS_SCALE = _tune("FPA_BIAS_SCALE", 1.0)   # times M.S1_FPA_BIAS, the flatter path late in the S1 burn


def upper_wind_qalpha_max():
    import os
    v = os.environ.get("MARSFLIGHT_REF_UPPER_WIND_QALPHA")
    return float(v) if v else UPPER_WIND_QALPHA_MAX


def _interp(tab, x):
    if x <= tab[0][0]:
        return tab[0][1]
    for (a, fa), (b, fb) in zip(tab, tab[1:]):
        if x <= b:
            return fa + (fb - fa) * (x - a) / (b - a)
    return tab[-1][1]


class EarthAscent:
    """Pad to parking orbit and through TMI. Hands over after S2 separation."""

    def __init__(self, fl):
        self.fl = fl
        self.m = fl.model
        self.phase = "pad"
        self.plan = None
        self.ins = None
        self.t_last = None
        self.tmi = None
        self.done = False
        self.t_cut = None
        self.t_s1_out = None    # S1 burnout: coast M.STAGE_COAST before separating it
        self.t_s2_out = None    # TMI cutoff: likewise before separating S2
        self.balloon = None     # the pre-launch balloon's winds over the pad (obs "launch_site_wind")
        self.lcc_for = None     # the launch time whose upper winds were judged
        self.slips = 0          # days the launch slipped for upper winds
        self.stg = None         # staging step: coast, sep, hold
        self.lifted = False     # liftoff seen (the pitch program's clock starts there)
        self.ins_fresh = False  # the S2 insertion solution has not converged yet

    # ---------------------------------------------------------------- planning
    def make_plan(self, t_day):
        t_dep = M.T_DEP
        t_arr = M.T_ARR
        vinf_dep, _ = planet_vinf("Earth", "Mars", t_dep, t_arr)
        s_hat = unit(vinf_dep)
        p = plan(t_day, s_hat, float(np.linalg.norm(vinf_dep)), self.fl.sites["earth_launch"], park_alt=PARK_ALT,
                 coast_guess=1500.0)
        p["s_hat"] = s_hat
        p["vinf"] = float(np.linalg.norm(vinf_dep))
        self.plan = p
        log("launch_plan", t_launch=p["t_launch"], n_ins=p["n_ins"], inc=p["inc_deg"], ang=p["angle_site_to_perigee_deg"],
            coast=p["coast_s"], vinf=p["vinf"], dla=math.degrees(math.asin(s_hat[2])))
        return p

    # ---------------------------------------------------------------- helpers
    def up(self, obs):
        return unit(np.array(obs["r"]))

    def az_dir(self, obs, az_deg):
        r = np.array(obs["r"])
        up = unit(r)
        pz = pole("Earth", obs["t"])
        east = unit(np.cross(pz, up))
        north = np.cross(up, east)
        a = math.radians(az_deg)
        return unit(math.sin(a) * east + math.cos(a) * north)

    def launch_azimuth(self, obs):
        """Inertial azimuth of the target plane at the site, then corrected for Earth rotation."""
        r = np.array(obs["r"])
        up = unit(r)
        pz = pole("Earth", obs["t"])
        east = unit(np.cross(pz, up))
        north = np.cross(up, east)
        w = unit(np.cross(self.plan["n_ins"], up))
        az_in = math.atan2(float(np.dot(w, east)), float(np.dot(w, north)))
        v_orb = 7800.0
        v_rot = 7.2921159e-5 * np.linalg.norm(r) * math.cos(math.radians(obs["lat"]))
        az_rel = math.atan2(v_orb * math.sin(az_in) - v_rot, v_orb * math.cos(az_in))
        return math.degrees(az_rel)

    # ---------------------------------------------------------------- step
    def step(self, obs):
        ph = self.phase
        if ph == "pad":
            return self.pad(obs)
        if ph == "s1":
            return self.s1(obs)
        if ph == "s2":
            return self.s2(obs)
        if ph == "park":
            return self.park(obs)
        if ph == "sep":
            if self.t_s2_out is None:
                # let the Raptor Vacuum's thrust tail off before S2 separates (it would push S2 into the stack)
                self.t_s2_out = obs["t"]
                return {"dir": obs["attitude"], "next": M.STAGE_COAST}
            self.done = True
            return {"actions": ["separate:S2"], "dir": obs["attitude"], "next": 1.0}
        raise RuntimeError(f"unknown phase {ph}")

    def pad(self, obs):
        if self.plan is None:
            self.make_plan(self.fl.t0)
        tl = self.plan["t_launch"]
        if obs.get("launch_site_wind"):
            self.balloon = obs["launch_site_wind"]
        if self.lcc_for != tl and obs["t"] >= tl - M.BALLOON_LEAD_S - 30.0:
            # Upper-level winds launch commit criterion, on the balloon released about 2 h before liftoff
            self.lcc_for = tl
            go, qa = self.upper_winds_go(obs)
            if not go and self.slips < M.LAUNCH_SLIP_MAX:
                self.slips += 1
                self.balloon = None
                self.make_plan(self.fl.t0 + self.slips * 86400.0)
                log("lcc_upper_winds", go=False, qalpha=qa, slips=self.slips, t_launch=self.plan["t_launch"])
                tl = self.plan["t_launch"]
                return {"dir": self.up(obs).tolist(), "next": max(1.0, tl - M.BALLOON_LEAD_S - 10.0 - obs["t"])}
            log("lcc_upper_winds", go=go, qalpha=qa, slips=self.slips)
        if obs["t"] < tl - 0.5:
            # hold the body axis on the local vertical while waiting (the attitude snaps on long waits)
            wait = tl - obs["t"]
            if self.lcc_for != tl:
                wait -= M.BALLOON_LEAD_S       # wake up for the balloon first
            return {"dir": self.up(obs).tolist(), "next": wait - 10.0 if wait > 20.0 else max(wait, 1.0)}
        return self.ignite(obs)

    def ignite(self, obs):
        """Command S1's ignition now. The plane is planned again through the pad's navigated position at this
        moment, so a launch that the weather held for hours or days (the simulator moves t on to the go time)
        still flies into a plane that contains the departure asymptote; the trans-Mars burn plans its own timing."""
        from .launch import plane_at
        p = self.plan
        late = obs["t"] - p["t_launch"]
        p.update(plane_at(obs["t"], np.array(obs["r"], float), p["s_hat"], p["vinf"], park_alt=PARK_ALT,
                          coast=p.get("coast_s", 1500.0)))
        log("launch_replan", t=obs["t"], late_s=late, inc=p["inc_deg"], ang=p["angle_site_to_perigee_deg"])
        self.phase = "s1"
        self.s2_load = prop_left(obs, "S2")   # S2's load as the pad gauges read it
        self.az = self.launch_azimuth(obs)
        self.t_liftoff = obs["t"] + 1.0
        # In 6-DOF the attitude lags the kick and the stack's lift lofts the turn, so the kick is larger.
        self.kick = self.choose_kick(obs) * (KICK_SCALE if self.six() else 1.0)
        log("ignite_s1", t=obs["t"], az=self.az, kick=self.kick, pred=self.kick_pred)
        # In 6-DOF a stiff attitude loop holds the commanded attitude against the unstable stack's aerodynamic
        # moment (the 3-DOF simulator ignores this key).
        return {"engines": {"S1": {"throttle": 1.0}}, "dir": self.up(obs).tolist(), "next": 2.0,
                "attitude": {"bandwidth": S1_BANDWIDTH}}

    def choose_kick(self, obs):
        from .s1sim import choose_kick
        m = self.m
        en, n = m.engine_of("S1")
        F = en["thrust_vac_kN"] * 1e3 * n
        Ae = math.pi * en["exit_diameter_m"] ** 2 / 4 * n
        md = en["mdot_kg_s"] * n
        m0 = stack_mass(obs, m)
        mb = m0 - prop_left(obs, "S1")
        dmax = max(e["diameter_m"] for e in m.elements.values() if e["kind"] != "parachute")
        S = math.pi * dmax ** 2 / 4
        pz = pole("Earth", obs["t"])
        kick, pred = choose_kick(np.array(obs["r"]), pz, self.az_dir(obs, self.az), F, Ae, md, m0, mb, S,
                                 M.T_VERT, M.T_KICK, q_target=M.Q_TARGET, fpa_min=M.FPA_MIN)
        self.kick_pred = pred
        return kick

    def balloon_wind(self, alt, up, east):
        """The balloon's wind (m/s, in the frame of up and east) at a height; above its top, its top wind."""
        b = self.balloon
        if not b:
            return np.zeros(3)
        we = float(np.interp(alt, b["alt_m"], b["east_mps"]))
        wn = float(np.interp(alt, b["alt_m"], b["north_mps"]))
        return we * east + wn * np.cross(up, east)

    def upper_winds_go(self, obs):
        """Fly the planned S1 ascent in the onboard simulator through the balloon's winds, with the load relief the
        flight will use, and compare the largest q-alpha left with M.UPPER_WIND_QALPHA_MAX (Pa deg). Saturn V and
        the Shuttle judged their launch-day winds the same way: measured winds through the planned trajectory
        against the structure's limit (Shuttle DOLILU; MSFC-MAN-503 upper wind limits)."""
        if not self.balloon:
            return True, None
        from .s1sim import simulate
        m = self.m
        pz = pole("Earth", obs["t"])
        # the pad where it will be at liftoff (the Earth turns under the planned plane in the meantime)
        tl = self.plan["t_launch"]
        o = dict(obs, t=tl, r=rodrigues(np.array(obs["r"], float), pz, 7.2921159e-5 * (tl - obs["t"])))
        az = self.launch_azimuth(o)
        en, n = m.engine_of("S1")
        F = en["thrust_vac_kN"] * 1e3 * n
        Ae = math.pi * en["exit_diameter_m"] ** 2 / 4 * n
        md = en["mdot_kg_s"] * n
        m0 = stack_mass(obs, m)
        mb = m0 - prop_left(obs, "S1")
        dmax = max(e["diameter_m"] for e in m.elements.values() if e["kind"] != "parachute")
        S = math.pi * dmax ** 2 / 4
        from .s1sim import choose_kick
        kick, _ = choose_kick(o["r"], pz, self.az_dir(o, az), F, Ae, md, m0, mb, S, M.T_VERT, M.T_KICK,
                              q_target=M.Q_TARGET, fpa_min=M.FPA_MIN)
        kick *= KICK_SCALE if self.six() else 1.0
        res = simulate(o["r"], pz, self.az_dir(o, az), F, Ae, md, m0, mb, S, kick, M.T_VERT, M.T_KICK, record=True)
        worst = 0.0
        for alt, q, vrel, r in res["path"]:
            V = float(np.linalg.norm(vrel))
            if V < 50.0:
                continue
            up = unit(r)
            east = unit(np.cross(pz, up))
            va = vrel - self.balloon_wind(alt, up, east)
            al = math.degrees(angle(vrel, va))
            f = min(1.0, max(0.0, (q - M.WIND_BIAS_Q[0]) / (M.WIND_BIAS_Q[1] - M.WIND_BIAS_Q[0])))
            worst = max(worst, q * max(0.0, al - f * M.WIND_BIAS_MAX_DEG))
        return worst <= upper_wind_qalpha_max(), round(worst)

    def load_relief(self, obs, d, vr):
        """Day-of-launch wind biasing with load relief: turn the steering towards the airflow the pre-launch balloon
        predicts, by at most WIND_BIAS_MAX_DEG, fading in from WIND_BIAS_Q[0] to [1] of dynamic pressure. A strong jet puts the airflow many degrees off the Earth-relative velocity, and flying off the airflow
        bends the stack and buckles S1 (7 of 48 worlds).

        The simulator gives no balloon, so the airflow comes from the vehicle's own air data: obs "airspeed_vector",
        the navigated velocity less the wind (manual, "Air data"). That is load relief as the Shuttle and the Saturn
        V's later flights flew it: near max q the stack steers onto the measured airflow, by up to RELIEF_MAX_DEG."""
        q = obs.get("q") or 0.0
        if self.balloon:
            f = min(1.0, max(0.0, (q - M.WIND_BIAS_Q[0]) / (M.WIND_BIAS_Q[1] - M.WIND_BIAS_Q[0])))
            lim = M.WIND_BIAS_MAX_DEG
            up = self.up(obs)
            east = unit(np.cross(pole("Earth", obs["t"]), up))
            va = vr - self.balloon_wind(float(obs.get("alt") or 0.0), up, east)
        elif obs.get("airspeed_vector") is not None:
            # Turn the steering by the wind's angle only (the rotation that takes the Earth-relative velocity onto
            # the measured airflow), so the small angle the flight path tracking asks for (S1_AOA_MAX) is kept.
            # Steering onto the airflow itself cancelled that tracking through max q and flew S1 flat (burnout at
            # 51 km and 6 kPa in seed 1009).
            f = min(1.0, max(0.0, (q - RELIEF_Q[0]) / (RELIEF_Q[1] - RELIEF_Q[0])))
            va = np.array(obs["airspeed_vector"], float)
            if np.linalg.norm(va) < 1.0 or np.linalg.norm(vr) < 1.0 or f <= 0.0:
                return d
            u0, u1 = unit(vr), unit(va)
            ang = angle(u0, u1)
            ax = np.cross(u0, u1)
            if ang < 1e-6 or np.linalg.norm(ax) < 1e-12:
                return d
            return unit(rodrigues(d, unit(ax), min(ang, math.radians(RELIEF_MAX_DEG)) * f))
        else:
            return d
        if f <= 0.0 or np.linalg.norm(va) < 1.0:
            return d
        va = unit(va)
        ang = angle(d, va)
        if ang < 1e-6:
            return d
        turn_by = min(ang, f * math.radians(lim))
        perp = unit(va - float(np.dot(va, d)) * d)
        return unit(math.cos(turn_by) * d + math.sin(turn_by) * perp)

    def six(self):
        return self.fl.info.get("dynamics") == "6dof"

    def vrel(self, obs):
        r = np.array(obs["r"])
        v = np.array(obs["v"])
        w = 7.2921159e-5 * pole("Earth", obs["t"])
        return v - np.cross(w, r)

    def s1(self, obs):
        if obs.get("mode") == "pad" and any("launch held" in str(e) for e in obs.get("command_errors") or []):
            # weather scrub: t is now the next attempt; command the ignition again (manual, "Launch weather")
            log("launch_hold", t=obs["t"], errors=obs.get("command_errors"), events=obs.get("events"))
            return self.ignite(obs)
        acts = []
        evs = obs["events"]
        if not self.lifted:
            # the clock of the pitch program starts at liftoff itself: with ENGINE_TRANSIENTS the thrust builds for
            # about 3.8 s after the start command, not the 1 s this assumed before
            if obs.get("mode") == "flight" or any(e.startswith("liftoff") for e in evs):
                self.lifted = True
                self.t_liftoff = obs["t"]
                log("liftoff_seen", t=obs["t"], alt=obs.get("alt"))
            else:
                return {"engines": {"S1": {"throttle": 1.0}}, "dir": self.up(obs).tolist(), "next": 0.5}
        t = obs["t"] - self.t_liftoff
        if self.t_s1_out is not None or any(e.startswith("burnout:S1") for e in evs) or prop_left(obs, "S1") <= 1.0:
            return self.staging(obs)
        up = self.up(obs)
        k = math.radians(self.kick)
        kick_dir = unit(math.cos(k) * up + math.sin(k) * self.az_dir(obs, self.az))
        if t < M.T_VERT:
            d = up
        elif t < M.T_VERT + M.T_KICK or float(np.linalg.norm(self.vrel(obs))) < S1_VFOLLOW_MIN:
            # hold the kick until the stack is fast enough that its velocity is a steady thing to follow; a gravity
            # turn that follows a 40 m/s velocity runs away (seed 1026: pitched to 28 deg in 10 s and broke up)
            d = kick_dir
        else:
            vr = self.vrel(obs)
            d = unit(vr)
            # never pitch below the kick attitude early on, so the turn starts cleanly
            if angle(d, up) < k and t < M.T_VERT + M.T_KICK + 20:
                d = kick_dir
            elif t >= M.T_VERT + M.T_KICK + (M.FPA_FB_DELAY_6DOF if self.six() else 20):
                d = self.fpa_steer(obs, vr, up)
            # the plane correction first, so load relief has the last word on the angle of attack
            d = self.yaw_correct(obs, d)
            d = self.load_relief(obs, d, vr)
        acts += self.fairing_check(obs)
        return {"engines": {"S1": {"throttle": self.s1_throttle(obs)}}, "dir": d.tolist(),
                "next": M.S1_DT_6DOF if self.six() else 2.0, "actions": acts}

    def s1_throttle(self, obs):
        """Throttle S1 down late in its burn so the stack's axial load stays under S1_G_MAX, as the Saturn V cut its
        S-IC centre engine early to hold 4 g. At full thrust S1 burns out above 5.8 g, which with what is left of
        the dynamic pressure buckled S1 in 4 of the 48 baseline worlds (axial load factor 4.7 to 5.9 g)."""
        F = self.m.thrust_vac("S1")
        m = stack_mass(obs, self.m)
        thr = S1_G_MAX * G0 * m / F
        return float(max(self.m.min_throttle("S1"), min(1.0, thr)))

    def staging(self, obs):
        """S1 to S2 staging, flown as the Saturn V staged its S-IC (AS-506, MPR-SAT-FE-69-9 table 2-2): cutoff, a
        coast while S1's thrust tails off with the S2 propellant settled by ullage thrust, separation, a short coast
        that opens the gap, then S2 ignition, all on one held attitude. Only once S1 is clear (`separation_clear`)
        or S2_HOLD_S after ignition does S2 turn to its insertion steering. With ENGINE_TRANSIENTS, separating at
        the end of a 2 s coast and lighting S2 at once let the tail-off and S2's first turn bring S1 back into the
        vehicle 2.6 to 5 s later in 4 of seeds 1000 to 1011."""
        evs = obs["events"]
        acts = self.fairing_check(obs)
        if self.t_s1_out is None:
            self.t_s1_out = obs["t"]
            self.stg = "coast"
            self.hold_dir = list(obs["attitude"])
            log("s1_burnout", t=obs["t"], alt=obs["alt"], q=obs.get("q"))
            return {"dir": self.hold_dir, "next": S1_TAILOFF_COAST_S, "settle": True, "actions": acts}
        if self.stg == "coast":
            self.stg = "sep"
            self.t_sep = obs["t"]
            log("s1_separate", t=obs["t"], alt=obs["alt"], q=obs.get("q"))
            return {"dir": self.hold_dir, "next": S1_SEP_COAST_S, "settle": True, "actions": acts + ["separate:S1"]}
        if self.stg == "sep":
            self.stg = "hold"
            self.t_s2_ign = obs["t"]
            self.s1_clear = any(e.startswith("separation_clear:S1") for e in evs)
            log("s2_ignite", t=obs["t"], alt=obs["alt"], q=obs.get("q"), clear=self.s1_clear)
            return {"engines": {"S2": {"throttle": 1.0}}, "dir": self.hold_dir, "next": 2.0, "settle": True,
                    "actions": acts, "attitude": {"bandwidth": M.BANDWIDTH}}
        # hold: S2 burns on the held attitude until S1 is clear
        self.s1_clear = self.s1_clear or any(e.startswith("separation_clear:S1") for e in evs)
        if not self.s1_clear and obs["t"] < self.t_s2_ign + S2_HOLD_S:
            return {"engines": {"S2": {"throttle": 1.0}}, "dir": self.hold_dir, "next": 1.0, "actions": acts}
        self.phase = "s2"
        self.ins = Insertion("Earth", 6378137.0 + PARK_ALT, 0.0, math.sqrt(MU["Earth"] / (6378137.0 + PARK_ALT)),
                             self.plan["n_ins"], h=4.0)
        self.ins_fresh = True
        log("staging", t=obs["t"], alt=obs["alt"], v=float(np.linalg.norm(obs["v"])), clear=self.s1_clear)
        return self.s2(obs)

    def fpa_steer(self, obs, vr, up):
        """Tilt the thrust off the Earth-relative velocity, in its vertical plane, so the flight path angle
        follows the nominal profile against speed (see M.S1_FPA_REF)."""
        V = float(np.linalg.norm(vr))
        vh = vr / V
        if V < M.S1_FPA_REF[0][0]:
            return vh
        gam = math.degrees(math.asin(max(-1.0, min(1.0, float(np.dot(vh, up))))))
        g_ref = _interp(M.S1_FPA_REF, V) + FPA_BIAS_SCALE * _interp(M.S1_FPA_BIAS, V) \
            + FPA_OFFSET_DEG * min(1.0, max(0.0, (V - 300.0) / 700.0))
        q = obs.get("q") or 0.0
        amax = next(a for qq, a in M.S1_AOA_MAX if q >= qq)
        a = math.radians(max(-amax, min(amax, M.S1_FPA_GAIN * (g_ref - gam))))
        n = up - float(np.dot(up, vh)) * vh
        if np.linalg.norm(n) < 1e-6:
            return vh
        return unit(math.cos(a) * vh + math.sin(a) * unit(n))

    def yaw_correct(self, obs, d):
        """Small out-of-plane thrust component that drives the inertial position and velocity back
        into the target plane (the Earth-relative gravity turn drifts out of it through Coriolis)."""
        n = self.plan["n_ins"]
        r = np.array(obs["r"])
        v = np.array(obs["v"])
        ev, er = float(np.dot(v, n)), float(np.dot(r, n))
        tau = M.YAW_TAU
        a_n = -(ev + er / tau) / tau
        a_T = max(accel_meas(obs), 5.0)
        q = obs.get("q") or 0.0
        lim = math.radians(M.YAW_LIM_HIGHQ if q > 10e3 else M.YAW_LIM)
        dl = max(-lim, min(lim, math.asin(max(-1.0, min(1.0, a_n / a_T)))))
        dn = d - np.dot(d, n) * n
        dn = unit(dn)
        return unit(math.cos(dl) * dn + math.sin(dl) * n)

    def fairing_check(self, obs):
        if "FAIR" in present(obs) and obs.get("alt", 0) > 100e3:
            q = obs.get("q")
            V = obs.get("airspeed", 0.0)
            if q is None or q * V < 900.0:
                log("fairing_sep", t=obs["t"], alt=obs["alt"], fmh=(q or 0) * V)
                return ["separate:FAIR"]
        return []

    def s2_thrust(self, obs, throttle=1.0):
        F = self.m.thrust_vac("S2", throttle=throttle)
        md = self.m.mdot("S2", throttle=throttle)
        return F, md

    def s2(self, obs, extra_actions=(), first=False):
        acts = list(extra_actions)
        acts += self.fairing_check(obs) if not first else []
        r = np.array(obs["r"])
        v = np.array(obs["v"])
        m = stack_mass(obs, self.m)
        if first:
            m -= self.m.dry("S1") + prop_left(obs, "S1")
        if "FAIR" in acts or ("FAIR" in present(obs) and "separate:FAIR" in acts):
            m -= self.m.dry("FAIR")
        F, md = self.s2_thrust(obs)
        pz = pole("Earth", obs["t"])
        dt_arc = 4.0
        shift = 0.0 if self.t_last is None else obs["t"] - self.t_last
        self.t_last = obs["t"]
        if first:
            # thrust starts 1 s after ignition
            r = r + v * 1.0
        x, g = self.ins.solve(r, v, m, F, md, iters=10 if (first or self.ins_fresh) else 2, shift=shift,
                              isp_g0=self.m.isp("S2") * G0, pz=pz)
        self.ins_fresh = False
        T = x[4]
        log("s2", t=obs["t"], alt=obs.get("alt"), T=T, res=g.tolist(), x=x.tolist(), m=m)
        if T < 1.5 * dt_arc and not first:
            # final arc: burn exactly the remaining time, then cut off
            d = self.ins.direction(r, T / 2)
            self.phase = "park"
            self.t_cut = obs["t"] + T
            return {"engines": {"S2": {"throttle": 1.0}}, "dir": d.tolist(), "next": max(T, 0.05), "actions": acts}
        d = self.ins.direction(r, dt_arc / 2 + (1.0 if first else 0.0))
        return {"engines": {"S2": {"throttle": 1.0}}, "dir": d.tolist(), "next": dt_arc, "actions": acts}

    # ---------------------------------------------------------------- parking orbit and TMI
    def park(self, obs):
        if self.tmi is None:
            from .departure import Departure
            from . import target as TG
            r = np.array(obs["r"])
            v = np.array(obs["v"])
            rp, ra, e, en = orbit_rp_ra(r, v, MU["Earth"])
            log("orbit", t=obs["t"], rp_alt=rp - 6378137.0, ra_alt=ra - 6378137.0, e=e)
            s_hat, vinf = self.tmi_target(obs["t"] + 1800.0)

            def aim_fn(enc):
                from .edl import EDL
                if not hasattr(self, "_edl"):
                    self._edl = EDL(self.fl)
                a = TG.aim_point_edl(self._edl, enc["S"], enc["vinf"], M.T_ARR, M.JEZERO, M.EI_FPA)
                self.fl.mars_aim = a
                return a
            self.tmi = Departure(self.fl, "Earth", "S2", s_hat * vinf, aim_fn, "Mars", M.T_ARR, M.TMI_G_MAX, True,
                                 M.T_ARR + 60 * 86400.0)
        cmd = self.tmi.step(obs)
        self.propellant_utilization(obs, cmd)
        if self.tmi.state == "ready" and not getattr(self, "tmi_margin_logged", False):
            self.tmi_margin_logged = True
            self.tmi_margin(obs)
        acts = self.fairing_check(obs)
        if acts:
            cmd.setdefault("actions", []).extend(acts)
        if self.tmi.done:
            self.phase = "sep"
        return cmd

    def propellant_utilization(self, obs, cmd):
        """Propellant utilization in the TMI, as the J-2's PU valve did on the S-II and S-IVB: run the Raptor Vacuum at
        the oxidizer to fuel ratio of what is left in the tanks (gauge readings, less each tank's 0.15% low level
        floor), so both tanks reach the floor together. S2 is loaded a little fuel rich (loaded_mixture_ratio 3.55
        against the engine's 3.6), so the ratio wanted stays inside the range the engine can run (78% to 100% of its
        design ratio). At the design ratio 8 of the 10 low level cutoffs in the 48 worlds of runs/v9 were the fuel
        tank running out with oxidizer left, after a burn that used no more than its plan."""
        eng = (cmd or {}).get("engines") or {}
        item = eng.get("S2")
        if not isinstance(item, dict) or float(item.get("throttle", 0.0) or 0.0) <= 0.0:
            return
        el = next((e for e in obs.get("elements", []) if e.get("id") == "S2"), None)
        if el is None or el.get("fuel_kg") is None or el.get("ox_kg") is None:
            return
        load = getattr(self, "s2_load", None) or 300000.0
        f_load, o_load = load / (1.0 + S2_LOADED_MR), load * S2_LOADED_MR / (1.0 + S2_LOADED_MR)
        fuel = float(el["fuel_kg"]) - 0.0015 * f_load
        ox = float(el["ox_kg"]) - 0.0015 * o_load
        if fuel <= 1.0 or ox <= 1.0:
            return
        lo, hi = PU_MR_RANGE
        item["mixture_ratio"] = float(min(hi, max(lo, ox / fuel)))
        if getattr(self, "_pu_logged", None) != self.tmi.state:
            self._pu_logged = self.tmi.state
            log("pu", t=obs["t"], fuel=el["fuel_kg"], ox=el["ox_kg"], mr=item["mixture_ratio"])

    def tmi_margin(self, obs):
        """S2 propellant margin for the trans-Mars burn: what the gauges read, less their 1-sigma error times 3
        (capacitance gauge: 0.05% + 0.25% x fill of the load, manual "Propellant loading, gauging and the low level
        cutoff"), less the low level cutoff's 0.15%, against the planned burn plus a 2% flight performance reserve
        of the load. A shortfall is logged: the burn still flies (it ends on velocity, and the low level cutoff
        stops S2 before a pump runs dry), and the Mars approach corrections take up an underburn."""
        load = getattr(self, "s2_load", None) or 300000.0
        left = prop_left(obs, "S2")
        sigma = (0.0005 + 0.0025 * left / max(load, 1.0)) * load
        usable = left - 3.0 * sigma - 0.0015 * load
        need = float(getattr(self.tmi, "used", 0.0) or 0.0) + S2_RESERVE * load
        log("tmi_margin", left=left, usable=usable, burn=getattr(self.tmi, "used", None), reserve=S2_RESERVE * load,
            margin=usable - need, ok=usable >= need)

    def tmi_target(self, t_burn_mid):
        vinf_dep, _ = planet_vinf("Earth", "Mars", t_burn_mid, M.T_ARR)
        return unit(vinf_dep), float(np.linalg.norm(vinf_dep))
