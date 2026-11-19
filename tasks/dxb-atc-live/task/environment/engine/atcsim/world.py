"""The world: one day at OMDB. Flights, pilots, runways, ground, radio, safety and the score.

World.step(dt) advances one step of sim time (seconds). Controllers act through World.instruct(seat, op), which
checks the op, puts the transmission on the right frequency and applies it when the readback ends. Everything a
pilot or the airport does by itself (fuel, diversions, go-arounds, ramp closures, crews) happens in the step.
"""
import collections
import hashlib
import math
import random

from . import airport as AP
from .air import Air, FT_PER_NM_GS
from .geo import bearing, dest, dist_nm, angdiff
from .ground import Ground, Mover, pos_xy, TAXI_KT, TAXI_LVP_KT, FOLLOW_M, FOLLOW_LVP_M, KT_MS
from .model import (Flight, wake_nm, wake_dep_s, MINIMA_RVR, MINIMA_CEIL, CAT_ORDER, RESERVE_CREWS, MIN_TURN_MIN,
                    RADAR_MIN_NM, RADAR_MIN_FINAL_NM)
from .radio import SECTORS, AIRTIME, PILOT_CALL, MAX_BACKLOG_S, Sectors, Transmission, RADIO_KINDS, GND2_APRONS
from . import phrase as PH
from .weather import Weather

SPAWN_LEAD_S = 900           # arrivals appear this long before they reach the entry fix
ENTRY_ALT_FT = 13000
ENTRY_SPD = 250             # the ground speed Center times its miles in trail with
DEFAULT_MIT_NM = 6.0         # what Center delivers at each entry fix without a request
READBACK_ERROR_P = 0.004     # chance a readback (and the crew's action) has a wrong number
GP_FT_PER_NM = 318.0          # a 3 degree glide path
UNSTABLE_GATE_NM = 4.0        # inside this, an aircraft more than UNSTABLE_HIGH_FT above the glide path goes around
UNSTABLE_HIGH_FT = 1000.0
REPORT_CALLS = {"checkin", "minimum_fuel", "mayday", "request", "deviation_request"}   # passed on at a handoff
# DFLOW, the UAE departure flow service: in congestion a departure gets a take-off slot (CTOT) when it asks for start-up,
# valid 2.5 minutes either side (UAE AIP ENR 1.9.2); a flight that misses it waits for a new one. Slots keep to the
# declared limit of 7 departures in any 10 minutes (ACL Winter 2019 capacity declaration for DXB).
DFLOW_PER_10MIN = 7
DFLOW_WINDOW_S = 150.0
DFLOW_TAXI_EST_S = 15 * 60.0  # start-up request to take-off, as DFLOW estimates it
DEV_REQUEST_MIN = 4.0
A380_ONLY_STANDS = {"B18R", "B21R", "B26R", "F26R"}   # UAE AIP OMDB AD 2.24 stand charts
CODE_F_AVOID = {"U"}          # UAE AIP OMDB AD 2.8: taxilane U is the code E taxilane; code F uses J
WINGTIP_M = 100.0             # how close two big aircraft on taxilanes J and U count as side by side
K_HOLDS = ("K1", "K2", "K3", "K4", "K5")   # UAE AIP OMDB AD 2.8: code F passes behind them only past code D or smaller
K_BEHIND_M = 250.0            # an aircraft stopped on a K1 to K5 link this close to taxiway K is waiting at its holding point
K_STOP_M = 60.0               # a code F crew on K stops this far before the K1 to K5 link it may not pass
INSPECT_S = 15 * 60          # a runway inspection closes the runway this long (the sim's other inspections take 15 to 20 minutes)
BRAKING_REPORT_GAP_S = 30 * 60   # after an inspection, a new braking report needs heavy rain to go on this long        # crews ask for a weather deviation when a cell is this many minutes ahead (ICAO: as early as possible)
PUSH_S = (150, 240)
LINEUP_S = 35.0
TAKEOFF_REACT_S = 8.0
GO_AROUND_BASE_P = 0.0025
MIN_FUEL_CALL_MIN = 10.0     # crews declare minimum fuel this many minutes before they must divert
DIVERT_RETURN_MIN = 75.0     # a diverted aircraft is ready to come back this long after it lands at the alternate
CREDIT_FULL_MIN = 15.0


def credit(delay_min):
    """Credit for a completed flight: full up to 15 minutes late (the on-time standard), then falling linearly
    to 0 at 4 hours 15 minutes late."""
    if delay_min <= CREDIT_FULL_MIN:
        return 1.0
    return max(0.0, 1.0 - (delay_min - CREDIT_FULL_MIN) / 240.0)


class Refusal(Exception):
    def __init__(self, msg, **extra):
        super().__init__(msg)
        self.extra = extra


class World:
    def __init__(self, scenario, secret="dev", standin=None, strict=True):
        self.sc = scenario
        self.secret = secret
        # strict: a failing stage of a step raises (development). Not strict (the live hub, offline runs): the failing
        # stage is recorded in stage_errors and the other stages of the step still run, so the day goes on.
        self.strict = strict
        self.stage_errors = []
        self.t = float(scenario.get("t0", 0.0))
        self.t_end = float(scenario["duration_s"])
        self.base_utc = scenario["start_utc"]         # ISO time of t = 0
        self.weather = Weather(scenario)
        self.ground = Ground()
        self.air = Air(AP.FIXES, AP.RUNWAYS, self.weather)
        self.air.rngf = self.day_rng
        self.sectors = Sectors()
        self.flights = {}
        for d in scenario["flights"]:
            f = Flight(d)
            f.phase = "scheduled" if f.kind == "ARR" else "not_ready"
            self.flights[f.id] = f
        self.events = collections.deque(maxlen=20000)   # (seq, t, scope, kind, data); scope: None = everyone
        self.seq = 0
        self.config = {"dir": scenario.get("initial_dir", "30"), "mode": "segregated", "closed": set(),
                       "change_at": None, "pending_dir": None}
        self.lvp = {"state": "off", "since": 0.0}
        self.mit = {e: DEFAULT_MIT_NM for e in AP.ENTRY}
        self.side_levels = {"N": {}, "S": {}, "M": {}, "M2": {}}   # arrival stacks per side; M, M2: the two missed approach holds
        self.last_entry_slot = {e: -1e9 for e in AP.ENTRY}
        self.alt_used = {k: 0 for k in AP.ALTERNATES}   # diversion stands in use at each alternate
        self.alt_ground = []          # (return flight id, alternate): a diverted aircraft holds its stand until its return flight leaves
        self.crews_left = dict(RESERVE_CREWS)
        self.stand_occ = {}           # stand -> flight id (parked or assigned)
        self.parked = {}              # flight id -> stand, aircraft physically on a stand
        self.movers = {}              # flight id -> Mover
        self.rwy = {"N": [], "S": []}  # runway body -> list of (flight id, role, along-from-12-end m)
        self.landing = {}             # flight id -> dict for the landing roll
        self.takeoff = {}             # flight id -> dict for the take-off roll
        self.lined = {}               # flight id -> (runway, junction)
        self.last_dep = []            # (t start of roll, group, gate, runway, start_m, flight)
        self.last_landing = {}        # runway -> (t, group, flight)
        self.final_order = collections.defaultdict(list)
        self.safety = []              # safety events
        self.fuel = {}                # flight id -> minutes of fuel above divert minimum (arrivals in the air)
        self.pilot_flags = collections.defaultdict(set)
        self.cleared_land = set()
        self.pending = set()          # (flight, op) queued on a frequency, not yet transmitted
        self.pending_alt = {}         # flight -> altitude in a queued altitude instruction
        self._stca_last = {}          # pair -> sim time of its last alert
        self.closures = []            # runway closures (inspection, events): (body, t0, t1, reason)
        self.dflow = {}               # departure -> its expected take-off time in the DFLOW plan (its slot, if it has one)
        self.closed_exits = {}
        self.braking_poor = {}        # runway body -> time a crew reported poor braking (cleared by an inspection)
        self.inspected = {}           # runway body -> time its last inspection ended        # exit gate -> (time closed, reason): departures to it wait on the ground
        self.ils_out = {}             # runway -> time the glide path is back
        self.ramp_closed = False
        self.events_plan = sorted(scenario.get("events", []), key=lambda e: e["t"])
        self._ev_i = 0
        self.standin = standin
        self.stats = collections.Counter()
        self.score_cache = None
        self._place_initial()

    # ------------------------------------------------------------------------------------------------------------
    # helpers
    def crew(self, f):
        """This flight's crew and operator habits, drawn once per run from realistic ranges. Real crews differ: when
        they ask for a weather deviation and by how much, when they turn on their own, when they call minimum fuel or
        divert, how much a go-around costs them, and how long the turnaround takes. Nothing here is announced."""
        c = f.data.get("crew")
        if c is None:
            r = self.day_rng("crew", f.id)
            tri = r.triangular
            c = {"ask_min": tri(2.5, 7.0, 4.0),         # asks for a deviation this far (minutes) before a cell
                 "ask_deg": r.choice((10, 15, 20, 20, 25, 30, 30, 35, 40)),
                 "reask_s": tri(40.0, 150.0, 70.0),     # asks again after this long with no answer
                 "turn_min": tri(1.0, 3.0, 2.0),        # turns on its own when the cell is this close
                 "turn_deg": r.choice((20, 25, 30, 35, 40, 40, 45, 50)),
                 "resume_s": tri(120.0, 420.0, 240.0),  # a departure resumes its own navigation after this long
                 "minfuel": tri(7.0, 15.0, 10.0),       # calls MINIMUM FUEL at this margin (minutes)
                 "divert_bias": tri(-2.0, 4.0, 0.5),    # diverts this many minutes earlier than its diversion fuel
                 "continue_min": tri(-12.0, -6.0, -9.0),  # cleared for the approach: continues down to this
                 "mayday": tri(-15.0, -9.0, -12.0),     # landing fuel below final reserve: MAYDAY FUEL
                 "ga_fuel": tri(3.0, 6.0, 4.0),         # minutes of margin one go-around costs
                 "gate_nm": tri(3.0, 5.0, 4.0),         # stabilised-approach gate
                 "high_ft": tri(600.0, 1200.0, 900.0),  # this far above the glide path at the gate: go around
                 "no_clr_nm": tri(0.3, 1.0, 0.5),       # no landing clearance by here: go around
                 "turn_factor": tri(0.85, 1.35, 1.0),   # turnaround time against the type's minimum
                 "return_min": tri(55.0, 120.0, 75.0),  # a diverted aircraft is back in the air after this long
                 "upstream_min": tri(6.0, 10.0, 8.0),   # a long-haul flight held upstream diverts at this margin
                 # How the aircraft flies: weight, and how its crew flies what it is told.
                 "mass": tri(0.78, 1.0, 0.9),           # of the type's reference mass (BlueSky's performance model)
                 "react_s": tri(2.0, 9.0, 4.0),         # seconds from the end of the readback to the new selection
                 "vs_desc": tri(1200.0, 2600.0, 1800.0),  # descent rate, ft/min
                 "vs_clb": tri(1500.0, 3200.0, 2200.0),   # climb rate, ft/min
                 "hdg_sd": tri(0.5, 3.0, 1.5),          # how closely the crew flies an assigned heading (degrees)
                 "spd_bias": max(-7.0, min(7.0, r.gauss(0.0, 3.0))),  # flies an assigned speed this much off
                 "min_spd_add": tri(10.0, 35.0, 20.0)}  # clean, far from the airport: the slowest it accepts, above its approach speed
            f.data["crew"] = c
        return c

    def day_rng(self, *key):
        """The day's own draws: who the crews are, the rare events, the wind aloft. Drawn once for the day, the same
        in every run, as the real day had the crews it had. No team can know them in advance: the engine code is
        root-only."""
        h = hashlib.sha256(("|".join(map(str, ("day:" + str(self.sc.get("name", "")),) + key))).encode()).digest()
        return random.Random(int.from_bytes(h[:8], "big"))

    def rng(self, *key):
        h = hashlib.sha256(("|".join(map(str, (self.secret,) + key))).encode()).digest()
        return random.Random(int.from_bytes(h[:8], "big"))

    def emit(self, kind, scope=None, **data):
        self.seq += 1
        self.events.append((self.seq, round(self.t, 1), scope, kind, data))
        return self.seq

    def clock(self, t=None):
        """HH:MM:SS UTC for a sim time."""
        t = self.t if t is None else t
        h0, m0 = int(self.base_utc[11:13]), int(self.base_utc[14:16])
        s = int(h0 * 3600 + m0 * 60 + t) % 86400
        return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}Z"

    @property
    def arr_rwy(self):
        if self.config["mode"].startswith("single"):
            return self.config["mode"].split(":")[1]
        return AP.CONFIGS[self.config["dir"]]["arr"]

    @property
    def dep_rwy(self):
        if self.config["mode"].startswith("single"):
            return self.config["mode"].split(":")[1]
        return AP.CONFIGS[self.config["dir"]]["dep"]

    def body(self, rwy):
        return AP.RUNWAY_BODY[rwy]

    def runway_closed(self, rwy):
        b = self.body(rwy)
        return any(c[0] == b and c[1] <= self.t < c[2] for c in self.closures)

    def tower_position(self, body):
        """UAE AIP OMDB AD 2.18: Tower Air North works runway 12L/30R, Air South runway 12R/30L."""
        return "TWR_N" if body == "N" else "TWR_S"

    def dep_position(self, gate):
        """Departures North takes the NORTH and EAST exits, Departures South the SOUTH and WEST exits."""
        return "DEP_N" if gate in ("NORTH", "EAST") else "DEP_S"

    def ground_position(self, stand):
        """UAE AIP OMDB AD 2.18: aircraft on taxilane P and aprons C, E, Q and S call GMC-2, the others GMC-1."""
        return "GND_2" if stand and str(stand)[:1] in GND2_APRONS else "GND"

    def sector_of_side(self, side):
        return "ARR"

    # ------------------------------------------------------------------------------------------------------------
    # initial state: aircraft already parked, arrivals already inbound
    def _place_initial(self):
        stands = self.ground.stands
        for f in list(self.flights.values()):
            if f.kind == "DEP" and f.prev_arr is None:
                sid = f.stand if f.stand in stands else None
                if sid is None or sid in self.stand_occ:
                    sid = self.pick_stand(f)
                if sid is not None:
                    self.stand_occ[sid] = f.id
                    self.parked[f.id] = sid
                    f.stand = sid
                f.tobt = max(f.sched - 15 * 60, f.data.get("ready_t", -1e9))
            if f.kind == "DEP":
                f.crew_out = f.crew_out if f.crew_out is not None else f.sched + self.rng("crew", f.id).uniform(3.0, 4.5) * 3600

    def pick_stand(self, f, exclude=()):
        """A free stand that fits the aircraft, preferring the operator's terminal (the usual allocation rule)."""
        pref = {"UAE": ("A", "B", "C"), "FDB": ("F", "D"), "QFA": ("C",)}.get(f.airline, ("D", "F"))
        order = "ABCDEFG"
        best = None
        for sid, s in self.ground.stands.items():
            if sid in self.stand_occ or sid in exclude or sid in self.ground.closed_stands:
                continue
            if order.index(s["code"]) < order.index(f.code):
                continue
            if sid in A380_ONLY_STANDS and f.code != "F":
                continue
            if s["area"] == "cargo" and not f.ti.get("cargo"):
                continue
            score = (0 if s["area"] in pref else (1 if s["area"] == "remote" else 2), order.index(s["code"]) - order.index(f.code))
            if best is None or score < best[0]:
                best = (score, sid)
        return best[1] if best else None

    # ------------------------------------------------------------------------------------------------------------
    # the step
    def step(self, dt=1.0):
        self.t += dt
        now = self.t
        stages = [self._scheduled_events, self._weather_effects, self._spawn_arrivals, self._departures_ready,
                  lambda: self.air.set_wind(now), lambda: self._steer_air(dt), lambda: self.air.step(int(round(dt))),
                  lambda: self._ground_step(dt), lambda: self._runway_step(dt), self._transitions,
                  lambda: self._radio_step(dt), lambda: self._pilots(dt),
                  self._departure_weather]
        if int(now) % 2 == 0:
            stages.append(self._tcas)
        if int(now) % 4 == 0:
            stages.append(self._separation)
        if int(now) % 5 == 0:
            stages.append(self._conflict_alerts)
        if int(now) % 15 == 0:
            stages.append(self._long_tail)
        if self.standin is not None:
            stages.append(lambda: self.standin.step(self, dt))
        for stage in stages:
            if self.strict:
                stage()
                continue
            try:
                stage()
            except Exception:  # noqa: BLE001 - the day goes on; the error is kept for the operators
                import traceback
                self.stage_errors = (self.stage_errors + [{"t": self.t, "trace": traceback.format_exc()[-1500:]}])[-50:]
                self.stats["stage_errors"] += 1
        if int(now) % 60 == 0:
            self.score_cache = None

    # -- conflict alerts ----------------------------------------------------------------------------------------
    def _conflict_alerts(self):
        """The radar's short-term conflict alert, as every approach unit has. It projects each airborne aircraft
        within 50 NM, between 1,500 and 20,000 ft, up to 90 seconds ahead on its present track, speed and climb or
        descent. A pair that would come within 3.3 NM and 950 ft, not both established on a localizer, raises an
        alert on the frequency of each staffed sector involved, at most once every 2 minutes per pair."""
        ac = []
        for fid, st in self.air.state.items():
            f = self.flights.get(fid)
            snap = self.air.snapshot(fid)
            if f is None or snap is None or not 1500 <= snap["alt_ft"] <= 20000:
                continue
            p = (snap["lat"], snap["lon"])
            if dist_nm(p, AP.ARP) <= 50:
                ac.append((f, st, snap, p))
        for i in range(len(ac)):
            for j in range(i + 1, len(ac)):
                a, b = ac[i], ac[j]
                if a[1].mode == "loc" and b[1].mode == "loc":
                    continue
                # Out of reach within 90 s even closing head-on at 500 kt each, or at 6,000 ft/min each.
                if dist_nm(a[3], b[3]) > 26 or abs(a[2]["alt_ft"] - b[2]["alt_ft"]) > 19000:
                    continue
                pair = tuple(sorted((a[0].id, b[0].id)))
                if self.t - self._stca_last.get(pair, -1e9) < 120:
                    continue
                for dt in (0, 15, 30, 45, 60, 75, 90):
                    pa, za = _project(a, dt)
                    pb, zb = _project(b, dt)
                    d = dist_nm(pa, pb)
                    if d < 3.3 and abs(za - zb) < 950:
                        self._stca_last[pair] = self.t
                        text = f"CONFLICT ALERT: {pair[0]} and {pair[1]}, {d:.1f} NM and {abs(za - zb):.0f} ft in {dt} s"
                        scopes = {self.sectors.scope_of(fr) for fr in {self.freq_for_flight(a[0]), self.freq_for_flight(b[0])} - {None}}
                        for sc in sorted(s for s in scopes if s.startswith("seat:")):
                            self.emit("alert", scope=sc, flights=list(pair), text=text)
                        break

    # -- scheduled events (scenario) ------------------------------------------------------------------------------
    def _scheduled_events(self):
        while self._ev_i < len(self.events_plan) and self.events_plan[self._ev_i]["t"] <= self.t:
            e = self.events_plan[self._ev_i]
            self._ev_i += 1
            self._apply_event(e)

    def _apply_event(self, e):
        k = e["kind"]
        if k == "inspection_done":
            self.braking_poor.pop(e["body"], None)
            self.inspected[e["body"]] = self.t
            self.emit("notam", text=f"Runway {e['runway']} inspection complete: open, {self.weather.runway_state(self.t).lower()}")
            return
        if k == "tow_done":
            if self.stand_occ.get(e["stand"]) == e["flight"] and self.parked.get(e["flight"]) != e["stand"]:
                self.stand_occ.pop(e["stand"], None)
            return
        if k == "runway_closure":
            self.closures.append((e["body"], e["t"], e["t"] + e["dur_s"], e.get("reason", "closure")))
            self.emit("notam", text=e.get("text") or f"Runway {e['body']} closed {self.clock(e['t'])} to {self.clock(e['t'] + e['dur_s'])}: {e.get('reason')}")
        elif k == "notam":
            self.emit("notam", text=e["text"])
        elif k == "medical":
            f = self.flights.get(e["flight"])
            if f:
                self.pilot_flags[f.id].add("priority")
                self.emit("pilot_report", text=f"{f.id} declares PAN PAN, medical emergency on board, requests priority", flight=f.id)
        elif k == "engine_failure":
            f = self.flights.get(e["flight"])
            if f:
                self.pilot_flags[f.id].update({"priority", "emergency", "inspect_after"})
                self.emit("pilot_report", text=f"{f.id} MAYDAY, engine failure, requests priority landing; runway inspection needed after landing", flight=f.id)
        elif k == "bird_strike":
            # The next arrival to land reports a bird strike on short final: the runway needs an inspection after it.
            cands = []
            for fid, st in self.air.state.items():
                if st.mode == "loc" and st.app_rwy:
                    s_ = self.air.snapshot(fid)
                    cands.append((self.air.loc_coords((s_["lat"], s_["lon"]), AP.RUNWAYS[st.app_rwy])[0], fid))
            if cands:
                fid = min(cands)[1]
                self.pilot_flags[fid].add("inspect_after")
                self.emit("pilot_report", flight=fid, text=f"{fid} reports a bird strike on short final; runway inspection needed after landing")
        elif k == "ils_outage":
            self.ils_out[e["runway"]] = e["t"] + e["dur_s"]
            self.emit("notam", text=e.get("text") or f"ILS {e['runway']} glide path unserviceable until {self.clock(e['t'] + e['dur_s'])}: localizer-only approach, minima RVR 1500 m, ceiling 500 ft")
        elif k == "stand_closed":
            self.ground.closed_stands.add(e["stand"])
            if e.get("dur_s"):
                self.events_plan.append({"t": e["t"] + e["dur_s"], "kind": "stand_open", "stand": e["stand"]})
                self.events_plan.sort(key=lambda x: x["t"])
            if e.get("reason") != "flooding":
                self.emit("notam", text=f"Stand {e['stand']} closed: {e.get('reason', 'unserviceable')}")
        elif k == "stand_open":
            self.ground.closed_stands.discard(e["stand"])
        elif k == "taxiway_closed":
            for a, b in e.get("edges", []):
                self.ground.closed_edges.update({(a, b), (b, a)})
            if e.get("dur_s"):
                self.events_plan.append({"t": e["t"] + e["dur_s"], "kind": "taxiway_open", "edges": e["edges"]})
                self.events_plan.sort(key=lambda x: x["t"])
            self.emit("notam", text=e.get("text", "taxiway closed"))
        elif k == "taxiway_open":
            for a, b in e.get("edges", []):
                self.ground.closed_edges.discard((a, b))
                self.ground.closed_edges.discard((b, a))
            self.emit("notam", text="Flooded taxilanes and stands reopened")
        elif k == "reroute":
            # Arrivals from one entry are sent to another, with extra flying time (airspace restriction).
            for f in list(self.flights.values()):
                if f.kind == "ARR" and f.entry == e["from"] and f.phase in ("scheduled", "enroute") and (f.eta_entry or 0) >= e["t"]:
                    f.entry = e["to"]
                    f.eta_entry += e.get("extra_min", 10) * 60
                    f.fuel_min = (f.fuel_min or 30) - e.get("extra_min", 10) * 0.5
            self.emit("notam", text=e["text"])
        elif k == "pilot_incursion":
            # The next departure to hold short of the arrival runway on its way out will cross without a clearance.
            body = self.body(self.arr_rwy)
            for fid, m in self.movers.items():
                f = self.flights[fid]
                if f.kind == "DEP" and any(n in self.ground.junction and self.ground.junction[n]["body"] == body for n in m.path[m.i:]):
                    self.pilot_flags[fid].add("will_incur")
                    break

    # -- weather effects ------------------------------------------------------------------------------------------
    def _weather_effects(self):
        now = self.t
        closed = self.weather.ramp_closed(now, AP.ARP)
        if closed != self.ramp_closed:
            self.ramp_closed = closed
            self.emit("ramp", text=("Lightning within 5 NM: ramp operations suspended (no pushback, towing or marshalling)"
                                    if closed else "Ramp operations resumed"))
        o = self.weather.current(now)
        if int(now) % 60 == 0:
            last = getattr(self, "_last_metar", None)
            if last is not o:
                self._last_metar = o
                self.emit("metar", text=o.raw)
            taf = self.weather.latest_taf(now)
            if taf is not None and getattr(self, "_last_taf", None) is not taf:
                self._last_taf = taf
                self.emit("taf", text=taf["raw"])
        # LVP: the supervisor declares it; preparation takes 10 minutes.
        if self.lvp["state"] == "prep" and now - self.lvp["since"] >= 600:
            self.lvp = {"state": "on", "since": now}
            self.emit("lvp", text="Low visibility procedures in force")

    def lvp_on(self):
        return self.lvp["state"] == "on"

    def lvp_conditions(self):
        o = self.weather.current(self.t)
        return o.rvr_or_vis <= 600 or (o.ceiling_ft is not None and o.ceiling_ft < 300)

    # -- arrivals appear ------------------------------------------------------------------------------------------
    def _spawn_arrivals(self):
        now = self.t
        # Whether a side's stack has a free level does not change while no arrival enters, so the answer is kept
        # for the rest of this step and dropped when one enters. Flights held upstream are not in the air, so the
        # probe's own-flight exclusion never applies to them.
        saturated = {}
        for f in list(self.flights.values()):
            if f.kind != "ARR" or f.phase != "scheduled":
                continue
            eta = f.eta_entry
            if f.origin_dep_t is not None and f.edct is not None:
                eta = f.eta_entry + max(0.0, f.edct - f.origin_dep_t)
            if eta - SPAWN_LEAD_S > now:
                continue
            # A long-haul flight held upstream (below) burns its margin while it waits, and diverts en route once
            # the margin is gone, whether or not a slot has come up.
            held = now - f.data.setdefault("held_since", now)
            if f.origin_dep_t is None and held > 0 and \
                    (f.fuel_min if f.fuel_min is not None else 30.0) - 0.6 * held / 60.0 < self.crew(f)["upstream_min"]:
                self._divert_upstream(f, held)
                continue
            # Center's miles in trail at the entry fix.
            gap = self.mit[f.entry] / ENTRY_SPD * 3600.0
            slot = max(eta, self.last_entry_slot[f.entry] + gap, now if eta < now else eta)
            if slot - SPAWN_LEAD_S > now:
                f.data["center_delay_s"] = slot - eta
                continue
            # The terminal area is saturated on this side (no free stack level): Center holds the flight upstream.
            side = self.side_of(f)
            if side not in saturated:
                saturated[side] = self.free_level(side, f.id, claim=False) is None
            if saturated[side]:
                f.data["center_delay_s"] = now - eta + SPAWN_LEAD_S
                continue
            saturated.clear()      # an arrival enters below: the stacks and the traffic change
            ent = AP.ENTRY[f.entry]
            # Center gives each arrival its own level in its side's stack (the levels the arrivals on that side
            # hold at), the lowest free one from 6,000 ft up, and delivers it at that level: 250 kt below 10,000 ft,
            # 280 kt above.
            lvl = self.free_level(side, f.id, claim=False) or 22000
            cas = 250 if lvl < 10000 else 280
            # Its ground speed at that level along the inbound track (true airspeed grows about 2% per 1,000 ft).
            wd, wk = self.weather.wind_at(now, lvl)
            dd, dk = self.air.wind_error(now, lvl)
            gs = cas * (1.0 + 0.02 * lvl / 1000.0) - max(0.0, wk + dk) * math.cos(math.radians(wd + dd - ent["inbound"]))
            gs = max(150.0, gs)
            # At least mit_nm behind the arrival ahead at the fix, at this aircraft's ground speed.
            slot = max(slot, self.last_entry_slot[f.entry] + self.mit[f.entry] / min(gs, ENTRY_SPD) * 3600.0)
            self.last_entry_slot[f.entry] = slot
            f.data["entry_slot"] = slot
            delay = max(0.0, slot - max(eta, 0.0))     # the day starts at t=0: no holding before it
            fuel0 = f.fuel_min if f.fuel_min is not None else 30.0
            if f.origin_dep_t is not None:
                # Short and medium haul: the en-route centres hold it on the ground at the origin (no fuel used).
                fuel = fuel0
            else:
                fuel = fuel0 - 0.6 * delay / 60.0
                if fuel < self.crew(f)["upstream_min"]:
                    # Long haul held too long upstream: it diverts en route and never reaches Dubai airspace.
                    self._divert_upstream(f, delay)
                    continue
            self.fuel[f.id] = fuel
            back = (ent["inbound"] + 180.0) % 360.0
            lvl = self.free_level(side, f.id) or 22000
            # Placed so that it reaches the entry fix at its slot.
            pos = dest(ent["pos"], back, max(1.0, (slot - now) / 3600.0 * gs))
            st = self.air.create(f.id, f.type, pos, ent["inbound"], lvl, cas)
            self.air.set_traits(f.id, self.crew(f))
            st.mode = "center"
            st.route = [f.entry]
            st.alt = lvl
            st.spd = None      # Center's speed (_default_speed)
            f.phase = "enroute"
            f.data["center_delay_s"] = delay
            self.emit("inbound", flight=f.id, text=f"{f.id} {f.type} from {f.other} estimating {f.entry} {self.clock(slot)}")

    # -- departures become ready --------------------------------------------------------------------------------
    def _departures_ready(self):
        now = self.t
        for f in list(self.flights.values()):
            if f.kind != "DEP" or f.phase != "not_ready":
                continue
            if f.prev_arr is not None:
                a = self.flights.get(f.prev_arr)
                if a is None or a.phase in ("cancelled",):
                    pass
                elif a.outcome == "diverted" or a.phase in ("diverting", "diverted"):
                    continue
                elif a.data.get("on_block_t") is None:
                    continue
                else:
                    ready = a.data["on_block_t"] + MIN_TURN_MIN[f.group] * 60 * self.crew(f)["turn_factor"]
                    f.tobt = max(f.sched - 15 * 60, ready, f.data.get("tobt_floor", -1e9))
                    if f.stand is None and f.id not in self.parked:
                        sid = a.data.get("stand")
                        self.parked[f.id] = sid
                        f.stand = sid
                        if sid:
                            self.stand_occ[sid] = f.id
            if f.tobt is None:
                continue
            if f.id not in self.parked:
                continue
            if now >= f.tobt:
                # Crew duty limit: a flight that cannot be off the ground by the crew's limit needs a reserve crew.
                f.phase = "ready"
                f.data["ready_t"] = now
                self.dflow_slot(f, now + DFLOW_TAXI_EST_S)
                self.pilot_call(f, "ready_pushback", self.ground_position(f.stand), f"{f.id} stand {f.stand}, ready for start-up and pushback")
        for f in list(self.flights.values()):
            if f.kind == "DEP" and f.phase in ("not_ready", "ready", "pushback", "taxi_out", "holding") and f.crew_out and now > f.crew_out and not f.reserve_crew:
                self._crew_out(f)

    def dflow_slot(self, f, earliest):
        """Plan the departure's take-off in DFLOW: the first minute from `earliest` that keeps every 10 minutes to the
        declared limit. More than 2 minutes after `earliest` means congestion: the flight gets that time as its slot."""
        self.dflow.pop(f.id, None)
        # Count take-offs that happened and plans still ahead; a plan whose time passed without a take-off is stale.
        planned = sorted(t for fid, t in self.dflow.items()
                         if self.flights[fid].outcome == "departed" or t >= self.t - DFLOW_WINDOW_S)
        t = math.ceil(earliest / 60.0) * 60.0
        while True:
            busy = max((sum(1 for p in planned if ws <= p < ws + 600) for ws in range(int(t) - 599, int(t) + 1, 30)), default=0)
            if busy < DFLOW_PER_10MIN:
                break
            t += 60.0
        self.dflow[f.id] = t
        if t - earliest > 120:
            f.data["ctot"] = t
            self.emit("flow", flight=f.id, text=f"DFLOW: {f.id} take-off slot (CTOT) {self.clock(t)[:5]}Z, valid {self.clock(t - DFLOW_WINDOW_S)[:5]} to {self.clock(t + DFLOW_WINDOW_S)[:5]}")
        elif f.data.pop("ctot", None) is not None:
            self.emit("flow", flight=f.id, text=f"DFLOW: {f.id} slot cancelled, no slot needed now")
        return f.data.get("ctot")

    def _crew_out(self, f):
        left = self.crews_left.get(f.airline, 0)
        if left > 0:
            self.crews_left[f.airline] = left - 1
            f.reserve_crew = True
            f.crew_out = self.t + 4 * 3600
            # The new crew needs 90 minutes to report; an aircraft taxiing out goes back to a stand.
            if f.phase in ("taxi_out", "holding"):
                self.emit("airline", text=f"{f.id}: crew out of duty hours, returning to stand for a reserve crew", flight=f.id)
                self._return_to_stand(f)
            f.data["tobt_floor"] = self.t + 90 * 60
            f.tobt = max(f.tobt or 0, f.data["tobt_floor"])
            if f.phase == "ready":
                f.phase = "not_ready"
            self.emit("airline", text=f"{f.id}: original crew out of duty hours; reserve crew called, new ready time {self.clock(f.tobt)}", flight=f.id)
        else:
            self._cancel(f, "crew out of duty hours, no reserve crew")

    def _return_to_stand(self, f):
        self.movers.pop(f.id, None)
        sid = self.pick_stand(f)
        if sid:
            self.stand_occ[sid] = f.id
            self.parked[f.id] = sid
            f.stand = sid
        f.phase = "not_ready"
        f.sector = None

    def _cancel(self, f, why):
        if f.phase in ("cancelled",) or f.outcome:
            return
        f.phase = "cancelled"
        f.outcome = "cancelled"
        f.cancel_reason = why
        f.data["out_t"] = self.t
        f.sector = None
        self.movers.pop(f.id, None)
        if f.id in self.parked:
            sid = self.parked.pop(f.id)
            if sid and self.stand_occ.get(sid) == f.id:
                self.stand_occ.pop(sid, None)
        self.emit("cancelled", flight=f.id, text=f"{f.id} cancelled: {why}")

    # -- radio --------------------------------------------------------------------------------------------------
    def freq_for_flight(self, f):
        return self.sectors.freq_of(f.sector) if f.sector else None

    def pilot_call(self, f, kind, sector, text, front=False):
        """A pilot's own transmission on the sector's frequency."""
        old = f.sector
        if self.nordo(f) and kind not in ("tcas_ra", "clear_of_conflict"):
            if f.sector != sector:
                f.sector = sector
            return
        if f.sector != sector:
            f.sector = sector
        if old is not None and old != sector and old in SECTORS and sector in SECTORS:
            self._handover(f, old, sector)
        fr = self.sectors.freq_of(sector)
        if fr is None:
            return
        if kind in ("ready_pushback", "ready_taxi", "holding_short"):
            f.data["waiting_call"] = [kind, sector, text, f.phase]   # repeated when a seat opens the sector
        tx = Transmission("pilot", f.id, kind, text, PILOT_CALL.get(kind, 4.0))
        fr.push(tx, front=front)

    def handoff_unmet(self, f, old, new):
        """The standard handoff conditions this handoff does not meet (MANUAL, "Handoffs"). Dubai publishes the
        speeds; where it does not publish the point, ICAO Doc 4444 or common practice is used."""
        ko, kn = SECTORS[old][0], SECTORS[new][0]
        st = self.air.state.get(f.id)
        s = self.air.snapshot(f.id) if st is not None else None
        out = []
        if old == "ARR" and new == "DIR":
            if s and s["cas_kt"] > 230.5:
                out.append("at 230 kt or less")
            if s and s["alt_ft"] > 10050:
                out.append("below 10,000 ft")
        elif ko == "ARR" and kn == "TWR":
            if st is None or st.mode != "loc":
                out.append("established on the localizer")
        elif ko == "TWR" and kn == "GND" and f.kind == "ARR":
            if f.phase not in ("taxi_in", "on_stand"):
                out.append("clear of the runway")
        elif ko == "GND" and kn == "TWR" and f.kind == "DEP":
            m = self.movers.get(f.id)
            near = m is not None and m.stop_at is not None and m.stop_at - m.i <= 2
            if f.phase != "holding" and not near:
                out.append("at the runway holding point")
        elif ko == "TWR" and kn == "DEP":
            if f.phase != "airborne":
                out.append("airborne")
        return out

    def _handover(self, f, old, new):
        """A flight moved to another sector's frequency. The receiving controller gets the handover: whether it met
        the standard conditions, and what the crew reported on earlier frequencies (ICAO Doc 4444 10.1.3)."""
        unmet = self.handoff_unmet(f, old, new)
        said = [r[2] for r in (f.data.get("reports") or []) if r[1] != new][-4:]
        f.data["handover"] = {"t": round(self.t), "from": old, "to": new, "standard": not unmet, "reported": said}
        fr = self.sectors.freq_of(new)
        self.emit("handoff", scope=self.sectors.scope_of(fr) if fr else None, flight=f.id, to=new, **{"from": old},
                  standard=not unmet, not_met=unmet, reported=said)

    def _radio_step(self, dt):
        for fr in list(self.sectors.freqs.values()):
            fr.step(self.t, dt, self._tx_done)

    # -- the long tail: what real crews bring that no list covers ------------------------------------------------
    def nordo(self, f):
        """A temporary radio failure (squawk 7600): the crew hears nothing and says nothing."""
        n = f.data.get("nordo")
        return bool(n) and n[0] <= self.t < n[1]

    def _long_tail_start(self, f):
        """Draw this flight's rare events once, when it comes onto Dubai's frequencies. Rates are estimates from
        published occurrence data (smoke or fumes about 1 in 2,500 flights; a radio problem lasting minutes, a
        few per thousand flights)."""
        if f.data.get("lt_drawn"):
            return
        f.data["lt_drawn"] = True
        r = self.day_rng("longtail", f.id)
        if r.random() < 1 / 2500.0 and f.kind == "ARR":
            f.data["lt_smoke_t"] = self.t + r.uniform(60.0, 900.0)
        if r.random() < 1 / 300.0:
            t0 = self.t + r.uniform(60.0, 1200.0)
            f.data["nordo"] = (t0, t0 + r.uniform(180.0, 600.0))
        f.data["lt_req"] = r.random() < 0.25          # this crew will ask for something (a shortcut, a level)

    def _confused_with(self, f, name):
        """Callsign confusion: with a similar callsign on the same frequency, now and then the wrong crew answers."""
        if name not in ("heading", "altitude", "speed") or not f.sector:
            return None
        pre, num = PH.split_id(f.id)
        if pre is None:
            return None
        sim = []
        for g in self.flights.values():
            if g.id == f.id or g.sector != f.sector or g.id not in self.air.state or self.nordo(g):
                continue
            gp, gn = PH.split_id(g.id)
            if gp != pre or not gn:
                continue
            if sorted(gn) == sorted(num) or gn[-2:] == num[-2:] or gn.startswith(num) or num.startswith(gn):
                sim.append(g)
        if not sim or self.rng("confuse", f.id, self.seq, name).random() >= 0.04:
            return None
        return sim[0]

    def _long_tail(self):
        now = self.t
        for fid in list(self.air.state.keys()):
            f = self.flights[fid]
            if not f.sector or f.phase in ("diverting", "gone"):
                continue
            st = self.air.state[fid]
            s = self.air.snapshot(fid)
            if s is None:
                continue
            r = self.day_rng("lt", fid, int(now))
            n = f.data.get("nordo")
            if n and now >= n[1] and not f.data.get("nordo_back"):
                f.data["nordo_back"] = True
                self.pilot_call(f, "request", f.sector, f"{fid}, radio check, we had a radio problem, request your last instruction", front=True)
            if f.data.get("lt_smoke_t") and now >= f.data["lt_smoke_t"] and "smoke" not in self.pilot_flags[fid]:
                self.pilot_flags[fid].update({"smoke", "priority"})
                f.data["smoke_t"] = now
                self.pilot_call(f, "request", f.sector, f"PAN PAN, PAN PAN, PAN PAN, {fid}, smoke in the cabin, "
                                f"request priority approach runway {self.arr_rwy}", front=True)
            if "smoke" in self.pilot_flags[fid] and f.kind == "ARR" and not st.app_rwy and now - f.data.get("smoke_t", now) > 20 * 60 \
                    and "divert" not in self.pilot_flags[fid]:
                self._start_divert(f, "smoke in the cabin, diverting")
                continue
            # Turbulence near convection: the crew will not descend into it for a while.
            if f.kind == "ARR" and f.phase == "arrival" and s["alt_ft"] > 4000 and not f.data.get("turb_done"):
                near = any(dist_nm((s["lat"], s["lon"]), (c["lat"], c["lon"])) < c["radius_nm"] + 15
                           for c in self.weather.active_cells(now))
                if near and r.random() < 0.0015:
                    f.data["turb_done"] = True
                    f.data["turb_until"] = now + r.triangular(180.0, 720.0, 360.0)
                    alt = int(round(s["alt_ft"] / 100.0)) * 100
                    self.pilot_call(f, "request", f.sector, f"{fid}, moderate turbulence, request to maintain {alt} ft for now", front=True)
            # Ordinary requests: a shortcut, or a different level.
            if f.data.get("lt_req") and not f.data.get("lt_req_done") and r.random() < 0.01:
                if f.kind == "ARR" and f.phase == "arrival" and st.mode == "route" and st.route:
                    side = self.side_of(f)
                    feeder = AP.proc_for(self.arr_rwy)["sides"][side]["feeder"]
                    f.data["lt_req_done"] = True
                    self.pilot_call(f, "request", f.sector, r.choice([f"{fid}, request direct {feeder}", f"{fid}, any chance of direct {feeder}?",
                                                                      f"{fid} request shortcut to {feeder} if possible"]))
                elif f.kind == "DEP" and f.phase == "airborne" and s["alt_ft"] > 3500 and st.gate:
                    f.data["lt_req_done"] = True
                    g = st.gate if isinstance(st.gate, str) else "the exit"
                    self.pilot_call(f, "request", f.sector, r.choice([f"{fid}, request higher", f"{fid}, request direct {g}",
                                                                      f"{fid}, request further climb when able"]))

    def _wind_report(self, f, sector, p):
        """Crews often pass on the wind they meet aloft, which can differ a lot from the surface wind."""
        r = self.rng("windrep", f.id, self.seq)
        if r.random() >= p:
            return
        w = self.air.wind_here(f.id)
        s = self.air.snapshot(f.id)
        if w is None or s is None or s["alt_ft"] < 1500:
            return
        frm, kt = w
        alt = int(round(s["alt_ft"] / 500.0)) * 500
        self.pilot_call(f, "report", sector, f"{f.id}, wind at {alt} ft {int(round(frm / 10.0)) * 10 % 360:03d} at {int(round(kt))} knots")

    def wake_word(self, f):
        """ICAO Doc 4444: an A380 adds SUPER and other heavy aircraft HEAVY to the callsign."""
        if f is None:
            return None
        return "super" if f.group == "A" else ("heavy" if f.group in ("B", "C") else None)

    def _tx_done(self, tx, fr):
        f = self.flights.get(tx.flight)
        scope = self.sectors.scope_of(fr)
        hear = self.rng("hear", tx.flight, self.seq)
        if tx.who == "pilot":
            if f is not None and tx.kind in REPORT_CALLS and (tx.kind != "checkin" or "fuel" in tx.text):
                f.data.setdefault("reports", []).append((round(self.t), f.sector, tx.text))
            if f is not None:
                f.data["last_call"] = (tx.kind, tx.text)
            self.emit("radio", scope=scope, flight=tx.flight, call=tx.kind, text=tx.text, sector=f.sector if f else None,
                      freq=fr.name, heard=PH.spoken(tx.text, tx.flight, hear, heavy=self.wake_word(f)))
            return
        if tx.on_end is not None:
            try:
                rb = tx.on_end()
            except Refusal as e:
                rb = f"{tx.flight}, unable, {e}"
                self.emit("radio", scope=scope, flight=tx.flight, call="unable", text=rb, op=tx.op, freq=fr.name,
                          heard=PH.spoken(rb, tx.flight, hear, heavy=self.wake_word(f)))
                return
            except (TypeError, ValueError, KeyError, AttributeError, OverflowError, IndexError):
                # A malformed field the checks let through: the crew cannot comply, and the frequency moves on.
                rb = f"{tx.flight}, unable, say again: the instruction was not understood"
                self.emit("radio", scope=scope, flight=tx.flight, call="unable", text=rb, op=tx.op, freq=fr.name,
                          heard=PH.spoken(rb, tx.flight, hear, heavy=self.wake_word(f)))
                return
            if rb is None:          # the crew did not hear it: no readback, nothing done
                return
            who = tx.flight
            if isinstance(rb, tuple):   # another crew took it (callsign confusion)
                who, rb = rb
            self.emit("radio", scope=scope, flight=who, call="readback", text=rb, op=tx.op, freq=fr.name,
                      heard=PH.readback(rb, who, hear))

    # -- the instruction interface ------------------------------------------------------------------------------
    def instruct(self, seat, op):
        """Check an op from a seat (or the built-in controller of a reference run when seat is None) and queue it. Returns a dict.
        Raises Refusal with a reason the controller can act on."""
        name = op.get("op")
        if not _finite(op):
            raise Refusal("every number in an op must be finite")
        if any(not isinstance(k, str) or k.startswith("_") for k in op):
            raise Refusal("op field names are strings that do not start with an underscore")
        _check_shape(op, name)
        if name in SYSTEM_OPS:
            return SYSTEM_OPS[name](self, seat, op)
        if name not in RADIO_OPS:
            raise Refusal(f"unknown op {name!r}")
        fid = op.get("flight")
        f = self.flights.get(fid)
        if f is None:
            raise Refusal(f"no flight {fid!r}")
        if f.sector is None:
            raise Refusal(f"{fid} is not on any of your frequencies", phase=f.phase)
        owner = self.sectors.owner.get(f.sector)
        if seat is not None and owner != seat:
            raise Refusal(f"{fid} is on {f.sector}, which you do not work", sector=f.sector)
        if seat is None and owner is not None:
            raise Refusal("sector is staffed")
        kinds = RADIO_OPS[name][0]
        st_ = self.air.state.get(f.id)
        if name in ("altitude", "heading", "hold") and SECTORS[f.sector][0] == "TWR" and st_ is not None and st_.mode in ("missed", "route", "hold", "heading") and f.data.get("go_arounds"):
            kinds = kinds | {"TWR"}     # the tower gives a go-around its missed approach instructions
        if SECTORS[f.sector][0] not in kinds:
            raise Refusal(f"{name} is not a {SECTORS[f.sector][0]} instruction (needs {'/'.join(sorted(kinds))})")
        fr = self.sectors.freq_of(f.sector)
        if fr.backlog(self.t) > MAX_BACKLOG_S:
            raise Refusal("frequency congested", retry_after_sec=round(fr.backlog(self.t) - MAX_BACKLOG_S + 1, 1))
        check, apply = RADIO_OPS[name][1], RADIO_OPS[name][2]
        if (f.id, name) in self.pending:
            raise Refusal(f"a {name} instruction for {f.id} is already waiting to be transmitted")
        check(self, f, op)
        c, r = AIRTIME.get(name, (4.0, 3.5))
        if self.rng("rb", f.id, self.seq, name).random() < READBACK_ERROR_P and name in ("altitude", "heading", "speed"):
            op = dict(op, _misheard=True)
        key = (f.id, name)
        self.pending.add(key)
        if name == "altitude":
            self.pending_alt[f.id] = float(op.get("ft"))
        missed = self.rng("missed", f.id, self.seq, name).random() < PH.P_MISSED

        def on_end():
            self.pending.discard(key)
            if name == "altitude":
                self.pending_alt.pop(f.id, None)
            if missed or self.nordo(f):
                return None
            other = self._confused_with(f, name)
            if other is not None:
                # Callsign confusion: a crew with a similar callsign on this frequency takes the instruction as its
                # own and reads it back; the crew it was meant for hears nothing.
                try:
                    check(self, other, dict(op, _rb=True))
                    return other.id, apply(self, other, dict(op, flight=other.id))
                except Refusal:
                    return None
            # The crew checks it again: the situation may have changed while it waited for the frequency, and only the
            # crew knows its own limits (minimum speed, turbulence), which it says on the radio as "unable".
            check(self, f, dict(op, _rb=True))
            return apply(self, f, op)

        self._strip(f, op)
        tx = Transmission("atc", f.id, name, self.phrase(f, op), c + r, on_end=on_end, op=_public_op(op), seat=seat)
        fr.push(tx)
        self.stats["tx:" + (seat or "standin")] += 1
        return {"queued": True, "flight": f.id, "op": name, "starts_in_s": round(fr.backlog(self.t) - (c + r), 1),
                "airtime_s": c + r}

    def strip_of(self, f):
        """The strip as the API shows it. Before any instruction: a departure is on its SID to its exit gate, and an
        arrival on the route Center cleared it to its entry fix."""
        sp = f.data.get("strip") or {}
        if f.kind == "DEP":
            base = {"mode": "sid", "route": [f.gate] if f.gate else []}
        else:
            base = {"mode": "route", "route": [f.entry] if f.entry else []}
        out = {"mode": None, "alt_ft": None, "speed_kt": None, "heading": None, "route": [], "hold": None,
               "approach": None, "landing": False}
        out.update(base)
        out.update(sp)
        out["handover"] = f.data.get("handover")
        return out

    def _strip(self, f, op):
        """The flight strip: what the controllers have cleared the flight to, written when the instruction is given.
        It is not what the crew is flying: a misheard or missed instruction, a weather deviation or a TCAS manoeuvre
        shows only on the radar and on the radio."""
        n, sp = op["op"], f.data.setdefault("strip", {})
        if n == "heading":
            sp.update(mode="heading", heading=op.get("hdg"))
        elif n == "altitude":
            sp["alt_ft"] = op.get("ft")
        elif n == "speed":
            sp["speed_kt"] = op.get("kt")
        elif n == "direct":
            sp.update(mode="route", route=[op.get("fix")] + list(op.get("then") or []), heading=None)
        elif n == "route":
            sp.update(mode="route", route=list(op.get("fixes") or []), heading=None)
        elif n == "hold":
            sp.update(mode="hold", hold=op.get("fix"), heading=None)
        elif n == "approach":
            sp.update(mode="approach", approach=op.get("runway"), heading=None)
        elif n == "land":
            sp["landing"] = True
        elif n == "go_around":
            sp.update(mode="missed", landing=False)
        elif n == "divert":
            sp.update(mode="divert", heading=None)
        elif n == "deviation" and op.get("approve"):
            sp.update(mode="deviating", heading=None)

    def phrase(self, f, op):
        n = op["op"]
        bits = [f.id, n.replace("_", " ")]
        for k in ("hdg", "ft", "kt", "fix", "runway", "to", "stand", "entry"):
            if op.get(k) is not None:
                bits.append(str(op[k]))
        return " ".join(bits)

    # -- airborne steering --------------------------------------------------------------------------------------
    def _steer_air(self, dt):
        now = self.t
        for fid in list(self.air.state.keys()):
            f = self.flights[fid]
            st = self.air.state[fid]
            snap = None
            default = self._default_speed(f, st)
            ev = self.air.steer(fid, f, now, default)
            if ev == "route_end":
                if st.mode == "center":
                    self._entry_handoff(f)
                elif st.mode == "divert":
                    self._divert_done(f)
                elif st.mode == "sid":
                    self._departure_exit(f)
                elif st.mode == "route" and f.kind == "ARR" and not st.app_rwy:
                    # Clearance limit reached: hold as published; elsewhere keep the heading and ask.
                    h = AP.HOLDS.get(st.route[0]) if st.route and isinstance(st.route[0], str) else None
                    if h:
                        st.mode, st.hold, st.hold_phase = "hold", h, "to_fix"
                        f.data.setdefault("holding_since", now)
                    else:
                        snap = self.air.snapshot(fid)
                        st.mode, st.hdg = "heading", snap["hdg"] if snap else 0
                        if f.sector:
                            self.pilot_call(f, "request", f.sector, f"{fid} at {st.route[0]}, maintaining heading {int(st.hdg):03d}, request further clearance", front=True)
            elif ev == "missed_turn":
                pass
            elif ev == "threshold":
                self._touchdown(f)
            if st.mode == "loc" and f.kind == "ARR":
                s_ = self.air.snapshot(fid)
                along = self.air.loc_coords((s_["lat"], s_["lon"]), AP.RUNWAYS[st.app_rwy])[0] if s_ is not None else None
                cw = self.crew(f)
                if along is not None and along < cw["gate_nm"] and s_["alt_ft"] > along * GP_FT_PER_NM + cw["high_ft"]:
                    # Stabilised approach criteria: far above the glide path this close in, every crew goes around.
                    self.go_around(f, "unstable approach, too high")
                    continue
                if along is not None and f.id not in self.cleared_land and along < cw["no_clr_nm"]:
                    self.go_around(f, "no landing clearance")
                    continue
            if f.kind == "ARR" and f.phase == "arrival" and st.mode == "loc" and st.established and f.phase != "final":
                f.phase = "final"
                self.leave_finals(f.id)
                self.final_order[st.app_rwy].append(f.id)
            if f.kind == "DEP" and st.mode == "sid":
                s = self.air.snapshot(fid)
                if s and dist_nm((s["lat"], s["lon"]), AP.ARP) > 40:
                    self._departure_exit(f)

    def _default_speed(self, f, st):
        """The speed a crew flies when ATC has not assigned one."""
        snap = self.air.snapshot(f.id)
        alt = snap["alt_ft"] if snap else 10000
        if f.kind == "DEP":
            return 250 if alt < 10000 else 290
        if st.mode == "loc":
            rw = AP.RUNWAYS[st.app_rwy]
            along, _ = self.air.loc_coords((snap["lat"], snap["lon"]), rw) if snap else (10, 0)
            if along < 4.0:
                return f.ti["vapp_kt"]
            return max(f.ti["vapp_kt"], 160)
        if st.mode == "hold":
            return 220
        if st.mode == "center":
            # Center adjusts the speed so the aircraft reaches the entry fix at its slot (from 60 kt slower to 20 kt
            # faster).
            base = 250 if alt < 10000 else 280
            slot = f.data.get("entry_slot")
            if snap and slot is not None and st.route:
                d = dist_nm((snap["lat"], snap["lon"]), AP.ENTRY[f.entry]["pos"])
                eta = self.t + d / max(100.0, snap["gs_kt"]) * 3600.0
                return base + max(-60.0, min(20.0, (eta - slot) * 0.5))
            return base
        return 250

    def _entry_handoff(self, f):
        st = self.air.state[f.id]
        st.mode = "route"
        rw = self.arr_rwy
        side = self.side_of(f)
        proc = AP.proc_for(rw)
        st.route = proc["sides"][side].get("via", {}).get(f.entry, []) + [proc["sides"][side]["feeder"]]
        st.spd = None
        f.phase = "arrival"
        f.data["entry_t"] = self.t
        sec = self.sector_of_side(side)
        s = self.air.snapshot(f.id)
        self.pilot_call(f, "checkin", sec, f"{sec} good evening, {f.id} {f.type}, passing {int(s['alt_ft']) // 100 * 100} ft for {int(st.alt or ENTRY_ALT_FT)}, "
                                           f"inbound {f.entry}, fuel endurance {int(self.fuel.get(f.id, 0))} minutes above diversion fuel")
        self._wind_report(f, sec, 0.2)
        self._long_tail_start(f)

    def leave_finals(self, fid):
        """Take a flight out of every runway's final sequence."""
        for order in self.final_order.values():
            if fid in order:
                order.remove(fid)

    # -- landing ------------------------------------------------------------------------------------------------
    def _touchdown(self, f):
        st = self.air.state[f.id]
        rwy = st.app_rwy
        snap = self.air.snapshot(f.id)
        o = self.weather.current(self.t)
        # Runway occupied?
        occ = self.runway_occupants(rwy, exclude=f.id)
        rrsm_ok = self._rrsm_ok(rwy)
        blocking = [x for x in occ if not (rrsm_ok and x[1] == "landing" and x[2] >= 2500
                                           and not self.landing.get(x[0], {}).get("stopped"))]
        cleared = f.id in self.cleared_land
        see = o.vis_m >= 1500 and not self.lvp_on()
        if not cleared:
            return self.go_around(f, "no landing clearance")
        if self.runway_closed(rwy):
            return self.go_around(f, "runway closed")
        if blocking:
            if see:
                self._safety("runway_incursion", "B", flights=[f.id] + [b[0] for b in blocking],
                             text=f"{f.id} cleared to land with runway {rwy} occupied by {', '.join(b[0] for b in blocking)}; crew saw it and went around")
                return self.go_around(f, "runway occupied")
            near = [b for b in blocking if b[2] < 2200]
            if near:
                self._safety("collision", "A", flights=[f.id] + [b[0] for b in near],
                             text=f"{f.id} landed in low visibility on runway {rwy} and collided with {near[0][0]}")
                self._crash(f, runway=rwy)
                for b in near:
                    self._crash(self.flights[b[0]], runway=rwy)
                return
            self._safety("runway_incursion", "B", flights=[f.id] + [b[0] for b in blocking],
                         text=f"{f.id} landed in low visibility with {blocking[0][0]} on runway {rwy}")
        # Windshear / gust / unstable approach go-arounds.
        p = GO_AROUND_BASE_P
        if o.windshear:
            p += 0.03
        if o.gust_kt and o.gust_kt > 15:
            p += 0.002 * (o.gust_kt - 15)
        if o.ts:
            p += 0.04
        if snap and snap["cas_kt"] > f.ti["vapp_kt"] + 25:
            p += 0.5
        head, cross, ghead, gcross = self.weather.runway_wind(self.t, AP.RUNWAYS[rwy].heading)
        state = self.weather.runway_state(self.t)
        xlim = {"DRY": 35, "WET": 30, "CONTAMINATED": 20}[state]
        if gcross > xlim or ghead < -10:
            p = max(p, 0.85)
        if self.rng("ga", f.id, int(self.t)).random() < p:
            return self.go_around(f, "windshear" if o.windshear else ("unstable approach" if p > 0.3 else "gusts"))
        # Minima.
        need = MINIMA_RVR[f.cat]
        if self.ils_out.get(rwy, 0) > self.t:
            need = max(need, 1500)
            if o.ceiling_ft is not None and o.ceiling_ft < 500:
                return self.go_around(f, "below localizer-only minima")
        if o.rvr_or_vis < need or (o.rvr_or_vis < 550 and not self.lvp_on()):
            return self.go_around(f, "below minima")
        # Land.
        self.air.delete(f.id)
        self.cleared_land.discard(f.id)
        self.leave_finals(f.id)
        f.phase = "landing"
        f.t_done = self.t
        f.outcome = "landed"
        prev = self.last_landing.get(rwy)
        self.last_landing[rwy] = (self.t, f.group, f.id)
        vland = f.ti["vland_kt"] * KT_MS
        ex = self._choose_exit(f, rwy, op_exit=f.data.get("exit"))
        self.landing[f.id] = {"rwy": rwy, "t0": self.t, "v0": vland, "exit": ex, "d": 0.0}
        self.emit("landed", flight=f.id, text=f"{f.id} landed {rwy} at {self.clock()}", delay_min=round((self.t - f.sched) / 60, 1))
        # Standing water: the first crew to land after heavy rain reports poor braking on the tower frequency.
        b = self.body(rwy)
        if (self.weather.runway_state(self.t) == "CONTAMINATED" and b not in self.braking_poor
                and self.t - self.inspected.get(b, -1e9) > BRAKING_REPORT_GAP_S and f.sector):
            self.braking_poor[b] = self.t
            self.pilot_call(f, "request", f.sector, f"{f.id} braking action poor, standing water on runway {rwy}", front=True)
        if "inspect_after" in self.pilot_flags[f.id]:
            self.closures.append((self.body(rwy), self.t + 60, self.t + 60 + 20 * 60, f"inspection after {f.id} emergency landing"))
            self.emit("notam", text=f"Runway {rwy} closed for inspection after {f.id} emergency landing, about 20 minutes")

    def _reduced_final_ok(self):
        """The 2.5 NM in-trail minimum inside 10 NM: dry runway, visibility 5 km or more, no wind shear."""
        o = self.weather.current(self.t)
        return o.vis_m >= 5000 and not o.windshear and self.weather.runway_state(self.t) == "DRY"

    def _rrsm_ok(self, rwy):
        o = self.weather.current(self.t)
        head, cross, gh, gc = self.weather.runway_wind(self.t, AP.RUNWAYS[rwy].heading)
        return (gh >= -5 and o.vis_m >= 5000 and (o.ceiling_ft is None or o.ceiling_ft >= 1000)
                and not o.windshear and self.weather.runway_state(self.t) == "DRY")

    def _choose_exit(self, f, rwy, op_exit=None):
        exits = self.ground.exits.get(rwy, [])
        state = self.weather.runway_state(self.t)
        k = {"DRY": 1.0, "WET": 1.15, "CONTAMINATED": 1.35}[state]
        need = {"A": 2700, "B": 2300, "C": 2100, "D": 1750, "E": 1650, "F": 1500, "G": 1300}[f.group] * k
        cand = [e for e in exits if e[1] >= need]
        if op_exit is not None:
            pick = [e for e in exits if str(e[0]) == str(op_exit) or e[1] == op_exit]
            if pick and pick[0][1] >= need * 0.95:
                return pick[0]
        rapid = [e for e in cand if e[2]]
        if rapid:
            return rapid[0]
        return cand[0] if cand else exits[-1]

    def go_around(self, f, why):
        st = self.air.state.get(f.id)
        if st is None:
            return
        rwy = st.app_rwy or self.arr_rwy
        self.cleared_land.discard(f.id)
        st.mode = "missed"
        st.missed_rwy = rwy
        st.fix = AP.proc_for(rwy)["missed_hold"]
        st.alt = 3000
        st.spd = None
        st.app_rwy = None
        st.established = False
        st.on_gs = False
        self.leave_finals(f.id)
        f.phase = "arrival"
        f.data["go_arounds"] = f.data.get("go_arounds", 0) + 1
        self.fuel[f.id] = self.fuel.get(f.id, 30) - 4.0
        self.stats["go_arounds"] += 1
        self.emit("go_around", flight=f.id, text=f"{f.id} going around runway {rwy}: {why}", why=why)
        if f.sector:
            self.pilot_call(f, "go_around", f.sector, f"{f.id} going around, {why}", front=True)

    # -- the runways ----------------------------------------------------------------------------------------------
    def runway_occupants(self, rwy, exclude=None):
        """[(flight, role, metres from the rwy threshold)] for aircraft on the runway body of rwy."""
        b = self.body(rwy)
        out = []
        for fid, L in self.landing.items():
            if fid != exclude and self.body(L["rwy"]) == b:
                out.append((fid, "landing", L["d"] + self._thr_offset(L["rwy"], rwy)))
        for fid, T in self.takeoff.items():
            if fid != exclude and self.body(T["rwy"]) == b:
                out.append((fid, "takeoff", self._to_along(T, rwy)))
        for fid, (r, j) in self.lined.items():
            if fid != exclude and self.body(r) == b:
                out.append((fid, "lined_up", self.ground.junction[j]["d"].get(rwy, 0)))
        for fid, m in self.movers.items():
            if fid == exclude:
                continue
            if self.flights[fid].phase == "taxi_in" and m.i == 0:
                continue      # turned off onto its exit: the crew has reported the runway vacated
            # On the runway: at a junction node, or moving onto one it is cleared to enter.
            cur = m.path[min(m.i, len(m.path) - 1)]
            nxt = m.path[m.i + 1] if m.i + 1 < len(m.path) else None
            node = None
            if cur in self.ground.junction:
                node = cur
            elif nxt in self.ground.junction and m.s > 0.5 and m.cleared_to is not None and m.i + 1 <= m.cleared_to:
                node = nxt
            if node is not None and self.ground.junction[node]["body"] == b:
                out.append((fid, "crossing", self.ground.junction[node]["d"].get(rwy, 0)))
        return out

    def _thr_offset(self, r_from, r_to):
        if r_from == r_to:
            return 0.0
        return 0.0 if AP.RUNWAYS[r_from].heading == AP.RUNWAYS[r_to].heading else -1e5

    def _to_along(self, T, rwy):
        return T["start"] + T["d"] - AP.RUNWAYS[T["rwy"]].displaced_m

    def _runway_step(self, dt):
        # Landing rolls.
        for fid in list(self.landing.keys()):
            L = self.landing[fid]
            f = self.flights[fid]
            ex_node, ex_d, rapid = L["exit"]
            v_exit = (25.0 if rapid else 9.0)
            rem = ex_d - L["d"]
            if rem > 0:
                # Constant deceleration from v0 to v_exit over the distance to the exit.
                total = max(1.0, ex_d)
                a = (L["v0"] ** 2 - v_exit ** 2) / (2 * total)
                v = math.sqrt(max(v_exit ** 2, L["v0"] ** 2 - 2 * a * L["d"]))
                L["d"] += v * dt
                continue
            # At the exit: turn off if the way is clear, else roll on to the next exit.
            path = self._vacate_path(f, L["rwy"], ex_node)
            if path is None or self._blocked_ahead(fid, path, 0, 0.0, 120.0):
                nxt = [e for e in self.ground.exits.get(L["rwy"], []) if e[1] > ex_d + 50]
                if nxt:
                    L["exit"] = nxt[0]
                    L["v0"] = v_exit + 5
                    L["d"] = ex_d
                    continue
                L["stuck"] = L.get("stuck", 0) + dt
                L["stopped"] = True      # stopped at the exit
                blk = self._blocked_ahead(fid, path, 0, 0.0, 120.0) if path is not None else None
                if L["stuck"] > 60 and blk in self.movers and blk in self.flights:
                    # A landed aircraft cannot leave the runway: ground control gets the aircraft blocking the exit
                    # towed clear, which takes 4 minutes for it.
                    b = self.flights[blk]
                    b.data["aside_until"] = self.t + 240
                    for x in ("blocked_since", "blocked_by", "row_over"):
                        b.data.pop(x, None)
                    self.stats["pulled_aside"] += 1
                    self.emit("ground", flight=blk, text=f"{blk} blocking runway exit for {fid}: towed clear, continuing in 4 minutes")
                    L["stuck"] = 0
                continue
            del self.landing[fid]
            f.phase = "taxi_in"
            f.data["vacated_t"] = self.t
            f.data["rot_s"] = self.t - L["t0"]
            m = Mover(path)
            m.stop_at = len(path) - 1
            self.movers[fid] = m
            then = f.data.pop("then_after_vacate", None)
            if then:
                self.pilot_call(f, "vacated", then, f"{f.id} runway vacated via {self.ground.edge_ref.get((path[0], path[1]), 'exit')}, request taxi")
            else:
                self.pilot_call(f, "vacated", f.sector, f"{f.id} runway vacated")
        # Take-off rolls.
        for fid in list(self.takeoff.keys()):
            T = self.takeoff[fid]
            f = self.flights[fid]
            if self.t < T["t0"] + TAKEOFF_REACT_S:
                continue
            T["v"] += T["a"] * dt
            T["d"] += T["v"] * dt
            if T["v"] >= T["vto"]:
                del self.takeoff[fid]
                self._liftoff(f, T)

    def _vacate_path(self, f, rwy, ex_node):
        """From the exit junction to the first node clear of the runway on the side toward the stand."""
        dest_node = None
        if f.stand and f.stand in self.ground.stands:
            dest_node = self.ground.stands[f.stand]["node"]
        best = None
        for v, L in self.ground.adj.get(ex_node, ()):
            if v in self.ground.junction:
                continue
            # walk off the runway two nodes
            p = [ex_node, v]
            for _ in range(3):
                nxts = [w for w, _ in self.ground.adj.get(p[-1], ()) if w not in p and w not in self.ground.junction]
                if not nxts:
                    break
                p.append(nxts[0])
            score = 0.0
            if dest_node is not None:
                rest = self.ground.path(p[-1], dest_node)
                score = self.ground.length(rest) if rest else 1e9
                if rest and any(n in self.ground.junction and self.ground.junction[n]["body"] == self.body(rwy) for n in rest):
                    score += 1e6     # never vacate to the side that needs this runway crossed again
            if best is None or score < best[0]:
                best = (score, p)
        return best[1] if best else None

    def _liftoff(self, f, T):
        rw = AP.RUNWAYS[T["rwy"]]
        start = rw.along(-rw.displaced_m + T["start"])
        pos = dest(start, rw.heading, T["d"] / 1852.0)
        st = self.air.create(f.id, f.type, pos, rw.heading, 100, f.ti["vto_kt"] + 10)
        self.air.set_traits(f.id, self.crew(f))
        st.mode = "sid"
        st.gate = f.gate or "WEST"
        st.route = [f"S{T['rwy'][:2]}"] + AP.SID_VIA.get((T["rwy"][:2], st.gate), []) + ["G" + st.gate[0]]
        st.alt = 4000
        st.missed_rwy = T["rwy"]
        f.phase = "airborne"
        f.t_done = self.t
        f.outcome = "departed"
        self.emit("departed", flight=f.id, text=f"{f.id} airborne {T['rwy']} at {self.clock()}", delay_min=round((self.t - f.sched) / 60, 1))
        then = f.data.pop("then_after_airborne", None)
        if then:
            self.pilot_call(f, "checkin", then, f"{then}, {f.id} passing 1500 ft climbing 4000, {st.gate} departure")
            self._long_tail_start(f)

    def _departure_exit(self, f):
        if f.phase == "gone":
            return
        self.air.delete(f.id)
        f.phase = "gone"
        f.sector = None

    # -- ground movement -----------------------------------------------------------------------------------------
    def _ground_step(self, dt):
        lvp = self.lvp_on() or self.weather.current(self.t).vis_m < 1000
        vmax = (TAXI_LVP_KT if lvp else TAXI_KT) * KT_MS
        follow = FOLLOW_LVP_M if lvp else FOLLOW_M
        positions = {fid: pos_xy(self.ground, m) for fid, m in self.movers.items()
                     if not (self.flights[fid].data.get("aside_until") or 0) > self.t}
        for fid, m in list(self.movers.items()):
            f = self.flights[fid]
            if f.phase == "pushed":
                continue
            if (f.data.get("aside_until") or 0) > self.t:
                continue
            if m.done():
                self._mover_arrived(f, m)
                continue
            # Runway junction ahead without clearance: stop at the holding point before it.
            limit = len(m.path) - 1 if m.stop_at is None else m.stop_at
            # An arrival whose stand is still occupied waits short of the apron (about 300 m before the stand).
            if f.kind == "ARR" and f.phase == "taxi_in":
                sid = f.data.get("stand")
                occ = self.stand_occ.get(sid) if sid else None
                if sid and occ not in (None, f.id) and (occ in self.parked or self.flights.get(occ) is not None and self.flights[occ].phase == "pushed") \
                        and self.ground.stands[sid]["node"] == m.path[-1]:
                    acc, k = 0.0, len(m.path) - 1
                    while k > m.i and acc < 300.0:
                        acc += self.ground._edge_len(m.path[k - 1], m.path[k])
                        k -= 1
                    limit = min(limit, max(m.i, k))
                    if m.i >= limit and m.s <= 0.01:
                        # Stopped short of the apron: apron control parks it on a remote holding spot, clear of the
                        # taxiway, and it carries on once the stand is free (checked every minute).
                        if not f.data.get("waiting_stand"):
                            f.data["waiting_stand"] = True
                            self.emit("ground", flight=fid, text=f"{fid} waiting on a remote holding spot for stand {sid} (occupied by {occ})")
                        f.data["aside_until"] = self.t + 60
                        continue
            # Junctions the aircraft is already on (a contiguous run from where it is) it may leave freely.
            k0 = m.i
            while k0 < len(m.path) and m.path[k0] in self.ground.junction:
                k0 += 1
            for k in range(max(m.i + 1, k0), len(m.path)):
                n = m.path[k]
                if n in self.ground.junction and (m.cleared_to is None or k > m.cleared_to):
                    limit = min(limit, self.ground.hold_before(m.path, k))
                    break
            if m.i >= limit and m.s <= 0.01 and "will_incur" in self.pilot_flags[fid] and m.waiting_for is not None \
                    and self.t - m.waiting_for > 20:
                # The crew misreads its clearance and crosses the stop bar. The ground radar alerts the tower.
                self.pilot_flags[fid].discard("will_incur")
                for k in range(m.i + 1, len(m.path)):
                    if m.path[k] in self.ground.junction:
                        while k + 1 < len(m.path) and m.path[k + 1] in self.ground.junction:
                            k += 1
                        m.cleared_to = k + 1
                        break
                m.waiting_for = None
                self.emit("alert", flight=fid, text=f"RUNWAY INCURSION ALERT: {fid} has crossed the stop bar without clearance and is entering the runway")
                continue
            if m.i >= limit and m.s <= 0.01:
                m.speed = 0.0
                if m.waiting_for is None:
                    m.waiting_for = self.t
                    self._mover_holding(f, m)
                continue
            blk = self._blocked_ahead(fid, m.path, m.i, m.s, follow, positions)
            if not blk:
                blk = self._wingtip(fid, m, positions)
            if not blk:
                blk = self._k_behind(fid, m, positions)
            if not blk and m.s < 1.0:
                blk = self._oncoming(fid, m)
            if not blk and m.s < 1.0 and m.i + 1 < len(m.path) and m.path[m.i + 1] in self.ground.junction \
                    and m.path[m.i] not in self.ground.junction:
                # About to enter a runway: crews do not enter unless they can clear it on the other side.
                k = m.i + 1
                while k + 1 < len(m.path) and m.path[k + 1] in self.ground.junction:
                    k += 1
                if k + 1 < len(m.path):
                    exit_xy = self.ground.xy[m.path[k + 1]]
                    for oid, p in positions.items():
                        if oid != fid and math.hypot(p[0] - exit_xy[0], p[1] - exit_xy[1]) < 80:
                            blk = oid
                            break
            if blk and f.data.get("row_over") == blk:
                blk = None       # it has the right of way over this aircraft at a crossing: keep going
            if blk:
                m.speed = 0.0
                bs = f.data.setdefault("blocked_since", self.t)
                f.data["blocked_by"] = blk
                # A queue behind an aircraft stopped at a holding point or a stand is normal and waits as long as it
                # must. A nose-to-nose block (each waiting for the other) for 90 s, or a longer circle of blocked
                # aircraft for 10 minutes, needs a tug.
                ob = self.flights.get(blk)
                mutual = ob is not None and ob.data.get("blocked_by") == fid and ob.data.get("blocked_since") is not None
                om = self.movers.get(blk)
                if mutual and om is not None and not self._head_on(m, om):
                    # Crossing paths, not nose to nose: one gives way. The arrival goes first, then the one that has
                    # waited longer; the other stays stopped until the first has passed.
                    mine_pri = (f.kind == "ARR", -bs, fid)
                    their_pri = (ob.kind == "ARR", -ob.data["blocked_since"], blk)
                    if mine_pri > their_pri and ob.data.get("row_over") != fid:
                        f.data["row_over"] = blk
                        blk = None
            if blk:
                # A closed loop of blocked aircraft (A waits for B, B for C, C for A) never clears by itself.
                loop, cur = False, blk
                for _ in range(50):
                    nxt = self.flights[cur].data.get("blocked_by") if cur in self.flights and self.flights[cur].data.get("blocked_since") is not None else None
                    if nxt is None:
                        break
                    if nxt == fid:
                        loop = True
                        break
                    cur = nxt
                chain = ob is not None and ob.data.get("blocked_since") is not None   # its blocker is itself blocked
                if self.movers.get(blk) is not None and ((mutual and self.t - bs > 90) or (loop and self.t - bs > 180)
                                                         or (chain and self.t - bs > 900)):
                    self._resolve_deadlock(f, m, blk)
                continue
            f.data.pop("blocked_since", None)
            f.data.pop("blocked_by", None)
            if f.data.get("row_over") and self._blocked_ahead(fid, m.path, m.i, m.s, follow, positions) != f.data["row_over"]:
                f.data.pop("row_over", None)     # past the crossing
            m.waiting_for = None
            m.speed = min(vmax, m.speed + 1.0 * dt)
            adv = m.speed * dt
            while adv > 0 and m.i < limit:
                L = max(0.1, self.ground._edge_len(m.path[m.i], m.path[m.i + 1]))
                if m.s + adv < L:
                    m.s += adv
                    adv = 0
                else:
                    adv -= (L - m.s)
                    m.i += 1
                    m.s = 0.0
            positions[fid] = pos_xy(self.ground, m)

    def opposing_edges(self, exclude=None, horizon=12):
        """Directed edges other taxiing aircraft will use soon (their next `horizon` edges)."""
        out = set()
        for oid, m in self.movers.items():
            if oid == exclude:
                continue
            for k in range(m.i, min(len(m.path) - 1, m.i + horizon)):
                out.add((m.path[k], m.path[k + 1]))
        return out

    def _ahead_edges(self, m, dist_m=800.0):
        out, acc = [], 0.0
        for k in range(m.i, len(m.path) - 1):
            e = (m.path[k], m.path[k + 1])
            out.append(e)
            acc += self.ground._edge_len(*e)
            if acc > dist_m:
                break
        return out

    def _oncoming(self, fid, m):
        """Give way at a taxiway entry: if an aircraft coming the other way is already inside the stretch I need
        (the next 800 m), wait for it. If neither has entered yet, the arrival goes first (it is leaving the runway
        area), then the one that has waited longer."""
        mine = self._ahead_edges(m)
        if not mine:
            return None
        rev_mine = {(b, a) for a, b in mine}
        f = self.flights[fid]
        my_cur = mine[0]
        for oid, o in self.movers.items():
            if oid == fid or o.done() or self.flights[oid].phase == "pushed" or (self.flights[oid].data.get("aside_until") or 0) > self.t:
                continue
            theirs = self._ahead_edges(o)
            if not theirs or not any(e in rev_mine for e in theirs):
                continue
            their_cur = theirs[0]
            they_inside = (their_cur[1], their_cur[0]) in set(mine[1:]) or (o.s > 1.0 and (their_cur[1], their_cur[0]) in set(mine))
            i_inside = (my_cur[1], my_cur[0]) in {(b, a) for a, b in theirs[1:]} and m.s > 1.0
            if they_inside and not i_inside:
                return oid
            if not they_inside and not i_inside:
                of = self.flights[oid]
                mine_pri = (f.kind == "ARR", -(f.data.get("blocked_since") or self.t), fid)
                their_pri = (of.kind == "ARR", -(of.data.get("blocked_since") or self.t), oid)
                if their_pri > mine_pri:
                    return oid
        return None

    def _head_on(self, m, o, dist_m=300.0):
        """The two are on the same stretch of taxiway in opposite directions (nose to nose)."""
        mine = self._ahead_edges(m, dist_m)
        theirs = {(b, a) for a, b in self._ahead_edges(o, dist_m)}
        return any(e in theirs for e in mine)

    def _blocked_ahead(self, fid, path, i, s, follow, positions=None):
        if positions is None:
            positions = {k: pos_xy(self.ground, m) for k, m in self.movers.items()
                         if not (self.flights[k].data.get("aside_until") or 0) > self.t}
        me = positions.get(fid)
        if me is None:
            me = self.ground.xy[path[i]]
        # look ahead along the path
        pts = [me]
        acc = 0.0
        k = i
        while k + 1 < len(path) and acc < follow + 40:
            a, b = self.ground.xy[path[k]], self.ground.xy[path[k + 1]]
            acc += math.hypot(b[0] - a[0], b[1] - a[1])
            pts.append(b)
            k += 1
        for other, p in positions.items():
            if other == fid:
                continue
            for q in pts[1:]:
                if math.hypot(p[0] - q[0], p[1] - q[1]) < 45.0 and math.hypot(p[0] - me[0], p[1] - me[1]) < follow + 45:
                    return other
        return None

    def _resolve_deadlock(self, f, m, other):
        """Two aircraft nose to nose: the departure (or, between two of a kind, the one that blocked later) is
        pulled aside onto an apron spot by a tug and waits there 4 minutes while the other passes, then carries on
        along its route. Ground controllers prevent this by not routing aircraft head-on."""
        of = self.flights[other]
        victim = f
        if f.kind == "ARR" and of.kind == "DEP":
            victim = of
        vm = self.movers[victim.id]
        vm.speed = 0.0
        victim.data["aside_until"] = self.t + 240
        for x in (victim, f, of):
            x.data.pop("blocked_since", None)
            x.data.pop("blocked_by", None)
        self.stats["pulled_aside"] += 1
        self.emit("ground", flight=victim.id, text=f"{victim.id} nose to nose with {other if victim is f else f.id}: pulled aside by a tug, continuing in 4 minutes")

    def _mover_holding(self, f, m):
        """The aircraft has stopped at a holding point, or at the end of its cleared route."""
        n = m.path[m.i]
        if f.kind == "DEP" and m.stop_at is not None and m.i >= m.stop_at:
            f.phase = "holding"
            then = f.data.pop("then_at_hold", None)
            j = f.data.get("entry_junction")
            if then:
                self.pilot_call(f, "holding_short", then, f"{then}, {f.id} holding short runway {f.data.get('dep_rwy')}, ready")
            else:
                self.pilot_call(f, "holding_short", f.sector, f"{f.id} holding short runway {f.data.get('dep_rwy')}")
        else:
            nxt = None
            for k in range(m.i + 1, len(m.path)):
                if m.path[k] in self.ground.junction:
                    nxt = m.path[k]
                    break
            if nxt is not None:
                body = self.ground.junction[nxt]["body"]
                f.data["crossing_junction"] = nxt
                self.pilot_call(f, "holding_short", f.sector, f"{f.id} holding short of runway {'12L/30R' if body == 'N' else '12R/30L'}, request crossing")

    def _edge_of(self, m):
        """The taxiway name of the edge a mover is on (or last moved along)."""
        if m.i + 1 < len(m.path):
            return self.ground.edge_ref.get((m.path[m.i], m.path[m.i + 1]))
        if m.i >= 1:
            return self.ground.edge_ref.get((m.path[m.i - 1], m.path[m.i]))
        return None

    def _wingtip(self, fid, m, positions):
        """UAE AIP OMDB AD 2.8. Code E and code F aircraft may not be on adjacent curves of taxilanes J (code F) and U
        (code E) at the same time. A crew about to break the rule waits: the one that came onto its taxilane later."""
        f = self.flights[fid]
        ref = self._edge_of(m)
        if ref in ("J", "U"):
            f.data.setdefault("lane_t", self.t)
        else:
            f.data.pop("lane_t", None)
        if f.code not in ("E", "F") or ref not in ("J", "U") or fid not in positions:
            return None
        me = positions[fid]
        for oid, om in self.movers.items():
            if oid == fid or oid not in positions:
                continue
            o = self.flights[oid]
            if o.code not in ("E", "F"):
                continue
            p = positions[oid]
            if math.hypot(p[0] - me[0], p[1] - me[1]) > WINGTIP_M:
                continue
            oref = self._edge_of(om)
            if ref in ("J", "U") and oref == ("U" if ref == "J" else "J") and "F" in (f.code, o.code):
                if o.data.get("lane_t", 1e18) < f.data["lane_t"]:
                    return oid
        return None

    def _k_links(self):
        """Taxiway K nodes where one of the links K1 to K5 joins it: node -> link name."""
        if getattr(self, "_k_link_at", None) is None:
            refs = {}
            for (a, b), r in self.ground.edge_ref.items():
                refs.setdefault(a, set()).add(r)
            self._k_link_at = {n: next(k for k in K_HOLDS if k in rs) for n, rs in refs.items()
                               if "K" in rs and any(k in rs for k in K_HOLDS)}
        return self._k_link_at

    def _k_behind(self, fid, m, positions):
        """UAE AIP OMDB AD 2.8. A code F aircraft on taxiway K may pass behind holding points K1 to K5 only when the
        aircraft waiting there is code D or smaller. Otherwise the crew stops short of that link and waits."""
        f = self.flights[fid]
        if f.code != "F" or self._edge_of(m) != "K" or fid not in positions or m.i + 1 >= len(m.path):
            return None
        nxt = m.path[m.i + 1]
        link = self._k_links().get(nxt)
        if link is None:
            return None
        if m.i + 2 < len(m.path) and self.ground.edge_ref.get((nxt, m.path[m.i + 2])) == link:
            return None     # turning into the link itself: it queues behind, it does not pass behind
        nx = self.ground.xy[nxt]
        me = positions[fid]
        if math.hypot(me[0] - nx[0], me[1] - nx[1]) > K_STOP_M:
            return None
        for oid, om in self.movers.items():
            if oid == fid or oid not in positions or self.flights[oid].code not in ("E", "F"):
                continue
            if om.speed > 0.1 or self._edge_of(om) != link:
                continue
            p = positions[oid]
            if math.hypot(p[0] - nx[0], p[1] - nx[1]) <= K_BEHIND_M:
                return oid
        return None

    def _mover_arrived(self, f, m):
        node = m.path[-1]
        if f.kind == "ARR":
            sid = f.data.get("stand")
            if sid and self.ground.stands.get(sid, {}).get("node") == node:
                if self.ramp_closed:
                    return  # no marshaller: wait on the lead-in line
                occ = self.stand_occ.get(sid)
                if occ is not None and occ != f.id and occ in self.parked:
                    return
                del self.movers[f.id]
                self.stand_occ[sid] = f.id
                f.phase = "on_stand"
                f.sector = None
                f.data["on_block_t"] = self.t
                nd = self.flights.get(f.next_dep)
                if nd is not None and nd.phase == "not_ready":
                    nd.stand = sid
                    self.parked[nd.id] = sid
                    self.stand_occ[sid] = nd.id
                return
        if f.kind == "DEP" and f.phase == "pushback":
            return

    def _transitions(self):
        for f in list(self.flights.values()):
            if f.phase == "diverting" and f.data.get("divert_leave_t") and self.t >= f.data["divert_leave_t"] and f.id in self.air.state:
                # Handed to the alternate's controllers: out of the Dubai terminal area.
                self._divert_done(f)
        """Frequency changes the crew makes by itself when told to ("contact tower at 8 miles", "when vacated
        contact ground"), and the end of a pushback."""
        now = self.t
        for f in list(self.flights.values()):
            if f.phase == "pushed" and f.data.get("push_done_t") is not None and now >= f.data["push_done_t"]:
                f.data["push_done_t"] = None
                then = f.data.pop("then_after_push", None) or f.sector
                self.pilot_call(f, "ready_taxi", then, f"{then}, {f.id} stand {f.stand}, ready to taxi")
            if f.kind == "ARR" and f.data.get("then_at_8nm") and f.id in self.air.state:
                st = self.air.state[f.id]
                if st.mode == "loc":
                    s = self.air.snapshot(f.id)
                    along = self.air.loc_coords((s["lat"], s["lon"]), AP.RUNWAYS[st.app_rwy])[0] if s is not None else None
                    if along is not None and along < 8.5:
                        to = f.data.pop("then_at_8nm")
                        self.pilot_call(f, "checkin", to, f"{to}, {f.id} established ILS {st.app_rwy}, {along:.0f} miles")
                        self._wind_report(f, to, 0.35)
            if f.data.get("then_after_cross") and f.id in self.movers:
                m = self.movers[f.id]
                if m.cleared_to is not None and m.i >= m.cleared_to:
                    to = f.data.pop("then_after_cross")
                    self.pilot_call(f, "checkin", to, f"{to}, {f.id} runway crossed")

    # -- pilots ---------------------------------------------------------------------------------------------------
    def _pilots(self, dt):
        now = self.t
        for fid in list(self.air.state.keys()):
            f = self.flights[fid]
            if f.kind != "ARR" or f.phase in ("diverting",):
                continue
            st = self.air.state[fid]
            # Fuel: the margin above diversion fuel is used up by time beyond the planned arrival (holding,
            # extra vectoring, go-arounds). The planned arrival itself is in the trip fuel.
            if f.phase in ("arrival", "final") and f.data.get("entry_t") is not None:
                planned = self.planned_arrival_min(f.entry)
                over = max(0.0, (now - f.data["entry_t"]) / 60.0 - planned)
                self.fuel[fid] = f.data.setdefault("fuel_entry", self.fuel.get(fid, 30.0)) - over - self.crew(f)["ga_fuel"] * f.data.get("go_arounds", 0)
            fuel = self.fuel.get(fid, 30.0)
            flags = self.pilot_flags[fid]
            cw = self.crew(f)
            if fuel < cw["minfuel"] and "minfuel" not in flags and f.sector:
                flags.add("minfuel")
                self.pilot_call(f, "minimum_fuel", f.sector, f"{f.id} MINIMUM FUEL, {int(fuel)} minutes to diversion fuel", front=True)
            # Expected approach time beyond the fuel: divert now.
            eat = f.data.get("eat")
            # The margin is counted to the planned alternate (about 25 NM away). When the nearer alternates are full or
            # closed, the crew needs more fuel to reach one that is open, so it leaves earlier.
            if int(now) % 30 == 0 or "alt_extra" not in f.data:
                alt = self.choose_alternate(f)
                f.data["alt_extra"] = None if alt is None else max(0.0, (AP.ALTERNATES[alt]["dist_nm"] - 25.0) / 4.0)
            extra = f.data["alt_extra"]
            must = (extra is not None and fuel <= extra + cw["divert_bias"]) or (eat is not None and (eat - now) / 60.0 > fuel + 5 and fuel < 15 + (extra or 0))
            if must and st.app_rwy and fuel > cw["continue_min"]:
                must = False     # cleared for the approach and in the sequence: the crew continues to land
            if must and st.mode not in ("loc",) and "divert" not in flags:
                self._start_divert(f, "fuel" if fuel <= (extra or 0) else "expected delay beyond fuel")
            if extra is None and fuel < 5 and "nowhere" not in flags and f.sector:
                flags.update({"nowhere", "priority", "emergency"})
                self.pilot_call(f, "mayday", f.sector, f"MAYDAY MAYDAY MAYDAY {fid}, no alternate available, {int(fuel)} minutes of fuel, request immediate approach", front=True)
            if fuel < cw["mayday"] and "mayday" not in flags:
                flags.add("mayday")
                if f.sector:
                    self.pilot_call(f, "mayday", f.sector, f"MAYDAY MAYDAY MAYDAY FUEL {fid}, landing below final reserve, request immediate approach", front=True)
                self._safety("fuel_emergency", "B", flights=[fid], text=f"{fid} MAYDAY FUEL: landing fuel below final reserve")
            if fuel < -30:
                self._safety("fuel_exhaustion", "A", flights=[fid], text=f"{fid} ran out of fuel")
                self._crash(f)
            # Thunderstorm cells: crews will not fly into one; they deviate and say so.
            if int(now) % 10 == 0:
                snap = self.air.snapshot(fid)
                if snap and f.phase == "enroute" and st.mode == "heading" and f.entry:
                    # Still with Center after a deviation: back direct to the entry fix once the way is clear (Center
                    # does this for its own traffic), or after 8 minutes in any case.
                    here = (snap["lat"], snap["lon"])
                    since = max(f.data.get("dev_t", {}).values() or [now])
                    if now - since > 60 and (self.path_clear_of_cells(here, AP.ENTRY[f.entry]["pos"]) or now - since > 480):
                        st.mode = "center"
                        st.route = [f.entry]
                self._weather_avoid(f, st, snap, now)

    def _departure_weather(self):
        """Departures still with Dubai avoid the cells like arrivals. Clear of the weather, a crew that turned asks for
        a direct route to its exit; with no answer for 4 minutes it resumes its own navigation to the exit."""
        now = self.t
        if int(now) % 10:
            return
        for fid in list(self.air.state.keys()):
            f = self.flights[fid]
            if f.kind != "DEP" or f.phase != "airborne":
                continue
            st = self.air.state[fid]
            snap = self.air.snapshot(fid)
            if not snap or st.mode == "missed":
                continue
            self._weather_avoid(f, st, snap, now)
            dd = f.data.get("dep_dev")
            gfix = next((x for x in reversed(st.route or []) if isinstance(x, str)), None)
            if not dd or st.mode != "heading" or gfix not in AP.FIXES:
                continue
            here = (snap["lat"], snap["lon"])
            if now - dd["t"] > 60 and self.path_clear_of_cells(here, AP.FIXES[gfix]):
                if not dd.get("called"):
                    dd["called"] = now
                    if f.sector:
                        self.pilot_call(f, "request", f.sector, f"{fid} clear of weather, request direct {gfix}", front=True)
                elif now - dd["called"] > self.crew(f)["resume_s"]:
                    st.mode, st.route = "sid", [gfix]
                    f.data.pop("dep_dev", None)
                    if f.sector:
                        self.pilot_call(f, "checkin", f.sector, f"{fid} resuming own navigation direct {gfix}", front=True)

    def _weather_avoid(self, f, st, snap, now):
        """Crews will not fly into a thunderstorm cell. In the terminal area (an arrival, or a departure still with
        Dubai) they ask for a deviation first and turn when it is approved; elsewhere, or with no answer when the cell
        is two minutes ahead, they turn on their own and say so."""
        fid = f.id
        if snap:
            here = (snap["lat"], snap["lon"])
            gsm = snap["gs_kt"] / 60.0
            cw = self.crew(f)
            cell = self.weather.in_cell(now, dest(here, snap["trk"], gsm * cw["turn_min"]))
            far = self.weather.in_cell(now, dest(here, snap["trk"], gsm * cw["ask_min"])) or cell
            devt = f.data.setdefault("dev_t", {})
            reqt = f.data.setdefault("req_t", {})
            req = f.data.get("devreq")
            if req and now - req["t"] > 240:
                f.data.pop("devreq", None)      # a stale request: the crew has sorted it out by now
                req = None
            # In the terminal area the crew asks first (ICAO Doc 4444: request the deviation as early as
            # possible) and turns when the controller approves or gives a heading. Once per cell, asked
            # again once if there is no answer; not from a holding pattern, whose track sweeps round.
            terminal = f.sector is not None and f.phase in ("arrival", "final", "airborne")
            if (terminal and far and req is None and st.mode not in ("loc", "hold")
                    and now - devt.get(far["id"], -1e9) > 180 and now - reqt.get(far["id"], -1e9) > 300):
                side = 1 if angdiff(snap["trk"], bearing(here, (far["lat"], far["lon"]))) < 0 else -1
                f.data["devreq"] = req = {"cell": far["id"], "side": side, "deg": cw["ask_deg"], "t": now, "asked": now}
                reqt[far["id"]] = now
                self.pilot_call(f, "deviation_request", f.sector, PH.deviation_request(fid, req["deg"], side, self.rng("say", fid, self.seq)), front=True)
            elif req and not cell and now - req["asked"] >= cw["reask_s"] and not req.get("again"):
                req["asked"], req["again"] = now, True
                self.pilot_call(f, "deviation_request", f.sector, PH.deviation_request(fid, req["deg"], req["side"], self.rng("say", fid, self.seq), again=True), front=True)
            # A cell two minutes ahead: the crew will not fly into it, approved or not.
            if cell and st.mode not in ("loc",) and now - devt.get(cell["id"], -1e9) > 180:
                devt[cell["id"]] = now
                c = (cell["lat"], cell["lon"])
                side = 1 if angdiff(snap["trk"], bearing(here, c)) < 0 else -1
                st.mode = "heading"
                st.hdg = (snap["trk"] + side * cw["turn_deg"]) % 360
                asked = f.data.pop("devreq", None)
                if f.kind == "DEP":
                    f.data["dep_dev"] = {"t": now}
                if f.sector:
                    text = (f"{fid} unable to continue, deviating {'right' if side > 0 else 'left'} heading {int(st.hdg):03d} for weather now"
                            if asked else f"{fid} deviating {'right' if side > 0 else 'left'} heading {int(st.hdg):03d} for weather")
                    self.pilot_call(f, "deviation", f.sector, text, front=True)

    def projected_done(self, f):
        """The earliest time a flight not yet completed could still land (arrival) or take off (departure), from
        where it is now. The record carries it as proj_t for reference only. A flight still pending at a grading earns 0 there."""
        now = self.t
        if f.kind == "ARR":
            if f.phase in ("scheduled", "enroute"):
                eta = (f.eta_entry or now)
                if f.edct is not None and f.origin_dep_t is not None:
                    eta += max(0.0, f.edct - f.origin_dep_t)
                eta = max(eta, now) + (f.data.get("center_delay_s") or 0.0) * (1.0 if f.phase == "scheduled" else 0.0)
                return eta + self.planned_arrival_min(f.entry or "VUTEB") * 60.0 - 8 * 60.0
            if f.phase in ("arrival", "final"):
                snap = self.air.snapshot(f.id)
                if snap is None:
                    return now + 600.0
                st = self.air.state.get(f.id)
                if st and st.app_rwy:
                    along, _ = self.air.loc_coords((snap["lat"], snap["lon"]), AP.RUNWAYS[st.app_rwy])
                    return now + max(0.0, along) / 150.0 * 3600.0
                return now + (dist_nm((snap["lat"], snap["lon"]), AP.ARP) + 20.0) / 200.0 * 3600.0
            return now + 600.0
        if f.phase == "not_ready":
            if f.tobt is not None:
                return max(now, f.tobt) + 15 * 60
            a = self.flights.get(f.prev_arr) if f.prev_arr else None
            if a is not None and a.outcome is None:
                return self.projected_done(a) + MIN_TURN_MIN[f.group] * 60 + 15 * 60
            return max(now, f.sched)
        if f.phase in ("ready", "pushed"):
            return now + 15 * 60
        if f.phase in ("taxi_out", "holding"):
            return now + 6 * 60
        return now + 60.0

    def side_of(self, f):
        """The feeder side an arrival is planned to, for the arrival runway in use. VUTEB, the busiest entry, feeds
        both sides: Center sends each VUTEB arrival to the side with fewer arrivals planned to it at the time."""
        rw = self.arr_rwy
        sides = f.data.setdefault("sides", {})
        if rw not in sides:
            flow = AP.flows_for(rw).get(f.entry, "N")
            if flow == "B":
                load = collections.Counter(g.data["sides"][rw] for g in self.flights.values()
                                           if g.kind == "ARR" and g.phase in ("enroute", "arrival") and rw in g.data.get("sides", {}))
                flow = "N" if load["N"] <= load["S"] else "S"
            sides[rw] = flow
        return sides[rw]

    def stack_clean(self, side):
        """Drop stack slots whose aircraft has left them: landed, diverted, gone, or cleared to another level."""
        reg = self.side_levels[side]
        for lvl, fid in list(reg.items()):
            st = self.air.state.get(fid)
            f = self.flights.get(fid)
            moving = (fid, "altitude") in self.pending
            if st is not None and f is not None and f.phase == "diverting":
                continue           # still in the area on its way out: the level stays taken until it is handed off
            if st is None or f is None or f.phase not in ("enroute", "arrival"):
                reg.pop(lvl)
                continue
            if side in ("M", "M2"):
                if st.app_rwy and not (st.mode == "missed"):
                    reg.pop(lvl)      # released from the missed approach hold
                continue
            snap = self.air.snapshot(fid)
            near_stack = snap is not None and abs(snap["alt_ft"] - lvl) < 600 and min(
                dist_nm((snap["lat"], snap["lon"]), AP.FIXES[p["sides"][sd]["feeder"]]) for p in AP.PROC.values() for sd in ("N", "S")) < 10
            if st.app_rwy and not near_stack:
                reg.pop(lvl)       # left the stack: the level is free once it is clear of the hold
            elif not st.app_rwy and not moving and st.alt is not None and abs(st.alt - lvl) > 50 \
                    and any(g == fid for L, g in reg.items() if L != lvl):
                reg.pop(lvl)       # it has another slot now
            # A cleared level that differs for a moment (a readback error being corrected) keeps the slot.
        return reg

    def missed_slot(self, fid, rw=None):
        """The missed approach hold (M at the first hold, M2 at the second, each at 3,000 ft) an aircraft keeps or can
        take now: (registry key, fix), or None when both are in use or have traffic near 3,000 ft around them."""
        proc = AP.proc_for(rw or self.arr_rwy)
        for key, fix in (("M", proc["missed_hold"]), ("M2", proc["missed_hold2"])):
            if self.stack_clean(key).get(3000) == fid:
                return key, fix
        for key, fix in (("M", proc["missed_hold"]), ("M2", proc["missed_hold2"])):
            if self.side_levels[key]:
                continue
            c = AP.FIXES[fix]
            busy = False
            for gid, gst in self.air.state.items():
                g = self.air.snapshot(gid)
                if gid == fid or g is None:
                    continue
                low = abs(g["alt_ft"] - 3000) < 900 or (gst.alt is not None and abs(gst.alt - 3000) < 900)
                bound = (gst.hold or {}).get("fix") == fix or (gst.route and gst.route[-1] == fix)
                if low and (bound or dist_nm(c, (g["lat"], g["lon"])) < 5.0):
                    busy = True       # holding there, on its way there, or passing close at that level
                    break
            if not busy:
                self.side_levels[key][3000] = fid
                return key, fix
        return None

    def free_level(self, side, fid, claim=True):
        reg = self.stack_clean(side)
        for lvl in ((3000,) if side == "M" else range(6000, 23000, 1000)):
            if lvl in reg:
                continue
            # nobody else near the stack at that level
            busy = False
            for gid, gst in self.air.state.items():
                if gid == fid:
                    continue
                if side != "M":
                    gf = self.flights.get(gid)
                    if gf is None or gf.kind != "ARR" or self.side_of(gf) != side:
                        continue      # only the arrivals for this side's feeder count
                g = self.air.snapshot(gid)
                near = g is not None and dist_nm((g["lat"], g["lon"]), AP.ARP) < 45
                if gst.alt is not None and abs(gst.alt - lvl) < 500 and gst.mode not in ("loc", "sid") and (near or gst.mode in ("hold", "center", "route")):
                    busy = True
                    break
                if g is not None and abs(g["alt_ft"] - lvl) < 500 and gst.mode not in ("loc", "sid") \
                        and dist_nm((g["lat"], g["lon"]), AP.ARP) < 45:
                    busy = True        # passing through that level near the airport
                    break
                if gid in self.pending_alt and abs(self.pending_alt[gid] - lvl) < 500:
                    busy = True        # a controller has just told it to go to that level
                    break
            if not busy:
                if claim:
                    reg[lvl] = fid
                return lvl
        return None

    def path_clear_of_cells(self, a, b, step_nm=2.0):
        """True when the straight line a -> b passes through no thunderstorm cell now."""
        d = dist_nm(a, b)
        n = max(1, int(d / step_nm))
        brg = bearing(a, b)
        for k in range(1, n + 1):
            if self.weather.in_cell(self.t, dest(a, brg, d * k / n)):
                return False
        return True

    def planned_arrival_min(self, entry):
        """Flight-planned minutes from the entry fix to touchdown: the standard arrival (entry, feeder, downwind to
        the 18 NM point, base, final from 11 NM) at 230 kt to the feeder, 180 kt on base and 160 kt on the final,
        plus the 8 minutes of holding the DXB AIP asks operators to plan in peak hours."""
        key = (entry, self.arr_rwy)
        cache = self.__dict__.setdefault("_planned", {})
        if key not in cache:
            rw = self.arr_rwy
            tag = rw[:2]
            flow = AP.flows_for(rw).get(entry, "N")
            mins = []
            for side in (("N", "S") if flow == "B" else (flow,)):    # a two-sided entry plans the longer arrival
                feeder = f"F{tag}{side}"
                pts = [AP.ENTRY[entry]["pos"]] + [AP.FIXES[x] for x in AP.proc_for(rw)["sides"][side].get("via", {}).get(entry, [])] + [AP.FIXES[feeder]]
                a = sum(dist_nm(p, q) for p, q in zip(pts, pts[1:]))
                b = dist_nm(AP.FIXES[feeder], AP.FIXES[f"D{tag}{side}8"]) + dist_nm(AP.FIXES[f"D{tag}{side}8"], AP.FIXES[f"{tag}F11"])
                mins.append(a / 230 * 60 + b / 180 * 60 + 11 / 160 * 60 + 8.0)
            cache[key] = max(mins)
        return cache[key]

    def _start_divert(self, f, why):
        flags = self.pilot_flags[f.id]
        flags.add("divert")
        alt = self.choose_alternate(f)
        st = self.air.state.get(f.id)
        if st is None:
            return
        if alt is None:
            self.emit("pilot_report", text=f"{f.id}: no alternate available, continuing to Dubai", flight=f.id)
            flags.discard("divert")
            return
        st.mode = "divert"
        snap = self.air.snapshot(f.id)
        st.route = [AP.ALTERNATES[alt]["pos"]]
        if snap is not None and dist_nm((snap["lat"], snap["lon"]), AP.ARP) < 40:
            # Out of the terminal area first, away from the airport and the other side's stack, then to the alternate.
            here = (snap["lat"], snap["lon"])
            st.route = [dest(here, bearing(AP.ARP, here), 12.0), AP.ALTERNATES[alt]["pos"]]
        st.alt = max(3000.0, snap["alt_ft"]) if snap else 8000     # leaves at its present level (at least 3,000 ft); the alternate's controllers take it from there
        f.data["divert_leave_t"] = self.t + 90
        st.app_rwy = None
        st.spd = None
        f.phase = "diverting"
        f.diverted_to = alt
        f.data["out_t"] = self.t
        sid = f.data.pop("stand", None)
        if sid and self.stand_occ.get(sid) == f.id:
            self.stand_occ.pop(sid, None)    # the booked stand is free again
        self.alt_used[alt] += 1
        self.leave_finals(f.id)
        self.emit("diverting", flight=f.id, text=f"{f.id} diverting to {alt}: {why}")
        if f.sector:
            self.pilot_call(f, "diverting", f.sector, f"{f.id} diverting to {AP.ALTERNATES[alt]['name']}, {why}", front=True)

    def release_alternate_stands(self):
        """A diverted aircraft frees its stand at the alternate when its return flight to Dubai departs."""
        keep = []
        for rid, code in self.alt_ground:
            r = self.flights.get(rid)
            dep = None if r is None else (r.edct if r.edct is not None else r.origin_dep_t)
            if dep is not None and self.t >= dep:
                self.alt_used[code] = max(0, self.alt_used[code] - 1)
            else:
                keep.append((rid, code))
        self.alt_ground = keep

    def choose_alternate(self, f):
        self.release_alternate_stands()
        prefs = {"UAE": ["OMDW", "OMAA", "OMSJ", "OOMS"], "FDB": ["OMDW", "OMSJ", "OMRK", "OMAA", "OOMS"]}.get(
            f.airline, ["OMSJ", "OMAA", "OMDW", "OOMS", "OMRK"]) + ["OTHH", "OBBI"]
        for code in prefs:
            a = AP.ALTERNATES[code]
            if self.alt_used[code] >= a["free_stands"]:
                continue
            if f.code == "F" and not a["a380"]:
                continue
            o = self.weather.alternate_obs(code, self.t) if code in ("OMDW", "OMSJ", "OMAA") else None
            need = MINIMA_RVR["I" if a["ils"] == "I" else ("II" if a["ils"] == "II" else f.cat)]
            if o is not None and o.rvr_or_vis < need:
                continue
            return code
        return None

    def _divert_upstream(self, f, delay):
        f.data["out_t"] = self.t
        f.phase = "diverted"
        f.outcome = "diverted"
        f.diverted_to = "OMAA"
        f.data["upstream"] = True
        self.emit("diverted", flight=f.id, text=f"{f.id} diverted en route after {int(delay // 60)} minutes of upstream holding (Dubai saturated)")
        self._create_return(f, self.t + 30 * 60)

    def _create_return(self, f, depart_t):
        rid = f.id + "R"
        if rid in self.flights:
            return
        d = dict(f.src)
        d.pop("prev_arr", None)
        d.update({"id": rid, "kind": "ARR", "airline": f.airline, "type": f.type, "reg": f.reg, "other": f.diverted_to,
                  "sched": f.sched, "entry": "VUTEB", "eta_entry": depart_t + 45 * 60, "origin_dep_t": depart_t,
                  "fuel_min": 45.0, "next_dep": f.next_dep, "return_of": f.id})
        r = Flight(d)
        r.phase = "scheduled"
        self.flights[rid] = r
        nd = self.flights.get(f.next_dep)
        if nd is not None:
            nd.prev_arr = rid

    def _divert_done(self, f):
        self.air.delete(f.id)
        f.phase = "diverted"
        f.outcome = "diverted"
        f.sector = None
        f.t_done = None
        self.emit("diverted", flight=f.id, text=f"{f.id} landed at {f.diverted_to}")
        # The aircraft comes back later as a return flight to DXB.
        rid = f.id + "R"
        if rid not in self.flights:
            d = dict(f.src)
            d.pop("prev_arr", None)
            d.update({"id": rid, "kind": "ARR", "airline": f.airline, "type": f.type, "reg": f.reg, "other": f.diverted_to,
                      "sched": f.sched, "entry": "VUTEB" if f.diverted_to in ("OMDW", "OMAA", "OTHH", "OBBI") else ("ITLAP" if f.diverted_to in ("OMSJ", "OMRK") else "IMPED"),
                      "eta_entry": self.t + (self.crew(f)["return_min"] + 2 * AP.ALTERNATES[f.diverted_to]["dist_nm"] / 7.0) * 60,
                      "origin_dep_t": self.t + (self.crew(f)["return_min"] + AP.ALTERNATES[f.diverted_to]["dist_nm"] / 7.0 - 20) * 60,
                      "fuel_min": 45.0, "next_dep": f.next_dep, "return_of": f.id})
            r = Flight(d)
            r.phase = "scheduled"
            self.flights[rid] = r
            nd = self.flights.get(f.next_dep)
            if nd is not None:
                nd.prev_arr = rid
        self.alt_ground.append((rid, f.diverted_to))

    def _crash(self, f, runway=None):
        """An accident. Only an accident on a runway closes that runway (for 4 hours: recovery and investigation)."""
        self.air.delete(f.id)
        self.movers.pop(f.id, None)
        self.landing.pop(f.id, None)
        self.takeoff.pop(f.id, None)
        self.lined.pop(f.id, None)
        f.phase = "crashed"
        f.outcome = "crashed"
        f.data["out_t"] = self.t
        f.sector = None
        if runway is not None and not any(c[3] == f"accident on runway {runway}" and c[2] > self.t for c in self.closures):
            self.closures.append((self.body(runway), self.t, self.t + 4 * 3600, f"accident on runway {runway}"))
            self.emit("notam", text=f"Runway {runway} closed after an accident, expected to reopen {self.clock(self.t + 4 * 3600)}")

    # -- safety -----------------------------------------------------------------------------------------------
    def _safety(self, kind, severity, flights, text):
        key = (kind, tuple(sorted(flights)))
        rank = {"A": 0, "A-": 1, "B": 2, "C": 3}
        worse = None
        for s in self.safety[-50:]:
            if s.get("replaced_t") is not None or self.t - s.get("seen_t", s["t"]) >= 300:
                continue
            same = s["key"] == key or (len(flights) > 1 and s["key"][1] == key[1])
            if not same:
                continue
            if rank.get(s["severity"], 9) <= rank.get(severity, 9):
                s["seen_t"] = round(self.t, 1)      # the same encounter goes on: it still counts once
                return
            worse = s
        if worse is not None:
            # The encounter got worse. It counts once: the earlier record ends now and the worse one starts now.
            worse["replaced_t"] = round(self.t, 1)
        ctx = {}
        for fid in flights:
            st = self.air.state.get(fid)
            snap = self.air.snapshot(fid)
            ctx[fid] = {"phase": self.flights[fid].phase if fid in self.flights else None, "sector": self.flights[fid].sector if fid in self.flights else None,
                        "mode": st.mode if st else None, "route": list(st.route)[:3] if st else None,
                        "hold": st.hold["fix"] if st and st.hold else None,
                        "alt": round(snap["alt_ft"]) if snap else None, "cleared_alt": st.alt if st else None,
                        "arp": (round(dist_nm((snap["lat"], snap["lon"]), AP.ARP), 1), round(bearing(AP.ARP, (snap["lat"], snap["lon"])))) if snap else None}
        ev = {"t": round(self.t, 1), "kind": kind, "severity": severity, "flights": flights, "text": text, "key": key, "ctx": ctx}
        self.safety.append(ev)
        self.emit("safety", what=kind, severity=severity, flights=flights, text=text)

    def _separation(self):
        ids, lat, lon, alt, gs = self.air.arrays()
        n = len(ids)
        if n < 2:
            return
        import numpy as np
        y = (lat - AP.ARP[0]) * 60.0
        x = (lon - AP.ARP[1]) * 60.0 * math.cos(math.radians(AP.ARP[0]))
        inside = (alt <= 25000) & (x * x + y * y <= 80 ** 2)     # the terminal area: within 80 NM, FL250 and below
        for i in range(n):
            if not inside[i]:
                continue
            dx = x[i + 1:] - x[i]
            dy = y[i + 1:] - y[i]
            dz = np.abs(alt[i + 1:] - alt[i])
            dh = np.sqrt(dx * dx + dy * dy)
            close = np.where((dh < RADAR_MIN_NM) & (dz < 900) & inside[i + 1:])[0]
            for k in close:
                j = i + 1 + int(k)
                a, b = ids[i], ids[j]
                d, v = float(dh[k]), float(dz[k])
                if d < 0.05 and v < 100:
                    self._safety("midair", "A", [a, b], f"Mid-air collision between {a} and {b}")
                    self._crash(self.flights[a])
                    self._crash(self.flights[b])
                    continue
                if self._exempt_pair(a, b, x[i], y[i], alt[i], x[j], y[j], alt[j]):
                    continue
                if d < 1.0 and v < 500:
                    self._safety("near_collision", "A-", [a, b], f"Near collision: {a} and {b} {d:.2f} NM / {int(v)} ft apart")
                elif d < 1.5 and v < 600:
                    self._safety("loss_of_separation", "B", [a, b], f"Serious loss of separation: {a} and {b} {d:.2f} NM / {int(v)} ft")
                else:
                    self._safety("loss_of_separation", "C", [a, b], f"Loss of separation: {a} and {b} {d:.2f} NM / {int(v)} ft")
        # In-trail spacing on final. The parallel runways count as one runway for wake turbulence, so the finals
        # of both runways in one direction form one sequence, measured along the first runway's centreline.
        dirs = collections.defaultdict(list)
        for rwy, order in self.final_order.items():
            if order:
                dirs[round(AP.RUNWAYS[rwy].heading)].append(rwy)
        for rwys in dirs.values():
            ref = AP.RUNWAYS[sorted(rwys)[0]]
            pts = []
            for rwy in rwys:
                for fid in self.final_order[rwy]:
                    s = self.air.snapshot(fid)
                    st_ = self.air.state.get(fid)
                    if s is None or st_ is None or st_.mode != "loc" or st_.app_rwy != rwy:
                        continue
                    along, cross = self.air.loc_coords((s["lat"], s["lon"]), ref)
                    pts.append((along, fid, rwy))
            pts.sort()
            for (d1, lead, r1), (d2, foll, rwy) in zip(pts, pts[1:]):
                gap = d2 - d1
                fl, ff = self.flights[lead], self.flights[foll]
                req = max(wake_nm(fl.group, ff.group), RADAR_MIN_NM)
                if req == RADAR_MIN_NM and d1 < 10 and r1 == rwy and self._reduced_final_ok():
                    req = RADAR_MIN_FINAL_NM
                if self.lvp_on():
                    req = max(req, 6.0)
                if gap < req:
                    w = wake_nm(fl.group, ff.group) > (RADAR_MIN_NM if d1 >= 10 else RADAR_MIN_FINAL_NM)
                    sev = "B" if gap < req * 0.75 else "C"
                    self._safety("wake_separation" if w else "final_spacing", sev, [lead, foll],
                                 f"{foll} {gap:.1f} NM behind {lead} on final {rwy} (minimum {req:.1f} NM)")

    def _tcas(self):
        """TCAS II, which every airliner here carries (ICAO Annex 6 requires ACAS II on aeroplanes over 5,700 kg or with
        more than 19 seats). It looks at each pair of airborne aircraft above 1,500 ft. A pair inside about 0.55 NM and
        600 ft, or that will be within 25 s on its present tracks and climb rates, gets a resolution advisory: the
        higher aircraft climbs 1,000 ft and the lower one descends 1,000 ft (not below 1,500 ft, and not off an ILS
        approach). An aircraft established on the localizer that must climb goes around instead. The crews report the
        advisory on their frequency, fly it for at least 30 s while the threat lasts, and then return to their
        cleared altitude. It is a last defence: the separation check still records how close the pair came."""
        now = self.t
        ac = []
        for fid, st in self.air.state.items():
            f = self.flights.get(fid)
            snap = self.air.snapshot(fid)
            if f is None or snap is None or f.outcome not in (None, "departed"):
                continue
            if st.ra is not None and now >= st.ra[2]:
                st.ra = None
                self.emit("tcas", flight=fid, text=f"{fid} clear of conflict")
                if f.sector:
                    self.pilot_call(f, "clear_of_conflict", f.sector,
                                    f"{fid}, clear of conflict, returning to {int(st.alt or snap['alt_ft'])} ft")
            if snap["alt_ft"] < 1500:
                continue
            y = (snap["lat"] - AP.ARP[0]) * 60.0
            x = (snap["lon"] - AP.ARP[1]) * 60.0 * math.cos(math.radians(AP.ARP[0]))
            g = snap["gs_kt"] / 3600.0
            tr = math.radians(snap["trk"])
            ac.append((fid, f, st, snap, x, y, g * math.sin(tr), g * math.cos(tr)))
        ac.sort(key=lambda a: a[4])
        for i in range(len(ac)):
            a = ac[i]
            for j in range(i + 1, len(ac)):
                b = ac[j]
                if b[4] - a[4] > 6.0:
                    break
                rx, ry = b[4] - a[4], b[5] - a[5]
                if rx * rx + ry * ry > 36.0:
                    continue
                za, zb = a[3]["alt_ft"], b[3]["alt_ft"]
                dz = zb - za
                if abs(dz) > 1500:
                    continue
                if self._exempt_pair(a[0], b[0], a[4], a[5], za, b[4], b[5], zb):
                    continue
                vx, vy = b[6] - a[6], b[7] - a[7]
                vz = (b[3]["vs_fpm"] - a[3]["vs_fpm"]) / 60.0
                vv = vx * vx + vy * vy
                tc = 0.0 if vv < 1e-9 else max(0.0, min(40.0, -(rx * vx + ry * vy) / vv))
                mx, my = rx + vx * tc, ry + vy * tc
                rng = math.hypot(rx, ry)
                close_now = rng < 0.55 and abs(dz) < 600
                close_soon = tc <= 25.0 and math.hypot(mx, my) < 0.55 and abs(dz + vz * tc) < 600
                if close_now or close_soon:
                    hi, lo = (b, a) if (zb, b[0]) > (za, a[0]) else (a, b)
                    self._advise(hi, "climb", lo[0])
                    if lo[2].mode != "loc" and lo[3]["alt_ft"] >= 2000:
                        self._advise(lo, "descend", hi[0])

    def _advise(self, a, sense, other):
        fid, f, st, snap = a[0], a[1], a[2], a[3]
        now = self.t
        if st.ra is not None:
            if st.ra[1] == sense:
                st.ra = (st.ra[0], sense, now + 30.0)   # the threat lasts: keep flying the advisory
            return
        if sense == "climb" and st.mode == "loc":
            self.go_around(f, "TCAS resolution advisory")
        target = snap["alt_ft"] + 1000.0 if sense == "climb" else max(1500.0, snap["alt_ft"] - 1000.0)
        st.ra = (round(target, -2), sense, now + 30.0)
        self.stats["tcas_ra"] += 1
        self.emit("tcas", flight=fid, other=other, sense=sense, text=f"{fid} TCAS RA: {sense} (traffic {other})")
        if f.sector:
            self.pilot_call(f, "tcas_ra", f.sector, f"{fid}, TCAS RA, {'climbing' if sense == 'climb' else 'descending'}",
                            front=True)

    def _exempt_pair(self, a, b, xa, ya, za, xb, yb, zb):
        """Pairs the radar minimum does not apply to: both on the same final (the in-trail check covers them),
        or close to the runways below 1,500 ft (runway separation applies there)."""
        sa, sb = self.air.state.get(a), self.air.state.get(b)
        if sa and sb and sa.mode == "loc" and sb.mode == "loc" and sa.app_rwy == sb.app_rwy \
                and a in self.final_order.get(sa.app_rwy, ()) and b in self.final_order.get(sa.app_rwy, ()):
            return True
        near = lambda x, y, z: x * x + y * y < 4.0 ** 2 and z < 1500
        if near(xa, ya, za) and near(xb, yb, zb):
            return True
        return False

    # -- the score --------------------------------------------------------------------------------------------
    def score(self, now=None, lag_s=1800):
        """The day score as the grader computes it (tests/grade.py), for offline runs: over the flights scheduled up
        to 30 minutes before now, and only completed flights earn credit."""
        now = self.t if now is None else now
        total, n = 0.0, 0
        rows = collections.Counter()
        for f in list(self.flights.values()):
            if f.data.get("return_of") or f.sched < 0 or f.sched > now - lag_s:
                continue
            n += 1
            if f.outcome in ("landed", "departed"):
                c, row = credit((f.t_done - f.sched) / 60.0), "done"
            elif f.outcome == "diverted":
                r = self.flights.get(f.id + "R")
                while r is not None and r.outcome == "diverted" and self.flights.get(r.id + "R") is not None:
                    r = self.flights[r.id + "R"]
                if r is not None and r.outcome == "landed":
                    c, row = credit((r.t_done - f.sched) / 60.0), "diverted_returned"
                elif r is not None and r.outcome is None:
                    c, row = 0.0, "diverted_returning"
                else:
                    c, row = 0.0, "diverted"
            elif f.outcome in ("cancelled", "crashed"):
                c, row = 0.0, f.outcome
            else:
                c, row = 0.0, "pending"
            total += c
            rows[row] += 1
        accidents, rest = 1.0, 1.0
        for s_ in self.safety:
            if s_.get("replaced_t") is not None:
                continue
            if s_["severity"] == "A":
                accidents *= 0.5
            else:
                rest *= {"A-": 0.9, "B": 0.97, "C": 0.995}.get(s_["severity"], 1.0)
        mult = accidents * rest ** (100.0 / max(n, 100))  # accidents in full, the rest per 100 flights counted
        base = total / n if n else 0.0
        return {"flights_counted": n, "credit": round(total, 3), "service": round(base, 4), "safety_multiplier": round(mult, 4),
                "score": round(base * mult, 4), "rows": dict(rows), "safety_events": sum(1 for s_ in self.safety if s_.get("replaced_t") is None)}


# ---------------------------------------------------------------------------------------------------------------
# Ops. Each radio op: (sector kinds allowed, check(world, flight, op), apply(world, flight, op) -> readback text)

def _project(a, t):
    """Where an airborne aircraft (flight, state, snapshot, position) will be in t seconds on its present track, speed
    and vertical speed, levelling at its cleared altitude."""
    f, st, snap, p = a
    z = snap["alt_ft"] + snap["vs_fpm"] * t / 60.0
    if st.alt is not None:
        z = min(z, max(st.alt, snap["alt_ft"])) if snap["vs_fpm"] >= 0 else max(z, min(st.alt, snap["alt_ft"]))
    return dest(p, snap["trk"], snap["gs_kt"] * t / 3600.0), z


MAX_VIA = 12  # taxiways in one taxi route; each one is a path search on the clock thread
MAX_LIST = 32  # items in any list of an op (fixes, sectors, ...)
MAX_STR = 64   # characters in any string of an op
CENTER_MIN_NM, CENTER_MIN_FT = 25.0, 9000  # where Center accepts a departure (the terminal area's edge)


def _check_shape(op, name):
    """Refuse an op whose fields are too large or of the wrong kind before anything acts on it."""
    def walk(v, depth):
        if depth > 3:
            raise Refusal("an op is nested too deeply")
        if isinstance(v, str) and len(v) > MAX_STR:
            raise Refusal(f"strings in an op are at most {MAX_STR} characters")
        if isinstance(v, list):
            if len(v) > MAX_LIST:
                raise Refusal(f"lists in an op have at most {MAX_LIST} items")
            for x in v:
                walk(x, depth + 1)
        if isinstance(v, dict):
            if len(v) > MAX_LIST:
                raise Refusal(f"an op has at most {MAX_LIST} fields")
            for x in v.values():
                walk(x, depth + 1)
    walk(op, 0)
    via = op.get("via")
    if via is not None and (not isinstance(via, list) or len(via) > MAX_VIA or not all(isinstance(x, str) for x in via)):
        raise Refusal(f"via must be a list of at most {MAX_VIA} taxiway names")
    then = op.get("then")
    if name != "direct" and then is not None and (not isinstance(then, str) or then not in SECTORS
                                                  or SECTORS[then][0] not in RADIO_KINDS):
        raise Refusal("then must be the name of a sector with a frequency")
    secs = op.get("sectors")
    if secs is not None and (not isinstance(secs, list) or len(set(map(str, secs))) != len(secs)):
        raise Refusal("sectors must be a list of distinct sector names")


def _finite(v):
    """False when v holds a NaN or an infinity anywhere (JSON parsers accept NaN and Infinity)."""
    if isinstance(v, bool):
        return True
    if isinstance(v, int):
        return abs(v) < 1e15  # Python ints have no limit; one too big for a float overflows later
    if isinstance(v, float):
        return math.isfinite(v)
    if isinstance(v, dict):
        return all(_finite(x) for x in v.values())
    if isinstance(v, (list, tuple)):
        return all(_finite(x) for x in v)
    if isinstance(v, str):
        try:
            return math.isfinite(float(v))
        except ValueError:
            return True
    return True


def _public_op(op):
    return {k: v for k, v in op.items() if not k.startswith("_")}


def _air(w, f):
    st = w.air.state.get(f.id)
    if st is None:
        raise Refusal(f"{f.id} is not airborne")
    return st


def _misheard(op, key, value, step):
    if op.get("_misheard"):
        return value + step
    return value


def _ck_heading(w, f, op):
    _air(w, f)
    h = op.get("hdg")
    if not isinstance(h, (int, float)) or not 0 <= h <= 360:
        raise Refusal("hdg must be 0 to 360")
    snap = w.air.snapshot(f.id)
    if snap and f.data.get("devreq"):
        here, gsm = (snap["lat"], snap["lon"]), snap["gs_kt"] / 60.0
        if any(w.weather.in_cell(w.t, dest(here, float(h), gsm * m)) for m in (1, 2, 3, DEV_REQUEST_MIN)):
            raise Refusal("unable, weather on that heading")


def _break_off(w, f, st):
    """A heading, direct, route or hold given to an aircraft on the localizer takes it off the approach."""
    if st.mode != "loc":
        return
    w.cleared_land.discard(f.id)
    w.leave_finals(f.id)
    st.app_rwy, st.established, st.on_gs = None, False, False
    if f.phase == "final":
        f.phase = "arrival"


def _ap_heading(w, f, op):
    st = _air(w, f)
    _break_off(w, f, st)
    h = _misheard(op, "hdg", float(op["hdg"]), 20)
    st.mode = "heading"
    st.hdg = h % 360
    st.hold = None
    req = f.data.pop("devreq", None)
    if req:
        f.data.setdefault("dev_t", {})[req["cell"]] = w.t
    f.data.pop("dep_dev", None)          # the controller is vectoring it now
    return f"{f.id} heading {int(st.hdg) % 360:03d}"


def _ck_deviation(w, f, op):
    _air(w, f)
    if not isinstance(op.get("approve"), bool):
        raise Refusal("deviation needs approve: true or false")
    if not f.data.get("devreq"):
        raise Refusal(f"{f.id} has not asked for a deviation")


def _ap_deviation(w, f, op):
    st, req = _air(w, f), f.data["devreq"]
    side = "right" if req["side"] > 0 else "left"
    if not op["approve"]:
        req["denied"] = w.t
        return f"{f.id} unable approve deviation, standing by, will advise"
    snap = w.air.snapshot(f.id)
    f.data.pop("devreq")
    f.data.setdefault("dev_t", {})[req["cell"]] = w.t
    st.mode, st.hold = "heading", None
    st.hdg = ((snap["trk"] if snap else st.hdg or 0) + req["side"] * req["deg"]) % 360
    if f.kind == "DEP":
        f.data["dep_dev"] = {"t": w.t}
    return f"{f.id} deviating {req['deg']} degrees {side}, will report clear of weather"


def _ck_alt(w, f, op):
    _air(w, f)
    a = op.get("ft")
    if not isinstance(a, (int, float)) or not 2000 <= a <= 41000:
        raise Refusal("ft must be 2000 to 41000")
    if op.get("_rb") and (f.data.get("turb_until") or 0) > w.t:
        s = w.air.snapshot(f.id)
        if s is not None and a < s["alt_ft"] - 200:
            raise Refusal("unable descent, moderate turbulence, will advise when able")


def _ap_alt(w, f, op):
    st = _air(w, f)
    a = _misheard(op, "ft", float(op["ft"]), 1000)
    st.alt = a
    return f"{f.id} {'climb' if f.kind == 'DEP' else 'descend'} {int(a)} ft"


def _ck_speed(w, f, op):
    st = _air(w, f)
    s = op.get("kt")
    if s is not None and (not isinstance(s, (int, float)) or not f.ti["vapp_kt"] <= s <= 340):
        raise Refusal(f"kt must be null (own speed) or {f.ti['vapp_kt']} to 340 for a {f.type}")
    snap = w.air.snapshot(f.id) if op.get("_rb") else None
    if snap is not None and s is not None and f.kind == "ARR" and st.mode not in ("loc",) and not st.app_rwy \
            and dist_nm((snap["lat"], snap["lon"]), AP.ARP) > 20.0:
        low = f.ti["vapp_kt"] + w.crew(f)["min_spd_add"]
        if s < low:
            raise Refusal(f"unable speed {int(s)}, minimum {int(round(low / 5.0) * 5)} until on the approach")


def _ap_speed(w, f, op):
    st = _air(w, f)
    s = op.get("kt")
    st.spd = None if s is None else _misheard(op, "kt", float(s), 20)
    return f"{f.id} speed {'own discretion' if s is None else int(st.spd)}"


def _ck_direct(w, f, op):
    _air(w, f)
    if not isinstance(op.get("fix"), str) or op["fix"] not in AP.FIXES:
        raise Refusal(f"unknown fix {op.get('fix')!r}")
    then = op.get("then", [])
    if not isinstance(then, list) or any(not isinstance(x, str) or x not in AP.FIXES for x in then):
        raise Refusal("then must be a list of known fix names")


def _ap_direct(w, f, op):
    st = _air(w, f)
    _break_off(w, f, st)
    then = [x for x in op.get("then", []) if x in AP.FIXES]
    st.mode = "route"
    st.route = [op["fix"]] + then
    st.hold = None
    f.data.pop("dep_dev", None)
    return f"{f.id} direct {op['fix']}" + (f" then {' '.join(then)}" if then else "")


def _ck_route(w, f, op):
    _air(w, f)
    r = op.get("fixes")
    if not isinstance(r, list) or not r or any(x not in AP.FIXES for x in r):
        raise Refusal("fixes must be a list of known fix names")


def _ap_route(w, f, op):
    st = _air(w, f)
    _break_off(w, f, st)
    st.mode = "route"
    st.route = list(op["fixes"])
    st.hold = None
    return f"{f.id} cleared via {' '.join(op['fixes'])}"


def _ck_hold(w, f, op):
    _air(w, f)
    if not isinstance(op.get("fix"), str) or op["fix"] not in AP.HOLDS:
        raise Refusal(f"no published hold at {op.get('fix')!r}; holds: {', '.join(sorted(AP.HOLDS))}")
    efc = op.get("efc")
    if efc is not None and (isinstance(efc, bool) or not isinstance(efc, (int, float))):
        raise Refusal("efc must be a number of airport seconds")


def _ap_hold(w, f, op):
    st = _air(w, f)
    _break_off(w, f, st)
    st.mode = "hold"
    st.hold = AP.HOLDS[op["fix"]]
    st.hold_phase = "to_fix"
    if op.get("efc") is not None:
        f.data["eat"] = float(op["efc"])
    f.data.setdefault("holding_since", w.t)
    return f"{f.id} hold at {op['fix']} as published" + (f", expect further clearance {w.clock(op['efc'])}" if op.get("efc") else "")


def _ck_approach(w, f, op):
    st = _air(w, f)
    r = op.get("runway")
    if r not in AP.RUNWAYS:
        raise Refusal("unknown runway")
    if w.runway_closed(r):
        raise Refusal(f"runway {r} is closed")
    if f.kind != "ARR":
        raise Refusal("not an arrival")
    if r != w.arr_rwy:
        raise Refusal(f"unable, runway {r} is not the arrival runway (arrivals use {w.arr_rwy})")


def _ap_approach(w, f, op):
    st = _air(w, f)
    r = op["runway"]
    o = w.weather.current(w.t)
    need = MINIMA_RVR[f.cat]
    if w.ils_out.get(r, 0) > w.t:
        need = max(need, 1500)
    if o.rvr_or_vis < need:
        raise Refusal(f"below our minima for runway {r} (we are CAT {f.cat}{', glide path out' if need >= 1500 else ''})")
    head, cross, gh, gc = w.weather.runway_wind(w.t, AP.RUNWAYS[r].heading)
    if gh < -10:
        raise Refusal(f"tailwind {int(-gh)} kt on runway {r} is beyond our limit")
    if st.app_rwy is not None and st.app_rwy != r:
        # A new runway replaces the old approach: the aircraft leaves the old final and its landing clearance.
        w.leave_finals(f.id)
        w.cleared_land.discard(f.id)
        st.established, st.on_gs = False, False
        if f.phase == "final":
            f.phase = "arrival"
    st.app_rwy = r
    if op.get("then"):
        f.data["then_at_8nm"] = op["then"]
    return f"{f.id} cleared ILS approach runway {r}"


def _ck_eat(w, f, op):
    if not isinstance(op.get("time"), (int, float)):
        raise Refusal("time must be in seconds since the start of the day")


def _ap_eat(w, f, op):
    f.data["eat"] = float(op["time"])
    return f"{f.id} expect approach at {w.clock(op['time'])}"


def _ck_divert(w, f, op):
    _air(w, f)
    if op.get("to") is not None and op["to"] not in AP.ALTERNATES:
        raise Refusal("unknown alternate")
    if w.choose_alternate(f) is None:
        raise Refusal("unable, no alternate available")


def _ap_divert(w, f, op):
    w._start_divert(f, "instructed by ATC")
    return f"{f.id} diverting to {f.diverted_to}"


def _ck_handoff(w, f, op):
    to = op.get("to")
    if to != "CENTER" and to not in SECTORS:
        raise Refusal("to must be a sector name or CENTER")
    if to == "CENTER" and f.kind != "DEP":
        raise Refusal("only departures are handed to CENTER")
    if to == "CENTER":
        s = w.air.snapshot(f.id)
        if s is None or s["alt_ft"] < CENTER_MIN_FT or dist_nm((s["lat"], s["lon"]), AP.ARP) < CENTER_MIN_NM:
            raise Refusal(f"CENTER takes departures {CENTER_MIN_NM:g} NM or more from the airport and at or above "
                          f"{CENTER_MIN_FT:,} ft")
    if to != "CENTER" and SECTORS[to][0] not in RADIO_KINDS:
        raise Refusal(f"{to} has no frequency")


def _ap_handoff(w, f, op):
    to = op["to"]
    if to == "CENTER":
        w._departure_exit(f)
        return f"{f.id} contact Center, good day"
    old = f.sector
    w.pilot_call(f, "checkin", to, f"{to}, {f.id} with you")
    return f"{f.id} contact {to}"


def _ck_land(w, f, op):
    st = _air(w, f)
    if st.app_rwy is None:
        raise Refusal("not cleared for an approach")
    if w.runway_closed(st.app_rwy):
        raise Refusal(f"runway {st.app_rwy} is closed")
    if w.body(st.app_rwy) in w.braking_poor:
        # Crews on the tower frequency heard the report: none lands until the runway has been inspected.
        raise Refusal(f"unable to land, braking action poor reported on runway {st.app_rwy}, request runway inspection")
    if f.id in w.cleared_land:
        raise Refusal("already cleared to land")


def _ap_land(w, f, op):
    st = _air(w, f)
    w.cleared_land.add(f.id)
    if op.get("exit") is not None:
        f.data["exit"] = op["exit"]
    if op.get("then"):
        f.data["then_after_vacate"] = op["then"]
    o = w.weather.current(w.t)
    d, kt, g = w.weather.surface_wind(w.t)
    # Landing on an occupied runway: the clearance itself is the error the safety check records at touchdown.
    return f"{f.id} cleared to land runway {st.app_rwy}, wind {int(d):03d}/{kt}" + (f" gust {g}" if g else "")


def _ck_go_around(w, f, op):
    st = _air(w, f)
    if f.phase == "diverting" or (st.app_rwy is None and st.mode != "loc"):
        raise Refusal(f"{f.id} is not on an approach")


def _ap_go_around(w, f, op):
    w.go_around(f, "instructed by ATC")
    return f"{f.id} going around"


def _ck_lineup(w, f, op):
    if f.kind != "DEP" or f.phase != "holding":
        raise Refusal(f"{f.id} is not holding at a runway (phase {f.phase})")
    r = f.data.get("dep_rwy")
    if w.runway_closed(r):
        raise Refusal(f"runway {r} is closed")


def _ap_lineup(w, f, op):
    m = w.movers.pop(f.id, None)
    r = f.data["dep_rwy"]
    j = f.data["entry_junction"]
    # Entering the runway while an arrival is landing or about to: an incursion if it is too close.
    for fid in list(w.cleared_land):
        st = w.air.state.get(fid)
        s = w.air.snapshot(fid)
        if st and s and st.app_rwy and w.body(st.app_rwy) == w.body(r):
            along, cross_ = w.air.loc_coords((s["lat"], s["lon"]), AP.RUNWAYS[st.app_rwy])
            if 0 < along < 2.0 and abs(cross_) < 0.5:
                w._safety("runway_incursion", "B", [f.id, fid], f"{f.id} lined up on runway {r} with {fid} {along:.1f} NM out cleared to land")
    for fid, T in list(w.takeoff.items()):
        if w.body(T["rwy"]) == w.body(r):
            w._safety("runway_incursion", "B", [f.id, fid], f"{f.id} lined up on runway {r} during {fid}'s take-off roll")
    for fid, L in list(w.landing.items()):
        if w.body(L["rwy"]) == w.body(r) and w.ground.junction[j]["d"].get(L["rwy"], 0) > L["d"]:
            w._safety("runway_incursion", "B", [f.id, fid], f"{f.id} lined up on runway {r} in front of {fid} on its landing roll")
    w.lined[f.id] = (r, j)
    f.phase = "lined_up"
    f.data["lined_t"] = w.t + LINEUP_S
    return f"{f.id} line up and wait runway {r}"


def _ck_takeoff(w, f, op):
    if f.phase != "lined_up":
        raise Refusal(f"{f.id} is not lined up")
    if f.data.get("dep_rwy") != w.dep_rwy:
        raise Refusal(f"unable, runway {f.data.get('dep_rwy')} is not the departure runway (departures use {w.dep_rwy})")
    if (f.gate or "WEST") in w.closed_exits:
        raise Refusal(f"the {f.gate or 'WEST'} exit is closed: {w.closed_exits[f.gate or 'WEST'][1]}")
    ctot = f.data.get("ctot")
    if ctot is not None and w.t < ctot - DFLOW_WINDOW_S:
        raise Refusal(f"{f.id} slot {w.clock(ctot)[:5]}Z: not before {w.clock(ctot - DFLOW_WINDOW_S)[:5]}Z")
    if ctot is not None and w.t > ctot + DFLOW_WINDOW_S:
        new = w.dflow_slot(f, w.t + 120)
        raise Refusal(f"{f.id} missed its slot; new slot {w.clock(new)[:5]}Z" if new else f"{f.id} missed its slot; DFLOW has a slot free now, ask again")
    if w.t < f.data.get("lined_t", 0):
        raise Refusal("still lining up", retry_after_sec=round(f.data["lined_t"] - w.t, 1))
    r, j = w.lined[f.id]
    if w.runway_closed(r):
        raise Refusal(f"runway {r} is closed")
    here = w.ground.junction[j]["d"].get(r, 0)
    ahead = [(fid, role) for fid, role, d in w.runway_occupants(r, exclude=f.id)
             if role in ("landing", "takeoff") or (role == "lined_up" and d > here)]
    if ahead:
        raise Refusal(f"unable, runway {r} not clear: {', '.join(fid for fid, _ in ahead)} on the runway")


def _ap_takeoff(w, f, op):
    r, j = w.lined.pop(f.id)
    start = w.ground.junction[j]["d"].get(r, 0) + AP.RUNWAYS[r].displaced_m
    o = w.weather.current(w.t)
    hot = (o.temp_c or 30) > 40
    a = (1.7 if f.group in "AB" else 2.1) * (0.88 if hot else 1.0)
    for fid, role, _d in w.runway_occupants(r, exclude=f.id):
        if role == "crossing":
            w._safety("runway_incursion", "B", [f.id, fid], f"{f.id} cleared for take-off on runway {r} while {fid} crosses it")
    w.takeoff[f.id] = {"rwy": r, "t0": w.t, "v": 0.0, "a": a, "vto": f.ti["vto_kt"] * KT_MS, "d": 0.0, "start": start, "j": j}
    f.phase = "takeoff"
    # Wake turbulence and route separation from the previous departure from either runway (< 760 m apart).
    for (t0, g, gate, rr, st0, _fid) in reversed(w.last_dep[-3:]):
        gap = w.t - t0
        need = wake_dep_s(g, f.group, intermediate=start > st0 + 500)
        route_need = 120.0 if gate == (f.gate or "WEST") else 60.0
        if gap < need:
            w._safety("wake_separation", "B" if gap < need * 0.75 else "C", [f.id], f"{f.id} take-off {int(gap)} s after a group {g} departure (minimum {int(need)} s)")
        if gap < route_need:
            w._safety("departure_spacing", "C", [f.id], f"{f.id} take-off {int(gap)} s after the previous departure on the {gate} route (minimum {int(route_need)} s)")
        break
    w.last_dep.append((w.t, f.group, f.gate or "WEST", r, start, f.id))
    w.dflow[f.id] = w.t          # the plan counts the actual take-off
    if op.get("then"):
        f.data["then_after_airborne"] = op["then"]
    d, kt, g = w.weather.surface_wind(w.t)
    return f"{f.id} cleared for take-off runway {r}, wind {int(d):03d}/{kt}"


def _ck_cancel_to(w, f, op):
    if f.phase not in ("lined_up", "takeoff"):
        raise Refusal("not on the runway")


def _ap_cancel_to(w, f, op):
    T = w.takeoff.pop(f.id, None)
    if T is not None and T["v"] > 0.8 * T["vto"]:
        w.takeoff[f.id] = T
        raise Refusal("too fast to stop, continuing")
    r, j = (T["rwy"], T.get("j")) if T else w.lined.get(f.id, (None, None))
    w.lined.pop(f.id, None)
    w.last_dep = [x for x in w.last_dep if x[5] != f.id]    # a take-off that did not happen leaves no wake
    if T is not None and T["v"] > 30:
        w.closures.append((w.body(r), w.t, w.t + 15 * 60, f"inspection after {f.id} rejected take-off"))
        w.emit("notam", text=f"Runway {r} closed about 15 minutes: inspection after a rejected take-off")
        w._return_to_stand(f)
        # Brakes cool and the aircraft is checked: the crew calls ready again in about 30 minutes.
        f.data["tobt_floor"] = w.t + 30 * 60
        f.tobt = max(f.tobt or 0, f.data["tobt_floor"])
        return f"{f.id} stopping, returning to stand"
    # Lined up, not rolling: vacate the runway at the entry and wait for a new taxi clearance.
    if j is not None:
        back = w.ground.holds.get(j) or [v for v, _ in w.ground.adj.get(j, ()) if v not in w.ground.junction]
        node = back[0] if back else j
        m = Mover([node, node])
        m.stop_at = 0
        w.movers[f.id] = m
    f.phase = "taxi_out"
    f.data["needs_taxi"] = True
    return f"{f.id} cancelling take-off, vacating the runway"


def _ck_cross(w, f, op):
    m = w.movers.get(f.id)
    if m is None or m.waiting_for is None:
        raise Refusal(f"{f.id} is not holding short of a runway")
    j = f.data.get("crossing_junction")
    if j is None:
        raise Refusal("no runway to cross ahead")
    b = w.ground.junction[j]["body"]
    if w.lvp_on():
        arr_body = w.body(w.arr_rwy)
        d = w.ground.junction[j]["d"].get(w.arr_rwy, 0)
        if b == arr_body and 500 < d < AP.RUNWAYS[w.arr_rwy].length_m - 500:
            raise Refusal("the landing runway may not be crossed in LVP except at its ends")


def _ap_cross(w, f, op):
    m = w.movers[f.id]
    j = f.data.pop("crossing_junction")
    b = w.ground.junction[j]["body"]
    # Crossing a runway with an arrival close in: a runway incursion.
    for fid in list(w.cleared_land):
        st = w.air.state.get(fid)
        s = w.air.snapshot(fid)
        if st and s and st.app_rwy and w.body(st.app_rwy) == b:
            along, cross_ = w.air.loc_coords((s["lat"], s["lon"]), AP.RUNWAYS[st.app_rwy])
            if 0 < along < 1.5 and abs(cross_) < 0.5:
                w._safety("runway_incursion", "B", [f.id, fid], f"{f.id} cleared to cross with {fid} {along:.1f} NM from touchdown")
    for fid, T in list(w.takeoff.items()):
        if w.body(T["rwy"]) == b:
            w._safety("runway_incursion", "B", [f.id, fid], f"{f.id} cleared to cross during {fid}'s take-off roll")
    for fid, L in list(w.landing.items()):
        if w.body(L["rwy"]) == b and w.ground.junction[j]["d"].get(L["rwy"], 0) > L["d"]:
            w._safety("runway_incursion", "B", [f.id, fid], f"{f.id} cleared to cross in front of {fid} on its landing roll")
    for k in range(m.i, len(m.path)):
        if m.path[k] == j:
            # The clearance covers every node of this runway on the path, and the first node off it.
            while k + 1 < len(m.path) and m.path[k + 1] in w.ground.junction and w.ground.junction[m.path[k + 1]]["body"] == b:
                k += 1
            m.cleared_to = k + 1
            break
    m.waiting_for = None
    if op.get("then"):
        f.data["then_after_cross"] = op["then"]
    return f"{f.id} cross runway {'12L/30R' if b == 'N' else '12R/30L'}"


def _ck_taxi(w, f, op):
    if f.kind == "ARR" and f.phase != "taxi_in":
        raise Refusal(f"{f.id} has not vacated the runway")
    if f.kind == "DEP" and f.phase not in ("taxi_out", "holding", "pushed"):
        raise Refusal(f"{f.id} is not ready to taxi (phase {f.phase})")
    done_t = f.data.get("push_done_t")
    if f.phase == "pushed" and done_t is not None and w.t < done_t:
        raise Refusal("pushback and start-up in progress", retry_after_sec=round(done_t - w.t, 1))
    to = op.get("to")
    if f.kind == "ARR":
        if to not in w.ground.stands:
            raise Refusal("to must be a stand for an arrival")
        _stand_fits(w, f, to)
    else:
        r = op.get("runway") or w.dep_rwy
        if r not in AP.RUNWAYS:
            raise Refusal("unknown runway")
        if r != w.dep_rwy:
            raise Refusal(f"unable, runway {r} is not the departure runway (departures use {w.dep_rwy})")
        if op.get("entry") is not None and str(op["entry"]) not in {str(e[0]) for e in w.ground.entries[r]}:
            raise Refusal(f"{op['entry']} is not an entry of runway {r}")


def _ap_taxi(w, f, op):
    m = w.movers.get(f.id)
    here = m.path[m.i] if m else w.ground.stands[f.stand]["node"]
    via = op.get("via")
    if f.kind == "ARR":
        sid = op["to"]
        if sid in w.stand_occ and w.stand_occ[sid] not in (f.id, None) and w.stand_occ[sid] in w.parked:
            pass  # the crew will wait at the stand entrance
        path = w.ground.path(here, w.ground.stands[sid]["node"], via_refs=via, opposing=w.opposing_edges(f.id),
                             avoid_refs=CODE_F_AVOID if f.code == "F" else None)
        if path is None:
            raise Refusal(f"no route to stand {sid}")
        f.data["stand"] = sid
        if sid not in w.stand_occ:
            w.stand_occ[sid] = f.id
    else:
        r = op.get("runway") or w.dep_rwy
        entries = w.ground.entries[r]
        if op.get("entry") is not None:
            entries = [e for e in entries if str(e[0]) == str(op["entry"])]
        else:
            entries = [e for e in entries if e[2]] or entries
        best = None
        for j, start, full in entries:
            p = w.ground.path(here, j, via_refs=via, opposing=w.opposing_edges(f.id),
                              avoid_refs=CODE_F_AVOID if f.code == "F" else None)
            if p and (best is None or w.ground.length(p) < best[0]):
                best = (w.ground.length(p), p, j)
        if best is None:
            raise Refusal(f"no route to runway {r}")
        path = best[1]
        f.data["entry_junction"] = best[2]
        f.data["dep_rwy"] = r
        f.phase = "taxi_out"
    nm = Mover(path)
    if f.kind == "DEP":
        nm.stop_at = w.ground.hold_before(path, len(path) - 1)
    if m is not None:
        nm.speed = m.speed
    w.movers[f.id] = nm
    if op.get("then"):
        f.data["then_at_hold"] = op["then"]
    return f"{f.id} taxi to {op.get('to') or ('runway ' + f.data.get('dep_rwy', ''))}" + (f" via {' '.join(via)}" if via else "")


def _ck_hold_pos(w, f, op):
    if f.id not in w.movers:
        raise Refusal("not taxiing")


def _ap_hold_pos(w, f, op):
    m = w.movers[f.id]
    m.stop_at = m.i
    return f"{f.id} holding position"


def _ck_pushback(w, f, op):
    if f.kind != "DEP" or f.phase != "ready":
        raise Refusal(f"{f.id} is not ready for pushback (phase {f.phase})")
    if w.ramp_closed:
        raise Refusal("ramp operations suspended (lightning), ground crew not available")


def _ap_pushback(w, f, op):
    if w.ramp_closed:
        raise Refusal("ramp closed, no ground crew")
    sid = w.parked.pop(f.id, None) or f.stand
    if sid and w.stand_occ.get(sid) == f.id:
        w.stand_occ.pop(sid, None)
    f.phase = "pushed"
    f.data["offblock_t"] = w.t
    if w.dflow.get(f.id, -1e9) < w.t + DFLOW_TAXI_EST_S - 4 * 60:
        w.dflow_slot(f, w.t + DFLOW_TAXI_EST_S - 3 * 60)   # started late (ramp closed, crew, stand): DFLOW plans it again
    dur = w.rng("push", f.id).uniform(*PUSH_S)
    node = w.ground.stands[sid]["node"] if sid in w.ground.stands else None
    m = Mover([node, node])
    m.i = 0
    m.stop_at = 0
    w.movers[f.id] = m
    f.data["push_done_t"] = w.t + dur
    then = op.get("then")
    f.data["then_after_push"] = then
    return f"{f.id} pushback and start-up approved"


def _stand_fits(w, f, sid):
    """Refuse a stand the aircraft may not use: too small, A380-only, or closed."""
    s = w.ground.stands[sid]
    order = "ABCDEFG"
    if order.index(s["code"]) < order.index(f.code):
        raise Refusal(f"stand {sid} is code {s['code']}, too small for a {f.type} (code {f.code})")
    if sid in A380_ONLY_STANDS and f.code != "F":
        raise Refusal(f"stand {sid} is for the A380 only")
    if sid in w.ground.closed_stands:
        raise Refusal("stand closed")


def _sys_assign_stand(w, seat, op):
    _need_any(w, seat, ("GND", "GND_2"))
    f = w.flights.get(op.get("flight"))
    sid = op.get("stand")
    if f is None or f.kind != "ARR" or f.outcome in ("diverted", "cancelled", "crashed") or f.phase == "on_stand":
        raise Refusal("flight must be an arrival not yet on stand")
    if sid not in w.ground.stands:
        raise Refusal("unknown stand")
    _stand_fits(w, f, sid)
    occ = w.stand_occ.get(sid)
    if occ is not None and occ != f.id and not (occ in w.flights and w.flights[occ].kind == "DEP" and w.flights[occ].phase in ("pushed", "taxi_out")):
        # Booking a stand that is still occupied is allowed: the aircraft will wait at the entrance until it is free.
        f.data["stand_wait"] = occ
    old = f.data.get("stand")
    if old and w.stand_occ.get(old) == f.id:
        w.stand_occ.pop(old, None)
    f.data["stand"] = sid
    f.stand = sid
    if occ is None:
        w.stand_occ[sid] = f.id
    return {"ok": True, "flight": f.id, "stand": sid, "occupied_by": occ}


def _sys_edct(w, seat, op):
    _need_sector(w, seat, "FLOW")
    f = w.flights.get(op.get("flight"))
    if f is None or f.kind != "ARR" or f.origin_dep_t is None:
        raise Refusal("ground delays apply only to arrivals that have a departure time at their origin (short and medium haul)")
    if w.t >= f.origin_dep_t + (f.edct - f.origin_dep_t if f.edct else 0) - 600:
        raise Refusal(f"{f.id} is already airborne or about to depart from {f.other}")
    t = float(op.get("time"))
    if t < f.origin_dep_t:
        raise Refusal("an EDCT cannot be earlier than the scheduled departure")
    if t < w.t:
        raise Refusal(f"an EDCT cannot be in the past (now {w.clock(w.t)[:5]}Z)")
    f.edct = t
    return {"ok": True, "flight": f.id, "edct": t, "new_eta_entry": f.eta_entry + (t - f.origin_dep_t)}


def _sys_mit(w, seat, op):
    _need_sector(w, seat, "FLOW")
    e = op.get("entry")
    nm = op.get("nm")
    if e not in AP.ENTRY or not isinstance(nm, (int, float)) or not 5 <= nm <= 60:
        raise Refusal("entry must be VUTEB, IMPED or ITLAP and nm 5 to 60")
    w.mit[e] = float(nm)
    w.emit("flow", text=f"Miles in trail at {e}: {nm} NM")
    return {"ok": True, "entry": e, "nm": nm}


def _sys_runway_config(w, seat, op):
    _need_sector(w, seat, "SUP")
    d = op.get("dir")
    mode = op.get("mode", "segregated")
    if d not in AP.CONFIGS:
        raise Refusal("dir must be 30 or 12")
    if mode != "segregated" and not (mode.startswith("single:") and mode.split(":")[1] in AP.RUNWAYS):
        raise Refusal("mode must be segregated or single:<runway>")
    w.config["dir"] = d
    w.config["mode"] = mode
    w.emit("runway_config", text=f"Runway configuration {d}, {mode}: arrivals {w.arr_rwy}, departures {w.dep_rwy}")
    return {"ok": True, "arr": w.arr_rwy, "dep": w.dep_rwy}


def _sys_inspect(w, seat, op):
    """Close a runway for an inspection by airport operations (standing water, debris). SUP."""
    _need_sector(w, seat, "SUP")
    r = op.get("runway")
    if r not in AP.RUNWAYS:
        raise Refusal(f"runway must be one of {', '.join(AP.RUNWAYS)}")
    b = w.body(r)
    if any(c[0] == b and c[2] > w.t and c[3].startswith("inspection") for c in w.closures):
        raise Refusal(f"runway {r} is already being inspected")
    w.closures.append((b, w.t, w.t + INSPECT_S, f"inspection of runway {r}"))
    w.events_plan.append({"t": w.t + INSPECT_S, "kind": "inspection_done", "body": b, "runway": r})
    w.events_plan.sort(key=lambda e: e["t"])
    w.emit("notam", text=f"Runway {r} closed for inspection, about {int(INSPECT_S // 60)} minutes")
    return {"ok": True, "runway": r, "until_t": round(w.t + INSPECT_S)}


def _sys_exit(w, seat, op):
    """Close or reopen a departure exit (the SIDs to one gate), e.g. for a storm on it. DEP_N, DEP_S or SUP."""
    _need_any(w, seat, ("DEP_N", "DEP_S", "SUP"))
    g = op.get("gate")
    if g not in AP.GATES:
        raise Refusal(f"gate must be one of {', '.join(AP.GATES)}")
    if op.get("closed"):
        why = str(op.get("reason") or "weather")[:40]
        w.closed_exits[g] = (w.t, why)
        w.emit("notam", text=f"{g} departure exit closed ({why}): no take-offs to it until reopened")
    elif w.closed_exits.pop(g, None):
        w.emit("notam", text=f"{g} departure exit open again")
    return {"ok": True, "closed_exits": sorted(w.closed_exits)}


EXIT_NEXT = {"NORTH": ("EAST", "WEST"), "EAST": ("NORTH", "SOUTH"), "SOUTH": ("EAST", "WEST"), "WEST": ("NORTH", "SOUTH")}


def _sys_reroute(w, seat, op):
    """A revised departure clearance to a neighbouring exit (by datalink, at the stand or on the ground). DEL, GND or GND_2."""
    _need_any(w, seat, ("DEL", "GND", "GND_2"))
    f = w.flights.get(op.get("flight"))
    if f is None or f.kind != "DEP" or f.phase not in ("not_ready", "ready", "pushed", "taxi_out", "holding"):
        raise Refusal("a departure not yet lined up")
    g = op.get("gate")
    if g not in EXIT_NEXT.get(f.gate or "WEST", ()):
        raise Refusal(f"{f.id} can be rerouted only to {' or '.join(EXIT_NEXT.get(f.gate or 'WEST', ()))}")
    f.data.setdefault("planned_gate", f.gate)
    f.gate = g
    return {"ok": True, "flight": f.id, "gate": g}


def _sys_lvp(w, seat, op):
    _need_sector(w, seat, "SUP")
    on = bool(op.get("on"))
    if on and w.lvp["state"] == "off":
        w.lvp = {"state": "prep", "since": w.t}
        w.emit("lvp", text="LVP preparation started; in force in 10 minutes")
    elif not on:
        w.lvp = {"state": "off", "since": w.t}
        w.emit("lvp", text="LVP cancelled")
    return {"ok": True, "lvp": w.lvp["state"]}


def _sys_tow(w, seat, op):
    _need_any(w, seat, ("GND", "GND_2"))
    f = w.flights.get(op.get("flight"))
    sid = op.get("stand")
    if f is None or f.id not in w.parked:
        raise Refusal("flight must be parked on a stand")
    if w.ramp_closed:
        raise Refusal("ramp closed")
    if sid not in w.ground.stands or sid in w.stand_occ:
        raise Refusal("target stand unknown or occupied")
    _stand_fits(w, f, sid)
    old = w.parked[f.id]
    # The aircraft leaves its old stand when the tow ends, 20 minutes from now.
    w.events_plan.append({"t": w.t + 20 * 60, "kind": "tow_done", "stand": old, "flight": f.id})
    w.events_plan.sort(key=lambda e: e["t"])
    w.parked[f.id] = sid
    w.stand_occ[sid] = f.id
    f.stand = sid
    if f.tobt is not None:
        f.data["tobt_floor"] = max(f.data.get("tobt_floor", -1e9), w.t + 20 * 60)
        f.tobt = max(f.tobt, w.t + 20 * 60)
    return {"ok": True, "flight": f.id, "from": old, "to": sid, "ready_after": w.t + 20 * 60}


def _sys_open(w, seat, op):
    if seat is None:
        raise Refusal("seats only")
    want = op.get("sectors") or []
    for s in want:
        if s not in SECTORS:
            raise Refusal(f"unknown sector {s}")
        if w.sectors.owner[s] not in (None, seat):
            raise Refusal(f"{s} is worked by {w.sectors.owner[s]}; they must close it first")
    for s in want:
        w.sectors.owner[s] = seat
        # Aircraft already in the sector stay with it: they now talk on the sector's own frequency.
        w.emit("sector", text=f"{s} opened by {seat}", sector=s, seat=seat)
    # A crew still waiting for an answer calls again on the newly opened frequency.
    for f in w.flights.values():
        u = f.data.get("waiting_call")
        if u and u[1] in want and f.sector == u[1] and f.phase == u[3] and (f.kind == "DEP" or f.data.get("crossing_junction")):
            w.pilot_call(f, u[0], u[1], u[2])
    return {"ok": True, "sectors": w.sectors.sectors_of(seat)}


def _sys_close(w, seat, op):
    want = op.get("sectors") or []
    for s in want:
        if w.sectors.owner.get(s) != seat:
            raise Refusal(f"you do not work {s}")
        w.sectors.owner[s] = None
        w.emit("sector", text=f"{s} closed by {seat}; it is unstaffed", sector=s, seat=None)
    return {"ok": True, "sectors": w.sectors.sectors_of(seat)}


def _need_any(w, seat, sectors):
    """The seat works one of these sectors (the stand-in, seat None, acts only while none of them is staffed)."""
    owners = {w.sectors.owner.get(x) for x in sectors}
    if seat is None:
        if owners - {None}:
            raise Refusal(f"{'/'.join(sectors)} is staffed")
        return
    if seat not in owners:
        raise Refusal(f"this needs the {' or '.join(sectors)} sector, which you do not work")


def _need_sector(w, seat, sector):
    owner = w.sectors.owner.get(sector)
    if seat is None:
        if owner is not None:
            raise Refusal(f"{sector} is staffed")
        return
    if owner != seat:
        raise Refusal(f"this needs the {sector} sector, which you do not work")


def _ck_say_again(w, f, op):
    if not f.data.get("last_call"):
        raise Refusal(f"{f.id} has made no call to repeat")


def _ap_say_again(w, f, op):
    kind, text = f.data["last_call"]
    w.pilot_call(f, kind, f.sector, text, front=True)
    return None         # no readback: the crew repeats its last call instead


RADIO_OPS = {
    "heading": ({"ARR", "DEP"}, _ck_heading, _ap_heading),
    "altitude": ({"ARR", "DEP"}, _ck_alt, _ap_alt),
    "speed": ({"ARR", "DEP"}, _ck_speed, _ap_speed),
    "deviation": ({"ARR", "DEP"}, _ck_deviation, _ap_deviation),
    "direct": ({"ARR", "DEP"}, _ck_direct, _ap_direct),
    "route": ({"ARR"}, _ck_route, _ap_route),
    "hold": ({"ARR"}, _ck_hold, _ap_hold),
    "approach": ({"ARR"}, _ck_approach, _ap_approach),
    "eat": ({"ARR"}, _ck_eat, _ap_eat),
    "divert": ({"ARR"}, _ck_divert, _ap_divert),
    "handoff": ({"ARR", "TWR", "GND", "DEP"}, _ck_handoff, _ap_handoff),
    "land": ({"TWR"}, _ck_land, _ap_land),
    "go_around": ({"TWR", "ARR"}, _ck_go_around, _ap_go_around),
    "lineup": ({"TWR"}, _ck_lineup, _ap_lineup),
    "takeoff": ({"TWR"}, _ck_takeoff, _ap_takeoff),
    "cancel_takeoff": ({"TWR"}, _ck_cancel_to, _ap_cancel_to),
    "cross": ({"TWR"}, _ck_cross, _ap_cross),
    "taxi": ({"GND", "TWR"}, _ck_taxi, _ap_taxi),
    "hold_position": ({"GND", "TWR"}, _ck_hold_pos, _ap_hold_pos),
    "pushback": ({"GND"}, _ck_pushback, _ap_pushback),
    "say_again": ({"ARR", "TWR", "GND", "DEP"}, _ck_say_again, _ap_say_again),
}
SYSTEM_OPS = {
    "assign_stand": _sys_assign_stand, "edct": _sys_edct, "mit": _sys_mit, "runway_config": _sys_runway_config,
    "lvp": _sys_lvp, "tow": _sys_tow, "exit": _sys_exit, "reroute": _sys_reroute, "inspect": _sys_inspect, "open": _sys_open, "close": _sys_close,
}
