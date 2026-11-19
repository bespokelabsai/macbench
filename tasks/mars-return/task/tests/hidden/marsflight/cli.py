"""marsflight command line.

  marsflight check   [design.json]                  validate the design: masses, engines, delta-v, cost
  marsflight cost    [design.json]                  the bill of materials against the hardware budget
  marsflight engine  [design.json] NAME             one engine's performance and limits
  marsflight keys    [design.json]                  the part keys that test results are matched by
  marsflight tests                                  the R&D test kinds, facilities and catalog engines
  marsflight fly     [--seed N] [--out DIR] [--scenario FILE] [--save-states DIR]
                                                    fly once (nominal world unless --seed)
  marsflight mc      [--n 8] [--first-seed 1000]    Monte Carlo over dispersed worlds, with the task score

fly and mc take --dynamics 6dof (the default, as the verifier flies: rigid-body attitude dynamics and stage
separation tracking, see sixdof.py) or 3dof (a slew-limited attitude). Without the flag they use
MARSFLIGHT_DYNAMICS if it is set, else 6dof, and set MARSFLIGHT_DYNAMICS for the run.

Defaults: design/vehicle.json and flight/guidance.py in the current directory. Local runs use the
measured values and qualifications in program/test_results.json when it exists (--tests FILE to use
another file, --tests none to ignore it). The verifier uses the program office's ledger instead.

fly and mc run the flight software the way the verifier does: from a copy of its folder (flight/) in an
empty temporary folder, next to a copy of the design named vehicle.json. The flight software cannot read
the rest of the repository, so data it needs, such as measured drag factors, belongs inside flight/.
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _paths(a):
    return a.design or "design/vehicle.json", a.guidance or "flight/guidance.py"


def _boxed(dpath, gpath):
    """The verifier's layout (tests/verify.py): a copy of the flight software's folder and of the design in an
    empty temporary folder. Returns the guidance path inside it."""
    import atexit
    import shutil
    import tempfile
    src = os.path.dirname(os.path.abspath(gpath))
    box = tempfile.mkdtemp(prefix="marsflight-box-")
    atexit.register(shutil.rmtree, box, True)
    dst = os.path.join(box, os.path.basename(src) or "flight")
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(dpath, os.path.join(box, "vehicle.json"))
    return os.path.join(dst, os.path.basename(gpath))


def _local(design, a):
    from .scoring import local_program
    path = getattr(a, "tests", None) or "program/test_results.json"
    if path == "none":
        path = None
    return local_program(design, path)


def main(argv=None):
    p = argparse.ArgumentParser(prog="marsflight")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("check", "cost", "keys"):
        c = sub.add_parser(name)
        c.add_argument("design", nargs="?")
        c.add_argument("--tests", default=None)
    e = sub.add_parser("engine")
    e.add_argument("design", nargs="?")
    e.add_argument("name")
    sub.add_parser("tests")
    f = sub.add_parser("fly")
    f.add_argument("--design")
    f.add_argument("--guidance")
    f.add_argument("--seed", type=int, default=None)
    f.add_argument("--out", default=None, help="write events.json, telemetry.jsonl, trajectory.csv, check.json and cost.json here")
    f.add_argument("--scenario", default=None, help="start from a saved state (development only)")
    f.add_argument("--save-states", default=None, help="write the state at each milestone as a scenario file here")
    f.add_argument("--max-wall", type=float, default=3600.0)
    f.add_argument("--tests", default=None)
    f.add_argument("--dynamics", choices=("3dof", "6dof"), default=None)
    f.add_argument("--engine-out", default=None, metavar="ELEMENT/ENGINE@SECONDS",
                   help="shut one of those engines down this long after the element first fires, as the verifier's engine-out checks do")
    m = sub.add_parser("mc")
    m.add_argument("--design")
    m.add_argument("--guidance")
    m.add_argument("--n", type=int, default=8)
    m.add_argument("--first-seed", type=int, default=1000)
    m.add_argument("--workers", type=int, default=None)
    m.add_argument("--max-wall", type=float, default=3600.0)
    m.add_argument("--tests", default=None)
    m.add_argument("--dynamics", choices=("3dof", "6dof"), default=None)
    m.add_argument("--engine-out", action="store_true", help="also fly the verifier's engine-out checks and count them")
    a = p.parse_args(argv)
    if a.cmd in ("fly", "mc"):
        # 6-DOF unless asked otherwise, as the verifier flies; the Monte Carlo workers read it too
        os.environ["MARSFLIGHT_DYNAMICS"] = a.dynamics or os.environ.get("MARSFLIGHT_DYNAMICS") or "6dof"

    if a.cmd == "tests":
        from .program import catalog
        print(json.dumps(catalog(), indent=1))
        return 0

    if a.cmd in ("check", "cost", "keys", "engine"):
        from .economics import cost_report
        from .program import component_keys
        from .vehicle import build_vehicle
        path = a.design or "design/vehicle.json"
        with open(path) as fh:
            design = json.load(fh)
        if a.cmd == "keys":
            print(json.dumps(component_keys(design), indent=1))
            return 0
        _, qual, _, n_results = _local(design, a) if a.cmd != "engine" else (None, [], None, 0)
        v = build_vehicle(design, qualified=qual)
        r = v.report()
        if a.cmd == "engine":
            print(json.dumps(r["engines"].get(a.name, {"error": f"no engine {a.name}"}), indent=1))
            return 0 if r["valid"] else 1
        cost = cost_report(v, design)
        if a.cmd == "cost":
            print(json.dumps(cost, indent=1))
            return 0 if not cost["over_budget"] else 1
        r["cost"] = {k: cost[k] for k in ("hardware_usd", "hardware_budget_usd", "over_budget", "by_element_usd", "engines")}
        r["tested_tank_designs"] = qual
        r["test_results_used"] = n_results
        if cost["over_budget"]:
            r["warnings"].append(f"hardware costs ${cost['hardware_usd'] / 1e6:.0f}M, over the ${cost['hardware_budget_usd'] / 1e6:.0f}M "
                                 "budget: the verifier scores a design that cannot be paid for as 0")
        print(json.dumps(r, indent=1))
        return 0 if r["valid"] else 1

    if a.cmd == "fly":
        from .economics import cost_report
        from .guidance_io import GuidanceHost
        from .sim import FlightFailure, Mission
        from .vehicle import build_vehicle
        dpath, gpath = _paths(a)
        with open(dpath) as fh:
            design = json.load(fh)
        truth, qual, hz, _ = _local(design, a)
        v = build_vehicle(design, qualified=qual)
        scenario = None
        if a.scenario:
            with open(a.scenario) as fh:
                scenario = json.load(fh)
        engine_out = None
        if a.engine_out:
            group, _, after = a.engine_out.partition("@")
            eid, _, engine = group.partition("/")
            engine_out = {"element": eid, "engine": engine, "after_ignition_s": float(after or 0.0)}
        try:
            ms = Mission(v, design, GuidanceHost(_boxed(dpath, gpath)), seed=a.seed, max_wall_s=a.max_wall, truth=truth,
                         scenario=scenario, telemetry=bool(a.out), save_states=a.save_states, engine_out=engine_out)
        except FlightFailure as ex:
            print(json.dumps({"failure": str(ex)}))
            return 1
        res = ms.run()
        from .scoring import expected_progress
        res.update(expected_progress(res, hz))
        if a.out:
            os.makedirs(a.out, exist_ok=True)
            with open(os.path.join(a.out, "events.json"), "w") as fh:
                json.dump({k: v_ for k, v_ in res.items() if k != "exposures"}, fh, indent=1, default=float)
            with open(os.path.join(a.out, "trajectory.csv"), "w") as fh:
                fh.write("met_s,central,x,y,z,vx,vy,vz,mass_kg\n")
                for row in ms.traj:
                    fh.write(",".join(str(x) for x in row) + "\n")
            with open(os.path.join(a.out, "telemetry.jsonl"), "w") as fh:
                for row in ms.telemetry or []:
                    fh.write(json.dumps(row, default=float) + "\n")
            with open(os.path.join(a.out, "check.json"), "w") as fh:
                json.dump(v.report(), fh, indent=1, default=float)
            with open(os.path.join(a.out, "cost.json"), "w") as fh:
                json.dump(cost_report(v, design), fh, indent=1, default=float)
            with open(os.path.join(a.out, "design.json"), "w") as fh:
                json.dump(design, fh, indent=1)
        res = dict(res)
        evs = res.pop("events")
        res.pop("exposures", None)
        res.pop("credit", None)
        print(json.dumps(res, indent=1, default=float))
        print("last events:")
        for ev in evs[-12:]:
            print("  ", json.dumps(ev, default=float))
        return 0

    if a.cmd == "mc":
        from .economics import cost_report
        from .grading import engine_out_cases, engine_out_kept, engine_out_survival
        from .scoring import monte_carlo, score_flights
        from .vehicle import build_vehicle
        dpath, gpath = _paths(a)
        with open(dpath) as fh:
            design = json.load(fh)
        truth, qual, hz, n_results = _local(design, a)
        cost = cost_report(build_vehicle(design, qualified=qual), design)
        cost_in = {"hardware_usd": cost["hardware_usd"], "hardware_budget_usd": cost["hardware_budget_usd"],
                   "rnd_budget_usd": cost["rnd_budget_usd"], "rnd_spent_usd": 0.0}
        seeds = list(range(a.first_seed, a.first_seed + a.n))
        workers = a.workers or int(os.environ.get("MARSFLIGHT_MC_WORKERS", "0") or 0) or None
        results, summary = monte_carlo(dpath, _boxed(dpath, gpath), seeds, workers=workers, max_wall_s=a.max_wall, truth=truth,
                                       qualified=qual, hz=hz, cost=cost_in)
        cases = engine_out_cases(design, results, hz) if a.engine_out else []
        if cases:
            eo_results, _ = monte_carlo(dpath, _boxed(dpath, gpath), [c[0] for c in cases], workers=workers, max_wall_s=a.max_wall,
                                        truth=truth, qualified=qual, engine_outs=[c[1] for c in cases])
            eo = engine_out_survival(cases, eo_results, results)
            hz = dict(hz, engine_out=eo)
            summary = score_flights(results, hz, cost_in)
            for c, r in zip(cases, eo_results):
                print(json.dumps({"engine_out_check": c[1], "seed": c[0], "weight": round(c[2], 4), "failure": r.get("failure"),
                                  "kept": round(engine_out_kept(results[seeds.index(c[0])], r), 4)}))
        from .grading import FORECAST_FILE, grade, load_json
        if not results:
            print(json.dumps({"score": 0.0, "worlds": 0, "note": "no worlds flown (--n must be at least 1)"}))
            return 0
        vehicle = build_vehicle(design, qualified=qual)
        containers = next((e.sample_containers for e in vehicle.elements if e.kind == "capsule"), 1)
        g = grade(results, hz, cost["hardware_usd"], load_json(FORECAST_FILE), containers)   # no tests ordered locally
        for r, v, o in zip(results, g.pop("world_values"), g.pop("world_outcomes")):
            r["grade_value"], r["grade_outcome"] = v, o
        for r in results:
            print(json.dumps({"seed": r["seed"], "grade_outcome": r["grade_outcome"], "grade_value": r["grade_value"],
                              "landing_errors_km": r.get("landing_errors_km"), "expected_progress": r.get("expected_progress"),
                              "physical_progress": r.get("physical_progress"), "failure": r.get("failure"),
                              "last_milestone": list((r.get("milestones") or {}).keys())[-1:],
                              "rare_draws": [k for k in ("arrival_storm", "high_winds") if (r.get("world") or {}).get(k)]}))
        summary = {"score": g["score"], "grade": g, **{("v2_score" if k == "score" else k): v for k, v in summary.items()}}
        summary["note"] = ("local estimate of the task score (docs/REFERENCE.md, Score): R&D spending is not counted, "
                           f"untested parts fly the design model ({n_results} saved test results used), and the "
                           "verifier also applies the correctness gate")
        summary["reliability_factors_used"] = {k: round(v, 4) for k, v in hz["factors"].items()}
        summary["reliability_note"] = ("estimates of each part's hidden reliability factor from the saved results "
                                       "(1 without evidence); the verifier uses the real factors")
        summary["engine_out_survival"] = hz.get("engine_out") or ("not checked (--engine-out flies the checks)" if not a.engine_out else {})
        print(json.dumps(summary, indent=1, default=float))
        return 0


if __name__ == "__main__":
    sys.exit(main())
