"""Departure burns from a parking orbit (TMI at Earth, TEI at Mars).

Closed loop: cross-product steering towards the velocity that puts the vehicle on the two-body
hyperbola with the target hyperbolic excess vector V, cut off when the velocity to be gained is used up.
Before the burn, V is corrected by shooting with the full force model (Tudat predictor) so that the
trajectory hits the B-plane aim point at the destination. The planning is spread over several guidance
calls during the parking-orbit coast so that no call runs long on one CPU.
"""
from __future__ import annotations

import math

import numpy as np

from . import target as TG
from .log import log
from .powered import _rk4
from .util import G0, J2, MU, RREF, hyperbola_velocity, kepler_vinf, pole, rodrigues, unit
from .vehicle_model import accel_meas, prop_left, stack_mass

TAIL_S = 0.21   # s: main engine thrust tail-off time constant after a shutdown (manual, "Actuators from flight data")
RCS_TRIM_MIN = 0.5     # m/s: an RCS trim follows a departure burn that ends at least this short of its target
RCS_TRIM_TOL = 0.1     # m/s: ... and ends within this
RCS_TRIM_SHARE = 0.5   # the trim uses at most this share of the stage's RCS propellant


class Departure:
    def __init__(self, fl, body, stage, V0, aim_fn, dest, t_arr, g_max, settle, t_max_enc):
        """aim_fn(enc) -> aim dict with bt, br, tp for the destination encounter enc."""
        self.fl, self.m = fl, fl.model
        self.body, self.stage = body, stage
        self.V = np.array(V0, float)
        self.aim_fn = aim_fn
        self.dest, self.t_arr = dest, t_arr
        self.g_max = g_max
        self.settle = settle
        self.t_max_enc = t_max_enc
        self.mu = MU[body]
        self.j2k = 1.5 * J2[body] * MU[body] * RREF[body] ** 2
        self.state = "plan0"
        self.t_ign = None
        self.thr_prev = None
        self.done = False
        self.k_iter = 0
        self.aim = None
        self.enc = None

    # ------------------------------------------------------------------ steering
    def steer(self, r, v, s_hat, vinf, a_T):
        h = np.cross(r, v)
        n = unit(np.cross(r, s_hat))
        if np.dot(n, h) < 0:
            n = -n
        vr = hyperbola_velocity(r, s_hat, vinf, self.mu, n)
        vg = vr - v
        Q = np.zeros((3, 3))
        for k in range(3):
            dr = np.zeros(3)
            dr[k] = 200.0
            Q[:, k] = (hyperbola_velocity(r + dr, s_hat, vinf, self.mu, n) - hyperbola_velocity(r - dr, s_hat, vinf, self.mu, n)) / 400.0
        p = -Q @ vg
        u = unit(vg)
        ap = p - np.dot(p, u) * u
        if np.linalg.norm(ap) >= a_T:
            d = u
        else:
            d = unit(ap + math.sqrt(a_T * a_T - float(np.dot(ap, ap))) * u)
        return d, vg

    def throttle(self, F, m):
        thr = min(1.0, self.g_max * G0 * m / F)
        return max(self.m.min_throttle(self.stage), thr)

    def thrust(self):
        return self.m.thrust_vac(self.stage), self.m.mdot(self.stage), self.m.isp(self.stage)

    def sim_burn(self, st0, t_coast, V, pz):
        F, md, isp = self.thrust()
        s_hat, vinf = unit(V), float(np.linalg.norm(V))
        st = _rk4(st0, 0.0, 0.0, lambda *a: (1.0, 0.0, 0.0), t_coast, self.mu, self.j2k, pz, 10.0) if t_coast > 0 else st0
        used, tb = 0.0, 0.0
        for _ in range(600):
            r = np.array(st[:3])
            v = np.array(st[3:6])
            mm = st[6]
            thr = self.throttle(F, mm)
            a_T = F * thr / mm
            d, vg = self.steer(r, v, s_hat, vinf, a_T)
            tgo = mm / (md * thr) * (1 - math.exp(-np.linalg.norm(vg) / (isp * G0)))
            h = min(4.0, tgo)
            st = _rk4(st, F * thr, md * thr, lambda tau, *a, d=d: (d[0], d[1], d[2]), h, self.mu, self.j2k, pz, 1.0)
            used += h * md * thr
            tb += h
            if tgo <= 4.0:
                break
        return st, tb, used

    def encounter_of(self, V):
        st, tb, used = self.sim_burn(self.st0, self.t_coast, V, self.pz)
        t_cut = self.t_plan + self.t_coast + tb
        return TG.encounter(t_cut, np.array(st[:6]), self.body, self.dest, self.t_max_enc, self.t_arr), used

    def g_of(self, enc):
        return np.array([(enc["bt"] - self.aim["bt"]) / 1e3, (enc["br"] - self.aim["br"]) / 1e3,
                         (enc["tp"] - self.aim["tp"]) / 10.0])

    # ------------------------------------------------------------------ planning over several calls
    def plan_step(self, obs):
        """Returns True when the plan is complete."""
        if self.state == "plan0":
            r0 = np.array(obs["r"])
            v0 = np.array(obs["v"])
            m = stack_mass(obs, self.m)
            self.pz = tuple(pole(self.body, obs["t"]))
            self.t_plan = obs["t"]
            self.st0 = (*r0, *v0, m)
            F, md, isp = self.thrust()
            s_hat, vinf = unit(self.V), float(np.linalg.norm(self.V))
            from .launch import perigee_dir
            n_orb = unit(np.cross(r0, v0))
            pdir, _ = perigee_dir(s_hat, n_orb, vinf, np.linalg.norm(r0), self.mu)
            ang = math.atan2(float(np.dot(np.cross(r0, pdir), n_orb)), float(np.dot(r0, pdir)))
            if ang < 0:
                ang += 2 * math.pi
            period = 2 * math.pi * math.sqrt(np.linalg.norm(r0) ** 3 / self.mu)
            t_imp = ang / (2 * math.pi) * period
            vp = math.sqrt(vinf ** 2 + 2 * self.mu / np.linalg.norm(r0))
            dv = vp - np.linalg.norm(v0)
            tb = m / (md * 0.7) * (1 - math.exp(-dv / (isp * G0)))
            best = None
            for frac in (0.35, 0.45, 0.55, 0.65):
                ts = max(240.0, t_imp - frac * tb)
                _, tbb, used = self.sim_burn(self.st0, ts, self.V, self.pz)
                if best is None or used < best[1]:
                    best = (ts, used)
            self.t_coast = best[0]
            self.t_ign = obs["t"] + self.t_coast
            self.enc, self.used = self.encounter_of(self.V)
            if self.enc is not None:
                self.aim = self.aim_fn(self.enc)
            log("dep_plan0", body=self.body, t_ign=self.t_ign, coast=self.t_coast, used=self.used, dv=dv,
                enc=None if self.enc is None else {k: self.enc[k] for k in ("bt", "br", "tp")}, aim=self.aim)
            self.state = "iter"
            return False
        if self.state == "iter":
            if self.enc is None or self.aim is None or self.k_iter >= 4:
                self.state = "ready"
                return True
            g = self.g_of(self.enc)
            if np.max(np.abs(g)) < 0.5:
                self.state = "ready"
                return True
            J = np.zeros((3, 3))
            for k in range(3):
                Vk = self.V.copy()
                Vk[k] += 0.05
                ek, _ = self.encounter_of(Vk)
                if ek is None:
                    self.state = "ready"
                    return True
                J[:, k] = (self.g_of(ek) - g) / 0.05
            step = np.linalg.lstsq(J, -g, rcond=None)[0]
            if np.linalg.norm(step) > 200.0:
                step *= 200.0 / np.linalg.norm(step)
            self.V = self.V + step
            self.enc, self.used = self.encounter_of(self.V)
            if self.enc is not None:
                self.aim = self.aim_fn(self.enc)
            self.k_iter += 1
            log("dep_iter", body=self.body, k=self.k_iter, g=g.tolist(), V=self.V.tolist(),
                enc=None if self.enc is None else {k: self.enc[k] for k in ("bt", "br", "tp")})
            return False
        return True

    # ------------------------------------------------------------------ flying
    def step(self, obs):
        """Coast/plan until ignition, then the burn. Returns a command; sets self.done after cutoff."""
        if self.state in ("plan0", "iter"):
            if self.state == "iter" and self.t_ign is not None and obs["t"] > self.t_ign - 90.0:
                self.state = "ready"
            else:
                ready = self.plan_step(obs)
                if not ready:
                    return {"dir": self.dir0(obs).tolist(), "next": 60.0}
        if self.state == "ready":
            if obs["t"] < self.t_ign - 12.0:
                return {"dir": self.dir0(obs).tolist(), "next": min(600.0, self.t_ign - 10.0 - obs["t"])}
            if obs["t"] < self.t_ign - 0.5:
                cmd = {"dir": self.dir0(obs).tolist(), "next": self.t_ign - obs["t"]}
                if self.settle:
                    cmd["settle"] = True
                return cmd
            self.state = "burn"
            return self.burn(obs, first=True)
        if self.state == "burn":
            return self.burn(obs)
        if self.state in ("cut", "trim"):
            r, v = np.array(obs["r"]), np.array(obs["v"])
            _, vg = self.steer(r, v, unit(self.V), float(np.linalg.norm(self.V)), 1.0)
            vgm = float(np.linalg.norm(vg))
            if self.state == "cut":
                log("dep_cut_residual", body=self.body, t=obs["t"], vg=vg.tolist(), vg_along=float(vg @ unit(v)), vg_mag=vgm)
            cmd = self.rcs_trim(obs, vg)
            if cmd is not None:
                return cmd
            self.done = True
            return {"dir": obs["attitude"], "next": 1.0}
        raise RuntimeError(self.state)

    def rcs_trim(self, obs, vg):
        """Finish a burn the stage could not complete on its RCS (a stage that ran dry short of the target, as the
        reference MAV2 does by about 10 m/s in the trans-Earth burn). Done here, right after cutoff, it costs the
        shortfall itself; left to the first correction a day later it costs several times more. Uses at most
        RCS_TRIM_SHARE of the stage's RCS propellant, so the cruise corrections keep the rest."""
        vgm = float(np.linalg.norm(vg))
        el = next((e for e in obs.get("elements") or [] if e["id"] == self.stage), None)
        F = next((float(e.get("rcs_thrust_N", 0.0) or 0.0) for e in self.fl.info["design"]["elements"]
                  if e.get("id") == self.stage), 0.0)
        if el is None or F <= 0.0 or el.get("rcs_kg") is None:
            return None
        if self.state == "cut":
            self.state = "trim"
            self.trim = {"rcs0": float(el["rcs_kg"]), "n": 0, "firing": False}
            if vgm < RCS_TRIM_MIN:
                return None
            log("dep_rcs_trim_start", body=self.body, t=obs["t"], vg=vgm, rcs_kg=el["rcs_kg"])
            # turn to the direction first (6-DOF: the RCS turns the stack within this arc)
            return {"dir": unit(vg).tolist(), "next": 30.0}
        tr = self.trim
        tr.setdefault("t0", obs["t"])
        # Arcs end at once while a stage that ran dry still tails off (about 1 ms each); those calls are not counted
        # as trim arcs, but all calls together are capped so the flight stays far from the 20,000-call limit.
        tr["calls"] = tr.get("calls", 0) + 1
        used = tr["rcs0"] - float(el["rcs_kg"])
        if vgm < RCS_TRIM_TOL or tr["n"] >= 20 or used > RCS_TRIM_SHARE * tr["rcs0"] or obs["t"] - tr["t0"] > 3600.0 \
                or tr["calls"] > 1500:
            log("dep_rcs_trim_end", body=self.body, t=obs["t"], vg=vgm, rcs_used=used, n=tr["n"])
            return None
        att = obs.get("attitude")
        if att is not None and float(np.dot(unit(np.array(att)), unit(vg))) < math.cos(math.radians(5.0)) \
                and obs["t"] - tr["t0"] < 600.0:
            # still turning (at most 10 minutes; after that fire anyway, the burn steers as it goes)
            return {"dir": unit(vg).tolist(), "next": 20.0}
        a = F / stack_mass(obs, self.m)
        if obs["t"] - tr.get("t_last", -1e9) > 0.1:
            tr["n"] += 1
        tr["t_last"] = obs["t"]
        return {"rcs": {"element": self.stage, "throttle": 1.0}, "dir": unit(vg).tolist(),
                "next": float(min(max(0.8 * vgm / a, 0.5), 60.0))}

    def dir0(self, obs):
        r = np.array(obs["r"])
        v = np.array(obs["v"])
        F, md, isp = self.thrust()
        m = stack_mass(obs, self.m)
        d, _ = self.steer(r, v, unit(self.V), float(np.linalg.norm(self.V)), F / m)
        return d

    def burn(self, obs, first=False):
        if not first and (any(e.startswith(f"burnout:{self.stage}") for e in obs["events"]) or prop_left(obs, self.stage) <= 1.0):
            log("dep_burnout", body=self.body, t=obs["t"])
            self.state = "cut"
            return {"dir": obs["attitude"], "next": 1.0}
        r = np.array(obs["r"])
        v = np.array(obs["v"])
        m = stack_mass(obs, self.m)
        F, md, isp = self.thrust()
        thr = self.throttle(F, m)
        fac = 1.0
        if not first:
            am = accel_meas(obs)
            if am > 0.5 and self.thr_prev:
                fac = am * m / (F * self.thr_prev)
        self.thr_prev = thr
        a_T = F * thr * fac / m
        d, vg = self.steer(r, v, unit(self.V), float(np.linalg.norm(self.V)), a_T)
        tgo = m / (md * thr * fac) * (1 - math.exp(-np.linalg.norm(vg) / (isp * G0)))
        log("dep_burn", body=self.body, t=obs["t"], vg=float(np.linalg.norm(vg)), tgo=tgo, thr=thr, fac=fac)
        eng = {self.stage: {"throttle": thr}}
        if first:
            return {"engines": eng, "dir": d.tolist(), "next": 5.0}
        vgm = float(np.linalg.norm(vg))
        # Cutoff on the measured state, not on one predicted arc: the cutoff arc used to be the predicted time to go,
        # and with the engine's measured transients the burn ended 10 m/s short (TEI in the nominal world), which
        # the RCS corrections could not make up (the return cruise missed Earth by 400,000 km). Now the last
        # seconds are flown in short arcs, each re-measured, and the engine stops when the time to go is down to
        # the shutdown tail-off (thrust falls with a 0.21 s time constant, so the tail-off adds about F x 0.21 s).
        self.n_trim = getattr(self, "n_trim", 0)
        if tgo <= 6.0:
            if vgm < 0.3 or tgo <= TAIL_S + 0.02 or self.n_trim >= 8:
                self.state = "cut"
                log("dep_cut", body=self.body, t=obs["t"], vg=vgm, tgo=tgo, n_trim=self.n_trim)
                return {"dir": d.tolist(), "next": 1.0}
            self.n_trim += 1
            return {"engines": eng, "dir": d.tolist(), "next": max(tgo - TAIL_S, 0.05)}
        return {"engines": eng, "dir": d.tolist(), "next": min(4.0, tgo - 2.0)}
