"""Seeded rescue scenario on a real cave: the overdue party, where its groups are, the clue trail, decoys.

    python -m crsim.scenario CAVE.json SEED OUT.json

Times are sim minutes from the call-out (t = 0). The storm started before the call-out.
"""
import json, math, random, sys
from .world import Cave, basins, flood_levels, storm_hyetograph

SIM_HOURS = 36            # length of the rescue window
PRE_STORM_H = 4           # rain began this long before the call-out
FIRST = ["Anna", "Tom", "Ingrid", "Mateo", "Priya", "Jonas", "Sasha", "Wen", "Leon", "Ruth", "Kofi", "Elif", "Bram", "Noor",
         "Dmitri", "Hana", "Olly", "Marta", "Yusuf", "Freya", "Tariq", "Lena", "Rafe", "Mina"]
LAST = ["Kowalski", "Brennan", "Haugen", "Ortiz", "Raman", "Weber", "Novak", "Liang", "Fischer", "Adeyemi", "Demir", "de Vries",
        "Haddad", "Petrov", "Sato", "Price", "Lindqvist", "Ruiz", "Okafor", "Moreau"]
SUITS = [(0.85, 0.25, 0.08), (0.92, 0.55, 0.05), (0.85, 0.75, 0.10), (0.15, 0.30, 0.70), (0.55, 0.10, 0.12), (0.20, 0.45, 0.25)]
HELMETS = [(0.92, 0.92, 0.90), (0.95, 0.80, 0.10), (0.80, 0.12, 0.10), (0.20, 0.25, 0.30)]

def hhmm(t_min, start_clock=19 * 60 + 30):
    m = int(round(start_clock + t_min)) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"

def build(cave_path, seed):
    cave = Cave(cave_path)
    rnd = random.Random(seed)
    lab, spill = basins(cave)
    rain = storm_hyetograph(SIM_HOURS + PRE_STORM_H, peak_mm_h=26 + rnd.uniform(-4, 6), seed=seed)
    levels, keep = flood_levels(cave, lab, spill, rain)
    # levels[k] is at sim minute (k*5 - PRE_STORM_H*60)
    def level_at(st, t):
        b = lab[st]; k = max(0, min(len(levels) - 1, int((t + PRE_STORM_H * 60) // 5)))
        return levels[k][1].get(b)
    def sumped(st, t):
        lv = level_at(st, t); return lv is not None and lv >= cave.roof[st] - 0.05

    unknown_st = {i for i in range(cave.n) if any(not cave.passages[pi]["known"] for _, _, pi in cave.adj[i])}
    dd, dprev = cave.dijkstra(cave.base, ok=cave.drone_ok)
    hd, hprev = cave.dijkstra(cave.base, ok=cave.human_ok)

    def roomy(i):   # somewhere a person can sit and a camera can see: not a shaft, not a squeeze
        s = cave.S[i]
        return 0.9 <= s["h"] <= 5.0 and s["w"] >= 0.8 and all(cave.passages[pi]["steep"] < 0.7 for _, _, pi in cave.adj[i])
    for attempt in range(400):
        # push point: deep in unknown passage, reachable by drone and on foot
        cands = [i for i in unknown_st if i in dd and i in hd and 1300 <= hd[i] <= 2300 and cave.S[i]["junction"] is False and roomy(i)]
        P = rnd.choice(cands)
        route = cave.path(hprev, P)                       # the way the party went in
        if not any(i in unknown_st for i in route[len(route) // 2:]): continue
        # A: the accident site at the push point
        A = P
        # B: two who headed out and were stopped part way back, at a dry spot on the route
        mid = [i for i in route[int(len(route) * 0.35): int(len(route) * 0.75)] if not sumped(i, 0) and i in dd and roomy(i)]
        if not mid: continue
        # prefer a spot just inboard of a passage that is sumped around the call-out
        cut = [k for k, i in enumerate(route) if sumped(i, 0) or sumped(i, 60)]
        if cut:
            k = cut[-1]
            inboard = [i for i in route[k + 1: k + 12] if not sumped(i, 0) and not sumped(i, 120) and i in dd and roomy(i)]
            B = inboard[0] if inboard else rnd.choice(mid)
            b_story = "water" if inboard else "light"
        else:
            B = rnd.choice(mid); b_story = "light"
        # C: one who went for another way out and is stuck in a side branch
        on_route = set(route)
        side = [i for i in dd if i not in on_route and i in hd and 300 <= hd[i] <= hd[P] - 200
                and min(abs(hd[i] - hd[j]) for j in (A, B)) > 150]
        side = [i for i in side if roomy(i) and any(cave.passages[pj]["steep"] > 0.85 for _, _, pi in cave.adj[i]
                                                    for _, _, pj in cave.adj[cave.passages[pi]["a"]] + cave.adj[cave.passages[pi]["b"]])]
        if not side: continue
        C = rnd.choice(side)
        if len({A, B, C}) < 3: continue
        break
    else:
        raise SystemExit("no layout found")

    # party
    names = rnd.sample(FIRST, 5); surn = rnd.sample(LAST, 5)
    people = [dict(name=f"{n} {s}", first=n, suit=rnd.choice(SUITS), helmet=rnd.choice(HELMETS)) for n, s in zip(names, surn)]
    injured = people[0]; carer = people[1]; outA, outB = people[2], people[3]; loner = people[4]
    entry_t = -rnd.randint(18, 22) * 60
    acc_t = -rnd.randint(8, 10) * 60 - rnd.randint(0, 59)
    groups = [
        dict(id="A", station=A, members=[injured["name"], carer["name"]], injured=True, cold_limit=acc_t + 18 * 60),
        dict(id="B", station=B, members=[outA["name"], outB["name"]], injured=False, cold_limit=acc_t + 24 * 60, story=b_story),
        dict(id="C", station=C, members=[loner["name"]], injured=False, cold_limit=acc_t + 20 * 60),
    ]
    objects, decals = [], []
    def obj(kind, st, group=None, **kw):
        objects.append(dict(id=f"o{len(objects)}", kind=kind, station=st, group=group, **kw))
    def chalk(st, toward, lines, kind, party=False, **kw):
        decals.append(dict(id=f"d{len(decals)}", station=st, toward=toward, lines=lines, kind=kind, party=party,
                           font=kw.pop("font", rnd.choice(["Caveat.ttf", "GochiHand-Regular.ttf", "IndieFlower-Regular.ttf"])), **kw))
    # people
    obj("person_lying", A, group="A", person=injured["name"], suit=injured["suit"], helmet=injured["helmet"])
    obj("person_sitting", A, group="A", person=carer["name"], suit=carer["suit"], helmet=carer["helmet"], side=1)
    obj("survival_bag", A, group="A")
    obj("person_sitting", B, group="B", person=outA["name"], suit=outA["suit"], helmet=outA["helmet"])
    obj("person_sitting", B, group="B", person=outB["name"], suit=outB["suit"], helmet=outB["helmet"], side=1)
    obj("person_sitting", C, group="C", person=loner["name"], suit=loner["suit"], helmet=loner["helmet"])
    # trail on the way in: arrows at junctions with initials and times
    ini = "".join(p["first"][0] for p in people)
    hd_route = [hd[i] for i in route]
    t_in = lambda k: entry_t + (hd_route[k] / max(hd_route[-1], 1)) * (acc_t - 30 - entry_t)
    for k, st in enumerate(route[:-1]):
        if cave.S[st]["junction"] and len(cave.junction_passages[st]) >= 3 and rnd.random() < 0.7:
            nxt = route[k + 1]
            txt = [f"{ini} {hhmm(t_in(k))} →"] if rnd.random() < 0.6 else ["→ " + ini]
            chalk(st, nxt, txt, "party_arrow", party=True)
    # notes
    chalk(A, route[-2], [f"{hhmm(acc_t)} {injured['first'].upper()} FELL - LEG BROKEN", f"{carer['first']} staying here", "NEED STRETCHER"], "party_note", party=True)
    kb = route.index(B)
    chalk(route[max(0, kb - 1)], B, [f"{outA['first']}+{outB['first']} {hhmm(acc_t + 70)}", "going out for help", "WATER RISING" if b_story == "water" else "one light left"], "party_note", party=True)
    # where C left the route
    cd, cprev = cave.dijkstra(C, ok=cave.human_ok)
    split = min(route, key=lambda i: cd.get(i, 1e18))
    cpath = cave.path(cprev, split)[::-1]
    chalk(split, cpath[1] if len(cpath) > 1 else C, [f"{loner['first']} {hhmm(acc_t + 40)}", "trying the other way out →"], "party_note", party=True)
    for k, st in enumerate(cpath[1:-1], 1):
        if cave.S[st]["junction"] and rnd.random() < 0.5:
            chalk(st, cpath[k + 1], [f"{loner['first'][0]} →"], "party_arrow", party=True)
    # gear the party dropped
    for st in rnd.sample(route[len(route) // 3:-2], 3):
        obj(rnd.choice(["glove", "tackle_bag", "food_wrapper"]), st, group=None, party=True)
    # decoys: old expedition marks and gear all over the cave
    decoy_txt = [["CUCC 09"], ["QM 23"], ["15X"], ["OUT →"], ["← OUT"], ["CAMP →"], ["2011"], ["NO WAY ON"], ["→ sump"],
                 ["P 30m"], ["J+M 2014"], ["→ dig"], ["← entrance"], ["water ←"], ["DO NOT PASS"], ["Tunnocks"]]
    junc = [i for i in cave.junctions if i in dd]
    for st in rnd.sample(junc, min(140, len(junc))):
        nb = rnd.choice(cave.adj[st])[0]
        chalk(st, nb, rnd.choice(decoy_txt), "old_mark", party=False)
    for st in rnd.sample(list(dd), 70):
        obj(rnd.choice(["old_bag", "rope_coil", "barrel", "survey_tape", "old_bag", "rope_coil", "tarp"]), st, group=None, party=False)
    # call-out sheet
    known_names = []
    for st in route:
        for _, _, pi in cave.adj[st]:
            p = cave.passages[pi]
            if p["known"] and p["name"] not in known_names and st in route: known_names.append(p["name"])
    last_known = known_names[-1] if known_names else "the bottom of the known cave"
    callout = (f"Party of five: {', '.join(p['name'] for p in people)}. Entered 258 at {hhmm(entry_t)} yesterday "
               f"(call-out time {hhmm(0)}). Plan on the board: down the main route via {', '.join(known_names[:4])}, "
               f"then push the new leads beyond {last_known}. Due out by {hhmm(entry_t + 16 * 60)}. They mark junctions with chalk "
               f"arrows, initials {ini} and the time. Rain on the plateau since {hhmm(-PRE_STORM_H * 60)}.")
    return dict(seed=seed, cave=cave_path, sim_hours=SIM_HOURS, pre_storm_h=PRE_STORM_H, rain_mm_h=rain,
                basin=lab, flood_basins=sorted(keep), flood=[[t - PRE_STORM_H * 60, lv] for t, lv in levels],
                party=people, groups=groups, route_in=route, objects=objects, decals=decals, callout=callout,
                entry_t=entry_t, accident_t=acc_t)

if __name__ == "__main__":
    sc = build(sys.argv[1], int(sys.argv[2]))
    json.dump(sc, open(sys.argv[3], "w"))
    print(sc["callout"])
    for g in sc["groups"]: print(g)
    print("objects", len(sc["objects"]), "decals", len(sc["decals"]), "route len", len(sc["route_in"]))
