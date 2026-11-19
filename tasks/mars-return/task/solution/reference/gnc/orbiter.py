"""Earth return orbiter of the reference: a sketch, not yet flown.

What is here:
1. Relative motion and relative navigation helpers, ported from the orbiter.py: the container's local frame,
   the Clohessy-Wiltshire equations, and RelNav, an extended Kalman filter on range, range rate and line of sight.
   the closed-loop Orbiter class is not ported: it reads obs fields ("crafts", "relnav") that the split
   simulator does not give until the multi-craft loop is wired (models/rendezvous/crafts.py).
2. the plan from the capture orbit (the orbit Mars orbit insertion leaves the orbiter in) down to the
   rendezvous orbit, a circular orbit a little above the container's, and the phasing wait (plan_descent,
   phasing_wait).
3. the second launch option, an orbiter that leaves Earth on its own launch and reaches Mars before the
   lander (SECOND_LAUNCH and second_launch_budget).

The approach: in the container's local frame (radial, along track, cross track) the orbiter aims at a point
part of the way to the container, closing at a speed that falls with range (PROX_SPEED); it keeps its capture cone
pointed at the container, which keeps the container in the laser sensor's 20 degree field inside 150 m, and holds
attitude in the last 3 m. Capture is commanded when the filter puts the container inside the envelope with margin.
"""
from __future__ import annotations

import math

import numpy as np

from .util import J2, MARS_R, MU, RREF, unit

MU_M = MU["Mars"]
RCS_ISP = 225.0
# closing speed (m/s) against range (m), falling to the middle of the capture envelope (0.03 to 0.06 m/s)
PROX_SPEED = [(5000.0, 5.0), (1000.0, 2.0), (150.0, 0.5), (20.0, 0.1), (3.0, 0.045)]
# envelope margins the reference keeps when it commands capture (fractions of each limit)
CAPTURE_MARGIN = 0.6


def lvlh(r_t, v_t):
    """Rows: radial, along track, cross track unit vectors of the target's local frame; and its angular rate."""
    x = unit(r_t)
    z = unit(np.cross(r_t, v_t))
    y = np.cross(z, x)
    w = float(np.linalg.norm(np.cross(r_t, v_t))) / float(np.dot(r_t, r_t))
    return np.array([x, y, z]), w


def rel_state(r_c, v_c, r_t, v_t):
    """The chaser's position and velocity relative to the target, in the target's rotating local frame."""
    R, w = lvlh(r_t, v_t)
    dr = r_c - r_t
    wv = w * R[2]
    return R @ dr, R @ ((v_c - v_t) - np.cross(wv, dr)), R


def cw_target(p, v, p1, tau, n):
    """The velocity now, in the target's rotating local frame, that reaches p1 after tau seconds, by the
    Clohessy-Wiltshire equations (x radial, y along track, z cross track; n the target's angular rate)."""
    c, s = math.cos(n * tau), math.sin(n * tau)
    Prr = np.array([[4 - 3 * c, 0, 0], [6 * (s - n * tau), 1, 0], [0, 0, c]])
    Prv = np.array([[s / n, 2 * (1 - c) / n, 0], [2 * (c - 1) / n, (4 * s - 3 * n * tau) / n, 0], [0, 0, s / n]])
    return np.linalg.solve(Prv, p1 - Prr @ p)


def cw_stm(n, t):
    """The Clohessy-Wiltshire state transition matrix over t seconds, state [x, y, z, vx, vy, vz]."""
    c, s = math.cos(n * t), math.sin(n * t)
    F = np.zeros((6, 6))
    F[:3, :3] = [[4 - 3 * c, 0, 0], [6 * (s - n * t), 1, 0], [0, 0, c]]
    F[:3, 3:] = [[s / n, 2 * (1 - c) / n, 0], [2 * (c - 1) / n, (4 * s - 3 * n * t) / n, 0], [0, 0, s / n]]
    F[3:, :3] = [[3 * n * s, 0, 0], [6 * n * (c - 1), 0, 0], [0, 0, -n * s]]
    F[3:, 3:] = [[c, 2 * s, 0], [-2 * s, 4 * c - 3, 0], [0, 0, c]]
    return F


def sensor_sigma(meas):
    """One sigma of range (m), range rate (m/s) and line of sight (rad) for a measurement, from the published
    sensor accuracies in the manual (3-sigma)."""
    from marsflight.models.rendezvous.relnav import sensor_sigmas
    off = 0.0 if meas["sensor"] == "laser" else 180.0
    _, s_r, s_v, s_a = sensor_sigmas(float(meas["range"]), off)
    return s_r, s_v, math.radians(s_a)


class RelNav:
    """Relative navigation: an extended Kalman filter on the orbiter's position and velocity relative to the
    container, in the container's rotating local frame, with Clohessy-Wiltshire dynamics. Each call it takes the
    sensor's range, range rate and line of sight; the orbiter's own velocity changes are added as commanded, with
    3% uncertainty."""

    def __init__(self):
        self.x = None
        self.P = None
        self.t = None
        self.n = None

    def start(self, t, n, x0, sigma_r=1000.0, sigma_v=1.0):
        self.t, self.n = t, n
        self.x = np.asarray(x0, float).copy()
        self.P = np.diag([sigma_r ** 2] * 3 + [sigma_v ** 2] * 3)

    def predict(self, t, n=None):
        n = self.n if n is None else n
        dt = t - self.t
        if dt <= 0:
            return
        F = cw_stm(n, dt)
        rho = float(np.linalg.norm(self.x[:3]))
        q = (1e-5 + rho * (n * n * rho / 3.7e6 + 1e-8)) ** 2
        Q = q * np.block([[dt ** 3 / 3 * np.eye(3), dt ** 2 / 2 * np.eye(3)], [dt ** 2 / 2 * np.eye(3), dt * np.eye(3)]])
        self.x = F @ self.x
        self.P = F @ self.P @ F.T + Q
        self.t = t

    def add_dv(self, dv):
        self.x[3:] += dv
        self.P[3:, 3:] += np.eye(3) * ((0.03 * float(np.linalg.norm(dv))) ** 2 + 1e-6)

    def update(self, meas, R):
        s_r, s_v, s_a = sensor_sigma(meas)
        rho = float(meas["range"])
        u = R @ np.array(meas["los"], float)          # line of sight, orbiter to container, local frame
        z = -rho * u                                   # the orbiter relative to the container
        Rm = s_r ** 2 * np.outer(u, u) + (rho * s_a) ** 2 * (np.eye(3) - np.outer(u, u))
        H = np.hstack([np.eye(3), np.zeros((3, 3))])
        S = H @ self.P @ H.T + Rm
        K = self.P @ H.T @ np.linalg.inv(S)
        self.x = self.x + K @ (z - H @ self.x)
        self.P = (np.eye(6) - K @ H) @ self.P
        p, v = self.x[:3], self.x[3:]
        r = float(np.linalg.norm(p))
        Hr = np.concatenate([v / r - float(np.dot(p, v)) * p / r ** 3, p / r])
        Sr = float(Hr @ self.P @ Hr) + s_v ** 2
        Kr = self.P @ Hr / Sr
        self.x = self.x + Kr * (float(meas["range_rate"]) - float(np.dot(p, v)) / r)
        self.P = (np.eye(6) - np.outer(Kr, Hr)) @ self.P


def prox_speed(rho):
    """Planned closing speed (m/s) at range rho (m), from PROX_SPEED."""
    tab = PROX_SPEED
    if rho >= tab[0][0]:
        return tab[0][1]
    for (a, fa), (b, fb) in zip(tab, tab[1:]):
        if b <= rho <= a:
            return fb + (fa - fb) * (rho - b) / (a - b)
    return tab[-1][1]


# ---------------------------------------------------------------- capture orbit to rendezvous orbit
def _vis_viva(r, a):
    return math.sqrt(MU_M * (2.0 / r - 1.0 / a))


def plan_descent(capture_peri_alt, capture_apo_alt, rdv_alt, plane_change_deg=0.0):
    """Burns that take the orbiter from its capture orbit (periapsis and apoapsis altitudes, m) to a circular
    rendezvous orbit at rdv_alt (m). Two impulsive burns: at periapsis, lower the apoapsis to rdv_alt; at the new
    apoapsis, circularize. A plane change, if any, is made in the first apoapsis pass of the capture orbit, where
    the speed is lowest, before the descent. Returns a list of {"where", "dv"} and the total (m/s)."""
    rp, ra, rc = MARS_R + capture_peri_alt, MARS_R + capture_apo_alt, MARS_R + rdv_alt
    if not rp <= rc:
        raise ValueError("the rendezvous orbit must be at or above the capture periapsis")
    burns = []
    a0 = (rp + ra) / 2
    if plane_change_deg:
        va = _vis_viva(ra, a0)
        burns.append({"where": "capture apoapsis", "dv": 2 * va * math.sin(math.radians(plane_change_deg) / 2)})
    a1 = (rp + rc) / 2
    burns.append({"where": "periapsis: lower apoapsis", "dv": _vis_viva(rp, a0) - _vis_viva(rp, a1)})
    burns.append({"where": "apoapsis: circularize", "dv": math.sqrt(MU_M / rc) - _vis_viva(rc, a1)})
    return burns, sum(abs(b["dv"]) for b in burns)


def phasing_wait(rdv_alt, container_alt, phase_deg):
    """Time (s) until the container, phase_deg ahead of the orbiter, is where a half-orbit transfer down to it
    should start, with both on circular coplanar orbits. Uses the J2 rate on an equatorial-like orbit as an
    estimate; the closed-loop plan solves it again from navigation."""
    r1, r2 = MARS_R + rdv_alt, MARS_R + container_alt
    n1, n2 = math.sqrt(MU_M / r1 ** 3), math.sqrt(MU_M / r2 ** 3)
    t_tr = math.pi * math.sqrt(((r1 + r2) / 2) ** 3 / MU_M)
    lead = math.pi - n2 * t_tr                      # the container's lead at the start of the transfer
    rel = n2 - n1
    if rel == 0:
        return math.inf
    dphi = (lead - math.radians(phase_deg)) % (2 * math.pi)
    if rel < 0:
        dphi = 2 * math.pi - dphi
    return dphi / abs(rel)


def j2_node_rate(alt, inc_deg):
    """Nodal regression rate (rad/s) of a circular Mars orbit: the two orbits must share a plane at the time of the
    rendezvous, so a different altitude or inclination drifts the planes apart."""
    a = MARS_R + alt
    n = math.sqrt(MU_M / a ** 3)
    return -1.5 * n * J2["Mars"] * (RREF["Mars"] / a) ** 2 * math.cos(math.radians(inc_deg))


# ---------------------------------------------------------------- the orbiter on a second launch
SECOND_LAUNCH = {
    # The orbiter leaves Earth on its own launcher one window before or in the same window as the lander, enters a
    # capture orbit by propulsive insertion, and waits. Engineering sketch from ESA's ERO (about 5,000 kg wet at
    # launch, electric cruise) scaled to a chemical cruise with the reference's AJ10 class engine.
    "insertion_capture_orbit": {"peri_alt_m": 300e3, "apo_alt_m": 33000e3},
    "arrival_vinf_mps": 2650.0,
    "isp_s": 320.0,
    "dry_kg": 1600.0,
    "rendezvous_orbit_alt_m": 380e3,
    "earth_return_dv_mps": 2300.0,
}


def second_launch_budget(cfg=SECOND_LAUNCH, payload_back_kg=60.0):
    """Delta-v (m/s) and propellant (kg) of the second-launch orbiter: Mars orbit insertion into the capture orbit,
    the descent to the rendezvous orbit (plan_descent), proximity operations (60 m/s allowance), and the
    trans-Earth burn with the captured samples on board."""
    c = cfg["insertion_capture_orbit"]
    rp, ra = MARS_R + c["peri_alt_m"], MARS_R + c["apo_alt_m"]
    a = (rp + ra) / 2
    v_hyp = math.sqrt(cfg["arrival_vinf_mps"] ** 2 + 2 * MU_M / rp)
    moi = v_hyp - _vis_viva(rp, a)
    _, desc = plan_descent(c["peri_alt_m"], c["apo_alt_m"], cfg["rendezvous_orbit_alt_m"])
    prox = 60.0
    ve = cfg["isp_s"] * 9.80665
    # backwards from the end: the trans-Earth burn carries dry + samples
    m = cfg["dry_kg"] + payload_back_kg
    m *= math.exp(cfg["earth_return_dv_mps"] / ve)
    m -= payload_back_kg                            # the samples are captured after the descent and proximity burns
    m *= math.exp((desc + prox) / ve)
    m *= math.exp(moi / ve)
    return {"moi_dv": moi, "descent_dv": desc, "prox_dv": prox, "return_dv": cfg["earth_return_dv_mps"],
            "wet_at_mars_kg": m, "propellant_kg": m - cfg["dry_kg"]}
