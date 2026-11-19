"""How pilots sound on the radio: callsigns, readbacks and calls as text, the way a controller hears them.

A controller hears the airline's radio callsign (ICAO Doc 8585 telephony designator), not the ICAO flight id. Most
readbacks are full, some are partial or only "wilco", and a few instructions are missed and never read back. The
rates are from FAA recordings of busy approach facilities (Prinzo, Hendrix and Hendrix, DOT/FAA/AM-06/25, 2006:
82.7% full readbacks, 7.9% partial, 4.0% acknowledgement only, 13.4% of callsigns without the airline name) and the
earlier FAA en-route study (Cardosi, DOT/FAA/RD-93/11, 1993: 2.7% of clearances had to be repeated).
"""
import re

# ICAO Doc 8585 telephony designators of the operators at Dubai. Operators not listed are called by their three
# letters, as the controller reads them from the flight plan.
TELEPHONY = {
    "UAE": "Emirates", "FDB": "Skydubai", "SVA": "Saudia", "AXB": "Express India", "IGO": "Ifly", "AIC": "Airindia",
    "SEJ": "Spicejet", "OMA": "Oman Air", "GFA": "Gulf Air", "KNE": "Nas Express", "PIA": "Pakistan", "JZR": "Jazeera",
    "THY": "Turkish", "UPS": "UPS", "CPA": "Cathay", "MEA": "Cedar Jet", "BAW": "Speedbird", "CSN": "China Southern",
    "AHY": "Azal", "ETH": "Ethiopian", "MSR": "Egyptair", "IRM": "Mahan Air", "KAC": "Kuwaiti", "ALK": "Srilankan",
    "SWR": "Swiss", "AFR": "Airfrans", "DHX": "Dilmun", "AFL": "Aeroflot", "AUI": "Ukraine International", "FDX": "Fedex",
    "FIN": "Finnair", "KZR": "Astanaline", "NAX": "Nor Shuttle", "CES": "China Eastern", "CCA": "Air China",
    "RJA": "Jordanian", "ABQ": "Pakblue", "OMS": "Mazoon", "IRA": "Iranair", "IAW": "Iraqi", "DLH": "Lufthansa",
    "AFG": "Ariana", "GOW": "Goair", "CEB": "Cebu", "KLM": "KLM", "BBC": "Bangladesh", "THA": "Thai", "PGT": "Sunturk",
    "RNA": "Royal Nepal", "RBA": "Brunei", "KQA": "Kenya", "BOX": "German Cargo", "VJT": "Vista", "UZB": "Uzbek",
    "NJE": "Fraction", "PBD": "Pobeda", "TRA": "Transavia", "SDM": "Russia", "PAL": "Philippine", "SIA": "Singapore",
    "KAL": "Koreanair", "TVS": "Skytravel", "ACA": "Air Canada", "VTI": "Vistara", "SVR": "Sverdlovsk Air",
    "QTR": "Qatari", "ETD": "Etihad", "ABY": "Arabia",
}

P_FULL, P_PARTIAL, P_ACK = 0.827, 0.079, 0.040     # the rest of the answered instructions get a readback with the call sign only
P_MISSED = 0.027                                   # the crew does not hear the instruction: no readback, nothing done
P_SHORT_CALLSIGN = 0.134                           # the crew leaves out the airline name


def split_id(fid):
    m = re.match(r"^([A-Z]{3})([0-9A-Z]+)$", fid or "")
    return (m.group(1), m.group(2)) if m else (None, fid)


def callsign(fid, short=False):
    """The radio callsign of a flight id: UAE202 -> Emirates 202. Short: the number only, as crews often say it."""
    pre, num = split_id(fid)
    if pre is None:
        return fid
    if short:
        return num
    name = TELEPHONY.get(pre)
    return f"{name} {num}" if name else fid


RADIO_NAME = {"ARR": "Dubai Arrivals", "DIR": "Dubai Director", "TWR_N": "Dubai Tower", "TWR_S": "Dubai Tower",
              "GND": "Dubai Ground", "GND_2": "Dubai Ground", "DEP_N": "Dubai Departures", "DEP_S": "Dubai Departures"}
P_WORDS = 0.55          # the crew says the numbers as words ("one three thousand"), as a transcript of real radio reads
P_FILLER = 0.12         # a hesitation ("uh") somewhere in the call
P_UNREADABLE = 0.03     # part of the call is lost (stepped on, clipped); Cardosi 1993: 2.7% of clearances repeated
P_MISHEARD_WORD = 0.04  # one word of the call comes through wrong
DIGIT = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
TENS = {13: "thirteen", 12: "twelve", 11: "eleven", 10: "ten", 14: "fourteen", 15: "fifteen", 16: "sixteen", 17: "seventeen",
        18: "eighteen", 19: "nineteen", 20: "twenty", 21: "twenty one", 22: "twenty two", 25: "twenty five", 30: "thirty"}


def _digits(n, rng):
    return " ".join(("niner" if d == "9" and rng.random() < 0.5 else DIGIT[int(d)]) for d in n)


def _say_num(m, rng):
    n = m.group(0).replace(",", "")
    if not n.isdigit():
        return m.group(0)
    v = int(n)
    if v >= 1000 and v % 100 == 0:
        th, hu = divmod(v, 1000)
        th_s = (TENS.get(th) if rng.random() < 0.5 else None) or _digits(str(th), rng)
        return th_s + " thousand" + (f" {DIGIT[hu // 100]} hundred" if hu else "")
    return _digits(n, rng)


def _alt(v, rng):
    """An altitude as crews say it: 13000 -> 13,000 ft, one three thousand, thirteen thousand."""
    return rng.choice([f"{v:,} ft", f"{v}", f"{v:,}"])


def _paraphrase(text, fid, rng):
    """A real crew does not use one fixed sentence. Re-say the common calls in one of the ways crews say them."""
    m = re.match(r"^(\w+) good evening, (\S+) (\S+), passing (\d+) ft for (\d+), inbound (\w+), fuel endurance (\d+) minutes above diversion fuel$", text)
    if m:
        sec, f, typ, alt, cl, entry, fuel = m.groups()
        rn = RADIO_NAME.get(sec, sec)
        greet = rng.choice(["good evening", "hello", "", "evening"])
        fuel_s = rng.choice([f"fuel endurance {fuel} minutes above diversion", f"we can hold about {fuel} minutes",
                             f"{fuel} minutes holding fuel", f"holding fuel {fuel} minutes", f"endurance {fuel} minutes to our alternate fuel",
                             f"about {fuel} minutes extra fuel"])
        lvl = rng.choice([f"passing {_alt(int(alt), rng)} for {_alt(int(cl), rng)}", f"descending {_alt(int(cl), rng)}",
                          f"leaving {_alt(int(alt), rng)} for {_alt(int(cl), rng)}", f"with you at {_alt(int(alt), rng)}"])
        parts = [rn + (" " + greet if greet else ""), f, rng.choice([typ, ""]), lvl, rng.choice([f"inbound {entry}", f"on the {entry} arrival", f"via {entry}"]), fuel_s]
        return ", ".join(x for x in parts if x)
    m = re.match(r"^(\S+) request deviation (\d+) degrees (left|right) for weather$", text)
    if m:
        return deviation_request(m.group(1), int(m.group(2)), 1 if m.group(3) == "right" else -1, rng)
    m = re.match(r"^(\S+) MINIMUM FUEL, (-?\d+) minutes to diversion fuel$", text)
    if m:
        f, n = m.groups()
        return rng.choice([f"{f} MINIMUM FUEL, {n} minutes to diversion fuel", f"{f}, declaring minimum fuel",
                           f"{f}, minimum fuel, we can accept about {n} more minutes", f"{f}, fuel is getting tight, minimum fuel",
                           f"{f} minimum fuel, {n} minutes then we divert"])
    m = re.match(r"^(\S+) stand (\S+), ready for start-up and pushback$", text)
    if m:
        f, st = m.groups()
        return rng.choice([text, f"{f} stand {st}, ready to push and start", f"{f}, request pushback, stand {st}",
                           f"{f} on {st}, ready for push", f"{f} stand {st}, request start-up"])
    m = re.match(r"^(\w+), (\S+) stand (\S+), ready to taxi$", text)
    if m:
        sec, f, st = m.groups()
        return rng.choice([f"{RADIO_NAME.get(sec, sec)}, {f} ready to taxi", f"{f}, push complete, ready to taxi",
                           f"{RADIO_NAME.get(sec, sec)} {f} stand {st}, request taxi", f"{f} ready for taxi"])
    m = re.match(r"^(\S+) runway vacated(.*)$", text)
    if m:
        f, rest = m.groups()
        return rng.choice([text, f"{f} clear of the runway{rest}", f"{f} vacated{rest}", f"{f}, runway vacated"])
    m = re.match(r"^(\S+) going around, (.*)$", text)
    if m:
        f, why = m.groups()
        return rng.choice([text, f"{f} going around", f"{f}, go around, {why}", f"{f} missed approach, {why}"])
    m = re.match(r"^(\w+), (\S+) established ILS (\S+), (\d+) miles$", text)
    if m:
        sec, f, r, d = m.groups()
        return rng.choice([f"{RADIO_NAME.get(sec, sec)}, {f} established ILS {r}", f"{f} on the localizer {r}, {d} miles",
                           f"{RADIO_NAME.get(sec, sec)} good evening {f} ILS {r}", f"{f}, {d} miles final {r}"])
    m = re.match(r"^(\w+), (\S+) passing 1500 ft climbing 4000, (\w+) departure$", text)
    if m:
        sec, f, g = m.groups()
        return rng.choice([f"{RADIO_NAME.get(sec, sec)}, {f} passing {_alt(1500, rng)} climbing {_alt(4000, rng)}",
                           f"{f} airborne, climbing {_alt(4000, rng)}", f"{RADIO_NAME.get(sec, sec)} {f}, {g.lower()} departure, climbing {_alt(4000, rng)}"])
    m = re.match(r"^(\w+), (\S+) with you$", text)
    if m:
        sec, f = m.groups()
        return rng.choice([f"{RADIO_NAME.get(sec, sec)}, {f} with you", f"{f} checking in", f"{RADIO_NAME.get(sec, sec)} good evening {f}", f"{f}, hello"])
    m = re.match(r"^(\S+) holding short runway (\S+)(, ready)?$", text)
    if m:
        f, r, ready = m.groups()
        return rng.choice([text, f"{f} holding point runway {r}" + (" ready for departure" if ready else ""),
                           f"{f} ready, runway {r}" if ready else f"{f} holding short {r}"])
    return text


def deviation_request(fid, deg, side, rng, again=False):
    """A weather deviation request, in one of the ways crews ask for one."""
    d = "right" if side > 0 else "left"
    if again:
        return rng.choice([f"{fid}, request deviation {deg} degrees {d}, weather ahead", f"{fid} again, we need {deg} {d} for weather",
                           f"{fid}, still requesting {d} deviation for the cell ahead"])
    return rng.choice([f"{fid} request deviation {deg} degrees {d} for weather", f"{fid}, requesting {deg} {d} due weather",
                       f"{fid} would like to deviate {d} about {deg} degrees, cell ahead", f"{fid} request {d} deviation up to {deg} degrees",
                       f"{fid}, we need to go {d} {deg} degrees for the weather", f"{fid}, weather on our nose, request {deg} degrees {d}"])


def _texture(text, rng):
    """Numbers as words, a hesitation, and the occasional word or stretch that does not come through."""
    if rng.random() < P_WORDS:
        text = re.sub(r"\b\d[\d,]*\b", lambda m: _say_num(m, rng), text)
    words = text.split(" ")
    if len(words) > 3 and rng.random() < P_FILLER:
        k = rng.randrange(1, len(words))
        words.insert(k, rng.choice(["uh", "ah", "er"]))
    if len(words) > 4 and rng.random() < P_MISHEARD_WORD:
        k = rng.randrange(2, len(words))
        words[k] = rng.choice(["...", "[garbled]", words[k][: max(1, len(words[k]) // 2)]])
    if len(words) > 4 and rng.random() < P_UNREADABLE:
        a = rng.randrange(2, len(words) - 1)
        b = min(len(words), a + rng.randrange(2, 6))
        words[a:b] = ["[unreadable]"]
    return " ".join(words)


def spoken(text, fid, rng, heavy=None):
    """A sim text as the crew says it: paraphrased, with the callsign as crews say it, and as it comes through."""
    if not fid:
        return text
    text = _paraphrase(text, fid, rng)
    cs = callsign(fid, short=rng.random() < P_SHORT_CALLSIGN)
    if heavy and not cs.isdigit() and rng.random() < 0.7:
        cs += " " + heavy
    return _texture(text.replace(fid, cs, 1).replace(fid, cs), rng)


def readback(core, fid, rng):
    """The crew's reply to an instruction whose clearance text is core (it starts with the flight id)."""
    body = core[len(fid):].strip(" ,") if core.startswith(fid) else core
    cs = callsign(fid, short=rng.random() < P_SHORT_CALLSIGN)
    u = rng.random()
    if u < P_FULL:
        out = rng.choice([f"{body}, {cs}", f"{cs}, {body}", f"{body}, {cs}"])
    elif u < P_FULL + P_PARTIAL:
        nums = re.findall(r"\b\d[\d/]*\b", body)
        out = f"{' '.join(nums[:1]) or body.split(',')[0]}, {cs}"
    elif u < P_FULL + P_PARTIAL + P_ACK:
        out = f"{rng.choice(['wilco', 'roger', 'copied', 'will do'])}, {cs}"
    else:
        out = cs
    return _texture(out, rng)
