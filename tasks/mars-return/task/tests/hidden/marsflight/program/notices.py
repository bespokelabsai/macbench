"""Program office notices: events that change the program's rules during the run.

Ported (marsflight/shocks.py there) onto the current program office. The program office posts
each event on the board while the team works. There are four kinds:
- launch_slip: the launch moves to the next opportunity (LAUNCH_PERIODS);
- hardware_budget_cut: the hardware budget is cut by HARDWARE_CUT;
- rnd_budget_cut: the R&D budget is cut by RND_CUT;
- engine_discontinued: the supplier of one flight-proven catalog engine stops selling it;
- new_requirement: the program adds a requirement (REQUIREMENTS), for example a larger sample mass.

Every run gets the launch slip, which makes the whole team replan, and DRAWN of the others. The kinds, the
times, the engine and the requirement are drawn from the hub secret, so a team cannot prepare a fixed answer.
Times are fractions of the run, because the run's length is set per run.

The whole feature is behind the switch MARSFLIGHT_NOTICES (default off). With the switch off, draw() gives
no events and rules() gives the starting rules, so nothing a team or the verifier sees changes.

rules(events) is the one accessor for the budgets, the catalog engines for sale, the launch period and the
requirements in force. It reads the base values from economics on every call, so those can change freely.
It does not change the score: the scoring code decides how to use the rules in force.

Command line, through the program office's own entry point (the board plugin runs it with the task image's
Python, as it runs quote and run):
    python -m marsflight.program notices < {"secret_file": ...}
    python -m marsflight.program rules   < {"events": [...]}
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import os

SWITCH = "NOTICES"
_TRUE = ("1", "true", "on", "yes", "")
_FALSE = ("0", "false", "off", "no")

KINDS = ("launch_slip", "hardware_budget_cut", "rnd_budget_cut", "engine_discontinued", "new_requirement")
ALWAYS = ("launch_slip",)
DRAWN = 2                 # kinds drawn per run from the others
# When each kind can come, as a fraction of the run. The design gave hours 2, 4 and 5 of an 8 hour run.
WHEN = {"launch_slip": (0.20, 0.40), "hardware_budget_cut": (0.35, 0.70), "rnd_budget_cut": (0.30, 0.65),
        "engine_discontinued": (0.35, 0.70), "new_requirement": (0.30, 0.60)}
HARDWARE_CUT = 0.20
RND_CUT = 0.25
# Launch opportunities (from the data.py). A launch period is the set of days on which the vehicle may launch.
LAUNCH_PERIODS = [
    {"name": "2031", "first_day": "2030-12-01", "last_day": "2031-03-31"},
    {"name": "2033", "first_day": "2033-03-01", "last_day": "2033-06-30"},
    {"name": "2035", "first_day": "2035-05-01", "last_day": "2035-08-31"},
]
# New requirements a notice can add. Each is a named field in rules()["requirements"]. The numbers scale the
# base values in data.py (SAMPLE_MASS_KG, the Utah landing radius), read on every call.
SAMPLE_MASS_GROWTH = 1.2
LANDING_RADIUS_SHRINK = 0.7


class _Requirements(dict):
    """REQUIREMENTS[name] -> {"value", "text"}, built from data.py when asked."""

    def _build(self):
        from .. import data as D
        m = float(D.SAMPLE_MASS_KG)
        rk = float(D.SITES["earth_landing"]["radius_km"])
        return {
            "sample_mass_kg": {"value": round(m * SAMPLE_MASS_GROWTH, 1),
                               "text": f"The returned sample must weigh at least {m * SAMPLE_MASS_GROWTH:.1f} kg "
                                       f"(it was {m:.1f} kg)."},
            "landing_radius_km": {"value": round(rk * LANDING_RADIUS_SHRINK, 1),
                                  "text": f"The capsule must land within {rk * LANDING_RADIUS_SHRINK:.1f} km of the "
                                          f"Utah target (it was {rk:.1f} km)."},
            "planetary_protection": {"value": True,
                                     "text": "The sample container must be sealed, and its outside kept clean of Mars "
                                             "material, before the orbiter captures it."},
        }

    def __getitem__(self, k):
        return self._build()[k]

    def __iter__(self):
        return iter(self._build())

    def __len__(self):
        return len(self._build())

    def __contains__(self, k):
        return k in self._build()

    def keys(self):
        return self._build().keys()


REQUIREMENTS = _Requirements()
TAIL = ("A change stays in force until the program office changes that rule again. program_info lists the rules "
        "in force as rules_in_force. The verifier grades against the rules in force at the end of the run.")


def enabled() -> bool:
    """Whether notices are on: MARSFLIGHT_NOTICES=1 or 0, default off."""
    v = (os.environ.get("MARSFLIGHT_" + SWITCH) or "").strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise ValueError(f"MARSFLIGHT_{SWITCH}={v!r}: use 1 or 0")


def _u(secret: str, label: str) -> float:
    """A uniform number in [0, 1), fixed by the secret and the label."""
    h = hmac.new(str(secret).encode(), ("notices:" + label).encode(), hashlib.sha256).digest()
    return int.from_bytes(h[:8], "little") / 2.0 ** 64


def _usd(x: float) -> str:
    return f"${x / 1e6:,.0f}M"


def _days(p: dict) -> str:
    return f"{p['first_day']} to {p['last_day']}"


def _econ():
    from .. import economics as E
    return E


def rules(events=()) -> dict:
    """The program's rules after these events, in order: both budgets, the catalog engines for sale and those
    no longer sold, the launch period, the added requirements and the ids of the notices applied."""
    E = _econ()
    hw, rnd, period, stopped, req = float(E.HARDWARE_BUDGET_USD), float(E.RND_BUDGET_USD), 0, [], {}
    for ev in events or ():
        kind = ev.get("kind")
        if kind == "launch_slip":
            period = min(period + 1, len(LAUNCH_PERIODS) - 1)
        elif kind == "hardware_budget_cut":
            hw *= 1.0 - float(ev["cut"])
        elif kind == "rnd_budget_cut":
            rnd *= 1.0 - float(ev["cut"])
        elif kind == "engine_discontinued":
            stopped.append(ev["engine"])
        elif kind == "new_requirement":
            req[ev["requirement"]] = REQUIREMENTS[ev["requirement"]]["value"]
        else:
            raise ValueError(f"unknown event kind {kind!r}")
    return {"hardware_budget_usd": round(hw, 0), "rnd_budget_usd": round(rnd, 0),
            "catalog_engines_for_sale": [n for n in sorted(E.CATALOG_ENGINES) if n not in stopped],
            "catalog_engines_not_sold": sorted(set(stopped)),
            "launch_period": dict(LAUNCH_PERIODS[period]),
            "requirements": req,
            "notices": [ev.get("id") for ev in events or ()]}


def _notice(ev: dict, before: dict, after: dict) -> dict:
    """The notice's title, its text, and the message posted on the board."""
    kind = ev["kind"]
    if kind == "launch_slip":
        p, q = before["launch_period"], after["launch_period"]
        title = f"The launch slips to the {q['name']} opportunity"
        text = (f"The launch slips to the next opportunity. The launch period is now {_days(q)} (UTC). Before, it was "
                f"{_days(p)}. Choose a new launch date inside the period and replan the mission for it.")
    elif kind == "hardware_budget_cut":
        title = f"The hardware budget is cut by {ev['cut']:.0%}"
        text = (f"The hardware budget is cut by {ev['cut']:.0%}, from {_usd(before['hardware_budget_usd'])} to "
                f"{_usd(after['hardware_budget_usd'])}. A design whose hardware costs more cannot be built.")
    elif kind == "rnd_budget_cut":
        title = f"The R&D budget is cut by {ev['cut']:.0%}"
        text = (f"The R&D budget is cut by {ev['cut']:.0%}, from {_usd(before['rnd_budget_usd'])} to "
                f"{_usd(after['rnd_budget_usd'])}. Tests already ordered stand. An order that would take the R&D "
                f"committed past {_usd(after['rnd_budget_usd'])} is refused.")
    elif kind == "engine_discontinued":
        title = f"The {ev['engine']} is no longer sold"
        text = (f"The supplier of the {ev['engine']} has stopped selling it. A design that uses it cannot be built. "
                "Tests of it, and static fires of a stage that carries it, are refused from now on.")
    elif kind == "new_requirement":
        title = f"New requirement: {ev['requirement']}"
        text = f"The program adds a requirement. {REQUIREMENTS[ev['requirement']]['text']}"
    else:
        raise ValueError(f"unknown event kind {kind!r}")
    return {"title": title, "text": text, "message": f"{text} {TAIL}"}


def draw(secret: str, kinds=None, force: bool = False) -> list:
    """The run's events in time order, drawn from the secret. Empty when the switch is off (unless force).
    kinds: which kinds, each at most once (default: ALWAYS plus DRAWN of the others). Each event has an id
    (N1, N2, ...), its kind, `at` (the fraction of the run when it comes), what it changes (`cut`, `engine`,
    `requirement`) and its notice (`title`, `text`, `message`)."""
    if not (force or enabled()):
        return []
    names = sorted(_econ().CATALOG_ENGINES)
    if kinds is None:
        others = [k for k in KINDS if k not in ALWAYS and (k != "engine_discontinued" or names)]
        kinds = list(ALWAYS) + sorted(others, key=lambda k: _u(secret, "pick:" + k))[:DRAWN]
    evs = []
    for kind in kinds:
        if kind not in WHEN:
            raise ValueError(f"unknown event kind {kind!r}")
        lo, hi = WHEN[kind]
        ev = {"kind": kind, "at": round(lo + (hi - lo) * _u(secret, "at:" + kind), 4)}
        if kind == "hardware_budget_cut":
            ev["cut"] = HARDWARE_CUT
        elif kind == "rnd_budget_cut":
            ev["cut"] = RND_CUT
        elif kind == "engine_discontinued":
            ev["engine"] = names[min(int(_u(secret, "engine") * len(names)), len(names) - 1)]
        elif kind == "new_requirement":
            keys = sorted(REQUIREMENTS)
            ev["requirement"] = keys[min(int(_u(secret, "requirement") * len(keys)), len(keys) - 1)]
        evs.append(ev)
    evs.sort(key=lambda e: e["at"])
    for i, ev in enumerate(evs):
        ev["id"] = f"N{i + 1}"
        ev.update(_notice(ev, rules(evs[:i]), rules(evs[:i + 1])))
    return evs


def due(events: list, fraction: float) -> list:
    """The events that have come by this fraction of the run (fake clocks in tests use this too)."""
    return [ev for ev in events or () if float(ev.get("at", 2.0)) <= fraction]


def in_force(state: dict | None, graded: str = "scheduled") -> list:
    """The events in force at the end of the run, from the program office's state ({"events", "fired"}).
    scheduled: every event drawn for the run, whether or not the team was still working when it came.
    fired: only the events that had come when the state was read."""
    state = state if isinstance(state, dict) else {}
    evs = [ev for ev in state.get("events") or [] if isinstance(ev, dict)]
    if graded == "fired":
        fired = state.get("fired") or {}
        evs = [ev for ev in evs if ev.get("id") in fired]
    elif graded != "scheduled":
        raise ValueError(f"graded must be scheduled or fired, not {graded!r}")
    return evs


def launch_epoch(design: dict, r: dict, default: str = "2031-01-15T00:00:00") -> tuple:
    """(the launch_epoch_utc the vehicle flies, whether the launch period moved it). A date outside the launch
    period in force moves to the nearest day of the period, at 00:00 UTC. A value that is not a date stays."""
    le = (design or {}).get("launch_epoch_utc", default)
    try:
        dt = _dt.datetime.fromisoformat(str(le).replace("Z", ""))
        if dt.tzinfo is not None:
            dt = dt.astimezone(_dt.timezone.utc).replace(tzinfo=None)
        first = _dt.datetime.fromisoformat(r["launch_period"]["first_day"])
        last = _dt.datetime.fromisoformat(r["launch_period"]["last_day"])
    except (TypeError, ValueError, KeyError):
        return le, False
    if dt < first:
        return first.isoformat(), True
    if dt >= last + _dt.timedelta(days=1):
        return last.isoformat(), True
    return le, False


def check_design(design: dict, r: dict) -> list:
    """Problems of a design under the rules in force: catalog engines no longer sold and a launch date outside
    the launch period. Reports only; what a problem does to the score is the verifier's call."""
    out = []
    stopped = set(r.get("catalog_engines_not_sold") or ())
    engines = (design or {}).get("engines") or {}
    used = {s.get("catalog") for s in engines.values() if isinstance(s, dict) and isinstance(s.get("catalog"), str)}
    for name in sorted(used & stopped):
        out.append(f"catalog engine {name} is no longer sold")
    if launch_epoch(design, r)[1]:
        out.append(f"launch_epoch_utc is outside the launch period {_days(r['launch_period'])}")
    return out


def cli_notices(req: dict) -> dict:
    with open(req["secret_file"]) as f:
        secret = json.load(f)["secret"]
    return {"enabled": enabled(), "events": draw(secret)}


def cli_rules(req: dict) -> dict:
    return dict(rules(req.get("events") or []), enabled=enabled())
