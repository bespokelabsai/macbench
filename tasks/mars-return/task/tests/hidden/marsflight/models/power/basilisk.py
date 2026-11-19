"""Electrical power on the Martian surface, sol by sol, with Basilisk's power modules.

Basilisk (AVS Lab, University of Colorado Boulder; ISC license; ``bsk==2.12.1``) gives:
- ``simpleSolarPanel``: power = solar flux at the panel's distance from the Sun x panel area x efficiency x the
  cosine of the Sun's angle from the panel normal (zero when the Sun is behind the panel);
- ``simplePowerSink``: a constant load;
- ``simpleBattery``: stored energy, the running sum of every node's power, held between empty and full.

What Basilisk does not have, and this file adds: where the Sun is in the sky of a place on Mars (latitude, season
Ls and local time; Mars's obliquity 25.19 deg), and how much of the sunlight the dusty air and the dust on the
panels let through. The air is the simulator's own rule (``exp(-0.6 tau)``, sim.py ``resources``) unless the caller
chooses the fit 1 / (1 + 0.3 tau) to Crisp et al. (2004) given by Lorenz et al. (2020, Earth and Space Science,
doi:10.1029/2019EA000992). Both are folded into the panel's efficiency each step, which Basilisk allows.

``fly_sols`` flies a horizontal array, its loads and its battery through whole sols at a fixed step, and gives the
energy per sol and the battery's lowest charge. sim.py's rule instead takes a day's average sunlight
(``0.30 x exp(-0.6 tau)`` of the flux at 1.524 AU) and asks the battery to cover the load for 12.3 hours.
results/issue-111/README.md compares the two against InSight's first sol.
"""
from __future__ import annotations

import math

from ...core.model import Model

OBLIQUITY_DEG = 25.19              # Mars (Allison and McEwen 2000)
SOL_S = 88775.244
AU_M = 1.495978707e11


def sun_enu(lat_deg, ls_deg, frac):
    """Unit vector to the Sun in local east, north, up at a latitude, season Ls (deg) and fraction of the sol
    from local midnight (0.5 is noon; local true solar time)."""
    phi = math.radians(lat_deg)
    dec = math.asin(math.sin(math.radians(OBLIQUITY_DEG)) * math.sin(math.radians(ls_deg)))
    h = 2.0 * math.pi * (frac - 0.5)
    e = -math.cos(dec) * math.sin(h)
    n = math.cos(phi) * math.sin(dec) - math.sin(phi) * math.cos(dec) * math.cos(h)
    u = math.sin(phi) * math.sin(dec) + math.cos(phi) * math.cos(dec) * math.cos(h)
    return e, n, u


def transmission(tau, kind="rule"):
    """Share of the sunlight that reaches the panel through dust of optical depth tau."""
    if kind == "rule":
        return math.exp(-0.6 * tau)
    if kind == "crisp":
        return 1.0 / (1.0 + 0.3 * tau)
    raise ValueError(kind)


def fly_sols(area_m2, efficiency, lat_deg, ls_deg, r_au, tau, loads_W, battery_Wh, sols=1.0, dust=1.0, soc0=1.0,
             dt=60.0, air="rule", keep_series=False):
    """A horizontal solar array, constant loads and a battery on the surface, flown by Basilisk at a fixed step.
    Starts at local midnight. Returns the array's energy per sol (Wh), the battery's lowest charge (Wh), the
    seconds it spent empty, and optionally the series (t, array W, stored Wh)."""
    from Basilisk.architecture import messaging
    from Basilisk.simulation import simpleBattery, simplePowerSink, simpleSolarPanel
    from Basilisk.utilities import SimulationBaseClass, macros

    sim = SimulationBaseClass.SimBaseClass()
    proc = sim.CreateNewProcess("power")
    proc.addTask(sim.CreateNewTask("power", macros.sec2nano(dt)))
    # the "spacecraft" is the lander at the origin, body axes = east, north, up
    st = messaging.SCStatesMsgPayload()
    st.r_BN_N = [0.0, 0.0, 0.0]
    st.sigma_BN = [0.0, 0.0, 0.0]
    st_msg = messaging.SCStatesMsg().write(st)
    sun = messaging.SpicePlanetStateMsgPayload()
    sun_msg = messaging.SpicePlanetStateMsg()
    panel = simpleSolarPanel.SimpleSolarPanel()
    panel.ModelTag = "array"
    panel.setPanelParameters([0.0, 0.0, 1.0], float(area_m2), float(efficiency))
    panel.stateInMsg.subscribeTo(st_msg)
    panel.sunInMsg.subscribeTo(sun_msg)
    sinks = []
    for i, w in enumerate(loads_W):
        s = simplePowerSink.SimplePowerSink()
        s.ModelTag = f"load{i}"
        s.nodePowerOut = -float(w)
        sinks.append(s)
    bat = simpleBattery.SimpleBattery()
    bat.ModelTag = "battery"
    cap = max(float(battery_Wh), 1e-6) * 3600.0
    bat.storageCapacity = cap
    bat.storedCharge_Init = soc0 * cap
    bat.addPowerNodeToModel(panel.nodePowerOutMsg)
    for s in sinks:
        bat.addPowerNodeToModel(s.nodePowerOutMsg)
    sim.AddModelToTask("power", panel, 30)
    for s in sinks:
        sim.AddModelToTask("power", s, 20)
    sim.AddModelToTask("power", bat, 10)
    trans = transmission(tau, air) * dust

    def aim(t):
        e, n, u = sun_enu(lat_deg, ls_deg, (t / SOL_S) % 1.0)
        d = r_au * AU_M
        sun.PositionVector = [e * d, n * d, u * d]
        sun_msg.write(sun, macros.sec2nano(t))
        panel.panelEfficiency = efficiency * trans if u > 0 else 0.0

    aim(0.0)
    sim.InitializeSimulation()
    n = int(round(sols * SOL_S / dt))
    energy = 0.0
    low = soc0 * cap
    empty = 0.0
    series = []
    for k in range(n):
        t = k * dt
        aim(t + 0.5 * dt)
        sim.ConfigureStopTime(macros.sec2nano(t + dt))
        sim.ExecuteSimulation()
        p = panel.nodePowerOutMsg.read().netPower
        q = bat.batPowerOutMsg.read().storageLevel
        energy += p * dt
        low = min(low, q)
        if q <= 1e-6:
            empty += dt
        if keep_series:
            series.append((t + dt, p, q / 3600.0))
    out = {"array_Wh_per_sol": energy / 3600.0 / sols, "battery_low_Wh": low / 3600.0, "empty_s": empty}
    if keep_series:
        out["series"] = series
    return out


def rule_supply_W(area_m2, efficiency, tau, dust=1.0, r_au=1.524):
    """sim.py's daily average surface supply: flux at r_au x 0.30 x exp(-0.6 tau) x dust x efficiency x area."""
    return area_m2 * efficiency * 1361.0 / r_au ** 2 * 0.30 * math.exp(-0.6 * tau) * dust


class BasiliskPower(Model):
    """Surface power, a sol at a time. The core calls ``surface_sol`` once per sol on the surface with the
    lander's arrays, loads and battery; it returns the sol's energy and whether the battery ran empty. Not wired
    in yet: sim.py's ``resources`` still decides."""
    name = "power_basilisk"
    area = "power"
    order = 60

    def configure(self, ctx):
        self.ctx = ctx
        self.soc = None

    def design_fields(self):
        return ["elements[].solar_array_m2", "elements[].battery_kWh"]

    def surface_sol(self, area_m2, efficiency, lat_deg, ls_deg, r_au, tau, dust, loads_W, battery_Wh, dt=120.0):
        soc0 = 1.0 if self.soc is None else self.soc
        out = fly_sols(area_m2, efficiency, lat_deg, ls_deg, r_au, tau, loads_W, battery_Wh, 1.0, dust, soc0, dt,
                       keep_series=True)
        self.soc = out["series"][-1][2] / max(battery_Wh, 1e-6) if battery_Wh > 0 else 0.0
        return out


MODELS = [BasiliskPower]
