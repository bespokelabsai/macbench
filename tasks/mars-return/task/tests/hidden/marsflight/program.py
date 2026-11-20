"""Program office: R&D tests, the hidden as-built truth of the hardware, and reliability.

What the team designs is a model. What gets built differs from the model in ways only tests reveal:
- engines: real Isp and thrust a little off the model, and possibly a flaw (combustion instability,
  or a real cooling margin below 1) that destroys the engine the first time it runs;
- tanks: weld efficiency (burst strength) and shell buckling strength;
- aerodynamics: the drag of the launch stack, and the drag and lift of each aeroshell and capsule;
- thermal protection: the heat load and heat flux each material really survives;
- parachutes: real drag; ISRU: real oxygen production.

The truth is drawn from a secret held on the hub, keyed by the design of each part, so it is fixed
for a design during the whole run and the same in every verifier flight. Change the design and you
get a new part with a new, unknown truth. Tests measure the truth. Reliability (the chance that an
engine start, a second of burning, a stage's first ignition or a parachute deployment fails) starts
high for new hardware and falls with testing, following the Duane reliability growth model. The real
chances of a new engine, stage or parachute design are the model's times a hidden reliability factor,
which tests reveal through the failures and anomalies they log. Engines from the flight-proven catalog
have no hidden deviations and flight-proven reliability.

Command line (used by the board plugin, which runs on the hub without numpy):
    python -m marsflight.program quote   < {"kind":..., "spec":..., "params":...}
    python -m marsflight.program run     < {"kind":..., "spec":..., "params":..., "test_id":..., "ledger": [...], "secret_file": ...}
    python -m marsflight.program catalog
    python -m marsflight.program notices     < {"secret_file": ...}             (MARSFLIGHT_NOTICES)
    python -m marsflight.program rules       < {"events": [...]}               
    python -m marsflight.program contractors < {"design":..., "report":..., "tests": [...], "events": [...]} 

Notices live in program/notices.py and the contractor split in program/contractors.py. Both are off
unless their switch is set (MARSFLIGHT_NOTICES=1, MARSFLIGHT_CONTRACTORS=1).
"""
from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import math
import os
import sys

import numpy as np

from . import data as D

# program.py is still a module, but the layout (docs/LAYOUT.md) moves the program office into program/. This lets
# the new files there import as marsflight.program.notices and marsflight.program.contractors until the move.
__path__ = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "program")]


def _sub(name: str):
    """marsflight.program.<name>, also when this file runs as __main__."""
    base = __spec__.name if __spec__ is not None and __spec__.name != "__main__" else "marsflight.program"
    return importlib.import_module(f"{base}.{name}")

# ---------------------------------------------------------------------------- facilities and tests
FACILITIES = {
    "engine_stand_1": "engine_stand", "engine_stand_2": "engine_stand",
    "stage_stand": "stage_stand", "structures_lab": "structures_lab", "wind_tunnel": "wind_tunnel",
    "arcjet": "arcjet", "drop_range": "drop_range", "thermal_vacuum": "thermal_vacuum",
    "vibration_lab": "vibration_lab",
}
TEST_KINDS = {
    "engine_hotfire": {
        "facility": "engine_stand",
        "target": "an engine spec (the same object as in design['engines'], or {'catalog': name})",
        "params": {"starts": "1 to 12 starts", "burn_s": "seconds per start, 1 to 900"},
        "reveals": "real vacuum Isp and thrust, the real cooling margin (once it fires over 20 s in all), any combustion "
                   "instability, and random failures and anomalies at the design's real reliability, which "
                   "estimate its hidden reliability factor. Every start and second raises the engine "
                   "design's reliability.",
    },
    "stage_static_fire": {
        "facility": "stage_stand",
        "target": "a stage element plus the engines it uses: {'stage': {...element...}, 'engines': {...}}",
        "params": {"burn_s": "seconds, 5 to 300"},
        "reveals": "integration problems: lowers the stage's first-ignition failure chance from 2% to 0.4% "
                   "after one test and 0.2% after two (model values, times the stage's hidden reliability "
                   "factor), and logs failures and anomalies that estimate that factor. Also counts as a "
                   "start and burn of every engine on it.",
    },
    "structural_qualification": {
        "facility": "structures_lab",
        "target": "a stage element: {'stage': {...element...}, 'engines': {...}} (engines may be {})",
        "params": {},
        "reveals": "proof test at 1.25 x tank pressure and an axial compression test of a tank article: the "
                   "real weld efficiency and buckling strength. A pass qualifies this tank design (its walls and its "
                   "capacity, to 1000 kg) for the tested factor of safety 1.25 instead of the untested 1.6.",
    },
    "wind_tunnel": {
        "facility": "wind_tunnel",
        "target": "{'stack': [...elements, bottom first...]} for the launch stack, or {'shell': {...heatshield or capsule element...}}",
        "params": {},
        "reveals": "the real drag factor of the launch stack, or the real drag and lift-to-drag factors of an "
                   "aeroshell or capsule, to about 1%.",
    },
    "arcjet": {
        "facility": "arcjet",
        "target": "{'tps': material}",
        "params": {"heat_flux_W_m2": "test condition, 1e5 to 4e8", "duration_s": "5 to 600"},
        "reveals": "whether the material survives that heat flux, and its real heat-load capacity factor "
                   "against the design correlation, to about 3%.",
    },
    "parachute_drop": {
        "facility": "drop_range",
        "target": "a parachute element",
        "params": {},
        "reveals": "one deployment at the canopy's design conditions: success or failure, anomalies, and the "
                   "real drag factor. Each drop lowers the deployment failure chance (x0.65, floor 0.3%, model "
                   "values, times the parachute's hidden reliability factor). A dgb drop is a supersonic "
                   "rocket-boosted test and costs more.",
    },
    "vibration_test": {
        "facility": "vibration_lab",
        "target": "a payload element: one a fairing covers, or one that is not a stage (its qual_acoustic_dB and qual_shock_g say what it is built to)",
        "params": {"acoustic_dB": "overall sound pressure level of a 2 minute reverberant chamber test, 120 to 165",
                   "shock_g": "peak shock response spectrum of a pyroshock test, 100 to 30000"},
        "reveals": "whether a unit built to the element's qualification levels survives that acoustic level and that "
                   "shock. The levels it really survives lie above its qualification levels by a hidden margin of "
                   "0 to 8 dB. Use the environment the vehicle report predicts (liftoff_acoustics, separation shock).",
    },
    "isru_demo": {
        "facility": "thermal_vacuum",
        "target": "{'isru': true}",
        "params": {},
        "reveals": "the real oxygen production of the ISRU plant design against the 0.75 kg per sol per kW model.",
    },
}

# Tests of the wired models (core/wiring.py), offered only while their model is switched on.
_FAILURE_REVEALS = {
    "avionics_burn_in": "infant-mortality failures of the element's avionics units; failed units are replaced, so the "
                        "flight units start older, at a lower failure rate",
    "phasing_test": "a reversed rate sensor or swapped actuator cable on the stage (Proton-M 2013, Vega VV17)",
    "trajectory_requalification": "heritage inertial software whose ranges this vehicle's ascent exceeds (Ariane 5 501)",
    "part_lot_screening": "a strut or rod end below its certified strength (CRS-7)",
    "centrifuge_test": "an inverted deceleration switch in the capsule's sequencer (Genesis)",
    "edl_hil_test": "an IMU saturation handling error at parachute inflation (Schiaparelli)",
    "nav_end_to_end": "a units error between ground software and the flight's small-force model (Mars Climate Orbiter)",
    "radiation_test": "memory parts that heavy ions upset (Phobos-Grunt)",
    "touchdown_sensor_test": "the touchdown sensors' spurious signal at leg deployment (Mars Polar Lander)",
}


def _wired_kinds() -> dict:
    """{kind: (module, public description)} of the wired models' tests that are switched on."""
    from .core.wiring import switch
    out = {}
    if switch("FAILURES") or switch("FLOWN_FAILURES"):
        from .models.failures import program_tests as FT
        for k, v in FT.TEST_KINDS.items():
            if (k == "avionics_burn_in" and switch("FAILURES")) or (k != "avionics_burn_in" and switch("FLOWN_FAILURES")):
                out[k] = ("failures", {"facility": v["facility"], "target": v["target"], "params": v["params"],
                                       "reveals": _FAILURE_REVEALS[k]})
    if switch("MASS_GROWTH"):
        from .models.mass_growth.weighing import TEST_KIND
        for k, v in TEST_KIND.items():
            out[k] = ("weighing", v)
    if switch("PROPULSION_FAILURES"):
        # F3 to F7 (models/failures/propulsion_gaps.py): one test per latent propulsion flaw
        from .models.failures import propulsion_gaps as PG
        for k, v in PG.TEST_KINDS.items():
            out[k] = ("propulsion_gaps", {"facility": v["facility"], "target": "a stage with engines, e.g. 'S2'",
                                          "params": {}, "reveals": v["lesson"]})
    return out


# One name per test kind; the old names still work (manual the office offered mass_properties while
# program.py registered weighing).
KIND_ALIASES = {"mass_properties": "weighing"}


def canonical_kind(kind):
    return KIND_ALIASES.get(kind, kind)


def failure_record(ledger) -> dict:
    """The failure models' record of done tests: program_tests.failure_record plus the propulsion flaw tests,
    counted by kind in the same failure_tests table that propulsion_gaps.p_flaw reads."""
    from .models.failures import program_tests as FT
    from .models.failures import propulsion_gaps as PG
    rec = FT.failure_record(ledger)
    for t in ledger or []:
        if t.get("status") == "done" and t.get("kind") in PG.TEST_KINDS:
            rec["failure_tests"][t["kind"]] = rec["failure_tests"].get(t["kind"], 0) + 1
    return rec


def _failure_kinds():
    from .models.failures import program_tests as FT
    from .models.failures import propulsion_gaps as PG
    return set(FT.TEST_KINDS) | set(PG.TEST_KINDS)


def test_kinds() -> dict:
    """Every test the program office offers now: TEST_KINDS plus those of the switched-on wired models."""
    kinds = dict(TEST_KINDS)
    kinds.update({k: v for k, (_, v) in _wired_kinds().items()})
    return kinds


def facilities() -> dict:
    f = dict(FACILITIES)
    if any(v["facility"] == "avionics_lab" for v in test_kinds().values()):
        f["avionics_lab"] = "avionics_lab"
    return f


def _wired_quote(kind, spec, params):
    mod, _ = _wired_kinds()[kind]
    if mod == "weighing":
        from .models.mass_growth.weighing import quote_weighing
        return quote_weighing(spec)
    if mod == "propulsion_gaps":
        from .models.failures import propulsion_gaps as PG
        return PG.quote(kind, spec, params)
    from .models.failures import program_tests as FT
    return FT.quote(kind, spec, params)


def _wired_run(kind, spec, params, test_id, ledger, secret):
    mod, _ = _wired_kinds()[kind]
    if mod == "weighing":
        from .models.mass_growth.weighing import run_weighing
        return run_weighing(spec, test_id, secret)
    if mod == "propulsion_gaps":
        from .models.failures import propulsion_gaps as PG
        return PG.run_test(kind, spec, params, test_id, ledger, secret,
                           record=failure_record([t for t in (ledger or []) if t.get("kind") in _failure_kinds()]))
    from .models.failures import program_tests as FT
    return FT.run_test(kind, spec, params, test_id, ledger, secret)


# ---------------------------------------------------------------------------- reliability model
P0_START = {"pressure_fed": 0.004, "electric_pump": 0.008, "gas_generator": 0.010, "expander": 0.010,
            "staged_ox": 0.018, "staged_fuel": 0.018, "full_flow": 0.020}
LAMBDA0 = {"pressure_fed": 0.8e-5, "electric_pump": 1.5e-5, "gas_generator": 2.0e-5, "expander": 2.0e-5,
           "staged_ox": 3.5e-5, "staged_fuel": 3.5e-5, "full_flow": 4.0e-5}
DUANE_ALPHA = 0.7
START_SCALE = 3.0       # test starts at which growth begins
SECOND_SCALE = 100.0    # test seconds at which growth begins
CATALOG_P_START = 0.0015
CATALOG_LAMBDA = 3.0e-6
STAGE_IGNITION_P = [0.02, 0.004, 0.002]          # by number of stage static fires: 0, 1, 2 or more
CHUTE_P0 = {"dgb": 0.08, "ringsail": 0.04}
CHUTE_GROWTH, CHUTE_FLOOR = 0.65, 0.003
SEPARATION_P = 0.002
# RCS thrusters are flight-proven (no hidden factor) but not free: failures per kN-second of RCS impulse. Model
# value: one 0.44 kN R-4D class thruster failing at the flight-proven engine rate (CATALOG_LAMBDA per second)
# gives 7e-6 per kN-second, and RCS takes about a quarter of that for its redundant thrusters. A 150 kg RCS
# burned dry is 330 kN-s (0.07% failure chance); the same impulse costs a 30 kN pressure-fed engine about 0.01%.
RCS_LAMBDA_PER_KN_S = 2.0e-6
# Hidden reliability. The chances above are the model. A new engine, stage or parachute design really fails
# at the model's chance times a hidden factor, drawn per design from the hub secret: gamma with shape
# FACTOR_SHAPE and mean 1. Tests run at the real chances and also log anomalies, off-nominal events that show
# the design's margins: ANOMALY_RATIO times the design's untested failure chance, times its factor, per start,
# second, stage ignition or deployment. Growth fixes failure modes, so failures get rarer with testing, but the
# anomaly rate stays, so every test adds evidence. Failures plus anomalies against what a factor of 1 would
# give estimate the factor (factor_estimate).
FACTOR_SHAPE = 2.0
ANOMALY_RATIO = 20.0
# Engine-out. Not every engine failure destroys its stage: many end in a shutdown that the rest of the stage
# can survive. Huang, Fint and Kuck, "Key Reliability Drivers of Liquid Propulsion Engines and A Reliability
# Model for Sensitivity Analysis" (AIAA 2005, NTRS 20050207429): "Historical Apollo and Space Shuttle liquid
# engine data have indicated Cf falls into the range of 20% to 40%", Cf being the share of engine failures that
# are catastrophic. CATASTROPHIC_FRACTION of the failures destroy the stage; the rest shut one engine down. With
# one engine of a kind, the stage loses that thrust and the mission. With several, it flies on, and the verifier
# flies that case (scoring.engine_out_cases) to see whether the mission survives it.
CATASTROPHIC_FRACTION = 0.3
# Failure checks beyond engine-out (grading.failure_check_cases), the same way: the share of each failure that a
# design could survive, which the verifier then flies to see whether this design does. No source gives either share
# directly; these are documented proxies (results/failure-checks/README.md).
# CHUTE_BACKUP_FRACTION: of parachute failures, the share a separate backup parachute can recover (the primary fails
# to deploy, extract or open). The rest are common-cause failures that defeat the backup too (fouling it, or a shared
# trigger). NASA's common-cause modeling for launch vehicles caps that share at 10%, 20% or 30% (Hark, Ring, Novack
# and Britton, NTRS 20160007073; the NRC's generic 5% to 10%, up to 33% in some data): 20% is its middle. Flown
# single-canopy failures agree: Apollo 15, the Starliner pad abort, Crew-2 and CRS-24 were survived on the other
# canopies, and Soyuz 1, whose reserve fouled, was not.
# IGNITION_RETRY_FRACTION: of stage first-ignition failures, the share that are no-lights a retry with a start left
# recovers. NASA's 1966 to 1987 launch record put propulsion failures at 10 benign to 1 catastrophic (Gunn, NTRS
# 19910018896), and every Space Shuttle pad abort after ignition was benign; but a retry fails when the cause
# persists (Centaur AC-71, 1992: the software ran a second start and it failed too; NTRS 20170009844). So 0.6,
# below the benign share and below engine-out's 70%.
CHUTE_BACKUP_FRACTION = 0.8
IGNITION_RETRY_FRACTION = 0.6


def _pc_factor(spec_cycle, pc_bar):
    return 1.0 + 0.5 * pc_bar / max(D.CYCLES.get(spec_cycle, {}).get("pc_max", 300.0), 1.0)


def engine_reliability(spec: dict, rec: dict | None, factor: float = 1.0) -> dict:
    """p_start: failure chance per engine start; lambda: failures per engine-second of burning.
    factor: the design's reliability factor (1 gives the model)."""
    rec = rec or {}
    if spec.get("catalog"):
        return {"p_start": CATALOG_P_START, "lambda_per_s": CATALOG_LAMBDA, "flight_proven": True}
    cyc = spec.get("cycle", "gas_generator")
    f = _pc_factor(cyc, float(spec.get("pc_bar", 100.0)))
    n, T = rec.get("starts", 0), rec.get("seconds", 0.0)
    return {"p_start": factor * P0_START.get(cyc, 0.015) * f / (1 + n / START_SCALE) ** DUANE_ALPHA,
            "lambda_per_s": factor * LAMBDA0.get(cyc, 3e-5) * f / (1 + T / SECOND_SCALE) ** DUANE_ALPHA,
            "test_starts": n, "test_seconds": round(T, 1), "factor": round(factor, 6)}


def chute_reliability(chute_type: str, n_drops: int) -> float:
    return max(CHUTE_FLOOR, CHUTE_P0.get(chute_type, 0.06) * CHUTE_GROWTH ** n_drops)


# ---------------------------------------------------------------------------- keys
def _q(v, step):
    try:
        return round(round(float(v) / step) * step, 6)
    except (TypeError, ValueError):
        return v


def _hash(prefix, obj):
    s = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return f"{prefix}:{hashlib.sha1(s.encode()).hexdigest()[:14]}"


def engine_key(spec: dict) -> str:
    if spec.get("catalog"):
        return "catalog:" + str(spec["catalog"])
    fields = {"fuel": None, "oxidizer": None, "cycle": None, "pc_bar": 1.0, "mixture_ratio": 0.01,
              "expansion_ratio": 0.5, "throat_diameter_m": 0.001, "cooling": None, "film_cooling_fraction": 0.005,
              "injector": None, "igniter": None, "igniter_units": None}
    d = {k: (_q(spec.get(k), st) if st else spec.get(k)) for k, st in fields.items()}
    d.setdefault("cooling", "regen")
    return _hash("engine", d)


def _tank_walls(stage: dict) -> str:
    """The tank's walls at any capacity: what its hidden weld and buckling truth and the stage key depend on."""
    w = stage.get("wall_thickness_mm") or {}
    d = {"material": stage.get("material", "Al2219"), "d": _q(stage.get("diameter_m"), 0.01),
         "p": _q(stage.get("tank_pressure_bar", 3.0), 0.1), "fuel": stage.get("fuel"),
         "ox": stage.get("oxidizer"), "wf": _q(w.get("fuel"), 0.01), "wo": _q(w.get("ox"), 0.01)}
    for k in ("common_bulkhead", "balloon_tanks"):   # only when set, so other tank designs keep their keys
        if stage.get(k):
            d[k] = True
    if isinstance(stage.get("stiffeners"), dict):
        d["stiffeners"] = {k: _q(v, 0.1) for k, v in sorted(stage["stiffeners"].items())}
    return _hash("tank", d)


def _tank_capacity(stage: dict):
    return _q(stage.get("tank_capacity_kg", stage.get("propellant_kg", 0)), 1000.0)


def tank_key(stage: dict) -> str:
    """The tank a structural qualification covers: its walls and its capacity, so a short article does not
    qualify a long tank."""
    return _hash("tank", {"walls": _tank_walls(stage), "cap": _tank_capacity(stage)})


def stage_key(stage: dict, engines: dict) -> str:
    engs = sorted((engine_key((engines or {}).get(it.get("engine"), {}) or {}), int(it.get("count", 1)))
                  for it in stage.get("engines", []) or [] if isinstance(it, dict))
    return _hash("stage", {"tank": _tank_walls(stage), "engines": engs,
                           "prop": _tank_capacity(stage)})


def chute_key(el: dict) -> str:
    return _hash("chute", {"type": el.get("type", "dgb"), "d": _q(el.get("diameter_m"), 0.1),
                           "q": _q(el.get("design_q_Pa"), 10.0)})


def shell_key(el: dict) -> str:
    return _hash("shell", {"kind": el.get("kind"), "d": _q(el.get("diameter_m"), 0.1),
                           "half": _q(el.get("half_angle_deg", 70), 1.0), "rn": _q(el.get("nose_radius_m", 0), 0.05),
                           "ld": _q(el.get("ld_trim", 0), 0.01)})


def stack_key(elements: list) -> str:
    out = []
    for el in elements:
        out.append((el.get("kind"), _q(el.get("diameter_m"), 0.1), _q(el.get("length_m"), 0.5)))
        if el.get("kind") == "fairing":
            break
    return _hash("stack", out)


def vibro_key(el: dict) -> str:
    """Hardware built to the same vibration qualification levels shares its real margins above them. A level the
    design leaves unset keys as unset: the program office then picks it from the vehicle's environment."""
    qa, qs = el.get("qual_acoustic_dB"), el.get("qual_shock_g")
    return _hash("vibro", {"kind": el.get("kind"), "d": _q(el.get("diameter_m"), 0.1),
                           "qa": None if qa is None else _q(qa, 0.1), "qs": None if qs is None else _q(qs, 1.0)})


def tps_key(material: str) -> str:
    return f"tps:{material}"


ISRU_KEY = "isru:plant"


def component_keys(design: dict) -> dict:
    """Every testable part of a design, by the name or id it has in the design."""
    engines = design.get("engines") or {}
    els = [e for e in design.get("elements") or [] if isinstance(e, dict)]
    out = {"engines": {n: engine_key(s or {}) for n, s in engines.items()}, "tanks": {}, "stages": {},
           "chutes": {}, "shells": {}, "tps": {}, "stack": stack_key(els), "isru": None, "vibro": {}}
    from .vibro import payload_flags
    for e, pay in zip(els, payload_flags([x.get("kind") for x in els])):
        if pay:
            out["vibro"][str(e.get("id"))] = vibro_key(e)
        k, eid = e.get("kind"), str(e.get("id"))
        if k == "stage":
            if e.get("propellant_kg") or e.get("tank_capacity_kg"):
                out["tanks"][eid] = tank_key(e)
            if e.get("engines"):
                out["stages"][eid] = stage_key(e, engines)
        elif k == "parachute":
            out["chutes"][eid] = chute_key(e)
        elif k in ("heatshield", "capsule"):
            out["shells"][eid] = shell_key(e)
        if e.get("tps"):
            out["tps"][str(e.get("tps"))] = tps_key(str(e.get("tps")))
        if e.get("isru_kW"):
            out["isru"] = ISRU_KEY
    return out


# ---------------------------------------------------------------------------- the hidden truth
def _rng(secret: str, key: str):
    h = hmac.new(secret.encode(), key.encode(), hashlib.sha256).digest()
    return np.random.default_rng(int.from_bytes(h[:8], "little"))


def _clip(x, lo, hi):
    return float(min(max(x, lo), hi))


WELD_RANGE = {"SS301": (0.85, 0.98), "Al2219": (0.75, 0.92), "Al2195": (0.70, 0.90), "Ti6Al4V": (0.85, 0.95),
              "CFRP": (0.80, 0.95)}


def engine_truth(secret: str, spec: dict, model=None) -> dict:
    """model: the Engine built from spec (for the cooling margin); built here when not given."""
    if spec.get("catalog"):
        return {"isp": 1.0, "thrust": 1.0, "flaw": None, "cooling_margin": None}
    r = _rng(secret, engine_key(spec))
    isp = _clip(r.normal(0.994, 0.006), 0.97, 1.01)
    thrust = _clip(r.normal(1.0, 0.01), 0.97, 1.03)
    cyc = spec.get("cycle", "gas_generator")
    pc_rel = float(spec.get("pc_bar", 100.0)) / max(D.CYCLES.get(cyc, {}).get("pc_max", 300.0), 1.0)
    p_unstable = 0.04 + 0.08 * pc_rel ** 2 + (0.06 if float(spec.get("throat_diameter_m", 0.2)) > 0.3 else 0.0)
    if spec.get("injector") == "pintle":
        p_unstable = max(0.01, p_unstable - 0.04)
    u_stab, f_cool = r.uniform(), r.normal(0.93, 0.07)
    flaw = "unstable" if u_stab < p_unstable else None
    margin = None
    if spec.get("cooling", "regen") == "regen":
        if model is None:
            from .engine import build_engine
            model = build_engine("x", spec)
        cm = getattr(model, "cooling_margin", float("inf"))
        if cm is not None and math.isfinite(cm):
            margin = cm * f_cool
            if margin < 1.0 and flaw is None:
                flaw = "cooling"
    return {"isp": isp, "thrust": thrust, "flaw": flaw, "cooling_margin": margin}


def tank_truth(secret: str, stage: dict) -> dict:
    r = _rng(secret, _tank_walls(stage))
    lo, hi = WELD_RANGE.get(stage.get("material", "Al2219"), (0.75, 0.92))
    return {"weld": float(r.uniform(lo, hi)), "knockdown": _clip(r.normal(1.05, 0.08), 0.85, 1.25)}


def shell_truth(secret: str, el: dict) -> dict:
    r = _rng(secret, shell_key(el))
    return {"cd": _clip(r.normal(1.0, 0.04), 0.9, 1.1), "ld": _clip(r.normal(1.0, 0.08), 0.8, 1.2)}


def stack_truth(secret: str, elements: list) -> dict:
    r = _rng(secret, stack_key(elements))
    return {"cd": _clip(r.normal(1.0, 0.06), 0.85, 1.2)}


def tps_truth(secret: str, material: str) -> dict:
    r = _rng(secret, tps_key(material))
    return {"capacity": _clip(r.normal(0.93, 0.08), 0.75, 1.1), "q_max": _clip(r.normal(0.95, 0.08), 0.75, 1.15)}


def chute_truth(secret: str, el: dict) -> dict:
    r = _rng(secret, chute_key(el))
    return {"cd": _clip(r.normal(0.97, 0.04), 0.88, 1.06)}


def isru_truth(secret: str) -> float:
    return _clip(_rng(secret, ISRU_KEY).normal(0.85, 0.12), 0.5, 1.1)


def vibro_truth(secret: str, el: dict) -> dict:
    """How far above its qualification levels an element really survives, in dB. A game parameter, not a
    physical number: units pass their qualification test at the qualification level, and their strength above it
    is spread between 0 and 8 dB around 3 dB."""
    r = _rng(secret, vibro_key(el))
    return {"acoustic_margin_dB": _clip(r.normal(3.0, 2.0), 0.0, 8.0), "shock_margin_dB": _clip(r.normal(3.0, 2.0), 0.0, 8.0)}


def reliability_factor(secret: str, key: str) -> float:
    """The hidden reliability factor of one engine, stage or parachute design: its real failure chances are
    the model's times this. A stream of its own ("reliability:" + key), so the other truths of the part stay
    as they were. Flight-proven catalog engines have none."""
    if key.startswith("catalog:"):
        return 1.0
    return float(_rng(secret, "reliability:" + key).gamma(FACTOR_SHAPE, 1.0 / FACTOR_SHAPE))


def reliability_keys(design: dict) -> list:
    """The keys of every part of this design whose reliability is hidden: engines it develops, stages with
    engines, parachutes."""
    keys = component_keys(design)
    out = [k for k in keys["engines"].values() if not k.startswith("catalog:")]
    out += list(keys["stages"].values()) + list(keys["chutes"].values())
    return sorted(set(out))


def reliability_factors(secret: str, design: dict) -> dict:
    """{part key: hidden reliability factor} for this design. The verifier flies these."""
    return {k: reliability_factor(secret, k) for k in reliability_keys(design)}


def factor_estimate(events: float, expected: float) -> float:
    """Best estimate of a reliability factor after tests that logged `events` (failures plus anomalies)
    where a factor of 1 would expect `expected`: the mean of the gamma posterior, shape
    FACTOR_SHAPE + events and rate FACTOR_SHAPE + expected."""
    return (FACTOR_SHAPE + events) / (FACTOR_SHAPE + expected)


def estimated_factors(design: dict, record: dict) -> dict:
    """{part key: estimated reliability factor} from the evidence of saved test results (record_from_ledger).
    Parts with no evidence get 1, the prior mean. Local runs fly these."""
    ev = (record or {}).get("evidence") or {}
    return {k: factor_estimate(ev[k]["events"], ev[k]["expected"]) if k in ev else 1.0 for k in reliability_keys(design)}


# ---------------------------------------------------------------------------- the ledger
def record_from_ledger(ledger: list) -> dict:
    """What the finished tests established, by part key."""
    rec = {"engines": {}, "stage_fires": {}, "tanks": {}, "chutes": {}, "shells": {}, "stacks": {}, "tps": {}, "isru": None,
           "evidence": {}}
    for t in ledger or []:
        if t.get("status") != "done":
            continue
        out = t.get("outcome") or {}
        for k, v in (out.get("engine_exposure") or {}).items():
            e = rec["engines"].setdefault(k, {"starts": 0, "seconds": 0.0, "failures": 0})
            e["starts"] += v.get("starts", 0)
            e["seconds"] += v.get("seconds", 0.0)
            e["failures"] += v.get("failures", 0)
        ev = out.get("reliability_evidence")
        if isinstance(ev, dict) and ev.get("key"):
            e = rec["evidence"].setdefault(ev["key"], {"events": 0, "expected": 0.0, "tests": 0})
            e["events"] += int(ev.get("events", 0))
            e["expected"] += float(ev.get("expected_events", 0.0))
            e["tests"] += 1
        kind, key = t.get("kind"), t.get("key")
        if kind == "stage_static_fire" and out.get("integration_ok") is not None:
            rec["stage_fires"][key] = rec["stage_fires"].get(key, 0) + 1
        elif kind == "structural_qualification":
            prev = rec["tanks"].get(key, {})
            rec["tanks"][key] = {"qualified": bool(out.get("qualified")) or prev.get("qualified", False),
                                 "measured": out.get("measured")}
        elif kind == "parachute_drop":
            rec["chutes"][key] = rec["chutes"].get(key, 0) + 1
        elif kind == "wind_tunnel":
            (rec["shells"] if key.startswith("shell:") else rec["stacks"])[key] = out.get("measured")
        elif kind == "arcjet":
            rec["tps"][key] = out.get("measured")
        elif kind == "isru_demo":
            rec["isru"] = out.get("measured")
    if any(t.get("status") == "done" and t.get("kind") == "weighing" for t in ledger or []):
        from .models.mass_growth.weighing import record_weighings
        rec["weighed"] = record_weighings(ledger)                 # {element key: measured dry kg}
    if any(t.get("status") == "done" and t.get("kind") in _failure_kinds() for t in ledger or []):
        rec["failure_record"] = failure_record(ledger)           # tests against the failure modes
    return rec


# ---------------------------------------------------------------------------- what the verifier flies
def flight_truth(secret: str, design: dict, vehicle, record: dict) -> dict:
    """The truth dict Mission(truth=...) flies with, for the parts of this design."""
    keys = component_keys(design)
    engines = design.get("engines") or {}
    t = {"engines": {}, "tanks": {}, "aero": {}, "tps": {}, "chutes": {}, "isru": 1.0}
    for name, spec in engines.items():
        spec = spec or {}
        t["engines"][name] = engine_truth(secret, spec, vehicle.engines.get(name) if vehicle else None)
    els = [e for e in design.get("elements") or [] if isinstance(e, dict)]
    for e in els:
        eid, k = str(e.get("id")), e.get("kind")
        if k == "stage" and eid in keys["tanks"]:
            tt = tank_truth(secret, e)
            tt["qualified"] = bool((record.get("tanks") or {}).get(keys["tanks"][eid], {}).get("qualified"))
            t["tanks"][eid] = tt
        elif k == "parachute":
            t["chutes"][eid] = chute_truth(secret, e)
        elif k in ("heatshield", "capsule"):
            t["aero"][eid] = shell_truth(secret, e)
    t["aero"]["stack"] = stack_truth(secret, els)
    t["vibro"] = {eid: vibro_truth(secret, e) for e in els for eid in [str(e.get("id"))] if eid in keys["vibro"]}
    for mat in keys["tps"]:
        t["tps"][mat] = tps_truth(secret, mat)
    if keys["isru"]:
        t["isru"] = isru_truth(secret)
    from .core.wiring import switch
    if switch("MASS_GROWTH") and vehicle is not None:
        # hidden as-built growth of every element, plus what weighings revealed
        from .models.mass_growth.weighing import flight_growth
        t["mass_growth"] = flight_growth(secret, design, vehicle, (record or {}).get("weighed"))
    if switch("FAILURES") or switch("FLOWN_FAILURES") or switch("PROPULSION_FAILURES"):
        t["failure_record"] = (record or {}).get("failure_record") or {"failure_tests": {}, "burn_in_h": {}}
    return t


def qualified_stages(design: dict, record: dict) -> list:
    keys = component_keys(design)
    return [eid for eid, k in keys["tanks"].items() if (record.get("tanks") or {}).get(k, {}).get("qualified")]


def hazards(design: dict, record: dict, factors: dict | None = None, engine_out: dict | None = None,
            failure_checks: dict | None = None) -> dict:
    """Failure chances per exposure for every part of this design, given what was tested. factors: {part key:
    reliability factor}, the real ones (reliability_factors) in the verifier and estimates (estimated_factors)
    in local runs; a part missing from it flies the model (factor 1). engine_out: {"element/engine": share of
    the engine-out checks the mission survived} (scoring.engine_out_survival) for groups of several engines."""
    from .core.wiring import switch
    keys = component_keys(design)
    engines = design.get("engines") or {}
    f = factors or {}
    # With the in-flight failures on (MARSFLIGHT_FAILURES, on by default) a separation fails in the
    # flight itself and leaves the element attached, so the score must not charge it a second time.
    sep = 0.0 if switch("FAILURES") else SEPARATION_P
    hz = {"start": {}, "burn": {}, "rcs": {}, "stage_ignition": {}, "chute": {}, "separation": sep, "detail": {},
          "factors": {k: f.get(k, 1.0) for k in reliability_keys(design)},
          "engine_out": {g: min(max(float(v), 0.0), 1.0) for g, v in (engine_out or {}).items() if g in multi_engine_groups(design)},
          "chute_backup": {k: min(max(float(v), 0.0), 1.0) for k, v in ((failure_checks or {}).get("chute_fail") or {}).items()
                           if k in backup_chutes(design)},
          "ignition_retry": {k: min(max(float(v), 0.0), 1.0) for k, v in ((failure_checks or {}).get("no_light") or {}).items()
                             if k in retry_stages(design)}}
    # RCS firings are "rcs" exposures in kN-seconds, keyed by element id, apart from the engines' burn rates.
    for e in design.get("elements") or []:
        if isinstance(e, dict) and e.get("rcs_thrust_N"):
            hz["rcs"][str(e.get("id"))] = RCS_LAMBDA_PER_KN_S
    for name, spec in engines.items():
        k = keys["engines"][name]
        rel = engine_reliability(spec or {}, record.get("engines", {}).get(k), f.get(k, 1.0))
        hz["start"][name], hz["burn"][name] = rel["p_start"], rel["lambda_per_s"]
        hz["detail"][name] = rel
    for eid, k in keys["stages"].items():
        n = record.get("stage_fires", {}).get(k, 0)
        hz["stage_ignition"][eid] = f.get(k, 1.0) * STAGE_IGNITION_P[min(n, len(STAGE_IGNITION_P) - 1)]
    for e in design.get("elements") or []:
        if isinstance(e, dict) and e.get("kind") == "parachute":
            eid = str(e.get("id"))
            k = keys["chutes"][eid]
            hz["chute"][eid] = f.get(k, 1.0) * chute_reliability(e.get("type", "dgb"), record.get("chutes", {}).get(k, 0))
    return hz


def hazard(hz: dict, kind: str, key: str) -> float:
    """The failure chance (or rate) of one exposure. A part the tables do not know is a verifier error, not a
    free pass."""
    try:
        return hz[kind][key]
    except KeyError:
        raise ValueError(f"no {kind} hazard for {key!r}; the tables have {sorted(hz.get(kind) or {})}") from None


def engine_loss_share(hz: dict, element, engine) -> float:
    """Share of this engine group's failures that lose the mission: all of them, less the shutdowns that the
    engine-out checks showed the mission survives."""
    s = (hz.get("engine_out") or {}).get(f"{element}/{engine}", 0.0)
    return 1.0 - (1.0 - CATASTROPHIC_FRACTION) * s


def backup_chutes(design: dict) -> list:
    """Parachutes with another parachute on the same carrier (the backshell or capsule they ride on): the ones a
    backup could replace. The carrier is the nearest element below a parachute in the stack that is not one."""
    # ids as vehicle.build_vehicle gives them, E<index> for an element without one
    els = [(str(e.get("id", f"E{i}")), e) for i, e in enumerate(design.get("elements") or []) if isinstance(e, dict)]
    carrier = {}
    for i, (eid, e) in enumerate(els):
        if e.get("kind") == "parachute":
            below = next((els[k][0] for k in range(i - 1, -1, -1) if els[k][1].get("kind") != "parachute"), None)
            carrier[eid] = below
    return [c for c, k in carrier.items() if sum(1 for v in carrier.values() if v == k) >= 2]


def retry_stages(design: dict) -> list:
    """Stages whose engines can all start again (max_starts of 2 or more, as the vehicle builds them): a no-light
    at first ignition can be retried. A stage with an engine of one start (a single TEA-TEB cartridge) cannot, and
    gets no ignition-retry credit."""
    from .engine import build_engine
    from .economics import CATALOG_ENGINES, build_catalog_engine
    out, starts = [], {}
    for name, spec in (design.get("engines") or {}).items():
        spec = spec if isinstance(spec, dict) else {}
        try:
            cat = spec.get("catalog")
            en = build_catalog_engine(name, cat) if cat in CATALOG_ENGINES else build_engine(name, spec)
            starts[name] = int(en.max_starts)
        except Exception:  # noqa: BLE001  an engine that does not build cannot be retried
            starts[name] = 0
    for i, e in enumerate(design.get("elements") or []):
        if isinstance(e, dict) and e.get("kind") == "stage" and e.get("engines"):
            if min(starts.get(g.get("engine"), 0) for g in e["engines"] if isinstance(g, dict)) >= 2:
                out.append(str(e.get("id", f"E{i}")))   # as vehicle.build_vehicle names an element without an id
    return out


def chute_loss_share(hz: dict, chute) -> float:
    """Share of this parachute's failures that lose the mission: all, less those a backup recovered in the checks."""
    return 1.0 - CHUTE_BACKUP_FRACTION * (hz.get("chute_backup") or {}).get(chute, 0.0)


def ignition_loss_share(hz: dict, stage) -> float:
    """Share of this stage's first-ignition failures that lose the mission: all, less the no-lights a retry
    recovered in the checks."""
    return 1.0 - IGNITION_RETRY_FRACTION * (hz.get("ignition_retry") or {}).get(stage, 0.0)


def multi_engine_groups(design: dict) -> list:
    """The "element/engine" groups of two or more engines of one kind: the ones that can fly on with one out."""
    out = []
    for i, e in enumerate(design.get("elements") or []):
        if isinstance(e, dict):
            eid = str(e.get("id", f"E{i}"))   # as vehicle.build_vehicle names an element without an id
            for g in e.get("engines") or []:
                if isinstance(g, dict) and int(g.get("count", 1) or 1) >= 2:
                    out.append(f"{eid}/{g.get('engine')}")
    return out


def survival(exposures: list, hz: dict, t: float) -> float:
    """Probability that no hardware failed up to time t, given a flight's exposures."""
    logp = 0.0
    for ex in exposures:
        te, kind, key, amount = ex[:4]
        if te > t + 1e-6:
            continue
        if kind == "start":
            p = hazard(hz, "start", key) * (engine_loss_share(hz, ex[4], key) if len(ex) > 4 else 1.0)
            logp += amount * math.log1p(-min(p, 0.999999))
        elif kind == "burn":
            logp -= hazard(hz, "burn", key) * amount * (engine_loss_share(hz, ex[4], key) if len(ex) > 4 else 1.0)
        elif kind == "rcs":
            logp -= hazard(hz, "rcs", key) * amount
        elif kind == "stage_ignition":
            logp += math.log1p(-min(hazard(hz, "stage_ignition", key) * ignition_loss_share(hz, key), 0.999999))
        elif kind == "chute":
            logp += math.log1p(-min(hazard(hz, "chute", key) * chute_loss_share(hz, key), 0.999999))
        elif kind == "separation":
            logp += math.log1p(-hz["separation"])
        else:
            raise ValueError(f"unknown exposure kind {kind!r}")
    return math.exp(logp)


# ---------------------------------------------------------------------------- local runs
def local_truth(design: dict, results: list) -> tuple[dict, list]:
    """Truth and qualified stages for local runs, from test results the team saved
    (program/test_results.json: a list of results as returned by test_result). Untested parts fly the
    model. The verifier ignores this file and uses the real ledger."""
    keys = component_keys(design)
    by_key = {}
    for r in results or []:
        if isinstance(r, dict) and r.get("key"):
            by_key.setdefault(r["key"], []).append(r)
    t = {"engines": {}, "tanks": {}, "aero": {}, "tps": {}, "chutes": {}, "isru": 1.0}
    for name, k in keys["engines"].items():
        m = [x.get("measured") or {} for x in by_key.get(k, [])]
        isp = [x["isp_factor"] for x in m if "isp_factor" in x]
        thr = [x["thrust_factor"] for x in m if "thrust_factor" in x]
        if isp or thr:
            t["engines"][name] = {"isp": isp[-1] if isp else 1.0, "thrust": thr[-1] if thr else 1.0}
    qual = []
    for eid, k in keys["tanks"].items():
        for x in by_key.get(k, []):
            if x.get("qualified"):
                qual.append(eid)
                t["tanks"][eid] = {"qualified": True, "knockdown": (x.get("measured") or {}).get("knockdown", 1.0)}
    for eid, k in keys["shells"].items():
        for x in by_key.get(k, []):
            if x.get("measured"):
                t["aero"][eid] = {"cd": x["measured"].get("cd_factor", 1.0), "ld": x["measured"].get("ld_factor", 1.0)}
    for x in by_key.get(keys["stack"], []):
        if x.get("measured"):
            t["aero"]["stack"] = {"cd": x["measured"].get("cd_factor", 1.0)}
    for mat, k in keys["tps"].items():
        for x in by_key.get(k, []):
            if x.get("measured", {}).get("capacity_factor"):
                t["tps"][mat] = {"capacity": x["measured"]["capacity_factor"], "q_max": 1.0}
    for eid, k in keys["chutes"].items():
        for x in by_key.get(k, []):
            if x.get("measured", {}).get("cd_factor"):
                t["chutes"][eid] = {"cd": x["measured"]["cd_factor"]}
    # A vibration test passed above the qualification level shows margin up to that level.
    t["vibro"] = {}
    for eid, k in keys["vibro"].items():
        for x in by_key.get(k, []):
            for kind in ("acoustic_margin_dB", "shock_margin_dB"):
                v = (x.get("measured") or {}).get(kind)
                if v is not None:
                    t["vibro"].setdefault(eid, {})[kind] = max(t["vibro"].get(eid, {}).get(kind, 0.0), v)
    if keys["isru"]:
        for x in by_key.get(ISRU_KEY, []):
            if x.get("measured", {}).get("production_factor"):
                t["isru"] = x["measured"]["production_factor"]
    from .core.wiring import switch
    if switch("MASS_GROWTH") and any(isinstance(x, dict) and x.get("kind") == "weighing" for x in results or []):
        # weighed elements fly at their measured mass; the rest at the design mass, as the team knows it
        from .models.mass_growth.growth import local_growth
        from .vehicle import build_vehicle
        t["mass_growth"] = local_growth(design, build_vehicle(design), results)
    if switch("FAILURES") or switch("FLOWN_FAILURES") or switch("PROPULSION_FAILURES"):
        t["failure_record"] = failure_record([{"status": "done", "kind": x.get("kind"), "outcome": x}
                                              for x in results or [] if isinstance(x, dict)])
    return t, qual


# ---------------------------------------------------------------------------- quotes and test runs
def _mini_vehicle(stage: dict, engines: dict):
    from .vehicle import build_vehicle
    return build_vehicle({"engines": engines or {}, "elements": [stage]})


def _engine_model(spec):
    from .engine import build_engine
    from .economics import CATALOG_ENGINES, build_catalog_engine
    if spec.get("catalog"):
        if spec["catalog"] not in CATALOG_ENGINES:
            raise ValueError(f"no catalog engine {spec['catalog']!r}; choose from {sorted(CATALOG_ENGINES)}")
        return build_catalog_engine("test", spec["catalog"])
    en = build_engine("test", spec)
    en.catalog = None
    if en.errors:
        raise ValueError("engine design is invalid: " + "; ".join(en.errors[:3]))
    return en


def quote(kind: str, spec: dict, params: dict) -> dict:
    """Key, cost (USD), duration (real minutes) and facility for a test, without running it."""
    from . import economics as E
    params = params or {}
    kind = canonical_kind(kind)
    if kind in _wired_kinds():
        return _wired_quote(kind, spec, params)
    if kind not in TEST_KINDS:
        raise ValueError(f"unknown test kind {kind!r}; choose from {sorted(test_kinds())}")
    fac = TEST_KINDS[kind]["facility"]
    if kind == "engine_hotfire":
        eng = spec.get("engine", spec)
        en = _engine_model(eng)
        starts = int(params.get("starts", 3))
        burn = float(params.get("burn_s", 60))
        if not (1 <= starts <= 12) or not (1 <= burn <= 900):
            raise ValueError("starts must be 1 to 12 and burn_s 1 to 900")
        u1 = E.engine_first_unit_usd(en)
        prop_kg = en.mdot * burn * starts
        fuel_kg = prop_kg / (1 + en.of)
        prop_usd = fuel_kg * E.PROPELLANT_USD_PER_KG.get(en.fuel, 1.0) + (prop_kg - fuel_kg) * E.PROPELLANT_USD_PER_KG.get(en.ox, 1.0)
        f = max(en.thrust_vac, 1e3) / 1e6
        cost = 0.6 * u1 + 0.2e6 + starts * 0.25e6 * f ** 0.5 + prop_usd
        minutes = 3.0 + 0.4 * starts + burn * starts / 300.0
        return {"kind": kind, "key": engine_key(eng), "facility": fac, "cost_usd": round(cost, 0),
                "minutes": round(minutes, 2), "note": "includes a test article at 60% of the engine's first-unit cost"}
    if kind in ("stage_static_fire", "structural_qualification"):
        stage, engines = spec.get("stage"), spec.get("engines") or {}
        if not isinstance(stage, dict) or stage.get("kind") != "stage":
            raise ValueError("target needs {'stage': {...a stage element...}, 'engines': {...}}")
        v = _mini_vehicle(stage, engines)
        bad = [x for x in v.errors if "no stage with engines" not in x and "exactly one capsule" not in x]
        if bad:
            raise ValueError("stage design is invalid: " + "; ".join(bad[:3]))
        rep = E.cost_report(v)
        stage_usd = rep["by_element_usd"].get(str(stage.get("id")), 0.0)
        if kind == "stage_static_fire":
            if not stage.get("engines"):
                raise ValueError("a static fire needs a stage with engines")
            burn = float(params.get("burn_s", 30))
            if not (5 <= burn <= 300):
                raise ValueError("burn_s must be 5 to 300")
            el = v.elements[0]
            mdot = sum(en.mdot * n for en, n in el.engines)
            cost = 0.12 * stage_usd + 1.0e6 + mdot * burn * 0.5
            return {"kind": kind, "key": stage_key(stage, engines), "facility": fac, "cost_usd": round(cost, 0),
                    "minutes": 12.0}
        tank_usd = sum(it["usd"] for it in rep["items"] if it["item"].startswith("tanks"))
        return {"kind": kind, "key": tank_key(stage), "facility": fac, "cost_usd": round(0.5 * tank_usd + 1.5e6, 0),
                "minutes": 8.0}
    if kind == "wind_tunnel":
        if "stack" in spec:
            return {"kind": kind, "key": stack_key(spec["stack"]), "facility": fac, "cost_usd": 2.5e6, "minutes": 6.0}
        if "shell" in spec:
            return {"kind": kind, "key": shell_key(spec["shell"]), "facility": fac, "cost_usd": 3.5e6, "minutes": 6.0}
        raise ValueError("target needs {'stack': [...]} or {'shell': {...}}")
    if kind == "arcjet":
        mat = spec.get("tps")
        if mat not in D.TPS:
            raise ValueError(f"tps must be one of {sorted(D.TPS)}")
        q = float(params.get("heat_flux_W_m2", 1e6))
        dur = float(params.get("duration_s", 60))
        if not (1e5 <= q <= 4e8 and 5 <= dur <= 600):
            raise ValueError("heat_flux_W_m2 must be 1e5 to 4e8 and duration_s 5 to 600")
        return {"kind": kind, "key": tps_key(mat), "facility": fac, "cost_usd": round(1.2e6 + 2e3 * dur, 0), "minutes": 5.0}
    if kind == "parachute_drop":
        if spec.get("kind") != "parachute":
            raise ValueError("target must be a parachute element")
        sup = spec.get("type", "dgb") == "dgb"
        return {"kind": kind, "key": chute_key(spec), "facility": fac, "cost_usd": 9e6 if sup else 2e6,
                "minutes": 15.0 if sup else 8.0}
    if kind == "isru_demo":
        return {"kind": kind, "key": ISRU_KEY, "facility": fac, "cost_usd": 6e6, "minutes": 10.0}
    if kind == "vibration_test":
        if not isinstance(spec, dict) or spec.get("kind") not in ("stage", "capsule", "heatshield", "backshell", "parachute"):
            raise ValueError("target must be an element (not a fairing)")
        a, g = params.get("acoustic_dB"), params.get("shock_g")
        if a is None and g is None:
            raise ValueError("give acoustic_dB, shock_g or both")
        if a is not None and not (120 <= float(a) <= 165):
            raise ValueError("acoustic_dB must be 120 to 165")
        if g is not None and not (100 <= float(g) <= 30000):
            raise ValueError("shock_g must be 100 to 30000")
        return {"kind": kind, "key": vibro_key(spec), "facility": fac,
                "cost_usd": (1.0e6 if a is not None else 0) + (0.5e6 if g is not None else 0), "minutes": 6.0}
    raise ValueError(kind)


def _evidence(key, failures, anomalies, expected):
    """What one test says about the hidden reliability factor of its target: the failures and anomalies it
    logged (events), and how many a factor of 1 would give over the same exposure (expected_events)."""
    return {"key": key, "failures": int(failures), "anomalies": int(anomalies), "events": int(failures + anomalies),
            "expected_events": round(expected, 6)}


def _anomalies(n):
    return "No anomalies logged." if n == 0 else f"{n} {'anomaly' if n == 1 else 'anomalies'} logged."


def run_test(kind: str, spec: dict, params: dict, test_id: str, ledger: list, secret: str) -> dict:
    """The outcome of one test. Deterministic for (secret, test_id). 'summary' is one plain sentence."""
    params = params or {}
    kind = canonical_kind(kind)
    if kind in _wired_kinds():
        return _wired_run(kind, spec, params, test_id, ledger, secret)
    q = quote(kind, spec, params)
    key = q["key"]
    rng = _rng(secret, "test:" + str(test_id))
    rec = record_from_ledger(ledger)
    out = {"kind": kind, "key": key, "measured": {}, "engine_exposure": {}}

    def noisy(x, s):
        return round(float(x * (1 + rng.normal(0, s))), 5)

    if kind == "engine_hotfire":
        eng = spec.get("engine", spec)
        model = _engine_model(eng)
        truth = engine_truth(secret, eng, model)
        hidden = not eng.get("catalog")
        factor = reliability_factor(secret, key)
        starts, burn = int(params.get("starts", 3)), float(params.get("burn_s", 60))
        r0 = rec["engines"].get(key, {"starts": 0, "seconds": 0.0, "failures": 0})
        done_starts, done_s, failure = 0, 0.0, None
        random_failures, anomalies, expected = 0, 0, 0.0   # evidence on the hidden factor
        for i in range(starts):
            sofar = {"starts": r0["starts"] + done_starts, "seconds": r0["seconds"] + done_s}
            rel, mod = engine_reliability(eng, sofar, factor), engine_reliability(eng, sofar)
            new = engine_reliability(eng, {})   # the untested chances, which set the anomaly rate
            done_starts += 1
            if truth["flaw"] == "unstable":
                failure = ("combustion instability: pressure oscillations grew within a second of ignition and "
                           "destroyed the injector and chamber")
                break
            expected += mod["p_start"] + ANOMALY_RATIO * new["p_start"]
            if hidden:
                anomalies += int(rng.poisson(ANOMALY_RATIO * factor * new["p_start"]))
            if rng.uniform() < rel["p_start"]:
                random_failures += 1
                failure = f"start {i + 1} failed: a hard start damaged the chamber (random failure)"
                break
            # A real cooling margin below 1 burns through after 20 s of firing over all the starts, as in
            # flight, unless a random failure comes first.
            span = min(burn, 20.0 - done_s) if truth["flaw"] == "cooling" else burn
            ran = span
            if rng.uniform() < 1 - math.exp(-rel["lambda_per_s"] * span):
                ran = float(rng.uniform(0, span))
                random_failures += 1
                failure = f"start {i + 1} shut down {ran:.0f} s in on a turbomachinery fault (random failure)"
            done_s += ran
            expected += (mod["lambda_per_s"] + ANOMALY_RATIO * new["lambda_per_s"]) * ran
            if hidden:
                anomalies += int(rng.poisson(ANOMALY_RATIO * factor * new["lambda_per_s"] * ran))
            if failure:
                break
            if span < burn:
                failure = (f"the chamber wall burned through in start {i + 1}, after about 20 s of firing: the real cooling margin is "
                           f"{truth['cooling_margin']:.2f}, below 1")
                break
        out["engine_exposure"][key] = {"starts": done_starts, "seconds": done_s, "failures": 1 if failure else 0}
        if done_s >= 1.0 or (done_starts and not failure):
            out["measured"] = {"isp_vac_s": noisy(model.isp_vac * truth["isp"], 0.001),
                               "thrust_vac_kN": noisy(model.thrust_vac * truth["thrust"] / 1e3, 0.002),
                               "isp_factor": noisy(truth["isp"], 0.001), "thrust_factor": noisy(truth["thrust"], 0.002),
                               "model_isp_vac_s": round(model.isp_vac, 2), "model_thrust_vac_kN": round(model.thrust_vac / 1e3, 2)}
            if truth["cooling_margin"] is not None and (done_s >= 20.0 or burn >= 20):
                out["measured"]["cooling_margin"] = noisy(truth["cooling_margin"], 0.03)
                out["measured"]["model_cooling_margin"] = round(model.cooling_margin, 3)
        rel = engine_reliability(eng, {"starts": r0["starts"] + done_starts, "seconds": r0["seconds"] + done_s})
        tested = {"test_starts": r0["starts"] + done_starts, "test_seconds": round(r0["seconds"] + done_s, 1)}
        if hidden:
            out["reliability_after"] = {"model_p_start": round(rel["p_start"], 6),
                                        "model_lambda_per_s": float(f"{rel['lambda_per_s']:.3e}"), **tested,
                                        "note": "model values; the real ones are these times the design's hidden reliability factor"}
            out["reliability_evidence"] = _evidence(key, random_failures, anomalies, expected)
        else:
            out["reliability_after"] = {"p_start": rel["p_start"], "lambda_per_s": rel["lambda_per_s"], "flight_proven": True, **tested}
        out["failure"] = failure
        out["summary"] = (f"Hot fire of the engine design: {done_starts} starts, {done_s:.0f} s. " +
                          (f"Failed: {failure}." if failure else "All starts and burns were clean.") +
                          (f" {_anomalies(anomalies)}" if hidden else "") +
                          (f" Measured vacuum Isp {out['measured']['isp_vac_s']:.1f} s against {model.isp_vac:.1f} s modeled."
                           if out["measured"] else ""))
        return out

    if kind == "stage_static_fire":
        stage, engines = spec["stage"], spec.get("engines") or {}
        v = _mini_vehicle(stage, engines)
        burn = float(params.get("burn_s", 30))
        n_prev = rec["stage_fires"].get(key, 0)
        p_model = STAGE_IGNITION_P[min(n_prev, len(STAGE_IGNITION_P) - 1)]
        factor = reliability_factor(secret, key)
        p_int = factor * p_model
        failure = None
        for en, n in v.elements[0].engines:
            spec_e = engines.get(en.name) or {}
            tr = engine_truth(secret, spec_e, en)
            ek = engine_key(spec_e)
            if tr["flaw"] == "unstable":
                failure = f"engine {en.name} went unstable at ignition and destroyed the stage article"
            elif tr["flaw"] == "cooling" and burn > 20 and failure is None:
                failure = f"engine {en.name} burned through about 20 s in (real cooling margin {tr['cooling_margin']:.2f})"
            out["engine_exposure"][ek] = {"starts": n, "seconds": n * (min(burn, 20.0) if failure else burn), "failures": 0}
        integration_failures, anomalies, expected = 0, 0, 0.0
        if failure is None:   # the stage's own ignition sequence runs only if no engine flaw ended the test first
            expected = p_model + ANOMALY_RATIO * STAGE_IGNITION_P[0]
            anomalies = int(rng.poisson(ANOMALY_RATIO * factor * STAGE_IGNITION_P[0]))
            if rng.uniform() < p_int:
                integration_failures = 1
                failure = "a propellant valve stuck and the stage aborted at ignition (integration failure); it is fixed for flight"
        out["integration_ok"] = failure is None
        out["failure"] = failure
        out["reliability_after"] = {"model_stage_first_ignition_p": STAGE_IGNITION_P[min(n_prev + 1, len(STAGE_IGNITION_P) - 1)],
                                    "static_fires": n_prev + 1,
                                    "note": "model value; the real one is this times the stage's hidden reliability factor"}
        out["reliability_evidence"] = _evidence(key, integration_failures, anomalies, expected)
        out["summary"] = (f"Stage static fire, {burn:.0f} s: " + (f"failed: {failure}." if failure else "clean.") +
                          f" {_anomalies(anomalies)}")
        return out

    if kind == "structural_qualification":
        stage = spec["stage"]
        v = _mini_vehicle(stage, spec.get("engines") or {})
        el = v.elements[0]
        tt = tank_truth(secret, stage)
        mat = D.MATERIALS[el.material]
        worst = None
        for role, tk in el.tanks.items():
            hoop = el.tank_pressure * tk.radius / tk.wall_t
            ratio = mat["sy"] * tt["weld"] / (hoop * D.PROOF_FACTOR)
            if worst is None or ratio < worst[1]:
                worst = (role, ratio)
        passed = worst is not None and worst[1] >= 1.0
        out["measured"] = {"weld_efficiency": noisy(tt["weld"], 0.01), "knockdown": noisy(tt["knockdown"], 0.02),
                           "proof_margin": round(worst[1], 3) if worst else None}
        out["qualified"] = passed
        out["failure"] = None if passed else (f"the {worst[0]} tank burst at {100 * worst[1]:.0f}% of proof pressure "
                                             f"(weld efficiency {tt['weld']:.2f})" if worst else "no tanks to test")
        if passed:
            out["covers"] = {"tank_capacity_kg": _tank_capacity(stage)}
        out["summary"] = ("Structural qualification passed: this tank design, with these walls and a capacity of "
                          f"{_tank_capacity(stage):.0f} kg, may use the tested factor of safety 1.25."
                          if passed else f"Structural qualification failed: {out['failure']}.")
        return out

    if kind == "wind_tunnel":
        if "stack" in spec:
            tr = stack_truth(secret, spec["stack"])
            out["measured"] = {"cd_factor": noisy(tr["cd"], 0.01)}
            out["summary"] = f"Launch stack drag is {out['measured']['cd_factor']:.3f} times the model."
        else:
            tr = shell_truth(secret, spec["shell"])
            out["measured"] = {"cd_factor": noisy(tr["cd"], 0.01), "ld_factor": noisy(tr["ld"], 0.02)}
            out["summary"] = (f"Aeroshell drag is {out['measured']['cd_factor']:.3f} and lift-to-drag "
                              f"{out['measured']['ld_factor']:.3f} times the model.")
        return out

    if kind == "arcjet":
        mat = spec["tps"]
        tr = tps_truth(secret, mat)
        qf = float(params.get("heat_flux_W_m2", 1e6))
        q_true = D.TPS[mat]["q_max"] * tr["q_max"]
        survived = qf <= q_true
        out["measured"] = {"capacity_factor": noisy(tr["capacity"], 0.03), "survived_heat_flux_W_m2": qf if survived else None}
        out["failure"] = None if survived else f"the {mat} sample failed at {qf / 1e6:.2f} MW/m^2"
        out["summary"] = (f"{mat} {'survived' if survived else 'failed at'} {qf / 1e6:.2f} MW/m^2; its heat-load capacity is "
                          f"{out['measured']['capacity_factor']:.3f} times the design correlation.")
        return out

    if kind == "parachute_drop":
        tr = chute_truth(secret, spec)
        n_prev = rec["chutes"].get(key, 0)
        p_model, p_new = chute_reliability(spec.get("type", "dgb"), n_prev), chute_reliability(spec.get("type", "dgb"), 0)
        factor = reliability_factor(secret, key)
        p = factor * p_model
        ok = rng.uniform() >= p
        anomalies = int(rng.poisson(ANOMALY_RATIO * factor * p_new))
        out["measured"] = {"cd_factor": noisy(tr["cd"], 0.02)}
        out["reliability_after"] = {"model_deployment_failure_p": round(chute_reliability(spec.get("type", "dgb"), n_prev + 1), 5),
                                    "drops": n_prev + 1,
                                    "note": "model value; the real one is this times the parachute's hidden reliability factor"}
        out["reliability_evidence"] = _evidence(key, 0 if ok else 1, anomalies, p_model + ANOMALY_RATIO * p_new)
        out["failure"] = None if ok else "the canopy tore during inflation; the fix is folded into the design"
        out["summary"] = ("Drop test: clean deployment." if ok else f"Drop test: {out['failure']}.") + \
            f" {_anomalies(anomalies)} Drag is {out['measured']['cd_factor']:.3f} times the model."
        return out

    if kind == "vibration_test":
        from .vibro import DEFAULT_QUAL_ACOUSTIC_DB, DEFAULT_QUAL_SHOCK_G
        tr = vibro_truth(secret, spec)
        # A level the design leaves unset is the one the vehicle gives it (vibro.default_levels), passed in as
        # qual_acoustic_resolved_dB by the program office.
        qa = float(spec.get("qual_acoustic_dB", spec.get("qual_acoustic_resolved_dB", DEFAULT_QUAL_ACOUSTIC_DB)))
        qs = float(spec.get("qual_shock_g", DEFAULT_QUAL_SHOCK_G))
        broke, said = [], []
        if params.get("acoustic_dB") is not None:
            a = float(params["acoustic_dB"])
            if a > qa + tr["acoustic_margin_dB"]:
                broke.append(f"broke at {a:.1f} dB")
            else:
                out["measured"]["acoustic_margin_dB"] = round(max(a - qa, 0.0), 2)
                said.append(f"survived {a:.1f} dB")
        if params.get("shock_g") is not None:
            g = float(params["shock_g"])
            if g > qs * 10 ** (tr["shock_margin_dB"] / 20):
                broke.append(f"broke at {g:.0f} g shock")
            else:
                out["measured"]["shock_margin_dB"] = round(max(20 * math.log10(g / qs), 0.0), 2)
                said.append(f"survived {g:.0f} g shock")
        out["failure"] = "; ".join(broke) or None
        out["summary"] = (f"Vibration test of {spec.get('id')} (built to {qa:.1f} dB and {qs:.0f} g): "
                          + "; ".join(said + broke) + ".")
        return out

    if kind == "isru_demo":
        f = isru_truth(secret)
        out["measured"] = {"production_factor": noisy(f, 0.03)}
        out["summary"] = f"The ISRU plant makes {out['measured']['production_factor']:.3f} times the modeled oxygen."
        return out
    raise ValueError(kind)


def catalog() -> dict:
    from .economics import CATALOG_ENGINES, HARDWARE_BUDGET_USD, RND_BUDGET_USD
    extra = {}
    if _sub("notices").enabled():
        extra["notices"] = "The program office posts notices during the run that change these rules."
    C = _sub("contractors")
    if C.enabled():
        extra["contractors"] = {"shares": dict(C.SHARES), "budgets": C.budgets(HARDWARE_BUDGET_USD, RND_BUDGET_USD),
                                "interface_fields": dict(C.INTERFACE_FIELDS)}
    return {**_catalog_base(CATALOG_ENGINES, HARDWARE_BUDGET_USD, RND_BUDGET_USD), **extra}


def _catalog_base(CATALOG_ENGINES, HARDWARE_BUDGET_USD, RND_BUDGET_USD) -> dict:
    return {"budgets": {"hardware_budget_usd": HARDWARE_BUDGET_USD, "rnd_budget_usd": RND_BUDGET_USD},
            "tests": test_kinds(), "facilities": facilities(),
            "hidden_reliability": {
                "factor_prior": f"gamma, shape {FACTOR_SHAPE:g}, mean 1, one per engine design you develop, stage and parachute",
                "anomaly_rate": f"{ANOMALY_RATIO:g} times the design's untested failure chance, times its factor, per "
                                "start, second, stage ignition or deployment, however much the design has been tested",
                "estimate": f"({FACTOR_SHAPE:g} + sum of events) / ({FACTOR_SHAPE:g} + sum of expected_events) over a part's "
                            "results (reliability_evidence)"},
            "catalog_engines": {k: {"unit_usd": v["unit_usd"], "note": v["note"], "spec": v["spec"]}
                                for k, v in CATALOG_ENGINES.items()}}


def contractor_report(design: dict, report: dict, tests=(), events=()) -> dict:
    """each contractor's spending against its share of the budgets in force (after any program notices).
    Gives {"enabled": False} when MARSFLIGHT_CONTRACTORS is off. Reports only; it does not score."""
    C = _sub("contractors")
    if not C.enabled():
        return {"enabled": False}
    r = _sub("notices").rules(events)
    return C.split(design, report, r["hardware_budget_usd"], r["rnd_budget_usd"], tests)


def _main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    try:
        if cmd == "catalog":
            print(json.dumps(catalog()))
            return 0
        req = json.loads(sys.stdin.read() or "{}")
        if cmd == "quote":
            print(json.dumps(quote(req["kind"], req.get("spec") or {}, req.get("params") or {})))
        elif cmd == "run":
            with open(req["secret_file"]) as f:
                secret = json.load(f)["secret"]
            print(json.dumps(run_test(req["kind"], req.get("spec") or {}, req.get("params") or {}, req["test_id"],
                                      req.get("ledger") or [], secret), default=float))
        elif cmd == "notices":
            print(json.dumps(_sub("notices").cli_notices(req)))
        elif cmd == "rules":
            print(json.dumps(_sub("notices").cli_rules(req)))
        elif cmd == "contractors":
            print(json.dumps(contractor_report(req.get("design") or {}, req.get("report") or {},
                                               req.get("tests") or [], req.get("events") or [])))
        else:
            print(json.dumps({"error": f"unknown command {cmd!r}"}))
            return 2
    except Exception as ex:  # noqa: BLE001
        print(json.dumps({"error": f"{type(ex).__name__}: {ex}"}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
