"""The controller API: status, query, events and act, for one seat at a time. docs/API.md describes it.

Every seat sees the shared radar, the flight strips, the weather and the airport. What a pilot says on a frequency
(check-in calls, readbacks, requests, fuel calls) reaches only the seat working that frequency.
"""
import math
import threading
import time

from . import airport as AP
from .geo import dist_nm
from .radio import SECTORS, AIRTIME, MAX_BACKLOG_S
from .world import Refusal, RADIO_OPS, SYSTEM_OPS

RATE_PER_SEC = 6.0
RADAR_SWEEP_S = 4.0      # approach radar antenna turn: the picture updates this often
RADAR_SIGMA_M = 40.0     # radar position error (one standard deviation, metres) at terminal ranges
BURST = 40.0
MAX_OPS = 20
MAX_ROWS = 400


MAX_FILTER = 200  # values in a query's ids, a where field or an events kinds list


def _check_filters(body):
    for key in ("ids", "kinds"):
        v = body.get(key)
        if v is not None and (not isinstance(v, list) or len(v) > MAX_FILTER or not all(isinstance(x, str) for x in v)):
            raise ApiError(f"{key} must be a list of at most {MAX_FILTER} strings")
    for key in ("limit", "offset", "after"):
        v = body.get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or abs(v) > 1e15):
            raise ApiError(f"{key} must be a number")
    where = body.get("where")
    if where is not None:
        if not isinstance(where, dict) or len(where) > 16:
            raise ApiError("where must be an object with at most 16 fields")
        for v in where.values():
            if isinstance(v, (list, dict)) and len(v) > MAX_FILTER:
                raise ApiError(f"a where field takes at most {MAX_FILTER} values")


class ApiError(Exception):
    def __init__(self, msg, code=400, **extra):
        super().__init__(msg)
        self.code, self.extra = code, extra


class Bucket:
    def __init__(self):
        self.s = {}
        self.lock = threading.Lock()

    def take(self, seat, cost, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            tok, last = self.s.get(seat, (BURST, now))
            tok = min(BURST, tok + (now - last) * RATE_PER_SEC)
            if tok >= cost:
                self.s[seat] = (tok - cost, now)
                return 0.0
            self.s[seat] = (tok, now)
            return (cost - tok) / RATE_PER_SEC


class Api:
    def __init__(self, world):
        self.w = world
        self.bucket = Bucket()

    def call(self, seat, endpoint, body, phase="running"):
        # The clock thread waits while a call runs (both hold the hub's lock), so query and events filters are bounded.
        try:
            if endpoint not in ("status", "query", "events", "act"):
                raise ApiError(f"no endpoint {endpoint}", 404)
            if not isinstance(body, dict):
                raise ApiError("the body must be a JSON object")
            cost = 1
            if endpoint == "act":
                ops = body.get("ops")
                if not isinstance(ops, list) or not ops:
                    raise ApiError("act needs a non-empty ops list")
                if len(ops) > MAX_OPS:
                    raise ApiError(f"at most {MAX_OPS} ops per call")
                cost = len(ops)
            else:
                _check_filters(body)
                if endpoint == "query" and getattr(self, "_q_" + str(body.get("of")), None) is None:
                    raise ApiError("of must be one of: " + ", ".join(sorted(k[3:] for k in dir(self) if k.startswith("_q_"))))
            self._phase = phase
            wait = self.bucket.take(seat, cost)
            if wait:
                raise ApiError(f"rate limit: retry in {wait:.2f} s", 429, retry_after_sec=round(wait, 2))
            result = getattr(self, "_" + endpoint)(seat, body)
            return 200, {"ok": True, "result": result, "status": self._status_short(seat, phase)}
        except ApiError as e:
            return e.code, {"ok": False, "error": str(e), **e.extra}

    # -- status ---------------------------------------------------------------------------------------------------
    def _status_short(self, seat, phase):
        w = self.w
        return {"phase": phase, "t": round(w.t), "clock": w.clock(), "day_end_t": w.t_end, "runways": {"arr": w.arr_rwy, "dep": w.dep_rwy,
                "dir": w.config["dir"], "mode": w.config["mode"]}, "lvp": w.lvp["state"], "ramp_closed": w.ramp_closed,
                "your_sectors": w.sectors.sectors_of(seat), "events_seq": w.seq}

    def _status(self, seat, body):
        w = self.w
        frs = w.sectors.seat_freqs(seat)
        o = w.weather.current(w.t)
        return {**self._status_short(seat, getattr(self, "_phase", "running")), "metar": o.raw, "runway_state": w.weather.runway_state(w.t),
                "your_frequencies": {s: {"backlog_s": round(fr.backlog(w.t), 1), "on_frequency": [f.id for f in w.flights.values() if f.sector == s]} for s, fr in frs.items()},
                "sectors": {s: (o_ or "unstaffed") for s, o_ in w.sectors.owner.items()}}

    # -- query ----------------------------------------------------------------------------------------------------
    def _query(self, seat, body):
        of = body.get("of")
        fn = getattr(self, "_q_" + str(of), None)
        if fn is None:
            raise ApiError("of must be one of: " + ", ".join(sorted(k[3:] for k in dir(self) if k.startswith("_q_"))))
        rows = fn(seat, body)
        if isinstance(rows, list):
            ids = body.get("ids")
            if ids:
                ids = set(ids)
                rows = [r for r in rows if r.get("id") in ids]
            flt = body.get("where") or {}
            for k, v in flt.items():
                vals = v if isinstance(v, list) else [v]
                rows = [r for r in rows if r.get(k) in vals]
            lim = min(int(body.get("limit") or MAX_ROWS), MAX_ROWS)
            off = max(0, int(body.get("offset") or 0))
            return {"rows": rows[off:off + lim], "total": len(rows), "offset": off}
        return rows

    def _q_traffic(self, seat, body):
        """The radar: every aircraft in the air in the Dubai area, as the approach radar shows it. The picture updates
        once per antenna turn (RADAR_SWEEP_S), positions carry the radar's own error, and Mode C/S altitude comes in
        100 ft steps. Between two turns every query returns the same picture."""
        w = self.w
        sweep = int(w.t // RADAR_SWEEP_S)
        cache = getattr(self, "_radar", None)
        if cache is not None and cache[0] == sweep:
            pic = cache[1]
        else:
            pic = {}
            for fid, st in w.air.state.items():
                s = w.air.snapshot(fid)
                if s is None:
                    continue
                r = w.rng("radar", fid, sweep)
                lat = s["lat"] + r.gauss(0.0, RADAR_SIGMA_M) / 111120.0
                lon = s["lon"] + r.gauss(0.0, RADAR_SIGMA_M) / (111120.0 * math.cos(math.radians(s["lat"])))
                pic[fid] = (sweep * RADAR_SWEEP_S, lat, lon, dict(s))
            self._radar = (sweep, pic)
        out = []
        for fid, (t_s, lat, lon, s) in pic.items():
            st = w.air.state.get(fid)
            f = w.flights.get(fid)
            if st is None or f is None:
                continue
            r = {"id": fid, "kind": f.kind, "type": f.type, "group": f.group, "radar_t": t_s, "lat": round(lat, 4), "lon": round(lon, 4),
                 "alt_ft": int(round(s["alt_ft"] / 100.0)) * 100, "gs_kt": round(s["gs_kt"]), "ias_kt": round(s["cas_kt"]), "trk": round(s["trk"]),
                 "hdg": round(s["hdg"]), "vs_fpm": int(round(s["vs_fpm"] / 100.0)) * 100, "sector": f.sector,
                 "squawk": "7600" if w.nordo(f) else ("7700" if w.pilot_flags.get(fid) and ({"mayday", "emergency"} & w.pilot_flags[fid]) else None),
                 "cleared": w.strip_of(f),
                 "dist_arp_nm": round(dist_nm((lat, lon), AP.ARP), 1)}
            out.append(r)
        return out

    def _q_flights(self, seat, body):
        """Flight strips: every flight of the day with its schedule and state."""
        w = self.w
        out = []
        for f in w.flights.values():
            r = {"id": f.id, "kind": f.kind, "airline": f.airline, "type": f.type, "group": f.group, "code": f.code, "reg": f.reg,
                 "other": f.other, "sched_t": f.sched, "phase": {"ready": "not_ready", "final": "arrival"}.get(f.phase, f.phase),
                 "sector": f.sector, "outcome": f.outcome,
                 "done_t": f.t_done, "approach_cat": f.cat, "pax": f.pax}
            if f.kind == "ARR":
                r.update({"entry": f.entry, "eta_entry_t": f.eta_entry + max(0.0, f.edct - f.origin_dep_t) if f.edct is not None and f.origin_dep_t is not None and f.eta_entry is not None else f.eta_entry, "origin_dep_t": f.origin_dep_t, "edct_t": f.edct,
                          "stand": f.data.get("stand"), "next_dep": f.next_dep, "diverted_to": f.diverted_to,
                          "return_of": f.data.get("return_of"), "go_arounds": f.data.get("go_arounds", 0),
                          "feeder_side": f.data.get("sides", {}).get(w.arr_rwy)})
            else:
                r.update({"stand": f.stand, "gate": f.gate, "prev_arr": f.prev_arr, "ctot_t": f.data.get("ctot"),
                          "dep_runway": f.data.get("dep_rwy"), "cancel_reason": f.cancel_reason})
            out.append(r)
        return out

    def _q_ground(self, seat, body):
        """Surface movement radar: aircraft moving or holding on the taxiways."""
        w = self.w
        from .ground import pos_xy
        out = []
        for fid, m in w.movers.items():
            f = w.flights[fid]
            x, y = pos_xy(w.ground, m)
            nxt = None
            for k in range(m.i + 1, len(m.path)):
                if m.path[k] in w.ground.junction:
                    nxt = {"node": m.path[k], "runway": "12L/30R" if w.ground.junction[m.path[k]]["body"] == "N" else "12R/30L"}
                    break
            out.append({"id": fid, "kind": f.kind, "type": f.type, "phase": f.phase, "sector": f.sector, "x_m": round(x), "y_m": round(y),
                        "node": m.path[m.i], "to_node": m.path[-1], "remaining_nodes": len(m.path) - 1 - m.i,
                        "holding": m.waiting_for is not None, "holding_since_t": m.waiting_for, "next_runway": nxt,
                        "stand": f.data.get("stand") or f.stand})
        for fid, (r, j) in w.lined.items():
            out.append({"id": fid, "kind": "DEP", "phase": "lined_up", "runway": r, "node": j, "sector": w.flights[fid].sector})
        for fid, L in w.landing.items():
            out.append({"id": fid, "kind": "ARR", "phase": "landing", "runway": L["rwy"], "rolled_m": round(L["d"]), "sector": w.flights[fid].sector})
        for fid, T in w.takeoff.items():
            out.append({"id": fid, "kind": "DEP", "phase": "takeoff", "runway": T["rwy"], "rolled_m": round(T["d"]), "sector": w.flights[fid].sector})
        return out

    def _q_runways(self, seat, body):
        w = self.w
        occ = {r: w.runway_occupants(r) for r in ("30L", "30R", "12L", "12R")}
        return {"config": {"dir": w.config["dir"], "mode": w.config["mode"], "arr": w.arr_rwy, "dep": w.dep_rwy},
                "occupied": {r: [{"id": a, "role": b, "pos_m": round(c)} for a, b, c in v] for r, v in occ.items()},
                "closures": [{"body": b, "runways": "12L/30R" if b == "N" else "12R/30L", "from_t": t0, "to_t": t1, "reason": why}
                             for b, t0, t1, why in w.closures if t1 > w.t - 3600],
                "last_departures": [{"t": t, "group": g, "gate": gate, "runway": r, "start_m": round(s0)} for t, g, gate, r, s0, _f in w.last_dep[-6:]],
                "last_landings": {r: {"t": v[0], "group": v[1], "id": v[2]} for r, v in w.last_landing.items()},
                "cleared_to_land": sorted(w.cleared_land), "lvp": w.lvp, "lvp_conditions": w.lvp_conditions(),
                "runway_state": w.weather.runway_state(w.t)}

    def _q_stands(self, seat, body):
        w = self.w
        out = []
        for sid, s in w.ground.stands.items():
            occ = w.stand_occ.get(sid)
            out.append({"id": sid, "area": s["area"], "contact": s["contact"], "code": s["code"], "x_m": s["xy"][0], "y_m": s["xy"][1],
                        "node": s["node"], "closed": sid in w.ground.closed_stands, "occupied_by": occ,
                        "parked": occ is not None and occ in w.parked})
        return out

    def _q_weather(self, seat, body):
        w = self.w
        o = w.weather.current(w.t)
        hist = [x.raw for x in w.weather.history(w.t, w.t - 6 * 3600)]
        out = {"metar": o.raw, "recent_metars": hist[-24:], "tafs": [x["raw"] for x in w.weather.tafs_issued(w.t)][-4:],
               "weather_radar": w.weather.precip_grid(w.t),
               "ramp_closed": w.ramp_closed, "runway_state": w.weather.runway_state(w.t), "lvp": w.lvp["state"],
               "alternates": {}}
        w.release_alternate_stands()
        for code, a in AP.ALTERNATES.items():
            ao = w.weather.alternate_obs(code, w.t)
            out["alternates"][code] = {"name": a["name"], "dist_nm": a["dist_nm"], "ils": a["ils"], "a380": a["a380"],
                                       "diversion_stands_in_use": w.alt_used[code], "free_stands_left": a["free_stands"] - w.alt_used[code],
                                       "metar": ao.raw if ao else None}
        return out

    def _q_sectors(self, seat, body):
        w = self.w
        out = {}
        for s, (kind, desc) in SECTORS.items():
            fr = w.sectors.freq_of(s)
            out[s] = {"kind": kind, "description": desc, "worked_by": w.sectors.owner[s] or "unstaffed",
                      "frequency": fr.name if fr else None, "backlog_s": round(fr.backlog(w.t), 1) if fr else None,
                      "aircraft": [f.id for f in w.flights.values() if f.sector == s]}
        return out

    def _q_flow(self, seat, body):
        w = self.w
        return {"mit_nm": dict(w.mit), "center_delays": {f.id: round(f.data.get("center_delay_s", 0)) for f in w.flights.values()
                                                         if f.kind == "ARR" and f.phase in ("scheduled",) and f.data.get("center_delay_s")},
                "reserve_crews_left": dict(w.crews_left),
                "closed_exits": {g: {"since_t": round(t), "reason": r} for g, (t, r) in w.closed_exits.items()},
                "center_levels": {side: {str(k): v for k, v in sorted(reg.items())} for side, reg in w.side_levels.items()}}

    def _q_chart(self, seat, body):
        """Static data: fixes, holds, runways, exits, departure entries. Also in docs/airport.json."""
        w = self.w
        return {"fixes": AP.chart(), "holds": {k: {kk: vv for kk, vv in v.items()} for k, v in AP.HOLDS.items()},
                "runways": {r: {"threshold": list(rw.thr), "heading": rw.heading, "length_m": rw.length_m, "lda_m": rw.lda_m,
                                "displaced_m": rw.displaced_m} for r, rw in AP.RUNWAYS.items()},
                "exits": {r: [{"node": n, "dist_m": d, "rapid": rp} for n, d, rp in v] for r, v in w.ground.exits.items()},
                "entries": {r: [{"node": n, "start_m": d, "full_length": fl} for n, d, fl in v] for r, v in w.ground.entries.items()},
                "gates": {k: list(v) for k, v in AP.GATES.items()}}

    # -- events ---------------------------------------------------------------------------------------------------
    def _events(self, seat, body):
        w = self.w
        after = int(body.get("after") or 0)
        lim = min(int(body.get("limit") or 300), 1000)
        mine = "seat:" + seat
        kinds = body.get("kinds")
        out = []
        ev = w.events
        start = 0
        if ev:
            first = ev[0][0]
            start = max(0, min(len(ev), after - first + 1))   # sequence numbers are consecutive
        import itertools
        for seq, t, scope, kind, data in itertools.islice(ev, start, None):
            if seq <= after:
                continue
            if scope is not None and scope != mine:
                continue
            if kinds and kind not in kinds:
                continue
            if kind == "radio":     # what was heard on the frequency, as words: no call type, flight id or op
                fq = data.get("freq")
                data = {"frequency": fq.split(":", 1)[1] if isinstance(fq, str) and fq.startswith("sector:") else fq,
                        "text": data.get("heard") or data.get("text")}
            out.append({"seq": seq, "t": t, "clock": w.clock(t), "kind": kind, **data})
            if len(out) >= lim:
                break
        return {"events": out, "last_seq": out[-1]["seq"] if out else after, "head_seq": w.seq}

    # -- act ------------------------------------------------------------------------------------------------------
    def _act(self, seat, body):
        res = []
        for op in body["ops"]:
            if not isinstance(op, dict):
                res.append({"ok": False, "error": "each op must be an object"})
                continue
            try:
                r = self.w.instruct(seat, op)
                res.append({"ok": True, **(r if isinstance(r, dict) else {})})
            except Refusal as e:
                res.append({"ok": False, "op": op.get("op"), "flight": op.get("flight"), "error": str(e), **e.extra})
            except (KeyError, TypeError, ValueError, AttributeError, OverflowError, IndexError) as e:
                res.append({"ok": False, "op": op.get("op"), "error": f"bad op: {type(e).__name__}: {e}"})
        return {"results": res}
