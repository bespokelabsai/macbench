"""Dubai International (OMDB): runways, airspace and procedures.

Runway data is from the UAE AIP (AD 2 OMDB, AIRAC 08/2026). The entry holds VUTEB, IMPED and ITLAP are the real
en-route holds (UAE AIP ENR 3.6). The terminal procedures (downwind trombones, feeder points, terminal holds, SID exit
gates) are a simplified version of the real ones: their shape follows the AIP (downwind legs parallel to final, base
over the sea on runway 12, initial climb to 4,000 ft) but the waypoint names and positions are our own. See
docs/AIRPORT.md for the chart.
"""
import json
import math
import os

from .geo import Local, dest, dist_nm, bearing

HERE = os.path.dirname(os.path.abspath(__file__))
ARP = (25.252778, 55.364444)  # aerodrome reference point
LOCAL = Local(ARP)
ELEV_FT = 62


def dms(d, m, s):
    return d + m / 60.0 + s / 3600.0


class Runway:
    def __init__(self, name, thr, heading, length_m, lda_m, displaced_m, opposite, ils_cat, exits, entries):
        self.name, self.thr, self.heading, self.length_m, self.lda_m = name, thr, heading, length_m, lda_m
        self.displaced_m, self.opposite, self.ils_cat = displaced_m, opposite, ils_cat
        self.exits = exits        # [(name, distance from threshold in m, rapid?)]
        self.entries = entries    # [(name, distance from threshold in m, full_length?)]

    def point_on_final(self, d_nm):
        """A point d_nm before the threshold on the extended centreline."""
        return dest(self.thr, (self.heading + 180.0) % 360.0, d_nm)

    def along(self, d_m):
        return dest(self.thr, self.heading, d_m / 1852.0)


# Thresholds (AIP AD 2.12). Exits are the AIP rapid exits (AD 2.20.5); distances from the threshold in metres.
RUNWAYS = {
    "12L": Runway("12L", (dms(25, 15, 52.66), dms(55, 21, 1.52)), 121.0, 4351, 3600, 450, "30R", "III",
                  exits=[("N5A", 1825, True), ("N6", 2143, True), ("N8", 2759, True), ("N11", 3300, False), ("N12", 3600, False)],
                  entries=[("M1C", 0, True), ("N1C", 0, True), ("N3", 1000, False)]),
    "30R": Runway("30R", (dms(25, 14, 51.75), dms(55, 22, 51.35)), 301.0, 4351, 4000, 300, "12L", "III",
                  exits=[("N5", 1775, True), ("N3A", 2351, True), ("N3", 2745, True), ("N1C", 3600, False), ("N1A", 4000, False)],
                  entries=[("M15", 0, True), ("N12", 0, True), ("M13", 450, True), ("N8A", 900, False), ("N7", 1300, False)]),
    "12R": Runway("12R", (dms(25, 15, 10.21), dms(55, 21, 51.75)), 121.0, 4447, 3600, 715, "30L", "III",
                  exits=[("K13", 2150, True), ("K15", 2800, True), ("K17", 3300, False), ("K18", 3600, False)],
                  entries=[("K1", 0, True), ("K2", 0, True), ("K5", 300, True), ("K7", 1200, False)]),
    "30L": Runway("30L", (dms(25, 14, 9.31), dms(55, 23, 41.57)), 301.0, 4447, 4315, 132, "12R", "III",
                  exits=[("K9", 2029, True), ("K8", 2390, True), ("K6", 2800, True), ("K3", 3600, False), ("K1", 4100, False)],
                  entries=[("K15A", 0, True), ("M18A", 0, True), ("K13", 900, False), ("K12", 1300, False)]),
}

# Runway configurations. Segregated mode is the norm (AIP AD 2.23.9): one runway for arrivals, the parallel one for
# departures. During the daily runway maintenance, or if a runway closes, the other runway works in mixed mode.
CONFIGS = {
    "30": {"arr": "30L", "dep": "30R"},
    "12": {"arr": "12L", "dep": "12R"},
}
SINGLE = {"30": {"south": "30R", "north": "30L"}, "12": {"south": "12L", "north": "12R"}}  # the runway left when one closes
RUNWAY_BODY = {"12L": "N", "30R": "N", "12R": "S", "30L": "S"}  # north runway 12L/30R, south runway 12R/30L

# -- airspace ----------------------------------------------------------------------------------------------------
# Entry fixes: the three real en-route holds. Inbound tracks and turns from ENR 3.6.
ENTRY = {
    "VUTEB": {"pos": (dms(25, 36, 45), dms(54, 51, 49)), "inbound": 107.0, "turn": "R"},
    "IMPED": {"pos": (dms(24, 58, 24), dms(56, 4, 6)), "inbound": 298.0, "turn": "R"},
    "ITLAP": {"pos": (dms(25, 49, 25), dms(55, 50, 10)), "inbound": 204.0, "turn": "L"},
}
TMA_RADIUS_NM = 60.0
DOWNWIND_OFFSET_NM = 7.0
DOWNWIND_STEP_NM = 2.0
DOWNWIND_POINTS = 9          # trombone points 1..9, from 4 NM to 20 NM beyond the threshold abeam
FINAL_JOIN_MIN_NM = 8.0      # base turns aim for the final at least this far out


def _side_brg(rwy, side):
    """Bearing from the final course to a side of it: N is the side to the north of the final, S the side to the south."""
    recip = (rwy.heading + 180.0) % 360.0
    a, b = (recip - 90.0) % 360.0, (recip + 90.0) % 360.0
    north = a if math.cos(math.radians(a)) > math.cos(math.radians(b)) else b
    return north if side == "N" else (north + 180.0) % 360.0


APPROACH_END_ENTRIES = {"30L": ("IMPED",), "12L": ("VUTEB",)}


def build_procedures():
    """Fixes, trombones, feeders and holds for each arrival runway. Returns a dict of named fixes and the
    procedure tables."""
    fixes = {k: v["pos"] for k, v in ENTRY.items()}
    proc = {}
    for rname in ("30L", "12L"):
        rwy = RUNWAYS[rname]
        recip = (rwy.heading + 180.0) % 360.0
        tag = rname[:2]
        sides = {}
        for side in ("N", "S"):
            sb = _side_brg(rwy, side)
            pts = []
            for k in range(1, DOWNWIND_POINTS + 1):
                along = 4.0 + DOWNWIND_STEP_NM * (k - 1)
                on_final = rwy.point_on_final(along)
                p = dest(on_final, sb, DOWNWIND_OFFSET_NM)
                name = f"D{tag}{side}{k}"
                fixes[name] = p
                pts.append(name)
            # Base point for each downwind point: where a base turn from it meets the final, 45 deg intercept.
            bases = {}
            for k, name in enumerate(pts, start=1):
                along = 4.0 + DOWNWIND_STEP_NM * (k - 1)
                join = max(FINAL_JOIN_MIN_NM, along + DOWNWIND_OFFSET_NM)
                bases[name] = round(join, 1)
            # Feeder: the point where traffic joins the downwind, abeam the threshold.
            feeder = dest(rwy.point_on_final(-2.0), sb, DOWNWIND_OFFSET_NM + 3.0)
            fname = f"F{tag}{side}"
            fixes[fname] = feeder
            # Terminal hold at the feeder (1 min legs) on the downwind course, turning away from the final.
            away_right = abs(((sb - (recip + 90.0)) + 180.0) % 360.0 - 180.0) < 1.0
            sides[side] = {"downwind": pts, "base_join_nm": bases, "feeder": fname,
                           "hold": {"fix": fname, "inbound": recip, "turn": "R" if away_right else "L", "leg_min": 1.0}}
        # Final: IF at 12 NM, FAF at 6 NM on a 3 degree path; and a point on the centreline at every NM from 5 to 25
        # ("30F09" is 9 NM from the 30L threshold), to aim base turns at.
        for k in range(5, 26):
            fixes[f"{tag}F{k:02d}"] = rwy.point_on_final(float(k))
        fixes[f"IF{tag}"] = rwy.point_on_final(12.0)
        fixes[f"FF{tag}"] = rwy.point_on_final(6.0)
        # Missed approach: straight ahead to 1,500 ft, then left (30) or right (12) to the missed approach hold at
        # 3,000 ft, 16 NM beyond the threshold and 7 NM off the departure climb-out. Departures pass it level at
        # 4,000 ft (the SID altitude), 1,000 ft above.
        mah, mah2 = ("EGNOT", "ADVAX") if tag == "30" else ("TALTU", "RIMOL")
        turn_side = (rwy.heading + (-90 if tag == "30" else 90)) % 360
        fixes[mah] = dest(dest(rwy.thr, rwy.heading, 16.0), turn_side, 7.0)
        # A second missed approach hold for a second go-around while the first is in use: 12 NM beyond the threshold
        # and 16 NM to the side, so the way there and its pattern stay clear of the first hold.
        fixes[mah2] = dest(dest(rwy.thr, rwy.heading, 12.0), turn_side, 16.0)
        # Arrivals from the approach end (IMPED for 30, VUTEB for 12) would cross a downwind on the way to its feeder.
        # They route via a point beyond the far end of that downwind and outside it (X30S, X12N, X12S), then along the
        # outside of the downwind to the feeder.
        for side in ("N", "S"):
            xname = f"X{tag}{side}"
            fixes[xname] = dest(rwy.point_on_final(26.0), _side_brg(rwy, side), 13.0)
            sides[side]["via"] = {e: [xname] for e in APPROACH_END_ENTRIES[rname]}
        proc[rname] = {"sides": sides, "if": f"IF{tag}", "faf": f"FF{tag}", "missed_hold": mah, "missed_hold2": mah2}
    # Which downwind each entry uses, per arrival runway.
    # "B": both sides, balanced by Center (VUTEB carries more than half of the arrivals).
    flows = {"30L": {"VUTEB": "B", "ITLAP": "N", "IMPED": "S"}, "12L": {"VUTEB": "B", "ITLAP": "N", "IMPED": "N"}}
    return fixes, proc, flows


FIXES, PROC, FLOWS = build_procedures()


def proc_for(rw):
    """The terminal procedures for an arrival runway. When one runway is closed the arrivals use the parallel runway
    of the same direction (30R or 12R) with the same procedures."""
    return PROC["12L" if str(rw).startswith("12") else "30L"]


def flows_for(rw):
    return FLOWS["12L" if str(rw).startswith("12") else "30L"]

# Entry holds, with 1 min legs.
HOLDS = {name: {"fix": name, "inbound": v["inbound"], "turn": v["turn"], "leg_min": 1.0}
         for name, v in ENTRY.items()}
for r in PROC.values():
    for s in r["sides"].values():
        HOLDS[s["hold"]["fix"]] = dict(s["hold"])
    for k in ("missed_hold", "missed_hold2"):
        HOLDS[r[k]] = {"fix": r[k], "inbound": None, "turn": "R", "leg_min": 1.0}
for mh, rw in (("EGNOT", "30L"), ("ADVAX", "30L"), ("TALTU", "12L"), ("RIMOL", "12L")):
    HOLDS[mh]["inbound"] = RUNWAYS[rw].heading

# SID exit gates. Four gates by direction, 35 NM out. Departures climb to 4,000 ft on the SID until DEP clears them
# higher. Departures to the same gate need 2 minutes (or 5 NM); diverging gates (45 deg or more) 1 minute.
GATES = {
    "NORTH": dest(ARP, 10.0, 35.0),
    "EAST": dest(ARP, 80.0, 35.0),
    "SOUTH": dest(ARP, 170.0, 35.0),
    "WEST": dest(ARP, 280.0, 35.0),
}
FIXES.update({f"G{k[0]}": v for k, v in GATES.items()})
# SID turn points: 8 NM straight out from the departure runways, so departures do not turn back over the arrivals.
FIXES["S30"] = dest(RUNWAYS["30R"].thr, 301.0, 8.0)
FIXES["S12"] = dest(RUNWAYS["12R"].thr, 121.0, 8.0)
# Departures to the gate on the far side of a downwind (GE from runway 30, GW from runway 12) would cross that
# downwind's near end at 4,000 ft, where arrivals descend from the stack. They fly via a point 25 NM out instead,
# which keeps them clear of the downwind.
FIXES["E30"] = dest(ARP, 15.0, 25.0)
FIXES["W12"] = dest(ARP, 195.0, 25.0)
SID_VIA = {("30", "EAST"): ["E30"], ("12", "WEST"): ["W12"]}
SID_TURN_ALT_FT = 1500   # straight ahead to 1,500 ft, then turn toward the gate


def gate_for_bearing(brg):
    """The SID gate for a destination at this bearing from DXB."""
    if brg < 45 or brg >= 330:
        return "NORTH"
    if brg < 125:
        return "EAST"
    if brg < 225:
        return "SOUTH"
    return "WEST"


def entry_for_bearing(brg):
    """The entry fix for an arrival coming from this bearing (origin bearing from DXB)."""
    if 255 <= brg or brg < 15:
        return "VUTEB" if brg >= 255 and brg < 345 else "ITLAP"
    if brg < 95:
        return "ITLAP"
    return "IMPED"


# Alternates. Free stands for diversions are an estimate (the declared stands minus the alternate's own traffic).
ALTERNATES = {
    "OMDW": {"name": "Al Maktoum (DWC)", "pos": (24.896, 55.161), "ils": "III", "free_stands": 30, "a380": True},
    "OMSJ": {"name": "Sharjah", "pos": (25.3286, 55.5172), "ils": "I", "free_stands": 12, "a380": False},
    "OMAA": {"name": "Abu Dhabi", "pos": (24.4330, 54.6511), "ils": "III", "free_stands": 20, "a380": True},
    "OMRK": {"name": "Ras Al Khaimah", "pos": (25.6135, 55.9388), "ils": "I", "free_stands": 6, "a380": False},
    "OOMS": {"name": "Muscat", "pos": (23.5933, 58.2844), "ils": "II", "free_stands": 25, "a380": True},
    # Far alternates: room for any number of diversions, but they need much more fuel to reach.
    "OTHH": {"name": "Doha", "pos": (25.2731, 51.6081), "ils": "III", "free_stands": 999, "a380": True},
    "OBBI": {"name": "Bahrain", "pos": (26.2708, 50.6336), "ils": "II", "free_stands": 999, "a380": True},
}
for code, a in ALTERNATES.items():
    a["dist_nm"] = round(dist_nm(ARP, a["pos"]), 1)


def final_fix_along(name):
    """Distance from the threshold of a final-approach point such as 30F09, or None."""
    if len(name) == 5 and name[2] == "F" and name[:2] in ("30", "12") and name[3:].isdigit():
        return float(name[3:])
    if name in ("IF30", "IF12"):
        return 12.0
    return None


def track_miles(pos, route, rwy):
    """Distance to fly to the threshold of rwy along route (a list of fix names), ending with the final."""
    from .geo import dist_nm as _d
    total = 0.0
    p = pos
    last_along = None
    for fx in route:
        q = FIXES[fx]
        total += _d(p, q)
        p = q
        last_along = final_fix_along(fx)
    if last_along is not None:
        return total + last_along
    return total + _d(p, RUNWAYS[rwy].point_on_final(10.0)) + 10.0


def load_ground(path=None):
    with open(path or os.path.join(HERE, "omdb_ground.json")) as f:
        return json.load(f)


def chart():
    """All named points with lat/lon, for docs and viewers."""
    return {k: [round(v[0], 6), round(v[1], 6)] for k, v in sorted(FIXES.items())}
