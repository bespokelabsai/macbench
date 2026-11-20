"""Sectors, frequencies and radio time.

Each controller position (sector) has its own frequency. A seat (one agent) can open several sectors and works
each on that sector's own frequency, so it can transmit on all of them at once. A sector that no seat has open has no controller: its aircraft get no instructions
and no clearances, so they wait (on the ground) or hold and finally divert (in the air). A frequency carries one transmission at a time: an instruction and its readback, or a pilot's call.
Every transmission takes radio time, so one frequency can carry only so much traffic.
"""
import collections

SECTORS = {
    # name: (kind, description)
    # The positions of UAE AIP OMDB AD 2.18, with their real frequencies.
    "ARR": ("ARR", "Dubai Arrivals (124.900): all arrivals, from the entry fix towards the final approach"),
    "DIR": ("ARR", "Dubai Director (127.900, opened on request): arrivals handed over by ARR, sequenced onto the ILS"),
    "TWR_N": ("TWR", "Dubai Tower, Air North (118.750): runway 12L/30R"),
    "TWR_S": ("TWR", "Dubai Tower, Air South (119.550): runway 12R/30L"),
    "GND": ("GND", "Dubai Ground, GMC-1 (118.350): start-up, pushback and taxiing, except the GND_2 area"),
    "GND_2": ("GND", "Dubai Ground, GMC-2 (121.650): start-up, pushback and taxiing for taxilane P and aprons C, E, Q and S"),
    "DEL": ("DEL", "Dubai Delivery (120.350): route clearances, by datalink (no radio work): departure reroutes"),
    "DEP_N": ("DEP", "Dubai Departures North (124.675): departures to the NORTH and EAST exits, from take-off to the exit"),
    "DEP_S": ("DEP", "Dubai Departures South (121.025): departures to the SOUTH and WEST exits, from take-off to the exit"),
    "FLOW": ("FLOW", "Flow management: ground delays for flights not yet airborne, miles in trail at the entry fixes"),
    "SUP": ("SUP", "Supervisor: runway configuration, low visibility procedures, runway inspections"),
}
RADIO_KINDS = {"ARR", "TWR", "GND", "DEP"}
GND2_APRONS = ("C", "E", "Q", "S")     # AD 2.18: aircraft on these aprons call GMC-2

# Radio time in seconds of sim time: the controller's transmission, then the pilot's readback.
AIRTIME = {
    "heading": (4.0, 3.5), "altitude": (4.0, 3.5), "speed": (3.5, 3.0), "direct": (4.0, 3.5), "hold": (6.5, 5.0),
    "route": (6.0, 5.0), "approach": (5.5, 4.5), "eat": (5.0, 3.5), "handoff": (3.5, 3.0), "divert": (5.0, 4.5), "deviation": (4.0, 4.0),
    "land": (4.5, 3.5), "go_around": (3.5, 3.0), "lineup": (4.0, 3.5), "takeoff": (5.0, 4.0), "cross": (4.5, 4.0),
    "taxi": (6.0, 5.0), "hold_position": (3.0, 2.5), "pushback": (4.0, 3.0), "climb": (4.0, 3.5), "cancel_takeoff": (3.5, 3.0),
    "continue": (3.0, 2.5), "say_again": (2.5, 0.0), "traffic": (5.0, 3.0), "exit": (3.0, 2.5),
}
PILOT_CALL = {"checkin": 4.5, "request": 5.0, "unable": 4.0, "readback_wrong": 4.5, "minimum_fuel": 5.0, "mayday": 6.0,
              "go_around": 3.5, "ready_pushback": 4.0, "ready_taxi": 4.0, "holding_short": 3.5, "vacated": 3.0,
              "windshear": 4.5, "deviation": 5.0, "deviation_request": 5.5, "diverting": 5.0, "tcas_ra": 3.5, "clear_of_conflict": 4.5}
MAX_BACKLOG_S = 40.0    # a controller cannot queue more than this much radio time; further instructions are refused


class Transmission:
    __slots__ = ("who", "flight", "kind", "text", "dur", "on_end", "op", "seat", "t_start", "t_end", "id")

    def __init__(self, who, flight, kind, text, dur, on_end=None, op=None, seat=None):
        self.who, self.flight, self.kind, self.text, self.dur = who, flight, kind, text, dur
        self.on_end, self.op, self.seat = on_end, op, seat
        self.t_start = self.t_end = None
        self.id = None


class Frequency:
    def __init__(self, name, owner):
        self.name = name
        self.owner = owner          # the sector ("sector:<S>"), or "unstaffed:<sector>"
        self.queue = collections.deque()
        self.current = None
        self.busy_s = 0.0           # radio time used, for the load figure
        self.log = collections.deque(maxlen=400)

    def backlog(self, now):
        s = sum(t.dur for t in self.queue)
        if self.current is not None:
            s += max(0.0, self.current.t_end - now)
        return s

    def push(self, tx, front=False):
        (self.queue.appendleft if front else self.queue.append)(tx)

    def step(self, now, dt, done):
        """Advance the frequency; call done(tx) for each transmission that ends."""
        if self.current is not None and now >= self.current.t_end:
            tx, self.current = self.current, None  # free the frequency first: a failing done() must not hold it
            done(tx, self)
        if self.current is None and self.queue:
            tx = self.queue.popleft()
            tx.t_start, tx.t_end = now, now + tx.dur
            self.current = tx
            self.busy_s += tx.dur
            self.log.append(tx)


class Sectors:
    """Who works each sector, and which frequency each sector is on."""

    def __init__(self):
        self.owner = {s: None for s in SECTORS}          # seat or None (unstaffed)
        self.freqs = {}                                  # frequency name -> Frequency
        for s in SECTORS:
            if SECTORS[s][0] in RADIO_KINDS:
                self.freqs["unstaffed:" + s] = Frequency("unstaffed:" + s, "unstaffed:" + s)

    def freq_of(self, sector):
        """The frequency a sector's aircraft talk on: the sector's own frequency while a seat works it, else the
        unstaffed one (a pilot's call there goes unanswered)."""
        if self.owner.get(sector) is None:
            return self.freqs.get("unstaffed:" + sector)
        key = "sector:" + sector
        if key not in self.freqs:
            self.freqs[key] = Frequency(key, key)
        return self.freqs[key]

    def scope_of(self, fr):
        """The event scope of a frequency: "seat:<seat>" for a staffed sector's frequency, else the frequency's own
        name (its transmissions reach no seat)."""
        if fr.name.startswith("sector:"):
            seat = self.owner.get(fr.name[7:])
            if seat is not None:
                return "seat:" + seat
        return fr.name

    def seat_freqs(self, seat):
        """The frequencies of the sectors a seat works, by sector."""
        return {s: self.freq_of(s) for s in self.sectors_of(seat) if SECTORS[s][0] in RADIO_KINDS}

    def sectors_of(self, seat):
        return [s for s, o in self.owner.items() if o == seat]

    def kind(self, sector):
        return SECTORS[sector][0]
