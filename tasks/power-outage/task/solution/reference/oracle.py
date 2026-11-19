"""Reference restoration controller (the task's oracle). One instance per area, using only what that area's
control room has: the public grid map, its own private data, its own observations and its messages.

Strategy, a textbook bottom-up restoration:
1. Open every closed breaker and feeder in the area (the "all-open" start), so nothing is energised by accident.
2. Areas with a black-start unit start it and grow an island outward along the shortest paths to the units
   that need cranking power, the nuclear plant's station service and the tie lines.
3. Start every unit the island reaches, pick up load in small blocks that the island's governors can absorb,
   keep one unit on AGC, keep thermal units above minimum load and voltages in range with the reactors.
4. Energise dead neighbours across tie lines (cranking paths), and join live islands with a small
   frequency bias so the synchro-check relay finds its window. Tie closes are agreed by message: a proposal
   names the step, the neighbour acks, both close in that step.
"""
import heapq
import json
import math


SYNC = ("hydro", "diesel", "ct", "ccgt", "coal", "nuclear")


def make_controller(area, grid, private):
    return wrap(Oracle(area, grid, private), area, grid, private)


def wrap(ctl, area, grid, private, vals=None, items=None):
    """The controller with a handler around it for each of the area's local equipment items (or those of `items`),
    the first item's innermost; the handlers of one area share a dict. `vals`: {item id: its parameters}."""
    shared = {}
    for it in (private.get("equipment") or []) if items is None else items:
        ctl = Handler(ctl, area, grid, private, (vals or {}).get(it["id"]), item=it, shared=shared)
    return ctl


class Oracle:
    def __init__(self, area, grid, private):
        self.area = area
        self.grid = grid
        self.priv = private
        self.buses = {b["id"]: b for b in grid["buses"] if b["area"] == area}
        self.branches = {b["id"]: b for b in grid["branches"] if area in b["areas"]}
        self.units = {u["id"]: u for u in grid["units"] if u["area"] == area}
        self.upriv = {u["id"]: u for u in private["units"]}
        self.feeders = {l["id"]: l for l in grid["loads"] if l["area"] == area}
        self.shunts = {s["id"]: s for s in grid["shunts"] if s["area"] == area}
        self.adj = {}
        for b in self.branches.values():
            if b["tie"]:
                continue
            self.adj.setdefault(b["from_bus"], []).append((b["to_bus"], b["id"], b["km"]))
            self.adj.setdefault(b["to_bus"], []).append((b["from_bus"], b["id"], b["km"]))
        self.ties = {k: b for k, b in self.branches.items() if b["tie"]}
        self.opened = False
        self.pending = {}        # tie -> step agreed to close
        self.proposed = {}       # tie -> step proposed (waiting for ack)
        self.bad = set()         # branches not to try again
        self.tries = {}
        self.last_close_step = -10

    # ----------------------------------------------------------------- helpers
    def _live(self, obs, bus):
        b = obs["buses"].get(bus, {})
        return bool(b.get("live"))

    def _path_next(self, obs, targets):
        """Next branch to close: Dijkstra from the live buses of the main island toward the nearest target."""
        live = [b for b in self.buses if self._live(obs, b)]
        if not live:
            return None
        dist = {b: 0.0 for b in live}
        first = {}
        pq = [(0.0, b, None) for b in live]
        heapq.heapify(pq)
        while pq:
            d, b, fb = heapq.heappop(pq)
            if d > dist.get(b, 1e18) + 1e-9:
                continue
            if b in targets and not self._live(obs, b):
                return fb
            for nb, br, km in self.adj.get(b, []):
                st = obs["branches"].get(br, {})
                if br in self.bad or st.get("fault_found") or st.get("failed_to_operate"):
                    continue
                nd = d + km
                if self._live(obs, nb):
                    continue
                if nd < dist.get(nb, 1e18):
                    dist[nb] = nd
                    first_br = fb if fb is not None else br
                    heapq.heappush(pq, (nd, nb, first_br if fb is not None else br))
        return None

    # ----------------------------------------------------------------- main
    def step(self, obs, inbox):
        acts, out = [], []
        step = obs["step"]
        sw = 0

        def do(a, switching=True):
            nonlocal sw
            if switching:
                if sw >= 12:
                    return False
                sw += 1
            acts.append(a)
            return True

        # messages: tie proposals and acks
        for m in inbox:
            try:
                msg = json.loads(m["text"])
            except Exception:
                continue
            tie = msg.get("tie")
            if tie not in self.ties:
                continue
            if msg.get("kind") == "propose":
                if self._tie_ok(obs, tie, as_receiver=True):
                    self.pending[tie] = msg["step"]
                    out.append(dict(to=m["sender"], text=json.dumps(dict(kind="ack", tie=tie, step=msg["step"]))))
                else:
                    out.append(dict(to=m["sender"], text=json.dumps(dict(kind="nack", tie=tie))))
            elif msg.get("kind") == "ack" and self.proposed.get(tie) == msg.get("step"):
                self.pending[tie] = msg["step"]
                self.proposed.pop(tie, None)
            elif msg.get("kind") == "nack":
                self.proposed.pop(tie, None)

        # phase 0: all-open (again whenever the whole area has gone dark)
        if self.opened and not any(self._live(obs, b) for b in self.buses) and not any(
                obs["units"][u]["state"] == "starting" for u in obs["units"]):
            if any(st.get("closed") for st in obs["branches"].values()) or any(st.get("on") for st in obs["feeders"].values()):
                self.opened = False
        if not self.opened:
            for br, st in obs["branches"].items():
                if st.get("closed"):
                    do(dict(op="open", branch=br))
            for f, st in obs["feeders"].items():
                if st.get("on"):
                    do(dict(op="feeder", feeder=f, on=False))
            if sw == 0:
                self.opened = True
            else:
                return acts, out

        live_buses = [b for b in self.buses if self._live(obs, b)]
        units = obs["units"]
        # black start
        if not live_buses:
            bs = [u for u in self.units.values() if u["black_start"] and units[u["id"]]["state"] in ("off",)
                  and "locked_until_min" not in units[u["id"]]]
            starting = any(units[u]["state"] == "starting" for u in units)
            if bs and not starting:
                u = max(bs, key=lambda u: u["pmax"])
                do(dict(op="start", unit=u["id"]))
        # online units: dispatch and AGC
        online = [u for u in self.units.values() if units[u["id"]]["state"] == "online" and u["kind"] not in ("wind", "solar")]
        by_island = {}
        for u in online:
            isl = obs["buses"].get(u["bus"], {}).get("island")
            by_island.setdefault(isl, []).append(u)
        island_load = {}
        for f, st in obs["feeders"].items():
            if st.get("on") and st.get("live"):
                isl = obs["buses"][self.feeders[f]["bus"]].get("island")
                island_load[isl] = island_load.get(isl, 0.0) + st["mw"]
        for isl, us in by_island.items():
            # flexible units hold frequency on AGC; thermal units carry their share above minimum load
            cap = sum(u["pmax"] for u in us)
            gen = sum(units[u["id"]]["p"] for u in us)
            flex = [u for u in us if u["kind"] in ("hydro", "ct", "diesel")]
            for u in (flex or [max(us, key=lambda u: u["pmax"])]):
                if not units[u["id"]]["agc"]:
                    do(dict(op="agc", unit=u["id"], on=True, hz=60.0), switching=False)
            if step % 4 == 0:
                for u in us:
                    if u in flex or units[u["id"]]["agc"]:
                        continue
                    pmin = self.upriv[u["id"]]["pmin"]
                    tgt = min(max(u["pmax"] * gen / max(cap, 1), pmin * 1.1), u["pmax"] * 0.95)
                    if abs(tgt - units[u["id"]]["p_target"]) > 2:
                        do(dict(op="set_p", unit=u["id"], mw=round(tgt, 1)), switching=False)
        # start units on live buses, one at a time per island, when the island can carry the cranking load
        cap_i, load_i = {}, {}
        for u in self.units.values():
            st = units[u["id"]]
            isl = obs["buses"].get(u["bus"], {}).get("island")
            if st["state"] == "online" and u["kind"] not in ("wind", "solar"):
                cap_i[isl] = cap_i.get(isl, 0) + u["pmax"]
                load_i[isl] = load_i.get(isl, 0) + st["p"]
            if st["state"] == "starting":
                load_i[isl] = load_i.get(isl, 0) + self.upriv[u["id"]]["crank_mw"]
        def start_min(u):
            p = self.upriv[u["id"]]
            return p["start_hot"] if units[u["id"]].get("hot") else p["start_cold"]
        for u in sorted(self.units.values(), key=start_min):
            st = units[u["id"]]
            isl = obs["buses"].get(u["bus"], {}).get("island")
            if st["state"] == "off" and u["kind"] not in ("wind", "solar", "nuclear") and self._live(obs, u["bus"]) \
                    and "locked_until_min" not in st and st.get("energy_left_mwh", 1) > 0:
                crank = self.upriv[u["id"]]["crank_mw"]
                fb = obs["buses"].get(u["bus"], {}).get("f") or 0
                if isl not in cap_i:
                    # energised from a neighbour: trust its island for one start at a time
                    ok = fb >= 59.95 and crank <= 5 and not any(units[v]["state"] == "starting" for v in units)
                else:
                    ok = cap_i[isl] * 0.8 - load_i.get(isl, 0) >= crank
                if ok:
                    if do(dict(op="start", unit=u["id"])):
                        load_i[isl] = load_i.get(isl, 0) + crank
        # voltage
        for b, st in obs["buses"].items():
            if st.get("live") and st.get("v", 1.0) > 1.07:
                for s, sh in self.shunts.items():
                    if not obs["shunts"][s]["on"] and self._live(obs, sh["bus"]):
                        do(dict(op="shunt", shunt=s, on=True))
                        break
                break
        vmax = max((st.get("v", 0) for st in obs["buses"].values() if st.get("live")), default=0)
        if vmax and vmax < 0.98:
            for s, sh in self.shunts.items():
                if obs["shunts"][s]["on"]:
                    do(dict(op="shunt", shunt=s, on=False))
                    break
        # load pickup, per island, within the island's headroom
        cap_by_isl, p_by_isl = {}, {}
        for u in online:
            isl = obs["buses"][u["bus"]].get("island")
            cap_by_isl[isl] = cap_by_isl.get(isl, 0) + u["pmax"]
            p_by_isl[isl] = p_by_isl.get(isl, 0) + units[u["id"]]["p"]
        f_by_isl = {obs["buses"][u["bus"]].get("island"): obs["buses"][u["bus"]].get("f") for u in online}
        cl = self.priv["cold_load_pickup"]["factor"]
        vmin_i, lmax = {}, 0.0
        for b, st in obs["buses"].items():
            if st.get("live"):
                vmin_i[st["island"]] = min(vmin_i.get(st["island"], 9), st["v"])
        for br, st in obs["branches"].items():
            lmax = max(lmax, st.get("loading", 0) or 0)
        if lmax > 1.1:
            # relieve an overloaded line: drop the largest non-priority feeder in the area
            on = [(obs["feeders"][f]["mw"], f) for f, fl in self.feeders.items()
                  if obs["feeders"].get(f, {}).get("on") and not fl["priority"]]
            if on:
                do(dict(op="feeder", feeder=max(on)[1], on=False))
        for isl, cap in cap_by_isl.items():
            f = f_by_isl.get(isl) or 60.0
            if f < 59.85 or vmin_i.get(isl, 1) < 0.95 or lmax > 0.85:
                continue
            load = island_load.get(isl, 0.0)
            head = 0.72 * cap - load
            # largest step the island can take: a share of online capacity
            stepmax = 0.07 * cap
            cands = []
            for fid, fl in self.feeders.items():
                st = obs["feeders"].get(fid, {})
                if st.get("on") or not st.get("live"):
                    continue
                if obs["buses"][fl["bus"]].get("island") != isl:
                    continue
                cands.append(((0 if fl["priority"] else 1), -st.get("demand_mw", fl["mw"]), fid, st.get("demand_mw", fl["mw"])))
            cands.sort()
            budget = min(head, stepmax)
            for _, _, fid, mw in cands:
                if mw * cl <= budget:
                    if do(dict(op="feeder", feeder=fid, on=True)):
                        budget -= mw * cl
        # grow the island one branch a step
        # one energisation every 10 steps: the control room's 5-minute interval (MANUAL, Energising dead network)
        if live_buses and step - self.last_close_step >= 10:
            stage_a = set()
            for u in self.units.values():
                if u["kind"] not in ("wind", "solar", "nuclear") and units[u["id"]]["state"] == "off":
                    stage_a.add(u["bus"])
                if u["kind"] == "nuclear" and not units[u["id"]].get("offsite_power"):
                    stage_a.add(u["bus"])
            for t, tb in self.ties.items():
                st = obs["branches"].get(t, {})
                if not st.get("other_side", {}).get("live") and not st.get("fault_found"):
                    stage_a.add(st.get("this_side", {}).get("bus"))
            stage_b = set(fl["bus"] for fl in self.feeders.values())
            nuke = {u["bus"] for u in self.units.values()
                    if u["kind"] == "nuclear" and not units[u["id"]].get("offsite_power")}
            br = (self._path_next(obs, nuke) if nuke else None) or self._path_next(obs, stage_a) \
                or self._path_next(obs, stage_b)
            if br:
                self.tries[br] = self.tries.get(br, 0) + 1
                if self.tries[br] > 3:
                    self.bad.add(br)
                elif do(dict(op="close", branch=br)):
                    self.last_close_step = step
        # ties: agreed closes, new proposals
        for tie, k in list(self.pending.items()):
            if k == step:
                do(dict(op="close", branch=tie))
                self.pending.pop(tie)
                self.tries[tie] = self.tries.get(tie, 0) + 1
            elif k < step:
                self.pending.pop(tie)
        for tie, k in list(self.proposed.items()):
            if k <= step:
                self.proposed.pop(tie)      # no answer in time (messages take two steps each way)
        for tie, b in self.ties.items():
            st = obs["branches"].get(tie, {})
            if st.get("closed") or st.get("fault_found") or tie in self.pending or tie in self.proposed:
                continue
            if self.tries.get(tie, 0) > 6:
                continue
            if self._tie_ok(obs, tie, as_receiver=False) and len(out) < min(2, obs.get("messages_allowed", 0)):
                self.proposed[tie] = step + 6
                out.append(dict(to=st["neighbour"], text=json.dumps(dict(kind="propose", tie=tie, step=step + 6))))
        # frequency bias for joins: if a live tie is pending, the smaller island runs slightly fast
        for tie in list(self.pending) + list(self.proposed):
            st = obs["branches"].get(tie, {})
            if st.get("this_side", {}).get("live") and st.get("other_side", {}).get("live"):
                for u in online:
                    if units[u["id"]]["agc"]:
                        hz = 60.05 if self.area < st["neighbour"] else 60.0
                        do(dict(op="agc", unit=u["id"], on=True, hz=hz), switching=False)
        return acts, out

    def _tie_ok(self, obs, tie, as_receiver):
        st = obs["branches"].get(tie, {})
        if st.get("closed") or st.get("fault_found"):
            return False
        mine, theirs = st.get("this_side", {}), st.get("other_side", {})
        ml, tl = mine.get("live"), theirs.get("live")
        if ml and not tl:
            # energise the neighbour from here if this island has room
            cap = sum(u["pmax"] for u in self.units.values()
                      if obs["units"][u["id"]]["state"] == "online" and u["kind"] not in ("wind", "solar")
                      and obs["buses"][u["bus"]].get("island") == obs["buses"][mine["bus"]].get("island"))
            return cap >= 150
        if tl and not ml:
            return True     # be energised from the neighbour
        if ml and tl:
            if mine.get("f") is None or theirs.get("f") is None:
                return False
            return abs(mine["f"] - theirs["f"]) < 0.12 and abs((mine.get("v") or 1) - (theirs.get("v") or 1)) < 0.06
        return False


class Handler:
    """An area's controller plus handling of its own local equipment item (MANUAL.md; the item's text in area_data).
    `vals`: the item's parameters as the control room knows them, by default the middle of each range (a drill
    teaches the rest)."""

    def __init__(self, inner, area, pub, private, vals=None, item=None, shared=None):
        self.inner, self.area = inner, area
        self.it = item or private["equipment"][0]
        self.shared = {} if shared is None else shared
        self.k = self.it["kind"]
        self.v = vals or {p: (lo + hi) / 2.0 for p, (lo, hi) in self.it["ranges"].items()}
        self.xy = {b["id"]: (b["x"], b["y"]) for b in pub["buses"]}
        self.priv = private
        self.pmax = {u["id"]: u["pmax"] for u in pub["units"]}
        self.kind = {u["id"]: u["kind"] for u in pub["units"]}
        self.ubus = {u["id"]: u["bus"] for u in pub["units"]}
        self.fbus = {l["id"]: l["bus"] for l in pub["loads"]}
        self.fmw = {l["id"]: l["mw"] for l in pub["loads"]}
        self.upriv = {u["id"]: u for u in private["units"]}
        self.ends = {b["id"]: (b["from_bus"], b["to_bus"]) for b in pub["branches"]}
        self.adj = {}
        for b in pub["branches"]:
            if area in b["areas"]:
                for x, y in ((b["from_bus"], b["to_bus"]), (b["to_bus"], b["from_bus"])):
                    self.adj.setdefault(x, []).append((y, b["id"]))
        self.my_buses = {b["id"] for b in pub["buses"] if b["area"] == area}
        self.ties = {b["id"] for b in pub["branches"] if b["tie"] and area in b["areas"]}
        self.s = {}                       # handler state
        self.hist = []
        self.qmin = {u["id"]: -u.get("qmin", 0.0) for u in private["units"]}
        if self.k == "dual_fuel":
            # the combined cycles trip below the pipeline's trip pressure: the linepack handler keeps a reserve above it
            pk = next((x for x in private["equipment"] if x["kind"] == "gas_linepack"), None)
            if pk:
                self.shared["pack_reserve_pct"] = self.v["trip_pct"] + 8.0

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def strip(acts, pred):
        return [x for x in acts if not (isinstance(x, dict) and pred(x))]

    def own_cap(self, obs, island=None):
        return sum(self.pmax[u] for u, d in obs["units"].items() if d.get("state") == "online"
                   and self.kind.get(u) in SYNC and (island is None or
                                                     obs["buses"].get(self.ubus[u], {}).get("island") == island))

    def headroom(self, obs):
        return sum(self.pmax[u] - d.get("p", 0.0) for u, d in obs["units"].items()
                   if d.get("state") == "online" and self.kind.get(u) in SYNC)

    def tie_links(self, obs):
        """Ties closed with both sides live: the island reaches a neighbour."""
        return sum(1 for t in self.ties if obs["branches"].get(t, {}).get("closed")
                   and obs["branches"][t].get("this_side", {}).get("live")
                   and obs["branches"][t].get("other_side", {}).get("live"))

    def strength(self, obs):
        """A lower-bound guess at the island's online capacity: own units, plus 250 MW per live tie."""
        return self.own_cap(obs) + 250.0 * self.tie_links(obs)

    def live(self, obs, bus):
        return bool(obs["buses"].get(bus, {}).get("live"))

    def hz(self, obs, bus):
        return obs["buses"].get(bus, {}).get("f") or 0.0

    def dead_section(self, obs, branch):
        """The dead buses (of this area) a close of `branch` would energise, from the map and closed flags."""
        a, b = self.ends[branch]
        start = [x for x in (a, b) if x in self.my_buses and not self.live(obs, x)]
        seen = set(start)
        stack = list(start)
        while stack:
            i = stack.pop()
            for j, br in self.adj.get(i, []):
                if j in seen or j not in self.my_buses or not obs["branches"].get(br, {}).get("closed"):
                    continue
                if self.live(obs, j):
                    continue
                seen.add(j)
                stack.append(j)
        return seen

    def path_step(self, obs, target):
        """The open breaker to close next on the shortest way from a live bus of this area to dead `target`: a
        breadth-first search from the target through dead buses, inside the area."""
        if self.live(obs, target):
            return None
        seen = {target}
        frontier = [target]
        while frontier:
            nxt = []
            for i in frontier:
                for j, br in self.adj.get(i, []):
                    if j not in self.my_buses or br in self.ties or j in seen:
                        continue
                    d = obs["branches"].get(br, {})
                    if self.live(obs, j):
                        if not d.get("closed") and not d.get("fault_found") and not d.get("failed_to_operate"):
                            return br
                        continue
                    seen.add(j)
                    nxt.append(j)
            frontier = nxt
        return None

    def demand(self, obs, f):
        return obs["feeders"].get(f, {}).get("demand_mw", 0.0)

    def clpu(self, obs):
        c = self.priv["cold_load_pickup"]
        sev = obs["weather"]["cold_load_severity"]
        h = obs["t_min"] / 60.0
        return 1.0 + (c["factor"] - 1.0) * sev * min(1.0, h / 4.0)

    # ------------------------------------------------------------------ the step
    def step(self, obs, inbox):
        acts, out = self.inner.step(obs, inbox)
        acts = [x for x in (acts or []) if isinstance(x, dict)]
        extra = []
        try:
            acts, extra = getattr(self, "h_" + self.k)(obs, acts, extra)
        except Exception as e:                     # a handler bug must not stop the area
            self.s["err"] = f"{self.k}: {e!r}"
        # the handler's commands first: what it adds is what its item needs now (the area has 12 switching
        # operations a step; the controller's commands that do not fit are tried again next step)
        return extra + acts, out

    def equip(self, **kw):
        return dict(op="equip", id=self.it["id"], **kw)

    # ------------------------------------------------------------------ one per kind
    def h_hydro_cascade(self, obs, acts, extra):
        it, up, low = self.it, self.it["upper"], self.it["lower"]
        self.hist.append(obs["units"][up].get("p", 0.0))
        lag = int(round(self.v["travel_min"] * 2))
        inflow = (self.hist[-lag - 1] if len(self.hist) > lag else 0.0) + it["inflow_mw"]
        pct = obs["equipment"][it["id"]]["pond_pct"]
        if pct is None:                            # no crew at the dam: dead-reckon from the last reading
            pct = self.s.get("pond", 100 * self.v["pond_start"])
            pct += (inflow - obs["units"][low].get("p", 0.0)) * 0.5 / 60.0 / self.v["pond_mwh"] * 100
        self.s["pond"] = pct
        lvl = pct / 100.0 * self.v["pond_mwh"]
        # run the lower plant on the inflow, spending the pond over the next 20 minutes, never below 1.5 MWh
        cap = max(0.0, inflow + max(0.0, lvl - 1.5) * 3.0 - 8.0)
        acts = self.strip(acts, lambda x: x.get("unit") == low and x.get("op") in ("set_p", "agc"))
        u = obs["units"][low]
        if u.get("state") == "online":
            extra += [dict(op="agc", unit=low, on=False), dict(op="set_p", unit=low, mw=round(min(cap, self.pmax[low]), 1))]
        return acts, extra

    def _centre_feeders(self, obs):
        return [f for f in self.it["feeders"] if f in obs["feeders"]]

    def h_urban_network(self, obs, acts, extra):
        t = obs["t_min"]
        fs = set(self._centre_feeders(obs))
        picks = self.s.setdefault("picks", [])
        # sectionalise first: open every closed centre feeder on a dead bus while remote control lasts
        acts = self.strip(acts, lambda x: x.get("op") == "feeder" and x.get("feeder") in fs and x.get("on"))
        n = 0
        for f in sorted(fs):
            d = obs["feeders"][f]
            if d.get("on") and not d.get("live") and n < 3:
                extra.append(dict(op="feeder", feeder=f, on=False))
                n += 1
        st = obs["equipment"][self.it["id"]]
        self.shared["urban"] = (picks, self.v["window_min"], self.v["block_mw"] * 0.85)
        q0, q1 = self.shared.get("quiet", (0, -1))
        if st.get("blocked_until") or q0 <= t <= q1:
            return acts, extra
        w, budget = self.v["window_min"], self.v["block_mw"] * 0.85
        picks[:] = [(tt, m) for tt, m in picks if t - tt < w + 0.6]
        room = budget - sum(m for _, m in picks)
        k = self.clpu(obs)
        for f in sorted(fs, key=lambda f: -self.demand(obs, f)):
            d = obs["feeders"][f]
            if d.get("on") or not d.get("live"):
                continue
            if self.hz(obs, self.fbus[f]) < 59.85:
                break
            mw = d.get("demand_mw", 0.0) * k
            if mw <= room:
                extra.append(dict(op="feeder", feeder=f, on=True))
                picks.append((t, mw))
                room -= mw
        return acts, extra

    def h_nuclear_aux(self, obs, acts, extra):
        st = obs["equipment"][self.it["id"]]
        f = self.it["feeder"]
        fd = obs["feeders"].get(f, {})
        if fd.get("live") and not fd.get("on") and not st.get("locked_until"):
            extra.append(dict(op="feeder", feeder=f, on=True))
        if st["stage"] in (1, 2) and st.get("since_min") is not None:
            n = st["stage"] + 1
            need = self.v["reserve_mw"] * (1.0 if n == 2 else 1.5) * 1.15
            bus = self.fbus[f]
            isl = obs["buses"].get(bus, {}).get("island")
            res = sum(min(self.upriv[u]["gov_reserve"] * self.pmax[u], self.pmax[u] - d.get("p", 0.0))
                      for u, d in obs["units"].items() if d.get("state") == "online" and self.kind[u] in SYNC
                      and obs["buses"].get(self.ubus[u], {}).get("island") == isl)
            res += 60.0 * self.tie_links(obs)
            if obs["t_min"] - st["since_min"] >= self.v["hold_min"] + 1 and abs(self.hz(obs, bus) - 60) <= 0.05 \
                    and res >= need:
                extra.append(self.equip(stage=n))
        return acts, extra

    def h_lcc_hvdc(self, obs, acts, extra):
        it = self.it
        st = obs["equipment"][it["id"]]
        b = obs["buses"].get(it["bus"], {})
        cap = self.strength(obs)
        hz = b.get("f") or 60.0
        picked = self.s.setdefault("picked", {})
        if st.get("blocked_until") or not b.get("live") or cap < self.v["scr"] * self.v["min_mw"] * 1.3:
            picked.clear()
            if st.get("target", 0) > 0:
                extra.append(self.equip(mw=0))
            return acts, extra
        lim = min(it["mw_max"], cap / (self.v["scr"] * 1.2))
        served = sum(obs["feeders"].get(f, {}).get("mw", 0.0) for f in picked)
        # pick up dark feeders as the import arrives so the area's own units keep their output
        if st.get("mw", 0.0) >= served - 5 and served < lim and hz < 60.05:
            room = min(12.0 if served else self.v["min_mw"] * 1.1, lim - served)
            for fid, f in sorted(obs["feeders"].items(), key=lambda x: -x[1].get("demand_mw", 0)):
                if room <= 0:
                    break
                if f.get("live") and not f.get("on") and f.get("demand_mw", 0) <= room:
                    extra.append(dict(op="feeder", feeder=fid, on=True))
                    picked[fid] = f["demand_mw"]
                    room -= f["demand_mw"]
        want = min(lim, max(sum(picked.values()), self.v["min_mw"] if picked else 0.0))
        if hz > 60.15:
            want = max(0.0, st.get("mw", 0.0) - 10)
        banks = min(it["banks"], max(1, math.ceil((max(want, st.get("mw", 0.0)) + 15) / (self.v["bank_mw"] * 0.95))))
        if banks != st.get("banks"):
            extra.append(self.equip(banks=banks))
        want = min(want, banks * self.v["bank_mw"] * 0.95)
        if abs(want - st.get("target", 0.0)) >= 2:
            extra.append(self.equip(mw=round(want, 1)))
        return acts, extra

    def h_retrofit_crank(self, obs, acts, extra):
        u = self.it["unit"]
        bus = self.ubus[u]
        d = obs["units"][u]
        st = obs["equipment"][self.it["id"]]
        if d.get("state") in ("online",) or self.live(obs, bus) or obs["t_min"] > 120 or \
                any(b.get("live") for b in obs["buses"].values()):
            return acts, extra
        # keep the dead network at its bus small until it is online
        mine = [br for _, br in self.adj.get(bus, [])]
        acts = self.strip(acts, lambda x: x.get("op") == "close" and x.get("branch") in mine)
        n = 0
        for br in mine:
            if obs["branches"].get(br, {}).get("closed") and n < 3:
                extra.append(dict(op="open", branch=br))
                n += 1
        if d.get("state") == "off" and not d.get("locked_until") and n == 0:
            ready = st.get("diesel_ready_from")
            if ready is None or obs["t_min"] >= ready:
                extra.append(dict(op="start", unit=u))
        return acts, extra

    def h_industrial_process(self, obs, acts, extra):
        seq = self.it["sequence"]
        t = obs["t_min"]
        acts = self.strip(acts, lambda x: x.get("op") == "feeder" and x.get("feeder") in seq and x.get("on"))
        n = 0
        for f in seq:
            d = obs["feeders"].get(f, {})
            if d.get("on") and not d.get("live") and n < 3:
                extra.append(dict(op="feeder", feeder=f, on=False))
                n += 1
        st = obs["equipment"][self.it["id"]]
        if st.get("locked_until"):
            self.s["last"] = -1e9
            return acts, extra
        for i, f in enumerate(seq):
            d = obs["feeders"].get(f, {})
            if d.get("on") and d.get("live"):
                continue
            if not d.get("live") or t - self.s.get("last", -1e9) < self.v["gap_min"] + 0.6:
                break
            bus = self.fbus[f]
            isl = obs["buses"][bus].get("island")
            cap = self.own_cap(obs, isl) + 250.0 * self.tie_links(obs)
            if abs(self.hz(obs, bus) - 60) <= 0.05 and cap >= self.v["ratio"] * d.get("demand_mw", 0) * 1.1:
                extra.append(dict(op="feeder", feeder=f, on=True))
                self.s["last"] = t
            break
        return acts, extra

    def h_district_heating(self, obs, acts, extra):
        it = self.it
        u = it["unit"]
        d = obs["units"][u]
        fs = set(self._centre_feeders(obs))
        warm = obs["equipment"][it["id"]]["heat_restored"]
        if d.get("state") == "online":
            floor = self.v["must_run"] * self.pmax[u] * 1.06 + 2
            acts = self.strip(acts, lambda x: x.get("unit") == u and x.get("op") == "agc")
            mine = [x for x in acts if x.get("unit") == u and x.get("op") == "set_p"]
            acts = self.strip(acts, lambda x: x.get("unit") == u and x.get("op") == "set_p")
            tgt = max([floor] + [float(x.get("mw", 0)) for x in mine] + [d.get("p_target", 0.0)])
            extra += [dict(op="agc", unit=u, on=False), dict(op="set_p", unit=u, mw=round(min(tgt, self.pmax[u]), 1))]
        elif d.get("state") == "off" and not d.get("locked_until"):
            extra.append(dict(op="start", unit=u))
        if not warm:
            acts = self.strip(acts, lambda x: x.get("op") == "feeder" and x.get("feeder") in fs and x.get("on"))
        return acts, extra

    def h_undervoltage_relays(self, obs, acts, extra):
        fs = set(self._centre_feeders(obs))
        vt = self.v["v_trip"]
        acts = self.strip(acts, lambda x: x.get("op") == "set_v")
        for u, d in obs["units"].items():
            if d.get("state") == "online" and self.kind[u] in SYNC and d.get("v_set", 1.0) < 1.05:
                extra.append(dict(op="set_v", unit=u, pu=1.05))
        for sh, d in obs["shunts"].items():
            b = sh.rsplit("-", 1)[0]
            if d.get("on") and (obs["buses"].get(b, {}).get("v") or 0) < 1.06:
                extra.append(dict(op="shunt", shunt=sh, on=False))
        acts = self.strip(acts, lambda x: x.get("op") == "shunt" and x.get("on")
                          and (obs["buses"].get(x.get("shunt", "").rsplit("-", 1)[0], {}).get("v") or 0) < 1.08)

        def low(x):
            if x.get("op") != "feeder" or not x.get("on") or x.get("feeder") not in fs:
                return False
            v = obs["buses"].get(self.fbus[x["feeder"]], {}).get("v") or 0.0
            return v < vt + 0.02
        return self.strip(acts, low), extra

    def h_gas_linepack(self, obs, acts, extra):
        it = self.it
        st = obs["equipment"][it["id"]]
        f = it["feeder"]
        fd = obs["feeders"].get(f, {})
        if fd.get("live") and not fd.get("on"):
            extra.append(dict(op="feeder", feeder=f, on=True))
        acts = self.strip(acts, lambda x: x.get("op") == "feeder" and x.get("feeder") == f and not x.get("on"))
        units = [u for u in it["units"] if obs["units"][u].get("state") == "online"]
        if not units:
            return acts, extra
        pack = st["pressure_pct"] / 100.0 * self.v["pack_mwh"]
        res = max(40.0, self.shared.get("pack_reserve_pct", 0.0) / 100.0 * self.v["pack_mwh"])
        cap = (self.v["refill_mw"] * 0.92 if st["compressor"] else 0.0) + max(0.0, pack - res) / 1.5
        tot = sum(obs["units"][u].get("p", 0.0) for u in units)
        want = {u: max(float(x.get("mw", 0)) for x in acts if x.get("unit") == u and x.get("op") == "set_p")
                for u in units if any(x.get("unit") == u and x.get("op") == "set_p" for x in acts)}
        tgt = {u: want.get(u, obs["units"][u].get("p_target", 0.0)) for u in units}
        if max(tot, sum(tgt.values())) > cap:
            k = cap / max(1.0, max(tot, sum(tgt.values())))
            acts = self.strip(acts, lambda x: x.get("unit") in units and x.get("op") in ("set_p", "agc"))
            for u in units:
                pmin = self.upriv[u]["pmin"] if self.kind[u] == "ccgt" else 0.0
                extra += [dict(op="agc", unit=u, on=False),
                          dict(op="set_p", unit=u, mw=round(max(pmin * 1.02, tgt[u] * k), 1))]
        return acts, extra

    def h_protection_reset(self, obs, acts, extra):
        it = self.it
        st = obs["equipment"][it["id"]]
        todo = [b for b in it["buses"] if b not in st["done"] and b not in st["under_way"]]
        if not todo and not st["under_way"]:
            for c, who in list(self.shared.get("crews", {}).items()):
                if who == "protection_reset":
                    del self.shared["crews"][c]
            return acts, extra
        mine = self.shared.setdefault("crews", {})
        crews = [c for c in obs["crews"] if mine.get(c) in (None, "protection_reset")]
        acts = self.strip(acts, lambda x: x.get("op") in ("dispatch", "repair") and x.get("crew") in crews)
        claimed = self.s.setdefault("claimed", {})
        for c, d in obs["crews"].items():
            if c not in crews:
                continue
            mine[c] = "protection_reset"
            if d.get("working_on") or d.get("done_min"):
                continue
            if d.get("driving_to"):
                continue
            at = d.get("at")
            if at in todo and claimed.get(at) in (None, c):
                extra.append(self.equip(crew=c, bus=at))
                claimed[at] = c
                todo.remove(at)
                continue
            free = [b for b in todo if claimed.get(b) in (None, c)]
            if free:
                nxt = min(free, key=lambda b: self.dist(at, b))
                claimed[nxt] = c
                extra.append(dict(op="dispatch", crew=c, bus=nxt))
        return acts, extra

    def dist(self, a, b):
        return math.dist(self.xy[a], self.xy[b])

    def h_governor_mode(self, obs, acts, extra):
        u = self.it["unit"]
        bus = self.ubus[u]
        isl = obs["buses"].get(bus, {}).get("island")
        cap = self.own_cap(obs, isl)
        big = cap >= 0.8 * self.v["ratio"] * self.pmax[u] or self.tie_links(obs) > 0
        mode = obs["equipment"][self.it["id"]]["mode"]
        want = "interconnected" if big else "isolated"
        if mode != want:
            extra.append(self.equip(mode=want))
        return acts, extra

    def h_cable_centre(self, obs, acts, extra):
        cables = set(self.it["cables"])
        strong = self.strength(obs) >= self.v["strength_mw"] * 1.1

        def n_cables(br):
            sec = self.dead_section(obs, br)
            return (br in cables) + sum(1 for c in cables if c != br and obs["branches"].get(c, {}).get("closed")
                                        and all(e in sec for e in self.ends[c]))

        def risky(x):
            if x.get("op") != "close" or not self.dead_section(obs, x.get("branch")):
                return False
            n = n_cables(x.get("branch"))
            return n > 1 or (n == 1 and not strong)
        acts = self.strip(acts, risky)
        # one cable at a time: open the cables that are closed with both ends dead
        k = 0
        for c in sorted(cables):
            if obs["branches"].get(c, {}).get("closed") and not any(self.live(obs, e) for e in self.ends[c]) and k < 3:
                extra.append(dict(op="open", branch=c))
                k += 1
        return acts, extra

    def h_coal_auxiliaries(self, obs, acts, extra):
        st = obs["equipment"][self.it["id"]]["boards"]
        for u in self.it["units"]:
            d = obs["units"][u]
            if d.get("state") == "unavailable":
                continue
            bus = self.ubus[u]
            if u not in st and self.live(obs, bus) and self.headroom(obs) > self.v["aux_share"] * self.pmax[u] * 1.5:
                extra.append(self.equip(unit=u, board=True))
            elif u in st and d.get("state") == "off" and not d.get("locked_until") and \
                    obs["t_min"] - st[u]["fed_since"] >= self.v["purge_min"] + 0.5:
                extra.append(dict(op="start", unit=u))
        return acts, extra

    def h_sync_condenser(self, obs, acts, extra):
        it = self.it
        hub, cen = it["bus"], set(it["buses"])
        st = obs["equipment"][it["id"]]
        if st["state"] == "on":
            return acts, extra

        def risky(x):
            if x.get("op") != "close":
                return False
            return len(self.dead_section(obs, x.get("branch")) & cen) > 1
        acts = self.strip(acts, risky)
        # one centre substation at a time: open the lines between dead centre substations
        n = 0
        for br, (a, b) in self.ends.items():
            if a in cen and b in cen and obs["branches"].get(br, {}).get("closed") and n < 3 \
                    and not self.live(obs, a) and not self.live(obs, b):
                extra.append(dict(op="open", branch=br))
                n += 1
        if not self.live(obs, hub):
            br = self.path_step(obs, hub)
            if br and n == 0 and len(self.dead_section(obs, br) & cen) <= 1:
                extra.append(dict(op="close", branch=br))
        elif st["state"] == "off" and self.headroom(obs) > self.v["start_mw"] * 1.3:
            extra.append(self.equip(on=True))
        return acts, extra

    def h_load_restoration(self, obs, acts, extra):
        armed = obs["equipment"][self.it["id"]]["armed"]
        for b in armed:
            if obs["buses"].get(b, {}).get("telemetry"):
                extra.append(self.equip(bus=b, armed=False))
        return acts, extra

    def h_house_load(self, obs, acts, extra):
        u = self.it["unit"]
        st = obs["equipment"][self.it["id"]]
        if not st["house_load"]:
            return acts, extra
        bus = self.ubus[u]
        if not self.live(obs, bus):
            acts = self.strip(acts, lambda x: x.get("op") == "start" and x.get("unit") == u)
            br = self.path_step(obs, bus)
            if br:
                extra.append(dict(op="close", branch=br))
            return acts, extra
        if abs(self.hz(obs, bus) - 60) > 0.15:
            acts = self.strip(acts, lambda x: x.get("op") == "start" and x.get("unit") == u)
            return acts, extra
        block = self.v["block"] * self.pmax[u]
        # make room: pick up live dark feeders worth the block, and resynchronise
        room = block * 0.8
        for fid, f in sorted(obs["feeders"].items(), key=lambda x: -x[1].get("demand_mw", 0)):
            if room <= 0:
                break
            if f.get("live") and not f.get("on") and f.get("demand_mw", 0) <= room:
                extra.append(dict(op="feeder", feeder=fid, on=True))
                room -= f["demand_mw"]
        extra.insert(0, dict(op="start", unit=u))
        return acts, extra

    # ------------------------------------------------------------------ version 5: the second items
    def crew_for(self, obs, tag, n=1):
        """Reserve up to n of the area's crews for this handler (shared with the area's other handlers)."""
        mine = self.shared.setdefault("crews", {})
        have = [c for c, who in mine.items() if who == tag]
        for c in sorted(obs["crews"]):
            if len(have) >= n:
                break
            if c not in mine:
                mine[c] = tag
                have.append(c)
        return have

    def send(self, obs, c, bus, extra):
        """Keep crew c at bus: dispatch it there unless it is there or on its way."""
        d = obs["crews"].get(c, {})
        if d.get("at") != bus and d.get("driving_to") != bus and not d.get("working_on"):
            extra.append(dict(op="dispatch", crew=c, bus=bus))

    def island_of(self, obs, bus):
        return obs["buses"].get(bus, {}).get("island")

    def h_dam_crew(self, obs, acts, extra):
        it = self.it
        u, bus = it["unit"], it["bus"]
        cs = self.crew_for(obs, "dam")
        acts = self.strip(acts, lambda x: x.get("crew") in cs)
        for c in cs:
            self.send(obs, c, bus, extra)
        if not obs["equipment"][it["id"]]["local_control"]:
            # the lower plant cannot be commanded yet: black-start the upper one instead
            if any(x.get("op") == "start" and x.get("unit") == u for x in acts + extra):
                acts = self.strip(acts, lambda x: x.get("unit") == u)
                extra = self.strip(extra, lambda x: x.get("unit") == u)
                other = [v for v, d in obs["units"].items() if v != u and d.get("state") == "off"
                         and self.kind.get(v) == "hydro" and not d.get("locked_until_min")]
                if other:
                    extra.append(dict(op="start", unit=other[0]))
            else:
                acts = self.strip(acts, lambda x: x.get("unit") == u)
                extra = self.strip(extra, lambda x: x.get("unit") == u)
        return acts, extra

    def h_hospital_transfer(self, obs, acts, extra):
        it = self.it
        t = obs["t_min"]
        st = obs["equipment"][it["id"]]
        held = set(st["held"])
        fuel_end = self.v["fuel_h"] * 60.0
        if not self.s.get("init"):
            self.s["init"] = True
            for b in it["buses"]:
                if b not in held:
                    extra.append(self.equip(bus=b, hold=True))
            return acts, extra
        diesel = set(st["on_diesel"])
        picks, w, budget = self.shared.get("urban", ([], 10.0, 50.0))
        q0, q1 = self.shared.get("quiet", (0, -1))
        if t <= q1:
            return acts, extra
        r = self.v["retransfer_min"]
        # release one substation at a time when its transfer fits in the centre's pickup window
        for b in sorted(held):
            fs = [f for f in it["feeders"] if f in diesel and self.fbus.get(f) == b]
            live = [f for f in fs if obs["feeders"].get(f, {}).get("on") and obs["feeders"][f].get("live")]
            if not live and fs:
                continue
            mw = sum(self.demand(obs, f) for f in live) * self.clpu(obs)
            due = t + r
            if mw > budget or any(pt > due - w - 1.0 for pt, _ in picks):
                if t < fuel_end - r - 30:
                    continue
            extra.append(self.equip(bus=b, hold=False))
            self.shared["quiet"] = (due - w - 1.0, due + 1.5)
            picks.append((due, mw))
            break
        return acts, extra

    def h_interruptible_contracts(self, obs, acts, extra):
        it = self.it
        st = obs["equipment"][it["id"]]
        t = obs["t_min"]
        fs = set(it["feeders"])
        if st["called_min"] is None:
            extra.append(self.equip(call=True))
            return self.strip(acts, lambda x: x.get("op") == "feeder" and x.get("feeder") in fs and x.get("on")), extra
        ready = t >= st["called_min"] + self.v["notice_min"] + 0.6
        off = st.get("off_until") or {}

        def bad(x):
            if x.get("op") != "feeder" or x.get("feeder") not in fs or not x.get("on"):
                return False
            f = x["feeder"]
            return not ready or f in off or self.hz(obs, self.fbus[f]) < self.v["trip_hz"] + 0.15
        return self.strip(acts, bad), extra

    def h_converter_overvoltage(self, obs, acts, extra):
        it = self.it
        link = next(x for x in self.priv["equipment"] if x["kind"] == "lcc_hvdc")
        st = obs["equipment"].get(link["id"], {})
        bus = it["bus"]
        if not self.live(obs, bus):
            weak = self.strength(obs) < self.v["ferranti_mw"] * 1.1
            acts = self.strip(acts, lambda x: x.get("op") == "close" and weak
                              and bus in self.dead_section(obs, x.get("branch")))
            extra = self.strip(extra, lambda x: x.get("op") == "close" and weak
                               and bus in self.dead_section(obs, x.get("branch")))
            return acts, extra
        p = st.get("mw", 0.0)
        hv = getattr(self.inner, "v", {}) if isinstance(self.inner, Handler) else {}
        p_eff = p if p > 0 else hv.get("min_mw", 45.0)
        allowed = int((self.v["q_margin"] * 0.9 + link["q_per_mw"] * p_eff) // link["bank_mvar"])
        for x in acts + extra:
            if x.get("op") == "equip" and x.get("id") == link["id"]:
                if "banks" in x and x["banks"] > allowed:
                    x["banks"] = max(0, allowed)
                if "mw" in x and x["mw"] > 0:
                    nb = min(allowed, link["banks"])
                    x["mw"] = round(min(x["mw"], nb * hv.get("bank_mw", 60.0) * 0.95), 1)
        if st.get("banks", 0) > allowed:
            extra.append(self.equip_of(link, banks=max(0, allowed)))
        return acts, extra

    def equip_of(self, item, **kw):
        return dict(op="equip", id=item["id"], **kw)

    def h_steam_soak(self, obs, acts, extra):
        st = obs["equipment"][self.it["id"]]
        t = obs["t_min"]
        for u in self.it["units"]:
            d = obs["units"].get(u, {})
            until = st.get(u, {}).get("soak_until")
            if d.get("state") == "online" and (until is None or t < until + 0.5):
                cap = self.v["soak_share"] * self.pmax[u] * 0.85
                acts = self.strip(acts, lambda x: x.get("unit") == u and x.get("op") in ("set_p", "agc"))
                extra = self.strip(extra, lambda x: x.get("unit") == u and x.get("op") in ("set_p", "agc"))
                if d.get("agc") or d.get("p_target", 0) > cap:
                    extra += [dict(op="agc", unit=u, on=False), dict(op="set_p", unit=u, mw=round(cap, 1))]
        return acts, extra

    def _voltage_up(self, obs, extra, bus=None, pu=1.03):
        """Raise the voltage the area's units hold (in the island of `bus`, if given), unless any bus is high."""
        if max((b.get("v") or 0.0) for b in obs["buses"].values()) > 1.06:
            return
        isl = self.island_of(obs, bus) if bus else None
        for u, d in obs["units"].items():
            if d.get("state") == "online" and self.kind[u] in SYNC and d.get("v_set", 1.0) < pu - 1e-6 and \
                    (isl is None or self.island_of(obs, self.ubus[u]) == isl):
                extra.append(dict(op="set_v", unit=u, pu=pu))

    def h_motor_voltage(self, obs, acts, extra):
        seq = set(self.it["sequence"])
        vs = self.v["v_start"] + 0.008

        def low(x):
            if x.get("op") != "feeder" or not x.get("on") or x.get("feeder") not in seq:
                return False
            return (obs["buses"].get(self.fbus[x["feeder"]], {}).get("v") or 0.0) < vs
        return self.strip(acts, low), extra

    def h_chp_steam(self, obs, acts, extra):
        # the district heating handler holds the must-run; it is reached once the steam section is warm. Keep the
        # unit's target at the must-run so it ramps the moment the cap lifts.
        return acts, extra

    def h_tap_changers(self, obs, acts, extra):
        it = self.it
        st = obs["equipment"][it["id"]]
        blocked, again = st["blocked"], st["again_from"]
        for b in it["buses"]:
            v = obs["buses"].get(b, {}).get("v") or 0.0
            if not self.live(obs, b):
                continue
            if v < 1.0 and b not in blocked and b not in again:
                extra.append(self.equip(bus=b, blocked=True))
        return acts, extra

    def h_dual_fuel(self, obs, acts, extra):
        it = self.it
        st = obs["equipment"][it["id"]]
        pk = next(x for x in self.priv["equipment"] if x["kind"] == "gas_linepack")
        pct = obs["equipment"][pk["id"]]["pressure_pct"]
        oil = st["oil_pct"]
        for u in it["units"]:
            f = st["fuel"][u]
            if u in st["switching"]:
                continue
            if f == "gas" and pct < 75 and oil > 15:
                extra.append(self.equip(unit=u, fuel="oil"))
            elif f == "oil" and (oil < 8 or pct > 95):
                extra.append(self.equip(unit=u, fuel="gas"))
        # no combined-cycle start below the trip pressure
        lim = self.v["trip_pct"] + 6
        if pct < lim:
            acts = self.strip(acts, lambda x: x.get("op") == "start" and x.get("unit") in it["ccgt"])
        return acts, extra

    def h_rural_switching(self, obs, acts, extra):
        """Crews take local control at rural substations with dark load, when nothing else needs them: an idle crew
        the controller (and the area's other handlers) left alone this step goes to the live rural substation with
        the most dark load and stays until its feeders are back."""
        it = self.it
        rural = set(it["buses"])
        local = set(obs["equipment"][it["id"]]["local_control"])
        t = obs["t_min"]
        used = {x.get("crew") for x in acts + extra if x.get("crew")}
        target = self.s.setdefault("target", {})
        since = self.s.setdefault("since", {})
        visited = self.s.setdefault("visited", {})
        for c, d in sorted(obs["crews"].items()):
            if c in used or d.get("driving_to") or d.get("working_on") or d.get("done_min"):
                continue
            at = d.get("at")
            b = target.get(c)
            if b is not None and at == b:
                since.setdefault(c, t)
                dark = [f for f, l in self.fbus.items() if l == b and f in obs["feeders"]
                        and not obs["feeders"][f].get("on")]
                if self.live(obs, b) and dark and (b not in local or t - since[c] < self.v["local_min"] + 20):
                    acts = self.strip(acts, lambda x: x.get("crew") == c)
                    continue
                visited[b] = t
                target.pop(c, None)
                since.pop(c, None)
            best = None
            for bb in rural - set(target.values()):
                if not self.live(obs, bb) or t - visited.get(bb, -1e9) < 90:
                    continue
                mw = sum(self.demand(obs, f) for f, l in self.fbus.items() if l == bb
                         and not obs["feeders"].get(f, {}).get("on"))
                if mw <= 0:
                    continue
                key = (-mw / (1.0 + self.dist(at, bb) / 30.0), bb) if at in self.xy else (-mw, bb)
                if best is None or key < best[0]:
                    best = (key, bb)
            if best:
                target[c] = best[1]
                extra.append(dict(op="dispatch", crew=c, bus=best[1]))
        return acts, extra

    def h_ferranti_line(self, obs, acts, extra):
        it = self.it
        cen = set(it["buses"])
        live = [b for b in cen if self.live(obs, b)]
        st = obs["equipment"][it["id"]]
        if not live:
            absorb = sum(self.qmin.get(u, 0.0) for u, d in obs["units"].items() if d.get("state") == "online"
                         and self.kind.get(u) in SYNC)
            absorb += 80.0 * self.tie_links(obs)
            ok = absorb >= self.v["absorb_mvar"] and not st.get("locked_until")
            need = self.v["min_mw"] * 1.3

            def gate(lst):
                keep = []
                for x in lst:
                    if x.get("op") != "close":
                        keep.append(x)
                        continue
                    sec = self.dead_section(obs, x.get("branch")) & cen
                    if not sec:
                        keep.append(x)
                        continue
                    if not ok:
                        continue
                    # energise the centre with its load ready: feeders closed on the dark substations come back with
                    # them (the control room closes them first where it still has remote control)
                    fs = [f for f, b in self.fbus.items() if b in sec and f in obs["feeders"]]
                    ready = sum(self.demand(obs, f) for f in fs if obs["feeders"][f].get("on"))
                    can = [f for f in fs if not obs["feeders"][f].get("on") and obs["feeders"][f].get("telemetry")
                           is not False]
                    if ready >= need or not can:
                        keep.append(x)
                        continue
                    for f in sorted(can, key=lambda f: -self.demand(obs, f)):
                        if ready >= need * 1.2:
                            break
                        extra.append(dict(op="feeder", feeder=f, on=True))
                        ready += self.demand(obs, f)
                return keep
            acts = gate(acts)
            extra = gate(extra)
            # the core's all-open must not undo the prepared feeders
            acts = self.strip(acts, lambda x: x.get("op") == "feeder" and not x.get("on")
                              and self.fbus.get(x.get("feeder")) in cen)
            return acts, extra
        # load the centre at once as it comes back: the relays look at its load a few minutes after
        t = obs["t_min"]
        since = st.get("centre_live_since")
        if since is not None and t - since < self.v["delay_min"] + 1.0:
            load = sum(obs["feeders"][f].get("mw", 0.0) for f, b in self.fbus.items()
                       if b in cen and obs["feeders"].get(f, {}).get("on"))
            acts = self.strip(acts, lambda x: x.get("op") == "feeder" and not x.get("on")
                              and self.fbus.get(x.get("feeder")) in cen)
            for f, b in sorted(self.fbus.items()):
                if load >= self.v["min_mw"] * 1.5:
                    break
                if b in cen and obs["feeders"].get(f, {}).get("live") and not obs["feeders"][f].get("on"):
                    extra.append(dict(op="feeder", feeder=f, on=True))
                    load += self.demand(obs, f)
        return acts, extra

    def h_sympathetic_inrush(self, obs, acts, extra):
        cen = set(self.it["buses"])
        at = obs["equipment"][self.it["id"]]["energised_at"]
        t = obs["t_min"]
        recent = any(t - t0 < self.v["settle_min"] + 1.0 for t0 in at.values())
        if recent:
            def into(x):
                return x.get("op") == "close" and bool(self.dead_section(obs, x.get("branch")) & cen)
            acts = self.strip(acts, into)
            extra = self.strip(extra, into)
        return acts, extra

    def h_coal_mills(self, obs, acts, extra):
        st = obs["equipment"][self.it["id"]]
        for u in self.it["units"]:
            d = obs["units"].get(u, {})
            if d.get("state") == "online" and st[u]["mills"] < 5:
                extra.append(self.equip(unit=u, mills=st[u]["mills"] + 1))
        return acts, extra

    def h_water_pumping(self, obs, acts, extra):
        it = self.it
        fs = set(it["feeders"])
        t = obs["t_min"]
        cond = next((x for x in self.priv["equipment"] if x["kind"] == "sync_condenser"), None)
        con_on = cond is not None and obs["equipment"].get(cond["id"], {}).get("state") == "on"
        con_isl = self.island_of(obs, cond["bus"]) if cond else None
        last = self.s.get("last", -1e9)
        ok_t = t - last >= self.v["stagger_min"] + 0.6
        stalled = obs["equipment"][it["id"]].get("stalled_until") or {}
        chosen = None
        out = []
        for x in acts + extra:
            if x.get("op") == "feeder" and x.get("on") and x.get("feeder") in fs:
                f = x["feeder"]
                b = self.fbus[f]
                good = (con_on and self.island_of(obs, b) == con_isl) or \
                    (obs["buses"].get(b, {}).get("v") or 0.0) >= self.v["v_start"] + 0.008
                if chosen is None and ok_t and good and f not in stalled:
                    chosen = f
                    continue
                out.append(id(x))
        acts = [x for x in acts if id(x) not in out]
        extra = [x for x in extra if id(x) not in out]
        if chosen:
            self.s["last"] = t
        return acts, extra

    def h_reclosers(self, obs, acts, extra):
        it = self.it
        fs = set(it["feeders"])
        st = obs["equipment"][it["id"]]
        split, locked = set(st["split"]), set(st["locked"])
        k = self.clpu(obs)

        def risky(f):
            mw = self.demand(obs, f) * k * (0.5 if f in split else 1.0)
            return f in locked or mw > self.v["setting"] * self.fmw.get(f, 1e9) * 0.93
        acts = self.strip(acts, lambda x: x.get("op") == "feeder" and x.get("on") and x.get("feeder") in fs
                          and risky(x["feeder"]))
        # crews split the risky feeders at live substations, the largest dark load first
        cs = self.crew_for(obs, "reclosers", 1)
        acts = self.strip(acts, lambda x: x.get("crew") in cs)
        work = self.s.setdefault("work", {})
        for c in cs:
            d = obs["crews"].get(c, {})
            if d.get("driving_to") or d.get("working_on") or d.get("done_min"):
                continue
            at = d.get("at")
            here = [f for f in fs if self.fbus[f] == at and f not in split and not obs["feeders"].get(f, {}).get("on")
                    and risky(f)]
            if here:
                f = max(here, key=lambda f: self.demand(obs, f))
                extra.append(self.equip(crew=c, feeder=f))
                continue
            cand = {}
            for f in fs:
                b = self.fbus[f]
                if f in split or obs["feeders"].get(f, {}).get("on") or not risky(f) or b in work.values():
                    continue
                cand[b] = cand.get(b, 0.0) + self.demand(obs, f) * (2.0 if self.live(obs, b) else 1.0)
            if cand:
                b = max(cand, key=lambda b: (cand[b] / (1.0 + self.dist(at, b) / 30.0), b))
                work[c] = b
                extra.append(dict(op="dispatch", crew=c, bus=b))
        return acts, extra

    def h_smart_loads(self, obs, acts, extra):
        it = self.it
        fs = set(it["feeders"])
        t = obs["t_min"]
        hist = self.s.setdefault("hist", [])
        hist[:] = [(tt, m) for tt, m in hist if t - tt < self.v["resync_min"] + 12]
        cap = self.own_cap(obs) + 250.0 * self.tie_links(obs)
        lim = 0.035 * cap
        near = sum(m for tt, m in hist if abs(tt - t) < 1.6)
        out = []
        for x in acts + extra:
            if x.get("op") == "feeder" and x.get("on") and x.get("feeder") in fs \
                    and not obs["feeders"].get(x["feeder"], {}).get("on"):
                m = self.v["surge"] * self.demand(obs, x["feeder"])
                if near + m > lim:
                    out.append(id(x))
                else:
                    near += m
                    hist.append((t, m))
        acts = [x for x in acts if id(x) not in out]
        extra = [x for x in extra if id(x) not in out]
        return acts, extra
