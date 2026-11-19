"""The program office's link between a team's SysML model and the hidden simulator. Runs on the hub.

    python -m designlang.hidden.office CMD < request.json

Commands (request and reply are JSON):
- catalog: the test kinds, facilities, budgets and catalog engines, as a team reads them.
- review: compile the model and run the simulator's build checks. Reply: ok, errors, warnings (all
  team-safe), the stack's elements, and every part a test can target.
- quote: compile the model, resolve the test's target (a part path such as "vehicle.S1"), and price the
  test. Reply: the quote, plus "spec", the target in the simulator's own form. The program office keeps
  "spec" private and runs the test on it, so a test measures the design as it was when ordered.
- run: run one test on a spec. Reply: "raw", the simulator's result (it goes in the ledger the verifier
  reads), and "team", what the team sees: absolute measurements, no model values or factors.
- cost: the hardware cost report of the model (what `marsflight cost` printed). Whether teams keep this
  is open.

A mission simulation (test kind "mission_sim") flies the team's committed design and flight/ through whole
dispersed worlds on the hidden simulator: the design as specified (no hidden as-built truth), with the tank
qualifications the team earned. The flight software runs as an unprivileged user that cannot read the
secret, the ledger or the simulator. Its quote pins the commit; its run takes real minutes, so the program
office runs it in the background.

The model comes from the team repository: {"repo": "/srv/git/team.git", "ref": "main"} reads every
design/**/*.sysml file at that ref, and {"dir": PATH} reads design/**/*.sysml under a folder (tests).
"""
from __future__ import annotations

import glob
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile

from .compile import CompileError, compile_files
from .sanitize import team_message, team_messages

MASS_LAB = "mass_properties_lab"
# Mission simulation: whole flights of the team's design and flight software.
SIM_KIND = "mission_sim"
SIM_FACILITY = "mission_simulator"
SIM_SLOTS = 2                   # simulations at once: one CPU each on the hub
SIM_USD_PER_WORLD = 1.0e6       # R&D price of one simulated world (a static fire is $10M to $17M)
SIM_MAX_WORLDS = 4
SIM_MAX_WALL_S = 1500.0         # wall clock per world, as in the verifier
SIM_USER = "marsflight-sim"     # the flight software's user during a simulation
SIM_EVENTS = 200                # events returned per world
CD_MACH = [0.6, 0.9, 1.2, 2.0, 3.0, 5.0, 10.0]


class TeamError(Exception):
    """A problem the team may read in full."""


# ---- the model

def model_files(source: dict) -> tuple[list, str]:
    """The design/**/*.sysml files of a team's model, copied to a new folder (the caller removes it)."""
    tmp = tempfile.mkdtemp(prefix="office-model-")
    if source.get("dir"):
        root = os.path.join(source["dir"], "design")
        for f in sorted(glob.glob(os.path.join(root, "**", "*.sysml"), recursive=True)):
            dst = os.path.join(tmp, os.path.relpath(f, root))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy(f, dst)
    else:
        repo, ref = source.get("repo", "/srv/git/team.git"), str(source.get("ref") or "main")
        if ref.startswith("-") or any(c in ref for c in " \n\t;|&$`"):
            raise TeamError(f"ref {ref!r} is not a branch, tag or commit")
        p = subprocess.run(["git", "-c", "safe.directory=*", "--git-dir", repo, "archive", "--format=tar", ref, "design/"], capture_output=True)
        if p.returncode != 0:
            raise TeamError(f"cannot read design/ at {ref} in the team repository: "
                            f"{p.stderr.decode(errors='replace').strip()[:200]}")
        subprocess.run(["tar", "-x", "-C", tmp], input=p.stdout, check=True)
        files = sorted(glob.glob(os.path.join(tmp, "design", "*.sysml")))
    found = sorted(glob.glob(os.path.join(tmp, "**", "*.sysml"), recursive=True))
    if not found:
        shutil.rmtree(tmp, ignore_errors=True)
        raise TeamError("there is no design/**/*.sysml in the team's model")
    return found, tmp


def compiled(source: dict):
    """(internal design, checked description) of the team's model, or TeamError with safe messages."""
    files, tmp = model_files(source)
    try:
        try:
            design, report = compile_files(files)
        except CompileError as ex:
            msgs = [_strip_tmp(e, tmp) for e in ex.errors]
            raise TeamError("\n".join(msgs if ex.public else team_messages(msgs)))
        return design, report["description"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _strip_tmp(msg, tmp):
    return str(msg).replace(tmp + os.sep, "").replace(tmp, "")


def review(source: dict) -> dict:
    from marsflight.vehicle import build_vehicle
    try:
        design, desc = compiled(source)
    except TeamError as ex:
        return {"ok": False, "errors": str(ex).split("\n"), "warnings": []}
    v = build_vehicle(design)
    return {"ok": not v.errors, "errors": team_messages(v.errors), "warnings": team_messages(v.warnings),
            "elements": [e["id"] for e in design["elements"]], "targets": targets(desc), "vibration": vibration(v)}


def vibration(v) -> dict:
    """What a launch provider's user guide tells a payload: the liftoff noise each payload element hears in the
    fairing, and the qualification levels it is built to (the team's own, or the default the manual states). No
    pogo margin: that comes from the hidden stage and engine model, and only flight shows it."""
    from marsflight import vibro
    if v.errors or not v.elements or not v.elements[0].engines:
        return {}
    levels = vibro.liftoff_acoustics(v.elements)["levels"]
    return {e.id: {"liftoff_level_dB": round(levels[e.id], 1), "qualification_acoustic_dB": round(e.qual_acoustic_dB, 1),
                   "qualification_shock_g": round(e.qual_shock_g), "separation_shock_g": vibro.SEPARATION_SHOCK_G}
            for e in v.elements if e.vibro_payload and e.id in levels}


def targets(desc) -> dict:
    out = {"stack": "vehicle", "elements": [], "engines": [], "tps": [], "parachutes": [], "isru": []}
    for e in desc["elements"]:
        p = f"vehicle.{e['id']}"
        out["elements"].append(p)
        for x in e.get("engines", []):
            out["engines"].append(f"{p}.{x['part']}")
        if e.get("tps"):
            out["tps"].append(p)
        if e["kind"] == "parachute":
            out["parachutes"].append(p)
        if e.get("isru_W"):
            out["isru"].append(p)
    return out


# ---- targets

def resolve(kind: str, target: str, design: dict, desc: dict) -> dict:
    """The simulator's form of a test target named by its part path."""
    if not isinstance(target, str):
        raise TeamError("target is a part path in the model, e.g. \"vehicle.S1\" or \"vehicle.S1.mainEngines\"")
    parts = [p for p in target.strip().split(".") if p]
    if parts and parts[0] == "vehicle":
        parts = parts[1:]
    els = {e["id"]: e for e in design["elements"]}
    descs = {e["id"]: e for e in desc["elements"]}
    if kind == "wind_tunnel" and not parts:
        return {"stack": design["elements"]}
    if not parts or parts[0] not in els:
        raise TeamError(f"no stack element {parts[0] if parts else target!r} in the model; elements are {', '.join(els)}")
    eid, el, de = parts[0], els[parts[0]], descs[parts[0]]
    rest = parts[1:]
    if kind == "engine_hotfire":
        eng = {x["part"]: x["engine"] for x in de.get("engines", [])}
        if len(rest) != 1 or rest[0] not in eng:
            raise TeamError(f"an engine hot fire targets an engine part, one of {', '.join(f'vehicle.{eid}.{p}' for p in eng) or 'none on ' + eid}")
        return {"engine": design["engines"][eng[rest[0]]]}
    if rest:
        raise TeamError(f"a {kind} test targets a whole stack element, e.g. vehicle.{eid}")
    if kind in ("stage_static_fire", "structural_qualification"):
        if el["kind"] != "stage" or not el.get("tank_capacity_kg"):
            raise TeamError(f"{eid} is not a stage with tanks")
        names = {x["engine"] for x in el.get("engines", [])}
        return {"stage": el, "engines": {n: design["engines"][n] for n in names}}
    if kind == "wind_tunnel":
        if el["kind"] not in ("heatshield", "capsule"):
            raise TeamError("a wind tunnel test targets the launch stack (\"vehicle\") or a heatshield or capsule")
        return {"shell": el}
    if kind == "arcjet":
        if not el.get("tps"):
            raise TeamError(f"{eid} has no ThermalProtection part")
        return {"tps": el["tps"], "_areal": el.get("areal_mass_kg_m2", 0.0), "_element": eid}
    if kind == "parachute_drop":
        if el["kind"] != "parachute":
            raise TeamError(f"{eid} is not a parachute")
        return el
    if kind == "isru_demo":
        if not el.get("isru_kW"):
            raise TeamError(f"{eid} has no IsruOxygenPlant")
        return {"isru": True, "_kW": el["isru_kW"]}
    if canonical(kind) == "weighing":
        return {"element": eid, "design": design}
    from marsflight import program as P
    if P._wired_kinds().get(kind, ("",))[0] in ("failures", "propulsion_gaps"):
        return {"element": eid}            # the failure model tests name a stack element
    if kind == "vibration_test":
        from marsflight.vehicle import build_vehicle
        built = build_vehicle(design).by_id(eid)
        if built is None or not built.vibro_payload:
            raise TeamError(f"{eid} is not payload: a vibration test targets an element a fairing covers, or one "
                            "that is not a stage")
        # The level the unit is built to: the team's own, or the default the manual states.
        return dict(el, qual_acoustic_resolved_dB=round(built.qual_acoustic_dB, 1))
    raise TeamError(f"unknown test kind {kind!r}")


def _private(spec):
    """The spec without the view-only fields, as the simulator takes it."""
    return {k: v for k, v in spec.items() if not k.startswith("_")} if isinstance(spec, dict) else spec


# ---- quotes and runs

# The office bills tests in steps of $0.1M. Several test prices follow a part's cost (a hot fire uses 60% of an
# engine's first unit, a weighing 1% of the element's cost), and parts are priced per kg of computed mass, so an
# exact price would give the masses away. A step of $0.1M blurs an element's cost to $10M through a weighing.
QUOTE_STEP_USD = 0.1e6
# What cost_report shows the team: the hardware total, each element's cost to the nearest $1M, and the room
# left in the budgets. Not the bill of materials: its lines are priced per kg of computed mass, and some name it.
COST_TOTAL_STEP_USD, COST_ELEMENT_STEP_USD = 0.1e6, 1e6


def _step(usd, step, up=False):
    n = usd / step
    return float((math.ceil(n - 1e-9) if up else round(n)) * step)


def team_cost(rep: dict) -> dict:
    """The team's view of the cost report. over_budget is decided on the exact total, as the verifier decides it."""
    return {"hardware_usd": _step(rep["hardware_usd"], COST_TOTAL_STEP_USD),
            "hardware_budget_usd": rep["hardware_budget_usd"], "over_budget": bool(rep["over_budget"]),
            "hardware_left_usd": _step(rep["hardware_budget_usd"] - rep["hardware_usd"], COST_TOTAL_STEP_USD),
            "rnd_budget_usd": rep["rnd_budget_usd"],
            "by_element_usd": {k: _step(v, COST_ELEMENT_STEP_USD) for k, v in rep["by_element_usd"].items()},
            "rounding": "the total and the room left to the nearest $0.1M, each element to the nearest $1M; "
                        "over_budget is exact"}


def canonical(kind):
    """One name per test: the office's old "mass_properties" is program.py's "weighing"."""
    from marsflight.program import canonical_kind
    return canonical_kind(kind)


def _program_weighs():
    """Whether program.py runs the weighing (MASS_GROWTH on: it weighs the hidden as-built mass, and the flight
    counts it); otherwise the office weighs the design's dry mass as before."""
    from marsflight import program as P
    return "weighing" in P._wired_kinds()


def quote(kind, spec, params):
    from marsflight import program as P
    kind = canonical(kind)
    if kind == "weighing" and _program_weighs():
        q = P.quote(kind, _private(spec), params)
    elif kind == "weighing":
        from marsflight.economics import cost_report
        from marsflight.vehicle import build_vehicle
        v = build_vehicle(spec["design"])
        usd = cost_report(v, spec["design"])["by_element_usd"].get(spec["element"], 0.0)
        q = {"kind": kind, "key": mass_key(spec), "facility": MASS_LAB, "cost_usd": 0.2e6 + 0.01 * usd,
             "minutes": 4.0, "note": "weighs a built article of the element"}
    else:
        q = P.quote(kind, _private(spec), params)
    return {**q, "cost_usd": _step(q["cost_usd"], QUOTE_STEP_USD, up=True)}


def mass_key(spec):
    el = next(e for e in spec["design"]["elements"] if e["id"] == spec["element"])
    blob = json.dumps({"element": el, "engines": spec["design"].get("engines")}, sort_keys=True)
    return "mass:" + hashlib.sha256(blob.encode()).hexdigest()[:14]


def run(kind, spec, params, test_id, ledger, secret):
    from marsflight import program as P
    if kind == SIM_KIND:
        raw = run_sim(spec, params, test_id, ledger, secret)
        return {"raw": raw, "team": sim_team_view(raw)}
    kind = canonical(kind)
    if kind == "weighing" and not _program_weighs():
        raw = run_mass(spec, test_id, secret)
    else:
        raw = P.run_test(kind, _private(spec), params, test_id, ledger, secret)
    return {"raw": raw, "team": team_view(kind, spec, params, raw)}


def run_mass(spec, test_id, secret):
    from marsflight import program as P
    from marsflight.vehicle import build_vehicle
    v = build_vehicle(spec["design"])
    el = v.by_id(spec["element"])
    rng = P._rng(secret, "test:" + str(test_id))
    m = float(el.dry_mass * (1 + rng.normal(0, 0.001)))
    return {"kind": "weighing", "key": mass_key(spec), "measured": {"dry_mass_kg": round(m, 1)},
            "engine_exposure": {}, "failure": None,
            "summary": f"{spec['element']} weighs {m:,.1f} kg dry (scale accuracy 0.1%)."}


def team_view(kind, spec, params, raw):
    """What a team reads: absolute measurements of the built article, and the reliability bookkeeping the
    manual describes. No model value, no factor against the hidden model, no hidden allowable."""
    from marsflight import data as D
    from marsflight import sim as SIM
    out = {k: raw[k] for k in ("kind", "key", "failure", "reliability_after", "reliability_evidence",
                               "integration_ok", "qualified") if k in raw}
    meas = raw.get("measured") or {}
    m = {}
    if kind == "engine_hotfire":
        for k in ("isp_vac_s", "thrust_vac_kN", "cooling_margin"):
            if k in meas:
                m[k] = meas[k]
        s = raw.get("summary", "").split(" Measured vacuum Isp")[0]
        if "isp_vac_s" in m:
            s += f" Measured vacuum Isp {m['isp_vac_s']:.1f} s and vacuum thrust {m['thrust_vac_kN']:.1f} kN."
        out["summary"] = s
    elif kind == "structural_qualification":
        m = {k: meas[k] for k in ("weld_efficiency", "knockdown") if k in meas}
        stage = spec["stage"]
        if raw.get("qualified"):
            # The capacity comes from the hidden propellant densities, so the team reads the size, not the kg.
            out["covers"] = (f"tanks with {stage.get('id')}'s walls whose capacity rounds to the same 1,000 kg of "
                             "propellant as the tested article")
            out["summary"] = ("Structural qualification passed: this tank design, at the size tested, may use the tested "
                              "factor of safety 1.25. The tank key rounds capacity to 1,000 kg of propellant, so a longer "
                              "or shorter tank with the same walls is a new tank design once its capacity rounds differently.")
        else:
            ratio = meas.get("proof_margin")
            if ratio is not None:
                p = stage["tank_pressure_bar"] * D.PROOF_FACTOR * ratio
                m["burst_pressure_bar"] = round(p, 2)
                out["failure"] = f"a tank burst at {p:.2f} bar during the proof test to {stage['tank_pressure_bar'] * D.PROOF_FACTOR:.2f} bar"
            else:
                out["failure"] = "no tanks to test"
            out["summary"] = f"Structural qualification failed: {out['failure']}."
        if m.get("knockdown") is not None:
            m["knockdown_note"] = "measured axial buckling load over the NASA SP-8007 design value"
    elif kind == "wind_tunnel":
        f = meas.get("cd_factor", 1.0)
        if "stack" in spec:
            dmax = max(e.get("diameter_m", 0.0) for e in spec["stack"])
            m["reference_area_m2"] = round(math.pi * dmax * dmax / 4, 3)
            m["axial_force_coefficient"] = {str(M): round(SIM.interp(D.SLENDER_CA_MACH, M) * f, 4) for M in CD_MACH}
            out["summary"] = "Launch stack axial force coefficient measured from Mach 0.6 to 10 (zero angle of attack)."
        else:
            el = spec["shell"]
            ld = el.get("ld_trim", 0.0)
            cd_h = SIM.newtonian_cd(el["half_angle_deg"], el.get("nose_radius_m", 0.25 * el["diameter_m"]), el["diameter_m"] / 2) * (1 - 0.5 * ld)
            m["reference_area_m2"] = round(math.pi * el["diameter_m"] ** 2 / 4, 4)
            m["drag_coefficient"] = {str(M): round(cd_h * SIM.interp(D.CAPSULE_CD_MACH, M) * f, 4) for M in CD_MACH}
            m["lift_to_drag_at_trim"] = round(ld * meas.get("ld_factor", 1.0), 4)
            out["summary"] = (f"{el['id']}: drag coefficient {m['drag_coefficient']['10.0']:.3f} at Mach 10 and "
                              f"lift-to-drag {m['lift_to_drag_at_trim']:.3f} at its trim angle.")
    elif kind == "arcjet":
        mat = spec["tps"]
        q = float((params or {}).get("heat_flux_W_m2", 1e6))
        cap = (spec.get("_areal", 0.0) / D.TPS[mat]["k_mass"]) ** (1 / D.TPS_EXPONENT) * meas.get("capacity_factor", 1.0)
        m["survived_heat_flux_W_m2"] = meas.get("survived_heat_flux_W_m2")
        m["heat_load_capacity_J_cm2"] = round(cap, 0)
        out["failure"] = None if raw.get("failure") is None else f"the sample failed at {q / 1e6:.2f} MW/m^2"
        out["summary"] = (f"A sample of {spec.get('_element')}'s TPS layer {'survived' if raw.get('failure') is None else 'failed at'} "
                          f"{q / 1e6:.2f} MW/m^2. The layer absorbs about {cap:,.0f} J/cm^2 before its bond line gets too hot.")
    elif kind == "parachute_drop":
        f = meas.get("cd_factor", 1.0)
        table = D.PARACHUTES[spec.get("type", "dgb")]["cd"]
        machs = [0.3, 0.6, 1.0, 1.5, 2.0] if spec.get("type", "dgb") == "dgb" else [0.3, 0.6]
        m["drag_coefficient"] = {str(M): round(SIM.interp(table, M) * f, 4) for M in machs}
        m["reference_area_m2"] = round(math.pi * spec["diameter_m"] ** 2 / 4, 2)
        s = raw.get("summary", "").split(" Drag is")[0]
        out["summary"] = s + f" Drag coefficient {m['drag_coefficient'][str(machs[1])]:.3f} at Mach {machs[1]}."
    elif kind == "isru_demo":
        kg = D.ISRU_KG_PER_SOL_PER_KW * spec.get("_kW", 0.0) * meas.get("production_factor", 1.0)
        m["oxygen_kg_per_sol"] = round(kg, 2)
        out["summary"] = f"The ISRU plant made {kg:.2f} kg of oxygen per sol at its rated power."
    elif canonical(kind) == "weighing":
        m = {k: meas[k] for k in ("dry_mass_kg", "scale_sd_pct") if k in meas}
        out["summary"] = raw.get("summary")
    elif kind == "vibration_test":
        # Margins measured against the team's own qualification level, up to the level tested; the summary names
        # only the test levels and the qualification levels.
        m = {k: meas[k] for k in ("acoustic_margin_dB", "shock_margin_dB") if k in meas}
        out["summary"] = raw.get("summary")
    else:
        out["summary"] = raw.get("summary")
    out["measured"] = m
    if out.get("failure"):
        out["failure"] = team_message(out["failure"]) if kind not in ("structural_qualification", "arcjet", "vibration_test") else out["failure"]
    return out


# ---- the catalog a team reads

TEAM_TESTS = {
    SIM_KIND: {
        "facility": SIM_FACILITY, "target": "\"vehicle\" (the whole design and flight/ at the ref you order from)",
        "params": {"world": "\"random\" (default): fresh dispersed worlds; \"nominal\": the one world with no "
                            "dispersions; or a seed, or a list of seeds, from an earlier result to fly those worlds again",
                   "worlds": f"with world \"random\", how many worlds to fly, 1 to {SIM_MAX_WORLDS} (default 1)"},
        "reveals": f"each world flown end to end on the program office's 6-DOF simulator, as the verifier flies it, "
                   f"with your committed flight/: the milestones reached and when, the failure that ended the flight "
                   f"and when, peak loads and heating, landing errors, guidance calls and wall time, and the flight's "
                   f"events. It flies the design as specified (not the as-built hardware your other tests measure), "
                   f"with the tank qualifications you have earned. ${SIM_USD_PER_WORLD / 1e6:.1f}M per world; up to "
                   f"{SIM_MAX_WALL_S / 60:.0f} minutes per world; {SIM_SLOTS} simulations run at once."},
    "engine_hotfire": {
        "facility": "engine_stand", "target": "an engine part, e.g. \"vehicle.S1.mainEngines\"",
        "params": {"starts": "1 to 12 starts", "burn_s": "seconds per start, 1 to 900"},
        "reveals": "the engine's measured vacuum Isp and thrust, its measured cooling margin (burns over 20 s), any "
                   "combustion instability, and failures and anomalies at the design's real reliability. Every start "
                   "and second counts toward the design's reliability."},
    "stage_static_fire": {
        "facility": "stage_stand", "target": "a stage with engines, e.g. \"vehicle.S1\"",
        "params": {"burn_s": "seconds, 5 to 300"},
        "reveals": "integration problems; lowers the stage's first-ignition failure chance from 2% to 0.4% after one "
                   "test and 0.2% after two (times the stage's hidden reliability factor). Counts as a start and "
                   "burn of every engine on it."},
    "structural_qualification": {
        "facility": "structures_lab", "target": "a stage with tanks, e.g. \"vehicle.S2\"", "params": {},
        "reveals": "a proof test at 1.25 x MEOP and an axial compression test of a tank article: pass or the burst "
                   "pressure, the measured weld efficiency, and the measured buckling load over the SP-8007 design "
                   "value. A pass qualifies this tank design, at the size tested, for the factor of safety 1.25 instead of 1.6."},
    "wind_tunnel": {
        "facility": "wind_tunnel", "target": "\"vehicle\" for the launch stack, or a heatshield or capsule part",
        "params": {},
        "reveals": "the launch stack's axial force coefficient, or a shell's drag coefficient and its lift-to-drag at "
                   "trim, from Mach 0.6 to 10, to about 1%."},
    "arcjet": {
        "facility": "arcjet", "target": "an element with a ThermalProtection part, e.g. \"vehicle.HS\"",
        "params": {"heat_flux_W_m2": "test condition, 1e5 to 4e8", "duration_s": "5 to 600"},
        "reveals": "whether the TPS material survives that heat flux, and how much heat load (J/cm^2) the layer as "
                   "designed absorbs, to about 3%."},
    "parachute_drop": {
        "facility": "drop_range", "target": "a parachute part, e.g. \"vehicle.CHUTE\"", "params": {},
        "reveals": "one deployment at the canopy's design conditions: success or failure, anomalies, and the measured "
                   "drag coefficient. Each drop multiplies the deployment failure chance by 0.65 (floor 0.3%, times the "
                   "parachute's hidden reliability factor). A dgb drop is a supersonic rocket-boosted test."},
    "isru_demo": {
        "facility": "thermal_vacuum", "target": "the element with the IsruOxygenPlant", "params": {},
        "reveals": "the plant's measured oxygen output per sol at its rated power."},
    "vibration_test": {
        "facility": "vibration_lab", "target": "a payload element, e.g. \"vehicle.DL\"",
        "params": {"acoustic_dB": "overall sound pressure level of a 2 minute reverberant chamber test, 120 to 165",
                   "shock_g": "peak shock response spectrum of a pyroshock test, 100 to 30000"},
        "reveals": "whether a unit built to the element's QualificationLevels survives that acoustic level and that "
                   "shock. A unit survives its qualification levels and some margin above them that only tests show."},
    "weighing": {
        "facility": MASS_LAB, "target": "a stack element, e.g. \"vehicle.MAV2\"", "params": {},
        "reveals": "the element's measured dry mass, from a built article on a scale accurate to 0.1%. "
                   "The old name mass_properties still orders this test."},
}


# ---- mission simulation

def _git(repo, *args):
    return subprocess.run(["git", "-c", "safe.directory=*", "--git-dir", repo, *args], capture_output=True)


def _plan(params) -> list:
    """The worlds a simulation flies, from params.world: "random" (the default) draws params.worlds fresh dispersed
    worlds (their seeds come later, from the secret and the test id: None here); "nominal" flies the one world with
    no dispersions; a seed, or a list of up to SIM_MAX_WORLDS seeds from earlier results, flies those worlds again."""
    params = params or {}
    world = params.get("world", "random")
    if world == "nominal":
        return ["nominal"]
    if world == "random":
        try:
            n = int(params.get("worlds", 1))
        except (TypeError, ValueError):
            n = 0
        if not 1 <= n <= SIM_MAX_WORLDS:
            raise TeamError("params.worlds is a whole number of worlds to fly, 1 to %d" % SIM_MAX_WORLDS)
        return [None] * n
    seeds = world if isinstance(world, list) else [world]
    if not 1 <= len(seeds) <= SIM_MAX_WORLDS or not all(
            isinstance(x, int) and not isinstance(x, bool) and 0 <= x < 2**31 - 1 for x in seeds):
        raise TeamError('params.world is "random", "nominal", or a seed (or a list of up to %d seeds) from an '
                        "earlier simulation's results" % SIM_MAX_WORLDS)
    return list(seeds)


def _worlds(params) -> int:
    return len(_plan(params))


def _plan_words(params) -> str:
    plan = _plan(params)
    if plan == ["nominal"]:
        return "nominal world (no dispersions)"
    if all(x is None for x in plan):
        return "fresh dispersed world(s)"
    return "replayed world(s)"


def quote_sim(source: dict, params: dict) -> dict:
    """Price a mission simulation of the model and flight software at a ref, and pin the ref to its commit."""
    n = _worlds(params)
    repo = None
    if source.get("dir"):
        commit = None
    else:
        repo, ref = source.get("repo", "/srv/git/team.git"), str(source.get("ref") or "main")
        if ref.startswith("-") or any(c in ref for c in " \n\t;|&$`"):
            raise TeamError(f"ref {ref!r} is not a branch, tag or commit")
        p = _git(repo, "rev-parse", "--verify", ref + "^{commit}")
        if p.returncode != 0:
            raise TeamError(f"cannot find {ref} in the team repository")
        commit = p.stdout.decode().strip()
        if _git(repo, "cat-file", "-e", commit + ":flight/guidance.py").returncode != 0:
            raise TeamError(f"there is no flight/guidance.py at {ref}")
    design, _ = compiled(source)
    from marsflight.vehicle import build_vehicle
    v = build_vehicle(design)
    if v.errors:
        raise TeamError("the design does not pass the build review: " + "; ".join(team_messages(v.errors)[:3]))
    key = "sim:" + hashlib.sha256(json.dumps({"design": design, "commit": commit}, sort_keys=True,
                                             default=str).encode()).hexdigest()[:14]
    return {"kind": SIM_KIND, "key": key, "facility": SIM_FACILITY, "cost_usd": SIM_USD_PER_WORLD * n,
            "minutes": round(SIM_MAX_WALL_S / 60.0 * n, 1),
            "note": f"flies {n} {_plan_words(params)} of this design and flight/ on the program office's simulator, "
                    f"up to {SIM_MAX_WALL_S / 60:.0f} minutes each",
            "spec": {"design": design, "commit": commit, "repo": repo, "source": source if commit is None else None,
                     "key": key}}


def _sim_user() -> str | None:
    """The unprivileged user flight software runs as during a simulation, made if missing. None when the office
    cannot switch users (not root): then no simulation runs, since the flight software could read the secret."""
    if os.geteuid() != 0:
        return None
    import pwd
    try:
        pwd.getpwnam(SIM_USER)
    except KeyError:
        subprocess.run(["useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin", SIM_USER],
                       capture_output=True)
        try:
            pwd.getpwnam(SIM_USER)
        except KeyError:
            return None
    return SIM_USER


def _close_to_flight_software(secret_file=None):
    """The simulator and compiler packages, and the program office's state, readable by root only, as the verifier
    closes them (tests/verify.py hide_simulator): the flight software must not read them."""
    import designlang
    import marsflight
    for mod in (marsflight, designlang):
        root = os.path.dirname(os.path.abspath(list(mod.__path__)[0]))
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass
    if secret_file:
        try:
            os.chmod(os.path.dirname(os.path.abspath(secret_file)), 0o700)
        except OSError:
            pass


def _event_view(e: dict, t0: float) -> dict:
    out = {"met_s": round(float(e.get("t", t0)) - t0, 1), "type": e.get("type")}
    for k, v in e.items():
        if k in ("t", "type") or isinstance(v, (dict, list)):
            continue
        out[k] = round(v, 3) if isinstance(v, float) else v
    return out


def run_sim(spec, params, test_id, ledger, secret, secret_file=None) -> dict:
    """Fly the pinned design and flight/ through dispersed worlds. Returns the ledger record."""
    from marsflight import program as P
    from marsflight.scoring import fly_one
    n = _worlds(params)
    user = _sim_user()
    if user is None:
        raise TeamError("the mission simulator is not available on this hub (it needs a separate flight user)")
    _close_to_flight_software(secret_file)
    os.environ["MARSFLIGHT_DYNAMICS"] = "6dof"   # as the verifier flies
    work = tempfile.mkdtemp(prefix="office-sim-")
    try:
        os.chmod(work, 0o755)
        if spec.get("commit"):
            repo = (spec.get("repo") or "/srv/git/team.git")
            p = _git(repo, "archive", "--format=tar", spec["commit"], "flight/")
            if p.returncode != 0:
                raise TeamError("cannot read flight/ at the commit this simulation was ordered for")
            subprocess.run(["tar", "-x", "-C", work], input=p.stdout, check=True)
        else:
            shutil.copytree(os.path.join(spec["source"]["dir"], "flight"), os.path.join(work, "flight"))
        subprocess.run(["chmod", "-R", "a+rX", work], check=True)
        design_path = os.path.join(work, "vehicle.json")
        with open(design_path, "w") as f:
            json.dump(spec["design"], f)
        os.chmod(design_path, 0o644)
        record = P.record_from_ledger(ledger or [])
        qualified = P.qualified_stages(spec["design"], record)
        # No hidden as-built truth, but the failure tests the team has done count as in grading: without them
        # the nominal world, whose fixed draw falls under the heritage overflow's prior, carried that flaw every time.
        truth = {"failure_record": record["failure_record"]} if record.get("failure_record") else None
        rng = P._rng(secret, "mission_sim:" + str(test_id))
        worlds = []
        for i, chosen in enumerate(_plan(params)):
            if chosen == "nominal":
                seed = None    # the world with no dispersions
            elif chosen is None:
                seed = int(rng.integers(0, 2**31 - 1))
            else:
                seed = int(chosen)
            r = fly_one(design_path, os.path.join(work, "flight", "guidance.py"), seed, user=user,
                        max_wall_s=SIM_MAX_WALL_S, keep_events=True, truth=truth, qualified=qualified)
            t0 = float(r.get("t0") or 0.0)
            ms = {k: round(float(v) - t0, 1) for k, v in sorted((r.get("milestones") or {}).items(), key=lambda kv: kv[1] or 0)}
            events = [_event_view(e, t0) for e in (r.get("events") or [])]
            worlds.append({"world": i + 1, "seed": seed, "nominal": seed is None,
                           "milestones_met_s": ms, "failure": r.get("failure"),
                           "failure_met_s": round(float(r["failure_t"]) - t0, 1) if r.get("failure_t") else None,
                           "end_met_s": round(float(r.get("end_t") or t0) - t0, 1),
                           "landing_errors_km": r.get("landing_errors_km"), "peaks": r.get("peaks"),
                           "guidance_calls": r.get("guidance_calls"), "wall_s": r.get("wall_s"),
                           "events": events[:SIM_EVENTS], "events_dropped": max(0, len(events) - SIM_EVENTS)})
        reached = [len(w["milestones_met_s"]) for w in worlds]
        return {"kind": SIM_KIND, "key": spec.get("key"), "worlds": worlds, "qualified_tank_stages": qualified,
                "failure": None, "engine_exposure": {},
                "summary": f"{n} world(s) flown; milestones reached {reached}; "
                           f"{sum(1 for w in worlds if not w['failure'])} without a failure."}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# The office's working folders for a simulation, as they appear in a flight software traceback.
_SIM_TMP = re.compile(r"/tmp/(?:flight|office-sim)-[^/\s\"']+/")
# The last frame of a traceback with the indented source and marker lines under it: the exception's type follows.
_LAST_FRAME = re.compile(r'File "[^"]+", line \d+, in \S+\n(?:[ \t]+[^\n]*\n)*')
_BUILTIN_ERRORS = {n for n, v in vars(__import__("builtins")).items()
                   if isinstance(v, type) and issubclass(v, BaseException)}


def sim_failure(text: str) -> str:
    """The failure that ended a simulated flight, as the team reads it. The simulator's own failure texts come in full:
    the design flies as specified (no hidden as-built truth), so their numbers tell the team only about its own design.
    Nothing the flight software chose at run time comes back: its reset info and observations hold the vehicle's mass
    and performance, which tests measure, and an exception's message, a made-up exception class, file or line number,
    a stray output line or stderr would carry them out. For an exception the team reads only its built-in type (or "an
    exception"): flight software that raised on purpose at a chosen line of a chosen generated module sent numbers
    out through the file and line. For a malformed command, which field."""
    text = _SIM_TMP.sub("", str(text))
    m = re.match(r"flight software command error: ([a-z_]+)", text)
    if m:     # the check quotes the bad value back, and an engine's key as sent
        return f"flight software command error: the command's {m.group(1)} is not valid (the office does not return the value)"
    m = re.match(r"simulator error: (\w+)", text)
    if m:     # its message may quote what the flight software sent
        kind = m.group(1) if m.group(1) in _BUILTIN_ERRORS else "an error"
        return f"simulator error: {kind} (the office does not return its message)"
    head = "flight software error: "
    if not text.startswith(head):
        return text
    body = text[len(head):]
    if body.startswith("flight software raised:"):
        tail = _LAST_FRAME.split(body)[-1] if _LAST_FRAME.search(body) else ""
        kind = re.match(r"\s*([A-Za-z_]\w*)(?::|\s*$)", tail)
        kind = kind.group(1) if kind and kind.group(1) in _BUILTIN_ERRORS else "an exception"
        return head + f"flight software raised {kind} (the office does not return its message or where it was raised)"
    if body.startswith("flight software wrote a non-JSON line"):
        return head + "flight software wrote a line that is not JSON (the office does not return it)"
    if body.startswith("flight software exited"):
        return head + "flight software exited (the office does not return its output)"
    return text


def sim_team_view(raw: dict) -> dict:
    """What the team reads: the flights, with the failure that ended each one (see sim_failure)."""
    out = {k: v for k, v in raw.items() if k not in ("engine_exposure", "flight_lines")}   # flight_lines: older results
    out["worlds"] = []
    for w in raw.get("worlds") or []:
        w = dict(w)
        if w.get("failure"):
            w["failure"] = sim_failure(w["failure"])
        w["events"] = [{**e, "reason": sim_failure(e["reason"])} if "reason" in e else e
                       for e in w.get("events") or []]
        out["worlds"].append(w)
    return out


def catalog() -> dict:
    from marsflight import program as P
    from marsflight.economics import CATALOG_ENGINES
    c = P.catalog()
    c["tests"] = TEAM_TESTS
    c["facilities"] = {**c["facilities"], MASS_LAB: MASS_LAB,
                       **{f"{SIM_FACILITY}_{i}": SIM_FACILITY for i in range(1, SIM_SLOTS + 1)}}
    c["catalog_engines"] = {k: {"unit_usd": v["unit_usd"], "note": v["note"], "thrust_vac_kN": v["thrust_vac_kN"],
                                "isp_vac_s": v["isp_vac_s"], "mass_kg": v["mass_kg"],
                                "propellants": f"{v['spec']['fuel']}/{v['spec']['oxidizer']}", "cycle": v["spec"]["cycle"],
                                "chamber_pressure_bar": v["spec"]["pc_bar"], "mixture_ratio": v["spec"]["mixture_ratio"],
                                "area_ratio": v["spec"]["expansion_ratio"], "gimbal_deg": v["spec"]["gimbal_deg"]}
                            for k, v in CATALOG_ENGINES.items()}
    return c


def cost(source: dict) -> dict:
    from marsflight.economics import cost_report
    from marsflight.vehicle import build_vehicle
    design, _ = compiled(source)
    v = build_vehicle(design)
    return {**team_cost(cost_report(v, design)), "build_errors": team_messages(v.errors)}


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    try:
        if cmd == "catalog":
            print(json.dumps(catalog()))
            return 0
        req = json.loads(sys.stdin.read() or "{}")
        if cmd == "review":
            out = review(req["source"])
        elif cmd == "quote" and req.get("kind") == SIM_KIND:
            out = quote_sim(req["source"], req.get("params") or {})
        elif cmd == "quote":
            design, desc = compiled(req["source"])
            spec = resolve(req["kind"], req.get("target"), design, desc)
            from marsflight.vehicle import build_vehicle
            v = build_vehicle(design)
            if v.errors and req["kind"] != "arcjet":
                raise TeamError("the design does not pass the build review: " + "; ".join(team_messages(v.errors)[:3]))
            try:
                out = {**quote(req["kind"], spec, req.get("params") or {}), "spec": spec}
            except ValueError as ex:
                raise TeamError(team_message(str(ex)))
        elif cmd == "run":
            with open(req["secret_file"]) as f:
                secret = json.load(f)["secret"]
            if req["kind"] == SIM_KIND:
                raw = run_sim(req["spec"], req.get("params") or {}, req["test_id"], req.get("ledger") or [], secret,
                              secret_file=req["secret_file"])
                out = {"raw": raw, "team": sim_team_view(raw)}
            else:
                out = run(req["kind"], req["spec"], req.get("params") or {}, req["test_id"], req.get("ledger") or [], secret)
        elif cmd == "cost":
            out = cost(req["source"])
        else:
            print(json.dumps({"error": f"unknown command {cmd!r}"}))
            return 2
        print(json.dumps(out, default=float))
    except TeamError as ex:
        print(json.dumps({"error": str(ex)}))
        return 1
    except Exception as ex:  # noqa: BLE001
        print(json.dumps({"error": team_message(f"{type(ex).__name__}: {ex}")}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
