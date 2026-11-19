"""The reference controller: rule-based, working every sector no seat has open. Only the reference solution uses it
(standalone.py --reference); on the live day a sector no seat has open has no controller.

It is slow and cautious, but not free of safety events: a full day can still have losses of separation and near
collisions. It uses the same instructions, the same frequencies and the same radio time as a seat, and it acts
on short-term conflict alerts in the approach and departure sectors it works. What it does not do:
- plan ahead: no ground delays at origin, no miles in trail, no expected approach times;
- use the runway well: it spaces arrivals by the wake minimum (at least 3 NM) plus 2 NM, or plus 5 NM when one
  runway takes both arrivals and departures, and by 10 NM in low visibility; it departs only from full length, and
  gives no rapid-exit or reduced separation clearances;
- see the weather coming: it changes the runway direction only after the tailwind has been over 7 kt for 5
  minutes, and declares low visibility procedures only once visibility is already below 600 m;
- manage stands: it books the first free stand at landing time.
"""
from atcsim import airport as AP
from atcsim.geo import angdiff, bearing, dest, dist_nm
from atcsim.model import wake_nm, MINIMA_RVR
from atcsim.radio import SECTORS
from atcsim.world import Refusal

PERIOD_S = 4.0
STCA_SECTORS = ("ARR", "DIR", "DEP_N", "DEP_S")
APPROACH_KT = 170.0


class StandIn:
    def __init__(self):
        self.last = 0.0
        self.slots = {}             # feeder -> {level: flight}: one aircraft per level in each terminal stack
        self.release = {}           # arrival runway -> (t, group) of the last release onto the final
        self.waiting = []           # arrivals in the order they reached the feeder
        self.tail_since = None

    def staffed(self, w, sector):
        return w.sectors.owner.get(sector) is not None

    def _do(self, w, op):
        if op.get("op") == "altitude" and op.get("_check", True):
            f = w.flights.get(op["flight"])
            if f is not None and f.id in w.air.state and not self._alt_ok(w, f, op["ft"], op.get("_radius", 12.0)):
                # Descend in steps: as low as is free now ("descend to X, expect lower").
                step = self._partial(w, f, op["ft"], op.get("_radius", 12.0))
                if step is None:
                    return None
                op = dict(op, ft=step)
        op = {k: v for k, v in op.items() if not k.startswith("_")}
        try:
            return w.instruct(None, op)
        except Refusal:
            return None

    def step(self, w, dt):
        if w.t - self.last < PERIOD_S:
            return
        self.last = w.t
        if not self.staffed(w, "SUP"):
            self._supervisor(w)
        if not (self.staffed(w, "GND") and self.staffed(w, "GND_2")):
            self._apron(w)
        self._stca(w)
        self._deviations(w)
        for sec in ("ARR", "DIR"):
            if not self.staffed(w, sec):
                self._approach(w, sec)
        if not self.staffed(w, self.twr_arr(w)):
            self._tower_arr(w)
        if not self.staffed(w, self.twr_dep(w)):
            self._tower_dep(w)
        for sec in ("GND", "GND_2"):
            if not self.staffed(w, sec):
                self._ground(w, sec)
        for sec in ("DEP_N", "DEP_S"):
            if not self.staffed(w, sec):
                self._departure(w, sec)

    def twr_arr(self, w):
        return w.tower_position(w.body(w.arr_rwy))

    def twr_dep(self, w):
        return w.tower_position(w.body(w.dep_rwy))

    def _deviations(self, w):
        """Approve each weather deviation request on a sector no seat works, once."""
        for fid in list(w.air.state.keys()):
            f = w.flights[fid]
            req = f.data.get("devreq")
            if req and f.sector and not self.staffed(w, f.sector) and req.get("answered") != req["asked"]:
                if self._do(w, {"op": "deviation", "flight": fid, "approve": True}):
                    req["answered"] = req["asked"]       # asked again (the crew missed it): answer again

    # -- short-term conflict alert ------------------------------------------------------------------------------------
    def _stca(self, w):
        """The radar's short-term conflict alert, as every approach unit has. Each cycle it projects every airborne
        aircraft within 50 NM 90 seconds ahead on its present track, speed and climb or descent. For the first pair
        that would come within 3.3 NM and 950 ft, it moves one aircraft it works (in an unstaffed approach or
        departure sector, not established on the localizer): 1,000 ft away from the other if that level is free
        within 8 NM, else a 30 degree turn away. It prefers an aircraft deviating for weather, then one not yet in
        the approach sequence. One instruction per cycle, and none to the same aircraft within 45 seconds."""
        ac = []
        for fid, st in w.air.state.items():
            f = w.flights.get(fid)
            s = w.air.snapshot(fid)
            if f is None or s is None or s["alt_ft"] < 1500 or s["alt_ft"] > 20000:
                continue
            p = (s["lat"], s["lon"])
            if dist_nm(p, AP.ARP) > 50:
                continue
            ac.append((f, st, s, p))
        hits = []
        for i in range(len(ac)):
            for j in range(i + 1, len(ac)):
                a, b = ac[i], ac[j]
                if a[1].mode == "loc" and b[1].mode == "loc":
                    continue     # both on a localizer: final spacing and parallel approaches have their own rules
                if dist_nm(a[3], b[3]) > 15 or abs(a[2]["alt_ft"] - b[2]["alt_ft"]) > 3000:
                    continue
                for t in (0, 15, 30, 45, 60, 75, 90):
                    pa, za = self._ahead(a, t)
                    pb, zb = self._ahead(b, t)
                    if dist_nm(pa, pb) < 3.3 and abs(za - zb) < 950:
                        hits.append((t, a, b))
                        break
        for t, a, b in sorted(hits, key=lambda h: h[0]):
            for mover, other in sorted(((a, b), (b, a)), key=lambda m: (m[0][1].mode != "heading", bool((m[0][0].data.get("si") or {}).get("released")))):
                f, st, s, p = mover
                if (f.sector not in STCA_SECTORS or self.staffed(w, f.sector) or st.mode in ("loc", "divert")
                        or f.phase not in ("arrival", "airborne") or w.t - f.data.get("stca_t", -1e9) < 45):
                    continue
                if self._stca_resolve(w, mover, other):
                    f.data["stca_t"] = w.t
                    return True
        return False

    @staticmethod
    def _ahead(a, t):
        f, st, s, p = a
        z = s["alt_ft"] + s["vs_fpm"] * t / 60.0
        if st.alt is not None:
            z = min(z, max(st.alt, s["alt_ft"])) if s["vs_fpm"] >= 0 else max(z, min(st.alt, s["alt_ft"]))
        return dest(p, s["trk"], s["gs_kt"] * t / 3600.0), z

    def _stca_resolve(self, w, mover, other):
        f, st, s, p = mover
        base = round(s["alt_ft"] / 1000.0) * 1000
        up_first = other[2]["alt_ft"] <= s["alt_ft"]
        floor = 3000 if dist_nm(p, AP.ARP) < 25 else 4000
        for lvl in ((base + 1000, base - 1000) if up_first else (base - 1000, base + 1000)):
            if not floor <= lvl <= 16000 or abs(lvl - other[2]["alt_ft"]) < 1000:
                continue
            if self._level_clear(w, f.id, p, lvl) and self._do(w, {"op": "altitude", "flight": f.id, "ft": lvl, "_check": False}):
                return True
        away = 1 if angdiff(s["trk"], bearing(p, other[3])) < 0 else -1
        return bool(self._do(w, {"op": "heading", "flight": f.id, "hdg": round((s["trk"] + away * 30) % 360)}))

    def _level_clear(self, w, fid, p, lvl, radius=8.0):
        for gid, gst in w.air.state.items():
            if gid == fid:
                continue
            g = w.air.snapshot(gid)
            if g is None or dist_nm(p, (g["lat"], g["lon"])) > radius:
                continue
            if abs(g["alt_ft"] - lvl) < 900 or (gst.alt is not None and abs(gst.alt - lvl) < 900):
                return False
        return True

    def mine(self, w, sector):
        return [f for f in w.flights.values() if f.sector == sector]

    # -- supervisor -----------------------------------------------------------------------------------------------
    def _supervisor(self, w):
        rw = w.arr_rwy
        head, cross, gh, gc = w.weather.runway_wind(w.t, AP.RUNWAYS[rw].heading)
        if gh < -7:
            if self.tail_since is None:
                self.tail_since = w.t
            elif w.t - self.tail_since > 300:
                d = "12" if w.config["dir"] == "30" else "30"
                self._do(w, {"op": "runway_config", "dir": d, "mode": w.config["mode"] if not w.config["mode"].startswith("single") else "segregated"})
                self.tail_since = None
        else:
            self.tail_since = None
        o = w.weather.current(w.t)
        if w.lvp["state"] == "off" and o.rvr_or_vis < 600:
            self._do(w, {"op": "lvp", "on": True})
        elif w.lvp["state"] == "on" and o.rvr_or_vis > 1500 and (o.ceiling_ft is None or o.ceiling_ft > 500):
            self._do(w, {"op": "lvp", "on": False})
        # Poor braking reported: inspect the runway (it closes, and the lines below work the other one meanwhile).
        for b in list(w.braking_poor):
            r = next((r for r in AP.RUNWAYS if w.body(r) == b), None)
            if r and not any(c[0] == b and c[2] > w.t and c[3].startswith("inspection") for c in w.closures):
                self._do(w, {"op": "inspect", "runway": r})
        # A closed runway: work the other one in mixed mode; back to segregated when it reopens.
        d = w.config["dir"]
        arr, dep = AP.CONFIGS[d]["arr"], AP.CONFIGS[d]["dep"]
        if w.config["mode"] == "segregated":
            if w.runway_closed(arr) and not w.runway_closed(dep):
                self._do(w, {"op": "runway_config", "dir": d, "mode": "single:" + dep})
            elif w.runway_closed(dep) and not w.runway_closed(arr):
                self._do(w, {"op": "runway_config", "dir": d, "mode": "single:" + arr})
        elif not w.runway_closed(arr) and not w.runway_closed(dep):
            self._do(w, {"op": "runway_config", "dir": d, "mode": "segregated"})

    # -- apron ----------------------------------------------------------------------------------------------------
    def _apron(self, w):
        for f in w.flights.values():
            booked = f.data.get("stand")
            if f.kind == "ARR" and f.phase in ("final", "landing", "taxi_in") and (not booked or booked in w.ground.closed_stands):
                sid = w.pick_stand(f)
                if sid:
                    self._do(w, {"op": "assign_stand", "flight": f.id, "stand": sid})
                    f.data.pop("si_taxi", None)
        if w.ramp_closed:
            return
        # Departure metering: no more pushbacks while 12 departures are already between pushback and take-off, so the
        # taxiways (and the arrival runway exits) do not fill up.
        out = sum(1 for g in w.flights.values() if g.kind == "DEP" and g.phase in ("pushed", "taxi_out", "holding", "lined_up"))
        ready = [f for sec in ("GND", "GND_2") if not self.staffed(w, sec) for f in self.mine(w, sec) if f.kind == "DEP"]
        for f in sorted(ready, key=lambda x: x.data.get("ready_t") or x.sched):
            ctot = f.data.get("ctot")
            if ctot is not None and w.t < ctot - 15 * 60:
                continue       # start up in time for the DFLOW slot, not before
            if f.phase == "ready" and out < 12:
                if self._do(w, {"op": "pushback", "flight": f.id, "then": self._gnd_for(w, f)}):
                    out += 1

    def _gnd_for(self, w, f):
        return w.ground_position(f.stand or f.data.get("stand"))

    # -- approach -------------------------------------------------------------------------------------------------
    def _approach(self, w, sec):
        """Each side's arrivals hold at their feeder, each at the level Center gave it: one aircraft per level, so
        no two aircraft in a stack share a level. When the bottom slot frees, the aircraft above steps down into it.
        The lowest aircraft leaves the hold when its track miles put it at least the wake minimum + 2 NM (10 NM in
        low visibility) behind every aircraft already on its way to the runway; it flies the downwind to the 18 NM
        point at its level, descends to 3,000 ft once it is more than 8 NM from the hold fix on the downwind, turns
        base at 180 kt and joins the final at 11 NM."""
        rw = w.arr_rwy
        if w.runway_closed(rw):
            return
        proc = AP.proc_for(rw)
        o = w.weather.current(w.t)
        lvp = w.lvp_on() or o.rvr_or_vis < 1500
        tag = rw[:2]
        mine = self.mine(w, sec)
        if getattr(self, "last_rw", rw) != rw:
            # Runway direction changed: everyone not yet established for the new runway joins the new stacks.
            for g in w.flights.values():
                gsi = g.data.get("si")
                gst = w.air.state.get(g.id)
                if gsi and gst is not None and g.phase == "arrival" and gst.app_rwy != rw:
                    gsi.clear()
            for k in w.side_levels:
                w.side_levels[k].clear()
        self.last_rw = rw
        for f in mine:
            st = w.air.state.get(f.id)
            if st is None or f.phase != "arrival":
                continue
            fl = f.data.setdefault("si", {})
            if st.mode == "heading":
                fl.setdefault("hdg_since", w.t)
            else:
                fl.pop("hdg_since", None)
            side = w.side_of(f)
            feeder = proc["sides"][side]["feeder"]
            if st.mode == "heading" and (f.id, "route") not in w.pending and (f.id, "hold") not in w.pending:
                # Deviating for weather: send it on again once the way is clear.
                s_ = w.air.snapshot(f.id)
                here = (s_["lat"], s_["lon"])
                if fl.get("released") and fl.get("route"):
                    nxt = fl["route"][0] if dist_nm(here, AP.FIXES[fl["route"][0]]) > 3 else fl["route"][-1]
                    if w.path_clear_of_cells(here, AP.FIXES[nxt]):
                        self._do(w, {"op": "route", "flight": f.id, "fixes": fl["route"][fl["route"].index(nxt):]})
                        return
                elif fl.get("feeder"):
                    if w.path_clear_of_cells(here, AP.FIXES[fl["feeder"]]) or w.t - fl.get("hdg_since", w.t) > 480:
                        self._do(w, {"op": "hold", "flight": f.id, "fix": fl["feeder"]})
                        return
                elif w.path_clear_of_cells(here, AP.FIXES[feeder]) or w.t - fl.get("hdg_since", w.t) > 480:
                    pass   # not yet in the stack: the check-in below sends it to the feeder
                else:
                    continue
            if fl.get("released") and f.data.get("go_arounds", 0) > fl.get("ga_seen", 0):
                keep = fl.get("m_level")
                fl.clear()
                fl["ga_seen"] = f.data.get("go_arounds", 0)
                if keep is not None:
                    fl["m_level"] = keep
            if fl.get("released") and sec == "ARR" and not self.staffed(w, "DIR"):
                # Released from the stack into the sequence: Arrivals hands it to Director, as at Dubai.
                if (f.id, "handoff") not in w.pending and self._do(w, {"op": "handoff", "flight": f.id, "to": "DIR"}):
                    return
                continue
            if fl.get("released"):
                s_ = w.air.snapshot(f.id)
                # Speed control: 180 kt, or slower when closing on the aircraft ahead in the sequence.
                want = max(f.ti["vapp_kt"] + 20, 180)
                gap, need = self._gap_ahead(w, f, rw)
                slow = max(f.ti["vapp_kt"] + 10, 160)
                if gap is not None and (gap < need + 1.5 or (st.spd == slow and gap < need + 3.0)):
                    want = slow          # slow down when closing; speed up again only once the gap has opened
                short = st.mode == "loc" and w.air.loc_coords((s_["lat"], s_["lon"]), AP.RUNWAYS[rw])[0] < 5.0
                if st.spd != want and (f.id, "speed") not in w.pending and not short and not (st.mode == "loc" and fl.get("slow_done")):
                    self._do(w, {"op": "speed", "flight": f.id, "kt": want})
                    if st.mode == "loc" and want < 180:
                        fl["slow_done"] = True
                    return
                down_wind = fl.get("route") and fl["route"][0] in AP.FIXES and fl["feeder"] in AP.FIXES and \
                    dist_nm((s_["lat"], s_["lon"]), AP.FIXES[fl["route"][0]]) < dist_nm(AP.FIXES[fl["feeder"]], AP.FIXES[fl["route"][0]]) - 2
                if st.alt and st.alt > 3000 and dist_nm((s_["lat"], s_["lon"]), AP.FIXES[fl["feeder"]]) > 8 and down_wind \
                        and (f.id, "altitude") not in w.pending and self._descent_clear(w, f, 3000, rw):
                    self._do(w, {"op": "altitude", "flight": f.id, "ft": 3000, "_check": False})
                    return
                if st.app_rwy is None and (f.id, "approach") not in w.pending and (f.id, "route") not in w.pending and st.mode != "missed":
                    self._do(w, {"op": "approach", "flight": f.id, "runway": rw, "then": self.twr_arr(w)})
                    return
                continue
            if fl.get("feeder") and fl.get("level") and st.alt is not None and abs(st.alt - fl["level"]) > 50 \
                    and (f.id, "altitude") not in w.pending:
                # Hearback: the crew is flying a different level from the one given (a readback error).
                self._do(w, {"op": "altitude", "flight": f.id, "ft": fl["level"], "_check": False})
                return
            if not fl.get("feeder"):
                # Check-in (or back from a go-around): keep the stack level, hold at the feeder. After a go-around,
                # or when the stack is full, hold at the missed approach hold at 3,000 to 5,000 ft.
                reg = w.stack_clean(side)
                lvl = next((L for L, g in reg.items() if g == f.id), None)
                if lvl is None and f.data.get("go_arounds", 0) > 0:
                    slot = w.missed_slot(f.id, rw)
                    if slot is None and w.free_level(side, f.id, claim=False) is None:
                        # No room anywhere: the unit cannot take it back in; advise a diversion.
                        if (f.id, "divert") not in w.pending and "divert" not in w.pilot_flags[f.id]:
                            self._do(w, {"op": "divert", "flight": f.id})
                        return
                    if slot is not None:
                        key, mh = slot
                        if self._do(w, {"op": "hold", "flight": f.id, "fix": mh}):
                            self._do(w, {"op": "altitude", "flight": f.id, "ft": 3000, "_check": False})
                            fl.update(feeder=mh, level=3000, side="M", ga_seen=f.data.get("go_arounds", 0))
                        return
                if lvl is None:
                    lvl = w.free_level(side, f.id)
                    if lvl is None:
                        continue
                    # After a go-around the climb into the stack is cleared only when the levels it crosses are free.
                    if not self._do(w, {"op": "altitude", "flight": f.id, "ft": lvl, "_check": not f.data.get("go_arounds")}):
                        reg.pop(lvl, None)
                        continue
                via = [x for x in proc["sides"][side].get("via", {}).get(f.entry, []) if x in (st.route or [])]
                op = {"op": "route", "flight": f.id, "fixes": via + [feeder]} if via else {"op": "hold", "flight": f.id, "fix": feeder}
                if self._do(w, op):     # a route ends in the feeder's published hold
                    fl.update(feeder=feeder, level=lvl, side=side, ga_seen=f.data.get("go_arounds", 0))
                return
        # Step down into a free slot below.
        for side in ("N", "S"):
            reg = w.stack_clean(side)
            for lvl in sorted(reg):
                below = lvl - 1000
                if below < 6000 or below in reg:
                    continue
                fid = reg[lvl]
                f = w.flights[fid]
                if f.sector != sec or (fid, "altitude") in w.pending or not f.data.get("si", {}).get("feeder") \
                        or f.data["si"].get("released"):
                    continue
                if not self._level_free_near(w, fid, below, f.data["si"]["feeder"]):
                    continue
                if self._do(w, {"op": "altitude", "flight": fid, "ft": below, "_check": False}):
                    reg.pop(lvl)
                    reg[below] = fid
                    f.data["si"]["level"] = below
                    return
        # Release the bottom of a stack.
        cands = []
        for f in mine:
            fl = f.data.get("si", {})
            if fl.get("released") or not fl.get("feeder") or f.phase != "arrival":
                continue
            s = w.air.snapshot(f.id)
            st = w.air.state.get(f.id)
            urgent = "priority" in w.pilot_flags[f.id] or "minfuel" in w.pilot_flags[f.id] or fl.get("side") == "M"
            if s is None or st is None:
                continue
            far = dist_nm((s["lat"], s["lon"]), AP.FIXES[fl["feeder"]])
            if not urgent and (far > 8 or abs(s["alt_ft"] - (st.alt or 0)) > 400):
                continue
            if urgent and fl.get("side") != "M" and far > 12:
                continue       # still on its way to the stack: it leaves from there, not across the departure paths
            cands.append((0 if urgent else 1, st.alt or 0, f))
        if not cands:
            return
        # The two lowest aircraft of each stack that are at their feeder may go (aircraft still flying in to the hold do
        # not block them). Of those whose track miles already give the spacing behind the last aircraft in the
        # sequence, the one with the fewest track miles goes; an aircraft with a fuel or priority problem goes first.
        lowest = {}
        for urg, alt, f in cands:
            sd = f.data["si"].get("side")
            lowest[sd] = min(lowest.get(sd, 1e9), alt)
        best = None
        m_busy = any(g.data.get("si", {}).get("released") and g.data["si"].get("side") == "M" and g.phase == "arrival"
                     and any(isinstance(x, str) and x.startswith("F") for x in (w.air.state[g.id].route or []))
                     for g in w.flights.values() if g.id in w.air.state)
        for urg, alt, f in cands:
            if not urg == 0 and alt > lowest[f.data["si"].get("side")] + 1100:
                continue
            if f.data["si"].get("side") == "M" and m_busy:
                continue       # one aircraft at a time from the missed approach holds to the feeder at 3,000 ft
            if f.data["si"].get("feeder") == proc["missed_hold2"] and w.stack_clean("M"):
                continue       # from the outer hold only once the inner one (on its way in) is empty
            plan = self._release_plan(w, f, rw, tag, lvp, o)
            if plan is None:
                continue
            key = (urg, plan[0])
            if best is None or key < best[0]:
                best = (key, f, plan)
        if best is not None:
            _, f, (mine_tm, need_after, route) = best
            fl = f.data["si"]
            self.releases = getattr(self, "releases", [])
            self.releases.append((w.t, f.id, fl.get("side"), round(mine_tm, 1), round(need_after, 1)))
            if self._do(w, {"op": "route", "flight": f.id, "fixes": route}):
                self._do(w, {"op": "speed", "flight": f.id, "kt": max(f.ti["vapp_kt"] + 20, 180)})
                self._do(w, {"op": "approach", "flight": f.id, "runway": rw, "then": self.twr_arr(w)})
                fl["released"] = w.t
                fl["route"] = route

    def _release_plan(self, w, f, rw, tag, lvp, o):
        """(track miles, track miles needed, route) when the aircraft can leave its hold now, else None."""
        if f.cat and o.rvr_or_vis < MINIMA_RVR[f.cat]:
            return None
        fl = f.data["si"]
        side_ = fl["side"] if fl["side"] in ("N", "S") else "S"
        if fl["side"] != "M":
            route = [f"D{tag}{side_}8", f"{tag}F11"]
        else:
            # From a missed approach hold: by the nearer feeder (under its stack) to the short end of that downwind.
            near = min(("N", "S"), key=lambda sd: dist_nm(AP.FIXES[fl["feeder"]], AP.FIXES[f"F{tag}{sd}"]))
            route = [f"F{tag}{near}", f"D{tag}{near}4", f"{tag}F11"]
        s = w.air.snapshot(f.id)
        mine_tm = AP.track_miles((s["lat"], s["lon"]), route, rw)
        need_after = 0.0
        for g in w.flights.values():
            if g.id == f.id or g.kind != "ARR":
                continue
            gs = w.air.state.get(g.id)
            gsi = g.data.get("si", {})
            if gs is None or (gs.app_rwy != rw and not (gsi.get("released") and gsi.get("route"))):
                continue
            gsnap = w.air.snapshot(g.id)
            if gs.mode == "loc":
                tm = w.air.loc_coords((gsnap["lat"], gsnap["lon"]), AP.RUNWAYS[rw])[0]
            elif gs.app_rwy == rw and gs.route:
                tm = AP.track_miles((gsnap["lat"], gsnap["lon"]), [x for x in gs.route if isinstance(x, str)], rw)
            else:
                tm = AP.track_miles((gsnap["lat"], gsnap["lon"]), gsi["route"], rw)
            if tm > 60.0:
                continue     # far off the arrival path (e.g., deviating): not in the sequence until it comes back
            need = 10.0 if lvp else max(3.0, wake_nm(g.group, f.group)) + 2.0
            if w.config["mode"].startswith("single"):
                need = max(need, max(3.0, wake_nm(g.group, f.group)) + 5.0)   # gaps for departures in mixed mode
            need_after = max(need_after, tm + need)
        if mine_tm < need_after:
            return None
        return mine_tm, need_after, route

    def _alt_ok(self, w, f, target, radius=12.0):
        """A level change is safe when no other aircraft within radius NM is, or is cleared to be, inside the band
        the aircraft will cross (900 ft either side)."""
        s = w.air.snapshot(f.id)
        if s is None:
            return False
        lo, hi = min(target, s["alt_ft"]) - 900, max(target, s["alt_ft"]) + 900
        for gid, st in w.air.state.items():
            if gid == f.id:
                continue
            g = w.air.snapshot(gid)
            if g is None or dist_nm((s["lat"], s["lon"]), (g["lat"], g["lon"])) > radius:
                continue
            if lo < g["alt_ft"] < hi or (st.alt is not None and lo < st.alt < hi):
                return False
        return True

    def _level_free_near(self, w, fid, lvl, fix, radius=15.0):
        """No other aircraft within radius NM of the hold fix is at, or cleared to, a level within 900 ft of lvl, and
        no other stack has lvl reserved."""
        if any(r.get(lvl) not in (None, fid) for r in w.side_levels.values()):
            return False
        c = AP.FIXES[fix]
        for gid, gst in w.air.state.items():
            if gid == fid:
                continue
            g = w.air.snapshot(gid)
            if g is None or dist_nm(c, (g["lat"], g["lon"])) > radius:
                continue
            if abs(g["alt_ft"] - lvl) < 900 or (gst.alt is not None and abs(gst.alt - lvl) < 900 and abs(g["alt_ft"] - lvl) < 3000):
                return False
        return True

    def _gap_ahead(self, w, f, rw):
        """Track miles to the aircraft ahead in the approach sequence, and the spacing needed behind it."""
        seq = []
        for g in w.flights.values():
            gs = w.air.state.get(g.id)
            gsi = g.data.get("si", {})
            if g.kind != "ARR" or gs is None or not (gs.app_rwy == rw or (gsi.get("released") and gsi.get("route"))):
                continue
            gsnap = w.air.snapshot(g.id)
            if gs.mode == "loc":
                tm = w.air.loc_coords((gsnap["lat"], gsnap["lon"]), AP.RUNWAYS[rw])[0]
            else:
                rt = [x for x in (gs.route or []) if isinstance(x, str)] or gsi.get("route") or []
                tm = AP.track_miles((gsnap["lat"], gsnap["lon"]), rt, rw) if rt else None
            if tm is not None:
                seq.append((tm, g))
        mine = next((tm for tm, g in seq if g.id == f.id), None)
        if mine is None:
            return None, 0.0
        ahead = [(tm, g) for tm, g in seq if tm < mine and g.id != f.id]
        if not ahead:
            return None, 0.0
        tm, g = max(ahead, key=lambda x: x[0])
        return mine - tm, max(3.0, wake_nm(g.group, f.group))

    def _descent_clear(self, w, f, target, rw, radius=7.0):
        """A released arrival descends to the approach altitude only when no aircraft outside the approach sequence
        (a stack, a missed approach hold, a diversion, a departure) is within radius NM inside the band it crosses."""
        s = w.air.snapshot(f.id)
        if s is None:
            return False
        lo, hi = target - 900, s["alt_ft"] + 900
        for gid, gst in w.air.state.items():
            if gid == f.id or gst.app_rwy == rw:
                continue
            g = w.air.snapshot(gid)
            if g is None or dist_nm((s["lat"], s["lon"]), (g["lat"], g["lon"])) > radius:
                continue
            if lo < g["alt_ft"] < hi or (gst.alt is not None and lo < gst.alt < hi):
                return False
        return True

    def _partial(self, w, f, target, radius):
        return None

    # -- tower ----------------------------------------------------------------------------------------------------
    def _tower_arr(self, w):
        rw = w.arr_rwy
        for f in self.mine(w, self.twr_arr(w)):
            st = w.air.state.get(f.id)
            if st is not None:
                if st.mode in ("missed", "route", "hold", "heading") and st.app_rwy is None:
                    # After a go-around: its own level at the missed approach hold at once, then back to approach.
                    fl = f.data.setdefault("si", {})
                    if fl.get("m_level") is None:
                        slot = w.missed_slot(f.id, rw)
                        if slot is not None and self._do(w, {"op": "hold", "flight": f.id, "fix": slot[1]}):
                            self._do(w, {"op": "altitude", "flight": f.id, "ft": 3000, "_check": False})
                            fl["m_level"] = 3000
                            continue
                    sec = w.sector_of_side(w.side_of(f))
                    # Approach takes it back as a new arrival from the missed approach hold (or its stack).
                    keep = {k: fl[k] for k in ("m_level",) if k in fl}
                    fl.clear()
                    fl.update(keep, ga_seen=f.data.get("go_arounds", 0))
                    self._do(w, {"op": "handoff", "flight": f.id, "to": sec})
                    continue
                if st.mode == "loc" and f.id in w.cleared_land and (f.id, "go_around") not in w.pending:
                    s = w.air.snapshot(f.id)
                    if s is None:
                        continue
                    along, _ = w.air.loc_coords((s["lat"], s["lon"]), AP.RUNWAYS[st.app_rwy])
                    rrsm = w._rrsm_ok(st.app_rwy)
                    occ = [o for o in w.runway_occupants(st.app_rwy, exclude=f.id) if not (rrsm and o[1] == "landing" and o[2] >= 2500)]
                    if along < 1.2 and occ:
                        self._do(w, {"op": "go_around", "flight": f.id})
                    continue
                if st.mode == "loc" and f.id not in w.cleared_land:
                    s = w.air.snapshot(f.id)
                    along, _ = w.air.loc_coords((s["lat"], s["lon"]), AP.RUNWAYS[st.app_rwy])
                    # Cleared to land when the runway is free, or only a departure already rolling is on it (it will be
                    # airborne before this aircraft reaches the threshold).
                    occ = [o for o in w.runway_occupants(st.app_rwy, exclude=f.id) if not (o[1] == "takeoff" and along > 2.0)]
                    if along < 4.0 and not occ:
                        self._do(w, {"op": "land", "flight": f.id, "then": self._gnd_for(w, f)})
                continue
            m = w.movers.get(f.id)
            if m is not None and m.waiting_for is not None and f.data.get("crossing_junction") is not None:
                if self._runway_free_for_crossing(w, f.data["crossing_junction"]):
                    self._do(w, {"op": "cross", "flight": f.id, "then": f.data.get("si_gnd") or self._gnd_for(w, f)})
            elif f.phase == "taxi_in" and m is not None and m.done():
                self._do(w, {"op": "handoff", "flight": f.id, "to": self._gnd_for(w, f)})

    def _runway_free_for_crossing(self, w, j):
        body = w.ground.junction[j]["body"]
        rw = w.arr_rwy if w.body(w.arr_rwy) == body else w.dep_rwy
        if w.runway_occupants(rw):
            return False
        for fid, st in w.air.state.items():
            if st.app_rwy and w.body(st.app_rwy) == body:
                s = w.air.snapshot(fid)
                along, _ = w.air.loc_coords((s["lat"], s["lon"]), AP.RUNWAYS[st.app_rwy])
                if along < 2.5:
                    return False      # a crossing takes under a minute; the next arrival is at least a minute out
        for fid, L in w.landing.items():
            if w.body(L["rwy"]) == body:
                return False
        for fid, T in w.takeoff.items():
            if w.body(T["rwy"]) == body:
                return False
        for fid, (r, jj) in w.lined.items():
            if w.body(r) == body:
                return False
        return True

    def _tower_dep(self, w):
        rw = w.dep_rwy
        mixed = w.config["mode"].startswith("single")
        for f in self.mine(w, self.twr_dep(w)):
            # After a runway change: departures at the old runway go back to ground for the new one.
            if f.data.get("dep_rwy") and f.data["dep_rwy"] != rw and f.phase in ("lined_up", "holding", "taxi_out"):
                if f.phase == "lined_up":
                    self._do(w, {"op": "cancel_takeoff", "flight": f.id})
                else:
                    self._do(w, {"op": "handoff", "flight": f.id, "to": self._gnd_for(w, f)})
                return
        # A line-up or take-off clearance still on the frequency counts as given (the readback is not yet back).
        on_air = {op for (g, op) in w.pending if op in ("lineup", "takeoff")}
        for f in sorted(self.mine(w, self.twr_dep(w)), key=lambda x: x.data.get("ready_t") or 0):
            if f.phase == "lined_up":
                if "takeoff" in on_air:
                    continue
                if w.t < f.data.get("lined_t", 0):
                    continue
                if self._dep_gap_ok(w, f) and not (mixed and self._arrival_close(w, rw, 2.5)) \
                        and not (not mixed and self._parallel_short_final(w)):
                    self._do(w, {"op": "takeoff", "flight": f.id, "then": w.dep_position(f.gate)})
                continue
            if f.phase == "holding":
                if any(w.body(r) == w.body(rw) for r, _ in w.lined.values()) or w.takeoff or on_air:
                    continue
                if mixed and (self._arrival_close(w, rw, 4.0) or not self._dep_gap_ok(w, f, ahead_s=30.0)):
                    continue       # one runway for both: line up only when the take-off can follow at once
                if w.runway_closed(f.data.get("dep_rwy")):
                    continue
                ctot = f.data.get("ctot")
                if ctot is not None and w.t < ctot - 150 - 45:
                    continue       # line up only for a take-off inside the slot window
                self._do(w, {"op": "lineup", "flight": f.id})
                break
            m = w.movers.get(f.id)
            if m is not None and m.waiting_for is not None and f.data.get("crossing_junction") is not None:
                if self._runway_free_for_crossing(w, f.data["crossing_junction"]):
                    self._do(w, {"op": "cross", "flight": f.id, "then": self._gnd_for(w, f)})

    def _parallel_short_final(self, w, nm=3.0):
        """An arrival on the parallel runway is inside nm of its threshold and not yet down: a take-off now would put
        the departure beside it if it goes around (the runways are 385 m apart and dependent)."""
        rw = w.arr_rwy
        for fid, st in w.air.state.items():
            if st.app_rwy == rw and st.mode == "loc":
                s = w.air.snapshot(fid)
                along, _ = w.air.loc_coords((s["lat"], s["lon"]), AP.RUNWAYS[rw])
                if -0.2 < along < nm:
                    return True
        return False

    def _arrival_close(self, w, rw, nm):
        for fid, st in w.air.state.items():
            if st.app_rwy and w.body(st.app_rwy) == w.body(rw):
                s = w.air.snapshot(fid)
                along, _ = w.air.loc_coords((s["lat"], s["lon"]), AP.RUNWAYS[st.app_rwy])
                if along < nm:
                    return True
        return any(w.body(L["rwy"]) == w.body(rw) for L in w.landing.values())

    def _dep_gap_ok(self, w, f, ahead_s=0.0):
        from atcsim.model import wake_dep_s
        if any(e[3] == "go_around" and w.t - e[1] < 120 for e in list(w.events)[-40:]):
            return False   # a go-around on the parallel runway: no take-off for 2 minutes
        if not w.last_dep:
            return True
        t0, g, gate, r, st0, _f = w.last_dep[-1]
        need = max(wake_dep_s(g, f.group), 120.0 if gate == (f.gate or "WEST") else 90.0)
        return w.t + ahead_s - t0 >= need

    # -- ground ---------------------------------------------------------------------------------------------------
    def _ground(self, w, sec):
        for f in self.mine(w, sec):
            m = w.movers.get(f.id)
            if f.kind == "ARR" and f.phase == "taxi_in":
                sid = f.data.get("stand")
                if not sid:
                    sid = w.pick_stand(f)
                    if sid:
                        w.instruct(None, {"op": "assign_stand", "flight": f.id, "stand": sid}) if not (self.staffed(w, "GND") or self.staffed(w, "GND_2")) else None
                        sid = f.data.get("stand")
                if sid and m is not None and (m.done() or m.path[-1] != w.ground.stands[sid]["node"]) and not f.data.get("si_taxi"):
                    if self._do(w, {"op": "taxi", "flight": f.id, "to": sid}):
                        f.data["si_taxi"] = True
            elif f.kind == "DEP" and f.phase == "pushed" and f.data.get("push_done_t") is None and \
                    (not f.data.get("si_taxi") or w.t - f.data.get("si_taxi_t", w.t) > 120):
                # (again after 2 minutes if the clearance did not take, e.g., it was refused when read back)
                if self._do(w, {"op": "taxi", "flight": f.id, "runway": w.dep_rwy, "then": self.twr_dep(w)}):
                    f.data["si_taxi"] = True
                    f.data["si_taxi_t"] = w.t
            elif f.kind == "DEP" and f.phase in ("taxi_out", "holding") and (f.data.get("needs_taxi") or (f.data.get("dep_rwy") and f.data["dep_rwy"] != w.dep_rwy)):
                if self._do(w, {"op": "taxi", "flight": f.id, "runway": w.dep_rwy, "then": self.twr_dep(w)}):
                    f.data.pop("needs_taxi", None)
            if m is not None and m.waiting_for is not None and f.data.get("crossing_junction") is not None:
                j = f.data["crossing_junction"]
                body = w.ground.junction[j]["body"]
                twr = w.tower_position(body)
                f.data["si_gnd"] = sec
                if not f.data.get("si_xfer") == j:
                    if self._do(w, {"op": "handoff", "flight": f.id, "to": twr}):
                        f.data["si_xfer"] = j

    # -- departure radar ------------------------------------------------------------------------------------------
    def _departure(self, w, sec):
        deps = [(f, w.air.state.get(f.id), w.air.snapshot(f.id)) for f in self.mine(w, sec)]
        deps = [(f, st, s) for f, st, s in deps if st is not None and s is not None]
        for f, st, s in deps:
            pos = (s["lat"], s["lon"])
            # Same route, closing up behind another departure at about the same level: slow down.
            nxt = next((x for x in (st.route or []) if isinstance(x, str)), None)
            for g, gst, gs in deps:
                if g is f or nxt is None or nxt not in (gst.route or []) or abs(gs["alt_ft"] - s["alt_ft"]) >= 1000:
                    continue
                if dist_nm(pos, (gs["lat"], gs["lon"])) < 5.0 and dist_nm((gs["lat"], gs["lon"]), AP.FIXES[nxt]) < dist_nm(pos, AP.FIXES[nxt]):
                    if st.spd != 210 and (f.id, "speed") not in w.pending:
                        self._do(w, {"op": "speed", "flight": f.id, "kt": 210})
                    break
            rw = w.arr_rwy
            proc = AP.proc_for(rw)
            near_holds = min(dist_nm(pos, AP.FIXES[proc["sides"][sd]["feeder"]]) for sd in ("N", "S"))
            near_entry = min(dist_nm(pos, AP.ENTRY[e]["pos"]) for e in AP.ENTRY)
            if not f.data.get("si_climb") and s["alt_ft"] > 2500 and near_holds > 12 and near_entry > 15 \
                    and dist_nm(pos, AP.ARP) > 15:
                # Climb only once clear of the arrival holds and the inbound flows near the airport; until then stay at
                # 4,000 ft, below the stacks and above the missed approach holds.
                if self._do(w, {"op": "altitude", "flight": f.id, "ft": 15000, "_radius": 15.0}):
                    f.data["si_climb"] = True
            elif dist_nm((s["lat"], s["lon"]), AP.ARP) > 25 and s["alt_ft"] > 9000:
                self._do(w, {"op": "handoff", "flight": f.id, "to": "CENTER"})
