"""The grade: the mission judged the way a program accepts one, and the net value it returns.

    acceptance = 0.90 * M + [S >= 0.5] * 0.10 * F
    score      = G * (0.60 * acceptance + [S >= 0.5] * 0.40 * (1 - exp(-max(0, EV - cost / V))))

G   correctness gate, 0 or 1 (deliverables, a valid design, the budgets, honest test records). verify.py
    applies it; records_honest() checks the last part.
M   mission value: the mean over the verifier's worlds of world_value(), judged against the success criteria.
S   the survival-weighted chance that the samples come home. F and the net value count only when S >= 0.5, so a
    submission that does not fly cannot score on predicting its own failure.
F   flight readiness forecast: the team's stated probabilities against what happened, by a proper rule that
    forgives the sampling noise of a finite number of worlds (FORECAST_TOL_SE).

Net value is expected benefit minus cost, the cost-benefit form a program justifies a mission with:
EV    the mean over the worlds of [samples home from the Jezero target] x s x sqrt(n): s is the real chance that
      no hardware failed up to the capsule's touchdown (no 0.90 cap), and n the Orbiting Sample containers the
      capsule brings home (SampleContainmentAssembly.sampleContainers, 30 tubes each). Samples landed off the
      Jezero target are not the cached ones and are worth nothing; Utah precision carries no science value.
      More samples are worth more with diminishing returns: four containers are worth twice one.
cost  hardware plus R&D spent.
V     what a full success with one container is worth: a mission that just meets the 0.90 requirement on the whole
      budget must be worth its cost (0.90 V >= budget), and V keeps a benefit-cost ratio of 2 above that:
      V = 2 x (hardware + R&D budgets) / 0.90 = $2.04B. One point of mission success is then worth about $20M.

The net value enters as 1 - exp(-net), the exponential utility of a risk-averse program: nearly the net value itself
while it is small (one point of mission success is still worth about $20M of cost), worth a little less per unit as
it grows, and below 1 however good the design. So the score stays in [0, 1], and cheaper, more reliable and more
samples always score higher. A perfect mission with one container at the reference's cost scores about 0.78; the
score approaches 1 only near the 20-container cap, perfectly reliable and nearly free. Every price, survival and
payload is the program's, so the exchange rates are the program office's own trade.

Reliability is a requirement: a world counts in full once the chance that no hardware failed up to the moment
the credit was earned is at least R_REQUIRED (0.90), and below that by survival / R_REQUIRED. The reference
architecture can reach it with testing (its floor, every hazard at its minimum, is about 0.94), so an ideal design
that brings the samples home on target in every world and forecasts honestly earns the whole acceptance part
(exactly, whenever each realized share falls within the forecast's tolerance).

World value, each part weighted by min(1, survival / R_REQUIRED) at the moment it was earned:
    samples home            0.60   capsule with the samples lands intact inside the Utah range
    + Mars precision        0.20   the samples came from within 3 km of the Jezero target
    + Utah precision        0.20   the capsule landed within 10 km of the Utah target
    capsule lost at Earth   0      the capsule reached Earth and did not land intact inside the range: an
                                   uncontained Mars sample on Earth is what sample return exists to prevent
    mission lost elsewhere  <=0.35 phase gates passed, plus at most half a gate's credit toward the next one,
                                   from measures earned with energy, height or time (never approach geometry)
"""
from __future__ import annotations

import json
import math
import os

from . import data as D
from . import economics as E
from .program import survival

W_MISSION, W_FORECAST = 0.90, 0.10   # within the acceptance part
W_ACCEPTANCE, W_NET_VALUE = 0.60, 0.40
BENEFIT_COST = 2.0
HOME, MARS_PRECISION, UTAH_PRECISION = 0.60, 0.20, 0.20
S_GATE = 0.5
R_REQUIRED = 0.90
# What a full success with one sample container is worth to the program: $4.09B with the doubled budgets.
MISSION_VALUE_USD = BENEFIT_COST * (E.HARDWARE_BUDGET_USD + E.RND_BUDGET_USD) / R_REQUIRED
RANGE_KM = float(D.SITES["earth_landing"].get("range_km", 50.0))

# gate, credit, earned measures (partial-credit keys) that count toward it
GATES = [
    ("earth_departure", 0.05, ["earth_orbit", "earth_departure"]),
    ("mars_arrival", 0.03, []),
    ("mars_touchdown", 0.10, ["mars_entry", "mars_descent"]),
    ("samples_loaded", 0.05, ["samples_loaded"]),
    ("mars_departure", 0.07, ["mars_orbit", "mars_departure"]),
    ("earth_entry", 0.03, ["earth_entry"]),
]
MARS_LANDING_ON_TARGET = 0.02   # a lost mission that still landed within 3 km of Jezero

PHASES = [  # name, started when, passed when (None: every world starts it)
    ("launch and trans-Mars burn", None, "earth_departure"),
    ("cruise to Mars", "earth_departure", "mars_arrival"),
    ("Mars entry, descent and landing", "mars_arrival", "mars_touchdown"),
    ("Mars landing within 3 km", "mars_touchdown", "mars_target"),
    ("surface, 30 sols", "mars_touchdown", "samples_loaded"),
    ("Mars ascent and trans-Earth burn", "samples_loaded", "mars_departure"),
    ("return, Earth entry and landing in the range", "mars_departure", "_home"),
    ("Utah landing within 10 km", "_home", "earth_target"),
]

FORECAST_FILE = "program/flight_readiness.json"
# An honest probability p, judged on n worlds, misses the realized share by about sqrt(p (1 - p) / n) by chance
# alone. Misses within this many of those standard errors are free, so an ideal design scores 1.0 in most seed
# sets, not only when the share happens to equal the probability (the oracle's Mars-within-1-km share was
# 16/48 on one set and 23/48 on another).
FORECAST_TOL_SE = 2.0
FORECAST_ITEMS = ("p_full_success", "p_samples_home", "p_mars_within_3km", "p_mars_within_1km",
                  "p_utah_within_10km", "p_utah_within_5km")


def landing_errors(events) -> dict:
    """{'Mars': km, 'Earth': km} from a flight's events: each landing_error_km follows its touchdown."""
    out, body = {}, None
    for e in events or []:
        if e.get("type") == "touchdown":
            body = e.get("body")
        elif e.get("type") == "landing_error_km" and body:
            out[body] = e.get("km")
    return out


def _home(res) -> bool:
    ms = res.get("milestones") or {}
    utah = (res.get("landing_errors_km") or {}).get("Earth")
    return "earth_touchdown" in ms and utah is not None and utah <= RANGE_KM


def world_value(res: dict, hz: dict | None) -> dict:
    """The value of one world and how it was judged."""
    ms, part, pt = res.get("milestones") or {}, res.get("partial") or {}, res.get("partial_t") or {}
    ex = res.get("exposures") or []
    raw = (lambda t: survival(ex, hz, t)) if hz else (lambda t: 1.0)  # noqa: E731
    surv = lambda t: min(1.0, raw(t) / R_REQUIRED)  # noqa: E731
    if _home(res):
        s = raw(ms["earth_touchdown"])
        mars_ok, utah_ok = "mars_target" in ms, "earth_target" in ms
        v = HOME + MARS_PRECISION * mars_ok + UTAH_PRECISION * utah_ok
        return {"outcome": "full success" if mars_ok and utah_ok else "samples home", "value": v * min(1.0, s / R_REQUIRED),
                "home_survival": s, "mars_precision": mars_ok, "utah_precision": utah_ok}
    if "earth_arrival" in ms:
        return {"outcome": "capsule lost at Earth", "value": 0.0, "home_survival": 0.0}
    v = 0.0
    for gate, credit, measures in GATES:
        if gate in ms:
            v += credit * surv(ms[gate])
            if gate == "mars_touchdown" and "mars_target" in ms:
                v += MARS_LANDING_ON_TARGET * surv(ms["mars_target"])
            continue
        if measures:
            fr = [1.0 if k in ms else _finite01(part.get(k, 0.0)) for k in measures]
            ts = [pt[k] for k in measures if k in pt] or [res.get("end_t") or 0.0]
            v += 0.5 * credit * (sum(fr) / len(fr)) * surv(max(ts))
        break
    return {"outcome": "mission lost", "value": v, "home_survival": 0.0}


# Engine-out (realism gate G4). program.CATASTROPHIC_FRACTION of engine failures destroy the stage; the rest
# shut one engine down. For each group of two or more engines of one kind, the verifier flies two of the graded
# worlds again with one of those engines shut down (engine_out_cases), and the share of the value at risk each
# kept (engine_out_kept) sets how much of the group's failure chance the mission survives (engine_out_survival,
# then program.hazards(..., engine_out=...)). Off by default (they doubled grading time for
# little benefit): every engine failure loses the mission, as before G4. MARSFLIGHT_ENGINE_OUT=1 turns them on.
ENGINE_OUT_CHECKS = os.environ.get("MARSFLIGHT_ENGINE_OUT", "0") == "1"


def engine_out_cases(design: dict, results: list, hz: dict) -> list:
    """The engine-out checks: for each group of two or more engines of one kind on an element
    (program.multi_engine_groups), one flight with an engine shut down when the element first fires, and one
    with it shut down halfway through the group's burning, both in the first graded world (in seed order) where
    the element fired. Returns [(seed, case, weight)]: the weight is the share of the group's failure chance in
    that world the case stands for, its starts for the first and its burning for the second. Nothing in it is
    random, so the same flights give the same checks."""
    from .program import multi_engine_groups
    cases = []
    for g in multi_engine_groups(design):
        eid, engine = g.split("/", 1)
        for r in sorted(results, key=lambda r: r.get("seed", 0)):
            exs = r.get("exposures") or []
            ex = [e for e in exs if len(e) > 4 and e[4] == eid and e[2] == engine]
            fired = [e[0] for e in exs if e[1] == "stage_ignition" and e[2] == eid]
            if not ex or not fired:
                continue
            t_lit = fired[0]
            starts = sum(e[3] for e in ex if e[1] == "start")
            burns = sorted((e[0], e[3]) for e in ex if e[1] == "burn")
            total = sum(a for _, a in burns)
            p_start = hz["start"].get(engine, 0.0) * starts
            p_burn = hz["burn"].get(engine, 0.0) * total
            w_start = p_start / (p_start + p_burn) if p_start + p_burn > 0 else 0.5
            cases.append((r["seed"], {"element": eid, "engine": engine, "after_ignition_s": 0.0}, w_start))
            # Burn exposures are stamped at the end of their arc (sim records (t + dt, n * dt)), so the halfway point
            # is placed inside the arc that crosses it, from the arc's start: the end of the group's previous burn
            # arc or its latest start, whichever is later. On a single long arc it lands mid-burn, not at burnout.
            start_ts = sorted(e[0] for e in ex if e[1] == "start")
            done, t_mid, prev_end = 0.0, None, None
            for t, a in burns:
                if done + a >= total / 2:
                    t0 = max([x for x in ([prev_end] + [s for s in start_ts if s <= t]) if x is not None], default=t)
                    t_mid = t0 + (t - t0) * ((total / 2 - done) / a if a > 0 else 1.0)
                    break
                done += a
                prev_end = t
            if t_mid is not None:
                cases.append((r["seed"], {"element": eid, "engine": engine, "after_ignition_s": round(t_mid - t_lit, 3)},
                              1.0 - w_start))
            break
    return cases


def _banked(res: dict, t: float) -> float:
    """The world value a flight had earned for certain by time t: the phase gates passed by then, no partial credit
    toward the next one and no hardware failure chance."""
    ms = {k: v for k, v in (res.get("milestones") or {}).items() if v <= t + 1e-6}
    return world_value({"milestones": ms, "partial": {}, "partial_t": {}, "end_t": t}, None)["value"]


def _kept(twin: dict, r: dict, t_fail) -> float:
    """The share a check flight r kept of the value at risk: the value its graded twin (the same world, nothing
    forced) earned beyond what it had banked at t_fail, judged as the grade judges a world (world_value, without
    hardware failure chances). A check whose failure never happened (t_fail None) is judged on the whole flight."""
    before = 0.0 if t_fail is None else _banked(twin, t_fail)
    at_risk = world_value(twin, None)["value"] - before
    kept = world_value(r, None)["value"] - before
    return 1.0 if at_risk <= 1e-9 else max(0.0, min(1.0, kept / at_risk))


def engine_out_kept(twin: dict, r: dict) -> float:
    """The share an engine-out flight r kept of the value at risk after its engine shut down (_kept)."""
    return _kept(twin, r, (r.get("engine_out") or {}).get("t"))


def engine_out_survival(cases: list, eo_results: list, results: list) -> dict:
    """{"element/engine": share of the group's failure chance that the mission survives}, for program.hazards:
    each case counts with its weight and the share of the value at risk it kept (engine_out_kept). A case whose
    flight did not come back counts as kept nothing."""
    twins = {r["seed"]: r for r in results}
    out = {}
    for i, (seed, case, w) in enumerate(cases):
        g = f"{case['element']}/{case['engine']}"
        r = eo_results[i] if i < len(eo_results) else None
        out[g] = out.get(g, 0.0) + w * (engine_out_kept(twins[seed], r) if r is not None and seed in twins else 0.0)
    return {g: round(v, 6) for g, v in out.items()}


# Failure checks beyond engine-out, the same pattern. A parachute with a backup on the same carrier
# (program.backup_chutes) is flown failing to open; a stage whose engines can restart (program.retry_stages) is
# flown with its first ignition a no-light. The share of the value at risk each check kept is how often the mission
# survives that failure, s, and the part's failure chance then counts with 1 - (survivable share) x s
# (program.chute_loss_share, ignition_loss_share). Off (MARSFLIGHT_FAILURE_CHECKS=0, or no time left), those
# failures lose the mission, as before.
FAILURE_CHECKS = os.environ.get("MARSFLIGHT_FAILURE_CHECKS", "1") != "0"


def failure_check_cases(design: dict, results: list) -> list:
    """The failure checks: for each parachute with a backup, one flight where it fails to open, and for each
    stage that can restart, one flight where its first ignition does not light, each in the first graded world
    (in seed order) where that parachute deployed or that stage fired. Returns [(seed, case)]; nothing in it is
    random."""
    from .program import backup_chutes, retry_stages
    cases = []
    ordered = sorted(results, key=lambda r: r.get("seed", 0))
    for kind, parts, exposure in (("chute_fail", backup_chutes(design), "chute"),
                                  ("no_light", retry_stages(design), "stage_ignition")):
        for part in parts:
            for r in ordered:
                if any(e[1] == exposure and e[2] == part for e in r.get("exposures") or []):
                    cases.append((r["seed"], {"kind": kind, "element": part}))
                    break
    return cases


def failure_check_kept(twin: dict, r: dict) -> float:
    """The share a failure-check flight r kept of the value at risk after its forced failure (_kept)."""
    return _kept(twin, r, (r.get("failure_check") or {}).get("t"))


def failure_check_survival(cases: list, fc_results: list, results: list) -> dict:
    """{"chute_fail": {parachute: s}, "no_light": {stage: s}} for program.hazards(failure_checks=...). A check whose
    flight did not come back counts as kept nothing."""
    twins = {r["seed"]: r for r in results}
    out = {"chute_fail": {}, "no_light": {}}
    for i, (seed, case) in enumerate(cases):
        r = fc_results[i] if i < len(fc_results) else None
        kept = failure_check_kept(twins[seed], r) if r is not None and seed in twins else 0.0
        out[case["kind"]][case["element"]] = round(kept, 6)
    return out


def _finite01(x) -> float:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, x)) if math.isfinite(x) else 0.0


def phase_report(results) -> list:
    """For each phase: how many worlds reached it and how many passed it."""
    def has(r, k):
        return _home(r) if k == "_home" else k in (r.get("milestones") or {})
    rows = []
    for name, start, end in PHASES:
        started = [r for r in results if start is None or has(r, start)]
        rows.append({"phase": name, "started": len(started), "passed": sum(1 for r in started if has(r, end))})
    return rows


def realized(results, hz: dict | None) -> dict:
    """What happened over the worlds, in the forecast's terms: the share of worlds with each outcome (physical,
    not survival-weighted, so a design that succeeds everywhere can know them exactly)."""
    n = max(1, len(results))
    vals = [world_value(r, hz) for r in results]
    errs = [r.get("landing_errors_km") or {} for r in results]

    def within(body, km):
        if body == "Earth":
            return sum(1 for r, e in zip(results, errs) if _home(r) and e.get("Earth") is not None and e["Earth"] <= km) / n
        return sum(1 for r, e in zip(results, errs)
                   if "mars_touchdown" in (r.get("milestones") or {}) and e.get("Mars") is not None and e["Mars"] <= km) / n
    return {"p_full_success": sum(1 for v in vals if v["outcome"] == "full success") / n,
            "p_samples_home": sum(1 for v in vals if v["outcome"] in ("full success", "samples home")) / n,
            "p_mars_within_3km": within("Mars", 3.0), "p_mars_within_1km": within("Mars", 1.0),
            "p_utah_within_10km": within("Earth", 10.0), "p_utah_within_5km": within("Earth", 5.0)}


def _number(x):
    """x as a finite float, or None: booleans, non-numbers, NaN, infinities and integers too large for a float."""
    if not isinstance(x, (int, float)) or isinstance(x, bool):
        return None
    try:
        x = float(x)
    except OverflowError:
        return None
    return x if math.isfinite(x) else None


def forecast_score(forecast, real: dict, n: int = 48) -> dict:
    """F: the mean over the six items of 1 - 4 e^2, not clipped, where e is the miss |p - f| less a tolerance of
    FORECAST_TOL_SE binomial standard errors of the stated p over n worlds, sqrt(p (1 - p) / n). A missing or
    non-numeric item, or a probability outside [0, 1], scores 0.

    Without the tolerance this is the quadratic (Brier) rule, which is proper: an honest probability is the best
    report. With it the rule stays nearly proper: shading a report toward 0.5 widens its own tolerance, but over
    48 worlds the most it can gain is under 0.0001 of F (dev/test_grading.py checks this), while the honest report
    earns 1 in about 95% of seed sets per item."""
    items = {}
    for k in FORECAST_ITEMS:
        p = _number(forecast.get(k)) if isinstance(forecast, dict) else None
        ok = p is not None and 0.0 <= p <= 1.0
        if ok:
            tol = FORECAST_TOL_SE * math.sqrt(p * (1.0 - p) / max(1, n))
            miss = max(0.0, abs(p - real[k]) - tol)
        items[k] = {"predicted": p if ok else None, "realized": round(real[k], 6),
                    "tolerance": round(tol, 6) if ok else None,
                    "score": round(1.0 - 4.0 * miss ** 2, 6) if ok else 0.0}
    return {"F": round(sum(v["score"] for v in items.values()) / len(items), 6), "items": items,
            "submitted": isinstance(forecast, dict), "n": n}


def payload_value(sample_containers) -> float:
    """What n sample containers are worth against one: more samples, with diminishing returns (sqrt)."""
    try:
        n = int(sample_containers)
    except (TypeError, ValueError):
        n = 1
    return math.sqrt(max(1, n))


def net_value(per: list, cost_usd: float, sample_containers=1) -> dict:
    """EV - cost / V: the expected value of what comes home, in units of a full one-container success, less the
    cost in the same units, and its utility to a risk-averse program, 1 - exp(-net), which the grade uses."""
    n = max(1, len(per))
    p_jezero = sum(v["home_survival"] for v in per
                   if v["outcome"] in ("full success", "samples home") and v.get("mars_precision")) / n
    ev = p_jezero * payload_value(sample_containers)
    cost = _number(cost_usd)
    cost_share = cost / MISSION_VALUE_USD if cost is not None and cost >= 0 else float("inf")
    return {"p_home_from_jezero": round(p_jezero, 6), "sample_containers": max(1, int(sample_containers or 1)),
            "payload_value": round(payload_value(sample_containers), 6), "EV": round(ev, 6),
            "cost_usd": cost, "cost_over_V": round(cost_share, 6) if math.isfinite(cost_share) else None,
            "V_usd": MISSION_VALUE_USD, "net": round(max(0.0, ev - cost_share), 6),
            "utility": round(1.0 - math.exp(-max(0.0, ev - cost_share)), 6)}


def records_honest(saved, ledger, rel_tol=1e-3, abs_tol=1e-6) -> dict:
    """G3. Every result the team saved must match an outcome the program office gave it: the same test kind and
    part key, and every measured number equal to within rounding, in tables too. A test's outcome is what the office
    recorded and,, the team's view of it (team_result: absolute measurements, no hidden factors), which is
    what a team reads and saves; a saved result may match either. Leaving results out is never a failure.
    Every other number of a saved entry is checked too, wherever the office's result has a value in the
    same place (a failure's MET, a simulated world's milestone times, a list's items): before, only `measured` was,
    so a changed number elsewhere passed. Fields the office's result does not have (a team's own notes) are not
    checked."""
    if isinstance(saved, dict):
        saved = saved.get("results", [])
    if saved is None:
        saved = []
    if not isinstance(saved, list):
        return {"ok": False, "checked": 0, "unmatched": ["program/test_results.json is not a list of results"]}
    # An outcome recorded without its part key (mission_sim before it carried one) takes the key of its test.
    outcomes = [o if o.get("key") is not None else {**o, "key": t.get("key")}
                for t in ledger or [] if t.get("status") == "done"
                for o in (t.get("outcome") or {}, t.get("team_result")) if isinstance(o, dict)]

    def close(v, w):
        if isinstance(v, dict):
            return isinstance(w, dict) and all(close(x, w.get(k)) for k, x in v.items())
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            v, w = _number(v), _number(w)
            return v is not None and w is not None and abs(v - w) <= max(abs_tol, rel_tol * abs(w))
        return True   # text and flags are not checked

    def has_number(v):
        if isinstance(v, dict):
            return any(has_number(x) for x in v.values())
        if isinstance(v, list):
            return any(has_number(x) for x in v)
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    def agrees(v, w):
        """v (saved) against w (the office's), where the office has a value: numbers equal to within rounding."""
        if isinstance(v, dict) and isinstance(w, dict):
            return all(agrees(x, w[k]) for k, x in v.items() if k in w)
        if isinstance(v, list) and isinstance(w, list):
            return all(agrees(x, y) for x, y in zip(v, w))
        if has_number(v):
            return close(v, w) if not isinstance(v, (dict, list)) else False
        return True

    def same(a, b):
        if a.get("kind") != b.get("kind") or a.get("key") != b.get("key"):
            return False
        ma, mb = a.get("measured") or {}, b.get("measured") or {}
        rest = {k: v for k, v in a.items() if k not in ("kind", "key", "measured") and k in b}
        return isinstance(ma, dict) and close(ma, mb) and agrees(rest, b)
    unmatched = []
    for i, r in enumerate(saved):
        if not isinstance(r, dict) or not any(same(r, o) for o in outcomes):
            unmatched.append(f"entry {i}: " + (f"{r.get('kind')} {r.get('key')}" if isinstance(r, dict) else "not an object"))
    return {"ok": not unmatched, "checked": len(saved), "unmatched": unmatched[:20]}


def grade(results, hz: dict | None, cost_usd: float, forecast=None, sample_containers=1) -> dict:
    """The score before the gate, its parts, and what the review board would read. cost_usd: hardware plus R&D."""
    n = len(results)
    if n == 0:
        return {"score": 0.0}
    per = [world_value(r, hz) for r in results]
    M = sum(v["value"] for v in per) / n
    S = sum(v["home_survival"] for v in per) / n
    real = realized(results, hz)
    fc = forecast_score(forecast, real, n)
    counted = S >= S_GATE
    acceptance = W_MISSION * M + (W_FORECAST * fc["F"] if counted else 0.0)
    nv = net_value(per, cost_usd, sample_containers)
    score = W_ACCEPTANCE * acceptance + (W_NET_VALUE * nv["utility"] if counted else 0.0)
    worst = sorted(v["value"] for v in per)[:max(1, n // 10)]
    outcomes = {k: sum(1 for v in per if v["outcome"] == k)
                for k in ("full success", "samples home", "capsule lost at Earth", "mission lost")}
    return {"score": round(max(0.0, score), 6), "acceptance": round(acceptance, 6), "M": round(M, 6), "S": round(S, 6),
            "F": fc["F"], "net_value": nv, "forecast_and_net_value_counted": counted, "outcomes": outcomes,
            "full_success_share": round(outcomes["full success"] / n, 6),
            "worst_10pct_mean_value": round(sum(worst) / len(worst), 6),
            "phases": phase_report(results), "forecast": fc,
            "world_values": [round(v["value"], 6) for v in per], "world_outcomes": [v["outcome"] for v in per]}


def load_json(path):
    """A team file, or None when it is missing or not JSON."""
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None
