"""Turn a Survex .3d survey into the cave graph the simulator runs on.

    build_graph.py CAVE.3d OUT.json [--prefix 258.]

Stations are survey stations (splay ends dropped). Legs keep their real length,
rise and the passage size from the survey's LRUD cross-sections; stations with no
cross-section borrow the nearest surveyed size along the passage. Chains of
two-legged stations collapse into passages between junctions, which is the unit
drones move along.
"""
import json, math, subprocess, sys, collections

def dump(path):
    out = subprocess.run(["dump3d", path], check=True, capture_output=True, text=True).stdout
    nodes, xsect, legs = {}, {}, []
    last = None
    for ln in out.splitlines():
        p = ln.split()
        if not p: continue
        if p[0] == "NODE":
            xyz = tuple(round(float(v), 2) for v in p[1:4])
            name = p[4].strip("[]")
            flags = set(p[5:])
            nodes.setdefault(xyz, {"names": [], "flags": set()})
            nodes[xyz]["names"].append(name); nodes[xyz]["flags"] |= flags
        elif p[0] == "XSECT":
            l, r, u, d = (float(v) for v in p[1:5])
            if min(l, r, u, d) >= 0 and (l + r) > 0 and (u + d) > 0:   # dump3d writes missing readings as negative
                xsect[p[5].strip("[]")] = (l, r, u, d)
        elif p[0] == "MOVE":
            last = tuple(round(float(v), 2) for v in p[1:4])
        elif p[0] == "LINE":
            cur = tuple(round(float(v), 2) for v in p[1:4])
            survey = p[4].strip("[]") if len(p) > 4 and p[4].startswith("[") else ""
            legs.append((last, cur, survey)); last = cur
    return nodes, xsect, legs

def build(path, prefix=""):
    nodes, xsect, legs = dump(path)
    splay = {k for k, v in nodes.items() if v["flags"] & {"ANON", "WALL"}}
    ids, st = {}, []
    def sid(xyz):
        if xyz not in ids:
            v = nodes.get(xyz, {"names": ["?"], "flags": set()})
            ids[xyz] = len(st)
            lrud = next((xsect[n] for n in v["names"] if n in xsect), None)
            st.append({"xyz": xyz, "name": sorted(v["names"], key=len)[0], "entrance": "ENTRANCE" in v["flags"], "lrud": lrud})
        return ids[xyz]
    adj = collections.defaultdict(dict)
    for a, b, survey in legs:
        if a is None or a == b or a in splay or b in splay: continue
        i, j = sid(a), sid(b)
        length = math.dist(a, b)
        adj[i][j] = adj[j][i] = {"len": length, "survey": survey}
    # keep the component holding the entrances
    ent = [i for i, s in enumerate(st) if s["entrance"]]
    seen, q = set(ent[:1]), list(ent[:1])
    while q:
        i = q.pop()
        for j in adj[i]:
            if j not in seen: seen.add(j); q.append(j)
    # fill missing sizes by BFS from surveyed stations
    size = {i: st[i]["lrud"] for i in seen if st[i]["lrud"]}
    frontier = list(size)
    while frontier:
        nxt = []
        for i in frontier:
            for j in adj[i]:
                if j in seen and j not in size: size[j] = size[i]; nxt.append(j)
        frontier = nxt
    for i in seen: size.setdefault(i, (1.0, 1.0, 1.0, 1.0))
    # collapse degree-2 chains into passages between junctions
    junction = {i for i in seen if len(adj[i]) != 2 or st[i]["entrance"]}
    passages, done = [], set()
    for a in junction:
        for b in adj[a]:
            if (a, b) in done: continue
            chain = [a, b]
            while chain[-1] not in junction:
                nxt = [k for k in adj[chain[-1]] if k != chain[-2]][0]
                chain.append(nxt)
                if chain[-1] == a: break
            for u, v in zip(chain, chain[1:]): done.add((u, v)); done.add((v, u))
            passages.append(chain)
    # remap to compact ids
    keep = sorted(seen); new = {o: n for n, o in enumerate(keep)}
    stations = []
    for o in keep:
        s = st[o]; l, r, u, d = size[o]
        stations.append({"id": new[o], "name": s["name"], "x": s["xyz"][0], "y": s["xyz"][1], "z": s["xyz"][2],
                         "l": l, "r": r, "u": u, "d": d, "w": round(l + r, 2), "h": round(u + d, 2), "surveyed": s["lrud"] is not None,
                         "entrance": s["entrance"], "junction": o in junction})
    out_p = []
    for c in passages:
        if new[c[0]] > new[c[-1]]: c = c[::-1]
        pts = [st[i]["xyz"] for i in c]
        L = sum(math.dist(p, q) for p, q in zip(pts, pts[1:]))
        out_p.append({"a": new[c[0]], "b": new[c[-1]], "stations": [new[i] for i in c], "len": round(L, 2),
                      "rise": round(pts[-1][2] - pts[0][2], 2),
                      "min_w": round(min(size[i][0] + size[i][1] for i in c), 2),
                      "min_h": round(min(size[i][2] + size[i][3] for i in c), 2),
                      "steep": round(max((abs(q[2] - p[2]) / max(math.dist(p, q), 1e-6) for p, q in zip(pts, pts[1:])), default=0), 3),
                      "survey": adj[c[0]][c[1]]["survey"]})
    # dedupe passages found from both ends
    uniq = {}
    for p in out_p: uniq.setdefault((p["a"], p["b"], tuple(p["stations"])), p)
    return {"source": path, "stations": stations, "passages": list(uniq.values())}

if __name__ == "__main__":
    g = build(sys.argv[1])
    json.dump(g, open(sys.argv[2], "w"))
    S, P = g["stations"], g["passages"]
    print(f"stations {len(S)}  junctions {sum(s['junction'] for s in S)}  passages {len(P)}  "
          f"length {sum(p['len'] for p in P):.0f} m  entrances {sum(s['entrance'] for s in S)}  "
          f"surveyed sizes {sum(s['surveyed'] for s in S)}  depth {max(s['z'] for s in S) - min(s['z'] for s in S):.0f} m")
