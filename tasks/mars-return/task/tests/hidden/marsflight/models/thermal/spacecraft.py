"""Thermal balance of a spacecraft in cruise and in orbit: survival heater power, and a radiator's temperature.

**Heater power.** A spacecraft's survival heaters make up what sunlight and its own electronics do not give. Its mean
heater power falls as the sunlight on it rises and rises with the time it spends in shadow. Fitted to Mars Express's
measured heater currents: all 33 heater lines, hourly, 22 Aug 2008 to 14 Apr 2014, three Mars years (Petkovic et al.
2022, Scientific Data 9, 229; figshare 10.6084/m9.figshare.14350460, CC0), at the 28 V bus. Daily means:

    heater W = 213.0 - 0.1407 x (solar flux W/m^2 x lit fraction)

within 16.7 W rms of 2,493 days (mean 134 W, range 83 to 191 W). ESA's own empirical model of the same form
(a baseline plus terms in the solar constant, the Sun angle and the eclipse length) holds to 10 W over a day or more
(Ferretti et al., SpaceOps 2006, AIAA 2006-5857). For another craft the law scales with its outer area, taken as
(dry mass / Mars Express's 666 kg) ^ (2/3): Mars Express launched at 1,123 kg with 457 kg of propellant (ESA).

**Radiator.** Basilisk's ``sensorThermal`` (AVS Lab, ISC license, ``bsk==2.12.1``) integrates one radiating node: the
sunlight it absorbs, the heat put into it and what it radiates. ``radiator_temperature_K`` runs it to steady state.
Checked against the MSL cruise stage's heat rejection system: 6 m^2 of silver-Teflon radiators rejecting about
2,150 W (the MMRTG's waste heat collected by the loop plus the rover's 150 W and the cruise electronics' 50 W), with
the fluid at about 70 C leaving the MMRTG and 10 to 15 C leaving the radiator, the radiator about 20 C below the fluid
(Bhandari et al., "Mars Science Laboratory Thermal Control Architecture", ICES 2005, SAE 2005-01-2828; Bhandari et al.
2006, JPL hdl:2014/40684).
"""
from __future__ import annotations

import math

from ...core.model import Model

MEX_HEATER = {"base_W": 213.0, "per_sun_W_per_W_m2": 0.1407, "dry_kg": 666.0, "rms_W": 16.7}
SILVER_TEFLON = {"absorptance": 0.09, "emittance": 0.78}   # typical new 5 mil silver Teflon (Gilmore, Spacecraft Thermal Control Handbook, vol. 1)
SIGMA = 5.670374419e-8


def bus_heater_W(solar_flux_W_m2: float, lit_fraction: float = 1.0, dry_kg: float = MEX_HEATER["dry_kg"]) -> float:
    """Mean survival heater power (W) of a spacecraft bus of `dry_kg`, at a solar flux and lit fraction."""
    scale = (max(dry_kg, 1.0) / MEX_HEATER["dry_kg"]) ** (2.0 / 3.0)
    w = MEX_HEATER["base_W"] - MEX_HEATER["per_sun_W_per_W_m2"] * solar_flux_W_m2 * max(0.0, min(1.0, lit_fraction))
    return max(0.0, w) * scale


def radiator_steady_K(area_m2, heat_W, absorbed_sun_W=0.0, emittance=SILVER_TEFLON["emittance"]):
    """Closed-form steady temperature of a one-sided radiator."""
    return ((heat_W + absorbed_sun_W) / (emittance * SIGMA * area_m2)) ** 0.25


def radiator_temperature_K(area_m2, heat_W, r_au=1.0, cos_sun=0.0, absorptance=SILVER_TEFLON["absorptance"],
                           emittance=SILVER_TEFLON["emittance"], mass_kg=60.0, cp=900.0, T0=290.0, hours=30.0,
                           dt=10.0):
    """Temperature (K) of a radiator node flown by Basilisk's sensorThermal until steady: `heat_W` put into it, the Sun
    at r_au and cos_sun from its normal."""
    from Basilisk.architecture import messaging
    from Basilisk.simulation import sensorThermal
    from Basilisk.utilities import SimulationBaseClass, macros

    sim = SimulationBaseClass.SimBaseClass()
    proc = sim.CreateNewProcess("p")
    proc.addTask(sim.CreateNewTask("t", macros.sec2nano(dt)))
    au = 1.495978707e11
    c = max(-1.0, min(1.0, cos_sun))
    sun = messaging.SpicePlanetStateMsgPayload()
    sun.PositionVector = [r_au * au * math.sqrt(1 - c * c), 0.0, r_au * au * c]
    sun_msg = messaging.SpicePlanetStateMsg().write(sun)
    st = messaging.SCStatesMsgPayload()
    st.r_BN_N = [0.0, 0.0, 0.0]
    st_msg = messaging.SCStatesMsg().write(st)
    node = sensorThermal.SensorThermal()
    node.ModelTag = "radiator"
    node.nHat_B = [0.0, 0.0, 1.0]
    node.sensorArea = float(area_m2)
    node.sensorAbsorptivity = float(absorptance)
    node.sensorEmissivity = float(emittance)
    node.sensorMass = float(mass_kg)
    node.sensorSpecificHeat = float(cp)
    node.sensorPowerDraw = float(heat_W)
    node.T_0 = float(T0) - 273.15
    node.sunInMsg.subscribeTo(sun_msg)
    node.stateInMsg.subscribeTo(st_msg)
    sim.AddModelToTask("t", node)
    sim.InitializeSimulation()
    sim.ConfigureStopTime(macros.sec2nano(hours * 3600.0))
    sim.ExecuteSimulation()
    return float(node.temperatureOutMsg.read().temperature) + 273.15


class SpacecraftThermal(Model):
    """Survival heaters of each powered element in cruise and in orbit, added to the power demand by the core's
    power balance (sim.py ``resources``, space branch): ``heater_W(r_au, lit_fraction, dry_kg)`` per element."""
    name = "spacecraft_thermal"
    area = "thermal"
    order = 57

    def configure(self, ctx):
        self.ctx = ctx

    @staticmethod
    def heater_W(r_au, lit_fraction, dry_kg):
        return bus_heater_W(1361.0 / r_au ** 2, lit_fraction, dry_kg)


MODELS = [SpacecraftThermal]
