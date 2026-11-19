"""6-DOF mode: rigid-body attitude dynamics, attitude control and stage separation tracking.

marsflight flies 3-DOF by default: Tudat propagates the translation and the mass, and the attitude turns
towards the commanded direction no faster than the slew rate. In 6-DOF mode (Mission(dynamics="6dof"),
MARSFLIGHT_DYNAMICS=6dof, or `marsflight fly --dynamics 6dof`) the vehicle is a rigid body:

- Its center of mass and inertia come from the stack layout (`layout`, `mass_items`): each element's dry
  mass is split into engines, tanks and structure at their places along the stack, and the propellant
  lies at the bottom of each tank.
- In every powered or atmospheric arc, Tudat propagates the attitude (quaternion and body rates) with the
  translation and the mass. The torques are engine gimbal, RCS, thrust misalignment, the aerodynamic
  normal force at the center of pressure (slender-body theory) with pitch damping, and the pull of a
  deployed parachute at the top of its carrier.
- An onboard controller turns the flight software's attitude command into actuator commands at every
  integrator step: a direction (an attitude autopilot), a body rate (a rate loop) or a torque.
- Coasts outside the atmosphere keep a kinematic attitude: the RCS turns the vehicle and pays propellant
  for it, and a vehicle without RCS keeps its body rates. A lifting entry flies the trimmed 3-DOF model,
  with its heatshield into the airflow.
- Separated elements are tracked for 60 s, or until they are 100 m clear, relative to the vehicle, with
  their own drag, the thrust of a stage that separates while its engines still burn (or while its thrust
  tails off after a shutdown), and a separation push. A recontact ends the flight.
- The autopilot and the flight software see the attitude through gyros and a star tracker, and Mach and
  dynamic pressure through navigation and an onboard atmosphere (attitude_sensors.py).

Body axes: x along the stack from the bottom to the top (the thrust direction), y and z across it. On the
pad x is up, y east and z north. Quaternions are scalar first and rotate body axes into J2000.
"""
from __future__ import annotations

import math
from types import SimpleNamespace

import numpy as np

from .. import data as D
from ..attitude_sensors import AttitudeSensors, air_data, rotation
from . import disturbances as dist
from .entry import EntryMixin, entry_on, forebody_depth
from ..models.slosh.coupled import A_SETTLED, Slosh, settle_time

SLOSH_DEFAULT = True      # MARSFLIGHT_SLOSH: see the dynamics wiring PR for why

BANDWIDTH = 1.5            # rad/s: natural frequency of the onboard attitude loop (the flight software may set 0.2 to 4)
DAMPING = 0.7
DEADBAND_DEG = 1.0         # coast attitude hold: the S-IVB's deadband (Astrionics System Handbook, MSFC IV-4-401-1, Fig. 3.4-6)
RCS_SHARE = 0.25           # share of an element's RCS thrust that acts about one axis
SEP_PUSH = 0.5             # m/s: relative speed the separation springs give
TRACK_S = 60.0             # s: how long a separated element is tracked
CLEAR_M = 100.0            # m: an opening gap this wide ends the tracking early
RECONTACT_M = 0.05         # m of overlap that counts as recontact
TIPOFF_DEG_S = 0.3         # one sigma per lateral axis of the tip-off rate at separation, dispersed worlds only
# s: time constant of the thrust tail-off after a shutdown or a burnout. Apollo 11's S-II stage fell to 5% of its
# cutoff thrust within 0.75 s, with a cutoff impulse of 0.21 s at the cutoff thrust (AS-506 launch vehicle flight
# evaluation report, MPR-SAT-FE-69-9, section 6.4); exp(-0.75 / 0.25) = 5%.
TAILOFF_TAU = 0.25
# Actuators. The sources are in REFERENCE.md ("Actuators") and results/issue-63/README.md.
GIMBAL_RATE_DEG_S = None   # gimbal rate limit for every engine (deg/s); None: each engine's own (Engine.gimbal_rate_deg_s)
GIMBAL_TAU_S = 0.074       # gimbal servo lag, first order: 1 / the Saturn servo loop gain of 13.56 per s (Douglas SM-43658, Table II)
RCS_MIN_ON_S = 0.022       # shortest RCS pulse: Aerojet MR-104H hydrazine thruster, 8.23 N s at 22 ms on (data sheet 2020, p. 13)
THROTTLE_TAU_S = 0.2       # engine thrust lag after a throttle command: the SSME's chamber pressure (Biggs 1992)
# fastest thrust change, as a share of full thrust per second: the SSME's 7,000 lb per 10 ms (Mitchell and Searle,
# SSME Digital Control Design Characteristics, NASA N85-16893, p. 2) over its 109% thrust of 2,279 kN
THROTTLE_RATE_PER_S = 700000 * 4.4482216 / 2279e3
T_EPS = 1e-5               # s: times are seconds since J2000 (about 1e9), resolved to about 1e-7 s
DT_RING = 0.3              # engines of a cluster sit on a ring of this fraction of the stage diameter (as for roll)
# Actuator data (MARSFLIGHT_ACTUATOR_DATA). The shortest RCS pulse gives the MR-104H data sheet's 8.23 N s, not
# its 22 ms at full thrust (10.46 N s, the comparison): the valve opens and the chamber pressure rises and falls,
# so the pulse delivers the impulse of 17.3 ms at full thrust. The main engine tail-off gives Apollo 11's S-II cutoff
# impulse of 0.21 s at the cutoff thrust (MPR-SAT-FE-69-9, section 6.4); a first-order fall's impulse is its time
# constant. The Saturn V's center engines did not gimbal (S-IC and S-II: four outboard engines gimballed, the
# center one fixed; Saturn V Flight Manual SA-503, sections II and III): a cluster of 5 or more with an odd count
# here gimbals all but its center engine.
RCS_MIN_IMPULSE_NS = 8.23
RCS_MIN_IMPULSE_FULL_NS = 10.46
TAILOFF_TAU_DATA = 0.21


def _switch(name, default):
    import os
    v = os.environ.get("MARSFLIGHT_" + name, "").strip().lower()
    if v in ("1", "true", "on", "yes"):
        return True
    if v in ("0", "false", "off", "no"):
        return False
    return default


def _transients():
    """Engine start and shutdown curves in 6-DOF (models/propulsion/switches.py)."""
    try:
        from ..models.propulsion.switches import on
        return on("ENGINE_TRANSIENTS")
    except ImportError:
        return False


def actuator_data():
    """the actuator numbers from flight data and data sheets (MARSFLIGHT_ACTUATOR_DATA)."""
    return _switch("ACTUATOR_DATA", ACTUATOR_DATA_DEFAULT)


ACTUATOR_DATA_DEFAULT = True


def rcs_bit():
    """The shortest RCS pulse as seconds at full thrust (its impulse over the thrust)."""
    return RCS_MIN_ON_S * (RCS_MIN_IMPULSE_NS / RCS_MIN_IMPULSE_FULL_NS if actuator_data() else 1.0)


def tailoff_tau():
    return TAILOFF_TAU_DATA if actuator_data() else TAILOFF_TAU


def gimballed_share(n):
    """Share of a cluster's engines that gimbal: all but a fixed center engine in an odd cluster of 5 or more."""
    return (n - 1) / n if (actuator_data() and n >= 5 and n % 2 == 1) else 1.0


# ---------------------------------------------------------------------- rotations
def q_to_R(q):
    w, x, y, z = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
                     [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
                     [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]])


def R_to_q(R):
    tr = R[0, 0] + R[1, 1] + R[2, 2]
    if tr > 0:
        s = 2.0 * math.sqrt(tr + 1.0)
        q = [0.25 * s, (R[2, 1] - R[1, 2]) / s, (R[0, 2] - R[2, 0]) / s, (R[1, 0] - R[0, 1]) / s]
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = 2.0 * math.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2])
        q = [(R[2, 1] - R[1, 2]) / s, 0.25 * s, (R[0, 1] + R[1, 0]) / s, (R[0, 2] + R[2, 0]) / s]
    elif R[1, 1] > R[2, 2]:
        s = 2.0 * math.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2])
        q = [(R[0, 2] - R[2, 0]) / s, (R[0, 1] + R[1, 0]) / s, 0.25 * s, (R[1, 2] + R[2, 1]) / s]
    else:
        s = 2.0 * math.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1])
        q = [(R[1, 0] - R[0, 1]) / s, (R[0, 2] + R[2, 0]) / s, (R[1, 2] + R[2, 1]) / s, 0.25 * s]
    q = np.array(q)
    return q / np.linalg.norm(q) * (1.0 if q[0] >= 0 else -1.0)


def q_mul(a, b):
    w1, x1, y1, z1 = a
    w2, x2, y2, z2 = b
    return np.array([w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2, w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
                     w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2, w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2])


def q_rotvec(rv):
    """The quaternion of a rotation vector (rad)."""
    a = float(np.linalg.norm(rv))
    if a < 1e-12:
        return np.array([1.0, 0.5 * rv[0], 0.5 * rv[1], 0.5 * rv[2]])
    s = math.sin(0.5 * a) / a
    return np.array([math.cos(0.5 * a), rv[0] * s, rv[1] * s, rv[2] * s])


def turn(q, w_body, dt):
    """Attitude after dt at a constant body rate."""
    q = q_mul(q, q_rotvec(np.asarray(w_body, float) * dt))
    return q / np.linalg.norm(q)


def frame_with_x(x, hint):
    """Body axes with x along `x` and y as close to `hint` as possible."""
    x = np.asarray(x, float) / np.linalg.norm(x)
    y = np.asarray(hint, float) - np.dot(hint, x) * x
    if np.linalg.norm(y) < 1e-6:
        y = np.cross([0.0, 0.0, 1.0], x)
        if np.linalg.norm(y) < 1e-6:
            y = np.cross([0.0, 1.0, 0.0], x)
    y = y / np.linalg.norm(y)
    return np.column_stack([x, y, np.cross(x, y)])


def _unit(v):
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def _interp(table, x):
    if x <= table[0][0]:
        return table[0][1]
    for (a, fa), (b, fb) in zip(table, table[1:]):
        if x <= b:
            return fa + (fb - fa) * (x - a) / (b - a)
    return table[-1][1]


def _cross(a, b):
    return np.array([a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]])


def actuator(x0, xc, dt, tau, rate):
    """Where a first-order actuator with a rate limit is dt after it started at x0 towards a held command xc:
    it moves at (xc - x) / tau, but no faster than `rate`. Works on each component of an array."""
    x0, xc = np.asarray(x0, float), np.asarray(xc, float)
    e = xc - x0
    ae, sg = np.abs(e), np.sign(e)
    if tau <= 0:
        return x0 + sg * np.minimum(ae, rate * dt)
    if not math.isfinite(rate):
        return xc - e * math.exp(-dt / tau)
    t1 = np.maximum(0.0, ae - rate * tau) / rate          # time at the rate limit
    lin = x0 + sg * rate * dt
    e1 = np.minimum(ae, rate * tau)                         # error when the lag takes over
    expo = xc - sg * e1 * np.exp(-np.maximum(0.0, dt - t1) / tau)
    return np.where(dt <= t1, lin, expo)


def actuator_mean(x0, xc, s0, s1, tau, rate):
    """The mean of `actuator` between s0 and s1 after it started (s1 > s0)."""
    x0, xc = np.asarray(x0, float), np.asarray(xc, float)
    e = xc - x0
    ae, sg = np.abs(e), np.sign(e)
    t1 = np.maximum(0.0, ae - rate * tau) / rate if math.isfinite(rate) else np.zeros_like(ae)
    e1 = np.minimum(ae, rate * tau) if math.isfinite(rate) else ae

    def area(s):
        lin = x0 * s + sg * rate * s * s / 2 if math.isfinite(rate) else 0.0 * s
        u = np.maximum(0.0, s - t1)
        full = x0 * t1 + sg * rate * t1 * t1 / 2 if math.isfinite(rate) else 0.0
        return np.where(s <= t1, lin, full + xc * u - sg * e1 * tau * (1.0 - np.exp(-u / tau)))
    return (area(s1) - area(s0)) / (s1 - s0)


def gimbal_rate(en):
    """An engine's gimbal rate limit (rad/s)."""
    return math.radians(GIMBAL_RATE_DEG_S if GIMBAL_RATE_DEG_S is not None else en.gimbal_rate_deg_s)


def ring(n, radius):
    """(y, z) of n engines evenly on a ring about the stack axis."""
    return [(radius * math.cos(2 * math.pi * i / n), radius * math.sin(2 * math.pi * i / n)) for i in range(n)]


def rcs_pulse(duty, frame):
    """The share of a control frame an RCS thruster fires, for a demanded share `duty`: no pulse is shorter than
    RCS_MIN_ON_S, so a demand below that rounds to one shortest pulse or to none."""
    on = min(1.0, abs(duty)) * frame
    if on < RCS_MIN_ON_S:
        # one shortest pulse gives the impulse of rcs_bit() at full thrust (with the data: 8.23 N s)
        on = rcs_bit() if on >= 0.5 * RCS_MIN_ON_S else 0.0
    return math.copysign(min(1.0, on / frame), duty) if frame > 0 else 0.0


def _flow_ratio(en, thr, thr0):
    """Propellant flow at throttle thr over the flow at thr0 (with the operating_point when the engine has it)."""
    if thr0 <= 0:
        return 0.0
    if hasattr(en, "operating_point"):
        return en.operating_point(thr)[1] / en.operating_point(thr0)[1]
    return thr / thr0


# ---------------------------------------------------------------------- stack layout and mass
def nozzle_length(en):
    De, Dt = math.sqrt(4 * en.Ae / math.pi), math.sqrt(4 * en.At / math.pi)
    return 0.8 * (De - Dt) / 2 / math.tan(math.radians(15)) + 2.0 * Dt


def engine_section(e):
    """Length of a stage's engine section, as vehicle.py sizes the stage."""
    return max([nozzle_length(en) + 0.5 for en, _ in e.engines] + [0.0])


def inside_map(elements):
    """Backshell id -> ids of the elements it encloses (from its heatshield)."""
    return {e.spec.get("_backshell"): list(e.spec.get("_inside", [])) for e in elements if e.kind == "heatshield"}


def layout(states, inside_of=None):
    """Where each element sits along the body x axis, from the bottom of the stack (x = 0) up:
    [(state, x_bottom, x_top)]. A fairing starts on the element below it and encloses what is above it.
    A backshell starts at the rim of its heatshield (or, once the heatshield is gone, at the bottom of what
    it encloses). A parachute sits at the top of its carrier."""
    inside_of = inside_of or {}
    out, x, rim, top, bottom = [], 0.0, {}, {}, {}
    for s in states:
        e = s.el
        if e.kind == "fairing":
            row = (s, x, x + e.length)
        elif e.kind == "heatshield":
            # 6-DOF entry: what it carries sits on its back, at the depth of the sphere-cone from the nose to the rim
            h = forebody_depth(e) if entry_on() else e.length
            row = (s, x, x + h)
            x += h
            rim[e.spec.get("_backshell")] = x
        elif e.kind == "backshell":
            base = rim.get(e.id)
            if base is None:
                base = min([bottom[i] for i in inside_of.get(e.id, []) if i in bottom] + [x])
            row = (s, base, base + e.length)
            x = max(x, base + e.length)
        elif e.kind == "parachute":
            t = top.get(getattr(e, "carrier", None), x)
            row = (s, t, t)
        else:
            row = (s, x, x + e.length)
            x += e.length
        out.append(row)
        bottom[e.id], top[e.id] = row[1], row[2]
    return out


def _shell(m, x0, x1, R):
    L = x1 - x0
    return (m, 0.5 * (x0 + x1), m * R * R, m * (R * R / 2 + L * L / 12))


def mass_items(lay, prop=None, rcs=None):
    """Point masses with their own inertias, [(mass, x, Ixx, Itransverse)], for everything on the stack.
    prop: {element id: (fuel, ox)} to use instead of the propellant now loaded; rcs: {element id: RCS
    propellant} likewise."""
    items = []
    for s, x0, x1 in lay:
        e = s.el
        L = x1 - x0
        R = max(e.diameter, 0.2) / 2
        f = s.dry / e.dry_mass if e.dry_mass > 0 else 0.0
        bd = e.mass_breakdown
        m_rcs = (rcs or {}).get(e.id, s.rcs)
        if m_rcs > 0:
            items.append((m_rcs, 0.5 * (x0 + x1), 0.0, 0.0))
        if e.is_stage:
            eng_len = engine_section(e)
            used = 0.0
            m_eng = f * sum(bd.get(k, 0.0) for k in ("engines", "thrust_structure", "landing_legs"))
            if m_eng > 0:
                r, h = 0.4 * R, max(eng_len, 0.1)
                items.append((m_eng, x0 + 0.5 * eng_len, 0.5 * m_eng * r * r, m_eng * (3 * r * r + h * h) / 12))
                used += m_eng
            fuel, ox = (prop or {}).get(e.id, (s.fuel, s.ox))
            xt = x0 + eng_len
            for role, tk in e.tanks.items():
                m_tank = f * sum(bd.get(k, 0.0) for k in (f"{role}_tank", f"{role}_tank_foam", f"{role}_tank_mli"))
                rt = tk.radius
                items.append((m_tank, xt + 0.5 * tk.length, m_tank * rt * rt, m_tank * (rt * rt / 2 + tk.length ** 2 / 12)))
                used += m_tank
                liq = fuel if role == "fuel" else ox
                if liq > 0:
                    # Settled at the bottom of the tank. A liquid does not spin up with its tank in roll.
                    h = min(tk.length, liq / D.PROPELLANTS[tk.propellant]["rho"] / (math.pi * rt * rt))
                    items.append((liq, xt + 0.5 * h, 0.0, liq * (rt * rt / 4 + h * h / 12)))
                xt += tk.length
            if s.dry - used > 0:
                items.append(_shell(s.dry - used, x0, x1, R))
        elif e.kind == "heatshield":
            items.append((s.dry, x0 + 0.4 * L, 0.5 * s.dry * R * R, 0.25 * s.dry * R * R))
        elif e.kind == "capsule":
            # A validation case may give a flown capsule's measured center of mass (from the nose) and inertia.
            xm, inertia = getattr(e, "com_axial_m", None), getattr(e, "inertia_kg_m2", None)
            Ix_c, It_c = inertia if inertia else (0.5 * s.dry * R * R, s.dry * (3 * R * R + L * L) / 12)
            items.append((s.dry, x0 + (0.35 * L if xm is None else xm), Ix_c, It_c))
        elif e.kind == "parachute":
            items.append((s.dry, x0, 0.0, 0.0))
        else:
            items.append(_shell(s.dry, x0, x1, R))
    return items


def mass_properties(items):
    """(mass, center of mass x, roll inertia, transverse inertia about the center of mass)."""
    M = sum(i[0] for i in items)
    if M <= 0:
        return 0.0, 0.0, 1.0, 1.0
    xc = sum(i[0] * i[1] for i in items) / M
    Ix = sum(i[2] for i in items)
    It = sum(i[3] + i[0] * (i[1] - xc) ** 2 for i in items)
    return M, xc, max(Ix, 1e-3 * M), max(It, 1e-3 * M)


def outer_segments(lay, inside_of=None):
    """The outer mold line, bottom first: [(x_bottom, x_top, diameter)]. What a fairing, an aeroshell or a
    backshell encloses is not on it, and neither are parachutes."""
    covered = set()
    for s, _, _ in lay:
        e = s.el
        if e.kind == "fairing":
            covered.update(e.spec.get("_covers", []))
        elif e.kind == "heatshield":
            covered.update(e.spec.get("_inside", []))
        elif e.kind == "backshell":
            covered.update((inside_of or {}).get(e.id, []))
    return sorted((x0, x1, s.el.diameter) for s, x0, x1 in lay
                  if s.el.kind != "parachute" and s.el.id not in covered and x1 > x0)


def normal_force_points(segs, S_ref):
    """Normal-force slope (per radian, on S_ref) and where it acts, [(x, CN_alpha)]. Slender-body theory
    for the nose (2 x its base area, two thirds of the way down it) and for every change of diameter
    (2 x the change in area: a flare stabilizes, a boattail destabilizes). The rest of the stack's
    SLENDER_CN_ALPHA acts as body lift at the centroid of the planform."""
    if not segs or S_ref <= 0:
        return []
    area = lambda d: math.pi * d * d / 4   # noqa: E731
    top = max(segs, key=lambda g: g[1])
    L_nose = min(top[1] - top[0], 1.5 * top[2])
    pts = [(top[1] - 2.0 / 3.0 * L_nose, 2.0 * area(top[2]) / S_ref)]
    for lo, up in zip(segs, segs[1:]):
        if abs(lo[2] - up[2]) > 1e-6:
            pts.append((up[0], 2.0 * (area(lo[2]) - area(up[2])) / S_ref))
    body = D.SLENDER_CN_ALPHA - sum(c for _, c in pts)
    if body > 0:
        w = sum((g[1] - g[0]) * g[2] for g in segs)
        pts.append((sum((g[1] - g[0]) * g[2] * 0.5 * (g[0] + g[1]) for g in segs) / w, body))
    return pts


def stack_report(vehicle):
    """Mass properties of the whole stack at liftoff, for `marsflight check`."""
    states = [SimpleNamespace(el=e, dry=e.dry_mass, fuel=e.loaded.get("fuel", 0.0), ox=e.loaded.get("ox", 0.0),
                              rcs=e.rcs_prop) for e in vehicle.elements]
    if not states:
        return None
    inside = inside_map(vehicle.elements)
    lay = layout(states, inside)
    M, xc, Ix, It = mass_properties(mass_items(lay))
    segs = outer_segments(lay, inside)
    dmax = max([s.el.diameter for s in states if s.el.kind != "parachute"] + [0.5])
    pts = normal_force_points(segs, math.pi * dmax ** 2 / 4)
    cna = sum(c for _, c in pts)
    xcp = sum(x * c for x, c in pts) / cna if cna else None
    out = {"length_m": round(max(x1 for _, _, x1 in lay), 2), "center_of_mass_from_bottom_m": round(xc, 2),
           "inertia_roll_kg_m2": round(Ix, 1), "inertia_pitch_yaw_kg_m2": round(It, 1)}
    if xcp is not None:
        out["center_of_pressure_from_bottom_m"] = round(xcp, 2)
        out["static_margin_calibers"] = round((xc - xcp) / dmax, 2)
    return out


def turn_plan(theta, w0, a, r_cap, T):
    """A rest-to-rest turn through theta about one axis, with angular acceleration a: from rate w0 (towards the
    target) change to a cruise rate, then stop at the target. The cruise rate is the slowest that ends the turn
    within T, and no faster than r_cap; if none ends it in time, the turn goes as fast as it can and carries on.
    A rate too fast to stop within theta brakes at once and overshoots. `turn_at` gives the angle and the rate
    at any time of the plan."""
    w0 = max(0.0, w0)
    if theta <= 1e-12 or w0 * w0 / (2 * a) >= theta:   # nothing to turn, or too fast to stop short: brake now
        return {"theta": max(theta, 0.0), "w0": w0, "a": a, "wp": 0.0, "brake": True}
    w_hi = min(r_cap, math.sqrt(a * theta + 0.5 * w0 * w0))

    def need(wp):
        d1 = abs(wp * wp - w0 * w0) / (2 * a)
        return abs(wp - w0) / a + max(0.0, theta - d1 - wp * wp / (2 * a)) / wp + wp / a

    wp = w_hi
    if need(w_hi) <= T:                      # it ends in time: the slowest cruise rate that still does
        lo = 1e-9
        for _ in range(60):
            mid = 0.5 * (lo + wp)
            if need(mid) <= T:
                wp = mid
            else:
                lo = mid
    return {"theta": theta, "w0": w0, "a": a, "wp": wp, "brake": False}


def turn_at(plan, t):
    """(angle turned, rate, total change of rate) at time t of a turn_plan."""
    theta, w0, a, wp = plan["theta"], plan["w0"], plan["a"], plan["wp"]
    if plan["brake"]:
        # too fast to stop at the target: brake at once, and the turn overshoots it
        tb = w0 / a if a > 0 else 0.0
        if t >= tb:
            return w0 * tb - a * tb * tb / 2, 0.0, w0
        return w0 * t - a * t * t / 2, w0 - a * t, a * t
    t1, d1 = abs(wp - w0) / a, abs(wp * wp - w0 * w0) / (2 * a)
    d2 = max(0.0, theta - d1 - wp * wp / (2 * a))
    t2 = d2 / wp
    sgn = 1.0 if wp >= w0 else -1.0
    if t <= t1:
        return w0 * t + sgn * a * t * t / 2, w0 + sgn * a * t, a * t
    if t <= t1 + t2:
        return d1 + wp * (t - t1), wp, abs(wp - w0)
    tau = min(t - t1 - t2, wp / a)
    return d1 + d2 + wp * tau - a * tau * tau / 2, wp - a * tau, abs(wp - w0) + a * tau


def note_gap(d, gap):
    """Record a separated element's gap to the vehicle: the latest, the largest so far, and the smallest once
    the gap has stopped growing (None while it only grows)."""
    if gap < d["peak"] - 1e-3:
        d["min_gap"] = gap if d["min_gap"] is None else min(d["min_gap"], gap)
    d["peak"], d["gap"] = max(d["peak"], gap), gap


def _vec3(v):
    try:
        out = [float(x) for x in v]
    except (TypeError, ValueError):
        return None
    if len(out) != 3 or not all(math.isfinite(x) for x in out):
        return None
    return out


_IDLE = (np.array([[1.0], [0.0], [0.0]]), np.zeros((3, 1)), 0.0, 0.0)


class SixDof(EntryMixin):
    """The 6-DOF state and models of one Mission. sim.Mission calls it at a few points: each command
    (`command`), each arc (`arc_body`, `begin_arc`, `rotational`, `end_arc`, `coast`, `track`), liftoff,
    separation, observations and telemetry."""

    def __init__(self, mission):
        self.m = mission
        self.inside_of = inside_map(mission.vehicle.elements)
        self.q = None
        self.w = np.zeros(3)
        self.mode, self.target = "dir", None
        self.rate_cmd = np.zeros(3)
        self.torque_cmd = np.zeros(3)
        self.bw = BANDWIDTH
        self.deadband = math.radians(DEADBAND_DEG)
        self.mis = (0.0, 0.0)
        self.arc = None
        self.debris = []
        self.last_thrust = {}
        self.recontact_check = True
        self.rng = np.random.default_rng(None if mission.seed is None else int(mission.seed) + 6006)
        self.sensors = AttitudeSensors(mission.seed, mission.disp.nav_scale)
        self.torque_models = {}
        self._key, self._out = None, None
        self._flows, self._use = [], []
        self._coast = None
        self.gim, self.dthr = {}, {}   # actuator states kept between arcs: gimbal angles, differential throttles
        self._fr = None
        self.frames = None              # set to a list to log every control frame (tests)
        self._entry_init()              # 6-DOF entry, canopy and trim ballast (dynamics/entry.py)
        # propellant slosh (with the Basilisk pendulum, models/slosh/coupled.py; MARSFLIGHT_SLOSH)
        self.slosh = Slosh() if _switch("SLOSH", SLOSH_DEFAULT) else None
        self._slosh_w = 0.0
        # settling (the rule, MARSFLIGHT_SETTLE_RULE): when the last main engine burn ended, and the settle burn now
        self.burn_end_t = mission.t
        self.settle_from, self.settle_accel, self._settle_seen = -1e30, 0.0, -1e30
        self._build_bodies()

    # ------------------------------------------------------------------ Tudat bodies
    def _build_bodies(self):
        from tudatpy.dynamics import environment_setup as es, propagation_setup as ps
        b = self.m.bodies
        ac = es.aerodynamic_coefficients
        for cb in ("Earth", "Mars", "Sun"):
            name = "VT6" + cb
            b.create_empty_body(name)
            b.get(name).mass = 1000.0
            es.add_rigid_body_properties(b, name, es.rigid_body.custom_mass_dependent_rigid_body_properties(
                1000.0, self._com, self._inertia))
            es.add_aerodynamic_coefficient_interface(b, name, ac.custom_aerodynamic_force_coefficients(
                self._coeffs, 1.0, [ac.mach_number_dependent, ac.angle_of_attack_dependent, ac.sideslip_angle_dependent,
                                    ac.time_dependent],
                force_coefficients_frame=ac.negative_body_fixed_frame_coefficients))
            es.add_variable_direction_engine_model(name, "E", ps.thrust.custom_thrust_magnitude(self._thrust_mag, self._isp),
                                                   b, self._thrust_dir)
            self._entry_bodies(b, cb)

    def arc_body(self, central, thrusting, in_atm, use_entry):
        """The 6-DOF body for this arc, or None for a kinematic arc (a coast outside the atmosphere, or a lifting
        entry unless MARSFLIGHT_ENTRY6DOF=1 makes it a rigid-body arc)."""
        if (use_entry and not self.entry6) or not (thrusting or in_atm or use_entry):
            return None
        return "VT6" + central

    def rotational(self, body, integ, term, h, t_start=None, x_start=None):
        """Tudat's rotational propagator for this arc; h is the fixed integrator step, which is also the control frame.
        An arc split where thrust starts continues each later part from the attitude and rates where the last
        part ended (x_start at t_start), with the control frames, actuators and RCS and gimbal records running on."""
        from tudatpy.dynamics import propagation_setup as ps
        if body not in self.torque_models:
            self.torque_models[body] = ps.create_torque_models(
                self.m.bodies, {body: {body: [ps.torque.custom_torque(self._torque)]}}, [body])
        self._key, self._out = None, None
        if x_start is None:
            self._flows, self._use, self._flow_t = [], [], []
            x_start, t_start = np.concatenate([self.q, self.w]), self.m.t
            # the arc may be flown again (to refine a crossing): start each time from the actuators at its start
            self._fr = {"t": self.m.t, "h": h, "cmd": None, "end": None, "hist": [],
                        "a0": {"gim": [x.copy() for x in self.arc["a_init"]["gim"]], "thr": [x.copy() for x in self.arc["a_init"]["thr"]]}}
        else:
            # a later part: the frame running at the split ends there, and frames of this part's step follow
            fr = self._fr
            if fr["cmd"] is not None and t_start > fr["t"] + T_EPS:
                fr["hist"].append((fr["t"], fr["a0"], fr["cmd"]))
                fr["a0"], fr["t"] = self._act_at(t_start), t_start
            fr["cmd"], fr["end"], fr["h"] = None, None, h
        return ps.propagator.rotational(self.torque_models[body], [body], x_start, t_start,
                                        integ, term, ps.propagator.quaternions)

    def step_size(self, in_atm):
        """Integrator step for a 6-DOF arc: short enough for the attitude loop."""
        h = min(0.25 if in_atm else 0.5, 0.6 / (2 * DAMPING * self.bw))
        w = getattr(self, "_slosh_w", 0.0)
        return min(h, 0.5 / w) if w > 0 else h   # RK4 keeps a slosh mode accurate at omega h <= 0.5

    @staticmethod
    def extra_dependent_variables(body, central, in_atm):
        """Inserted before the last dependent variable of a 6-DOF arc: the thrust acceleration (3). In the
        atmosphere the arc already has the sideslip angle (index 8) and the aerodynamic acceleration (9 to 11)."""
        from tudatpy.dynamics import propagation_setup as ps
        return [ps.dependent_variable.single_acceleration(ps.acceleration.thrust_acceleration_type, body, body)]

    # ------------------------------------------------------------------ commands
    def command(self, cmd, want_dir):
        """Read this arc's attitude command and return the body axis (the thrust direction before gimbal)."""
        m = self.m
        if self.q is None:
            x = m.dir if m.dir is not None else (want_dir if want_dir is not None else np.array([1.0, 0.0, 0.0]))
            self.q = R_to_q(frame_with_x(x, [0.0, 0.0, 1.0]))
        att = cmd.get("attitude")
        direct = False
        if att is not None:
            if not isinstance(att, dict):
                m.cmd_errors.append("attitude must be an object")
                att = {}
            if "bandwidth" in att:
                try:
                    self.bw = min(4.0, max(0.2, float(att["bandwidth"])))
                except (TypeError, ValueError):
                    m.cmd_errors.append(f"attitude.bandwidth {att['bandwidth']!r} is not a number")
            if "deadband_deg" in att:
                try:
                    self.deadband = math.radians(min(10.0, max(0.1, float(att["deadband_deg"]))))
                except (TypeError, ValueError):
                    m.cmd_errors.append(f"attitude.deadband_deg {att['deadband_deg']!r} is not a number")
            self._entry_command(att)
            if "torque_Nm" in att:
                v = _vec3(att["torque_Nm"])
                if v is None:
                    m.cmd_errors.append("attitude.torque_Nm must be 3 numbers")
                else:
                    self.mode, self.torque_cmd, direct = "torque", np.array(v), True
            elif "rate_deg_s" in att:
                v = _vec3(att["rate_deg_s"])
                if v is None:
                    m.cmd_errors.append("attitude.rate_deg_s must be 3 numbers")
                else:
                    self.mode, self.rate_cmd, direct = "rate", np.radians(v), True
        if want_dir is not None and not direct:
            self.mode, self.target = "dir", tuple(float(v) for v in want_dir)
        R = q_to_R(self.q)
        if m.disp.pointing_sigma_deg:
            # The per-arc pointing error that 3-DOF adds to the thrust direction (the same random draw):
            # here a thrust misalignment, which also makes a torque.
            p = R.T @ m.disp.rng.normal(0, math.radians(m.disp.pointing_sigma_deg), 3)
            self.mis = (float(p[1]), float(p[2]))
        else:
            self.mis = (0.0, 0.0)
        m.dir = R[:, 0].copy()
        self._note_settle(cmd)
        return m.dir

    def slew_limit(self):
        """The fastest turn the autopilot commands: the 3-DOF slew rate rule."""
        return math.radians(max([s.el.slew_rate for s in self.m.present()
                                 if (s.el.rcs_thrust > 0 or any(en.gimbal_deg > 0 for en, _ in s.el.engines)
                                     or getattr(s.el, "differential_throttle", False))] + [0.5]))

    # ------------------------------------------------------------------ actuators and mass over an arc
    def _rcs_list(self, lay, xc):
        out = []
        for s, x0, x1 in lay:
            e = s.el
            if e.rcs_thrust > 0 and s.rcs > 0:
                R = max(e.diameter / 2, 0.3)
                out.append((s, e.rcs_thrust, max(abs(0.5 * (x0 + x1) - xc) + 0.5 * (x1 - x0), R), R))
        return out

    def begin_arc(self, body, groups, rcs_F, dt, in_atm, aero, rcs_state=None, entry=False):
        """Context of one 6-DOF arc: the mass properties at its start and at its end (after the engines' and the
        commanded RCS's propellant), the actuators and the aerodynamic model."""
        from tudatpy.dynamics.environment_setup.aerodynamic_coefficients import AerodynamicsReferenceFrameAngles as AA
        m = self.m
        lay = layout(m.present(), self.inside_of)
        M0, xc0, Ix0, It0 = mass_properties(mass_items(lay))
        burn = {}
        for g in groups:
            s = g["state"]
            if _transients():   # along the start or shutdown profile
                used = m.group_used(g, m.t, m.t + dt)
            else:
                used = g["mdot"] * max(0.0, m.t + dt - max(m.t, g["t_on"]))
            lf = used / (1 + g["engine"].of)
            f, o = burn.get(s.el.id, (s.fuel, s.ox))
            burn[s.el.id] = (max(0.0, f - lf), max(0.0, o - (used - lf)))
        rcs_left = {}
        if rcs_F > 0 and rcs_state is not None:
            rcs_left[rcs_state.el.id] = max(0.0, rcs_state.rcs - rcs_F / (D.RCS_ISP * D.G0) * dt)
        M1, xc1, Ix1, It1 = mass_properties(mass_items(lay, burn, rcs_left)) if (burn or rcs_left) else (M0, xc0, Ix0, It0)
        pos = {s.el.id: (x0, x1) for s, x0, x1 in lay}
        # per lit group: thrust factor, engine, throttle, start time, gimbal point x, gimbal limit, roll arm, and for
        # differential throttling the engines' places on their ring
        gr, init = [], {"gim": [], "thr": []}
        for g in groups:
            e, en = g["state"].el, g["engine"]
            # A cluster lit in several ignitions is one cluster: each lit batch holds its share of its ring.
            n, lim, nc, at, part = g["n"], math.radians(en.gimbal_deg), g.get("cluster", g["n"]), g.get("ring_at", 0), g.get("part", 0)
            # Only running engines throttle differentially: engines tailing off after a cutoff follow their
            # shutdown curve. A tail-off flown as a differential-throttling group kept its full thrust to the end of the
            # arc, so the vehicle burned propellant its tanks no longer held (sim-robust).
            dt_ok = (bool(getattr(e, "differential_throttle", False)) and nc >= 2 and en.min_throttle < 1.0
                     and g.get("kind", "run") == "run")
            key = (e.id, en.name, part) if part else (e.id, en.name)
            row = {"nf": n * g["factor"], "en": en, "thr": g["throttle"], "t_on": g["t_on"], "n": n,
                   "xg": pos[e.id][0] + nozzle_length(en), "lim": lim, "sd": math.sin(lim) * gimballed_share(nc), "kg": gimballed_share(nc), "rate": gimbal_rate(en),
                   "rr": DT_RING * e.diameter if nc >= 2 else 0.0, "g": g, "key": key, "dt": dt_ok}
            if dt_ok:
                yz = np.array(ring(nc, DT_RING * e.diameter)[at:at + n])
                k = g["factor"] * en.thrust_vac          # torque per unit of throttle, per metre of arm
                row["yz"] = yz.tolist()
                row["A"] = k * np.vstack([yz[:, 1], -yz[:, 0]])   # (pitch, yaw) torque of each engine's throttle change
                row["cap"] = 0.25 * n * k * DT_RING * e.diameter * (1.0 - en.min_throttle)
                off = self.dthr.get(key)
                th = g["throttle"] + (off if off is not None and len(off) == n else np.zeros(n))
                init["thr"].append(np.clip(th, en.min_throttle, 1.0))
            else:
                init["thr"].append(np.full(1, g["throttle"]))
            init["gim"].append(np.clip(self.gim.get(key, np.zeros(3)), -lim, lim))
            gr.append(row)
        self.dthr = {k: v for k, v in self.dthr.items() if any(r["key"] == k and r["dt"] for r in gr)}
        rcs = self._rcs_list(lay, xc0)
        Fr = sum(f for _, f, _, _ in rcs)
        cap = (RCS_SHARE * sum(f * lr for _, f, _, lr in rcs), RCS_SHARE * sum(f * lp for _, f, lp, _ in rcs))
        slender = aero is not None and aero.get("kind") == "slender" and in_atm
        cn = (0.0, 0.0, 0.0)
        if slender:
            pts = normal_force_points(outer_segments(lay, self.inside_of), aero["S"])
            if dist.fins_on():   # fins (MARSFLIGHT_FINS): Barrowman's normal force at their center of pressure
                pts += [dist.fin_point(s.el.spec["fins"], x0, s.el.diameter, aero["S"])
                        for s, x0, _ in lay if isinstance(getattr(s.el, "spec", None), dict) and s.el.spec.get("fins")]
            cn =(sum(c for _, c in pts), sum(c * x for x, c in pts), sum(c * x * x for x, c in pts))
        true_cd = (m.truth.get("aero") or {}).get("stack", {}).get("cd", 1.0)
        # deployed parachutes pull along the airspeed at the top of their carrier (weighted by canopy area)
        chutes = [(s.el.chute_diameter ** 2, x1) for s, _, x1 in lay if s.el.kind == "parachute" and s.el.id in m.chute_deployed]
        chute_x = sum(w * x for w, x in chutes) / sum(w for w, _ in chutes) if (chutes and in_atm) else None
        self.arc = {"body": body, "bobj": m.bodies.get(body), "M": (M0, M1), "xc": (xc0, xc1), "Ix": (Ix0, Ix1),
                    "It": (It0, It1), "groups": gr, "rcs": (Fr, cap[0], cap[1]) if rcs else None, "atm": in_atm,
                    "slender": slender, "cn": cn, "S": aero["S"] if aero else 1.0, "slew": self.slew_limit(),
                    "ca_k": (aero["S"] if aero else 1.0) * m.disp.aero_cd_factor * true_cd, "chutes": bool(m.chute_deployed),
                    "AA": (AA.angle_of_attack, AA.angle_of_sideslip), "chute_x": chute_x, "a_init": init,
                    "trans": _transients()}
        self._entry_begin(lay, xc0, chute_x, in_atm, aero, entry, groups)   # entry, canopy, lateral center of mass
        self._slosh_w = 0.0
        if self.slosh is not None:
            # slosh only where thrust holds the liquid at the bottom of the tanks
            F = sum(g["n"] * g["factor"] * g["engine"].thrust(g["throttle"], 0.0) for g in groups) + rcs_F
            self.slosh.begin_arc(lay, burn, engine_section, F / M0 > A_SETTLED and not self.arc.get("var"), m.t)
            if self.slosh.arc:
                self._slosh_w = self.slosh.max_omega(F / min(M0, M1), M0, xc0, It0)
        self.sensors.begin(m.t, dt, self.sensors.tracker_sees(in_atm, self.w))
        self._key, self._out = None, None

    def _props(self, mass):
        a = self.arc
        M0, M1 = a["M"]
        f = min(1.0, max(0.0, (M0 - mass) / (M0 - M1))) if M0 - M1 > 1e-9 else 0.0
        (x0, x1), (i0, i1), (t0, t1) = a["xc"], a["Ix"], a["It"]
        return x0 + f * (x1 - x0), i0 + f * (i1 - i0), t0 + f * (t1 - t0)

    # ------------------------------------------------------------------ Tudat callbacks
    def _com(self, mass):
        return np.zeros((3, 1))

    def _inertia(self, mass):
        if self.arc is None:
            return np.diag([1000.0, 1000.0, 1000.0])
        _, Ix, It = self._props(mass)
        return np.diag([Ix, It, It])

    def _coeffs(self, iv):
        """Aerodynamic force coefficients times area, body axes (Tudat's negative body-fixed frame)."""
        if self.arc is not None and self.arc.get("var"):
            return self._entry_coeffs(iv)   # a 6-DOF entry or canopy arc
        m = self.m
        a = m.ctl["aero"]
        if a is None:
            return [0.0, 0.0, 0.0]
        mach, al, be, t = float(iv[0]), float(iv[1]), float(iv[2]), float(iv[3])
        arc = self.arc
        if a["kind"] == "slender" and arc is not None and arc["slender"]:
            # Axial force as 3-DOF's zero-angle drag; the normal force replaces 3-DOF's drag due to angle of attack.
            ca = _interp(D.SLENDER_CA_MACH, mach) * arc["ca_k"] + (m._chute_area(mach, t) if arc["chutes"] else 0.0)
            cn = arc["cn"][0] * a["S"]
            return [ca, cn * math.sin(be), cn * math.sin(al) * math.cos(be)]
        # Bluff: drag along the airspeed, no lift.
        drag = m._coeffs_thrust([mach, 0.0, 0.0, t])[0]
        cb = math.cos(be)
        return [drag * math.cos(al) * cb, drag * math.sin(be), drag * math.sin(al) * cb]

    def _thrust_mag(self, t):
        F = self._control(t)[2]
        self.m.ctl["F_now"] = F
        return F

    def _isp(self, t):
        _, _, F, mdot = self._control(t)
        return F / (D.G0 * mdot) if F > 0 and mdot > 0 else 300.0

    def _thrust_dir(self, t):
        return self._control(t)[0]

    def _torque(self, t):
        return self._control(t)[1]

    def _control(self, t):
        """Thrust direction, torque, thrust and propellant flow now, from the actuators' states, the attitude, the
        rates and the mass. Tudat asks several times per integrator stage; the answer is kept until the state
        changes.

        The onboard computer samples the sensors once per control frame (one integrator step) and holds its
        actuator commands through the frame. Tudat's fixed RK4 step evaluates the state at its start, twice at its
        middle and at its end; the end of a step is evaluated again, with the accepted state, as the start of the
        next. So the first evaluation at the end of a frame keeps the frame's commands, and the next one at that
        time starts a new frame. Between commands the actuators move as `actuator` says."""
        a = self.arc
        body = a["bobj"]
        if t != t:
            # Tudat also calls with time NaN when it resets its models after each evaluation: answer with the
            # last evaluation and book nothing. The next call starts a new evaluation, even of the same state: at the
            # end of a step the accepted state can equal the last stage's, and it still starts a new control frame.
            self._key = None
            return self._out if self._out is not None else _IDLE
        try:
            R = body.body_fixed_to_inertial_frame
            w = body.body_fixed_angular_velocity
        except RuntimeError:
            # Tudat asks once while it sets up the propagation, before the body has a rotational state.
            return _IDLE
        if a.get("var"):
            return self._entry_control(t, body, R, w)   # a 6-DOF entry or canopy arc, variable step
        _, vy, vz = np.ravel(w).tolist()   # true rates, for the aerodynamic damping
        # The autopilot steers what the attitude estimate and the gyros say.
        Re = self.sensors.estimate(R, t)
        we = self.sensors.rates(w)
        key = (t, *we.tolist(), Re[0, 0], Re[1, 0], Re[2, 0], Re[0, 1])
        if key == self._key:
            return self._out
        xc, Ix, It = self._props(float(body.mass))
        fc = body.flight_conditions if a["atm"] else None
        p = float(fc.pressure) if fc is not None else 0.0
        fr = self._fr
        args = (t, xc, Ix, It, p, Re, we)
        while fr["hist"] and t < fr["t"] - T_EPS:
            # Tudat flies the last step of an arc again: go back to the frame it starts in
            fr["t"], fr["a0"], fr["cmd"] = fr["hist"].pop()
            fr["end"] = None
        if fr["end"] is not None and t > fr["end"][0] + T_EPS:
            # the accepted state at the end of the last step was the same as its last stage, so Tudat did not
            # ask again: start the frame from that state now
            self._new_frame(fr["end"], commit=True)
        if fr["cmd"] is None or abs(t - fr["t"]) < T_EPS:
            self._new_frame(args)
        elif t > fr["t"] + fr["h"] - T_EPS:
            if fr["end"] is None:      # the last stage of the step: still this frame
                fr["end"] = args
            else:                      # the accepted state: a new frame starts
                self._new_frame(args, commit=True)
        act = self._act_at(t)
        # thrust of each lit group at its engines' throttles, and the lateral force of its gimbal and of misalignment
        my, mz = self.mis
        Fy = Fz = ty = tz = tr = Ftot = mdot = 0.0
        use = 0.0
        lat_y, lat_z = a.get("lat", (0.0, 0.0))   # a center of mass off the axis (MARSFLIGHT_ENTRY6DOF)
        for g, gm, th in zip(a["groups"], act["gim"], act["thr"]):
            if not self.m._lit(g, t):   # on or off for a whole part of a split arc (Mission._lit)
                continue
            en = g["en"]
            if g["dt"]:
                # each engine at its own throttle, scaled along the group's start curve as the plain branch is
                ku, kf = self._profile_scale(g, t, p)
                fe = [ku * g["nf"] / g["n"] * en.thrust(x, p) for x in th]
                f = sum(fe)
                for (ey, ez), fi in zip(g["yz"], fe):
                    ty += ez * fi
                    tz -= ey * fi
                flow = kf * g["g"]["mdot"] * sum(_flow_ratio(en, x, g["thr"]) for x in th) / g["n"]
            else:
                if a["trans"]:
                    # along the group's start or shutdown profile, at its mixture ratio
                    f = self.m.group_thrust(g["g"], t, p)
                    flow = self.m.group_flow(g["g"], t)
                else:
                    f = g["nf"] * en.thrust(g["thr"], p)
                    flow = g["g"]["mdot"]
            # only the gimballed engines of the cluster turn (a fixed center engine)
            sy, sz, sr = math.sin(gm[0]) * g["kg"], math.sin(gm[1]) * g["kg"], math.sin(gm[2]) * g["kg"]
            fy, fz = f * (-sz + my), f * (sy + mz)
            ty += (xc - g["xg"]) * fz
            tz -= (xc - g["xg"]) * fy
            tr += f * sr * g["rr"]
            if lat_y or lat_z:
                # thrust along the axis misses a center of mass that sits off it
                ty -= lat_z * f
                tz += lat_y * f
                tr += lat_z * fy - lat_y * fz
            Fy, Fz, Ftot, mdot = Fy + fy, Fz + fz, Ftot + f, mdot + flow
            if g["lim"] > 0:
                use = max(use, math.hypot(gm[0], gm[1]) / g["lim"])
        rcs_F = self.m.ctl["rcs"]
        Fx = Ftot + rcs_F
        n = math.sqrt(Fx * Fx + Fy * Fy + Fz * Fz)
        d = np.array([[Fx / n], [Fy / n], [Fz / n]]) if n > 0 else np.array([[1.0], [0.0], [0.0]])
        # aerodynamic moment of the normal force about the center of mass, with pitch damping, and the pull of
        # deployed parachutes (along the airspeed, at the top of their carrier)
        tay = taz = 0.0
        fax = fay = faz = 0.0   # aerodynamic force, body axes (for the slosh model)
        if a["slender"] or a["chute_x"] is not None:
            qd, V = float(fc.dynamic_pressure), float(fc.airspeed)
            if qd > 0 and V > 1.0:
                ang = fc.aerodynamic_angle_calculator
                al, be = ang.get_angle(a["AA"][0]), ang.get_angle(a["AA"][1])
                if a["slender"]:
                    S0, S1, S2 = a["cn"]
                    K1, K2 = S1 - xc * S0, S2 - 2 * xc * S1 + xc * xc * S0
                    qs = qd * a["S"]
                    tay += qs * (K1 * math.sin(al) * math.cos(be) - K2 * vy / V)
                    taz += qs * (-K1 * math.sin(be) - K2 * vz / V)
                    if self.slosh is not None:
                        fax = -qd * _interp(D.SLENDER_CA_MACH, float(fc.mach_number)) * a["ca_k"]
                        fay, faz = -qs * S0 * math.sin(be), -qs * S0 * math.sin(al) * math.cos(be)
                if a["chute_x"] is not None:
                    qa = qd * self.m._chute_area(float(fc.mach_number), t) * (a["chute_x"] - xc)
                    tay += qa * math.sin(al) * math.cos(be)
                    taz -= qa * math.sin(be)
        rc = fr["cmd"]["rcs"]
        tq = np.array([[tr + rc[0]], [ty + rc[1] + tay], [tz + rc[2] + taz]])
        if self.slosh is not None and self.slosh.arc and not a.get("var"):
            # propellant slosh: the swinging slosh masses turn the vehicle (models/slosh/coupled.py)
            M = float(body.mass)
            M0, M1 = a["M"]
            f = min(1.0, max(0.0, (M0 - M) / (M0 - M1))) if M0 - M1 > 1e-9 else 0.0
            sy, sz = self.slosh.torque(t, fr["h"], (f, (Fx + fax) / M, xc, Ix, It, M, np.ravel(w).tolist(),
                                                    (Fy + fay, Fz + faz), np.ravel(tq).tolist()))
            tq[1, 0] += sy
            tq[2, 0] += sz
        if rcs_F:
            mdot += rcs_F / (D.RCS_ISP * D.G0)
        self._key, self._out = key, (d, tq, Fx, mdot)
        self._flows.append(fr["cmd"]["flow"])
        self._use.append(use)
        return self._out

    def _profile_scale(self, g, t, p):
        """(thrust, flow) of a differential-throttling group at time t over the same at its commanded throttle in
        steady running: both 1 without the transients, and along its start curve with them, so that its engines
        give what Mission.group_thrust and group_flow give for the group at equal throttles."""
        if not self.arc["trans"] or "profile" not in g["g"]:
            return 1.0, 1.0
        full = g["nf"] * g["en"].thrust(g["thr"], p)
        ku = self.m.group_thrust(g["g"], t, p) / full if full > 0 else 0.0
        kf = self.m.group_flow(g["g"], t) / g["g"]["mdot"] if g["g"]["mdot"] > 0 else 0.0
        return ku, kf

    def _new_frame(self, args, commit=False):
        """Start a control frame at time args[0] (commit: the previous frame ends there), with the commands for
        the state in args."""
        fr, t = self._fr, args[0]
        if commit:
            a0 = self._act_at(t)
            fr["hist"].append((fr["t"], fr["a0"], fr["cmd"]))
            fr["a0"], fr["t"] = a0, t
        fr["end"] = None
        fr["cmd"] = c = self._command(*args)
        if self.frames is not None:
            self.frames.append({"t": t, "h": fr["h"], "rcs": c["rcs"].tolist(), "rcs_on": c["on"].tolist(), "gim_cmd": [x.tolist() for x in c["gim"]],
                                "gim": [x.tolist() for x in fr["a0"]["gim"]], "thr": [x.tolist() for x in fr["a0"]["thr"]],
                                "thr_cmd": [x.tolist() for x in c["thr"]], "I": [args[2], args[3]], "w": np.asarray(args[6]).tolist(), "x_axis": args[5][:, 0].tolist()})

    def _command(self, t, xc, Ix, It, p, Re, we):
        """The actuator commands for one control frame: the torque the attitude loop wants, from the engine gimbals
        first (pitch and yaw, then roll from what is left), then from differential throttling, then from the RCS
        in pulses."""
        a = self.arc
        (r00, r01, r02), (r10, r11, r12), (r20, r21, r22) = Re.tolist()
        wx, wy, wz = we.tolist()
        lit = [self.m._lit(g, t) for g in a["groups"]]
        T_py = T_roll = 0.0
        for g, on in zip(a["groups"], lit):
            if on:
                f = self.m.group_thrust(g["g"], t, p) if a["trans"] else g["nf"] * g["en"].thrust(g["thr"], p)
                T_py += f * g["sd"] * (xc - g["xg"])
                T_roll += f * g["sd"] * g["rr"]
        dt_cap = sum(g["cap"] for g, on in zip(a["groups"], lit) if on and g["dt"])
        # torque demand (body axes)
        if self.mode == "torque":
            td = [float(v) for v in self.torque_cmd]
        else:
            wcx = wcy = wcz = 0.0
            if self.mode == "rate":
                wcx, wcy, wcz = (float(v) for v in self.rate_cmd)
            elif self.target is not None:
                tx, ty_, tz_ = self.target
                # rotation axis from the body axis to the target, in inertial then in body axes
                cx, cy, cz = r10 * tz_ - r20 * ty_, r20 * tx - r00 * tz_, r00 * ty_ - r10 * tx
                s = math.sqrt(cx * cx + cy * cy + cz * cz)
                co = r00 * tx + r10 * ty_ + r20 * tz_
                ang = math.atan2(s, co)
                if s > 1e-9:
                    aby, abz = (r01 * cx + r11 * cy + r21 * cz) / s, (r02 * cx + r12 * cy + r22 * cz) / s
                else:
                    aby, abz = (1.0, 0.0) if co < 0 else (0.0, 0.0)
                k = self.bw / (2 * DAMPING) * ang
                wcy, wcz = k * aby, k * abz
                # No faster than the slew rate, nor than the actuators can stop within the angle left.
                acc = (abs(T_py) + dt_cap + (a["rcs"][2] if a["rcs"] else 0.0)) / It
                # The torque does not reverse at once: a deflected gimbal swings to the other side at its rate
                # limit, the throttles lag, and the computer holds its commands for a frame. Stopping from rate w
                # after a delay td takes w td + w^2 / (2 acc) of angle.
                td = 0.5 * self._fr["h"]
                if abs(T_py) > 1e-6:
                    td += max(2.0 * math.hypot(x[0], x[1]) / g["rate"] for x, g, on in zip(self._fr["a0"]["gim"], a["groups"], lit) if on)
                    td += GIMBAL_TAU_S
                elif dt_cap > 0:
                    td += THROTTLE_TAU_S
                lim = min(a["slew"], 0.8 * acc * (math.sqrt(td * td + 2.0 * ang / acc) - td)) if acc > 0 else 0.0
                n = math.hypot(wcy, wcz)
                if n > lim:
                    wcy, wcz = wcy * lim / n, wcz * lim / n
            kr = 2 * DAMPING * self.bw
            td = [Ix * kr * (wcx - wx), It * kr * (wcy - wy) + (Ix - It) * wx * wz, It * kr * (wcz - wz) + (It - Ix) * wx * wy]
        lat_y, lat_z = a.get("lat", (0.0, 0.0))
        if (lat_y or lat_z) and self.mode != "torque":
            # the autopilot trims the gimbals for the torque of thrust past a center of mass off the axis
            Fsum = sum(g["nf"] * g["en"].thrust(g["thr"], p) for g, on in zip(a["groups"], lit) if on)
            td = [td[0], td[1] + lat_z * Fsum, td[2] - lat_y * Fsum]
        # engine gimbal: the same deflection on every gimballed engine, pitch and yaw first, roll from what is left
        uy = uz = ur = 0.0
        if abs(T_py) > 1e-6:
            uy, uz = td[1] / T_py, td[2] / T_py
            n = math.hypot(uy, uz)
            if n > 1.0:
                uy, uz = uy / n, uz / n
        if T_roll > 1e-6:
            lim = math.sqrt(max(0.0, 1.0 - uy * uy - uz * uz))
            ur = min(lim, max(-lim, td[0] / T_roll))
        gim = [np.array([uy, uz, ur]) * g["lim"] for g in a["groups"]]
        rem = np.array([td[1] - T_py * uy, td[2] - T_py * uz])
        # differential throttling of fixed or gimballed clusters, for the pitch and yaw torque the gimbals leave
        thr = [np.full(len(x), g["thr"]) for g, x in zip(a["groups"], a["a_init"]["thr"])]
        dts = [i for i, (g, on) in enumerate(zip(a["groups"], lit)) if on and g["dt"]]
        if dts and float(np.hypot(*rem)) > 1e-6:
            A = np.hstack([a["groups"][i]["A"] for i in dts])
            dx = np.linalg.pinv(A) @ rem
            parts, j = [], 0
            for i in dts:
                parts.append(dx[j:j + a["groups"][i]["n"]])
                j += a["groups"][i]["n"]
            s = 1.0
            for i, dg in zip(dts, parts):
                g = a["groups"][i]
                span = float(dg.max() - dg.min())
                if span > 1e-12:
                    s = min(s, (1.0 - g["en"].min_throttle) / span)
            for i, dg in zip(dts, parts):
                g = a["groups"][i]
                dg = s * dg
                # a common shift gives no torque: keep the throttle the flight software set where the range allows
                c = min(max(0.0, g["en"].min_throttle - g["thr"] - float(dg.min())), 1.0 - g["thr"] - float(dg.max()))
                thr[i] = np.clip(g["thr"] + dg + c, g["en"].min_throttle, 1.0)
            rem = rem - s * (A @ dx)
        # RCS takes the rest, within its torque, in pulses no shorter than RCS_MIN_ON_S
        on = np.zeros(3)
        rcs = np.zeros(3)
        flow = 0.0
        if a["rcs"]:
            Fr, cr, cp = a["rcs"]
            caps = np.array([cr, cp, cp])
            want = np.array([td[0] - T_roll * ur, rem[0], rem[1]])
            on = np.array([rcs_pulse(x / c, self._fr["h"]) for x, c in zip(want, caps)])
            rcs = on * caps
            flow = float(np.sum(np.abs(on))) * RCS_SHARE * Fr / (D.RCS_ISP * D.G0)
        return {"gim": gim, "thr": thr, "rcs": rcs, "on": on, "flow": flow}

    def _throttle_refund(self, t_end):
        """Per group, the propellant (kg) that differential throttling saved against the commanded throttle up to
        t_end, from each engine's throttle through every control frame (the lag and the rate limit integrated exactly)."""
        fr = self._fr
        frames = fr["hist"] + [(fr["t"], fr["a0"], fr["cmd"])]
        out = [0.0] * len(self.arc["groups"])
        for k, (t0, a0, cmd) in enumerate(frames):
            t1 = frames[k + 1][0] if k + 1 < len(frames) else t_end
            if cmd is None or t1 <= t0:
                continue
            for i, g in enumerate(self.arc["groups"]):
                s0, s1 = max(0.0, g["t_on"] - t0), t1 - t0
                if not g["dt"] or s1 <= s0:
                    continue
                mean = actuator_mean(a0["thr"][i], cmd["thr"][i], s0, s1, THROTTLE_TAU_S, THROTTLE_RATE_PER_S)
                ratio = sum(_flow_ratio(g["en"], x, g["thr"]) for x in mean) / g["n"]
                # the flow at the commanded throttle over the frame, along the start curve with the start and shutdown transients
                out[i] += self.m.group_used(g["g"], t0 + s0, t0 + s1) * (1.0 - ratio)
        return out

    def _act_at(self, t):
        """The actuators at time t of the current frame: gimbal angles (pitch, yaw, roll) and engine throttles."""
        fr = self._fr
        c, a0, dt = fr["cmd"], fr["a0"], max(0.0, t - fr["t"])
        if c is None:
            return a0
        gr = self.arc["groups"]
        return {"gim": [actuator(x, xc, dt, GIMBAL_TAU_S, g["rate"]) for g, x, xc in zip(gr, a0["gim"], c["gim"])],
                "thr": [actuator(x, xc, dt, THROTTLE_TAU_S, THROTTLE_RATE_PER_S) if g["dt"] else xc
                        for g, x, xc in zip(self.arc["groups"], a0["thr"], c["thr"])]}

    # ------------------------------------------------------------------ after an arc
    def end_arc(self, sh, dvh, ts, k0, in_atm):
        """Take the attitude from a 6-DOF arc, charge the RCS for attitude control, and return the sensed
        (non-gravitational) acceleration at its end."""
        x = np.asarray(sh[ts[-1]], float)
        k = self._entry_end(x)   # 6, or 12 with a parachute canopy flown as its own body
        q, w = x[k:k + 4], x[k + 4:k + 7]
        big = float(np.max(np.abs(q))) if np.all(np.isfinite(q)) else 0.0
        if not (big > 0 and np.all(np.isfinite(w))):
            # A diverged attitude would leave the thrust direction undefined, and every later arc would fly nowhere.
            self.m.fail(f"the vehicle's attitude state became invalid (not a finite number) at mission time "
                        f"{ts[-1] - self.m.t0:.1f} s: the rigid-body attitude dynamics diverged")
        q = q / big   # scaled first: the norm of a quaternion of 1e200s overflows
        self.q = q / np.linalg.norm(q)
        self.w = w.copy()
        self.sensors.end(ts[-1])
        self.m.dir = q_to_R(self.q)[:, 0].copy()
        span = ts[-1] - ts[0]
        if self._flows and span > 0:
            self._charge_rcs(self._entry_rcs_kg(span), ts[-1])
        if self._fr is not None and self._fr["cmd"] is not None and span > 0:
            # sim.py booked each group's flow at its commanded throttle: give back what differential throttling saved
            for g, kg in zip(self.arc["groups"], self._throttle_refund(ts[-1])):
                if kg == 0.0:
                    continue
                s, of = g["g"]["state"], g["en"].of
                s.fuel = max(0.0, s.fuel + kg / (1 + of))
                s.ox = max(0.0, s.ox + kg * of / (1 + of))
        if self._fr is not None and self._fr["cmd"] is not None:
            act = self._act_at(ts[-1])
            for g, gm, th in zip(self.arc["groups"], act["gim"], act["thr"]):
                self.gim[g["key"]] = np.asarray(gm, float).copy()
                if g["dt"]:
                    self.dthr[g["key"]] = np.asarray(th, float) - g["thr"]
        self.gimbal_use = max(self._use) if self._use else 0.0
        self.note_thrust([g["g"] for g in self.arc["groups"]], float(ts[-1]))
        if self.arc["groups"]:
            self.burn_end_t = float(ts[-1])
        if self.slosh is not None:
            for (eid, role), _, wave, lim in self.slosh.end_arc(float(ts[-1])):
                if (eid, role) not in self.slosh.breaking:
                    self.slosh.breaking.add((eid, role))
                    self.m.event("slosh_wave_breaking", element=eid, tank=role, wave_m=round(wave, 3), radius_m=round(lim, 3))
        d = dvh[ts[-1]]
        acc = np.array(d[k0:k0 + 3], float)
        if in_atm:
            acc = acc + np.array(d[9:12], float)
        return acc

    def _charge_rcs(self, kg, t):
        """Take kg of attitude-control propellant from the RCS, and log its impulse as an RCS exposure at time t.
        A powered arc can ask for more than is left: its integrator fired the RCS for all of it, so the rest is
        charged to the largest RCS."""
        rcs = sorted((s for s in self.m.present() if s.el.rcs_thrust > 0), key=lambda s: -s.el.rcs_thrust)
        for s in rcs:
            take = min(kg, s.rcs)
            s.rcs -= take
            kg -= take
            if take > 0:
                self.m.exposures.append((t, "rcs", s.el.id, take * D.RCS_ISP * D.G0 / 1e3))
            if kg <= 0:
                break
        if kg > 1e-9 and rcs:
            self.m.exposures.append((t, "rcs", rcs[0].el.id, kg * D.RCS_ISP * D.G0 / 1e3))

    def coast(self, dt, entry=False, x_end=None):
        """The attitude, and its onboard estimate, over an arc that is not propagated in 6-DOF."""
        self.sensors.begin(self.m.t, dt, self.sensors.tracker_sees(entry, self.w))
        if self.slosh is not None:
            self.slosh.coast()
        self._coast_arc(dt, entry, x_end)
        self.sensors.end(self.m.t + dt)

    def _coast_arc(self, dt, entry=False, x_end=None):
        """The attitude over an arc that is not propagated in 6-DOF. A lifting entry flies trimmed with its
        heatshield (the bottom of the stack) into the airflow. Outside the atmosphere the RCS turns the vehicle
        towards `dir` with the torque it has (see turn_plan), and pays propellant for each change of rate.
        Without RCS the vehicle keeps turning at its body rates."""
        self._coast = {"t0": self.m.t, "q0": None if self.q is None else self.q.copy(), "w": self.w.copy(), "kind": "free"}
        if entry:
            self._coast["kind"] = "entry"
            if x_end is not None and self.q is not None:
                air = np.asarray(x_end[3:6], float) - self.m.omega_cross(np.asarray(x_end[:3], float))
                R = q_to_R(self.q)
                tgt = -_unit(air)
                c = _cross(R[:, 0], tgt)
                ang = math.atan2(float(np.linalg.norm(c)), float(np.dot(R[:, 0], tgt)))
                if ang > 1e-9:
                    axis = R.T @ (c / np.linalg.norm(c)) if np.linalg.norm(c) > 1e-12 else np.array([0.0, 1.0, 0.0])
                    self.q = turn(self.q, axis * ang, 1.0)
                self.m.dir = q_to_R(self.q)[:, 0].copy()
            self._coast.update(q1=None if self.q is None else self.q.copy(), dt=dt)
            self.w = np.zeros(3)
            return
        if dt <= 0 or self.q is None:
            return
        lay = layout(self.m.present(), self.inside_of)
        _, xc, Ix, It = mass_properties(mass_items(lay))
        rcs = self._rcs_list(lay, xc)
        if not rcs:
            self.q = turn(self.q, self.w, dt)
            self.m.dir = q_to_R(self.q)[:, 0].copy()
            return
        Fsum = sum(f for _, f, _, _ in rcs)
        lp = sum(f * lev for _, f, lev, _ in rcs) / Fsum
        lr = sum(f * lev for _, f, _, lev in rcs) / Fsum
        avail = sum(s.rcs for s, _, _, _ in rcs)
        isp = D.RCS_ISP * D.G0

        def cost(dw):   # propellant for a change of body rate dw: angular impulse / lever arm / exhaust speed
            return (Ix * abs(dw[0]) / lr + It * math.hypot(dw[1], dw[2]) / lp) / isp

        if self.mode == "torque":
            cap = np.array([RCS_SHARE * Fsum * lr, RCS_SHARE * Fsum * lp, RCS_SHARE * Fsum * lp])
            tq = np.clip(self.torque_cmd, -cap, cap)
            burn = float(np.sum(np.abs(tq) / cap)) * RCS_SHARE * Fsum / isp * dt
            frac = min(1.0, avail / burn) if burn > 0 else 1.0
            w1 = self.w + frac * tq / np.array([Ix, It, It]) * dt
            self._coast.update(kind="torque", dw_dt=(w1 - self.w) / dt)
            self.q = turn(self.q, 0.5 * (self.w + w1), dt)
            self.w = w1
            self._charge_rcs(frac * burn, self.m.t + dt)
            self.m.dir = q_to_R(self.q)[:, 0].copy()
            return
        if self.mode == "rate":
            goal = self.rate_cmd - self.sensors.bias   # the rate loop holds what the gyros read
            # in whole shortest pulses: what is left is less than half the rate change of one pulse
            dw = np.array([RCS_SHARE * Fsum * lr / Ix, RCS_SHARE * Fsum * lp / It, RCS_SHARE * Fsum * lp / It]) * rcs_bit()
            step = np.round((goal - self.w) / dw) * dw if RCS_MIN_ON_S > 0 else goal - self.w
            need = cost(step)
            frac = min(1.0, avail / need) if need > 0 else 1.0
            self.w = self.w + frac * step
            self._coast.update(w=self.w.copy())
            self.q = turn(self.q, self.w, dt)
            self._charge_rcs(frac * need, self.m.t + dt)
            self.m.dir = q_to_R(self.q)[:, 0].copy()
            return
        # The RCS speeds the turn up and stops it with its torque, no faster than the slew rate and as slowly as
        # still ends the turn within the arc. A turn that cannot end in time carries on into the next arc. Rates
        # across the turn axis (and away from the target) are stopped first, which takes time at the RCS's
        # torque; the turn is planned over what is left of the arc. Propellant pays for every change of rate.
        a = RCS_SHARE * Fsum * lp / It
        cap = np.array([RCS_SHARE * Fsum * lr, RCS_SHARE * Fsum * lp, RCS_SHARE * Fsum * lp])
        ang, axis, R = self._aim(self.m.t)
        # Within the deadband the RCS holds the attitude in a limit cycle: the shortest pulse reverses the rate at
        # each edge, so the vehicle swings across the deadband at half the rate change of one pulse.
        dw = cap * rcs_bit() / np.array([Ix, It, It])
        if ang <= self.deadband and np.all(np.abs(self.w) <= dw * (0.5 + 1e-9)):
            self._hold(dt, axis, ang, R, dw, Fsum, avail)
            return
        w_ax = float(np.dot(self.w, axis)) if ang > 1e-9 else 0.0
        w0 = max(0.0, w_ax)
        w_off = self.w - w0 * axis
        t_stop = max(Ix * abs(w_off[0]) / cap[0], It * math.hypot(w_off[1], w_off[2]) / cap[1])
        stop = 0.0
        if t_stop > 1e-6:
            stop = min(t_stop, dt)
            w_s = self.w - stop / t_stop * w_off
            need = cost(self.w - w_s)
            if need > avail:   # the RCS runs dry: the vehicle keeps turning as it was
                self.q = turn(self.q, self.w, dt)
                self._charge_rcs(avail, self.m.t + dt)
                self.m.dir = q_to_R(self.q)[:, 0].copy()
                return
            pre = {"t0": self.m.t, "q0": self.q.copy(), "w0": self.w.copy(), "dw_dt": (w_s - self.w) / stop, "T": stop}
            self.q = turn(self.q, 0.5 * (self.w + w_s), stop)
            self.w = w_s
            avail -= need
            self._charge_rcs(need, self.m.t + dt)
            self._coast.update(t0=self.m.t + stop, q0=self.q.copy(), w=self.w.copy(), pre=pre)
            if stop >= dt:
                self.m.dir = q_to_R(self.q)[:, 0].copy()
                return
            ang, axis, R = self._aim(self.m.t + stop)
            w0 = max(0.0, float(np.dot(self.w, axis))) if ang > 1e-9 else 0.0
        plan = turn_plan(ang, w0, a, self.slew_limit(), dt - stop)
        turned, w_end, dv = turn_at(plan, dt - stop)
        need = cost(self.w - w0 * axis) + It * dv / lp / isp
        if need > avail:   # the RCS runs dry: the vehicle keeps turning as it was
            self.q = turn(self.q, self.w, dt - stop)
            self._charge_rcs(avail, self.m.t + dt)
        else:
            self._coast.update(kind="turn", axis=axis, plan=plan)
            self.q = turn(self.q, axis * turned, 1.0)
            if self.sensors.scale:
                # the turn ends where the estimate meets the target, so the true attitude is off by the error then
                est = q_to_R(turn(R_to_q(R), axis * turned, 1.0))
                self.q = R_to_q(est @ rotation(-self.sensors.error(self.m.t + dt)))
            self.w = axis * w_end
            self._charge_rcs(need, self.m.t + dt)
        self.m.dir = q_to_R(self.q)[:, 0].copy()

    # vents in a coast hold: [(thrust N, misalignment deg, arm m, offset m)] per element id, from its spec
    def _disturbance_cost(self, dt, R, ang, axis):
        """RCS propellant (kg) to cancel the coast disturbance torques over a hold of dt (MARSFLIGHT_COAST_DISTURBANCES): gravity gradient at the hold's mean attitude, solar radiation pressure
        and vents (dynamics/disturbances.py)."""
        m = self.m
        lay = layout(m.present(), self.inside_of)
        _, xc, Ix, It = mass_properties(mass_items(lay))
        rcs = self._rcs_list(lay, xc)
        if not rcs:
            return 0.0
        Fsum = sum(f for _, f, _, _ in rcs)
        lp = sum(f * lev for _, f, lev, _ in rcs) / Fsum
        lr = sum(f * lev for _, f, _, lev in rcs) / Fsum
        tgt = q_to_R(turn(R_to_q(R), axis * ang, 1.0)) if ang > 1e-12 else R
        r_body = tgt.T @ np.asarray(m.x[:3], float)
        top = max([x1 for _, _, x1 in lay] + [0.0])
        dmax = max([s.el.diameter for s, _, _ in lay if s.el.kind != "parachute"] + [0.5])
        vents = []
        for s, x0, x1 in lay:
            for v in (getattr(s.el, "spec", None) or {}).get("vents", []) if isinstance(getattr(s.el, "spec", None), dict) else []:
                vents.append((float(v.get("thrust_N", 0.0)), float(v.get("misalign_deg", 0.0)),
                              abs(x1 - xc), float(v.get("offset_m", 0.0))))
        # solar pressure on the side of the stack; the center of pressure at mid length
        srp = (top * dmax, abs(0.5 * top - xc), dist.AU_M)
        tq = dist.coast_torque(m.gm(m.central), r_body, Ix, It, vents, srp)
        return (dist.hold_propellant(tq[0], dt, lr, D.RCS_ISP) + dist.hold_propellant(math.hypot(tq[1], tq[2]), dt, lp, D.RCS_ISP))

    def _hold(self, dt, axis, ang, R, dw, Fsum, avail):
        """A coast arc held in the deadband: a limit cycle about each axis, at half the rate change of one shortest
        pulse, with one pulse at each edge. The direction swings about the target along the axis of the error."""
        db = self.deadband
        w_lc = 0.5 * dw
        pulses = dt * w_lc / (2.0 * db)       # per axis: one pulse at each edge, two in each period of 4 db / w_lc
        need = float(np.sum(pulses)) * RCS_SHARE * Fsum * rcs_bit() / (D.RCS_ISP * D.G0)
        if dist.coast_on():
            need += self._disturbance_cost(dt, R, ang, axis)
        if need > avail:   # the RCS runs dry: the vehicle keeps turning as it was
            self.q = turn(self.q, self.w, dt)
            self._charge_rcs(avail, self.m.t + dt)
            self.m.dir = q_to_R(self.q)[:, 0].copy()
            return
        self._charge_rcs(need, self.m.t + dt)
        if ang < 1e-12:
            axis = np.array([0.0, 0.0, 1.0])
        tgt = q_to_R(turn(R_to_q(R), axis * ang, 1.0))   # the estimate turned onto the target
        # phase of the swing (a triangle wave of amplitude db) from where the error is now, moving towards the target
        P = 4.0 * db / w_lc[1]
        ph0 = (db - ang) / (4.0 * db)

        def off(tau):
            u = (ph0 + tau / P) % 1.0
            return db * (1.0 - 4.0 * u if u < 0.5 else 4.0 * u - 3.0)

        def rate(tau):
            u = (ph0 + tau / P) % 1.0
            return -w_lc[1] if u < 0.5 else w_lc[1]
        q_t = R_to_q(tgt)
        self._coast.update(kind="hold", q0=q_t, axis=-axis, off=off)
        est = q_to_R(turn(q_t, -axis * off(dt), 1.0))
        self.q = R_to_q(est @ rotation(-self.sensors.error(self.m.t + dt))) if self.sensors.scale else R_to_q(est)
        self.w = -axis * rate(dt)
        self.m.dir = q_to_R(self.q)[:, 0].copy()

    def _aim(self, t):
        """The angle and the body axis of the turn from the attitude estimate at time t to the target, and the
        estimate."""
        ang, axis = 0.0, np.zeros(3)
        R = self.sensors.estimate(q_to_R(self.q), t)   # the turn is planned from the attitude estimate
        if self.target is not None:
            c = np.cross(R[:, 0], self.target)
            ang = math.atan2(float(np.linalg.norm(c)), float(np.dot(R[:, 0], self.target)))
            axis = R.T @ (c / np.linalg.norm(c)) if np.linalg.norm(c) > 1e-12 else np.array([0.0, 1.0, 0.0])
        return ang, axis, R

    # ------------------------------------------------------------------ liftoff, separation, recontact
    def on_liftoff(self):
        """On the ground the vehicle stands upright and turns with the planet."""
        m = self.m
        r = m.x[:3]
        if m.central == "Earth":
            f = 1 / 298.257223563
            up = _unit(np.array([r[0], r[1], r[2] / (1 - f) ** 2]))
        else:
            up = _unit(r)
        R = frame_with_x(up, np.cross([0.0, 0.0, 1.0], up))
        self.q = R_to_q(R)
        wpl = {"Earth": 7.2921159e-5, "Mars": 7.088218e-5}.get(m.central, 0.0)
        axis = np.array(m.bodies.get(m.central).rotation_model.inertial_to_body_fixed_rotation(m.t)).T @ np.array([0, 0, 1.0])
        self.w = R.T @ (wpl * axis)
        m.dir = up
        self.sensors.align()

    def layout_now(self):
        return layout(self.m.present(), self.inside_of)

    def separated(self, lay, removed):
        """Start tracking what separated, relative to the vehicle. Pushes both apart at SEP_PUSH, gives the
        vehicle a tip-off rate in dispersed worlds, and keeps the thrust of a stage whose engines still burn
        (separating does not shut them down) until its propellant runs out."""
        m = self.m
        if m.mode != "flight" or m.x is None or self.q is None:
            return
        rem = [row for row in lay if row[0].el.id in removed]
        keep = [row for row in lay if row[0].el.id not in removed]
        if not rem or not keep or all(r[0].el.kind in ("fairing", "parachute") for r in rem):
            return    # fairing halves are pushed sideways, clear of the stack
        Md, xcd, _, Itd = mass_properties(mass_items(rem))
        Mv, xcv, _, Itv = mass_properties(mass_items(keep))
        body_rows = [r for r in keep if r[0].el.kind != "parachute"] or keep
        v_lo, v_hi = min(r[1] for r in body_rows), max(r[2] for r in body_rows)
        d_lo, d_hi = min(r[1] for r in rem), max(r[2] for r in rem)
        tol = 1e-6
        if d_hi <= v_lo + tol:                    # below the vehicle (a spent stage, a heatshield)
            above, face_d, face_v = False, d_hi, v_lo
            near_v = min(body_rows, key=lambda r: r[1])[0].el
        elif d_lo >= v_hi - tol or (d_lo <= v_lo + tol and d_hi >= v_hi - tol):
            # above the vehicle, or a backshell over it: the vehicle leaves through the open bottom
            above, face_d, face_v = True, (d_lo if d_lo >= v_hi - tol else d_hi), v_hi
            near_v = max(body_rows, key=lambda r: r[2])[0].el
        else:
            return    # from the middle of the stack (a mass with no place of its own): nothing to track
        R = q_to_R(self.q)
        x_ax = R[:, 0]
        w_i = R @ self.w
        r_rel = (xcd - xcv) * x_ax
        n = x_ax if above else -x_ax
        v_rel = _cross(w_i, r_rel) + SEP_PUSH * n
        m.x[3:] -= SEP_PUSH * Md / (Md + Mv) * n
        wd = w_i.copy()
        if m.seed is not None:
            dw = np.radians(self.rng.normal(0.0, TIPOFF_DEG_S, 2))
            self.w = self.w + np.array([0.0, dw[0], dw[1]])
            wd = wd - R @ np.array([0.0, dw[0], dw[1]]) * Itv / max(Itd, 1e-6)
        # A stage still burns if its engines are lit and it has propellant: a burnout leaves rounding crumbs.
        burning = [r[0] for r in rem if r[0].el.is_stage and r[0].lit and min(r[0].fuel, r[0].ox) > 1.0]
        F0 = sum(self.last_thrust.get(s.el.id, (0.0, 0.0, 0.0))[0] for s in burning)
        mdot = sum(self.last_thrust.get(s.el.id, (0.0, 0.0, 0.0))[1] for s in burning)
        t_burn = sum(s.fuel + s.ox for s in burning) / mdot if mdot > 0 else 0.0
        # A stage shut down (or burnt out) when its last powered arc ended still pushes while its thrust tails off.
        tail = [(self.last_thrust[s.el.id][0], self.last_thrust[s.el.id][2]) for s in (r[0] for r in rem)
                if s.el.is_stage and s not in burning and s.el.id in self.last_thrust]
        chutes = [r[0].el for r in rem if r[0].el.kind == "parachute" and r[0].el.id in m.chute_deployed]
        dmax = max(r[0].el.diameter for r in rem if r[0].el.kind != "parachute") if any(r[0].el.kind != "parachute" for r in rem) else 1.0
        g0 = face_d - face_v if above else face_v - face_d
        self.debris.append({
            "ids": list(removed), "M": Md, "r": r_rel, "v": v_rel, "t0": m.t, "t": m.t, "F0": F0, "t_burn": t_burn, "tail": tail,
            "x0": x_ax.copy(), "w": wd,
            "A": D.BLUFF_CD * math.pi * dmax ** 2 / 4, "chutes": chutes, "R": dmax / 2, "Rv": max(near_v.diameter, 0.2) / 2,
            "face_d": face_d - xcd, "face_v": face_v - xcv, "above": above, "gap": g0, "peak": g0, "min_gap": None})

    def note_thrust(self, groups, t_end):
        """The thrust and propellant flow of each stage at the end of its last powered arc, and when that arc
        ended (for a stage that separates while it burns, or while its thrust tails off)."""
        p = self.m._p_amb() if self.m.ctl.get("atm") else 0.0
        now = {}
        for g in groups:
            eid = g["state"].el.id
            F, md, _ = now.get(eid, (0.0, 0.0, t_end))
            if _transients():
                # at t_end along the group's profile: a stage separated after its cutoff carries only its tail-off
                now[eid] = (F + self.m.group_thrust(g, t_end, p), md + self.m.group_flow(g, t_end), t_end)
            else:
                now[eid] = (F + g["n"] * g["factor"] * g["engine"].thrust(g["throttle"], p), md + g["mdot"], t_end)
        self.last_thrust.update(now)

    def _gap(self, d, r, x_v, x_d):
        s_d = float(np.dot(r, x_v)) + d["face_d"] * float(np.dot(x_d, x_v))
        tilt = math.sqrt(max(0.0, 1.0 - float(np.dot(x_d, x_v)) ** 2))
        gap = (s_d - d["face_v"]) if d["above"] else (d["face_v"] - s_d)
        lateral = float(np.linalg.norm(r - np.dot(r, x_v) * x_v))
        # a relative tilt closes the gap at the rim of the smaller of the two faces
        return gap - min(d["R"], d["Rv"]) * tilt, lateral

    def track(self, ts, sh, dvh, k0, kind, in_atm):
        """Fly every tracked separated element through the arc just flown, relative to the vehicle: the same
        gravity, its own drag and residual thrust, against the vehicle's non-gravitational acceleration.
        kind: '6dof' (accelerations from the arc's dependent variables), 'entry' (drag along the airspeed)
        or 'coast' (none)."""
        if not self.debris:
            return
        m = self.m
        keep = []
        for d in self.debris:
            t_end = min(ts[-1], d["t0"] + TRACK_S)
            done, hit = False, None
            for i in range(1, len(ts)):
                ta, tb = ts[i - 1], min(ts[i], t_end)
                if tb <= d["t"]:
                    continue
                if ta >= t_end:
                    break
                xa, xb = np.asarray(sh[ts[i - 1]], float), np.asarray(sh[ts[i]], float)
                ea = self._vehicle_env(kind, dvh, ts[i - 1], xa, k0, in_atm)
                eb = self._vehicle_env(kind, dvh, ts[i], xb, k0, in_atm)
                xv_a, xv_b = self._axis(kind, xa), self._axis(kind, xb)
                t = max(ta, d["t"])
                while t < tb - 1e-9:
                    age = t - d["t0"]
                    h = min(tb - t, 0.02 if age < 2.0 else (0.1 if age < 10.0 else 1.0))
                    f = (t + 0.5 * h - ts[i - 1]) / max(ts[i] - ts[i - 1], 1e-9)
                    av = ea[0] + f * (eb[0] - ea[0])
                    rho, mach = ea[1] + f * (eb[1] - ea[1]), ea[2] + f * (eb[2] - ea[2])
                    xv = _unit(xv_a + f * (xv_b - xv_a)) if kind == "6dof" else self._coast_axis(t + h)
                    xs = xa[:6] + f * (xb[:6] - xa[:6])
                    acc = self._debris_acc(d, t + 0.5 * h, xs, self._debris_axis(d, t + 0.5 * h), rho, mach) - av
                    d["v"] = d["v"] + acc * h
                    d["r"] = d["r"] + d["v"] * h
                    t += h
                    gap, lat = self._gap(d, d["r"], xv, self._debris_axis(d, t))
                    # clear along the axis, or side by side
                    gap = max(gap, lat - (d["R"] + d["Rv"]))
                    note_gap(d, gap)
                    if self.recontact_check and gap < -RECONTACT_M:
                        hit = (t, gap)
                        break
                    if m.telemetry is not None and t - d.get("tele_t", -1e30) >= 0.5:
                        d["tele_t"] = t
                        m._tele({"kind": "debris", "met": round(t - m.t0, 3), "ids": d["ids"], "gap_m": round(gap, 3),
                                 "r_rel": [round(float(v), 3) for v in d["r"]]})
                    opening = float(np.dot(d["v"], xv)) * (1 if d["above"] else -1) > 0
                    if gap > CLEAR_M and opening and age >= d["t_burn"]:
                        done = True
                        break
                d["t"] = t
                if hit or done:
                    break
            if hit:
                m.fail(f"{d['ids'][0]} hit the vehicle again {hit[0] - d['t0']:.1f} s after it separated (recontact)")
            if done or d["t"] >= d["t0"] + TRACK_S - 1e-6:
                m.event("separation_clear", element=d["ids"][0], after_s=round(d["t"] - d["t0"], 1), gap_m=round(d["gap"], 3),
                        min_gap_m=None if d["min_gap"] is None else round(d["min_gap"], 3))
                continue
            keep.append(d)
        self.debris = keep

    def _vehicle_env(self, kind, dvh, t, x, k0, in_atm):
        """The vehicle's non-gravitational acceleration, and the air density and Mach number around it."""
        if not dvh or t not in dvh:
            return np.zeros(3), 0.0, 0.0
        d = dvh[t]
        rho, mach = (float(d[4]), float(d[2])) if in_atm else (0.0, 0.0)
        if kind == "6dof":
            a = np.array(d[k0:k0 + 3], float)
            if in_atm:
                a = a + np.array(d[9:12], float)
            return a, rho, mach
        if kind == "entry":
            air = x[3:6] - self.m.omega_cross(x[:3])
            return -_unit(air) * float(d[6]), rho, mach
        return np.zeros(3), rho, mach

    def _axis(self, kind, x):
        k = self.arc.get("k_rot", 6) if self.arc else 6
        if kind == "6dof" and len(x) >= k + 7:
            return q_to_R(x[k:k + 4] / np.linalg.norm(x[k:k + 4]))[:, 0]
        return np.asarray(self.m.dir, float)

    def _coast_axis(self, t):
        """The body axis at time t of the kinematic arc just flown (coast() recorded how it moved)."""
        c = self._coast
        if not c or c.get("q0") is None:
            return np.asarray(self.m.dir, float)
        pre = c.get("pre")
        if pre is not None and t < pre["t0"] + pre["T"]:
            # while the rates across the turn stop
            tau = max(0.0, t - pre["t0"])
            return q_to_R(turn(pre["q0"], pre["w0"] * tau + 0.5 * pre["dw_dt"] * tau * tau, 1.0))[:, 0]
        tau = max(0.0, t - c["t0"])
        if c["kind"] == "entry":
            x0, x1 = q_to_R(c["q0"])[:, 0], q_to_R(c.get("q1") if c.get("q1") is not None else c["q0"])[:, 0]
            return _unit(x0 + min(1.0, tau / max(c.get("dt", 1.0), 1e-9)) * (x1 - x0))
        if c["kind"] == "turn":
            ang, _, _ = turn_at(c["plan"], tau)
            return q_to_R(turn(c["q0"], c["axis"] * ang, 1.0))[:, 0]
        if c["kind"] == "hold":
            return q_to_R(turn(c["q0"], c["axis"] * c["off"](tau), 1.0))[:, 0]
        if c["kind"] == "torque":
            return q_to_R(turn(c["q0"], c["w"] * tau + 0.5 * c["dw_dt"] * tau * tau, 1.0))[:, 0]
        return q_to_R(turn(c["q0"], c["w"] * tau, 1.0))[:, 0]

    def _debris_axis(self, d, t):
        rv = d["w"] * (t - d["t0"])
        a = float(np.linalg.norm(rv))
        if a < 1e-12:
            return d["x0"]
        k = rv / a
        x = d["x0"]
        return x * math.cos(a) + _cross(k, x) * math.sin(a) + k * float(np.dot(k, x)) * (1 - math.cos(a))

    def _debris_acc(self, d, t, xs, xd, rho, mach):
        """Non-gravitational acceleration of a separated element: the thrust of engines still burning, along
        its axis, and drag as a tumbling bluff body plus any deployed parachute it carries."""
        m = self.m
        acc = np.zeros(3)
        if d["F0"] > 0 and t - d["t0"] < d["t_burn"]:
            acc += xd * d["F0"] / d["M"]
        for F, t_cut in d.get("tail", ()):
            acc += xd * F * math.exp(-max(0.0, t - t_cut) / tailoff_tau()) / d["M"]
        if rho > 0:
            air = xs[3:6] - m.omega_cross(xs[:3]) - self.wind(xs[:3], t) + d["v"]
            V = float(np.linalg.norm(air))
            area = d["A"] + sum(_interp(D.PARACHUTES[c.chute_type]["cd"], mach) * math.pi * (c.chute_diameter / 2) ** 2
                                for c in d["chutes"])
            acc -= 0.5 * rho * V * air * area / d["M"]
        return acc

    def wind(self, r, t):
        """The wind (J2000, m/s) at r: the world's wind model, which Tudat reads in the vertical frame (north, east,
        down). Separated elements drift with it as the vehicle does."""
        m = self.m
        fn = {"Earth": "_earth_wind", "Mars": "_mars_wind"}.get(m.central)
        if fn is None:
            return np.zeros(3)
        lat, lon, _ = m.latlon(r, t)
        keep = getattr(m.world, "_mars_alt_km", None)   # the Mars wind model notes the height for the density scale
        # The world's wind models take radians, as Tudat passes them (degrees were passed before, which
        # the position-free wind models of the time did not notice).
        n, e, dn = np.ravel(getattr(m.world, fn)(m.altitude(r, t), math.radians(lon), math.radians(lat), t)).tolist()
        if keep is not None:
            m.world._mars_alt_km = keep
        la, lo = math.radians(lat), math.radians(lon)
        north = np.array([-math.sin(la) * math.cos(lo), -math.sin(la) * math.sin(lo), math.cos(la)])
        east = np.array([-math.sin(lo), math.cos(lo), 0.0])
        down = -np.array([math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)])
        Rm = np.array(m.bodies.get(m.central).rotation_model.inertial_to_body_fixed_rotation(t))
        return Rm.T @ (n * north + e * east + dn * down)

    # ------------------------------------------------------------------ observations, telemetry, saved states
    def observe(self, o):
        """What the flight software sees: the attitude estimate, the gyro rates, and air data from navigation."""
        m = self.m
        q, R = self.q, (q_to_R(self.q) if self.q is not None else np.eye(3))
        if q is not None and self.sensors.error(m.t).any():
            R = self.sensors.estimate(R, m.t)
            qe = R_to_q(R)
            q = qe if float(np.dot(qe, self.q)) >= 0 else -qe
        o["dynamics"] = "6dof"
        o["attitude_q"] = None if q is None else [float(v) for v in q]
        o["body_rates_deg_s"] = [float(v) for v in np.degrees(self.sensors.rates(self.w))]
        o["attitude"] = R[:, 0].tolist() if self.q is not None else o.get("attitude")
        if "mach" in o and m.x is not None:
            r, v = np.array(o["r"], float), np.array(o["v"], float)
            o["mach"], o["q"] = air_data(m, r, v)
            air = v - m.omega_cross(r)
            o["airspeed_vector"], o["airspeed"] = air.tolist(), float(np.linalg.norm(air))
            self._entry_observe(o, R, r, air)   # angle of attack and bank of a blunt body
        lay = layout(self.m.present(), self.inside_of)
        if lay:
            _, xc, Ix, It = mass_properties(mass_items(lay))
            o["mass_properties"] = {"center_of_mass_from_bottom_m": round(xc, 3), "inertia_kg_m2": [round(Ix, 1), round(It, 1), round(It, 1)]}

    def tele(self):
        return {"attitude_q": None if self.q is None else [round(float(v), 6) for v in self.q],
                "body_rates_deg_s": [round(float(v), 4) for v in np.degrees(self.w)],
                "gimbal_use": round(getattr(self, "gimbal_use", 0.0), 3),
                "attitude_error_deg": round(math.degrees(float(np.linalg.norm(self.sensors.err))), 5),
                **self._entry_tele(), **(self.slosh.tele() if self.slosh is not None else {})}

    def settle_short(self, pump, e):
        """Why a pump-fed start of stage e that relies on settling fails in 6-DOF, or None (the rule; switch
        MARSFLIGHT_SETTLE_RULE). Liquid needs settle_time(L, a) to settle in a tank of length L at the settling
        acceleration a, scaled from AS-203's flown settling. After a coast longer than that since the last main
        engine burn, the settle command must have run that long without a break. The core calls it at an engine
        start (models/propulsion/engines.py, a change asked of its owner)."""
        m = self.m
        if not _switch("SETTLE_RULE", False) or not pump or m.mode != "flight" or m.t > m.settle_until or not e.tanks:
            return None
        need = settle_time(max(tk.length for tk in e.tanks.values()), self.settle_accel)
        coast, took = m.t - self.burn_end_t, m.t - self.settle_from
        if coast <= need or took >= need - 1e-3:
            return None
        return (f"the propellant was settled for {took:.1f} s after a {coast:.0f} s coast; at the settling acceleration "
                f"of {self.settle_accel:.2e} m/s^2 the liquid in {e.id}'s tanks needs {need:.0f} s to settle (scaled from "
                "the S-IVB's flown settling on AS-203)")

    def _note_settle(self, cmd):
        """Track the settle command (Mission.step sets settle_until before it calls command)."""
        m = self.m
        if cmd.get("settle") and m.settle_until > self._settle_seen:
            if m.t > self._settle_seen:
                self.settle_from = m.t
            rs = next((s for s in m.present() if s.el.rcs_thrust > 0 and s.rcs > 0), None)
            if rs is not None:
                self.settle_accel = 0.1 * rs.el.rcs_thrust / max(m.total_mass(), 1.0)
            self._settle_seen = m.settle_until

    def save(self, sc):
        if self.q is not None:
            sc["q"], sc["w"] = [float(v) for v in self.q], [float(v) for v in self.w]
        self._entry_save(sc)

    def load(self, sc):
        self._entry_load(sc)
        if sc.get("q"):
            q = np.array(sc["q"], float)
            self.q = q / np.linalg.norm(q)
            self.w = np.array(sc.get("w") or [0.0, 0.0, 0.0], float)
            self.m.dir = q_to_R(self.q)[:, 0].copy()
