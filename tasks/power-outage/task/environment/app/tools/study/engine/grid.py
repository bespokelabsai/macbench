"""The study simulator's grid: the public map (grid/public_grid.json) plus whatever private data the caller passes
(what the team read with area_data), and nominal values for everything else.

Nothing here comes from the hub. Where an area's private data is missing, its lines get impedances from their length,
its units the typical values of their kind (MANUAL.md, How the grid behaves), and its relays, cold load pickup and
crews middle-of-the-range values. Feeders' power factors are not in anyone's data; they are nominal everywhere.
"""
import math

N_AREAS = 16
BASE_MVA = 100.0

# Typical data by kind (MANUAL.md and the usual textbook values). area_data gives each unit's own numbers.
TIMING = {
    "hydro":  dict(start=(8, 8, 10), hot_h=999, warm_h=999, ramp=30, gov=0.15, retry=10, pmin=0.10, crank=0.0, H=3.0, Tg=2.0),
    "diesel": dict(start=(5, 5, 6), hot_h=999, warm_h=999, ramp=40, gov=0.25, retry=10, pmin=0.20, crank=0.0, H=1.2, Tg=0.8),
    "ct":     dict(start=(10, 12, 15), hot_h=8, warm_h=48, ramp=10, gov=0.25, retry=15, pmin=0.25, crank=0.03, H=4.5, Tg=1.5),
    "ccgt":   dict(start=(75, 150, 220), hot_h=4, warm_h=48, ramp=3, gov=0.10, retry=60, pmin=0.40, crank=0.05, H=5.5, Tg=2.5),
    "coal":   dict(start=(165, 300, 480), hot_h=6, warm_h=48, ramp=1.5, gov=0.05, retry=90, pmin=0.45, crank=0.08, H=6.0, Tg=5.0),
}
OTHER = {
    "wind":    dict(pmin=0.0, H=0.0, Tg=1.0, ramp=100),
    "solar":   dict(pmin=0.0, H=0.0, Tg=1.0, ramp=100),
    "nuclear": dict(pmin=0.6, H=7.0, Tg=10.0, ramp=0.5),
}
NOMINAL_RELAYS = dict(cold_load_pickup=dict(factor=2.3, decay_min=45.0), inrush_ratio=0.8, transformer_inrush_mva=18.0)
NOMINAL_CREW_KMH = 55.0
PF_TAN = 0.3


def area_name(a):
    return f"a{a + 1:02d}"


def _nominal_unit(u):
    kind = u["kind"]
    pmax = u["pmax"]
    T = TIMING.get(kind)
    if T:
        hot, warm, cold = T["start"]
        d = dict(pmin=round(pmax * T["pmin"], 1), start_hot=hot, start_warm=warm, start_cold=cold,
                 hot_window_h=T["hot_h"], warm_window_h=T["warm_h"], retry_min=T["retry"],
                 crank_mw=round(pmax * T["crank"], 1), H=T["H"], Tg=T["Tg"], ramp_mw_min=round(pmax * T["ramp"] / 100, 1),
                 gov_reserve=T["gov"])
    else:
        O = OTHER[kind]
        d = dict(pmin=round(pmax * O["pmin"], 1), start_hot=99999, start_warm=99999, start_cold=99999, hot_window_h=0,
                 warm_window_h=0, retry_min=30, crank_mw=0.0, H=O["H"], Tg=O["Tg"],
                 ramp_mw_min=round(pmax * O["ramp"] / 100, 1), gov_reserve=0.0)
    bs = bool(u.get("black_start"))
    d.update(droop=0.05, qmax=round(pmax * (0.6 if bs else 0.45), 1), qmin=round(-pmax * (0.45 if bs else 0.3), 1),
             energy_mwh=round(pmax * 7, 0) if kind == "hydro" else (round(pmax * 8, 0) if kind == "diesel" else None))
    return d


def build(public, private=None):
    """The simulator's grid from the public map and {area: area_data(area)} for the areas the caller has."""
    private = private or {}
    B = public["buses"]
    idx = {b["id"]: i for i, b in enumerate(B)}
    anum = {A["id"]: k for k, A in enumerate(public["areas"])}
    buses = [dict(id=b["id"], area=anum[b["area"]], x=b["x"], y=b["y"], substation=b["substation"]) for b in B]
    areas = []
    for k, A in enumerate(public["areas"]):
        mine = [i for i, b in enumerate(buses) if b["area"] == k]
        areas.append(dict(id=k, name=A["id"], row=A["row"], col=A["col"], first_bus=mine[0], n_bus=len(mine)))
    line_data = {}
    for P in private.values():
        for ln in P.get("lines", []):
            line_data[ln["id"]] = ln
    branches = []
    for br in public["branches"]:
        f, t = idx[br["from_bus"]], idx[br["to_bus"]]
        own = line_data.get(br["id"])
        x = 0.00085 * br["km"]
        d = dict(id=br["id"], f=f, t=t, km=br["km"], tie=br["tie"], area=None if br["tie"] else buses[f]["area"],
                 r=round(0.115 * x, 6), x=round(x, 6), b=round(0.0017 * br["km"], 6),
                 rating=450.0 if br["tie"] else 360.0)
        if own:
            d.update(r=own["r"], x=own["x"], b=own["b"], rating=own["rating"])
        branches.append(d)
    unit_data = {}
    for P in private.values():
        for u in P.get("units", []):
            unit_data[u["id"]] = u
    units = []
    for u in public["units"]:
        d = dict(id=u["id"], kind=u["kind"], bus=idx[u["bus"]], area=anum[u["area"]], pmax=u["pmax"],
                 black_start=bool(u["black_start"]))
        d.update(_nominal_unit(u))
        if u["id"] in unit_data:
            d.update({k: v for k, v in unit_data[u["id"]].items() if k not in ("id", "kind")})
        units.append(d)
    stages = {}
    for P in private.values():
        for f in P.get("ufls", []):
            stages[f["id"]] = f["stage"]
    loads = []
    for ld in public["loads"]:
        d = dict(id=ld["id"], bus=idx[ld["bus"]], area=anum[ld["area"]], mw=ld["mw"], priority=ld["priority"],
                 pf_tan=PF_TAN, ufls_stage=stages.get(ld["id"], 0))
        if ld["id"].endswith("-aux"):
            d["nuclear_aux"] = ld["id"][:-4]
        loads.append(d)
    shunts = [dict(id=s["id"], bus=idx[s["bus"]], area=anum[s["area"]], mvar=s["mvar"]) for s in public["shunts"]]
    g = dict(seed=0, buses=buses, branches=branches, units=units, loads=loads, shunts=shunts, areas=areas)
    # local equipment (area_data()["equipment"]) of the areas the caller has data for
    eq = {anum[name]: P["equipment"] for name, P in private.items() if P.get("equipment") and name in anum}
    if eq:
        g["equipment"] = eq
    g["_private"] = {}
    for k, A in enumerate(areas):
        P = private.get(A["name"]) or {}
        rel = {key: P.get(key, NOMINAL_RELAYS[key]) for key in NOMINAL_RELAYS}
        crews = P.get("crews") or [dict(id=f"{A['name']}-crew{c + 1}", depot=buses[A["first_bus"]]["id"],
                                        kmh=NOMINAL_CREW_KMH) for c in range(2)]
        g["_private"][k] = dict(area=A["name"], crews=crews, **rel)
    return g


def area_private(grid, a):
    return grid["_private"][a]


def distance(grid, i, j):
    B = grid["buses"]
    return math.dist((B[i]["x"], B[i]["y"]), (B[j]["x"], B[j]["y"]))
