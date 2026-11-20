"""Propellant slosh flown by Basilisk.

Basilisk (AVS Lab, University of Colorado Boulder; ISC license; ``bsk==2.12.1``) models a slosh mode as a
``sphericalPendulum`` state effector on a rigid ``Spacecraft`` hub, coupled to the hub by the back-substitution
method (Allard, Diaz Ramos and Schaub, "Spacecraft Dynamics Integrating Hinged Solar Panels and Lumped-Mass Fuel
Slosh Model", 2016; Basilisk-SPHERICALPENDULUM-20180518). The pendulum swings in the vehicle's own acceleration,
so its frequency follows the thrust and the drag by itself: omega^2 = a / L.

What this file does:
- **Parameters** (Basilisk does not compute them): each tank's first lateral slosh mode from NASA SP-106 (H. N.
  Abramson, ed., 1966), Table 6.1, p. 204, for a flat-bottomed cylinder holding the same volume, with the
  correction of the frequency for the domed bottom. Damping: SP-106 eq. (4.10a) for the smooth wall, Miles'
  eq. (4.16) for ring baffles under the free surface, and the rule of strong extra damping once the wave at
  the wall is taller than the radius. Inputs and sources: ``data/basilisk/slosh_sp106.json``.
- **SloshRig**: a Basilisk simulation of the rigid vehicle with one spherical pendulum per tank. Basilisk
  integrates the hub's rotation and the pendulums together (RK4 at a fixed step). It is the reference the
  written-out pendulum below is checked against, and a way for Basilisk to fly the attitude itself outside the
  atmosphere if the core wants that later (it costs about 115 us per 0.02 s step, 6 ms per flown second).
- **pendulum_rates**: the same pendulum, linearised and written out, for use inside Tudat's integrator, where it is
  called at every integrator stage. Stepping a Basilisk simulation once per Tudat step instead costs about 20 times
  more per call (250 to 320 us against 10 to 14 us) and holds the coupling over the step, which adds energy: at a
  0.1 s step the heavy LOX slosh grows 3 times too big in 15 s (results/issue-111/README.md). The written-out
  pendulum matches Basilisk within 0.02% in frequency; ``dev/test_basilisk.py`` checks it.
- **BasiliskSlosh**: the Model (core/model.py) that holds the tanks and their states for the core.

Not modelled: higher modes, swirl, roll coupling, and liquid not held at the tank bottom.

Known limit, to fix before wiring in: ``pendulum_rates`` is linear, so when the thrust tails off a swinging slosh
mass loses its restoring force and can move past its pendulum length, which Basilisk's nonlinear pendulum
cannot (results/issue-111/README.md, section 6).
"""
from __future__ import annotations

import json
import math
import os

import numpy as np

from ...core.model import Model

_DATA = json.load(open(os.path.join(os.path.dirname(__file__), "..", "..", "data", "basilisk", "slosh_sp106.json")))


def _v(key):
    return _DATA[key]["value"]


XI = _v("xi")
WAVE_PER_DISPLACEMENT = 2 * XI / (XI * XI - 1)   # x tanh(lam): wave height at the wall per slosh mass displacement
WALL_K = _v("wall_k")
MILES_K = _v("miles_k")
BREAK_ZETA = _v("break_zeta")
A_SETTLED = _v("a_settled_m_s2")
DOME_H = _v("dome_height_per_radius")
NU = _v("kinematic_viscosity_m2_s")
DOME_OMEGA_RATIO = [tuple(r) for r in _v("dome_omega_ratio")]
PKEYS = ("m1", "L", "x", "R", "h", "z_s", "nu")


# ---------------------------------------------------------------------- SP-106 parameters of one tank
def mode(liquid_kg, rho, R):
    """SP-106 Table 6.1 for a flat-bottomed cylinder of radius R: (depth h, slosh mass m1, pendulum length
    L = a / omega^2, depth of the slosh mass below the free surface, tanh(xi h / R))."""
    h = liquid_kg / rho / (math.pi * R * R)
    if h <= 1e-6 or R <= 0:
        return h, 0.0, 1.0, 0.0, 0.0
    th = math.tanh(XI * h / R)
    m1 = liquid_kg * 2.0 * R * th / (XI * (XI * XI - 1.0) * h)
    return h, m1, R / (XI * th), R / XI * th, th


def frequency(a, R, h):
    """SP-106 first slosh frequency (rad/s) of a flat-bottomed cylinder: omega^2 = a xi tanh(xi h / R) / R."""
    return math.sqrt(a * XI * math.tanh(XI * h / R) / R)


def surface_height(v, R):
    """Free-surface height above the bottom of a tank whose bottom is a 1:sqrt(2) dome, holding volume v."""
    c = R * DOME_H
    v_dome = (2.0 / 3.0) * math.pi * R * R * c
    if v >= v_dome:
        return c + (v - v_dome) / (math.pi * R * R)
    lo, hi = 0.0, c
    for _ in range(50):
        z = 0.5 * (lo + hi)
        if math.pi * R * R * (z * z / c - z ** 3 / (3 * c * c)) < v:
            lo = z
        else:
            hi = z
    return 0.5 * (lo + hi)


def dome_factor(z_s, R):
    """Domed tank's first slosh frequency over the flat cylinder's with the same volume (the solver)."""
    x, t = z_s / R, DOME_OMEGA_RATIO
    if x <= t[0][0]:
        return t[0][1]
    for (a, fa), (b, fb) in zip(t, t[1:]):
        if x <= b:
            return fa + (fb - fa) * (x - a) / (b - a)
    return t[-1][1]


def tank_params(x_bottom, radius, liquid_kg, rho, propellant):
    """Pendulum of one tank: slosh mass m1, length L, body x of the slosh mass at rest, radius, depth h, free
    surface height z_s above the tank bottom, kinematic viscosity."""
    h, m1, L, below, _ = mode(liquid_kg, rho, radius)
    z_s = surface_height(liquid_kg / rho, radius)
    L = L / dome_factor(z_s, radius) ** 2
    return {"m1": m1, "L": L, "x": x_bottom + z_s - below, "R": radius, "h": h, "z_s": z_s, "nu": NU.get(propellant, 1e-6)}


def wall_damping(nu, R, h, a):
    """Smooth-wall damping ratio, SP-106 eq. (4.10a) with K = 5.23 (logarithmic decrement / 2 pi)."""
    if a <= 0 or h <= 0:
        return 0.0
    lam = 1.84 * h / R
    delta = WALL_K * math.sqrt(nu) * R ** -0.75 * a ** -0.25 * math.tanh(lam) * (
        1.0 + 2.0 * (1.0 - h / R) / math.sinh(min(2 * lam, 700.0)))
    return max(0.0, delta) / (2 * math.pi)


def baffle_damping(rings, width_frac, R, z_surface, wave):
    """Miles' ring-baffle damping ratio, SP-106 eq. (4.16), summed over the rings under the free surface."""
    if not rings or wave <= 0:
        return 0.0
    c1 = 1.0 - (1.0 - width_frac) ** 2
    k = MILES_K * c1 ** 1.5 * math.sqrt(wave / R)
    return sum(k * math.exp(-4.6 * (z_surface - z) / R) for z in rings if z <= z_surface)


def wave_height(p, amp):
    """Wave height at the wall (m) for a slosh mass displaced by amp (m), linear potential theory."""
    return WAVE_PER_DISPLACEMENT * math.tanh(XI * p["h"] / p["R"]) * amp


def damping_ratio(p, a, amp, baffles=None):
    """Total damping ratio of one tank's mode at axial acceleration a and slosh mass amplitude amp."""
    wave = wave_height(p, amp)
    z = wall_damping(p["nu"], p["R"], p["h"], a)
    if baffles:
        z += baffle_damping(baffles["rings"], baffles["width_frac"], p["R"], p["z_s"], wave)
    if wave > p["R"]:
        z += BREAK_ZETA
    return z, wave


def interp(p0, p1, f):
    return {k: p0[k] + f * (p1[k] - p0[k]) for k in PKEYS}


# ---------------------------------------------------------------------- Basilisk: the vehicle and its pendulums
class SloshRig:
    """A Basilisk simulation of the rigid vehicle (hub) and one spherical pendulum per tank, in free space.

    Body axes: x along the vehicle from tail to nose (the thrust direction), as in marsflight. The body point B
    of Basilisk sits at body x = ``origin``. Forces given to ``step`` act at B; torques are about B.
    Masses: ``M`` total, ``xc`` its centre with the liquid at rest, ``Ix`` roll inertia, ``It`` pitch and yaw
    inertia about ``xc`` with all liquid counted as rigid (what marsflight's mass_properties gives). The slosh
    masses are taken out of the hub and hung from their hinges, a length L above their place at rest.
    """

    def __init__(self, M, xc, Ix, It, tanks, dt=0.02, origin=None, baffles=None):
        from Basilisk.simulation import extForceTorque, spacecraft, sphericalPendulum
        from Basilisk.utilities import SimulationBaseClass, macros
        self.macros = macros
        self.dt = float(dt)
        self.xB = float(xc if origin is None else origin)
        self.baffles = list(baffles) if baffles else [None] * len(tanks)
        sim = SimulationBaseClass.SimBaseClass()
        proc = sim.CreateNewProcess("dyn")
        proc.addTask(sim.CreateNewTask("dyn", macros.sec2nano(self.dt)))
        sc = spacecraft.Spacecraft()
        sc.ModelTag = "vehicle"
        self.pend = []
        for i, _ in enumerate(tanks):
            p = sphericalPendulum.SphericalPendulum()
            p.pHat_01 = [[-1.0], [0.0], [0.0]]     # hangs aft: the thrust pushes the liquid to the tank bottom
            p.pHat_02 = [[0.0], [1.0], [0.0]]
            p.pHat_03 = [[0.0], [0.0], [-1.0]]
            for attr in ("Phi", "Theta", "PhiDot", "ThetaDot", "Mass"):
                setattr(p, f"nameOf{attr}State", f"slosh{i}{attr}")
            p.massInit = 1.0
            sc.addStateEffector(p)
            self.pend.append(p)
        ext = extForceTorque.ExtForceTorque()
        ext.ModelTag = "actuators"
        sc.addDynamicEffector(ext)
        sim.AddModelToTask("dyn", sc)
        sim.AddModelToTask("dyn", ext)
        self.sim, self.sc, self.ext = sim, sc, ext
        self.params = [dict(p) for p in tanks]
        self.set_mass(M, xc, Ix, It, tanks, init=True)
        sim.InitializeSimulation()
        self._st = [[sc.dynManager.getStateObject(f"slosh{i}{a}") for a in ("Phi", "Theta", "PhiDot", "ThetaDot", "Mass")]
                    for i in range(len(tanks))]
        for i, p in enumerate(tanks):
            self._st[i][4].setState([[p["m1"]]])
        self.t = 0.0

    def set_mass(self, M, xc, Ix, It, tanks, init=False):
        """Hub and pendulums for these mass properties and SP-106 parameters (call as the tanks drain)."""
        msl = sum(p["m1"] for p in tanks)
        mh = M - msl
        xh = (M * xc - sum(p["m1"] * p["x"] for p in tanks)) / mh
        It_rigid = It - sum(p["m1"] * (p["x"] - xc) ** 2 for p in tanks)
        Ih = max(It_rigid - mh * (xh - xc) ** 2, 1e-6 * It)
        self.sc.hub.mHub = mh
        self.sc.hub.r_BcB_B = [[xh - self.xB], [0.0], [0.0]]
        self.sc.hub.IHubPntBc_B = [[Ix, 0.0, 0.0], [0.0, Ih, 0.0], [0.0, 0.0, Ih]]
        for i, (pd, p) in enumerate(zip(self.pend, tanks)):
            pd.pendulumRadius = p["L"]
            pd.d = [[p["x"] + p["L"] - self.xB], [0.0], [0.0]]
            if not init:
                self._st[i][4].setState([[p["m1"]]])
        self.params = [dict(p) for p in tanks]
        self.M, self.xc, self.It_rigid = M, xc, It_rigid

    def set_damping(self, i, ratio, a):
        """Damping ratio of pendulum i at axial acceleration a: D = 2 zeta omega m1 (N s/m)."""
        p = self.params[i]
        c = 2.0 * ratio * math.sqrt(max(a, 0.0) / p["L"]) * p["m1"]
        self.pend[i].D = [[c, 0.0, 0.0], [0.0, c, 0.0], [0.0, 0.0, c]]

    def push(self, i, uy, uz=0.0):
        """Displace slosh mass i sideways by (uy, uz) metres in body axes, at rest."""
        L = self.params[i]["L"]
        th = math.asin(max(-1.0, min(1.0, uz / L)))
        ph = math.asin(max(-1.0, min(1.0, uy / (L * math.cos(th)))))
        for k, v in enumerate((ph, th, 0.0, 0.0)):
            self._st[i][k].setState([[v]])

    def slosh(self, i):
        """Slosh mass i's sideways displacement and velocity relative to its hinge, body axes: (uy, uz, vy, vz)."""
        L = self.params[i]["L"]
        ph, th, phd, thd = (float(s.getState()[0][0]) for s in self._st[i][:4])
        return (L * math.sin(ph) * math.cos(th), L * math.sin(th),
                L * (phd * math.cos(ph) * math.cos(th) - thd * math.sin(ph) * math.sin(th)), L * thd * math.cos(th))

    def hub(self):
        """(sigma_BN MRP, omega_BN_B rad/s, r_BN_N m, v_BN_N m/s) of the hub."""
        s = self.sc.scStateOutMsg.read()
        return np.array(s.sigma_BN), np.array(s.omega_BN_B), np.array(s.r_BN_N), np.array(s.v_BN_N)

    def step(self, dt, force_B=(0.0, 0.0, 0.0), torque_B=(0.0, 0.0, 0.0)):
        """Fly dt seconds with this force (N, at B) and torque (N m, about B), both in body axes."""
        self.ext.extForce_B = [[float(v)] for v in force_B]
        self.ext.extTorquePntB_B = [[float(v)] for v in torque_B]
        self.t += dt
        self.sim.ConfigureStopTime(self.macros.sec2nano(self.t))
        self.sim.ExecuteSimulation()


def mrp_to_dcm(s):
    """Direction cosine matrix [BN] (inertial to body) of a modified Rodrigues parameter set."""
    s = np.asarray(s, float)
    s2 = s @ s
    S = np.array([[0, -s[2], s[1]], [s[2], 0, -s[0]], [-s[1], s[0], 0]])
    return np.eye(3) + (8 * S @ S - 4 * (1 - s2) * S) / (1 + s2) ** 2


# ---------------------------------------------------------------------- the same pendulum, written out (in the air)
def pendulum_rates(params, x, ax, xc, Ix, It_rigid, M, w, F_lat, tq, zetas, I_T=None):
    """Linearised spherical pendulums on a rigid hub, for Tudat's integrator in the air: Basilisk's pendulum
    for small swings (results/issue-111 checks the two against each other and against an eigenvalue analysis).

    The hub is the vehicle without the slosh masses: mass M - sum(m1), centre xh, pitch inertia about xh. Each
    slosh mass sits at its place x + its swing u, hangs from a hinge L ahead of x, and feels the vehicle's axial
    acceleration ax (its restoring force m1 ax / L per metre) and the hub's lateral and angular acceleration.

    params: SP-106 parameters per tank; x: (uy, uz, vy, vz) per tank, flat; ax: axial specific force (m/s^2);
    xc: centre of mass of the whole vehicle with the liquid at rest; Ix: roll inertia; It_rigid: pitch inertia
    about xc without the slosh masses; M: total mass; w: body rates; F_lat: other lateral forces (y, z), N, at
    xc; tq: other torques about xc (body axes); zetas: damping ratio per tank; I_T: the pitch inertia the core's
    rigid body flies with (default It_rigid).
    Returns (derivatives of x, torque (pitch, yaw) for the core to add so that its rigid body turns as the hub
    does, slosh force (y, z) on the hub, angular acceleration (pitch, yaw) of the hub)."""
    msum = sum(p["m1"] for p in params if p["m1"] > 0) if ax > A_SETTLED else 0.0
    mh = max(M - msum, 1e-6)
    xh = (M * xc - sum(p["m1"] * p["x"] for p in params)) / mh if msum > 0 else xc
    Ih = max(It_rigid - mh * (xh - xc) ** 2, 1e-6 * It_rigid)
    Fy = Fz = ty = tz = 0.0
    keep = []
    for i, p in enumerate(params):
        uy, uz, vy, vz = x[4 * i:4 * i + 4]
        m1 = p["m1"]
        if ax <= A_SETTLED or m1 <= 0:
            keep.append(None)
            continue
        om2 = ax / p["L"]
        c = 2.0 * zetas[i] * math.sqrt(om2)
        sy, sz = m1 * (om2 * uy + c * vy), m1 * (om2 * uz + c * vz)
        d = p["x"] - xh
        Fy, Fz = Fy + sy, Fz + sz
        ty += -d * sz - m1 * ax * uz
        tz += d * sy + m1 * ax * uy
        keep.append((om2, c, d))
    wx, wy, wz = w
    # other forces and torques moved from xc to the hub's centre
    tqy = tq[1] - (xc - xh) * F_lat[1]
    tqz = tq[2] + (xc - xh) * F_lat[0]
    ay = (tqy + ty - (Ix - Ih) * wx * wz) / Ih
    az = (tqz + tz - (Ih - Ix) * wx * wy) / Ih
    fy, fz = (F_lat[0] + Fy) / mh, (F_lat[1] + Fz) / mh
    dx = [0.0] * len(x)
    for i, k in enumerate(keep):
        if k is None:
            continue
        om2, c, d = k
        uy, uz, vy, vz = x[4 * i:4 * i + 4]
        dx[4 * i:4 * i + 4] = [vy, vz, -om2 * uy - c * vy - (fy + d * az), -om2 * uz - c * vz - (fz - d * ay)]
    # the torque that makes the core's rigid body (inertia I_T about xc, Euler's equations) turn at (ay, az)
    I_T = It_rigid if I_T is None else I_T
    sy_ = I_T * ay - tq[1] + (Ix - I_T) * wx * wz
    sz_ = I_T * az - tq[2] + (I_T - Ix) * wx * wy
    return dx, (sy_, sz_), (Fy, Fz), (ay, az)


def pendulum_rates_nl(params, x, ax, xc, Ix, It_rigid, M, w, F_lat, tq, zetas, I_T=None):
    """The pendulums of ``pendulum_rates`` with their swing angles as states, nonlinear as Basilisk's
    ``sphericalPendulum`` (each lateral axis a planar pendulum of length L): x per tank is (theta_y, theta_z,
    theta_y', theta_z'), the slosh mass's sideways place is L sin(theta). For small swings this is
    ``pendulum_rates`` exactly. When the thrust tails off at an engine cutoff the restoring force goes, and a
    swinging mass then turns about its hinge instead of running off sideways without end: its place stays within
    L, as in Basilisk (results/issue-111, section 6, the cutoff defect). Same inputs and outputs otherwise."""
    msum = sum(p["m1"] for p in params if p["m1"] > 0) if ax > A_SETTLED else 0.0
    mh = max(M - msum, 1e-6)
    xh = (M * xc - sum(p["m1"] * p["x"] for p in params)) / mh if msum > 0 else xc
    Ih = max(It_rigid - mh * (xh - xc) ** 2, 1e-6 * It_rigid)
    Fy = Fz = ty = tz = 0.0
    keep = []
    for i, p in enumerate(params):
        thy, thz, oy, oz = x[4 * i:4 * i + 4]
        m1, L = p["m1"], p["L"]
        if ax <= A_SETTLED or m1 <= 0:
            keep.append(None)
            continue
        c = 2.0 * zetas[i] * math.sqrt(ax / L)
        uy, uz = L * math.sin(thy), L * math.sin(thz)
        sy, sz = m1 * (ax * math.sin(thy) + c * L * oy), m1 * (ax * math.sin(thz) + c * L * oz)
        d = p["x"] - xh
        Fy, Fz = Fy + sy, Fz + sz
        ty += -d * sz - m1 * ax * uz
        tz += d * sy + m1 * ax * uy
        keep.append((c, d, L))
    wx, wy, wz = w
    tqy = tq[1] - (xc - xh) * F_lat[1]
    tqz = tq[2] + (xc - xh) * F_lat[0]
    ay = (tqy + ty - (Ix - Ih) * wx * wz) / Ih
    az = (tqz + tz - (Ih - Ix) * wx * wy) / Ih
    fy, fz = (F_lat[0] + Fy) / mh, (F_lat[1] + Fz) / mh
    dx = [0.0] * len(x)
    for i, k in enumerate(keep):
        if k is None:
            continue
        c, d, L = k
        thy, thz, oy, oz = x[4 * i:4 * i + 4]
        dx[4 * i:4 * i + 4] = [oy, oz,
                               -(ax / L) * math.sin(thy) - c * oy - (fy + d * az) / L * math.cos(thy),
                               -(ax / L) * math.sin(thz) - c * oz - (fz - d * ay) / L * math.cos(thz)]
    I_T = It_rigid if I_T is None else I_T
    sy_ = I_T * ay - tq[1] + (Ix - I_T) * wx * wz
    sz_ = I_T * az - tq[2] + (I_T - Ix) * wx * wy
    return dx, (sy_, sz_), (Fy, Fz), (ay, az)


def swing_amplitude(p, ax, th, om):
    """Displacement amplitude (m) of a slosh mass swinging at angle th with rate om: from the pendulum's energy,
    never more than its length (a mass past 90 deg is the whole liquid off the bottom)."""
    om2 = max(ax, 1e-9) / p["L"]
    # the swing angle with the same energy, from the pendulum's energy (1 - cos th) + om^2 / (2 om2): a mass that
    # went over the top and came back down (th near 2 pi) is at rest again, as the liquid is
    e = (1.0 - math.cos(th)) + 0.5 * om * om / om2
    a = math.acos(max(-1.0, 1.0 - e))
    return p["L"] * math.sin(min(a, 0.5 * math.pi))


# ---------------------------------------------------------------------- the Model
class BasiliskSlosh(Model):
    """Slosh of every tank held at its bottom by thrust. The core gives each arc's tanks with
    ``on_arc_start(arc, state)`` where ``state.slosh_tanks`` is [(key, x_bottom, radius, liquid_kg, rho,
    propellant, baffles)] at the arc start and ``state.slosh_tanks_end`` the same after the arc's burn, and
    ``state.mass_props`` is (M, xc, Ix, It).

    In every phase Tudat flies the vehicle and the slosh states are integrated with ``rates`` (the written-out
    Basilisk pendulum) through a state hook the core does not have yet (asked). ``rates`` also gives the
    torque the core must add so that its rigid body turns as Basilisk's hub does.
    """
    name = "slosh_basilisk"
    area = "slosh"
    order = 40

    def configure(self, ctx):
        self.ctx = ctx
        self.states = {}         # tank key -> [uy, uz, vy, vz]
        self.arc_tanks = []      # (key, p0, p1, baffles)
        self.peak = {}
        self.breaking = set()

    def design_fields(self):
        return ["elements[].baffles.width_frac", "elements[].baffles.spacing_m"]

    def on_arc_start(self, arc, state):
        start = {t[0]: t for t in getattr(state, "slosh_tanks", [])}
        end = {t[0]: t for t in getattr(state, "slosh_tanks_end", [])}
        self.arc_tanks = []
        for key, t in start.items():
            p0 = tank_params(t[1], t[2], t[3], t[4], t[5])
            p1 = tank_params(*end[key][1:6]) if key in end else dict(p0, m1=0.0)
            self.arc_tanks.append((key, p0, p1, t[6]))
            self.states.setdefault(key, [0.0, 0.0, 0.0, 0.0])
        live = {k for k, *_ in self.arc_tanks}
        self.states = {k: v for k, v in self.states.items() if k in live}

    def zetas(self, f, ax):
        out, waves = [], []
        for key, p0, p1, baf in self.arc_tanks:
            p = interp(p0, p1, f)
            uy, uz, vy, vz = self.states[key]
            om2 = max(ax, 1e-9) / p["L"]
            z, wave = damping_ratio(p, ax, math.sqrt(uy * uy + uz * uz + (vy * vy + vz * vz) / om2), baf)
            out.append(z)
            waves.append(wave)
        return out, waves

    def rates(self, f, x, ax, xc, Ix, It_rigid, M, w, F_lat, tq):
        params = [interp(p0, p1, f) for _, p0, p1, _ in self.arc_tanks]
        zetas, _ = self.zetas(f, ax)
        return pendulum_rates(params, x, ax, xc, Ix, It_rigid, M, w, F_lat, tq, zetas)

    def on_arc_end(self, arc, out, state):
        for key, p0, p1, _ in self.arc_tanks:
            uy, uz, vy, vz = self.states[key]
            ax = getattr(state, "axial_accel", 0.0) or 0.0
            if ax <= A_SETTLED:
                continue
            om2 = ax / p1["L"]
            wave = wave_height(p1, math.sqrt(uy * uy + uz * uz + (vy * vy + vz * vz) / om2))
            self.peak[key] = max(self.peak.get(key, 0.0), wave / p1["R"])

    def coast(self):
        """Nothing holds the liquid at the tank bottoms: the pendulums start again at rest."""
        self.states = {k: [0.0, 0.0, 0.0, 0.0] for k in self.states}
        self.breaking = set()

    def obs(self, state):
        return {}


MODELS = [BasiliskSlosh]
