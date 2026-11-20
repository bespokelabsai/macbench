"""The national grid: one synthetic transmission network, generated from a fixed seed.

Every run and every team size gets the same grid. Scenarios (scenario.py) vary what is broken, the
weather and the clock, never the grid itself. The grid has a public part (the map every control room
has: buses, lines, plants, load sizes) and a private part per area (what only that area's control room
knows: plant start-up data, line impedances, relay settings, cold load pickup, crews).

Units are MW, MVAr, km, minutes and per unit on a 100 MVA base.
"""
import math
import random

GRID_SEED = 20261101
N_ROWS, N_COLS = 4, 4
N_AREAS = N_ROWS * N_COLS
BASE_MVA = 100.0

# Unit types: Pmax range, min load share, start minutes (hot, cold), hot window h, cranking share of Pmax,
# inertia H s, governor time constant s, ramp % of Pmax per minute, black-start capable, dispatchable.
UNIT_TYPES = {
    "hydro":   dict(pmax=(60, 160), pmin=0.10, start=(4, 6), hot_h=999, crank=0.0, H=3.0, Tg=6.0, ramp=50, bs=True),
    "diesel":  dict(pmax=(30, 60), pmin=0.20, start=(2, 3), hot_h=999, crank=0.0, H=1.2, Tg=1.5, ramp=60, bs=True),
    "ct":      dict(pmax=(40, 110), pmin=0.25, start=(12, 25), hot_h=999, crank=0.03, H=4.5, Tg=4.0, ramp=12, bs=False),
    "ccgt":    dict(pmax=(220, 420), pmin=0.40, start=(60, 210), hot_h=6, crank=0.05, H=5.5, Tg=7.0, ramp=4, bs=False),
    "coal":    dict(pmax=(300, 620), pmin=0.45, start=(180, 540), hot_h=8, crank=0.08, H=6.0, Tg=9.0, ramp=1.5, bs=False),
    "wind":    dict(pmax=(60, 220), pmin=0.0, start=(1, 1), hot_h=999, crank=0.0, H=0.0, Tg=1.0, ramp=100, bs=False),
    "solar":   dict(pmax=(50, 160), pmin=0.0, start=(1, 1), hot_h=999, crank=0.0, H=0.0, Tg=1.0, ramp=100, bs=False),
    "nuclear": dict(pmax=(900, 1300), pmin=0.6, start=(99999, 99999), hot_h=0, crank=0.0, H=7.0, Tg=10.0, ramp=0.5,
                    bs=False),
}

# Start-up and response data by kind, calibrated to published plant data: minutes to
# synchronise from a hot, warm and cold state; hours a unit stays hot and warm after it stops (a unit that tripped in
# the blackout loses its auxiliaries, so it cools faster than after a planned shutdown); ramp in % of Pmax per minute
# once synchronised; the share of Pmax its governor can pick up within seconds (primary reserve); minutes before a
# failed start can be retried; probability that a start attempt fails after a blackout; probability that the unit
# trips within its first hours back (early-life trips after a restart); the governor's time constant (s), set so a
# load block of about 10% of a hydro, diesel or gas-turbine unit (8% of a combined cycle, 5% of a coal unit) dips an
# island by about 1 Hz, as restoration plans assume.
TIMING = {
    "hydro":  dict(start=(8, 8, 10), hot_h=999, warm_h=999, ramp=30, gov=0.15, retry=10, p_fail=0.12, p_early=0.03, Tg=2.0),
    "diesel": dict(start=(5, 5, 6), hot_h=999, warm_h=999, ramp=40, gov=0.25, retry=10, p_fail=0.12, p_early=0.05, Tg=0.8),
    "ct":     dict(start=(10, 12, 15), hot_h=8, warm_h=48, ramp=10, gov=0.25, retry=15, p_fail=0.10, p_early=0.06, Tg=1.5),
    "ccgt":   dict(start=(75, 150, 220), hot_h=4, warm_h=48, ramp=3, gov=0.10, retry=60, p_fail=0.12, p_early=0.10, Tg=2.5),
    "coal":   dict(start=(165, 300, 480), hot_h=6, warm_h=48, ramp=1.5, gov=0.05, retry=90, p_fail=0.15, p_early=0.12, Tg=5.0),
}

# Which areas have black-start units, and which have nuclear plants. Fixed so the grid's character is a
# design choice: six areas can start themselves, ten depend on a neighbour, and the three nuclear plants
# sit in areas that cannot.
BS_AREAS = {0: "hydro", 3: "diesel", 5: "hydro", 9: "diesel", 10: "hydro", 14: "hydro"}
NUCLEAR_AREAS = (2, 7, 12)


def area_name(a):
    return f"a{a + 1:02d}"


def _mst(points):
    """Prim's minimum spanning tree over 2-D points; returns index pairs."""
    n = len(points)
    in_tree = [False] * n
    best = [(math.inf, -1)] * n
    best[0] = (0.0, -1)
    edges = []
    for _ in range(n):
        i = min((d, k) for k, (d, _) in enumerate(best) if not in_tree[k])[1]
        in_tree[i] = True
        if best[i][1] >= 0:
            edges.append((best[i][1], i))
        for j in range(n):
            if not in_tree[j]:
                d = math.dist(points[i], points[j])
                if d < best[j][0]:
                    best[j] = (d, i)
    return edges


def build(seed=GRID_SEED, equipment=True):
    """Return the grid as plain dicts: {'buses', 'branches', 'units', 'loads', 'shunts', 'areas'}."""
    rng = random.Random(seed)
    buses, branches, units, loads, shunts = [], [], [], [], []
    areas = []
    for a in range(N_AREAS):
        r, c = divmod(a, N_COLS)
        cx, cy = c * 110.0 + rng.uniform(-8, 8), r * 110.0 + rng.uniform(-8, 8)
        nb = rng.randint(30, 37)
        first = len(buses)
        pts = []
        for k in range(nb):
            x, y = cx + rng.uniform(-42, 42), cy + rng.uniform(-42, 42)
            pts.append((x, y))
            buses.append(dict(id=f"{area_name(a)}-b{k + 1:02d}", area=a, x=round(x, 1), y=round(y, 1),
                              substation=f"{area_name(a)}-s{k + 1:02d}"))
        # Internal network: a spanning tree plus about a third more lines to the nearest buses.
        pairs = set(tuple(sorted(e)) for e in _mst(pts))
        for i in range(nb):
            order = sorted(range(nb), key=lambda j: math.dist(pts[i], pts[j]))
            for j in order[1:4]:
                if rng.random() < 0.22:
                    pairs.add(tuple(sorted((i, j))))
        for i, j in sorted(pairs):
            length = max(6.0, math.dist(pts[i], pts[j]) * rng.uniform(1.05, 1.25))
            branches.append(_line(len(branches), first + i, first + j, length, rng, tie=False, area=a))
        areas.append(dict(id=a, name=area_name(a), row=r, col=c, first_bus=first, n_bus=nb))

    # Tie lines: two between each pair of neighbouring areas, between their closest buses.
    for a in range(N_AREAS):
        r, c = divmod(a, N_COLS)
        for b in ([a + 1] if c + 1 < N_COLS else []) + ([a + N_COLS] if r + 1 < N_ROWS else []):
            A, B = areas[a], areas[b]
            cand = []
            for i in range(A["first_bus"], A["first_bus"] + A["n_bus"]):
                for j in range(B["first_bus"], B["first_bus"] + B["n_bus"]):
                    cand.append((math.dist((buses[i]["x"], buses[i]["y"]), (buses[j]["x"], buses[j]["y"])), i, j))
            cand.sort()
            used = set()
            n = 0
            for d, i, j in cand:
                if i in used or j in used:
                    continue
                used.update((i, j))
                branches.append(_line(len(branches), i, j, max(30.0, d * rng.uniform(1.1, 1.3)), rng, tie=True,
                                      area=None))
                n += 1
                if n == 2:
                    break

    # Plants and loads, area by area.
    for A in areas:
        a = A["id"]
        ids = list(range(A["first_bus"], A["first_bus"] + A["n_bus"]))
        rng.shuffle(ids)
        plant_buses = ids[:9]
        mix = []
        if a in BS_AREAS:
            mix.append(BS_AREAS[a])
            if BS_AREAS[a] == "hydro" and rng.random() < 0.5:
                mix.append("hydro")
        if a in NUCLEAR_AREAS:
            mix.append("nuclear")
        mix += ["ct"] * rng.randint(2, 3) + ["ccgt"] * rng.randint(1, 2) + ["coal"] * rng.randint(0, 2)
        mix += [rng.choice(["wind", "solar"])] + (["wind"] if rng.random() < 0.4 else [])
        for k, kind in enumerate(mix):
            bus = plant_buses[k % len(plant_buses)]
            units.append(_unit(len(units), kind, bus, a, rng))
        cap = sum(u["pmax"] for u in units if u["area"] == a and u["kind"] not in ("nuclear", "wind", "solar"))
        peak = cap * rng.uniform(0.55, 0.75)
        load_buses = [i for i in ids if rng.random() < 0.7] or ids[:5]
        weights = [rng.uniform(0.4, 1.6) for _ in load_buses]
        for i, w in zip(load_buses, weights):
            mw = peak * w / sum(weights)
            n_blocks = 4
            shares = [rng.uniform(0.6, 1.4) for _ in range(n_blocks)]
            for k in range(n_blocks):
                bmw = mw * shares[k] / sum(shares)
                prio = rng.random() < 0.09
                loads.append(dict(id=f"{buses[i]['id']}-f{k + 1}", bus=i, area=a, mw=round(bmw, 1), priority=prio,
                                  pf_tan=round(rng.uniform(0.2, 0.4), 3),
                                  ufls_stage=0 if prio else rng.choice([0] * 7 + [1, 2, 3])))
        for u in units:
            if u["area"] == a and u["kind"] == "nuclear":
                # Station service for the nuclear plant's cooling: a priority load on its bus.
                loads.append(dict(id=f"{u['id']}-aux", bus=u["bus"], area=a, mw=30.0, priority=True, pf_tan=0.4,
                                  ufls_stage=0, nuclear_aux=u["id"]))
        # Shunt reactors at three buses with the longest lines, for voltage control while lines are lightly loaded.
        longest = sorted((b for b in branches if b["area"] == a or (b["tie"] and (buses[b["f"]]["area"] == a))),
                         key=lambda b: -b["km"])[:3]
        for b in longest:
            bus = b["f"] if buses[b["f"]]["area"] == a else b["t"]
            shunts.append(dict(id=f"{buses[bus]['id']}-r{len(shunts) + 1}", bus=bus, area=a,
                               mvar=round(rng.uniform(20, 45), 1)))
    grid = dict(seed=seed, buses=buses, branches=branches, units=units, loads=loads, shunts=shunts, areas=areas)
    _plant_data(grid)
    _second_circuits(grid)
    if equipment:                 # each area's own equipment (equipment.py)
        from . import equipment as EQ
        grid["equipment"] = EQ.assign(grid)
    return grid


def _second_circuits(grid):
    """A nuclear plant needs two independent off-site circuits for its cooling (US NRC GDC 17, IAEA SSR-2/1): give
    any nuclear bus left on a single line a second line to the nearest bus of its area. Drawn from a stream of its
    own and appended last, so every other line keeps its id; scenario.py keeps these lines out of its fault draws, so
    the scenarios stay as they were."""
    rng = random.Random(grid["seed"] * 15485863 + 7)
    B, L = grid["buses"], grid["branches"]
    for u in grid["units"]:
        if u["kind"] != "nuclear":
            continue
        i = u["bus"]
        near = {b["t"] if b["f"] == i else b["f"] for b in L if i in (b["f"], b["t"])}
        if len(near) >= 2:
            continue
        cand = sorted((math.dist((B[i]["x"], B[i]["y"]), (B[j]["x"], B[j]["y"])), j) for j in range(len(B))
                      if B[j]["area"] == u["area"] and j != i and j not in near)
        d, j = cand[0]
        line = _line(len(L), min(i, j), max(i, j), max(6.0, d * rng.uniform(1.05, 1.25)), rng, tie=False,
                     area=u["area"])
        line["second_circuit"] = True
        L.append(line)


def _plant_data(grid):
    """Start-up, ramp and governor data per unit from TIMING, drawn from a stream of their own so the network, the
    plants' sizes and the loads stay exactly as generated above."""
    rng = random.Random(grid["seed"] * 104729 + 3)
    for u in grid["units"]:
        T = TIMING.get(u["kind"])
        if T is None:
            u.update(start_warm=u["start_cold"], warm_window_h=u["hot_window_h"], gov_reserve=0.0, retry_min=30)
            continue
        k = rng.uniform(0.85, 1.25)
        hot, warm, cold = (round(m * k * rng.uniform(0.95, 1.05), 0) for m in T["start"])
        u.update(start_hot=hot, start_warm=max(hot, warm), start_cold=max(hot, warm, cold),
                 hot_window_h=T["hot_h"] if T["hot_h"] >= 999 else round(T["hot_h"] * rng.uniform(0.7, 1.3), 1),
                 warm_window_h=T["warm_h"] if T["warm_h"] >= 999 else round(T["warm_h"] * rng.uniform(0.8, 1.2), 1),
                 ramp_mw_min=round(u["pmax"] * T["ramp"] / 100.0 * rng.uniform(0.8, 1.2), 1),
                 gov_reserve=round(T["gov"] * rng.uniform(0.8, 1.2), 3), retry_min=T["retry"], Tg=T["Tg"])


def _line(k, f, t, km, rng, tie, area):
    x = 0.00085 * km * rng.uniform(0.9, 1.1)
    return dict(id=f"L{k + 1:03d}", f=f, t=t, km=round(km, 1), tie=tie, area=area,
                r=round(x * rng.uniform(0.09, 0.14), 6), x=round(x, 6),
                b=round(0.0017 * km * rng.uniform(0.9, 1.1), 6),
                rating=float(rng.choice([300, 400, 500, 600] if tie else [250, 300, 400, 500])))


def _unit(k, kind, bus, area, rng):
    T = UNIT_TYPES[kind]
    pmax = round(rng.uniform(*T["pmax"]), 0)
    hot, cold = T["start"]
    return dict(id=f"G{k + 1:03d}", kind=kind, bus=bus, area=area, pmax=pmax,
                pmin=round(pmax * T["pmin"] * rng.uniform(0.9, 1.1), 1),
                start_hot=round(hot * rng.uniform(0.85, 1.25), 0), start_cold=round(cold * rng.uniform(0.85, 1.25), 0),
                hot_window_h=T["hot_h"] if T["hot_h"] >= 999 else round(T["hot_h"] * rng.uniform(0.7, 1.3), 1),
                crank_mw=round(pmax * T["crank"] * rng.uniform(0.8, 1.3), 1), H=T["H"], Tg=T["Tg"],
                ramp_mw_min=round(pmax * T["ramp"] / 100.0, 1), droop=0.05, black_start=T["bs"],
                qmax=round(pmax * (0.6 if T["bs"] else 0.45), 1), qmin=round(-pmax * (0.45 if T["bs"] else 0.3), 1),
                energy_mwh=round(pmax * rng.uniform(5, 9), 0) if kind == "hydro" else (
                    round(pmax * rng.uniform(6, 10), 0) if kind == "diesel" else None))


def area_private(grid, a):
    """Everything only area a's control room knows: its nominal values, or the day's once a scenario has set them
    (scenario.apply_day: units, cold load pickup and crews). Relay settings and line data do not change."""
    rng = random.Random(grid["seed"] * 7919 + a)
    bus_ids = {i for i, b in enumerate(grid["buses"]) if b["area"] == a}
    lines = [dict(id=b["id"], r=b["r"], x=b["x"], b=b["b"], rating=b["rating"]) for b in grid["branches"]
             if b["area"] == a or (b["tie"] and (b["f"] in bus_ids or b["t"] in bus_ids))]
    units = [{k: u[k] for k in ("id", "kind", "pmin", "start_hot", "start_warm", "start_cold", "hot_window_h",
                                "warm_window_h", "retry_min", "crank_mw", "H", "Tg", "ramp_mw_min", "droop",
                                "gov_reserve", "qmax", "qmin", "energy_mwh")} for u in grid["units"]
             if u["area"] == a]
    # an item's exact values (`secret`) stay on the hub: the area is told their ranges (equipment.py)
    eq = {"equipment": [{k: v for k, v in it.items() if k != "secret"} for it in grid["equipment"].get(a, [])]} \
        if "equipment" in grid else {}
    day = (grid.get("day") or {}).get("areas", {}).get(a)
    out = dict(
        area=area_name(a),
        lines=lines,
        units=units,
        **eq,
        # Cold load pickup: a feeder that has been off for h hours draws (1 + (clpu - 1) * min(1, h / 4)) times its
        # load when it comes back, decaying to normal with the time constant below.
        # The excess (factor - 1) is scaled by the scenario's weather (obs["weather"]["cold_load_severity"]).
        cold_load_pickup=dict(factor=round(rng.uniform(1.8, 2.8), 2), decay_min=round(rng.uniform(30, 60), 0)),
        # Energising a dead section trips its breaker when the section's charging plus transformer inrush (MVA)
        # exceeds this ratio times the online synchronous capacity (MW) of the island it is energised from.
        inrush_ratio=round(rng.uniform(0.6, 1.0), 2),
        transformer_inrush_mva=round(rng.uniform(12, 24), 1),
        # Under-frequency load shedding stages (Hz) for feeders marked with stage 1, 2 or 3.
        ufls_hz=[59.0, 58.7, 58.4],
        # Field crews: where they start and how fast they drive.
        crews=[dict(id=f"{area_name(a)}-crew{k + 1}", depot=grid["buses"][sorted(bus_ids)[rng.randrange(len(bus_ids))]]["id"],
                    kmh=round(rng.uniform(45, 70), 0)) for k in range(2)],
        ufls=[dict(id=l["id"], stage=l["ufls_stage"]) for l in grid["loads"] if l["area"] == a and l["ufls_stage"]],
    )
    if day:
        out["cold_load_pickup"] = dict(day["cold_load_pickup"])
        out["crews"] = [dict(c, depot=depot, kmh=kmh) for c, (depot, kmh) in zip(out["crews"], day["crews"])]
    return out


def public_view(grid):
    """The grid map every control room has."""
    B = grid["buses"]
    return dict(
        areas=[dict(id=A["name"], row=A["row"], col=A["col"]) for A in grid["areas"]],
        buses=[dict(id=b["id"], area=area_name(b["area"]), x=b["x"], y=b["y"], substation=b["substation"]) for b in B],
        branches=[dict(id=b["id"], from_bus=B[b["f"]]["id"], to_bus=B[b["t"]]["id"], km=b["km"], tie=b["tie"],
                       areas=sorted({area_name(B[b["f"]]["area"]), area_name(B[b["t"]]["area"])}))
                  for b in grid["branches"]],
        units=[dict(id=u["id"], kind=u["kind"], bus=B[u["bus"]]["id"], area=area_name(u["area"]), pmax=u["pmax"],
                    black_start=u["black_start"]) for u in grid["units"]],
        loads=[dict(id=l["id"], bus=B[l["bus"]]["id"], area=area_name(l["area"]), mw=l["mw"], priority=l["priority"])
               for l in grid["loads"]],
        shunts=[dict(id=s["id"], bus=B[s["bus"]]["id"], area=area_name(s["area"]), mvar=s["mvar"])
                for s in grid["shunts"]],
    )
