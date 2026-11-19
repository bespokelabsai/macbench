"""Score a design + flight software over Monte Carlo flights.

Each flight earns dense, continuous credit. The weights of the milestones it reaches count in full,
and the next milestone counts in part, by a physical measure of how close the flight got (orbital
energy and periapsis on ascent, closest approach on the way to a planet, the share of entry energy
removed intact, touchdown speed against the limit, sols of surface work). See sim.Mission._track.

Each credit is then weighted by the chance that the hardware survived to that moment: the product,
over every engine start, second of burning, first stage ignition, parachute deployment and separation
the flight used up to then, of one minus its failure chance (program.hazards). Testing lowers those
chances. This is the expected progress of the flight.

    task score = 0.85 * mean expected progress
               + 0.15 * mean(chance of a full success) * program cost efficiency

A full success lands the capsule in Utah. Cost efficiency is the share of the program budget
(hardware plus R&D) left unspent. A design over the hardware budget cannot be built and scores 0.
"""
from __future__ import annotations

import json
import math
import os
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool

from . import data as D
from .sim import MILESTONES, TARGET_GATES, TARGET_SCALE_KM, clamp01, target_credit

W = {k: w for k, w, _ in MILESTONES}
# One simulator worker at its peak: 0.27 to 0.30 GB for the reference, the oracle and the three v2 designs, and
# a flat 0.26 GB through 7 million calls in 1,500 s of flight software looping on the pad.
SIM_BYTES = 0.4e9
RESERVE_BYTES = 0.5e9   # the verifier's own process and the page cache


def usable_cpus() -> int:
    """CPUs this process may use. Inside a container os.cpu_count() gives the host's CPUs, so read the cgroup
    CPU quota (v2 cpu.max, else v1 cfs quota), else the affinity mask."""
    try:
        quota, period = open("/sys/fs/cgroup/cpu.max").read().split()[:2]
        if quota != "max":
            return max(1, int(int(quota) / int(period)))
    except (OSError, ValueError):
        pass
    try:
        quota = int(open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read())
        period = int(open("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read())
        if quota > 0:
            return max(1, quota // period)
    except (OSError, ValueError):
        pass
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


def usable_memory() -> float | None:
    """The cgroup memory limit in bytes, or None when there is none."""
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            v = open(path).read().strip()
            if v != "max" and int(v) < 1 << 60:
                return float(v)
        except (OSError, ValueError):
            pass
    return None


def oom_kills() -> int | None:
    """How many processes the out-of-memory killer has killed in this cgroup (v2 memory.events, else v1
    memory.oom_control), or None when the kernel does not say. The flight software can't write it."""
    for path in ("/sys/fs/cgroup/memory.events", "/sys/fs/cgroup/memory/memory.oom_control"):
        try:
            with open(path) as f:
                for line in f:
                    k, _, v = line.partition(" ")
                    if k == "oom_kill":
                        return int(v)
        except (OSError, ValueError):
            pass
    return None


def default_workers(n: int) -> int:
    """How many flights to fly at once: no more than the CPUs, and few enough to fit in memory even with every
    flight software at its data limit (guidance_io.FS_DATA_BYTES): 3 in the verifier's 8 GB. The limit is per
    process, so flight software that starts more processes can still fill memory; the kernel then kills a process,
    and tests/verify.py keeps that from giving any flight a second try."""
    from .guidance_io import FS_DATA_BYTES
    w = min(n, usable_cpus())
    mem = usable_memory()
    if mem:
        w = min(w, max(1, int((mem - RESERVE_BYTES) // (FS_DATA_BYTES + SIM_BYTES))))
    return max(1, w)


def fly_one(design_path, guidance_path, seed, user=None, max_wall_s=3600.0, keep_events=True, truth=None,
            qualified=None, deadline=None, engine_out=None, failure_check=None, live=None):
    """Run one flight in this process. Returns the result dict (never raises). deadline: a time.time() by which
    the flight must end; a flight that starts late gets only the time left (at least 1 s).
    killed_by_kernel and flight_software_exit are diagnostics only: whether the flight software died from SIGKILL
    while the out-of-memory killer killed a process in this cgroup, and its exit status. A flight whose flight
    software dies is that flight's failure and is never flown again. engine_out: an engine-out case to fly
    (grading.engine_out_cases); failure_check: a failure check to fly (grading.failure_check_cases).
    live: {"dir", "kind", "index", ...} writes the flight's live progress there (marsflight.live), or None."""
    if deadline is not None:
        max_wall_s = max(1.0, min(max_wall_s, deadline - time.time()))
    log = None
    if live and live.get("dir"):
        from .live import LiveLog
        log = LiveLog(dict(live, engine_out=engine_out, failure_check=failure_check, max_wall_s=round(max_wall_s, 1)),
                      seed=seed)
    res = _fly_one(design_path, guidance_path, seed, user, max_wall_s, keep_events, truth, qualified, engine_out,
                   failure_check, log)
    if log is not None:
        log.finish(res)
    return res


def _fly_one(design_path, guidance_path, seed, user, max_wall_s, keep_events, truth, qualified, engine_out,
             failure_check, log):
    from .guidance_io import GuidanceHost
    from .sim import FlightFailure, Mission
    from .vehicle import build_vehicle
    try:
        with open(design_path) as f:
            design = json.load(f)
    except Exception as ex:  # noqa: BLE001
        return {"seed": seed, "progress": 0.0, "milestones": {}, "failure": f"cannot read design: {ex}",
                "liftoff_mass_kg": 0.0, "exposures": []}
    vehicle = build_vehicle(design, qualified=qualified)
    g = None
    oom0 = oom_kills()
    try:
        g = GuidanceHost(guidance_path, user=user)
        m = Mission(vehicle, design, g, seed=seed, max_wall_s=max_wall_s, truth=truth, engine_out=engine_out,
                    failure_check=failure_check, live=log)
        res = m.run()
        if log is not None:
            log.sample(m, force=True)
        res["trajectory_points"] = len(m.traj)
        step = max(1, len(m.traj) // 1500)
        res["trajectory"] = [[round(float(x), 3) if isinstance(x, float) else x for x in row] for row in m.traj[::step]]
    except FlightFailure as ex:
        res = {"seed": seed, "progress": 0.0 if vehicle.errors else 0.02, "milestones": {}, "failure": str(ex),
               "liftoff_mass_kg": vehicle.liftoff_mass, "exposures": []}
    except Exception as ex:  # noqa: BLE001
        res = {"seed": seed, "progress": 0.0, "milestones": {}, "failure": f"simulator error: {ex}",
               "trace": traceback.format_exc()[-2000:], "liftoff_mass_kg": vehicle.liftoff_mass, "exposures": []}
    finally:
        if g is not None:
            g.close()
    oom1 = oom_kills()
    res["killed_by_kernel"] = bool(g is not None and g.sigkilled and oom0 is not None and oom1 is not None
                                   and oom1 > oom0)
    res["flight_software_exit"] = g.exit_code if g is not None else None
    res["max_wall_s"] = round(max_wall_s, 1)
    from .grading import landing_errors
    res["landing_errors_km"] = landing_errors(res.get("events"))   # the grade needs them after events are dropped
    if not keep_events:
        res.pop("events", None)
    return res


def _fly_star(a):
    return fly_one(*a)


def earth_miss_km(res: dict):
    """How far from the Utah target the capsule came down, or None."""
    e = (res.get("landing_errors_km") or {}).get("Earth")
    if e is not None:
        return e
    km = [ev.get("km") for ev in res.get("events") or [] if ev.get("type") == "landing_error_km"]
    if "earth_touchdown" in (res.get("milestones") or {}) and km:
        return km[-1]          # the Mars landing's comes first
    km = res.get("landing_error_km") or []
    return km[-1] if "earth_touchdown" in (res.get("milestones") or {}) and len(km) >= 2 else None


def expected_progress(res: dict, hz: dict | None) -> dict:
    """Milestone and partial credit of one flight, each weighted by hardware survival to its time. A capsule that
    lands intact outside the Utah range earns half of earth_touchdown scaled by range / miss, no earth_target and no
    full success: the flight lands where it lands, and only the score judges the range. Each landing target
    earns target_credit of its weight from the landing's distance to the site (core.common)."""
    from .program import survival
    ms = res.get("milestones") or {}
    partial = res.get("partial") or {}
    pt = res.get("partial_t") or {}
    miss, rng = earth_miss_km(res), D.SITES["earth_landing"]["range_km"]
    if "earth_touchdown" in ms and miss is not None and miss > rng:
        t_down = ms["earth_touchdown"]
        ms = {k: v for k, v in ms.items() if k not in ("earth_touchdown", "earth_target")}
        partial = dict(partial, earth_touchdown=0.5 * rng / miss)
        pt = dict(pt, earth_touchdown=t_down)
    # Landing targets earn target_credit(distance to the site) of their weight, at the touchdown's survival. A result
    # without the landing's distance (older saved flights) keeps the milestone and partial credit it has.
    errs = res.get("landing_errors_km") or {}
    for k, (gate, body) in TARGET_GATES.items():
        if gate in ms and errs.get(body) is not None:
            ms = {m: v for m, v in ms.items() if m != k}
            partial = dict(partial, **{k: target_credit(errs[body], TARGET_SCALE_KM[k])})
            pt = dict(pt, **{k: ms[gate]})
    ex = res.get("exposures") or []
    end_t = res.get("end_t") or 0.0
    rows, total, raw = [], 0.0, 0.0
    for k, w, optional in MILESTONES:
        if k in ms:
            r = survival(ex, hz, ms[k]) if hz else 1.0
            rows.append({"milestone": k, "weight": w, "reached": True, "fraction": 1.0, "survival": round(r, 6)})
            total += w * r
            raw += w
            continue
        f = float(partial.get(k, 0.0))
        if f > 0:
            r = survival(ex, hz, pt.get(k, end_t)) if hz else 1.0
            rows.append({"milestone": k, "weight": w, "reached": False, "fraction": round(f, 6), "survival": round(r, 6)})
            total += w * f * r
            raw += w * f
        if not optional:
            break
    full = "earth_touchdown" in ms
    p_full = (survival(ex, hz, ms["earth_touchdown"]) if hz else 1.0) if full else 0.0
    return {"expected_progress": round(total, 6), "physical_progress": round(raw, 6), "full_success": full,
            "p_full_success": round(p_full, 6), "credit": rows}


def score_flights(results, hz=None, cost=None):
    """cost: {'hardware_usd', 'rnd_spent_usd', 'hardware_budget_usd', 'rnd_budget_usd'} or None."""
    n = len(results)
    if n == 0:
        return {"score": 0.0}
    per = [expected_progress(r, hz) for r in results]
    for r, p in zip(results, per):
        r.update(p)
    mean_e = sum(p["expected_progress"] for p in per) / n
    mean_phys = sum(p["physical_progress"] for p in per) / n
    p_full = sum(p["p_full_success"] for p in per) / n
    eff = 0.0
    over = False
    if cost:
        budget = cost["hardware_budget_usd"] + cost["rnd_budget_usd"]
        spent = cost["hardware_usd"] + cost.get("rnd_spent_usd", 0.0)
        if math.isfinite(budget) and math.isfinite(spent):
            eff = clamp01(1.0 - spent / budget)
            over = cost["hardware_usd"] > cost["hardware_budget_usd"]
        else:   # a non-finite cost can't be checked against the budget: the design scores 0
            over = True
    score = 0.0 if over else 0.85 * mean_e + 0.15 * p_full * eff
    reached = {k: sum(1 for r in results if k in (r.get("milestones") or {})) / n for k, _, _ in MILESTONES}
    return {"score": round(score, 6), "mean_expected_progress": round(mean_e, 6),
            "mean_physical_progress": round(mean_phys, 6), "mean_p_full_success": round(p_full, 6),
            "cost_efficiency": round(eff, 4), "over_hardware_budget": over,
            "liftoff_mass_kg": results[0].get("liftoff_mass_kg") or 0.0, "milestone_rates": reached,
            "failures": [r.get("failure") for r in results]}


class PoolBroken(BrokenProcessPool):
    """A Monte Carlo worker died (the kernel killed a simulator process). results: every flight's result, None for
    the flights the broken pool lost."""

    def __init__(self, results):
        super().__init__(f"a Monte Carlo worker died: {sum(r is None for r in results)} of {len(results)} flights lost")
        self.results = results


def monte_carlo(design_path, guidance_path, seeds, workers=None, user=None, max_wall_s=3600.0, truth=None,
                qualified=None, hz=None, cost=None, keep=False, deadline=None, engine_outs=None, failure_checks=None,
                live_dir=None, live_kind="world"):
    """Raises PoolBroken, with the flights that finished, when a worker dies. engine_outs, failure_checks: one
    engine-out case or failure check (or None) per seed. live_dir: write each flight's live progress there as
    <live_kind>-<index>.jsonl (marsflight.live); None reads MARSFLIGHT_LIVE_DIR, unset or empty is off."""
    from .live import live_dir as _live_dir
    workers = workers or default_workers(len(seeds))
    eos = engine_outs or [None] * len(seeds)
    fcs = failure_checks or [None] * len(seeds)
    ld = live_dir if live_dir is not None else _live_dir()
    args = [(design_path, guidance_path, s, user, max_wall_s, keep, truth, qualified, deadline, eo, fc,
             {"dir": ld, "kind": live_kind, "index": i} if ld else None)
            for i, (s, eo, fc) in enumerate(zip(seeds, eos, fcs))]
    if workers == 1:
        results = [_fly_star(a) for a in args]
    else:
        results = [None] * len(args)
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_fly_star, a): i for i, a in enumerate(args)}
            for f in as_completed(futures):
                try:
                    results[futures[f]] = f.result()
                except BrokenProcessPool:
                    pass
        if any(r is None for r in results):
            raise PoolBroken(results)
    return results, score_flights(results, hz, cost)


def local_program(design: dict, path="program/test_results.json"):
    """Truth, qualified stages and hazards for local runs from the team's saved test results."""
    from . import program as P
    results = []
    if path and os.path.isfile(path):
        try:
            with open(path) as f:
                results = json.load(f)
            if isinstance(results, dict):
                results = results.get("results", [])
        except Exception:  # noqa: BLE001
            results = []
    truth, qual = P.local_truth(design, results)
    ledger = [{"status": "done", "kind": r.get("kind"), "key": r.get("key"), "outcome": r}
              for r in results if isinstance(r, dict)]
    rec = P.record_from_ledger(ledger)
    # The real reliability factors are hidden: local runs fly the estimates the saved results support.
    return truth, qual, P.hazards(design, rec, P.estimated_factors(design, rec)), len(results)
