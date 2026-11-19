"""Program office tests that reveal the failure modes.

Each flown loss in data/failures/catalog.json was a flaw that a known test would have found. The test kinds:

| kind | finds | real lesson |
|---|---|---|
| ``avionics_burn_in`` | infant-mortality failures of an element's avionics units (Castet and Saleh shapes below 1); the units that fail are replaced and the flight units start older, at a lower hazard | burn-in and thermal cycling of flight boxes |
| ``phasing_test`` | a reversed rate sensor or swapped actuator cable on a stage | Proton-M 2013 (check dropped from the list in 2011), Vega VV17 2020 |
| ``trajectory_requalification`` | heritage inertial software whose ranges this vehicle's ascent exceeds | Ariane 5 501: the alignment code was never run on an Ariane 5 trajectory |
| ``part_lot_screening`` | a strut or rod end below its certified strength | CRS-7: SpaceX now tests every strut |
| ``centrifuge_test`` | an inverted deceleration switch in the capsule sequencer | Genesis: drawn inverted, built to the drawing, never spun |
| ``edl_hil_test`` | an IMU saturation handling error at parachute inflation | Schiaparelli: the saturation flag was believed to last 15 ms |
| ``nav_end_to_end`` | a units error between ground software and the flight's small-force model | Mars Climate Orbiter: lbf s read as N s |
| ``radiation_test`` | memory parts that heavy ions upset | Phobos-Grunt: non-space-qualified RAM chips |
| ``touchdown_sensor_test`` | the touchdown sensors' spurious signal at leg deployment | Mars Polar Lander |

A latent flaw is found with the test's coverage (flown.LATENT); each test done multiplies the flaw's remaining
chance by (1 - coverage). Outcomes are drawn from the hub secret and the test id, like program.run_test.
Costs and durations are game values, set near program.py's tests of similar size.

Proposed wiring (program/office.py): ``quote`` and ``run_test`` dispatch here for these kinds, and
``failure_record(ledger)`` goes into the flight context as ``ctx.record`` next to program.record_from_ledger.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import random

from . import flown as F
from . import rates as R

TEST_KINDS = {
    "avionics_burn_in": {"facility": "thermal_vacuum", "target": "an element with avionics: {'element': id}",
                         "params": {"hours": "100 to 2000 hours of powered thermal cycling"},
                         "cost_usd": 1.5e6, "minutes": 10.0},
    "phasing_test": {"facility": "stage_stand", "target": "a stage: {'element': id}", "params": {},
                     "cost_usd": 0.4e6, "minutes": 4.0, "mode": "reversed_polarity"},
    "trajectory_requalification": {"facility": "avionics_lab", "target": "the launch vehicle: {}", "params": {},
                                   "cost_usd": 1.0e6, "minutes": 6.0, "mode": "heritage_overflow"},
    "part_lot_screening": {"facility": "structures_lab", "target": "a stage: {'element': id}", "params": {},
                           "cost_usd": 0.8e6, "minutes": 5.0, "mode": "weak_strut"},
    "centrifuge_test": {"facility": "structures_lab", "target": "the return capsule: {'element': id}", "params": {},
                        "cost_usd": 0.5e6, "minutes": 4.0, "mode": "inverted_gswitch"},
    "edl_hil_test": {"facility": "avionics_lab", "target": "the Mars lander: {}", "params": {},
                     "cost_usd": 2.0e6, "minutes": 8.0, "mode": "imu_saturation_hold"},
    "nav_end_to_end": {"facility": "avionics_lab", "target": "the mission's navigation chain: {}", "params": {},
                       "cost_usd": 0.6e6, "minutes": 5.0, "mode": "units_mismatch"},
    "radiation_test": {"facility": "thermal_vacuum", "target": "the avionics parts list: {}", "params": {},
                       "cost_usd": 1.2e6, "minutes": 6.0, "mode": "see_reset"},
    "touchdown_sensor_test": {"facility": "drop_range", "target": "a lander stage with legs: {'element': id}",
                              "params": {}, "cost_usd": 0.7e6, "minutes": 5.0, "mode": "touchdown_transient"},
}
LESSONS = {
    "reversed_polarity": "a rate sensor was mounted reversed; the stage would have steered the wrong way",
    "heritage_overflow": "the reused inertial software overflows above this vehicle's horizontal speed",
    "weak_strut": "a pressurant bottle strut broke at a fifth of its certified load",
    "inverted_gswitch": "the capsule's deceleration switches were installed inverted; the parachutes would never fire",
    "imu_saturation_hold": "the IMU saturation flag holds far longer than the flight software assumes",
    "units_mismatch": "small-force impulses were written in lbf s and read as N s",
    "see_reset": "memory chips upset under heavy ions and reboot the computer",
}


def _u(secret, key):
    h = hmac.new(str(secret).encode(), key.encode(), hashlib.sha256).digest()
    return random.Random(int.from_bytes(h[:8], "little"))


def quote(kind, spec, params):
    if kind not in TEST_KINDS:
        raise ValueError(f"unknown test kind {kind!r}")
    k = TEST_KINDS[kind]
    cost, minutes = k["cost_usd"], k["minutes"]
    if kind == "avionics_burn_in":
        h = float((params or {}).get("hours", 500))
        if not (100 <= h <= 2000):
            raise ValueError("hours must be 100 to 2000")
        cost += 1.0e3 * h
    key = f"failtest:{kind}:{(spec or {}).get('element', 'vehicle')}"
    return {"kind": kind, "key": key, "facility": k["facility"], "cost_usd": cost, "minutes": minutes}


def failure_record(ledger):
    """What the done tests established: {"failure_tests": {kind: n}, "burn_in_h": {element: hours}}."""
    rec = {"failure_tests": {}, "burn_in_h": {}}
    for t in ledger or []:
        if t.get("status") != "done" or t.get("kind") not in TEST_KINDS:
            continue
        rec["failure_tests"][t["kind"]] = rec["failure_tests"].get(t["kind"], 0) + 1
        out = t.get("outcome") or {}
        if t["kind"] == "avionics_burn_in" and out.get("element"):
            rec["burn_in_h"][out["element"]] = rec["burn_in_h"].get(out["element"], 0.0) + float(out.get("hours", 0.0))
    return rec


def run_test(kind, spec, params, test_id, ledger, secret):
    q = quote(kind, spec, params)
    rng = _u(secret, "failtest:" + str(test_id))
    rec = failure_record(ledger)
    out = {"kind": kind, "key": q["key"], "measured": {}}
    if kind == "avionics_burn_in":
        h = float((params or {}).get("hours", 500))
        eid = (spec or {}).get("element")
        age0 = rec["burn_in_h"].get(eid, 0.0) / 8766.0
        age1 = age0 + h / 8766.0
        failed = [u for u in R.part("avionics_string")["units"]
                  if rng.random() < 1.0 - math.exp((age0 / R.weibull(u)[1]) ** R.weibull(u)[0]
                                                   - (age1 / R.weibull(u)[1]) ** R.weibull(u)[0])]
        p_mission = R.p_fail(R.part("avionics_string")["units"], 4.0, age1)
        out.update(element=eid, hours=h, measured={"units_failed_and_replaced": failed,
                                                    "string_failure_chance_4_years_after": round(p_mission, 5)})
        out["summary"] = (f"{h:.0f} h burn-in of {eid}'s avionics: {len(failed)} unit(s) failed and were replaced. "
                          f"One string now has a {100 * p_mission:.2f}% chance to fail in 4 years.")
        return out
    mode = TEST_KINDS[kind]["mode"]
    if mode == "touchdown_transient":
        legs = sum(rng.random() < 0.5 for _ in range(3)) or 1
        ms = round(10 + 40 * rng.random(), 0)
        out["measured"] = {"legs_with_spurious_signal": legs, "signal_ms": ms}
        out["summary"] = (f"Leg deployment test: {legs} of 3 touchdown sensors gave a spurious touchdown signal of about "
                          f"{ms:.0f} ms as the legs locked. Flight software must not latch it.")
        return out
    p_before = F.p_flaw(mode, rec)
    cov = F.LATENT[mode][2]
    found = rng.random() < p_before * cov    # a flaw was there and this test caught it
    out["measured"] = {"flaw_found": bool(found), "remaining_chance": round(p_before * (1 - cov), 6)}
    out["summary"] = (f"Found and fixed: {LESSONS[mode]}." if found else "No flaw found.") + \
        f" The chance that one is still there is now {100 * p_before * (1 - cov):.3f}%."
    return out
