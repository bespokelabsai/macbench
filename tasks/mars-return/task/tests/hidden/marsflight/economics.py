"""Program economics: what the vehicle costs to build, and the program's two budgets.

Every part is priced from the design the team submits, the same way masses are: nothing is declared.
Prices are engineering estimates for a modern commercial program. They are calibrated against public
figures for flown hardware (see REFERENCE.md, "Costs"), and they are game prices, not quotes.
dev/price_check.py compares them with published costs of flown hardware by element class.

- Hardware budget: everything on the vehicle (engines, tanks, structure, heat shields, parachutes,
  power, avionics), the propellant loaded at launch, and launch operations.
- R&D budget: the tests the team orders through the program office (see program.py). Test articles,
  facility time and propellant for tests are paid from it.

Engines follow a learning curve: the n-th unit of one engine design costs U1 * n^log2(0.85). Engines
bought from the flight-proven catalog have a fixed unit price and no learning.
"""
from __future__ import annotations

import math

from . import data as D

# Set from the reference mission so that both budgets bind. Its SysML design costs $597M, so $800M leaves
# room for a less optimized design; a design over $800M cannot be built and scores 0. The grade values a full success
# at 2 x these budgets / 0.90 (grading.MISSION_VALUE_USD), so every dollar under them is net value. $120M of R&D
# fits a sensible test plan (the reference's, $70M) but not everything: in an earlier version, with $1.2B and $300M,
# teams spent $658M to $877M on hardware and $176M to $212M on tests, and no team had to give up one part to pay for
# another.
HARDWARE_BUDGET_USD = 1600e6   # doubled from $800M
RND_BUDGET_USD = 240e6        # doubled from $120M

LEARNING = 0.85
_B = math.log2(LEARNING)

# First-unit engine cost (USD) for a 1 MN engine at 100 bar, by cycle; scaled by thrust^0.6,
# chamber pressure^0.25 and propellant. Calibrated so that first units fall between commercial and
# legacy prices for Merlin 1D, Raptor 2, RS-25, RL10B-2, RD-180, AJ10-190 and Rutherford.
ENGINE_K_USD = {
    "pressure_fed": 1.6e6,
    "electric_pump": 2.0e6,
    "gas_generator": 3.0e6,
    "expander": 15.0e6,
    "staged_ox": 7.0e6,
    "staged_fuel": 14.0e6,
    "full_flow": 6.5e6,
}
ENGINE_FIXED_USD = 0.4e6               # a floor every engine pays: valves, controller, qualification paperwork
ENGINE_PROP_FACTOR = {"LH2": 1.5, "LCH4": 1.0, "RP1": 0.9, "MMH": 1.2}
ENGINE_COOLING_FACTOR = {"regen": 1.0, "ablative": 0.6, "radiative": 0.7}

# Structure and systems, USD per kg of the computed mass (fabrication included).
MATERIAL_USD_PER_KG = {"SS301": 150.0, "Al2219": 900.0, "Al2195": 1600.0, "Ti6Al4V": 2500.0, "CFRP": 3000.0}
STAGE_INTEGRATION_USD = 4.0e6
ENGINE_INSTALL_USD = 0.15e6
AVIONICS_USD = 3.0e6
THRUST_STRUCTURE_USD_PER_KG = 1000.0
INTERSTAGE_USD_PER_KG = 1500.0
PRESSURANT_SYSTEM_USD_PER_KG = 800.0     # helium and COPVs
STAGE_SYSTEMS_USD_PER_KG = 1200.0        # skirts, intertank, feed and pressurization lines, valves, wiring
FOAM_USD_PER_M2 = 300.0
MLI_USD_PER_M2 = 500.0
MLI_USD_PER_M2_PER_LAYER = 40.0
FAIRING_USD_PER_M2 = 25e3
LEGS_USD_PER_KG = 8e3
# Landing radar, telecom, thermal control, and assembly and test as a spacecraft, once per element with legs
# (terrain-relative navigation is its own part, priced separately). Set so that a Nova-C-class lunar lander costs about what NASA paid for IM-1 ($122M
# in 2024) less a Falcon 9 launch at list price ($70M). Without it, such a lander came to $11M (dev/price_check.py).
LANDING_SYSTEM_USD = 40e6
# Thermal protection, USD per m^2 of forebody at 10 kg/m^2 (scaled by (areal mass / 10)^0.5).
TPS_USD_PER_M2 = {"SLA561V": 60e3, "PICA": 200e3, "Avcoat": 250e3, "carbon_phenolic": 350e3}
AEROSHELL_STRUCTURE_USD_PER_KG = 8e3
CAPSULE_STRUCTURE_USD_PER_KG = 30e3
CAPSULE_FIXED_USD = 10e6                 # sample containment, sealing, beacons and recovery aids
PARACHUTE_FIXED_USD = 1.0e6
PARACHUTE_USD_PER_M2_DIAMETER = 10e3     # times (design q / 500 Pa)^0.5
RCS_FIXED_USD = 1.5e6
RCS_USD_PER_N = 50.0
RCS_USD_PER_KG_PROP = 200.0              # tanks and lines, per kg of propellant capacity
SOLAR_USD_PER_M2 = 250e3
BATTERY_USD_PER_KWH = 15e3
# RTGs: a fixed cost once per vehicle that carries any (fueling campaign, nuclear launch approval) plus a cost
# per MMRTG-class unit. NASA charged New Frontiers 4 proposers $103M, $133M and $163M for 1, 2 and 3 MMRTGs
# (FY2015 dollars, launch approval included), which is $95M + $40M per unit in 2024 dollars.
RTG_USD = 40e6                           # per MMRTG-class unit
RTG_FIXED_USD = 95e6                     # once per vehicle with RTGs
FISSION_USD = 250e6                      # per 10 kWe unit
CRYOCOOLER_USD_PER_KW = 4e6
ISRU_USD_PER_KW = 25e6
EXTRA_MASS_USD_PER_KG = 5e3
INSULATION_USD_PER_KG = 3e3
PROPELLANT_USD_PER_KG = {"LOX": 0.25, "LCH4": 0.8, "RP1": 2.0, "LH2": 8.0, "MMH": 200.0, "NTO": 80.0}
HYDRAZINE_USD_PER_KG = 250.0
LAUNCH_OPERATIONS_USD = 20e6

# Flight-proven engines a team may buy instead of developing its own. Each is a fixed design
# (built by the same engine model), at a fixed unit price, with flight-proven reliability and no
# hidden flaws. Prices are approximate public figures. Throat diameters are set so the engine model
# gives each engine's published vacuum thrust.
CATALOG_ENGINES = {
    "Merlin 1D": {"spec": {"fuel": "RP1", "oxidizer": "LOX", "cycle": "gas_generator", "pc_bar": 97, "mixture_ratio": 2.36,
                           "expansion_ratio": 16, "throat_diameter_m": 0.258, "cooling": "regen", "igniter": "tea_teb",
                           "igniter_units": 3, "injector": "pintle", "gimbal_deg": 5},
                  "unit_usd": 1.5e6, "note": "SpaceX first stage engine", "thrust_vac_kN": 914, "isp_vac_s": 311, "mass_kg": 470},
    "Merlin 1D Vacuum": {"spec": {"fuel": "RP1", "oxidizer": "LOX", "cycle": "gas_generator", "pc_bar": 97, "mixture_ratio": 2.36,
                                  "expansion_ratio": 165, "throat_diameter_m": 0.255, "cooling": "regen", "igniter": "tea_teb",
                                  "igniter_units": 4, "injector": "pintle", "gimbal_deg": 4},
                         "unit_usd": 2.5e6, "note": "SpaceX upper stage engine", "thrust_vac_kN": 981, "isp_vac_s": 348, "mass_kg": 490},
    "Raptor 2": {"spec": {"fuel": "LCH4", "oxidizer": "LOX", "cycle": "full_flow", "pc_bar": 300, "mixture_ratio": 3.6,
                          "expansion_ratio": 34, "throat_diameter_m": 0.233, "cooling": "regen", "igniter": "spark_torch",
                          "gimbal_deg": 15},
                 "unit_usd": 2.0e6, "note": "SpaceX Starship booster engine", "thrust_vac_kN": 2450, "isp_vac_s": 347, "mass_kg": 1630},
    "Raptor Vacuum": {"spec": {"fuel": "LCH4", "oxidizer": "LOX", "cycle": "full_flow", "pc_bar": 300, "mixture_ratio": 3.6,
                               "expansion_ratio": 90, "throat_diameter_m": 0.233, "cooling": "regen", "igniter": "spark_torch",
                               "gimbal_deg": 0},
                      "unit_usd": 4.0e6, "note": "SpaceX Starship vacuum engine (fixed nozzle)", "thrust_vac_kN": 2530, "isp_vac_s": 380, "mass_kg": 2100},
    "RS-25": {"spec": {"fuel": "LH2", "oxidizer": "LOX", "cycle": "staged_fuel", "pc_bar": 206.4, "mixture_ratio": 6.0,
                       "expansion_ratio": 69, "throat_diameter_m": 0.271, "cooling": "regen", "igniter": "spark_torch",
                       "gimbal_deg": 10.5},
              "unit_usd": 100e6, "note": "Space Shuttle and SLS core stage engine", "thrust_vac_kN": 2279, "isp_vac_s": 452.3, "mass_kg": 3177},
    "RL10B-2": {"spec": {"fuel": "LH2", "oxidizer": "LOX", "cycle": "expander", "pc_bar": 44.1, "mixture_ratio": 5.88,
                         "expansion_ratio": 280, "throat_diameter_m": 0.126, "cooling": "regen", "igniter": "spark_torch",
                         "gimbal_deg": 4},
                "unit_usd": 15e6, "note": "Delta IV and SLS upper stage engine", "thrust_vac_kN": 110, "isp_vac_s": 465.5, "mass_kg": 301},
    "AJ10-190": {"spec": {"fuel": "MMH", "oxidizer": "NTO", "cycle": "pressure_fed", "pc_bar": 8.6, "mixture_ratio": 1.65,
                          "expansion_ratio": 55, "throat_diameter_m": 0.146, "cooling": "regen", "igniter": "hypergolic",
                          "gimbal_deg": 6},
                 "unit_usd": 6e6, "note": "Shuttle OMS and Orion service module engine", "thrust_vac_kN": 26.7, "isp_vac_s": 316, "mass_kg": 118},
}


def build_catalog_engine(name: str, catalog_name: str, chilldown: str | None = None):
    """A flight-proven engine: the engine model's geometry and limits, with the published vacuum thrust
    (throat and exit areas scaled to it), vacuum Isp and dry mass. `chilldown` is the stage's choice for a cryogenic
    engine ("dump" or "recirculation"), the one engine field a design sets on a bought engine."""
    from .engine import build_engine
    c = CATALOG_ENGINES[catalog_name]
    en = build_engine(name, dict(c["spec"], **({"chilldown": chilldown} if chilldown is not None else {})))
    en.catalog = catalog_name
    if not en.errors:
        scale = 1e3 * float(c["thrust_vac_kN"]) / en.thrust_vac   # throat and exit areas scale with thrust
        en.At *= scale
        en.Ae *= scale
        en.thrust_vac = 1e3 * float(c["thrust_vac_kN"])
        en.isp_vac = float(c["isp_vac_s"])
        en.mdot = en.thrust_vac / (en.isp_vac * D.G0)
        en.mass = float(c["mass_kg"])
    return en


def engine_first_unit_usd(en) -> float:
    if getattr(en, "catalog", None):
        return CATALOG_ENGINES[en.catalog]["unit_usd"]
    spec = en.spec or {}
    k = ENGINE_K_USD.get(en.cycle, 5e6)
    f = max(en.thrust_vac, 1e3) / 1e6
    pc = max(en.pc / 1e5, 1.0) / 100.0
    cost = ENGINE_FIXED_USD + k * f ** 0.6 * pc ** 0.25 * ENGINE_PROP_FACTOR.get(en.fuel, 1.0)
    cost *= ENGINE_COOLING_FACTOR.get(spec.get("cooling", "regen"), 1.0)
    if spec.get("injector") == "pintle":
        cost *= 1.1
    return cost


def learning_total(u1: float, n: int) -> float:
    return u1 * sum(i ** _B for i in range(1, int(n) + 1))


def _tps_cost(material, area, areal):
    return TPS_USD_PER_M2.get(material, 200e3) * area * max(areal / 10.0, 0.05) ** 0.5


def cost_report(vehicle, design=None) -> dict:
    """The bill of materials: every priced item, totals per element, the hardware total and the budgets."""
    items = []

    def add(element, item, usd, qty=None, unit=None):
        if usd > 0:
            items.append({"element": element, "item": item, "usd": round(float(usd), 0),
                          **({"qty": qty} if qty is not None else {}), **({"unit_usd": round(float(unit), 0)} if unit else {})})

    # Engines: count every unit of each design across the vehicle; learning is per design.
    counts = {}
    for e in vehicle.elements:
        for en, n in e.engines:
            counts[en.name] = counts.get(en.name, 0) + n
    eng_total = {}
    for name, n in counts.items():
        en = vehicle.engines[name]
        u1 = engine_first_unit_usd(en)
        tot = u1 * n if getattr(en, "catalog", None) else learning_total(u1, n)
        eng_total[name] = (tot, n, u1)
    placed = {}
    for e in vehicle.elements:
        bd = e.mass_breakdown
        for en, n in e.engines:
            tot, n_all, u1 = eng_total[en.name]
            share = tot * n / n_all
            placed[en.name] = placed.get(en.name, 0) + n
            add(e.id, f"engine {en.name}" + (f" ({en.catalog}, flight proven)" if getattr(en, "catalog", None) else ""),
                share, n, u1)
            add(e.id, "engine installation", ENGINE_INSTALL_USD * n, n, ENGINE_INSTALL_USD)
        if e.kind == "stage":
            if e.tanks:
                tank_kg = sum(v for k, v in bd.items() if k.endswith(("_tank", "_tank_head", "_tank_stiffeners"))) + bd.get("common_bulkhead", 0)
                add(e.id, f"tanks ({e.material}, {tank_kg:.0f} kg)", tank_kg * MATERIAL_USD_PER_KG.get(e.material, 1000.0))
                add(e.id, "pressurant system", (bd.get("helium", 0) + bd.get("copv", 0)) * PRESSURANT_SYSTEM_USD_PER_KG)
                add(e.id, "secondary structure and stage systems",
                    (bd.get("secondary_structure_and_systems", 0) + bd.get("intertank", 0)) * STAGE_SYSTEMS_USD_PER_KG)
                foam = sum(tk.area for r, tk in e.tanks.items() if f"{r}_tank_foam" in bd or f"{r}_tank_insulation" in bd)
                add(e.id, "spray-on foam", foam * FOAM_USD_PER_M2)
                if e.mli_layers:
                    mli = sum(tk.area for tk in e.tanks.values())
                    add(e.id, f"MLI ({e.mli_layers} layers)", mli * (MLI_USD_PER_M2 + MLI_USD_PER_M2_PER_LAYER * e.mli_layers))
            add(e.id, "thrust structure", bd.get("thrust_structure", 0) * THRUST_STRUCTURE_USD_PER_KG)
            add(e.id, "interstage", (bd.get("interstage", 0) + bd.get("interstage_above", 0)) * INTERSTAGE_USD_PER_KG)
            add(e.id, "landing legs", bd.get("landing_legs", 0) * LEGS_USD_PER_KG)
            if bd.get("landing_legs"):
                add(e.id, "landing system (radar, telecom, thermal control, assembly and test)", LANDING_SYSTEM_USD)
            add(e.id, "stage integration", STAGE_INTEGRATION_USD)
        elif e.kind == "fairing":
            area = math.pi * e.diameter * e.length + math.pi * (e.diameter / 2) ** 2
            add(e.id, "fairing", area * FAIRING_USD_PER_M2)
        elif e.kind in ("heatshield", "capsule"):
            fore = math.pi * (e.diameter / 2) ** 2 / math.sin(math.radians(e.half_angle))
            add(e.id, f"{e.tps} heat shield", _tps_cost(e.tps, fore, e.tps_areal))
            add(e.id, "aeroshell structure", bd.get("aeroshell_structure", 0) * AEROSHELL_STRUCTURE_USD_PER_KG)
            if e.kind == "capsule":
                add(e.id, "capsule structure and sample containment", (bd.get("structure_and_containment", 0)
                    + bd.get("containment_of_more_containers", 0)) * CAPSULE_STRUCTURE_USD_PER_KG)
                add(e.id, "capsule backshell TPS", _tps_cost(e.tps, 0.5 * fore * 0.3, e.tps_areal))
                add(e.id, "sample container, sealing and recovery aids", CAPSULE_FIXED_USD)
                if getattr(e, "sample_containers", 1) > 1:   # each more container: its own container and sealing
                    add(e.id, f"{e.sample_containers - 1} more sample containers, each sealed",
                        (e.sample_containers - 1) * CAPSULE_FIXED_USD)
        elif e.kind == "backshell":
            area = e.mass_breakdown.get("backshell_structure", 0) / 5.0
            add(e.id, f"{e.tps} backshell", _tps_cost(e.tps, area, e.tps_areal) * 0.5)
            add(e.id, "backshell structure", bd.get("backshell_structure", 0) * AEROSHELL_STRUCTURE_USD_PER_KG)
        elif e.kind == "parachute":
            add(e.id, f"{e.chute_type} parachute and mortar",
                PARACHUTE_FIXED_USD + PARACHUTE_USD_PER_M2_DIAMETER * e.chute_diameter ** 2 * (e.chute_design_q / 500.0) ** 0.5)
        # systems that can sit on any element
        if "avionics" in bd:
            add(e.id, "avionics", AVIONICS_USD)
        if e.rcs_prop or e.rcs_thrust:
            add(e.id, "RCS", RCS_FIXED_USD + RCS_USD_PER_N * e.rcs_thrust + RCS_USD_PER_KG_PROP * e.rcs_prop)
            add(e.id, "hydrazine", e.rcs_prop * HYDRAZINE_USD_PER_KG)
        add(e.id, "solar arrays", e.solar_m2 * SOLAR_USD_PER_M2)
        add(e.id, "batteries", e.battery_Wh / 1000.0 * BATTERY_USD_PER_KWH)
        add(e.id, "RTGs", e.rtg_units * RTG_USD, e.rtg_units, RTG_USD)
        add(e.id, "fission power units", e.fission_units * FISSION_USD, e.fission_units, FISSION_USD)
        add(e.id, "cryocoolers", e.cryocooler_kW * CRYOCOOLER_USD_PER_KW)
        add(e.id, "ISRU oxygen plant", e.isru_kW * ISRU_USD_PER_KW)
        add(e.id, "extra equipment", bd.get("extra", 0) * EXTRA_MASS_USD_PER_KG)
        add(e.id, "vibration hardening (qualification above the Falcon 9 levels)", bd.get("vibration_hardening", 0) * EXTRA_MASS_USD_PER_KG)
        add(e.id, "pogo accumulators", bd.get("pogo_accumulators", 0) * EXTRA_MASS_USD_PER_KG)
        add(e.id, "surface insulation", bd.get("surface_insulation", 0) * INSULATION_USD_PER_KG)
        if e.trn:
            add(e.id, "terrain-relative navigation (descent camera and vision computer)", D.TRN["usd"])
        if e.is_stage:
            for role, prop in (("fuel", e.fuel), ("ox", e.ox)):
                kg = e.loaded.get(role, 0.0)
                if prop and kg:
                    add(e.id, f"{prop} propellant", kg * PROPELLANT_USD_PER_KG.get(prop, 1.0))
    if any(e.rtg_units for e in vehicle.elements):
        add("launch", "RTG fueling campaign and nuclear launch approval", RTG_FIXED_USD)
    add("launch", "launch operations (pad, range, recovery teams)", LAUNCH_OPERATIONS_USD)

    by_element = {}
    for it in items:
        by_element[it["element"]] = by_element.get(it["element"], 0.0) + it["usd"]
    total = sum(it["usd"] for it in items)
    return {"hardware_usd": round(total, 0), "hardware_budget_usd": HARDWARE_BUDGET_USD,
            "over_budget": total > HARDWARE_BUDGET_USD, "rnd_budget_usd": RND_BUDGET_USD,
            "by_element_usd": {k: round(v, 0) for k, v in by_element.items()},
            "engines": {name: {"units": n, "first_unit_usd": round(u1, 0), "total_usd": round(tot, 0)}
                        for name, (tot, n, u1) in eng_total.items()},
            "items": items}
