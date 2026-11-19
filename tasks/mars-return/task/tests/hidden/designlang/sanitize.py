"""Turn the simulator's build messages into messages a team may read.

The simulator's own messages name its internal fields and print numbers it derives from hidden data,
e.g. "hoop stress 512 MPa > 380 MPa allowed" gives away a material allowable. Every message a team can
see passes through `team_message`. Each known message has a rule that keeps only what the team wrote
or what the manual publishes; any message without a rule becomes a plain sentence that names the
element and no number. A leak test scans team-facing messages for hidden numbers.
"""
from __future__ import annotations

import re

# Internal design fields and the SysML attribute a team wrote for each. Fields the compiler derives
# from hidden data (areal masses, trimmed L/D, capacities) have no unit here and report no range.
FIELDS = {
    "diameter_m": ("outerDiameter, baseDiameter or nominalDiameter", 'm'),
    "length_m": ("length", 'm'),
    "half_angle_deg": ("halfAngle", 'deg'),
    "nose_radius_m": ("noseRadius", None),
    "tank_pressure_bar": ("PressurizationSystem.meop", 'bar'),
    "ullage_fraction": ("ullageFraction", ''),
    "mli_layers": ("MultiLayerInsulation.layers", 'layers'),
    "propellant_kg": ("propellantLoad", 'kg'),
    "tank_capacity_kg": ("the tank volumes", None),
    "oxidizer_loaded_fraction": ("oxidizerLoadFraction", ''),
    "areal_mass_kg_m2": ("ThermalProtection.thickness", None),
    "ld_trim": ("trimAngleOfAttack", None),
    "structure_mass_kg": ("SampleContainmentAssembly.mass", 'kg'),
    "sample_containers": ("SampleContainmentAssembly.sampleContainers", 'containers'),
    "g_limit": ("DecelerationLimit.maxLoadFactor", 'Earth g'),
    "design_q_Pa": ("designDynamicPressure", 'Pa'),
    "touchdown_speed_mps": ("LandingGear.touchdownSpeedRating", 'm/s'),
    "battery_kWh": ("Battery.energy", 'kWh'),
    "solar_array_m2": ("SolarArray.area", 'm^2'),
    "rtg_units": ("the number of MMRTG parts", 'units'),
    "fission_units": ("the number of FissionPowerUnit parts", 'units'),
    "cryocooler_kW": ("Cryocooler.inputPower", 'kW'),
    "isru_kW": ("IsruOxygenPlant.inputPower", 'kW'),
    "rcs_prop_kg": ("ReactionControlSystem.propellantLoad", 'kg'),
    "rcs_thrust_N": ("ReactionControlSystem.totalThrust", 'N'),
    "surface_insulation_W_per_K_m2": ("SurfaceThermalBlanket.heatLeakCoefficient", 'W/(m^2*K)'),
    "extra_mass_kg": ("EquipmentItem.mass", 'kg'),
    "slew_rate_deg_s": ("AttitudeControlSystem.maxSlewRate", 'deg/s'),
    "qual_acoustic_dB": ("QualificationLevels.acousticLevel", 'dB'),
    "qual_shock_g": ("QualificationLevels.shockLevel", 'g'),
    "pc_bar": ("chamberPressure", 'bar'),
    "mixture_ratio": ("mixtureRatio", ''),
    "expansion_ratio": ("areaRatio", ''),
    "throat_diameter_m": ("throatDiameter", 'm'),
    "film_cooling_fraction": ("filmCoolingFraction", ''),
    "gimbal_deg": ("gimbalRange", 'deg'),
    "igniter_units": ("igniterUnits", 'units'),
    "stringer_height_mm": ("StringerRingStiffening.stringerHeight", 'mm'),
    "stringer_thickness_mm": ("StringerRingStiffening.stringerThickness", 'mm'),
    "stringer_spacing_mm": ("StringerRingStiffening.stringerSpacing", 'mm'),
    "stringer_flange_mm": ("StringerRingStiffening.stringerFlangeWidth", 'mm'),
    "ring_spacing_mm": ("StringerRingStiffening.ringSpacing", 'mm'),
    "ring_height_mm": ("StringerRingStiffening.ringHeight", 'mm'),
    "ring_thickness_mm": ("StringerRingStiffening.ringThickness", 'mm'),
    # fields of the wired models
    "gauge": ("PropellantGauging.gauge", None),
    "low_level_cutoff": ("PropellantGauging.lowLevelCutoff", None),
    "rcs_branches": ("Redundancy.rcsBranches", ''),
    "landing_radars": ("Redundancy.landingRadars", ''),
    "pyro_initiators": ("Redundancy.pyroInitiators", ''),
    "battery_strings": ("Redundancy.batteryStrings", ''),
    "isru_units": ("Redundancy.isruUnits", ''),
    "hga_m": ("TelecomSystem.highGainAntennaDiameter", 'm'),
    "tx_rf_W": ("TelecomSystem.transmitPower", 'W'),
    "sample_alpha_over_eps": ("SampleContainmentAssembly.sampleCarrierAlphaOverEpsilon", ''),
    "passes_per_day": ("MissionStack.dsnPassesPerDay", ''),
    "ground_wind_limit_ms": ("MissionStack.groundWindLimit", 'm/s'),
    "launch.site": ("MissionStack.launchSite", None),
    # design fields passed through for the wired models (fins, propulsion flaws, plumes)
    "count": ("FinSet.finCount", 'fins'),
    "span_m": ("FinSet.span", 'm'),
    "root_chord_m": ("FinSet.rootChord", 'm'),
    "tip_chord_m": ("FinSet.tipChord", 'm'),
    "sweep_m": ("FinSet.sweep", 'm'),
    "pyro_isolation_valves": ("PressurizationSystem.pyroIsolationValves", None),
    "pulsed": ("RocketEngine.pulsed", None),
}

_W = r"(?P<where>(?:element|engine|stage|fairing|heatshield) [A-Za-z0-9_]+(?: legs| wall_thickness_mm| stiffeners| pogo_accumulator_L| fins)?)"
_N = r"-?[0-9.eE+\-]+"

RULES = [
    # the sizes the program builds: the team's own sizes and counts against the manual's limits
    (re.compile(r"^element \S+: (?:a|a rigid) \w+ [0-9.e+]+ m across is wider than the program builds \([0-9.]+ m\)$"), None),
    (re.compile(r"^element \S+: \d+ engines on one stage, more than the program integrates \(\d+\)$"), None),
    (re.compile(r"^\d+ disk gap band parachutes \(.*\): the program qualifies at most \d+ supersonic canopy"), None),
    # ranges
    (re.compile(_W + r": (?P<key>[a-zA-Z_0-9]+)=(?P<v>" + _N + r") outside \[(?P<lo>" + _N + r"), (?P<hi>" + _N + r")\]"), "range"),
    (re.compile(r"^(?:engine (?P<eng>[A-Za-z0-9_]+): )?(?P<key>[a-zA-Z_0-9]+)=(?P<v>" + _N + r") outside \[(?P<lo>" + _N + r"), (?P<hi>" + _N + r")\]$"), "range"),
    # walls and pressure
    (re.compile(_W + r": (?P<role>fuel|ox) tank wall [0-9.]+ mm yields at proof pressure .*factor of safety (?P<f>[0-9.]+)\)"),
     lambda m: f"{m['where']}: the {_role(m['role'])} tank wall yields at proof pressure, with the factor of safety {m['f']} "
               "(1.6 untested, 1.25 after a passed structural qualification)"),
    (re.compile(_W + r": tank pressure (?P<p>[0-9.]+) bar is below the (?P<need>[0-9.]+) bar engine (?P<e>\S+) needs at its inlet"),
     lambda m: f"{m['where']}: the tank MEOP of {m['p']} bar is below the {m['need']} bar engine {m['e']} needs at its inlet "
               "(pressure-fed engines need 1.25 times chamber pressure plus 2 bar, pumps 1.8 bar)"),
    (re.compile(_W + r": electric pumps draw .*"),
     lambda m: f"{m['where']}: the battery cannot deliver the power the electric pumps draw; add Battery energy"),
    # stack fit
    (re.compile(r"fairing (?P<id>\S+) is [0-9.]+ m long but the payload stack above it is about [0-9.]+ m"),
     lambda m: f"fairing {m['id']} is too short for the elements above it"),
    (re.compile(r"fairing (?P<id>\S+) diameter (?P<d>[0-9.]+) m is less than the payload's (?P<n>[0-9.]+) m \(incl. 0.2 m clearance\)"),
     lambda m: f"fairing {m['id']} ({m['d']} m) is narrower than the widest element above it plus 0.2 m of clearance"),
    (re.compile(r"element (?P<x>\S+) \((?P<d>[0-9.]+) m\) does not fit inside aeroshell (?P<hs>\S+) \((?P<D>[0-9.]+) m; 92% usable\)"), None),
    # engines: keep the rules, drop the model's numbers
    (re.compile(r"(?P<pre>.*)gas-generator turbine needs [0-9.]+% of the flow; above 15% the cycle does not close"),
     lambda m: f"{m['pre']}the gas-generator power balance does not close (the turbine would need more than 15% of the flow)"),
    (re.compile(r"(?P<pre>.*)regenerative cooling cannot absorb the wall heat load \(capacity/load = [0-9.]+\);(?P<rest>.*)"),
     lambda m: f"{m['pre']}regenerative cooling cannot absorb the wall heat load;{m['rest']}"),
    (re.compile(r"(?P<pre>.*)chamber pressure (?P<pc>[0-9]+) bar exceeds the (?P<c>\S+) envelope of (?P<mx>[0-9]+) bar"), None),
    # tank options
    (re.compile(_W + r": balloon tanks must be .*"),
     lambda m: f"{m['where']}: balloon tanks must be AISI 301 full hard, the thin cold-rolled stainless steel of Atlas and Centaur"),
    (re.compile(_W + r": stiffeners overlap.*"),
     lambda m: f"{m['where']}: the stiffeners overlap. Each spacing must be more than twice the stringer or ring thickness"),
    # RCS limits: the cap is a share of the element's own mass, which is hidden
    (re.compile(_W + r": rcs_prop_kg=.* is more than an RCS carries.*"),
     lambda m: f"{m['where']}: ReactionControlSystem.propellantLoad is more than an RCS carries: at most 25% of the "
               "element's own mass without it (dry mass plus main propellant), or 50 kg"),
    # the compiler
    (re.compile(r"(?P<where>stage \S+): the tanks hold .* the load is .*"),
     lambda m: f"{m['where']}: the tanks are too small for the propellantLoad at the engines' mixture ratio"),
    (re.compile(r"(?P<pre>.*)thermochemistry failed: .*"),
     lambda m: f"{m['pre']}NASA CEA found no solution for these propellants, chamber pressure, mixture ratio and area ratio"),
]
# Messages with no number in them that the simulator writes about rules the manual states.
SAFE = [re.compile(p) for p in (
    r"is not compatible with LOX", r"has engines but no propellant capacity", r"not a supported pair",
    r"burns \S+/\S+, but the stage holds", r"must share a mixture ratio", r"the vehicle has no stage with engines",
    r"the vehicle needs exactly one capsule", r"needs a backshell above it", r"expander cycle needs",
    r"oxidizer-rich staged combustion", r"full-flow staged combustion", r"fuel-rich staged combustion",
    r"cannot regeneratively cool", r"ablative chambers are limited to 30 bar", r"radiation-cooled chambers are limited",
    r"is not hypergolic", r"spark/torch igniters need", r"chambers need fuel film cooling of at least 0.10",
    r"fewer than 10 MLI layers", r"nozzle flow separates at sea level", r"duplicate id",
    r"balloon tanks take no compression in the skin", r"stiffeners must be an object",
    r"domeAspectRatio [0-9.]+; the v1 compiler takes 0.7071", r"so its radius must be the stage radius",
    r"the simulator takes one", r"but the tanks hold \S+/\S+$", r"needs loadedMixtureRatio",
    r"launch.site .* is not one of the published pads", r"launchSite must be",   # public pad names
)]
_ANY_NUMBER = re.compile(r"\d")


def _role(r):
    return "oxidizer" if r == "ox" else "fuel"


def _range(m):
    key = m["key"]
    attr, unit = FIELDS.get(key, (key, None))
    where = m.groupdict().get("where") or (f"engine {m['eng']}" if m.groupdict().get("eng") else "the design")
    acc = " pogo_accumulator_L" in where
    where = where.replace(" wall_thickness_mm", "").replace(" legs", "").replace(" stiffeners", "").replace(" pogo_accumulator_L", "").replace(" fins", "")
    if acc and key in ("fuel", "ox"):
        return (f"{where}: PogoAccumulator.{'oxidizerGasVolume' if key == 'ox' else 'fuelGasVolume'} is outside the range "
                "the program office accepts (0 to 5000 L)")
    if key in ("fuel", "ox"):
        return f"{where}: the {_role(key)} tank's wallThickness is outside what the program office can build (0.8 to 100 mm)"
    if unit is not None:
        return (f"{where}: {attr} is outside the range the program office accepts "
                f"({float(m['lo']):g} to {float(m['hi']):g}{' ' + unit if unit else ''})")
    return f"{where}: {attr} gives a value outside the range the program office can build"


def team_message(msg: str) -> str:
    """The message a team may read for one simulator or compiler message."""
    msg = str(msg)
    for rx, rule in RULES:
        m = rx.match(msg) or rx.search(msg)
        if m:
            if rule is None:
                return msg
            if rule == "range":
                return _range(m)
            return rule(m)
    if any(rx.search(msg) for rx in SAFE):
        return msg
    if not _ANY_NUMBER.search(msg.split(":", 1)[-1]):
        return msg
    where = msg.split(":", 1)[0] if ":" in msg else "the design"
    return f"{where}: the program office's build review rejects this part (no detail can be given)"


def team_messages(msgs):
    out = []
    for m in msgs:
        t = team_message(m)
        if t not in out:
            out.append(t)
    return out
