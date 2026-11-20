"""The study simulator's blackouts: the team defines them (a JSON spec, see ../README.md). There is no scenario
generator and no hidden seed here; a random choice (`{"fraction": 0.45, "seed": 3}`) uses the team's own seed.

The failure processes of MANUAL.md (start failures, damaged units, early trips, failed RTUs, misset relays, relays
tripping on inrush) are off unless the spec asks for them, by listing them or with `"failures": {"seed": s}`, which
draws them at the rates MANUAL.md states with the team's seed. These are not the hidden scenarios' draws.
"""
import math
import random

HORIZON_MIN = 16 * 60
STEP_S = 30
START_ATTEMPTS = 8
GAS = ("ct", "ccgt")
# MANUAL.md, Start failures and early trips: chance a start attempt fails, chance a unit trips in its first 3 hours.
P_FAIL = {"hydro": 0.12, "diesel": 0.12, "ct": 0.10, "ccgt": 0.12, "coal": 0.15}
P_EARLY = {"hydro": 0.03, "diesel": 0.05, "ct": 0.06, "ccgt": 0.10, "coal": 0.12}


def load_profile(start_hour, minute):
    """Demand as a share of peak through the day (a typical two-peak curve)."""
    h = (start_hour + minute / 60.0) % 24
    return 0.62 + 0.18 * math.exp(-((h - 8.5) / 2.5) ** 2) + 0.26 * math.exp(-((h - 18.5) / 3.0) ** 2) - 0.1 * math.exp(-((h - 3.5) / 2.5) ** 2)


def solar_profile(start_hour, minute):
    h = (start_hour + minute / 60.0) % 24
    return max(0.0, math.sin(math.pi * (h - 6.0) / 13.0)) if 6.0 <= h <= 19.0 else 0.0


def cold_load_severity(temp_c):
    """MANUAL.md, Weather."""
    return round(1.0 + max(0.0, abs(temp_c - 18.0) - 6.0) / 28.0, 3)


def _pick(spec, ids, default_fraction, salt):
    """A list of ids, or {"fraction": f, "seed": s} for a random share of them chosen with the team's seed."""
    if isinstance(spec, list):
        return spec
    spec = spec if isinstance(spec, dict) else {}
    rng = random.Random(f"{salt}:{spec.get('seed', 0)}")
    return [i for i in ids if rng.random() < float(spec.get("fraction", default_fraction))]


def failures(grid, spec, unavailable, offline_h, faulted):
    """The failure processes the spec asks for: listed ones as given, `"failures": {"seed": s}` drawn at MANUAL.md's
    rates for whatever the spec does not list, nothing otherwise."""
    L, U, B = grid["branches"], grid["units"], grid["buses"]
    li = {b["id"]: k for k, b in enumerate(L)}
    ui = {u["id"]: k for k, u in enumerate(U)}
    bi = {b["id"]: k for k, b in enumerate(B)}
    fs = spec.get("failures")
    draw = isinstance(fs, dict) and "seed" in fs
    rng = random.Random(f"failures:{fs.get('seed') if draw else 0}")
    cold = spec.get("weather") == "cold"
    startable = [i for i, u in enumerate(U) if u["kind"] in P_FAIL and i not in unavailable]
    out = dict(inrush_trips=bool(spec.get("inrush_trips", draw)))
    if "start_fail" in spec:
        out["start_fail"] = {ui[u]: [bool(x) for x in seq] for u, seq in spec["start_fail"].items()}
    elif draw:
        out["start_fail"] = {}
        for i in startable:
            k = U[i]["kind"]
            p = P_FAIL[k] * (2.5 if cold and k in GAS else 1.0)
            seq = [rng.random() < p for _ in range(START_ATTEMPTS)]
            if any(seq):
                out["start_fail"][i] = seq
    if "damaged" in spec:
        out["damaged"] = sorted(ui[u] for u in spec["damaged"])
    elif draw:
        ran = [i for i in startable if U[i]["kind"] in ("ct", "ccgt", "coal") and i not in offline_h]
        out["damaged"] = sorted(rng.sample(ran, min(len(ran), rng.randint(1, 3))))
    if "early_trip" in spec:
        out["early_trip"] = {ui[u]: float(m) for u, m in spec["early_trip"].items()}
    elif draw:
        out["early_trip"] = {i: round(rng.uniform(5, 180), 1) for i in startable if rng.random() < P_EARLY[U[i]["kind"]]}
    if "rtu_failed" in spec:
        out["scada_lost"] = sorted(bi[b] for b in spec["rtu_failed"])
    elif draw:                          # about 3 to 7% of substations; not the control centres (each area's first bus)
        firsts = {A["first_bus"] for A in grid["areas"]}
        out["scada_lost"] = sorted(rng.sample([i for i in range(len(B)) if i not in firsts], rng.randint(15, 35)))
    if "relay_misset" in spec:
        out["relay_misop"] = {li[x]: float(v) for x, v in spec["relay_misset"].items()}
    elif draw:                          # 5 to 10 lines inside the areas, tripping at 55 to 85% of their rating
        internal = [i for i, b in enumerate(L) if not b["tie"] and not b.get("second_circuit") and i not in faulted]
        out["relay_misop"] = {i: round(rng.uniform(0.55, 0.85), 2) for i in rng.sample(internal, rng.randint(5, 10))}
    return out


def build(grid, spec):
    """The simulator's scenario dict from a team's blackout spec. The failure processes are off unless the spec asks
    for them (see failures())."""
    L, U, LD, B = grid["branches"], grid["units"], grid["loads"], grid["buses"]
    li = {b["id"]: k for k, b in enumerate(L)}
    ui = {u["id"]: k for k, u in enumerate(U)}
    fi = {f["id"]: k for k, f in enumerate(LD)}

    def lines(key):
        return sorted(li[x] for x in spec.get(key, []))

    temp = float(spec.get("temperature_c", 18.0))
    open_ids = _pick(spec.get("open_at_start", {}), [b["id"] for b in L], 0.45, "open")
    closed_ids = _pick(spec.get("feeders_closed", {}), [f["id"] for f in LD], 0.30, "feeders")
    faulted = lines("faulted")
    bat = spec.get("battery_h", 7.0)
    tel = spec.get("telecom_h", 5.0)
    nuc = spec.get("nuclear_deadline_h", 8.0)
    events = []
    for m, kind, target in spec.get("events", []):
        k = ui[target] if kind == "unit_trip" else li[target] if kind == "line_trip" else 0
        events.append((int(m), kind, k))
    s = dict(
        seed=int(spec.get("seed", 0)),
        start_hour=int(spec.get("start_hour", 18)),
        weather=str(spec.get("weather", "mild")),
        temperature_c=temp,
        cold_load_severity=cold_load_severity(temp),
        line_rating_factor=float(spec.get("line_rating_factor", 1.0)),
        crew_speed=float(spec.get("crew_speed", 1.0)),
        load_scale=float(spec.get("load_scale", 0.9)),
        wind=float(spec.get("wind", 0.5)),
        solar=float(spec.get("solar", 0.6)),
        faulted=faulted,
        unavailable=sorted(ui[x] for x in spec.get("unavailable", [])),
        stuck=[k for k in lines("stuck") if k not in faulted],
        open_at_start=sorted(li[x] for x in open_ids),
        feeders_closed=sorted(fi[x] for x in closed_ids),
        battery_h=[float(bat.get(b["id"], 7.0)) if isinstance(bat, dict) else float(bat) for b in B],
        telecom_h=[float(tel.get(A["name"], 5.0)) if isinstance(tel, dict) else float(tel) for A in grid["areas"]],
        nuclear_deadline_h={u["id"]: float(nuc.get(u["id"], 8.0)) if isinstance(nuc, dict) else float(nuc)
                            for u in U if u["kind"] == "nuclear"},
        offline_h={ui[k]: float(v) for k, v in spec.get("offline_h", {}).items()},
        events=sorted(events),
        scada_lost=[], relay_misop={}, start_fail={}, damaged=[], early_trip={}, inrush_trips=False,
    )
    s.update(failures(grid, spec, set(s["unavailable"]), s["offline_h"], set(faulted)))
    s["unavailable"] = [i for i in s["unavailable"] if i not in s["damaged"]]
    return s


# Today's values, as the grader draws them (the true simulator's scenario.apply_day, same ranges): on the day of a
# blackout each unit's start times, ramp rate, cranking power, governor reserve and minimum stable load, the energy
# in a hydro reservoir or diesel tank, each area's cold load pickup and where its crews start differ from the
# nominal values area_data gives. In grading each controller is told only its own area's values for the day.
DAY_UNIT = {"start": (0.8, 1.3), "ramp_mw_min": (0.75, 1.15), "crank_mw": (0.9, 1.25), "gov_reserve": (0.75, 1.1),
            "pmin": (0.9, 1.15), "energy_mwh": (0.5, 1.0)}
DAY_CLPU = dict(factor=(1.8, 2.8), decay_min=(30, 60))
DAY_CREW_KMH = (45, 70)
DAY_KINDS = ("hydro", "diesel", "ct", "ccgt", "coal")


def day_private(private, public, seed):
    """A day's private data: the given {area: area_data(area)} with today's values drawn in the grader's ranges
    (not the grader's draws: those come from scenario seeds you never see)."""
    import copy
    import random
    rng = random.Random(f"study-day:{seed}")
    out = copy.deepcopy(private)
    for name, P in sorted(out.items()):
        for u in P.get("units", []):
            if u.get("kind") not in DAY_KINDS:
                continue
            f = rng.uniform(*DAY_UNIT["start"])
            u["start_hot"] = round(u["start_hot"] * f, 0)
            u["start_warm"] = max(u["start_hot"], round(u["start_warm"] * f, 0))
            u["start_cold"] = max(u["start_warm"], round(u["start_cold"] * f, 0))
            for k in ("ramp_mw_min", "crank_mw", "gov_reserve"):
                u[k] = round(u[k] * rng.uniform(*DAY_UNIT[k]), 3)
            pmax = next((x["pmax"] for x in public["units"] if x["id"] == u["id"]), None)
            u["pmin"] = round(u["pmin"] * rng.uniform(*DAY_UNIT["pmin"]), 1)
            if pmax:
                u["pmin"] = min(u["pmin"], round(0.8 * pmax, 1))
            if u.get("energy_mwh") is not None:
                u["energy_mwh"] = round(u["energy_mwh"] * rng.uniform(*DAY_UNIT["energy_mwh"]), 0)
        if "cold_load_pickup" in P:
            P["cold_load_pickup"] = dict(factor=round(rng.uniform(*DAY_CLPU["factor"]), 2),
                                         decay_min=round(rng.uniform(*DAY_CLPU["decay_min"]), 0))
        buses = [b["id"] for b in public["buses"] if b["area"] == name]
        for c in P.get("crews", []):
            c.update(depot=rng.choice(buses), kmh=round(rng.uniform(*DAY_CREW_KMH), 0))
    return out

