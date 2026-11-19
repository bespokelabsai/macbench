"""Eclipses, the battery through them, and battery ageing (with the surface battery rules).

**Shadow.** The fraction of the Sun's disk a craft sees past a planet: the conical model with umbra and penumbra
(the apparent disks of the Sun and the planet and their overlap), the same geometry as Basilisk's ``eclipse``
module (AVS Lab, ISC license, ``bsk==2.12.1``). ``basilisk_shadow`` runs Basilisk's module itself, and
dev/test_power_thermal.py checks that ``shadow_factor`` (numpy, about 100 times faster per sample) agrees with it.
In an orbit arc whose samples are sparser than the orbit, the shadow comes from the osculating orbit flown once at
a fine step (``orbit_shadow``): the Sun barely moves in one orbit.

**Battery through an eclipse.** In shadow the arrays give nothing; the battery carries the load less any RTG power,
down to its depth of discharge limit, and the arrays recharge it in sunlight with the round trip loss. A craft that
cannot carry an eclipse fails. ``basilisk_orbit`` flies one orbit with Basilisk's ``eclipse``,
``simpleSolarPanel``, ``simplePowerSink`` and ``simpleBattery`` for the check.

**Battery ageing.** Capacity fades with the square root of the time since the cells were made (calendar ageing)
and with equivalent full cycles. Fitted to two batteries together: Mars Express's flown Li-ion battery (Sony 18650HC,
24 Ah; capacity from in-flight deep discharge tests, Sousa and van der Pols, SpaceOps 2012, Table 2; ten points over
eleven years) and JPL's ground twin of the MER rover battery (cruise then 730 sols at about 45% depth of discharge,
Smart et al. 2007, NTRS 20090022179, Table 1). Within 2.3% rms of Mars Express and 3.2% of MER; Venus Express's
battery of the same cell batch is a holdout (6% rms, it lost less). Usable depth of discharge 60%, as InSight
(PDS InSight instrument host page) and MER's flight rules (40% charge kept, Herman et al. 2021); Mars Express kept
30% to 50% charge (Sousa and van der Pols 2012). Round trip 90% (Schmitz, NASA GRC 2023, NTRS 20230001487).
"""
from __future__ import annotations

import math

import numpy as np

from ...core.model import Model
from . import arrays as A

R_SUN_M = 6.957e8                  # IAU 2015 nominal solar radius
R_MARS_M = 3396.19e3               # equatorial radius, as Basilisk's eclipse module uses (astroConstants REQ_MARS)
R_EARTH_M = 6378.137e3
MU = {"Mars": 4.282837e13, "Earth": 3.986004418e14}
RADIUS = {"Mars": R_MARS_M, "Earth": R_EARTH_M}

BATTERY_DEPTH_OF_DISCHARGE = 0.6
BATTERY_ROUND_TRIP = 0.9
# capacity fraction = 1 - FADE_CAL * sqrt(years since the cells were made) - FADE_CYCLE * equivalent full cycles
FADE_CAL_PER_SQRT_YEAR = 0.0660
FADE_PER_FULL_CYCLE = 1.09e-4
BATTERY_AGE_AT_LAUNCH_YEARS = 1.0  # cells made about a year before launch (Mars Express: two years; MER: about one)


# ---- shadow geometry -------------------------------------------------------------------------------------------------
def shadow_factor(r_sc, r_sun, r_body, R_body, R_sun=R_SUN_M):
    """Fraction of the Sun's disk visible from the craft (1 lit, 0 umbra). Positions (m) in any common frame; arrays
    of shape (..., 3) work too."""
    r_sc, r_sun, r_body = (np.asarray(x, dtype=float) for x in (r_sc, r_sun, r_body))
    s = r_sun - r_sc
    b = r_body - r_sc
    ds = np.linalg.norm(s, axis=-1)
    db = np.linalg.norm(b, axis=-1)
    a_s = np.arcsin(np.clip(R_sun / ds, -1, 1))          # apparent radius of the Sun
    a_b = np.arcsin(np.clip(R_body / db, -1, 1))         # apparent radius of the planet
    c = np.arccos(np.clip(np.sum(s * b, axis=-1) / (ds * db), -1, 1))
    out = np.ones_like(c)
    behind = np.sum(s * b, axis=-1) > 0                  # the planet must be on the Sun's side of the craft
    full = behind & (c <= a_b - a_s)
    annular = behind & (c <= a_s - a_b)
    part = behind & ~full & ~annular & (c < a_s + a_b)
    out = np.where(full, 0.0, out)
    out = np.where(annular, 1.0 - (a_b / a_s) ** 2, out)
    if np.any(part):
        # overlap area of two disks of radii a_s and a_b at distance c (small angles)
        with np.errstate(divide="ignore", invalid="ignore"):
            x = (c ** 2 + a_s ** 2 - a_b ** 2) / (2 * np.where(c > 0, c, 1.0))
        y = np.sqrt(np.clip(a_s ** 2 - x ** 2, 0, None))
        area = (a_s ** 2 * np.arccos(np.clip(x / a_s, -1, 1)) + a_b ** 2 * np.arccos(np.clip((c - x) / a_b, -1, 1))
                - c * y)
        out = np.where(part, 1.0 - area / (math.pi * a_s ** 2), out)
    return out


def basilisk_shadow(r_sc, r_sun, r_body, body="mars"):
    """The shadow factor from Basilisk's own eclipse module, for one craft position (m, inertial frame)."""
    from Basilisk.architecture import messaging
    from Basilisk.simulation import eclipse
    from Basilisk.utilities import SimulationBaseClass, macros

    sim = SimulationBaseClass.SimBaseClass()
    proc = sim.CreateNewProcess("p")
    proc.addTask(sim.CreateNewTask("t", macros.sec2nano(1.0)))
    sun = messaging.SpicePlanetStateMsgPayload()
    sun.PositionVector = [float(v) for v in r_sun]
    sun.PlanetName = "sun"
    pl = messaging.SpicePlanetStateMsgPayload()
    pl.PositionVector = [float(v) for v in r_body]
    pl.PlanetName = body
    sc = messaging.SCStatesMsgPayload()
    sc.r_BN_N = [float(v) for v in r_sc]
    msgs = [messaging.SpicePlanetStateMsg().write(sun), messaging.SpicePlanetStateMsg().write(pl),
            messaging.SCStatesMsg().write(sc)]
    ecl = eclipse.Eclipse()
    ecl.ModelTag = "eclipse"
    ecl.sunInMsg.subscribeTo(msgs[0])
    ecl.addPlanetToModel(msgs[1])
    ecl.addSpacecraftToModel(msgs[2])
    sim.AddModelToTask("t", ecl)
    sim.InitializeSimulation()
    sim.ConfigureStopTime(macros.sec2nano(1.0))
    sim.ExecuteSimulation()
    return float(ecl.eclipseOutMsgs[0].read().shadowFactor)


def kepler_states(r0, v0, mu, times):
    """Positions (m) on the two-body orbit through (r0, v0) at `times` (s after r0), by universal variables via
    Kepler's equation on the ellipse. Only for bound orbits."""
    r0, v0 = np.asarray(r0, float), np.asarray(v0, float)
    r = np.linalg.norm(r0)
    v2 = v0 @ v0
    a = 1.0 / (2.0 / r - v2 / mu)
    if a <= 0:
        raise ValueError("not a bound orbit")
    h = np.cross(r0, v0)
    e_vec = np.cross(v0, h) / mu - r0 / r
    e = np.linalg.norm(e_vec)
    p_hat = e_vec / e if e > 1e-9 else r0 / r
    w_hat = h / np.linalg.norm(h)
    q_hat = np.cross(w_hat, p_hat)
    nu0 = math.atan2(r0 @ q_hat, r0 @ p_hat)
    E0 = 2 * math.atan2(math.sqrt(1 - e) * math.sin(nu0 / 2), math.sqrt(1 + e) * math.cos(nu0 / 2))
    M0 = E0 - e * math.sin(E0)
    n = math.sqrt(mu / a ** 3)
    M = M0 + n * np.asarray(times, float)
    E = M.copy()
    for _ in range(30):
        E = E - (E - e * np.sin(E) - M) / (1 - e * np.cos(E))
    x = a * (np.cos(E) - e)
    y = a * math.sqrt(1 - e * e) * np.sin(E)
    return np.outer(x, p_hat) + np.outer(y, q_hat), 2 * math.pi / n


def orbit_shadow(r0, v0, sun_rel, central="Mars", steps=720):
    """One revolution of the osculating orbit about `central`, with the Sun at sun_rel (m, from the planet's centre,
    held fixed over the orbit). Returns the lit fraction, the longest shadow (s, umbra plus half the penumbra) and the
    period (s)."""
    mu, R = MU[central], RADIUS[central]
    _, period = kepler_states(r0, v0, mu, [0.0])
    t = np.linspace(0.0, period, steps, endpoint=False)
    pos, _ = kepler_states(r0, v0, mu, t)
    f = shadow_factor(pos, np.asarray(sun_rel, float), np.zeros(3), R)
    dark = f < 0.5
    longest = 0
    if dark.any():
        k = np.argmin(dark) if not dark.all() else 0       # start at a lit sample, so a shadow is never split
        d = np.roll(dark, -k)
        run = best = 0
        for x in d:
            run = run + 1 if x else 0
            best = max(best, run)
        longest = best
    return {"lit": float(f.mean()), "longest_s": longest * period / steps, "period_s": period}


def cylinder_eclipse_fraction(alt_m, beta_deg=0.0, R=R_MARS_M):
    """The analytic shadow of a circular orbit in a cylinder of the planet's radius: the share of the orbit in shadow
    for a Sun at beta_deg from the orbit plane (Vallado, Fundamentals of Astrodynamics, eclipse of a circular orbit)."""
    r = R + alt_m
    b = math.radians(beta_deg)
    x = math.sqrt(alt_m ** 2 + 2 * R * alt_m) / (r * math.cos(b)) if math.cos(b) > 0 else 2.0
    return math.acos(x) / math.pi if x < 1 else 0.0


# ---- battery ---------------------------------------------------------------------------------------------------------
def capacity_fraction(years_since_made: float, full_cycles: float) -> float:
    """Capacity left, as a fraction of the new battery's."""
    return max(0.0, 1.0 - FADE_CAL_PER_SQRT_YEAR * math.sqrt(max(0.0, years_since_made))
               - FADE_PER_FULL_CYCLE * max(0.0, full_cycles))


def eclipse_need_Wh(load_W: float, nuclear_W: float, shadow_s: float) -> float:
    """Energy the battery gives through one shadow."""
    return max(0.0, load_W - nuclear_W) * shadow_s / 3600.0


def orbit_balance(load_W, nuclear_W, array_lit_W, lit_fraction, period_s, longest_shadow_s, battery_Wh, capacity=1.0):
    """The power balance of one orbit: whether the battery carries the longest shadow within its depth of discharge,
    and whether the arrays in sunlight cover the load and recharge what the shadow took (with the round trip loss)."""
    need = eclipse_need_Wh(load_W, nuclear_W, longest_shadow_s)
    usable = battery_Wh * capacity * BATTERY_DEPTH_OF_DISCHARGE
    dark_s = (1.0 - lit_fraction) * period_s
    lit_s = lit_fraction * period_s
    drain = eclipse_need_Wh(load_W, nuclear_W, dark_s)
    spare = (array_lit_W + nuclear_W - load_W) * lit_s / 3600.0
    return {"need_Wh": need, "usable_Wh": usable, "carries_shadow": need <= usable + 1e-9,
            "recharges": spare * BATTERY_ROUND_TRIP >= drain - 1e-9, "depth": need / max(battery_Wh * capacity, 1e-9),
            "full_cycles": drain / max(battery_Wh * capacity, 1e-9)}


def basilisk_orbit(r0, v0, sun_rel, central, area_m2, load_W, battery_Wh, soc0=1.0, dt=20.0, nuclear_W=0.0,
                   r_au=1.524):
    """One orbit flown in Basilisk: eclipse -> simpleSolarPanel -> simpleBattery, with a constant sink. The arrays are
    held on the Sun. Returns the lowest stored energy (Wh), the stored energy at the end, and the lit fraction."""
    from Basilisk.architecture import messaging
    from Basilisk.simulation import eclipse, simpleBattery, simplePowerSink, simpleSolarPanel
    from Basilisk.utilities import SimulationBaseClass, macros

    mu = MU[central]
    _, period = kepler_states(r0, v0, mu, [0.0])
    sim = SimulationBaseClass.SimBaseClass()
    proc = sim.CreateNewProcess("p")
    proc.addTask(sim.CreateNewTask("t", macros.sec2nano(dt)))
    sun_p = messaging.SpicePlanetStateMsgPayload()
    sun_p.PlanetName = "sun"
    s_hat = np.asarray(sun_rel, float) / np.linalg.norm(sun_rel)
    # the Sun message's distance sets the flux at the panel: put the Sun at r_au along its direction from the planet
    sun_p.PositionVector = [float(v) for v in s_hat * r_au * 1.495978707e11]
    sun_msg = messaging.SpicePlanetStateMsg().write(sun_p)
    pl = messaging.SpicePlanetStateMsgPayload()
    pl.PlanetName = central.lower()
    pl.PositionVector = [0.0, 0.0, 0.0]
    pl_msg = messaging.SpicePlanetStateMsg().write(pl)
    sc = messaging.SCStatesMsgPayload()
    sc_msg = messaging.SCStatesMsg()
    ecl = eclipse.Eclipse()
    ecl.ModelTag = "eclipse"
    ecl.sunInMsg.subscribeTo(sun_msg)
    ecl.addPlanetToModel(pl_msg)
    ecl.addSpacecraftToModel(sc_msg)
    panel = simpleSolarPanel.SimpleSolarPanel()
    panel.ModelTag = "array"
    # the body frame is the inertial frame (sigma_BN = 0), so the panel normal is the Sun's direction: arrays on the Sun
    panel.setPanelParameters([float(v) for v in s_hat], float(area_m2), A.SOLAR_EFFICIENCY)
    panel.stateInMsg.subscribeTo(sc_msg)
    panel.sunInMsg.subscribeTo(sun_msg)
    panel.sunEclipseInMsg.subscribeTo(ecl.eclipseOutMsgs[0])
    sink = simplePowerSink.SimplePowerSink()
    sink.ModelTag = "load"
    sink.nodePowerOut = -float(load_W - nuclear_W)
    bat = simpleBattery.SimpleBattery()
    bat.ModelTag = "battery"
    cap = max(battery_Wh, 1e-6) * 3600.0
    bat.storageCapacity = cap
    bat.storedCharge_Init = soc0 * cap
    bat.addPowerNodeToModel(panel.nodePowerOutMsg)
    bat.addPowerNodeToModel(sink.nodePowerOutMsg)
    sim.AddModelToTask("t", ecl, 40)
    sim.AddModelToTask("t", panel, 30)
    sim.AddModelToTask("t", sink, 20)
    sim.AddModelToTask("t", bat, 10)
    n = int(math.ceil(period / dt))
    pos, _ = kepler_states(r0, v0, mu, np.arange(n + 1) * dt)
    sc.r_BN_N = [float(v) for v in pos[0]]
    sc_msg.write(sc)
    sim.InitializeSimulation()
    low, lit = cap * soc0, 0.0
    for k in range(n):
        sc.r_BN_N = [float(v) for v in pos[k]]
        sc_msg.write(sc, macros.sec2nano(k * dt))
        sim.ConfigureStopTime(macros.sec2nano((k + 1) * dt))
        sim.ExecuteSimulation()
        q = bat.batPowerOutMsg.read().storageLevel
        low = min(low, q)
        lit += ecl.eclipseOutMsgs[0].read().shadowFactor
    return {"low_Wh": low / 3600.0, "end_Wh": bat.batPowerOutMsg.read().storageLevel / 3600.0, "lit": lit / n,
            "period_s": period}


class EclipsePower(Model):
    """Eclipses and the battery in space, and battery ageing everywhere.

    Wiring: ``columns`` asks for the craft's position relative to the Sun, Mars and Earth and its state
    relative to the central body; ``on_arc_end`` finds the shadow along the arc, takes the battery through it and
    raises ``FlightFailure`` when the battery cannot carry the longest shadow. ``state`` is the mission (today's
    ``sim.Mission``): it reads ``present()`` (elements with ``el.solar_m2``, ``el.rtg_units``, ``battery_Wh``),
    ``t``, ``t0`` and the load the power code set, ``power_load_W`` (default: data.BASE_POWER_W per element)."""
    name = "eclipse_power"
    area = "power"
    order = 61

    def configure(self, ctx):
        self.ctx = ctx
        self.full_cycles = 0.0
        self.log = []

    def design_fields(self):
        return ["elements[].battery_kWh", "elements[].solar_array_m2", "elements[].rtg_units"]

    def columns(self, arc, cols, dv):
        cols.add("rel_sun", dv.relative_position(arc.body, "Sun"), width=3, owner=self.name)
        cols.add("rel_mars", dv.relative_position(arc.body, "Mars"), width=3, owner=self.name)
        cols.add("rel_earth", dv.relative_position(arc.body, "Earth"), width=3, owner=self.name)
        if arc.central in MU:
            cols.add("rel_central_vel", dv.relative_velocity(arc.body, arc.central), width=3, owner=self.name)

    def capacity(self, years_since_launch):
        return capacity_fraction(years_since_launch + BATTERY_AGE_AT_LAUNCH_YEARS, self.full_cycles)

    def arc_shadow(self, arc, out):
        """Lit fraction and longest shadow of an arc, from its samples, or from its osculating orbit when the samples
        are sparser than a tenth of the orbit."""
        t, sun = out.series("rel_sun")
        sun = -np.asarray(sun)                    # Sun relative to the craft
        lit = np.ones(len(t))
        for body in ("Mars", "Earth"):
            _, rb = out.series("rel_" + body.lower())
            rb = -np.asarray(rb)
            lit = np.minimum(lit, shadow_factor(np.zeros(3), sun, rb, RADIUS[body]))
        res = {"lit": float(np.mean(lit)) if len(t) else 1.0, "longest_s": 0.0, "period_s": None}
        if arc.central in MU and "rel_central_vel" in out:
            _, rc = out.series("rel_" + arc.central.lower())
            _, vc = out.series("rel_central_vel")
            r0, v0 = np.asarray(rc)[0], np.asarray(vc)[0]
            try:
                _, period = kepler_states(r0, v0, MU[arc.central], [0.0])
            except ValueError:
                period = None
            if period and (arc.dt > period or len(t) < 10 * arc.dt / period):
                sun_rel = np.asarray(sun)[0] + r0            # Sun from the planet's centre
                orb = orbit_shadow(r0, v0, sun_rel, arc.central)
                res = {"lit": orb["lit"], "longest_s": min(orb["longest_s"], arc.dt), "period_s": period}
                return res
        # longest shadow along the samples themselves
        if len(t) > 1:
            dark = lit < 0.5
            best = run = 0.0
            for k in range(1, len(t)):
                run = run + (t[k] - t[k - 1]) if dark[k] else 0.0
                best = max(best, run)
            res["longest_s"] = best
        return res

    def on_arc_end(self, arc, out, state):
        if arc.in_atmosphere:
            return
        from ...core.common import FlightFailure
        pres = state.present()
        battery = sum(getattr(s, "battery_Wh", 0.0) for s in pres if getattr(s, "battery_Wh", 0.0) > 0)
        nuclear_years = (state.t - state.t0) / A.YEAR_S
        nuclear = sum(A.rtg_power_W(s.el.rtg_units, nuclear_years) for s in pres)
        area = sum(s.el.solar_m2 for s in pres)
        load = getattr(state, "power_load_W", None)
        if load is None:
            from ... import data as D
            load = D.BASE_POWER_W
        sh = self.arc_shadow(arc, out)
        # A grazing shadow under 30 s is mostly penumbra, where the arrays still give part of their power; it is
        # not a battery case (it failed vehicles with no battery for a 0.3 Wh need).
        if sh["longest_s"] < 30.0 or nuclear >= load:
            return
        cap = self.capacity(nuclear_years)
        need = eclipse_need_Wh(load, nuclear, sh["longest_s"])
        usable = battery * cap * BATTERY_DEPTH_OF_DISCHARGE
        if sh["period_s"]:
            n_orbits = arc.dt / sh["period_s"]
            dark_s = (1.0 - sh["lit"]) * sh["period_s"]
            self.full_cycles += n_orbits * eclipse_need_Wh(load, nuclear, dark_s) / max(battery * cap, 1e-9)
        self.log.append({"t": arc.t0, "lit": round(sh["lit"], 4), "shadow_s": round(sh["longest_s"], 1),
                         "need_Wh": round(need, 1), "usable_Wh": round(usable, 1), "capacity": round(cap, 4)})
        if need > usable:
            raise FlightFailure(
                f"the battery ran down in a {sh['longest_s'] / 60:.0f} minute shadow: it needs {need:.0f} Wh at "
                f"{load - nuclear:.0f} W but can give {usable:.0f} Wh ({battery:.0f} Wh, {cap:.0%} of its capacity "
                f"left, {BATTERY_DEPTH_OF_DISCHARGE:.0%} depth of discharge)")

    def obs(self, state):
        return {}


MODELS = [EclipsePower]
