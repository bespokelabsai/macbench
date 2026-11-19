"""Propellant slosh in the 6-DOF rigid body (wired by the dynamics wiring PR; switch MARSFLIGHT_SLOSH).

The physics is the (models/slosh/basilisk.py): each tank's first slosh mode from NASA SP-106 (Table 6.1, with
the dome correction), smooth-wall damping (SP-106 eq. 4.10a) and Miles' ring-baffle damping (SP-106 eq. 4.16),
swung as Basilisk's spherical pendulum on the vehicle (``pendulum_rates_nl``: the pendulum written out, nonlinear,
so a swinging mass stays within its pendulum length when the thrust tails off at an engine cutoff, which the
linear spring-mass model did not; the Basilisk comparison measured the written-out form within 0.02% of Basilisk).

The coupling is Basilisk's: the slosh masses hang from the hub, which is the vehicle without them, and the torque
they put on it turns the rigid body (the Basilisk comparison found an earlier version turned the rigid part about the wrong point, 2.5% off in
frequency on a heavy LOX slosh mass).

Integration: the slosh states are integrated with the attitude, in lock step with Tudat's fixed-step RK4 (the
6-DOF arcs in the air and under thrust). dynamics/rigid_body.py asks ``Slosh.torque`` at every evaluation of its
torque callback; this class knows which of the four RK4 stages that is from its time within the step, gives the
stage's slosh states, keeps the stage's derivative, and adds the step up once the next step starts. Tudat flying
the end of an arc again (to refine a crossing) rewinds to the states kept at each step. So no core change is
needed (the core's Model interface has no state hook yet).

Ring baffles: a stage design field ``"baffles": {"width_frac": 0.05 to 0.3, "spacing_m": 0.1 to 20}``, read
from the element's spec. Their mass (the ``build_baffles``) belongs in vehicle.py, which this PR asks of its
owner (``baffle_mass``).

Settling: the rule, ``settle_time`` = 1.93 sqrt(L / a), scaled from AS-203's flown LH2 settling (90 s at 4e-4 g
in the S-IVB's 8.56 m tank; NTRS 19680012073, p. VI-2).

Out of the model, as in the Basilisk pendulum: higher modes, swirl, roll coupling, and liquid that is not held at the
bottom of its tank (axial acceleration below A_SETTLED); there the states are frozen, and a coast starts them
again at rest.
"""
from __future__ import annotations

import json
import math
import os

from ... import data as D
from .basilisk import (A_SETTLED, BREAK_ZETA, PKEYS, baffle_damping, interp, pendulum_rates_nl, swing_amplitude,
                       tank_params, wall_damping, wave_height)

SETTLE_K = 1.93
T_EPS = 1e-5


def settle_time(tank_length, accel):
    """Seconds of settling that liquid needs to collect at the bottom of a tank this long (m) at this axial
    acceleration (m/s^2), scaled from AS-203's flown settling."""
    return SETTLE_K * math.sqrt(tank_length / max(accel, 1e-6))


def baffle_spec(e):
    """(width_frac, spacing_m) of a stage's ring baffles, or None (design field ``baffles``, as in the slosh model)."""
    spec = (e.spec or {}).get("baffles") if hasattr(e, "spec") else None
    if not isinstance(spec, dict):
        return None
    try:
        w, s = float(spec.get("width_frac", 0.15)), float(spec.get("spacing_m", 0.0))
    except (TypeError, ValueError):
        return None
    if not (0.1 <= s <= 20.0):
        return None
    return min(max(w, 0.05), 0.3), s


def tank_baffles(e, tk):
    b = baffle_spec(e)
    if b is None:
        return None
    w, s = b
    return {"rings": [s * k for k in range(1, int(tk.length / s) + 1) if s * k < tk.length - 1e-6], "width_frac": w}


def baffle_mass(e):
    """Mass of a stage's ring baffles (kg), the build_baffles: flat annuli of the tank wall's material and
    thickness. For vehicle.py's owner to add to the stage's mass breakdown."""
    b = baffle_spec(e)
    if b is None or not e.tanks:
        return 0.0
    w, s = b
    mat = D.MATERIALS[e.material]
    out = 0.0
    for tk in e.tanks.values():
        R = tk.radius
        n = len([k for k in range(1, int(tk.length / s) + 1) if s * k < tk.length - 1e-6])
        out += n * math.pi * (R * R - (R * (1.0 - w)) ** 2) * tk.wall_t * mat["rho"] * mat.get("liner_factor", 1.0)
    return out


def tanks(lay, prop, engine_section):
    """Every tank with liquid: [(key, x of the tank bottom, Tank, liquid kg, baffles or None)], placed as
    rigid_body.mass_items places them. prop: {element id: (fuel, ox)} instead of the propellant loaded now."""
    out = []
    for s, x0, _ in lay:
        e = s.el
        if not e.is_stage:
            continue
        fuel, ox = (prop or {}).get(e.id, (s.fuel, s.ox))
        xt = x0 + engine_section(e)
        for role, tk in e.tanks.items():
            liq = fuel if role == "fuel" else ox
            if liq > 1.0:
                out.append(((e.id, role), xt, tk, liq, tank_baffles(e, tk)))
            xt += tk.length
    return out


class Slosh:
    """The slosh states of one flight and their part in each 6-DOF arc."""

    def __init__(self):
        self.state = {}        # tank key -> [theta_y, theta_z, theta_y', theta_z']
        self.arc = []          # (key, params at the arc start, at its end, baffles)
        self.peak = {}         # tank key -> largest wave at the wall over the radius, this flight
        self.arc_peak = {}
        self.wave_now = {}
        self.breaking = set()
        self.log = os.environ.get("MARSFLIGHT_SLOSH_LOG")
        self._reset_steps(0.0)

    # ---------------------------------------------------------------- arcs
    def begin_arc(self, lay, burn, engine_section, settled, t0):
        end = {k: (xt, tk, liq) for k, xt, tk, liq, _ in tanks(lay, burn, engine_section)}
        self.arc = []
        if not settled:
            self.coast()
        for key, xt, tk, liq, baf in (tanks(lay, None, engine_section) if settled else []):
            rho = D.PROPELLANTS[tk.propellant]["rho"]
            p0 = tank_params(xt, tk.radius, liq, rho, tk.propellant)
            p1 = tank_params(end[key][0], tk.radius, end[key][2], rho, tk.propellant) if key in end else dict(p0, m1=0.0)
            self.arc.append((key, p0, p1, baf))
            self.state.setdefault(key, [0.0, 0.0, 0.0, 0.0])
        live = {a[0] for a in self.arc}
        self.state = {k: v for k, v in self.state.items() if k in live}
        self.arc_peak = {}
        self._reset_steps(t0)

    def _reset_steps(self, t0):
        x = [v for a in self.arc for v in self.state[a[0]]] if self.arc else []
        self.t_n, self.x_n, self.ks, self.hist = t0, x, [], [(t0, list(x))]
        self.last_in = None

    def params(self, f):
        return [interp(p0, p1, f) for _, p0, p1, _ in self.arc]

    def inertia_removed(self, f, xc):
        return sum(p["m1"] * (p["x"] - xc) ** 2 for p in self.params(f))

    def max_omega(self, a, M, xc, It):
        """Highest slosh frequency of the arc (rad/s) with the vehicle's reaction, for the integrator step."""
        out = 0.0
        for f in (0.0, 1.0):
            for p in self.params(f):
                if p["m1"] <= 0:
                    continue
                d = p["x"] - xc
                k = 1.0 + p["m1"] / max(M - p["m1"], 1e-6) + p["m1"] * abs(d * (d + p["L"])) / max(It, 1e-6)
                out = max(out, math.sqrt(max(a, 0.0) / p["L"] * k))
        return out

    # ---------------------------------------------------------------- one evaluation of the torque callback
    def _zetas(self, ps, x, ax):
        out = []
        for i, (p, (key, _, _, baf)) in enumerate(zip(ps, self.arc)):
            th, om = x[4 * i:4 * i + 2], x[4 * i + 2:4 * i + 4]
            amp = math.hypot(swing_amplitude(p, ax, th[0], om[0]), swing_amplitude(p, ax, th[1], om[1]))
            wave = wave_height(p, amp)
            z = wall_damping(p["nu"], p["R"], p["h"], ax)
            if baf:
                z += baffle_damping(baf["rings"], baf["width_frac"], p["R"], p["z_s"], wave)
            if wave > p["R"]:
                z += BREAK_ZETA
            out.append(z)
            self.wave_now[key] = (wave, p["R"])
            if wave > self.arc_peak.get(key, (0.0, p["R"]))[0]:
                self.arc_peak[key] = (wave, p["R"])
        return out

    def _deriv(self, x, inp):
        f, ax, xc, Ix, It, M, w, F_lat, tq = inp
        ps = self.params(f)
        It_rigid = max(It - sum(p["m1"] * (p["x"] - xc) ** 2 for p in ps), 0.05 * It)
        zetas = self._zetas(ps, x, ax)
        dx, (sy, sz), _, _ = pendulum_rates_nl(ps, x, ax, xc, Ix, It_rigid, M, w, F_lat, tq, zetas, I_T=It)
        return dx, (sy, sz)

    def _commit(self, h):
        k1, k2, k3, k4 = self.ks
        self.x_n = [x + h / 6.0 * (a + 2 * b + 2 * c + d) for x, a, b, c, d in zip(self.x_n, k1, k2, k3, k4)]
        self.t_n += h
        self.hist.append((self.t_n, list(self.x_n)))
        self.ks = []

    def torque(self, t, h, inp):
        """The slosh torque (pitch, yaw) to add to the rigid body at this evaluation, at time t of a fixed RK4 step
        h. inp: (share of the arc's mass change, axial specific force, xc, Ix, It (all liquid rigid), M,
        true body rates, other lateral forces (y, z), other torques (body axes))."""
        if not self.arc:
            return 0.0, 0.0
        if t < self.t_n - T_EPS:
            # Tudat flies part of the arc again: go back to the last step that starts at or before t
            while len(self.hist) > 1 and self.hist[-1][0] > t + T_EPS:
                self.hist.pop()
            self.t_n, self.x_n = self.hist[-1][0], list(self.hist[-1][1])
            self.ks = []
        tau = t - self.t_n
        if tau > h - T_EPS and len(self.ks) == 4:
            self._commit(h)
            tau = t - self.t_n
        if tau > 0.5 * h + T_EPS and len(self.ks) < 3 and self.ks:
            # a stage was not asked for (its state equalled another's): repeat the last derivative for it
            while len(self.ks) < 3:
                self.ks.append(self.ks[-1])
        if abs(tau) < T_EPS or not self.ks or tau < -T_EPS:
            x, slot = self.x_n, 0
        elif abs(tau - 0.5 * h) < T_EPS:
            slot = min(len(self.ks), 2)
            x = [a + 0.5 * h * k for a, k in zip(self.x_n, self.ks[slot - 1])]
        else:
            slot = 3
            x = [a + h * k for a, k in zip(self.x_n, self.ks[2] if len(self.ks) >= 3 else self.ks[-1])]
            if tau > h + T_EPS:   # a whole step went by unseen (should not happen): restart from here
                self.t_n, self.ks = t, []
                self.hist.append((t, list(x)))
                self.x_n, slot = list(x), 0
        dx, tq = self._deriv(x, inp)
        self.ks = self.ks[:slot] + [dx]
        self.last_in = (t, h)
        return tq

    def states_at(self, t):
        """The slosh states at time t of the arc (the end of a step at or before t)."""
        if self.ks and len(self.ks) == 4 and self.last_in and t > self.t_n + T_EPS:
            self._commit(self.last_in[1])
        best = self.hist[0]
        for row in self.hist:
            if row[0] <= t + T_EPS:
                best = row
        return best[1]

    def end_arc(self, t_end):
        """Keep the states at the arc's end; return the tanks whose wave grew taller than their radius."""
        if not self.arc:
            return []
        x = self.states_at(t_end)
        for i, (key, _, _, _) in enumerate(self.arc):
            self.state[key] = list(x[4 * i:4 * i + 4])
        out = []
        for key, (wave, lim) in self.arc_peak.items():
            r = wave / lim if lim > 0 else 0.0
            self.peak[key] = max(self.peak.get(key, 0.0), r)
            if r > 1.0:
                out.append((key, r, wave, lim))
        if self.log:
            try:
                with open(self.log, "a") as fh:
                    fh.write(json.dumps({"t": t_end, "peak_wave_over_R": {f"{k[0]}/{k[1]}": round(w / l, 5)
                                                                           for k, (w, l) in self.arc_peak.items()}}) + "\n")
            except OSError:
                pass
        return out

    def coast(self):
        """Nothing holds the liquid at the tank bottoms: the pendulums start again at rest."""
        self.state = {k: [0.0, 0.0, 0.0, 0.0] for k in self.state}
        self.wave_now, self.arc_peak, self.breaking = {}, {}, set()

    def tele(self):
        return {"slosh_wave_m": {f"{k[0]}/{k[1]}": round(w, 4) for k, (w, _) in self.wave_now.items()}}


__all__ = ["Slosh", "settle_time", "baffle_mass", "tanks", "A_SETTLED", "PKEYS"]
