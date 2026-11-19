"""The ground: the real OMDB taxiway network (OpenStreetMap), stands, and the runways as places aircraft occupy.

Aircraft taxi along graph edges at taxi speed and stop behind traffic they can see. A path that touches a runway
junction (where a taxiway meets a runway) needs a tower clearance: without one the aircraft stops at the holding
point before it. In low visibility, pilots see less far, so they close up on traffic ahead before they stop.
"""
import heapq
import json
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
TAXI_KT = 16.0           # straight taxiway speed
TAXI_LVP_KT = 10.0
TURN_KT = 10.0
FOLLOW_M = 90.0          # pilots keep this far behind the aircraft ahead in good visibility
FOLLOW_LVP_M = 60.0
KT_MS = 0.514444
CROSSING_PENALTY_M = 2500.0
OPPOSING_PENALTY_M = 3000.0
LOOKAHEAD_EDGES = 6


class Ground:
    def __init__(self, path=None):
        with open(path or os.path.join(HERE, "omdb_ground.json")) as f:
            g = json.load(f)
        self.xy = {int(k): tuple(v) for k, v in g["nodes"].items()}
        self.adj = {}
        self.edge_ref = {}
        for a, b, L, ref in g["edges"]:
            self.adj.setdefault(a, []).append((b, L))
            self.adj.setdefault(b, []).append((a, L))
            self.edge_ref[(a, b)] = self.edge_ref[(b, a)] = ref
        self.junction = {int(k): v for k, v in g["junctions"].items()}
        self.cluster_of = {j: v.get("cluster") for j, v in self.junction.items()}
        self.holds = {int(k): v for k, v in g["holds"].items()}
        self.hold_to_junction = {}
        for j, hs in self.holds.items():
            for h in hs:
                self.hold_to_junction.setdefault(h, j)
        self.exits = {r: [(n, d, rapid) for n, d, rapid in v] for r, v in g["exits"].items()}
        self.entries = {r: [(n, d, full) for n, d, full in v] for r, v in g["entries"].items()}
        self.stands = g["stands"]
        self.stand_by_node = {}
        for sid, s in self.stands.items():
            self.stand_by_node.setdefault(s["node"], sid)
        self.closed_edges = set()
        self.closed_nodes = set()
        self.closed_stands = set()

    # -- paths -----------------------------------------------------------------------------------------------
    def path(self, a, b, avoid_runway_body=None, via_refs=None, allow_junctions=True, opposing=None, avoid_refs=None):
        """Shortest path a -> b. via_refs: taxiway refs the path must use, in order (soft: each leg is a shortest
        path to the nearest node of that taxiway)."""
        if via_refs:
            out = [a]
            cur = a
            for ref in via_refs:
                nodes = {n for (x, y), r in self.edge_ref.items() if r == ref for n in (x, y)}
                if not nodes:
                    return None
                leg = self._dijkstra(cur, nodes, avoid_runway_body, opposing, avoid_refs)
                if leg is None:
                    return None
                out += leg[1:]
                cur = leg[-1]
            leg = self._dijkstra(cur, {b}, avoid_runway_body, opposing, avoid_refs)
            return None if leg is None else out + leg[1:]
        return self._dijkstra(a, {b}, avoid_runway_body, opposing, avoid_refs)

    def _dijkstra(self, a, targets, avoid_body=None, opposing=None, avoid_refs=None):
        dist = {a: 0.0}
        prev = {}
        pq = [(0.0, a)]
        while pq:
            d, u = heapq.heappop(pq)
            if u in targets:
                p = [u]
                while u in prev:
                    u = prev[u]
                    p.append(u)
                return p[::-1]
            if d > dist.get(u, 1e18):
                continue
            for v, L in self.adj.get(u, ()):
                if (u, v) in self.closed_edges or v in self.closed_nodes:
                    continue
                if avoid_refs and self.edge_ref.get((u, v)) in avoid_refs:
                    continue
                if avoid_body and v in self.junction and self.junction[v]["body"] in avoid_body and v not in targets:
                    continue
                nd = d + L
                if v in self.junction and u not in self.junction and v not in targets:
                    nd += CROSSING_PENALTY_M   # standard routes cross a runway only when they must
                if opposing and (v, u) in opposing:
                    nd += OPPOSING_PENALTY_M   # an aircraft is coming the other way along this edge
                if nd < dist.get(v, 1e18):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd, v))
        return None

    def length(self, path):
        return sum(self._edge_len(a, b) for a, b in zip(path, path[1:]))

    def _edge_len(self, a, b):
        for v, L in self.adj.get(a, ()):
            if v == b:
                return L
        ax, ay = self.xy[a]
        bx, by = self.xy[b]
        return math.hypot(ax - bx, ay - by)

    def runway_nodes_on(self, path):
        """The runway junctions a path touches, in order, with the runway body (N or S)."""
        return [(i, n, self.junction[n]["body"]) for i, n in enumerate(path) if n in self.junction]

    def hold_before(self, path, idx):
        """Index of the node where an aircraft stops before path[idx] (a runway junction): the last holding point
        before it, or the node just before it."""
        for k in range(idx - 1, max(-1, idx - 8), -1):
            if path[k] in self.hold_to_junction:
                return k
            if path[k] in self.junction:
                break
        return max(0, idx - 1)


class Mover:
    """An aircraft moving on the ground along a node path."""
    __slots__ = ("path", "i", "s", "speed", "stop_at", "cleared_to", "waiting_for")

    def __init__(self, path):
        self.path = path
        self.i = 0          # current edge is path[i] -> path[i+1]
        self.s = 0.0        # metres along the current edge
        self.speed = 0.0
        self.stop_at = None  # path index to stop at (a holding point), or None
        self.cleared_to = None
        self.waiting_for = None

    def node(self):
        return self.path[min(self.i, len(self.path) - 1)]

    def done(self):
        return self.i >= len(self.path) - 1


def pos_xy(ground, m):
    if m.done():
        return ground.xy[m.path[-1]]
    a, b = m.path[m.i], m.path[m.i + 1]
    ax, ay = ground.xy[a]
    bx, by = ground.xy[b]
    L = max(0.1, ground._edge_len(a, b))
    f = min(1.0, m.s / L)
    return (ax + (bx - ax) * f, ay + (by - ay) * f)
