"""Mars surface stay, Mars ascent to a parking orbit and trans-Earth injection.

Surface: wait for the samples and for the launch time when the plane through Jezero also contains the
trans-Earth departure asymptote (due east, inclination equal to the site latitude). Ascent: vertical
rise, then closed-loop two-stage shooting (MAV1 then MAV2) to a 200 km circular orbit in that plane.
TEI: the same departure module as the TMI, aimed at the Earth B-plane point for the Utah landing.
"""
from __future__ import annotations

import math

import numpy as np

from . import mission as M
from . import target as TG
from .departure import Departure
from .interplanetary import planet_vinf
from .launch import plan
from .log import log
from .powered import StagedInsertion
from .util import G0, MARS_R, MU, OMEGA, SOL, grav_j2, orbit_rp_ra, pole, unit
from .vehicle_model import prop_left, present, stack_mass


def _mav_numbers(fl):
    m = fl.model
    F1, md1 = m.thrust_vac("MAV1"), m.mdot("MAV1")
    F2, md2 = m.thrust_vac("MAV2"), m.mdot("MAV2")
    d = {e["id"]: e for e in fl.info["vehicle"]["elements"]}
    def wet(k):
        e = d[k]
        return e["dry_mass_kg"] + sum(e.get("loaded_kg", {}).values()) + e.get("rcs_prop_kg", 0.0)
    p1 = sum(d["MAV1"]["loaded_kg"].values())
    p2 = sum(d["MAV2"]["loaded_kg"].values())
    m0 = wet("MAV1") + wet("MAV2") + wet("CAP") + m.sample_kg
    return F1, md1, F2, md2, p1, p2, m0, d["MAV1"]["dry_mass_kg"]


def _aoa_deg(obs, d=None):
    """Angle (deg) between the body axis (or d) and the navigated airflow, or None outside the air."""
    va = obs.get("airspeed_vector")
    if va is None or not obs.get("airspeed"):
        return None
    ax = np.asarray(obs["attitude"] if d is None else d, float)
    return math.degrees(math.acos(max(-1.0, min(1.0, float(np.dot(unit(ax), unit(np.asarray(va, float))))))))


def _limit_aoa(obs, d):
    """Keep the commanded axis within the allowed angle of the navigated airflow: q times the angle of attack at most
    M.MAV_QALPHA_MAX (Pa deg) and never above M.MAV_AOA_MAX. The bending from q alpha is what buckles the MAV
    tanks; the closed-loop insertion makes up for the steering it loses in the air on its next solutions."""
    va = obs.get("airspeed_vector")
    q = float(obs.get("q") or 0.0)
    if va is None or q <= M.MAV_Q_LIMIT_ON or not obs.get("airspeed"):
        # at low speed the airflow direction is mostly the wind's, and the load is small: steering towards it only
        # pulls the MAV out of its plane (seed 1006: 260 km out of plane at insertion)
        return d
    a = unit(np.asarray(va, float))
    d = unit(np.asarray(d, float))
    lim = math.radians(min(M.MAV_AOA_MAX, M.MAV_QALPHA_MAX / q))
    ang = math.acos(max(-1.0, min(1.0, float(np.dot(a, d)))))
    if ang <= lim:
        return d
    perp = d - float(np.dot(a, d)) * a
    if float(np.linalg.norm(perp)) < 1e-9:
        return a
    return unit(math.cos(lim) * a + math.sin(lim) * unit(perp))


def sim_mav1(r0, v0, pz, w, kick_deg, F1, md1, m0, t_burn, az_dir, t_vert, t_kick, h=1.0):
    """Gravity turn of the first stage (vacuum, point mass + J2 not needed for planning)."""
    r = np.array(r0, float)
    v = np.array(v0, float)
    m = m0
    t = 0.0
    up0 = unit(r)
    k = math.radians(kick_deg)
    mu = MU["Mars"]
    while t < t_burn - 1e-9:
        dt = min(h, t_burn - t)
        up = unit(r)
        if t < t_vert:
            d = up
        elif t < t_vert + t_kick:
            d = unit(math.cos(k) * up + math.sin(k) * az_dir)
        else:
            vr = v - np.cross(w, r)
            d = unit(vr)
        a = -mu * r / np.linalg.norm(r) ** 3 + F1 / m * d
        v = v + a * dt
        r = r + v * dt
        m -= md1 * dt
        t += dt
    return r, v, m


def choose_mav_kick(fl, obs, t_launch, n_ins):
    """Pick the first-stage kick that leaves the most MAV2 propellant after a closed-loop insertion."""
    import time as _time
    from .util import site_inertial
    t_w = _time.time()
    F1, md1, F2, md2, p1, p2, m0, dry1 = _mav_numbers(fl)
    lat, lon = M.JEZERO
    s = obs.get("surface") or {}
    lat, lon = s.get("lat", lat), s.get("lon", lon)
    r0 = site_inertial("Mars", lat, lon, -2550.0 + 1.0, t_launch + 1.0)
    pz = pole("Mars", t_launch)
    w = OMEGA["Mars"] * pz
    v0 = np.cross(w, r0)
    up = unit(r0)
    az_dir = unit(np.cross(n_ins, up))
    rt = MARS_R + M.MARS_PARK_ALT
    best = None
    res = {}
    for kick in (1.0, 2.0, 3.0, 4.5, 6.0, 8.0, 10.0):
        r1, v1, m1 = sim_mav1(r0, v0, pz, w, kick, F1, md1, m0, p1 / md1, az_dir, M.MAV_T_VERT, 6.0)
        m2 = m1 - dry1
        ins = StagedInsertion("Mars", rt, 0.0, math.sqrt(MU["Mars"] / rt), n_ins, h=4.0)
        x, g = ins.solve(r1 + v1 * 1.0, v1, m2, F2, md2, iters=25, isp_g0=fl.model.isp("MAV2") * G0, pz=pz)
        used = x[4] * md2
        ok = float(np.max(np.abs(g))) < 1.0
        alt1 = np.linalg.norm(r1) - MARS_R
        res[kick] = (round(used), ok, round(alt1 / 1e3, 1))
        if ok and (best is None or used < best[1]):
            best = (kick, used)
    log("mav_kick", res=res, best=best, dt=_time.time() - t_w)
    return best[0] if best else 4.0


class MarsSurface:
    def __init__(self, fl):
        self.fl = fl
        self.done = False
        self.plan = None

    def make_plan(self):
        vd, _ = planet_vinf("Mars", "Earth", M.T_MDEP, M.T_EARR)
        s_hat = unit(vd)
        site = {"lat": M.JEZERO[0], "lon": M.JEZERO[1], "alt": -2550.0}
        p = plan(M.T_MDEP - 0.75 * SOL, s_hat, float(np.linalg.norm(vd)), site, park_alt=M.MARS_PARK_ALT, body="Mars",
                 span=1.5 * SOL, coast_guess=2500.0, downrange_deg=M.MAV_DOWNRANGE_DEG)
        p["s_hat"], p["vinf"] = s_hat, float(np.linalg.norm(vd))
        self.plan = p
        self.fl.mav_plan = p
        log("mav_plan", t_launch=p["t_launch"], inc=p["inc_deg"], coast=p["coast_s"], ang=p["angle_site_to_perigee_deg"])

    def step(self, obs):
        if self.plan is None:
            self.make_plan()
        up = unit(np.array(obs["r"]))
        tl = self.plan["t_launch"]
        if obs["t"] < tl - 0.5:
            wait = tl - obs["t"]
            if not obs.get("samples_loaded") and wait < 2 * 86400.0:
                log("no_samples_yet", t=obs["t"])
            nxt = min(wait - 10.0 if wait > 20.0 else wait, 29.0 * 86400.0)
            return {"dir": up.tolist(), "next": max(nxt, 1.0)}
        self.done = True
        return None

    def next_handler(self):
        return MarsAscent(self.fl, self.plan)


class DeadReckon:
    """The MAV's own inertial navigation from the surface: the launch site's state at liftoff, then gravity (point
    mass and J2) plus the accelerometers' sensed_accel, linear between two calls (it is the specific force at each
    arc's end). The filter's state in obs is used again once it reports a fresh fix after liftoff with a small
    sigma. Why: after a long surface stay the filter's state in obs can be far off (nav_sigma of hundreds of km
    with nav_update.t at the Mars touchdown, 458 days old), and steering on it flies the MAV into the ground."""

    def __init__(self, obs):
        self.t = float(obs["t"])
        self.r = np.array(obs["r"], float)          # on the ground obs gives the site's own state
        self.v = np.array(obs["v"], float)
        self.a = None
        self.t_lift = self.t
        self.trusting = False
        self.powered = True       # the liftoff arc

    def _acc(self, r, a_ng, pz):
        return grav_j2("Mars", r, pz) + a_ng

    def advance(self, obs):
        t1 = float(obs["t"])
        a1 = np.array(obs.get("sensed_accel") or [0.0, 0.0, 0.0], float)
        if self.a is None and obs.get("mode") == "flight" and t1 > self.t:
            # the liftoff arc: the vehicle stands on the ground (turning with Mars) while its engines come up to
            # thrust, about M.MAV_HOLD_S, and then flies on the thrust it ends the arc with
            hold = min(M.MAV_HOLD_S, t1 - self.t)
            ang = OMEGA["Mars"] * hold
            pz = pole("Mars", self.t)
            self.r = self.r * math.cos(ang) + np.cross(pz, self.r) * math.sin(ang) + pz * float(np.dot(pz, self.r)) * (1 - math.cos(ang))
            self.v = np.cross(OMEGA["Mars"] * pz, self.r)
            self.t += hold
            self.a = a1
        if self.a is None:
            self.t, self.a = t1, None if obs.get("mode") != "flight" else a1
            return
        dt_all = t1 - self.t
        if dt_all > 0:
            a0 = self.a
            if self.powered:
                if float(np.linalg.norm(a0)) < 0.05:
                    # an ignition from a coast: about 1 s to thrust, then the thrust the arc ends with
                    def prof(tau):
                        return a1 if tau >= 1.0 else a1 * tau
                else:
                    def prof(tau):
                        return a0 + (a1 - a0) * (tau / dt_all)
                grid = np.linspace(0.0, dt_all, max(2, int(math.ceil(dt_all / 0.25)) + 1))
            else:
                # a coast: the thrust tails off (time constant 0.21 s), plus the drag the arc ends with
                tail = a0 if float(np.linalg.norm(a0)) > 0.05 else np.zeros(3)

                def prof(tau):
                    return tail * math.exp(-tau / 0.21) + a1 * (tau / dt_all)
                fine = np.linspace(0.0, min(2.0, dt_all), 41)
                rest = np.linspace(min(2.0, dt_all), dt_all, max(2, int(math.ceil((dt_all - 2.0) / 5.0)) + 1)) \
                    if dt_all > 2.0 else np.array([])
                grid = np.concatenate([fine, rest[1:]])
            pz = pole("Mars", self.t)
            r, v = self.r, self.v
            for ta, tb in zip(grid[:-1], grid[1:]):
                h = tb - ta
                tm = ta + h / 2
                k1v = self._acc(r, prof(ta), pz)
                k1r = v
                k2v = self._acc(r + k1r * h / 2, prof(tm), pz)
                k2r = v + k1v * h / 2
                k3v = self._acc(r + k2r * h / 2, prof(tm), pz)
                k3r = v + k2v * h / 2
                k4v = self._acc(r + k3r * h, prof(tb), pz)
                k4r = v + k3v * h
                r = r + h / 6 * (k1r + 2 * k2r + 2 * k3r + k4r)
                v = v + h / 6 * (k1v + 2 * k2v + 2 * k3v + k4v)
            self.r, self.v = r, v
        self.t, self.a = t1, a1

    def note(self, cmd):
        """Whether the arc the command starts is powered (any engine commanded above zero throttle)."""
        eng = (cmd or {}).get("engines") or {}
        on = False
        for c in eng.values():
            for item in ([c] if isinstance(c, dict) else c if isinstance(c, list) else []):
                if isinstance(item, dict) and float(item.get("throttle", 0.0) or 0.0) > 0.0:
                    on = True
        self.powered = on

    def apply(self, obs):
        """obs with r and v from this inertial state while the filter's own state is not to be trusted."""
        self.advance(obs)
        sig = (obs.get("nav_sigma") or {}).get("position_m")
        if sig is not None and sig < 1e4:
            log("mav_dr_check", t=obs["t"], sigma=sig, dr_minus_nav_m=float(np.linalg.norm(self.r - np.array(obs["r"]))),
                dv_mps=float(np.linalg.norm(self.v - np.array(obs["v"]))))
        if not self.trusting and sig is not None and sig < M.MAV_NAV_TRUST_M:
            self.trusting = True
            log("mav_nav_trust", t=obs["t"], sigma=sig, dr_minus_nav_m=float(np.linalg.norm(self.r - np.array(obs["r"]))))
        if self.trusting:
            return obs
        o = dict(obs)
        o["r"], o["v"] = self.r.tolist(), self.v.tolist()
        h = float(np.linalg.norm(self.r)) - MARS_R
        o["alt"] = h
        if "q" in obs:
            # obs q comes from the filter's state and the onboard atmosphere: with that state far off it read
            # 1.9 kPa ten seconds after liftoff (truly 4 Pa). Use this state and the nominal atmosphere instead.
            from .marsatm import density
            w = OMEGA["Mars"] * pole("Mars", self.t)
            va = self.v - np.cross(w, self.r)
            o["q"] = 0.5 * density(h) * float(np.dot(va, va))
            # obs airspeed_vector is the filter's velocity less the wind: as far off as its state
            o["airspeed_vector"], o["airspeed"] = va.tolist(), float(np.linalg.norm(va))
        return o


class MarsAscent:
    def __init__(self, fl, p):
        self.fl = fl
        self.m = fl.model
        self.plan = p
        self.phase = "liftoff"
        self.done = False
        self.ins = None
        self.t_last = None
        self.tei = None
        self.immediate = True
        self.t_mav1_out = None   # MAV1 burnout: coast M.STAGE_COAST before separating it
        self.dr = None           # DeadReckon from liftoff

    def step(self, obs):
        if self.dr is None:
            self.dr = DeadReckon(obs)
        nav = obs.get("nav_sigma") or {}
        obs = self.dr.apply(obs)
        if self.phase in ("vertical", "ascent", "park"):
            mav1 = next((e for e in obs.get("elements") or [] if e.get("id") == "MAV1"), {})
            log("mav_nav", t=obs["t"], phase=self.phase, sigma=nav.get("position_m"), trusting=self.dr.trusting,
                sensed=float(np.linalg.norm(obs.get("sensed_accel") or [0, 0, 0])), mav1_prop=prop_left(obs, "MAV1"),
                eng=mav1.get("engines"), events=obs.get("events"), errors=obs.get("command_errors"),
                faults=obs.get("faults"), avionics=mav1.get("avionics"))
        cmd = self._step(obs)
        self.dr.note(cmd)
        return cmd

    def _step(self, obs):
        ph = self.phase
        if ph == "liftoff":
            return self.liftoff(obs)
        if ph == "vertical":
            return self.vertical(obs)
        if ph == "ascent":
            return self.ascent(obs)
        if ph == "park":
            return self.park(obs)
        if ph == "coast":
            self.done = True
            return {"dir": obs["attitude"], "next": 60.0}
        raise RuntimeError(ph)

    def liftoff(self, obs):
        up = unit(np.array(obs["r"]))
        acts = [f"separate:{e}" for e in M.MARS_STAY if e in present(obs)]
        self.phase = "vertical"
        self.t_lift = obs["t"] + 1.0
        log("mav_liftoff", t=obs["t"], samples=obs.get("samples_loaded"))
        return {"actions": acts, "engines": {"MAV1": {"throttle": 1.0}}, "dir": up.tolist(), "next": 3.0}

    def vertical(self, obs):
        up = unit(np.array(obs["r"]))
        if obs["t"] - self.t_lift >= M.MAV_T_VERT:
            self.phase = "ascent"
            return self.ascent(obs)
        return {"engines": {"MAV1": {"throttle": 1.0}}, "dir": up.tolist(), "next": 2.0}

    def stage_params(self, obs):
        F1, md1 = self.m.thrust_vac("MAV1"), self.m.mdot("MAV1")
        F2, md2 = self.m.thrust_vac("MAV2"), self.m.mdot("MAV2")
        return F1, md1, F2, md2

    def ascent(self, obs, extra=()):
        acts = list(extra)
        r = np.array(obs["r"])
        v = np.array(obs["v"])
        F1, md1, F2, md2 = self.stage_params(obs)
        staged = "MAV1" not in present(obs)
        if not staged and (any(e.startswith("burnout:MAV1") for e in obs["events"]) or prop_left(obs, "MAV1") <= 0.5) \
                and self.t_mav1_out is None:
            # coast while MAV1's thrust tails off, or it pushes into MAV2 after separation
            self.t_mav1_out = obs["t"]
            log("mav1_burnout", t=obs["t"], alt=obs.get("alt"))
            return {"dir": obs["attitude"], "next": M.STAGE_COAST}
        if not staged and (self.t_mav1_out is not None or any(e.startswith("burnout:MAV1") for e in obs["events"]) or prop_left(obs, "MAV1") <= 0.5):
            # stage: drop MAV1 and light MAV2 in the same command
            acts.append("separate:MAV1")
            staged = True
            log("mav_staging", t=obs["t"], alt=obs.get("alt"), v=float(np.linalg.norm(v)))
            first2 = True
        else:
            first2 = False
        m = stack_mass(obs, self.m)
        if staged and "MAV1" in present(obs):
            m -= self.m.dry("MAV1") + prop_left(obs, "MAV1")
        if self.ins is None:
            rt = MARS_R + M.MARS_PARK_ALT
            self.ins = StagedInsertion("Mars", rt, 0.0, math.sqrt(MU["Mars"] / rt), self.plan["n_ins"], h=4.0, tangent=True)
            self.ins.x = np.array([math.radians(70.0), -0.05, 0.0, 0.0, 0.6 * prop_left(obs, "MAV2") / md2])
        if not staged:
            t1 = prop_left(obs, "MAV1") / md1
            self.ins.stage1 = {"F": F1, "mdot": md1, "t1": t1, "m_drop": self.m.dry("MAV1"), "t_gap": 1.0 + M.STAGE_COAST}
            F, md = F2, md2
        else:
            self.ins.stage1 = None
            F, md = F2, md2
            if first2:
                # thrust starts one second after ignition
                r = r + v * 1.0
        shift = 0.0 if self.t_last is None else obs["t"] - self.t_last
        self.t_last = obs["t"]
        pz = pole("Mars", obs["t"])
        if first2 and self.ins.stage1 is None:
            # re-anchor the steering time origin: the staged law was parameterised from the last call
            pass
        x, g = self.ins.solve(r, v, m, F, md, iters=3 if self.ins.x is not None else 12, shift=shift,
                              isp_g0=self.m.isp("MAV2") * G0, pz=pz)
        T = x[4]
        log("mav_ascent", t=obs["t"], alt=obs.get("alt"), staged=staged, T=T, res=g.tolist(), x=x.tolist(), m=m,
            q=obs.get("q"), aoa=_aoa_deg(obs), nav=obs.get("nav_sigma"))
        stage = "MAV2" if staged else "MAV1"
        dt_arc = 4.0
        if staged and T < 1.5 * dt_arc and not first2:
            self.phase = "park"
            d = self.ins.direction(r, T / 2)
            return {"actions": acts, "engines": {"MAV2": {"throttle": 1.0}}, "dir": d.tolist(), "next": max(T, 0.05)}
        q = float(obs.get("q") or 0.0)
        if q > M.MAV_Q_SHORT_ARC:
            dt_arc = M.MAV_AIR_ARC   # the attitude holds through an arc while the airflow turns: short arcs in the air
        d = self.ins.direction(r, dt_arc / 2 + (1.0 if first2 else 0.0))
        d = _limit_aoa(obs, d)
        if not staged:
            t1 = prop_left(obs, "MAV1") / md1
            dt_arc = min(dt_arc, max(0.5, t1 + 0.01))
        return {"actions": acts, "engines": {stage: {"throttle": 1.0}}, "dir": d.tolist(), "next": dt_arc}

    def park(self, obs):
        if self.tei is None:
            r = np.array(obs["r"])
            v = np.array(obs["v"])
            rp, ra, e, en = orbit_rp_ra(r, v, MU["Mars"])
            log("mars_orbit", t=obs["t"], rp_alt=rp - MARS_R, ra_alt=ra - MARS_R, e=e)
            trim = getattr(self, "trim_s", 0.0)
            if rp - MARS_R < M.MAV_RP_MIN and e < 0.9 and trim < M.MAV_TRIM_MAX_S and prop_left(obs, "MAV2") > 20.0:
                # closed-loop check of the insertion on the navigated orbit: a periapsis this low decays or hits
                # Mars before the trans-Earth burn. Coast to apoapsis, then burn along the velocity to raise it
                # (a burn near periapsis would only raise the apoapsis).
                rn = float(np.linalg.norm(r))
                a_sm = 1.0 / (2.0 / rn - float(np.dot(v, v)) / MU["Mars"])
                n_mean = math.sqrt(MU["Mars"] / a_sm ** 3)
                cosE = (1.0 - rn / a_sm) / max(e, 1e-9)
                E = math.acos(max(-1.0, min(1.0, cosE)))
                if float(np.dot(r, v)) < 0:
                    E = 2 * math.pi - E
                M_now = E - e * math.sin(E)
                t_apo = ((math.pi - M_now) % (2 * math.pi)) / n_mean
                log("mav_rp_trim", t=obs["t"], rp_alt=rp - MARS_R, ra_alt=ra - MARS_R, t_apo=t_apo, trim_s=trim)
                if t_apo > 20.0 and t_apo < math.pi / n_mean * 1.9:
                    return {"dir": unit(v).tolist(), "next": max(1.0, t_apo - 5.0)}
                self.trim_s = trim + 2.0
                return {"engines": {"MAV2": {"throttle": 1.0}}, "dir": unit(v).tolist(), "next": 2.0}
            vd, _ = planet_vinf("Mars", "Earth", obs["t"] + 1800.0, M.T_EARR)

            def aim_fn(enc):
                a = TG.aim_point(enc["S"], enc["vinf"], M.T_EARR, M.UTTR_GC[0], M.UTTR_GC[1], M.EARTH_EI_FPA, M.EARTH_RANGE_DEG,
                                 M.EARTH_ENTRY_TIME, body="Earth", r_ei=6378137.0 + 120e3)
                self.fl.earth_aim = a
                # planetary protection: the trans-Earth burn aims at a miss; rtcm4, a week out, moves onto entry
                return TG.safe_miss_aim(a, enc["vinf"], M.EARTH_SAFE_PERIGEE_M)
            self.tei = Departure(self.fl, "Mars", "MAV2", vd, aim_fn, "Earth", M.T_EARR, M.TEI_G_MAX, False,
                                 M.T_EARR + 60 * 86400.0)
        cmd = self.tei.step(obs)
        if self.tei.done:
            self.phase = "coast"
        return cmd

    def next_handler(self):
        from .cruise import Cruise
        return Cruise(self.fl, leg="back")
