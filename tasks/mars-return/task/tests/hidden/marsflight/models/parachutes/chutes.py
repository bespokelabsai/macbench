"""Parachute deployment, opening and area.

Methods of Mission, moved verbatim from sim.py. Mission inherits them from ParachuteMixin."""
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


def cluster_cd_factor(n):
    """Drag area of n canopies flying together, over n times one canopy's (data.CHUTE_CLUSTER_CD)."""
    if n <= 1:
        return 1.0
    if n in D.CHUTE_CLUSTER_CD:
        return D.CHUTE_CLUSTER_CD[n]
    top = max(D.CHUTE_CLUSTER_CD)
    return max(D.CHUTE_CLUSTER_CD_MIN, D.CHUTE_CLUSTER_CD[top] - D.CHUTE_CLUSTER_CD_SLOPE * (n - top))


def reef_stages(chute_type):
    """The reefed stages of a canopy type, [(drag area ratio, disreef time after line stretch s), ...], then the
    full open stage (1.0, None). An unreefed type has the full open stage only."""
    r = D.CHUTE_REEFING.get(chute_type)
    return (list(r["stages"]) if r else []) + [(1.0, None)]


def rated_load_N(chute_type, diameter, design_q):
    """The opening load (N) a canopy holds: a hard opening (kP at its 99th percentile, data.CHUTE_KP) at its design
    q. A reefed canopy opens first to its first stage, so it is rated for that stage's load with Apollo's margin for
    the later stages (data.CHUTE_REEFING)."""
    f = parachute_peak_load(chute_type, diameter, design_q, D.CHUTE_KP["rated"])
    r = D.CHUTE_REEFING.get(chute_type)
    if r:
        f *= r["stages"][0][0] * r["rated_over_first"]
    return f


def canopy_mass_kg(chute_type, diameter, design_q):
    """Mass of a parachute with its mortar (data.CANOPY_AREAL_KG_M2, CANOPY_KG_PER_N, PARACHUTE_BASE_KG): the fabric
    over its nominal area plus strength members for the load it is rated for. vehicle.py should use this in place
    of PARACHUTE_MASS_K D^2 q + PARACHUTE_BASE_KG."""
    s0 = math.pi * (diameter / 2.0) ** 2
    return D.CANOPY_AREAL_KG_M2 * s0 + D.CANOPY_KG_PER_N * rated_load_N(chute_type, diameter, design_q) + D.PARACHUTE_BASE_KG


class ParachuteMixin:
    def _chute_factor(self, e):
        return self.disp.chute_cd_factor * ((self.truth.get("chutes") or {}).get(e.id) or {}).get("cd", 1.0)

    def _cluster(self, e):
        """The canopies of e's cluster: the parachutes still on the vehicle of the same type and size."""
        return [s.el for s in self.present() if s.el.kind == "parachute" and s.el.chute_type == e.chute_type
                and abs(s.el.chute_diameter - e.chute_diameter) < 1e-6]

    def _chute_area(self, mach, t=None):
        """Drag area of the deployed parachutes at time t. A canopy that is still opening has no drag until its
        lines stretch. Then, stage by stage (one stage unless it is reefed, data.CHUTE_REEFING), its drag area
        grows as the fourth power of the time since the stage began, up to the stage's peak opening load at its
        full inflation; after that it has the stage's steady drag until the next disreef. Canopies flying together
        lose drag to interference (data.CHUTE_CLUSTER_CD)."""
        area, n_open = 0.0, 0
        for st in self.present():
            e = st.el
            if e.kind == "parachute" and e.id in self.chute_deployed:
                op = self.chute_open.get(e.id)
                cd = interp(D.PARACHUTES[e.chute_type]["cd"], mach) * self._chute_factor(e)
                s0 = math.pi * (e.chute_diameter / 2) ** 2
                if op is None or t is None:
                    area += cd * s0
                    n_open += 1
                    continue
                if t <= op["t_ls"]:
                    continue
                n_open += 1
                stages = op["stages"]
                k = len(stages) - 1
                while k > 0 and t < stages[k]["t0"]:
                    k -= 1
                sg = stages[k]
                if t < sg["t_fi"]:
                    prev = cd * s0 * (stages[k - 1]["eps"] if k > 0 else 0.0)
                    peak = self._chute_factor(e) * op["cds_peak"] * sg["eps"]
                    area += prev + (peak - prev) * ((t - sg["t0"]) / (sg["t_fi"] - sg["t0"])) ** 4
                else:
                    area += cd * s0 * sg["eps"]
        return area * cluster_cd_factor(n_open)

    def _stage_fill_s(self, e, eps0, eps1, speed):
        """Time for a canopy to fill from drag area ratio eps0 to eps1 at airspeed `speed` (Knacke sec. 7.3.7.4)."""
        return D.CHUTE_FILL_DIAMETERS * e.chute_diameter * math.sqrt(max(eps1 - eps0, 0.0)) / max(speed, 1.0)

    def deploy(self, st):
        e = st.el
        if e.kind != "parachute" or e.id in self.chute_deployed or e.id in self.chute_failed:
            self.cmd_errors.append(f"deploy:{e.id}: not a stowed parachute")
            return
        mach, q = self.last.get("mach", 0.0), self.last.get("q", 0.0)
        env = D.PARACHUTES[e.chute_type]
        if mach > env["mach"][1]:
            self.fail(f"parachute {e.id} deployed at Mach {mach:.2f}, above the {e.chute_type} limit of {env['mach'][1]}")
        if q > env["q"][1]:
            self.fail(f"parachute {e.id} deployed at {q:.0f} Pa, above the {e.chute_type} qualification limit of {env['q'][1]:.0f} Pa")
        if self._failure_now("chute_fail", e.id):
            # A failure check: the canopy never opens (a failed extraction or deployment). It is spent, and it is
            # not in chute_deployed, so it adds no drag, no 6-DOF load and does not count as a parachute landing.
            self.chute_failed.add(e.id)
            self.event("parachute_failed", element=e.id, mach=round(mach, 3), q=round(q, 1))
            return
        self.chute_deployed.add(e.id)
        # The opening (data.CHUTE_*): the lines stretch, then the canopy fills over a fixed distance, in stages if it
        # is reefed. The peak opening load of each stage comes at its full inflation, and _chute_opened checks it
        # against what the canopy holds.
        if self.seed is None:
            kp = D.CHUTE_KP["mean"]
        else:   # its own generator, so the other draws of this world do not move
            g = np.random.default_rng([abs(int(self.seed)) % 2 ** 32, zlib.crc32(e.id.encode())])
            kp = float(np.clip(g.normal(D.CHUTE_KP["mean"], D.CHUTE_KP["sigma"]), 0.5, 1.1))
        t_ls = self.t + D.CHUTE_LINE_STRETCH_S_PER_M * e.chute_diameter
        v0 = max(self.last.get("airspeed", 0.0), 1.0)
        stages, eps_prev, t0 = [], 0.0, t_ls
        # Later stages start slower. Their speed is estimated at deploy as the cluster's terminal speed under the
        # stage before, in the air of this moment (never above the speed now), so that the whole opening is known
        # before Tudat flies it.
        n_cl = len(self._cluster(e))
        r = self.x[:3] if self.x is not None else None
        g_loc = self.gm(self.central) / float(np.dot(r, r)) if r is not None else D.G0
        rho = self.last.get("rho") or 0.0
        cd0 = interp(env["cd"], 0.0)
        for eps, t_disreef in reef_stages(e.chute_type):
            if stages:
                t0 = max(t_ls + stages[-1]["t_disreef"], stages[-1]["t_fi"])
            v = v0
            if stages and rho > 0:
                a_prev = n_cl * cluster_cd_factor(n_cl) * cd0 * math.pi * (e.chute_diameter / 2) ** 2 * eps_prev
                v = min(v0, math.sqrt(2.0 * self.total_mass() * g_loc / (rho * max(a_prev, 1e-6))))
            stages.append({"eps": eps, "t0": t0, "t_fi": t0 + self._stage_fill_s(e, eps_prev, eps, v), "t_disreef": t_disreef})
            eps_prev = eps
        self.chute_open[e.id] = {"t_ls": t_ls, "t_fi": stages[0]["t_fi"], "kp": kp, "stage": 0, "stages": stages,
                                 "v_start": {0: self.last.get("airspeed")},
                                 "cds_peak": parachute_peak_load(e.chute_type, e.chute_diameter, 1.0, kp)}
        self.exposures.append((self.t, "chute", e.id, 1))
        self.event("parachute_deployed", element=e.id, mach=round(mach, 3), q=round(q, 1))

    def _fill_dynamics(self, e, op, mach):
        """What the opening needs of the vehicle's motion: air density, the cluster's drag multiplier, mass, gravity
        along the path, this canopy's Cd and nominal area."""
        cd = interp(D.PARACHUTES[e.chute_type]["cd"], mach) * self._chute_factor(e)
        s0 = math.pi * (e.chute_diameter / 2) ** 2
        n_cl = max(1, sum(1 for c in self._cluster(e) if c.id in self.chute_deployed))
        n_open = sum(1 for s in self.present() if s.el.kind == "parachute" and s.el.id in self.chute_deployed)
        r = self.x[:3]
        air = self.x[3:] - self.omega_cross(r) if self.central != "Sun" else self.x[3:]
        g_down = self.gm(self.central) / float(np.dot(r, r)) * float(-np.dot(unit(air), unit(r)))
        return (self.last.get("rho") or 0.0, n_cl * cluster_cd_factor(max(n_open, 1)), max(self.total_mass(), 1.0),
                g_down, cd, s0)

    @staticmethod
    def _speed_along(v, t_span, area, rho, mult, M, g_down, n=64):
        """Airspeed along a straight path under drag area area(tau) (one canopy; mult for the cluster) and gravity:
        RK4 forward over t_span. Yields (tau, v) at each step, the start included."""
        h = t_span / n

        def f(tau, u):
            return -0.5 * rho * mult * area(tau) * u * u / M + g_down

        yield 0.0, v
        for i in range(n):
            tau = i * h
            k1 = f(tau, v)
            k2 = f(tau + h / 2, v + h / 2 * k1)
            k3 = f(tau + h / 2, v + h / 2 * k2)
            k4 = f(tau + h, v + h * k3)
            v = max(v + h / 6 * (k1 + 2 * k2 + 2 * k3 + k4), 0.0)
            yield tau + h, v

    def _fill_peak_load(self, e, op, k, q_fi, mach):
        """Peak opening load (N) of stage k of canopy e, and the dynamic pressure then. The peak drag area, 2 kP
        S_proj times the stage's drag area ratio, comes at full inflation (Way 2018). On a heavy, fast vehicle (a
        Mars DGB) the speed hardly drops while the canopy fills, so the load peaks there. A light vehicle under a
        large canopy slows down while it fills and the load peaks earlier, at a smaller drag area and a higher q
        (the finite mass case, Knacke sec. 5.4.6 and 7.3.7). So the fill is flown forward from the stage's starting
        speed with the drag area this simulation flies (_chute_area: fourth power growth, the whole cluster) and
        gravity along the path, as in Knacke's force-trajectory-time method, and the load is the largest drag on
        this canopy along it (at least the drag at full inflation)."""
        st = op["stages"][k]
        a_pk = self._chute_factor(e) * op["cds_peak"] * st["eps"]
        v0 = (op.get("v_start") or {}).get(k)
        tf = st["t_fi"] - st["t0"]
        if self.x is None or v0 is None or tf <= 0.0:
            return a_pk * q_fi, q_fi
        rho, mult, M, g_down, cd, s0 = self._fill_dynamics(e, op, mach)
        if rho <= 0.0:
            return a_pk * q_fi, q_fi
        a_prev = cd * s0 * (op["stages"][k - 1]["eps"] if k > 0 else 0.0)

        def area(tau):
            return a_prev + (a_pk - a_prev) * (tau / tf) ** 4

        best = (a_pk * q_fi, q_fi)
        for tau, v in self._speed_along(v0, tf, area, rho, mult, M, g_down):
            q = 0.5 * rho * v * v
            if area(tau) * q > best[0]:
                best = (area(tau) * q, q)
        return best

    def _next_stage_speed(self, e, op, k, q_fi, mach):
        """The airspeed at which stage k + 1 begins: from full inflation of stage k, steady under its drag until the
        disreef."""
        st, nx = op["stages"][k], op["stages"][k + 1]
        rho, mult, M, g_down, cd, s0 = self._fill_dynamics(e, op, mach) if self.x is not None else (0.0,) * 6
        if rho <= 0.0:
            return None
        v = math.sqrt(2.0 * q_fi / rho)
        span = nx["t0"] - st["t_fi"]
        if span <= 0.0:
            return v
        a = cd * s0 * st["eps"]
        for _, v in self._speed_along(v, span, lambda tau: a, rho, mult, M, g_down):
            pass
        return v

    def _chute_opened(self, e, op, q, mach, t):
        """Full inflation of a stage: its peak opening load (_fill_peak_load: at most 2 kP q S_proj times the stage's
        drag area ratio, Way 2018 and data.CHUTE_REEFING), against what the canopy holds (rated_load_N). The shock it gives the vehicle, with the
        rest of its cluster opening alongside, counts against the capsule's g limit (the arc's samples are too far
        apart to see it). A reefed canopy then waits for its next disreef; models/structures/loads.py calls this
        again when that stage fills (op["t_fi"])."""
        k = op.get("stage", 0)
        stages = op["stages"]
        eps = stages[k]["eps"]
        n_cl = max(1, sum(1 for c in self._cluster(e) if c.id in self.chute_deployed))
        load, q_pk = self._fill_peak_load(e, op, k, q, mach)
        rated = rated_load_N(e.chute_type, e.chute_diameter, e.chute_design_q)
        # The canopies of a cluster open together and their shocks add at the vehicle.
        g_open = load * n_cl / max(self.total_mass(), 1.0) / D.G0
        self.peak["g"] = max(self.peak["g"], g_open)
        last = k == len(stages) - 1
        self.event("parachute_opened", at=t, element=e.id, mach=round(mach, 3), q=round(q, 1), q_at_peak=round(q_pk, 1),
                   peak_load_kN=round(load / 1e3, 1),
                   rated_kN=round(rated / 1e3, 1), kp=round(op["kp"], 3), g=round(g_open, 2),
                   stage="full open" if last else f"reefed {k + 1}", drag_area_ratio=round(eps, 4))
        if last:
            op["open"] = True
        else:
            op.setdefault("v_start", {})[k + 1] = self._next_stage_speed(e, op, k, q, mach)
            op["stage"] = k + 1
            op["t_fi"] = stages[k + 1]["t_fi"]
        if load > rated:
            self.fail(f"parachute {e.id} tore at full inflation{'' if last else f' of reefed stage {k + 1}'}: its peak "
                      f"opening load of {load / 1e3:.0f} kN is above the {rated / 1e3:.0f} kN it holds (design q "
                      f"{e.chute_design_q:.0f} Pa)")
        for s in self.present():
            if s.el.kind == "capsule" and g_open > s.el.g_limit:
                self.fail(f"the opening shock of parachute {e.id}, {g_open:.1f} g, exceeds the capsule's {s.el.g_limit:.0f} g limit")
