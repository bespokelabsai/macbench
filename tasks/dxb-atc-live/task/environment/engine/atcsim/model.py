"""Aircraft types, wake turbulence separation, approach minima, and the Flight record."""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "types.json")) as f:
    TYPES = json.load(f)
# OpenAP has no data for these; values from the type certificates and ICAO Doc 8643 groupings.
TYPES.setdefault("A310", {})
TYPES["A310"].update({"group": "C", "code": "D", "mtow_kg": 164000, "span_m": 43.9, "vapp_kt": 135, "vland_kt": 132,
                      "vto_kt": 160, "hold_kg_min": 70.0, "seats": 0, "cargo": True, "engines": 2})
TYPES["A124"].update({"group": "B", "code": "F", "span_m": 73.3})
DEFAULT_TYPE = {"N": "B738", "W": "B77W"}


def type_info(code):
    if code in TYPES:
        return code, TYPES[code]
    return DEFAULT_TYPE["N"], TYPES[DEFAULT_TYPE["N"]]


# ICAO Doc 4444 8.7.3.6 (eWTS, the seven groups used at DXB): distance minima on approach, NM. leader -> follower.
WAKE_NM = {
    "A": {"B": 4, "C": 5, "D": 5, "E": 6, "F": 6, "G": 8},
    "B": {"B": 3, "C": 4, "D": 4, "E": 5, "F": 5, "G": 7},
    "C": {"D": 3, "E": 3.5, "F": 3.5, "G": 6},
    "D": {"G": 4},
    "E": {"G": 4},
}
# Doc 4444 5.8.3.2: time minima for take-off from the same point, seconds. leader -> follower.
WAKE_DEP_S = {
    "A": {"B": 100, "C": 120, "D": 140, "E": 160, "F": 160, "G": 180},
    "B": {"D": 100, "E": 120, "F": 120, "G": 140},
    "C": {"D": 80, "E": 100, "F": 100, "G": 120},
    "D": {"G": 120},
    "E": {"G": 100},
}
# Doc 4444 5.8.3.4: from an intermediate point (or a parallel runway < 760 m away), seconds.
WAKE_DEP_INT_S = {
    "A": {"B": 160, "C": 180, "D": 200, "E": 220, "F": 220, "G": 240},
    "B": {"D": 160, "E": 180, "F": 180, "G": 200},
    "C": {"D": 140, "E": 160, "F": 160, "G": 180},
    "D": {"G": 180},
    "E": {"G": 160},
}
RADAR_MIN_NM = 3.0          # in the TMA
RADAR_MIN_FINAL_NM = 2.5    # same final, inside 10 NM, dry runway, good visibility (Doc 4444 8.7.3.2)
VERT_MIN_FT = 1000


def wake_nm(leader_group, follower_group):
    return WAKE_NM.get(leader_group, {}).get(follower_group, 0.0)


def wake_dep_s(leader_group, follower_group, intermediate=False):
    table = WAKE_DEP_INT_S if intermediate else WAKE_DEP_S
    return table.get(leader_group, {}).get(follower_group, 0.0)


# Approach minima by category: RVR in metres (decision heights are not modelled separately; a ceiling below the
# CAT I decision height of 200 ft also needs CAT II/III).
MINIMA_RVR = {"I": 550, "II": 300, "IIIA": 175, "IIIB": 50}
MINIMA_CEIL = {"I": 200, "II": 100, "IIIA": 0, "IIIB": 0}
CAT_ORDER = ["I", "II", "IIIA", "IIIB"]
# What each operator's aircraft and crews are approved for. Emirates and flydubai operate CAT IIIB at DXB; most large
# network carriers CAT IIIA or better; a few smaller operators only CAT I or II. Unlisted operators: CAT II.
AIRLINE_CAT = {"UAE": "IIIB", "FDB": "IIIB", "QTR": "IIIB", "ETD": "IIIB", "BAW": "IIIB", "DLH": "IIIB", "AFR": "IIIB",
               "KLM": "IIIB", "SIA": "IIIB", "QFA": "IIIB", "CPA": "IIIB", "THY": "IIIA", "SVA": "IIIA", "AIC": "II",
               "IGO": "II", "AXB": "II", "GFA": "IIIA", "OMA": "IIIA", "KAC": "II", "PIA": "I", "ABY": "II", "JZR": "I",
               "SEJ": "II", "RBA": "II", "UZB": "I", "TJK": "I", "KZR": "II", "AFL": "IIIA", "ETH": "II", "KQA": "II",
               "MSR": "II", "RJA": "II", "MEA": "II", "IRA": "I", "IRM": "I", "TAJ": "I", "SXS": "II", "LNI": "I"}
# Reserve crews on standby at DXB for the day (airline estimate). A flight that misses its crew's duty limit needs one.
RESERVE_CREWS = {"UAE": 10, "FDB": 4}
# Minimum turnaround by wake group / code, minutes (Emirates: A380 about 105 min, B777 about 90 min; narrow-body 45).
MIN_TURN_MIN = {"A": 100, "B": 80, "C": 70, "D": 45, "E": 40, "F": 35, "G": 30}


class Flight:
    """One scheduled movement. Arrivals and departures are separate records; a rotation links an arrival to the
    departure the same aircraft flies next."""

    def __init__(self, d):
        self.id = d["id"]                  # callsign, e.g. UAE202
        self.kind = d["kind"]              # ARR or DEP
        self.airline = d["airline"]
        self.type, self.ti = type_info(d.get("type"))
        self.reg = d.get("reg")
        self.other = d.get("other")        # origin for an arrival, destination for a departure
        self.sched = float(d["sched"])     # scheduled landing (ARR) or take-off (DEP) time, sim seconds
        self.pax = int(d.get("pax", self.ti["seats"]))
        self.cat = d.get("cat") or AIRLINE_CAT.get(self.airline, "II")
        self.group = self.ti["group"]
        self.code = self.ti["code"]
        self.src = dict(d)
        self.data = dict(d)
        self.phase = None
        self.sector = None                 # the sector that has the aircraft on frequency
        self.notes = []
        # outcomes
        self.t_done = None                 # landing (ARR) or take-off (DEP) time
        self.outcome = None                # landed, departed, diverted, cancelled, crashed
        self.diverted_to = None
        self.cancel_reason = None
        # arrival
        self.entry = d.get("entry")
        self.origin_dep_t = d.get("origin_dep_t")   # when it takes off at the origin (None if airborne already)
        self.block_min = d.get("block_min")
        self.edct = None
        self.eta_entry = d.get("eta_entry")
        self.fuel_min = d.get("fuel_min")  # minutes of holding fuel above (alternate + final reserve) at entry
        self.alternates = d.get("alternates") or []
        self.next_dep = d.get("next_dep")  # departure id flown next by this aircraft
        # departure
        self.prev_arr = d.get("prev_arr")
        self.stand = d.get("stand")
        self.tobt = None                   # target off-block time (ready time)
        self.gate = d.get("gate")          # SID exit gate
        self.crew_out = d.get("crew_out")  # sim time when the crew's duty limit is reached for take-off
        self.reserve_crew = False

    def to_public(self):
        return {"id": self.id, "kind": self.kind, "airline": self.airline, "type": self.type, "group": self.group,
                "code": self.code, "reg": self.reg, "other": self.other, "sched": self.sched, "pax": self.pax,
                "cat": self.cat, "phase": self.phase, "sector": self.sector}
