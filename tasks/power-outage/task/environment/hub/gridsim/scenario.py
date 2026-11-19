"""Blackout scenarios. A scenario is everything that varies around the fixed grid, drawn from a seed.

Hidden scenarios (the ones the verifier grades on) come from a salt only the verifier has (tests/hidden/salt): this
copy of the simulator, in the image root-only for the operations office, cannot produce them. Practice scenarios
come from the same distribution under a different salt, so a team can practise as much as it likes
without ever meeting a graded scenario.
"""
import hashlib
import math
import random

from . import grid as G

HORIZON_MIN = 16 * 60
STEP_S = 30
N_HIDDEN = 48


def _seed(salt, k):
    return int.from_bytes(hashlib.sha256(f"{salt}:{k}".encode()).digest()[:8], "big")


def hidden_seeds(salt, n=N_HIDDEN):
    if not salt:
        raise RuntimeError("the hidden scenarios need the verifier's salt")
    return [_seed(salt, k) for k in range(n)]


def practice_seed(k):
    return _seed("black-start-practice", int(k))


# Weather regimes, drawn per scenario: probability, temperature (C), demand scale, wind, solar, and what each does to
# the restoration. Storms bring clustered line faults and slow the crews; heat derates lines and raises cold load
# pickup; a cold snap raises demand and cold load pickup and makes gas units fail to start (frozen instruments and
# gas supply, as in Texas in February 2021).
WEATHER = {
    "mild":  dict(p=0.30, temp=(10, 24), load=(0.70, 0.95), wind=(0.15, 0.80), solar=(0.4, 1.0)),
    "storm": dict(p=0.25, temp=(6, 20), load=(0.75, 0.95), wind=(0.50, 0.95), solar=(0.1, 0.4)),
    "heat":  dict(p=0.20, temp=(32, 41), load=(0.95, 1.12), wind=(0.05, 0.35), solar=(0.7, 1.0)),
    "cold":  dict(p=0.25, temp=(-16, -3), load=(0.95, 1.12), wind=(0.05, 0.45), solar=(0.2, 0.6)),
}
START_ATTEMPTS = 8
GAS = ("ct", "ccgt")


def cold_load_severity(temp_c):
    """How much the weather amplifies cold load pickup: 1 near 18 C, more in heat and cold."""
    return round(1.0 + max(0.0, abs(temp_c - 18.0) - 6.0) / 28.0, 3)


def build(grid, seed):
    rng = random.Random(seed)
    B, L, U = grid["buses"], grid["branches"], grid["units"]
    # Second circuits to nuclear plants (grid._second_circuits) are kept out of every draw, so adding them left the
    # scenarios unchanged.
    internal = [i for i, b in enumerate(L) if not b["tie"] and not b.get("second_circuit")]
    ties = [i for i, b in enumerate(L) if b["tie"]]
    dispatchable = [i for i, u in enumerate(U) if u["kind"] in ("ct", "ccgt", "coal")]
    bs_units = [i for i, u in enumerate(U) if u["black_start"]]
    startable = [i for i, u in enumerate(U) if u["kind"] in G.TIMING]
    hour = rng.randrange(24)
    r, acc = rng.random(), 0.0
    for kind, W in WEATHER.items():
        acc += W["p"]
        if r < acc:
            break
    temp = round(rng.uniform(*W["temp"]), 1)
    storm = kind == "storm"
    # Storms damage the network around one or two centres: most of the faults fall in those areas.
    centres = rng.sample(range(G.N_AREAS), 2 if storm else 1)
    near = [i for i in internal if L[i]["area"] in centres]
    n_int = rng.randint(14, 24) if storm else rng.randint(7, 13)
    n_near = min(len(near), int(n_int * (0.65 if storm else 0.3)))
    faulted_int = set(rng.sample(near, n_near))
    faulted_int |= set(rng.sample([i for i in internal if i not in faulted_int], n_int - n_near))
    faulted = sorted(faulted_int | set(rng.sample(ties, rng.randint(3, 6) if storm else rng.randint(2, 4))))
    s = dict(
        seed=seed,
        start_hour=hour,
        weather=kind,
        temperature_c=temp,
        cold_load_severity=cold_load_severity(temp),
        line_rating_factor=round(rng.uniform(0.85, 0.93), 3) if kind == "heat" else 1.0,
        crew_speed=round(rng.uniform(0.5, 0.7), 2) if storm else 1.0,
        load_scale=round(rng.uniform(*W["load"]), 3),
        wind=round(rng.uniform(*W["wind"]), 3),
        solar=round(rng.uniform(*W["solar"]), 3),
        # Lines with a permanent fault: closing onto one trips it at once. Found only by trying (or by a crew).
        faulted=faulted,
        # Units out for maintenance: known from the start.
        unavailable=sorted(rng.sample(dispatchable, rng.randint(3, 7))),
        # Breakers that do not respond to a remote close until a crew has worked on them.
        stuck=sorted(rng.sample(internal, rng.randint(4, 8))),
        # Lines that tripped during the collapse and are open at the start; the rest stayed closed.
        open_at_start=sorted(i for i, b in enumerate(L) if not b.get("second_circuit") and rng.random() < 0.45),
        # Feeders whose breakers stayed closed through the collapse: they come back with their bus.
        feeders_closed=sorted(i for i in range(len(grid["loads"])) if rng.random() < 0.3),
        # Hours each substation's batteries keep its telemetry and remote control alive while it is dark.
        battery_h=[round(rng.uniform(4.0, 10.0), 2) for _ in B],
        # Hours each area's telecom node keeps the inter-control-room channel at full rate while dark.
        telecom_h=[round(rng.uniform(2.0, 8.0), 2) for _ in range(G.N_AREAS)],
        # Hours the backup diesels can cool each nuclear plant before off-site power must be back.
        nuclear_deadline_h={U[i]["id"]: round(rng.uniform(6.0, 10.0), 2) for i, u in enumerate(U) if u["kind"] == "nuclear"},
    )
    # Substations whose remote terminal unit or its link failed in the blackout: no telemetry and no remote control,
    # live or dark, until a crew has spent 30 minutes there. Telecom nodes are kept out (their loss is the battery rule).
    firsts = {A["first_bus"] for A in grid["areas"]}
    s["scada_lost"] = sorted(rng.sample([i for i in range(len(B)) if i not in firsts], rng.randint(15, 35)))
    # Protection that misoperates: a distance relay set too tight trips its line whenever the line's loading passes a
    # threshold (load encroachment), until a crew corrects the setting.
    s["relay_misop"] = {i: round(rng.uniform(0.55, 0.85), 2)
                        for i in rng.sample([i for i in internal if i not in faulted_int], rng.randint(5, 10))}
    # What each thermal unit was doing before the blackout: running units tripped hot; the others had been off for
    # some hours or days, so they are warm or cold. The share running follows demand at the hour of the blackout.
    lvl = load_profile(hour, 0) * s["load_scale"]
    p_run = {"coal": 0.55 + 0.35 * lvl, "ccgt": 0.35 + 0.45 * lvl, "ct": max(0.0, 0.6 * lvl - 0.3), "hydro": 0.5}
    s["offline_h"] = {}
    for i in startable:
        k = U[i]["kind"]
        if k in p_run and rng.random() >= p_run[k]:
            s["offline_h"][i] = round(rng.choice([rng.uniform(2, 20), rng.uniform(20, 200)]), 1)
    # Start attempts after a blackout fail more often than in normal service. Each unit's attempts are drawn here, so
    # the same unit fails the same way whenever a team starts it. In a cold snap gas units fail far more often.
    s["start_fail"] = {}
    for i in startable:
        k = U[i]["kind"]
        p = G.TIMING[k]["p_fail"] * (2.5 if kind == "cold" and k in GAS else 1.0)
        seq = [rng.random() < p for _ in range(START_ATTEMPTS)]
        if any(seq):
            s["start_fail"][i] = seq
    # Units damaged when they tripped: every start fails, they are lost for the horizon (found at the first attempt).
    ran = [i for i in dispatchable if i not in s["offline_h"]]
    s["damaged"] = sorted(rng.sample(ran, min(len(ran), rng.randint(1, 3))))
    # Early-life trips: minutes after a unit first comes back online at which it trips (boiler, feed pumps, controls).
    s["early_trip"] = {i: round(rng.uniform(5, 180), 1) for i in startable
                       if rng.random() < G.TIMING[U[i]["kind"]]["p_early"]}
    # Events during the restoration: (minute, kind, target index).
    n_line = rng.randint(3, 6) if storm else rng.randint(1, 3)
    s["events"] = sorted(
        [(rng.randint(60, HORIZON_MIN - 60), "unit_trip", rng.choice(dispatchable)) for _ in range(rng.randint(2, 4))]
        + [(rng.randint(60, HORIZON_MIN - 60), "line_trip", rng.choice(internal)) for _ in range(n_line)]
        + ([(rng.randint(120, HORIZON_MIN - 60), "wind_drop", 0)] if rng.random() < (0.8 if storm else 0.4) else []))
    s["unavailable"] = [i for i in s["unavailable"] if i not in s["damaged"]]
    s["stuck"] = [i for i in s["stuck"] if i not in faulted]
    apply_day(grid, seed)
    return s


# Today's values. What an area's control room knows about its own plant is not a fixed table: on the day of a
# blackout each unit's start times, ramp rate, cranking power, governor reserve and minimum stable load depend on
# its condition and its crew, a hydro reservoir or a diesel tank holds what it holds that day, the cold load pickup
# of the feeders depends on the hour and the mix of load on them, and the field crews start where they happen to be.
# Each scenario draws these from the nominal values (what area_data gives, grid.area_private without a day), from
# its own random stream, so the rest of the scenario is unchanged. The simulator, the capacity bound and each
# area's controller (its `private` at init) all use today's values; only the area itself is told them.
DAY_UNIT = {"start": (0.8, 1.3), "ramp_mw_min": (0.75, 1.15), "crank_mw": (0.9, 1.25), "gov_reserve": (0.75, 1.1),
            "pmin": (0.9, 1.15), "energy_mwh": (0.5, 1.0)}
DAY_CLPU = dict(factor=(1.8, 2.8), decay_min=(30, 60))
DAY_CREW_KMH = (45, 70)


def apply_day(grid, seed):
    """Set the grid's values for the day of the scenario with this seed, from its nominal values (kept in
    grid["nominal_units"] the first time, so a grid can be reused for any number of scenarios)."""
    rng = random.Random(_seed("black-start-day", seed))
    nominal = grid.setdefault("nominal_units", [dict(u) for u in grid["units"]])
    for u, n in zip(grid["units"], nominal):
        if n["kind"] not in G.TIMING:      # nuclear, wind and solar: nothing a crew changes
            continue
        f = rng.uniform(*DAY_UNIT["start"])
        u["start_hot"] = round(n["start_hot"] * f, 0)
        u["start_warm"] = max(u["start_hot"], round(n["start_warm"] * f, 0))
        u["start_cold"] = max(u["start_warm"], round(n["start_cold"] * f, 0))
        u["ramp_mw_min"] = round(n["ramp_mw_min"] * rng.uniform(*DAY_UNIT["ramp_mw_min"]), 2)
        u["crank_mw"] = round(n["crank_mw"] * rng.uniform(*DAY_UNIT["crank_mw"]), 1)
        u["gov_reserve"] = round(n["gov_reserve"] * rng.uniform(*DAY_UNIT["gov_reserve"]), 3)
        u["pmin"] = round(min(0.8 * n["pmax"], n["pmin"] * rng.uniform(*DAY_UNIT["pmin"])), 1)
        if n["energy_mwh"] is not None:
            u["energy_mwh"] = round(n["energy_mwh"] * rng.uniform(*DAY_UNIT["energy_mwh"]), 0)
    areas = {}
    for a in range(G.N_AREAS):
        buses = [b["id"] for b in grid["buses"] if b["area"] == a]
        areas[a] = dict(cold_load_pickup=dict(factor=round(rng.uniform(*DAY_CLPU["factor"]), 2),
                                              decay_min=round(rng.uniform(*DAY_CLPU["decay_min"]), 0)),
                        crews=[(rng.choice(buses), round(rng.uniform(*DAY_CREW_KMH), 0)) for _ in range(2)])
    grid["day"] = dict(seed=seed, areas=areas)


def load_profile(start_hour, minute):
    """Demand as a share of peak through the day (a typical two-peak curve)."""
    h = (start_hour + minute / 60.0) % 24
    return 0.62 + 0.18 * math.exp(-((h - 8.5) / 2.5) ** 2) + 0.26 * math.exp(-((h - 18.5) / 3.0) ** 2) - 0.1 * math.exp(-((h - 3.5) / 2.5) ** 2)


def solar_profile(start_hour, minute):
    h = (start_hour + minute / 60.0) % 24
    return max(0.0, math.sin(math.pi * (h - 6.0) / 13.0)) if 6.0 <= h <= 19.0 else 0.0
