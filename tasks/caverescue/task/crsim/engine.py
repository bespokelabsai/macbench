"""Deterministic rescue simulation. Everything the agents do is an op applied at a sim time; replaying the
same ops on the same scenario gives the same world, which is how checkpoints are graded.

Sim time is in seconds from the call-out.
"""
import heapq, json, math, os, random
from .world import Cave, HUMAN_W, HUMAN_H, STRETCHER_W, STRETCHER_H, DRONE_W, DRONE_H

N_DRONES, N_RELAYS, N_CHARGERS, N_KITS, N_TEAMS = 12, 16, 5, 6, 3
BATTERY_M = 2000.0           # metres of flight on a full battery
CHARGE_S = 30 * 60           # empty to full on a charger or at the entrance
RADIO_M = 120.0              # one relay covers this much passage
PHOTO_S = 20                 # time to take the junction photos
WALL_S = 40                  # time for a close-up of a wall
DROP_S = 30
KIT_EXTRA_S = 30 * 3600      # a survival kit buys this much more time against the cold
TEAM_IN_MPS, TEAM_OUT_MPS, STRETCHER_MPS = 0.35, 0.22, 0.08
RIG_S = 20 * 60              # rigging a pitch the first time
WIDEN_S = 90 * 60            # widening a squeeze so a stretcher fits
TEAM_WAIT_S = 2 * 3600       # how long a team waits at flood water before turning back
ORACLE_RAW = 0.8769           # raw score of the relaxed oracle (best plan per group, full fleet) on s5
SHOW_PX = 150                # a person counts as visible in a photo above this many pixels
HAZARD_S = 300               # water and cold are checked on this fixed sim-time grid (so replays match)

class Engine:
    def __init__(self, scenario_path, views_path):
        self.sc = json.load(open(scenario_path))
        cave_path = self.sc["cave"]
        if not os.path.isabs(cave_path) and not os.path.exists(cave_path):
            cave_path = os.path.join(os.path.dirname(os.path.abspath(scenario_path)), os.path.basename(cave_path))
        self.cave = Cave(cave_path)
        self.views = {v["view"]: v for v in json.load(open(views_path))}
        c = self.cave
        self.basin = self.sc["basin"]
        self.flood = self.sc["flood"]            # [[t_min, {basin: level}]]
        self.flood_t0 = self.flood[0][0]
        # public ids: a seeded permutation so ids carry no survey order
        rnd = random.Random(self.sc["seed"] * 7919 + 11)
        perm = list(range(c.n)); rnd.shuffle(perm)
        self.pub = {i: f"S{perm[i] + 100}" for i in range(c.n)}
        self.priv = {v: k for k, v in self.pub.items()}
        self.groups = {g["id"]: dict(g) for g in self.sc["groups"]}
        fl = self.sc.get("fleet", {})          # v5 scenarios size the fleet; older ones use the defaults
        self.n_drones, self.n_kits, self.n_teams = fl.get("drones", N_DRONES), fl.get("kits", N_KITS), fl.get("teams", N_TEAMS)
        self.n_relays, self.n_chargers = fl.get("relays", N_RELAYS), fl.get("chargers", N_CHARGERS)
        # only people count for a report (a group's dropped gear does not)
        self.obj_group = {o["id"]: o["group"] for o in self.sc["objects"] if o.get("group") and o["kind"].startswith("person")}
        self.reset()

    # ---------- state ----------
    def reset(self):
        c = self.cave
        self.t = 0.0; self.hazard_t = 0.0
        self.known_passages = {p["id"] for p in c.passages if p["known"]}
        self.mapped = set(self.known_passages)
        self.seen_stations = set()
        for pi in self.mapped: self.seen_stations.update(c.passages[pi]["stations"])
        self.seen_stations.add(c.base)
        self.reach = set(self.seen_stations)   # stations on a mapped passage or a flown leg (leads start here)
        self.flown = set()                     # legs (frozenset of 2 stations) control knows were flown
        self.flown_any = set()                 # legs any drone has flown, reported or not (forward stills)
        self.rigged = set()                    # pitches rigged / squeezes widened by any team
        self.too_tight = set()
        self.drones = {f"D{k + 1}": dict(id=f"D{k + 1}", at=c.base, battery=BATTERY_M, state="idle", plan=[], carry=None,
                                         relays=0, busy_until=0.0, log=[], pending=[], pend_map=[], lost=None, charge_from=None,
                                         last=dict(at=c.base, battery=BATTERY_M, state="idle", carry=None, t=0.0))
                       for k in range(self.n_drones)}
        self.stock = dict(relays=self.n_relays, chargers=self.n_chargers, kits=self.n_kits)
        self.relays = []                 # stations with a relay
        self.chargers = []               # stations with a charger
        self.kits = []                   # (station, t)
        self.drop_log = []               # (kind, station, t, drone): status lists a drop once its drone has reported
        self.photos = {}                 # photo id -> dict(view, file, t, drone, station)
        self.photo_n = 0
        self.reports = []                # (t, photo, verdict, group)
        self.found = {}                  # group -> t
        self.kit_for = {}                # group -> t
        self.kit_times = {}              # group -> [t, ...]: every kit that reached them alive
        self.rig_ready = {}              # pitch / squeeze -> sim time its first team finishes rigging it
        self.news = sorted((n["t"] * 60, n["text"]) for n in self.sc.get("news", []))
        self.news_k = 0
        self.teams = []                  # rescue teams
        self.events = []                 # text feed for the agents
        self.ops = []                    # (t, op dict) for replay
        self.dead = {}

    # ---------- flood ----------
    def level(self, st, t):
        k = int((t / 60 - self.flood_t0) // 5)
        k = max(0, min(len(self.flood) - 1, k))
        lv = self.flood[k][1]
        b = self.basin[st]
        return lv.get(str(b), lv.get(b))

    def airspace(self, st, t):
        lv = self.level(st, t)
        if lv is None or lv <= self.cave.floor[st]: return 99.0
        return self.cave.roof[st] - lv

    def depth(self, st, t):
        lv = self.level(st, t)
        return 0.0 if lv is None else max(0.0, lv - self.cave.floor[st])

    # ---------- radio ----------
    def _ball(self, st, radius):
        dist = {st: 0.0}; pq = [(0.0, st)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist[u]: continue
            for v, L, _ in self.cave.adj[u]:
                nd = d + L
                if nd <= radius and nd < dist.get(v, 1e18): dist[v] = nd; heapq.heappush(pq, (nd, v))
        return dist, None

    def covered(self):
        """Stations in radio contact with control: within range of the entrance or of a chain of relays."""
        key = tuple(sorted(self.relays))
        if getattr(self, "_cov_key", None) == key: return self._cov
        cov = self._covered()
        self._cov_key, self._cov = key, cov
        return cov

    def _covered(self):
        cov = set(self._ball(self.cave.base, RADIO_M)[0])
        live = [self.cave.base]; frontier = list(self.relays); linked = set()
        changed = True
        while changed:
            changed = False
            for r in frontier:
                if r in linked: continue
                if r in cov:
                    linked.add(r); cov |= set(self._ball(r, RADIO_M)[0]); changed = True
        return cov

    # ---------- movement ----------
    def speed(self, pi):
        p = self.cave.passages[pi]
        if p["steep"] > 0.85: return 0.5
        return max(0.4, min(1.5, 0.4 + 0.3 * min(p["min_w"], p["min_h"])))

    def leg_known(self, a, b, pi):
        return pi in self.mapped or frozenset((a, b)) in self.flown

    def _drone_fits(self, pi):
        p = self.cave.passages[pi]
        return pi not in self.too_tight and p["min_w"] >= DRONE_W and p["min_h"] >= DRONE_H   # sizes are on the map

    def _own_legs(self, d):
        """Legs this drone knows itself: flown out of contact and not yet reported, and the leg it is on."""
        own = {frozenset((x[1], x[2])) for x in d["pend_map"] if x[0] == "leg"}
        if d["plan"] and d["plan"][0][1] == "leg": own.add(frozenset(d["plan"][0][2][:2]))
        return own

    def plan_path(self, src, dst, drone=True, extra=frozenset()):
        c = self.cave
        known = lambda u, v, pi: self.leg_known(u, v, pi) or frozenset((u, v)) in extra
        edge = (lambda u, v, pi: known(u, v, pi) and self._drone_fits(pi)) if drone else known
        dist, prev = c.dijkstra(src, targets={dst}, edge=edge)
        if dst not in dist: return None
        return c.path(prev, dst)

    def _learn(self, item):
        """Control learns a flown leg or a station seen from a junction (when the drone is in contact)."""
        if item[0] == "seen":
            self.seen_stations.add(item[1]); return
        _, a, b, pi = item
        self.flown.add(frozenset((a, b))); self.seen_stations.update((a, b)); self.reach.update((a, b))
        st = self.cave.passages[pi]["stations"]
        if pi not in self.mapped and all(frozenset((x, y)) in self.flown for x, y in zip(st, st[1:])):
            self.mapped.add(pi); self.seen_stations.update(st)

    def _map_note(self, d, item):
        if d["at"] in self.covered(): self._learn(item)
        else: d["pend_map"].append(item)

    # ---------- events ----------
    def emit(self, text):
        self.events.append((self.t, text))

    def advance(self, t_end):
        """Run the world forward to t_end."""
        while True:
            nxt = []
            for d in self.drones.values():
                if d["lost"] is None and d["plan"]: nxt.append((d["plan"][0][0], 0, d["id"]))
            for k, tm in enumerate(self.teams):
                if tm["state"] in ("in", "out", "wait") and tm["next_t"] is not None: nxt.append((tm["next_t"], 1, k))
            tick = (math.floor(self.hazard_t / HAZARD_S) + 1) * HAZARD_S
            nxt.append((tick, 2, None))
            if self.news_k < len(self.news): nxt.append((self.news[self.news_k][0], 3, None))
            tt, kind, who = min(nxt)
            if tt > t_end: break
            self.t = max(self.t, tt)
            if kind == 0: self._drone_step(self.drones[who])
            elif kind == 1: self._team_step(self.teams[who])
            elif kind == 3: self.emit(self.news[self.news_k][1]); self.news_k += 1
            else: self.hazard_t = tt; self._check_hazards()
        self.t = max(self.t, t_end)

    def _check_hazards(self):
        for d in self.drones.values():
            if d["lost"] is None and self.airspace(d["at"], self.t) < 0.15:
                d["lost"] = self.t; d["plan"] = []; d["state"] = "lost"
                self._drone_note(d, f"{d['id']} went under rising water at {self.pub[d['at']]} and is lost")
            elif d["lost"] is None and not d["plan"]:
                self._flush(d)       # an idle drone reports if relays now reach it, or tries again to get back
        for g in self.groups:
            if g not in self.dead and not self.groups[g].get("reached"): self._dead_now(g)

    def _limit(self, g):
        G = self.groups[g]
        lim = G["cold_limit"] * 60
        if "kit_extra_h" not in self.sc:     # v3 scenarios: one kit, +30 h
            return lim + (KIT_EXTRA_S if g in self.kit_for and self.kit_for[g] <= lim else 0)
        for t in sorted(self.kit_times.get(g, [])):   # every kit that arrives in time buys more time
            if t <= lim: lim += self.sc["kit_extra_h"] * 3600
        return lim

    def _present(self, g, t=None):
        """Late groups are only in the cave from their `appear` time (sim minutes)."""
        a = self.groups[g].get("appear")
        return a is None or (self.t if t is None else t) >= a * 60

    def _dead_now(self, g):
        """Record and return whether group g has died by now (cold, or water over them)."""
        if g in self.dead: return True
        if not self._present(g): return False
        if self.t >= self._limit(g): self.dead[g] = self._limit(g)
        elif self.airspace(self.groups[g]["station"], self.t) < 0.3: self.dead[g] = self.t
        return g in self.dead

    def _drone_note(self, d, text):
        if d["at"] in self.covered() and not d["pending"]: self.emit(text)
        elif not d["pending"] or d["pending"][-1][1] != text: d["pending"].append((self.t, text))

    def _snapshot(self, d):
        """What control last heard from the drone (status shows this while it is out of contact)."""
        d["last"] = dict(at=d["at"], battery=d["battery"], state=d["state"], carry=d["carry"], relays=d["relays"], t=self.t,
                         eta=d["plan"][-1][0] if d["plan"] else None)

    def _flush(self, d):
        if d["at"] in self.covered():
            for item in d["pend_map"]: self._learn(item)
            d["pend_map"] = []
            if d["pending"] or any(self.photos[p]["uploaded"] is None for p in d["log"][-50:]):
                # messages keep the time they happened; they reach control now
                for t, text in d["pending"]: self.events.append((t, text))
                for pid in d["log"]:
                    if self.photos[pid]["uploaded"] is None: self.photos[pid]["uploaded"] = self.t
                d["pending"] = []
            self._snapshot(d)
        if not d["plan"] and d["lost"] is None and d["at"] not in self.covered():
            self._return_to_contact(d)

    def _return_to_contact(self, d):
        """Lost-link failsafe: a drone left idle out of radio contact flies back to the nearest station in contact,
        along legs it or control knows, avoiding water it cannot fly through. With no such way it waits and retries."""
        c = self.cave; cov = self.covered()
        own = {frozenset((x[1], x[2])) for x in d["pend_map"] if x[0] == "leg"}
        edge = lambda u, v, pi: ((self.leg_known(u, v, pi) or frozenset((u, v)) in own) and self._drone_fits(pi)
                                 and self.airspace(v, self.t) >= 0.3)
        dist, prev = c.dijkstra(d["at"], targets=cov, edge=edge)
        home = min((s for s in dist if s in cov), key=dist.get, default=None)
        if home is None:
            self._drone_note(d, f"{d['id']} has no radio contact at {self.pub[d['at']]} and no clear way back; waiting there")
            return
        self._drone_note(d, f"{d['id']} has no radio contact at {self.pub[d['at']]}; returning to {self.pub[home]}")
        self._start_path(d, c.path(prev, home), "junctions")

    def _drone_step(self, d):
        t, action, arg = d["plan"].pop(0)
        c = self.cave
        if action == "leg":
            a, b, pi = arg
            p = c.passages[pi]
            if p["min_w"] < DRONE_W or p["min_h"] < DRONE_H:
                self.too_tight.add(pi); d["plan"] = []; d["state"] = "idle"
                self._take_photos(d, a)
                self._drone_note(d, f"{d['id']} could not fit along the passage from {self.pub[a]} towards {self.pub[b]}; it is too tight for a drone. {d['id']} is waiting at {self.pub[a]}")
                self._flush(d); return
            if self.airspace(b, self.t) < 0.3:
                d["plan"] = []; d["state"] = "idle"
                self._take_photos(d, a)
                self._drone_note(d, f"{d['id']} stopped at {self.pub[a]}: water ahead with no airspace towards {self.pub[b]}")
                self._flush(d); return
            L = dict((v, L) for v, L, _ in c.adj[a])[b] * (1.2 if d["carry"] else 1.0)
            d["battery"] -= L
            if d["battery"] < 0:
                d["lost"] = self.t; d["plan"] = []; d["state"] = "lost"
                self._drone_note(d, f"{d['id']} ran out of battery between {self.pub[a]} and {self.pub[b]} and is lost"); return
            d["at"] = b
            leg = frozenset((a, b))
            # first flight of a leg off the rescue survey: the camera keeps a forward still at every station
            first = pi not in self.mapped and leg not in self.flown_any
            self.flown_any.add(leg)
            if not self.leg_known(a, b, pi): self._map_note(d, ("leg", a, b, pi))
            last = not d["plan"] or d["plan"][0][1] != "leg"
            if first and not (c.S[b]["junction"] or last):
                self._take_photo(d, b, d["plan"][0][2][1])
            if c.S[b]["junction"] or last:
                for v, _, pj in c.adj[b]:
                    if v not in self.seen_stations: self._map_note(d, ("seen", v))
                self._take_photos(d, b)
            if last and not d["plan"]:
                d["state"] = "idle"; self._drone_note(d, f"{d['id']} arrived at {self.pub[b]} (battery {d['battery']:.0f} m)")
                self._dock(d)
            self._flush(d)
        elif action == "wall":
            self._take_photo(d, d["at"], arg); d["state"] = "idle" if not d["plan"] else "flying"
            self._drone_note(d, f"{d['id']} took a close-up of the {'left' if arg == 'L' else 'right'} wall at {self.pub[d['at']]}")
            if not d["plan"]: self._dock(d)
            self._flush(d)
        elif action == "drop":
            ok, msg = self._do_drop(d)
            d["state"] = "idle" if not d["plan"] else "flying"; self._drone_note(d, f"{d['id']}: {msg}")
            if not d["plan"]: self._dock(d)
            self._flush(d)
        elif action == "charged":
            d["battery"] = BATTERY_M; d["state"] = "idle"; d["charge_from"] = None
            self._drone_note(d, f"{d['id']} is fully charged at {self.pub[d['at']]}"); self._flush(d)

    def _dock(self, d):
        """A drone that ends its flight on a placed charger docks and charges itself (in contact or not)."""
        if d["at"] in self.chargers and d["battery"] < BATTERY_M - 1 and not d["plan"]:
            self._start_charge(d)
            self._drone_note(d, f"{d['id']} docked on the charger at {self.pub[d['at']]}; full at {self.clock(d['plan'][0][0])}")

    def _start_charge(self, d):
        need = (BATTERY_M - d["battery"]) / BATTERY_M * CHARGE_S
        d["plan"] = [(self.t + need, "charged", None)]; d["state"] = "charging"; d["charge_from"] = (self.t, d["battery"])

    def _stop_charge(self, d):
        """An order that interrupts a charge keeps the charge so far."""
        if d["state"] == "charging" and d["charge_from"]:
            t0, b0 = d["charge_from"]
            d["battery"] = min(BATTERY_M, b0 + (self.t - t0) / CHARGE_S * BATTERY_M)
        d["plan"] = [x for x in d["plan"] if x[1] != "charged"]; d["charge_from"] = None
        if d["state"] == "charging": d["state"] = "idle"

    def _take_photos(self, d, st):
        for v, _, _ in self.cave.adj[st]: self._take_photo(d, st, v)

    def _take_photo(self, d, st, to):
        key = f"{st}-{to}"
        V = self.views.get(key)
        if V is None: return
        lv = self.level(st, self.t)
        if to not in ("L", "R"):
            lt = self.level(to, self.t)
            if lt is not None and (lv is None or lt > lv): lv = lt
        # people who have been taken out are no longer in the picture
        gone = {g for g, G in self.groups.items() if G.get("reached") or not self._present(g)}
        show_gone = {x.get("gone") for x in V["variants"] if x.get("gone")} & gone
        g0 = min(show_gone) if show_gone else None
        cand = [x for x in V["variants"] if x.get("gone") == g0]
        var = cand[0]
        for x in cand[1:]:
            if lv is not None and x["level"] is not None and x["level"] <= lv: var = x
        self.photo_n += 1
        rnd = random.Random(self.sc["seed"] * 100003 + self.photo_n)
        pid = "P" + "".join(rnd.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
        self.photos[pid] = dict(id=pid, file=var["file"], t=self.t, drone=d["id"], station=st, to=to,
                                objects=var["objects"], uploaded=self.t if st in self.covered() else None)
        d["log"].append(pid)

    # ---------- ops ----------
    def apply(self, op):
        """Apply one agent op at the current time. Returns (ok, message or data)."""
        self.ops.append((self.t, op))
        f = getattr(self, "op_" + op.get("op", ""), None)
        if f is None: return False, f"unknown op {op.get('op')}"
        return f(op)

    def _drone(self, op, need_link=True):
        d = self.drones.get(str(op.get("drone", "")).upper())
        if d is None: return None, "no such drone"
        if d["lost"] is not None: return None, f"{d['id']} is lost"
        if need_link and d["at"] not in self.covered(): return None, f"{d['id']} is out of radio contact; it will report when it is back in range"
        return d, None

    def _origin(self, d):
        """Where a new order starts: a drone in the middle of a leg, a close-up or a drop finishes that first."""
        if d["plan"] and d["plan"][0][1] == "leg": return d["plan"][0][2][1], [d["plan"][0]]
        if d["plan"] and d["plan"][0][1] in ("wall", "drop"): return d["at"], [d["plan"][0]]
        return d["at"], []

    def op_fly(self, op):
        d, err = self._drone(op)
        if err: return False, err
        dst = self.priv.get(str(op.get("to", "")).upper())
        if dst is None or dst not in self.seen_stations: return False, "unknown station (only stations on the map can be targets)"
        path = self.plan_path(self._origin(d)[0], dst, extra=self._own_legs(d))
        if path is None:
            # allow flying into an unmapped lead: one hop from a mapped station
            return False, "no mapped route there for a drone"
        return self._start_path(d, path, op.get("photos", "junctions"), op.get("drop"))

    def op_explore(self, op):
        """Fly to a station, then into the lead towards a neighbouring station and on to the next junction."""
        d, err = self._drone(op)
        if err: return False, err
        frm = self.priv.get(str(op.get("from", "")).upper()); to = self.priv.get(str(op.get("toward", "")).upper())
        if frm is None or to is None or frm not in self.reach: return False, "unknown station (a lead starts at a station on a mapped passage)"
        pi, _ = self.cave.leg_passage(frm, to)
        if pi is None: return False, f"{op.get('toward')} is not next to {op.get('from')}"
        here = self._origin(d)[0]
        path = self.plan_path(here, frm, extra=self._own_legs(d)) if here != frm else [frm]
        if path is None: return False, "no mapped route to the start of that lead"
        st = self.cave.passages[pi]["stations"]
        k = st.index(frm)
        seq = st[k:] if k + 1 < len(st) and st[k + 1] == to else st[:k + 1][::-1]
        ok, msg = self._start_path(d, path + seq[1:], "junctions", op.get("drop"))
        if ok: msg = f"{d['id']} flying {len(path) - 1} known legs to {self.pub[frm]}, then into the lead towards {self.pub[to]}; battery {d['battery']:.0f} m"
        return ok, msg

    def _start_path(self, d, path, photos, drop=None):
        _, keep = self._origin(d)
        t = keep[0][0] if keep else self.t; plan = list(keep)
        for a, b in zip(path, path[1:]):
            pi, L = self.cave.leg_passage(a, b)
            t += L / self.speed(pi) / (0.8 if d["carry"] else 1.0)
            plan.append((t, "leg", (a, b, pi)))
        if len(plan) == len(keep) and not (keep and keep[0][1] == "leg"): return False, f"{d['id']} is already there"
        if drop:
            if not d["carry"]: return False, f"{d['id']} is not carrying anything to drop"
            plan.append((t + DROP_S, "drop", None))
        self._stop_charge(d)
        d["plan"] = plan; d["state"] = "flying"
        need = sum(dict((v, L) for v, L, _ in self.cave.adj[a])[b] for a, b in zip(path, path[1:]))
        return True, f"{d['id']} flying {len(path) - 1} legs, {need:.0f} m, eta {self.clock(t)} sim; battery {d['battery']:.0f} m"

    def op_wall(self, op):
        d, err = self._drone(op)
        if err: return False, err
        side = str(op.get("side", "")).upper()[:1]
        if side not in ("L", "R"): return False, "side must be left or right"
        if d["plan"]: return False, f"{d['id']} is busy"
        d["plan"] = [(self.t + WALL_S, "wall", side)]; d["state"] = "photographing"
        return True, f"{d['id']} taking a close-up of the {'left' if side == 'L' else 'right'} wall"

    def op_charge(self, op):
        d, err = self._drone(op)
        if err: return False, err
        if d["at"] != self.cave.base and d["at"] not in self.chargers: return False, "no charger here (chargers: the entrance and any placed charger)"
        if d["plan"]: return False, f"{d['id']} is busy"
        self._start_charge(d)
        return True, f"{d['id']} charging, full at {self.clock(d['plan'][0][0])} sim"

    def op_load(self, op):
        d, err = self._drone(op)
        if err: return False, err
        if d["at"] != self.cave.base: return False, "loading is only possible at the entrance"
        if any(x[1] != "charged" for x in d["plan"]): return False, f"{d['id']} is busy"
        what = op.get("item")
        if d["carry"] or d["relays"]: return False, f"{d['id']} is already carrying something"
        if what == "relays":
            n = int(op.get("n", 1))
            if n < 1 or n > 4: return False, "a drone carries 1 to 4 relays"
            if self.stock["relays"] < n: return False, f"only {self.stock['relays']} relays left"
            self.stock["relays"] -= n; d["relays"] = n; d["carry"] = "relays"
            return True, f"{d['id']} loaded {n} relays"
        if what in ("kit", "charger"):
            key = what + "s"
            if self.stock[key] < 1: return False, f"no {what}s left"
            self.stock[key] -= 1; d["carry"] = what
            return True, f"{d['id']} loaded a {what}"
        return False, "item must be relays, kit or charger"

    def op_drop(self, op):
        d, err = self._drone(op)
        if err: return False, err
        if d["plan"]: return False, f"{d['id']} is busy"
        ok, msg = self._do_drop(d)
        if ok: self._snapshot(d)
        return ok, msg

    def _do_drop(self, d):
        st = d["at"]
        if d["carry"] == "relays":
            if st in self.relays: return False, f"there is already a relay at {self.pub[st]}; {d['id']} kept its {d['relays']} on board"
            self.relays.append(st); d["relays"] -= 1; self.drop_log.append(("relay", st, self.t, d["id"]))
            if d["relays"] == 0: d["carry"] = None
            return True, f"relay placed at {self.pub[st]} ({d['relays']} left on board)"
        if d["carry"] == "charger":
            self.chargers.append(st); d["carry"] = None; self.drop_log.append(("charger", st, self.t, d["id"]))
            return True, f"charger placed at {self.pub[st]}"
        if d["carry"] == "kit":
            self.kits.append((st, self.t)); d["carry"] = None; self.drop_log.append(("kit", st, self.t, d["id"]))
            for g, G in self.groups.items():
                if self._present(g) and not G.get("reached") and not self._dead_now(g) and self._path_m(st, G["station"]) <= 15:
                    self.kit_for.setdefault(g, self.t)
                    self.kit_times.setdefault(g, []).append(self.t)
            return True, f"survival kit left at {self.pub[st]}"
        return False, f"{d['id']} is not carrying anything"

    def _path_m(self, a, b):
        d, _ = self._ball(a, 40)
        return d.get(b, 1e9)

    def op_report(self, op):
        pid = str(op.get("photo", "")).upper()
        ph = self.photos.get(pid)
        if ph is None or ph["uploaded"] is None: return False, "no such photo"
        groups = {self.obj_group[o] for o, px in ph["objects"].items() if o in self.obj_group and px >= SHOW_PX}
        if not groups:
            self.reports.append((self.t, pid, False, None))
            return True, "report logged: the incident controller could not confirm anyone in that photo"
        new = [g for g in groups if g not in self.found]
        for g in new: self.found[g] = self.t
        self.reports.append((self.t, pid, True, sorted(groups)))
        n = sum(len(self.groups[g]["members"]) for g in groups)
        return True, f"confirmed: {n} {'person' if n == 1 else 'people'} in photo {pid}" + ("" if new else " (already reported)")

    def op_rescue(self, op):
        """Send a rescue team along a route of stations from the entrance. The team brings out anyone it meets."""
        route = [self.priv.get(str(s).upper()) for s in op.get("route", [])]
        if not route or None in route: return False, "route must be a list of station ids"
        if route[0] != self.cave.base: return False, f"a route must start at the entrance {self.pub[self.cave.base]}"
        for a, b in zip(route, route[1:]):
            pi, _ = self.cave.leg_passage(a, b)
            if pi is None or not self.leg_known(a, b, pi): return False, f"{self.pub[a]} to {self.pub[b]} is not a mapped leg"
        busy = sum(1 for tm in self.teams if tm["state"] in ("in", "out", "wait"))
        if busy >= self.n_teams: return False, f"all {self.n_teams} rescue teams are already underground"
        # people within 15 m of a station on the way in are met there
        near = {k: [g for g, G in self.groups.items() if self._path_m(st, G["station"]) <= 15] for k, st in enumerate(route)}
        tm = dict(id=f"T{len(self.teams) + 1}", route=route, k=0, state="in", next_t=self.t, near=near, carrying=[],
                  stretcher=False, wait_since=None, sent=self.t)
        self.teams.append(tm); self._team_schedule(tm)
        return True, f"rescue team {tm['id']} leaving the entrance along {len(route) - 1} legs"

    def _team_schedule(self, tm):
        r = tm["route"]
        if tm["state"] == "in":
            if tm["k"] >= len(r) - 1: tm["next_t"] = self.t; return
            a, b = r[tm["k"]], r[tm["k"] + 1]
        else:
            if tm["k"] <= 0: tm["next_t"] = self.t; return
            a, b = r[tm["k"]], r[tm["k"] - 1]
        pi, L = self.cave.leg_passage(a, b)
        v = TEAM_IN_MPS if tm["state"] == "in" else (STRETCHER_MPS if tm["stretcher"] else TEAM_OUT_MPS)
        dt = L / v
        p = self.cave.passages[pi]
        work = []
        if p["steep"] > 0.85: work.append((pi, RIG_S))            # a rigged pitch stays rigged
        if tm["state"] == "out" and tm["stretcher"] and (p["min_w"] < STRETCHER_W or p["min_h"] < STRETCHER_H):
            work.append((("w", pi), WIDEN_S))
        for key, cost in work:
            if key not in self.rigged:
                dt += cost; self.rigged.add(key); self.rig_ready[key] = self.t + dt
            elif self.rig_ready.get(key, 0) > self.t:
                dt = max(dt, self.rig_ready[key] - self.t)    # still being rigged by another team: wait for it
        tm["next_t"] = self.t + dt; tm["leg"] = (a, b, pi)

    def _team_meet(self, tm):
        """On the way in: anyone near this station is met. Returns True if the team now turns back with people."""
        st = self.pub[tm["route"][tm["k"]]]
        for g in tm["near"].get(tm["k"], []):
            G = self.groups[g]
            if G.get("reached") or G.get("recovered") or not self._present(g): continue
            if self._dead_now(g):
                G["recovered"] = self.t
                self.emit(f"rescue team {tm['id']} reached {len(G['members'])} of the missing party at {st}; they had not survived")
            else:
                G["reached"] = self.t; tm["carrying"].append(g); tm["stretcher"] |= bool(G["injured"])
                self.emit(f"rescue team {tm['id']} reached {len(G['members'])} of the missing party at {st}")
        if tm["carrying"]:
            self.emit(f"rescue team {tm['id']} is bringing them out")
            return True
        return False

    def _team_step(self, tm):
        r = tm["route"]
        if tm["state"] == "in" and tm["k"] >= len(r) - 1:
            if not self._team_meet(tm):
                end = r[-1]
                if any(self._path_m(end, G["station"]) <= 15 and (G.get("reached") or G.get("recovered")) for G in self.groups.values()):
                    self.emit(f"rescue team {tm['id']} reached {self.pub[end]}; nobody is left there. Returning")
                else:
                    self.emit(f"rescue team {tm['id']} reached {self.pub[end]} and found nobody; returning")
            tm["state"] = "out"; self._team_schedule(tm); return
        if tm["state"] == "out" and tm["k"] <= 0:
            tm["state"] = "done"; tm["next_t"] = None
            if tm["carrying"]:
                for g in tm["carrying"]: self.groups[g]["saved"] = self.t
                names = [m for g in tm["carrying"] for m in self.groups[g]["members"]]
                self.emit(f"rescue team {tm['id']} is out with {', '.join(names)}")
            else: self.emit(f"rescue team {tm['id']} is back at the entrance")
            return
        a, b, pi = tm["leg"]
        p = self.cave.passages[pi]
        human_fit = p["min_w"] >= HUMAN_W and p["min_h"] >= HUMAN_H
        if not human_fit:
            self.emit(f"rescue team {tm['id']} cannot get through between {self.pub[a]} and {self.pub[b]}: too tight; turning back")
            if tm["state"] == "in": tm["state"] = "out"; self._team_schedule(tm); return
            tm["state"] = "done"; tm["next_t"] = None; return
        if self.airspace(b, self.t) < 0.4 or self.depth(b, self.t) > 1.2:
            if tm["wait_since"] is None:
                tm["wait_since"] = self.t
                self.emit(f"rescue team {tm['id']} is held by flood water at {self.pub[a]}")
            if self.t - tm["wait_since"] < TEAM_WAIT_S or tm["state"] == "out":
                tm["next_t"] = self.t + 600; return
            self.emit(f"rescue team {tm['id']} gave up waiting at {self.pub[a]} and is coming back")
            tm["state"] = "out"; tm["wait_since"] = None; self._team_schedule(tm); return
        tm["wait_since"] = None
        tm["k"] += 1 if tm["state"] == "in" else -1
        if tm["state"] == "in" and tm["k"] < len(r) - 1 and self._team_meet(tm):
            tm["state"] = "out"
        self._team_schedule(tm)

    # ---------- views for the agents ----------
    def clock(self, t):
        m = int(19 * 60 + 30 + t // 60) % (24 * 60)
        return f"{m // 60:02d}:{m % 60:02d}"

    def score(self):
        """Score the world as it stands at self.t (call after advancing to the end of the storm).

        Per group, weighted by head count: found 0.25 * (1 - t_found / end), kit 0.25 * (1 - t_kit / cold
        limit) when the kit lands before the cold limit, reached alive 0.50; minus 0.05 per false report and
        0.01 per lost drone. The raw total is divided by ORACLE_RAW (the best plan's raw total on this
        scenario, so the oracle scores 1.0) and clipped to [0, 1]."""
        total = sum(len(G["members"]) for G in self.groups.values())
        end = self.sc["sim_hours"] * 3600
        s = 0.0; parts = {}
        for g, G in self.groups.items():
            w = len(G["members"]) / total
            # everything counts only for people control found in a photo: reported while alive, before a team met them
            alive_found = (g in self.found and (g not in self.dead or self.found[g] < self.dead[g])
                           and (not G.get("reached") or self.found[g] <= G["reached"]))
            lim = G["cold_limit"] * 60
            f = 0.25 * max(0.0, 1 - self.found[g] / end) if alive_found else 0.0
            k = (0.25 * max(0.0, 1 - self.kit_for[g] / lim)
                 if alive_found and g in self.kit_for and self.kit_for[g] <= lim else 0.0)
            sv = 0.50 if alive_found and G.get("reached") and (g not in self.dead or self.dead[g] > G["reached"]) else 0.0
            parts[g] = dict(found=round(f, 4), kit=round(k, 4), saved=sv, weight=round(w, 3))
            s += w * (f + k + sv)
        false = sum(1 for r in self.reports if not r[2])
        lost = sum(1 for d in self.drones.values() if d["lost"] is not None)
        raw = s - 0.05 * false - 0.01 * lost
        s = max(0.0, min(1.0, raw / ORACLE_RAW))
        return round(s, 4), dict(groups=parts, false_reports=false, drones_lost=lost, raw=round(raw, 4),
                                 oracle_raw=ORACLE_RAW)
