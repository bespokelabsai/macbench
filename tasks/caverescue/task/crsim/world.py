"""The cave as the simulator sees it: stations, legs, junction-to-junction passages, flooding."""
import heapq, json, math, collections

DRONE_W, DRONE_H = 0.35, 0.30     # smallest gap a caged micro-drone fits through (m)
HUMAN_W, HUMAN_H = 0.30, 0.25     # smallest squeeze a caver on foot passes (m)
STRETCHER_W, STRETCHER_H = 0.50, 0.45   # a stretcher needs this, or the squeeze must be widened first
KNOWN_UNTIL = "2013"              # surveys up to this year are on the rescue survey

class Cave:
    def __init__(self, path):
        g = json.load(open(path))
        self.raw = g
        self.S = g["stations"]
        self.n = len(self.S)
        self.z = [s["z"] for s in self.S]
        self.floor = [s["z"] - s.get("d", 0.5) for s in self.S]
        self.roof = [s["z"] + s.get("u", 0.5) for s in self.S]
        self.surveys = g.get("surveys", {})
        self.passages = []
        self.adj = collections.defaultdict(list)          # station -> [(nbr, leg_len, passage_idx)]
        self.junction_passages = collections.defaultdict(list)
        for pi, p in enumerate(g["passages"]):
            st = p["stations"]
            sv = self.surveys.get(p.get("survey_key", ""), {})
            year = (sv.get("date") or "9999")[:4]
            known = year <= KNOWN_UNTIL
            name = sv.get("title") or p.get("survey_key", "") or "unnamed"
            legs = []
            for a, b in zip(st, st[1:]):
                L = math.dist((self.S[a]["x"], self.S[a]["y"], self.S[a]["z"]), (self.S[b]["x"], self.S[b]["y"], self.S[b]["z"]))
                legs.append(L)
                self.adj[a].append((b, L, pi)); self.adj[b].append((a, L, pi))
            w = min(self.S[i]["w"] for i in st); h = min(self.S[i]["h"] for i in st)
            self.passages.append(dict(id=pi, a=st[0], b=st[-1], stations=st, legs=legs, len=sum(legs),
                                      min_w=w, min_h=h, rise=p["rise"], steep=p["steep"], known=known,
                                      year=year, name=name))
            self.junction_passages[st[0]].append(pi); self.junction_passages[st[-1]].append(pi)
        self.entrances = [i for i, s in enumerate(self.S) if s["entrance"]]
        self.base = min(self.entrances, key=lambda i: -self.S[i]["z"])
        self.junctions = sorted(self.junction_passages)

    def xyz(self, i): s = self.S[i]; return (s["x"], s["y"], s["z"])

    def drone_ok(self, pi): p = self.passages[pi]; return p["min_w"] >= DRONE_W and p["min_h"] >= DRONE_H
    def human_ok(self, pi): p = self.passages[pi]; return p["min_w"] >= HUMAN_W and p["min_h"] >= HUMAN_H
    def stretcher_ok(self, pi): p = self.passages[pi]; return p["min_w"] >= STRETCHER_W and p["min_h"] >= STRETCHER_H

    def dijkstra(self, src, ok=lambda pi: True, targets=None, edge=None):
        """edge(u, v, pi), when given, filters single legs (ok filters whole passages)."""
        dist = {src: 0.0}; prev = {}; pq = [(0.0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist[u]: continue
            if targets is not None and u in targets: break
            for v, L, pi in self.adj[u]:
                if not ok(pi) or (edge is not None and not edge(u, v, pi)): continue
                nd = d + L
                if nd < dist.get(v, 1e18): dist[v] = nd; prev[v] = (u, pi); heapq.heappush(pq, (nd, v))
        return dist, prev

    def path(self, prev, dst):
        out = [dst]
        while out[-1] in prev: out.append(prev[out[-1]][0])
        return out[::-1]

    def leg_passage(self, a, b):
        for v, L, pi in self.adj[a]:
            if v == b: return pi, L
        return None, None

# ---------- flooding: fill-and-spill basins ----------
def basins(cave):
    """Steepest-descent basins over station floors; returns (label per station, spill level per basin, sink)."""
    n = cave.n; fl = cave.floor
    down = list(range(n))
    for i in range(n):
        best = i
        for j, _, _ in cave.adj[i]:
            if fl[j] < fl[best]: best = j
        down[i] = best
    lab = [-1] * n
    for i in range(n):
        path = [i]
        while down[path[-1]] != path[-1] and lab[path[-1]] < 0: path.append(down[path[-1]])
        root = lab[path[-1]] if lab[path[-1]] >= 0 else path[-1]
        for k in path: lab[k] = root
    spill = {}
    for i in range(n):
        for j, _, _ in cave.adj[i]:
            if lab[i] != lab[j]:
                s = max(fl[i], fl[j])
                if s < spill.get(lab[i], 1e18): spill[lab[i]] = s
    return lab, spill

def storm_hyetograph(hours, peak_mm_h=28.0, seed=0):
    """Hourly rain (mm/h): a front with two cells, the second heavier, like the Alpine summer storms."""
    import random
    rnd = random.Random(seed)
    out = []
    for h in range(hours):
        a = peak_mm_h * 0.55 * math.exp(-((h - 5) / 2.2) ** 2)
        b = peak_mm_h * math.exp(-((h - 15) / 3.0) ** 2)
        out.append(max(0.0, a + b + 0.6 + rnd.uniform(-0.4, 0.4)))
    return out

def flood_levels(cave, lab, spill, rain_mm_h, dt_min=5, catchment_m2_per_m=110.0, drain_m3_s=0.015, min_depth=0.8):
    """Water level per basin over time: (list of (t_min, {basin: level}), basins tracked).

    Each basin takes rain from the plateau above its own passages (catchment per metre of passage), loses a
    little through the floor of its sink, and once full to its spill height passes the excess on to the basin
    on the far side of the spill point, so water works its way down the cave.
    """
    members = collections.defaultdict(list)
    for i, b in enumerate(lab): members[b].append(i)
    seglen = [sum(L for _, L, _ in cave.adj[i]) / 2 for i in range(cave.n)]
    area = [math.pi / 4 * max(s["w"], 0.3) * max(min(s["h"], 6.0), 0.3) for s in cave.S]
    # downstream basin = the basin across the lowest saddle
    down = {}
    for i in range(cave.n):
        for j, _, _ in cave.adj[i]:
            b = lab[i]
            if lab[j] != b and b in spill and abs(max(cave.floor[i], cave.floor[j]) - spill[b]) < 1e-9 and cave.floor[j] <= cave.floor[i] + 1e-9:
                down.setdefault(b, lab[j])
    keep = {b for b in members if b in spill}
    # plateau water reaches the cave through its shafts, so the upper series stay fairly dry and the deep
    # cave takes most of it: scale the catchment by depth below the entrance
    top = max(cave.z[i] for i in cave.entrances)
    depthf = lambda i: min(1.0, max(0.03, (top - cave.floor[i] - 60) / 400))
    catch = {b: catchment_m2_per_m * sum(seglen[i] * depthf(i) for i in members[b]) for b in keep}
    def vol(b, h):
        v = 0.0
        for i in members[b]:
            f, r = cave.floor[i], cave.roof[i]
            if h > f: v += area[i] * seglen[i] * min(1.0, (h - f) / max(r - f, 0.2))
        return v
    vmax = {b: vol(b, spill[b]) for b in keep}
    order = sorted(keep, key=lambda b: -spill[b])          # highest basins first so overflow cascades down
    V = {b: 0.0 for b in keep}; level = {b: cave.floor[b] for b in keep}
    out = []
    steps = int(len(rain_mm_h) * 60 / dt_min)
    for k in range(steps + 1):
        t = k * dt_min
        rain = rain_mm_h[min(int(t // 60), len(rain_mm_h) - 1)] / 1000 / 3600
        extra = collections.defaultdict(float)
        for b in order:
            V[b] = max(0.0, V[b] + (catch[b] * rain - drain_m3_s) * dt_min * 60 + extra[b])
            if V[b] > vmax[b]:
                if b in down and down[b] in keep: extra[down[b]] += V[b] - vmax[b]
                V[b] = vmax[b]
        snap = {}
        for b in keep:
            if spill[b] - cave.floor[b] < min_depth: continue
            if V[b] >= vmax[b]: level[b] = spill[b]
            elif V[b] <= 0: level[b] = cave.floor[b]
            else:
                lo, hi = cave.floor[b], spill[b]
                for _ in range(20):
                    mid = (lo + hi) / 2
                    if vol(b, mid) < V[b]: lo = mid
                    else: hi = mid
                level[b] = lo
            snap[b] = round(level[b], 3)
        out.append((t, snap))
    return out, {b for b in keep if spill[b] - cave.floor[b] >= min_depth}
