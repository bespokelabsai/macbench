"""Failure investigations: telemetry around a failed test or flight, and a paid root cause.

Switch: MARSFLIGHT_INVESTIGATION=1 in program_plugin.py. Off by default. Nothing here changes a flight
or a test outcome: it only reads a failure that already happened.

After a test or flight fails, the program office offers an investigation:
- `telemetry()` is free. It returns synthetic sensor traces around the failure: time from ignition (or
  from the event) and each channel in percent of its nominal value, with sensor noise. It is drawn from
  the failure class and from numbers the team already read in its test result, never from a hidden
  model value. Every number is written with at most 3 significant digits, so the leak rule of
  dev/test_leaks.py (no hidden number with 4 or more significant digits) holds by construction.
- `root_cause()` costs money from the R&D budget (`price()`). It names the cause class (a design flaw
  or a random failure) and what to change, in words, with no numbers.

It lives in program/ beside notices.py and contractors.py and imports as marsflight.program.investigation
(program.py sets __path__ to this folder). Run it with `python -m marsflight.program.investigation`: one JSON
object on stdin, one JSON line on stdout.
    {"op": "telemetry" | "root_cause" | "price", "kind": ..., "raw_failure": ..., "team": {...},
     "test_id": ..., "secret": ..., "cost_usd": ...}
"""
import hashlib
import json
import math
import random
import sys

ROOT_CAUSE_FRACTION = 0.25     # of the failed test's cost
ROOT_CAUSE_MIN_USD = 0.5e6
FLIGHT_ROOT_CAUSE_USD = 5e6

# Failure classes: a pattern in the simulator's failure text, the class, whether it is a design flaw, the
# telemetry signature, and the root cause a team buys.
CLASSES = [
    ("combustion instability", "instability", True, "oscillation",
     "Combustion instability. Pressure oscillations coupled with a chamber acoustic mode. This is a flaw of "
     "the engine design, not bad luck: a retest of the same design fails the same way. Change the injector "
     "or chamber design (a new engine key), for example the cycle, chamber pressure or element pattern."),
    ("cooling", "cooling", True, "thermal_runaway",
     "Insufficient regenerative cooling. The chamber wall ran hotter than it can stand and the burn was cut. "
     "This is a flaw of the engine design: lower the chamber pressure, change the cycle, or pick a coolant "
     "and chamber that carry more heat."),
    ("hard start", "hard_start", False, "start_spike",
     "A hard start: a random ignition failure. The design is not at fault. More starts on the test stand "
     "raise the engine's demonstrated reliability."),
    ("random failure", "random", False, "step_loss",
     "A random failure in the rated operating range. The design is not at fault; more testing lowers the "
     "failure chance."),
    ("burst", "burst", True, "pressure_ramp",
     "The tank wall was too thin for proof pressure with its weld efficiency. Thicken the wall, lower the "
     "tank pressure, or use a material with a higher allowable."),
    ("sample failed", "tps_overheat", True, "thermal_runaway",
     "The thermal protection layer is too thin for this heat flux; its bond line overheated. Thicken the "
     "layer or choose a material rated for a higher heat flux."),
    ("canopy tore", "canopy", False, "load_spike",
     "The canopy tore at line stretch, a random deployment failure. More drops lower the deployment failure "
     "chance."),
    ("acoustic", "vibration", True, "load_spike",
     "The part's qualification level was below the environment it saw. Raise the qualification level or "
     "stiffen the part's mounting."),
    ("shock", "vibration", True, "load_spike",
     "A separation or landing shock beyond the part's qualification. Raise the shock qualification level."),
    ("max-q", "max_q", True, "load_spike",
     "Aerodynamic load beyond the structure's limit near maximum dynamic pressure. Fly a lower-q trajectory "
     "(throttle down through max-q) or strengthen the stage structure."),
    ("heat", "entry_heating", True, "thermal_runaway",
     "Entry heating beyond the heat shield's capacity. Thicken the TPS or fly a shallower entry."),
]
GENERIC = ("unknown", False, "step_loss",
           "The failure has no single design cause the investigators could find; treat it as random.")

CHANNELS = {
    "oscillation": ("chamber_pressure_pct", "chamber_pressure_oscillation_pct", "thrust_pct"),
    "thermal_runaway": ("wall_temperature_pct_of_limit", "coolant_outlet_temperature_pct", "thrust_pct"),
    "start_spike": ("chamber_pressure_pct", "igniter_current_pct", "thrust_pct"),
    "step_loss": ("chamber_pressure_pct", "turbopump_speed_pct", "thrust_pct"),
    "pressure_ramp": ("tank_pressure_pct_of_proof", "wall_strain_pct_of_yield", "acoustic_emission_pct"),
    "load_spike": ("axial_load_pct_of_limit", "bending_load_pct_of_limit", "acceleration_pct"),
}


def sig3(x: float) -> float:
    """x rounded to 3 significant digits."""
    if x == 0 or not math.isfinite(x):
        return 0.0
    v = float(f"{x:.3g}")
    return int(v) if v.is_integer() else v   # 100, not 100.0, so the JSON text has no extra digit


def classify(raw_failure: str):
    text = (raw_failure or "").lower()
    for pat, cls, flaw, sig, cause in CLASSES:
        if pat in text:
            return cls, flaw, sig, cause
    return GENERIC


def _rng(secret: str, test_id: str) -> random.Random:
    h = hashlib.sha256(f"{secret}:investigation:{test_id}".encode()).hexdigest()
    return random.Random(int(h[:16], 16))


def _fail_time(kind: str, team: dict, rng: random.Random) -> float:
    """When the failure happened, in seconds from the start of the run, from what the team already knows."""
    t = team.get("t_fail_s")
    if isinstance(t, (int, float)) and t > 0:
        return float(t)
    if kind == "engine_hotfire":
        return 0.8 if "instab" in str(team.get("failure", "")).lower() else 20.0
    return 10.0 + 5.0 * rng.random()


def _shape(sig: str, ch: int, x: float) -> float:
    """Percent of nominal at x = t - t_fail (seconds), for channel ch of a signature."""
    after = x >= 0
    if sig == "oscillation":
        if ch == 1:
            return 2.0 * math.exp(min(x, 0.0) * 3.0 + 3.0) if not after else 0.0
        return 100.0 if not after else 0.0
    if sig == "thermal_runaway":
        if ch in (0, 1):
            return 70.0 + 30.0 * math.exp(min(x, 0.0) / 6.0) + (8.0 if after else 0.0)
        return 100.0 if not after else 0.0
    if sig == "start_spike":
        if ch == 1:
            return 100.0 if x < 0 else 0.0
        return (180.0 if -0.05 <= x < 0.05 else (100.0 if x < 0 else 0.0))
    if sig == "step_loss":
        return 100.0 if not after else 0.0
    if sig == "pressure_ramp":
        if ch == 2:
            return 5.0 + 95.0 * math.exp(min(x, 0.0) / 2.0) if not after else 0.0
        return (60.0 + 40.0 * max(0.0, 1 + x / 20.0)) if not after else 0.0
    if sig == "load_spike":
        return (80.0 + 40.0 * math.exp(-abs(x) * 2.0)) if x <= 0.2 else 0.0
    return 100.0


def telemetry(kind: str, raw_failure: str, team: dict, test_id: str, secret: str, window_s: float = 10.0,
              rate_hz: float = 5.0) -> dict:
    """Synthetic traces from window_s before the failure to 2 s after. Channels in percent of nominal."""
    rng = _rng(secret, test_id)
    cls, flaw, sig, _ = classify(raw_failure)
    tf = _fail_time(kind, team or {}, rng)
    t0 = max(0.0, tf - window_s)
    n = int(round((tf + 2.0 - t0) * rate_hz)) + 1
    names = CHANNELS[sig]
    times = [sig3(t0 + i / rate_hz) for i in range(n)]
    traces = {}
    for ch, name in enumerate(names):
        vals = []
        for t in times:
            v = _shape(sig, ch, t - tf)
            if sig == "oscillation" and ch == 0 and t < tf:
                v += _shape(sig, 1, t - tf) * math.sin(2 * math.pi * 7.0 * t)
            v *= 1 + rng.gauss(0, 0.004)
            vals.append(sig3(v))
        traces[name] = vals
    return {"test": test_id, "kind": kind, "failure": (team or {}).get("failure"), "t_fail_s": sig3(tf),
            "rate_hz": rate_hz, "units": "seconds from the start of the run; each channel in percent of its "
                                         "nominal value (or of its limit, where the name says so)",
            "t_s": times, "channels": traces,
            "note": "Synthetic telemetry from the test article's instrumentation. Noise is about 0.4% of reading."}


def price(kind: str, cost_usd: float) -> float:
    if kind == "flight":
        return FLIGHT_ROOT_CAUSE_USD
    return float(max(ROOT_CAUSE_MIN_USD, ROOT_CAUSE_FRACTION * float(cost_usd or 0.0)))


def root_cause(kind: str, raw_failure: str) -> dict:
    cls, flaw, _, cause = classify(raw_failure)
    return {"kind": kind, "cause_class": cls, "design_flaw": bool(flaw), "finding": cause}


def _main(argv):
    try:
        req = json.loads(sys.stdin.read() or "{}")
        op = req.get("op")
        if op == "telemetry":
            out = telemetry(req.get("kind", ""), req.get("raw_failure", ""), req.get("team") or {},
                            str(req.get("test_id", "")), str(req.get("secret", "")))
        elif op == "root_cause":
            out = root_cause(req.get("kind", ""), req.get("raw_failure", ""))
        elif op == "price":
            out = {"price_usd": price(req.get("kind", ""), req.get("cost_usd", 0.0))}
        else:
            out = {"error": f"unknown op {op!r}"}
    except Exception as e:
        out = {"error": f"investigation error: {e}"}
    print(json.dumps(out))


if __name__ == "__main__":
    _main(sys.argv[1:])
