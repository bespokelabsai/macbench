"""Capture hardware in the program office: hidden truth, a test, and a failure chance at capture.

Each capture system design has a hidden per-attempt failure chance, drawn from the program secret and the design's
key, so the same design always has the same truth (data.CAPTURE_FAIL_*). The team learns about it only through the
capture test, which runs N capture attempts on an air-bearing floor and reports how many worked. In flight, a capture
command inside the envelope still fails with that chance, larger near the envelope's edge.

Wiring (coordinator): add ``TEST_KIND`` to ``program.TEST_KINDS``, a ``"capture"`` branch in ``program.quote`` and
``program.run_test`` that call ``quote_capture_test`` and ``run_capture_test``, and ``flight_capture_truth`` in
``program.flight_truth`` (key ``"capture"``). Nothing here is called until then.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math

import numpy as np

from . import data as D

T = D.CAPTURE_TEST

TEST_KIND = {
    "capture": {
        "facility": "air_bearing_floor",
        "target": "a capture system and its container: {'capture_diameter_m': 1.5, 'container_diameter_m': 0.4, "
                  "'trials': 20}. A flight-like capture system and a mock container are built for the test.",
        "params": {"trials": f"number of capture attempts, {T['trials'][0]} to {T['trials'][1]}"},
        "reveals": "how many of the attempts captured the container, and for each failed one whether the container "
                   "rebounded out of the cone or the mechanism jammed. No capture system of this kind has flown.",
    }
}


def _rng(secret, key):
    h = hmac.new(str(secret).encode(), key.encode(), hashlib.sha256).digest()
    return np.random.default_rng(int.from_bytes(h[:8], "little"))


def capture_key(spec):
    """The design key of a capture system: its cone opening and the container it takes, to the centimetre."""
    return "capture:" + json.dumps({"d": round(float(spec["capture_diameter_m"]), 2),
                                    "c": round(float(spec["container_diameter_m"]), 2)}, sort_keys=True)


def capture_truth(secret, spec):
    """The hidden per-attempt failure chance of a capture system design (never shown to the team)."""
    z = float(_rng(secret, "truth:" + capture_key(spec)).standard_normal())
    p = D.CAPTURE_FAIL_MEDIAN * math.exp(D.CAPTURE_FAIL_SIGMA_LN * z)
    lo, hi = D.CAPTURE_FAIL_CLIP
    return {"key": capture_key(spec), "fail_p": float(min(max(p, lo), hi)), "rebound_share": D.CAPTURE_REBOUND_SHARE}


def attempt(truth, rng, used=0.0):
    """One capture attempt inside the envelope: "captured", "rebound" (try again) or "jam" (the capture system is
    lost). used is the envelope fraction from capture.envelope; the chance grows toward the edge."""
    p = min(1.0, truth["fail_p"] * (1.0 + D.CAPTURE_EDGE_GAIN * max(0.0, min(used, 1.0)) ** 2))
    u = float(rng.random())
    if u >= p:
        return "captured"
    return "rebound" if u < p * truth["rebound_share"] else "jam"


def _trials(spec):
    n = int(spec.get("trials", 10))
    lo, hi = T["trials"]
    if not lo <= n <= hi:
        raise ValueError(f"trials must be {lo} to {hi}")
    from .capture import check_sizes
    errs = check_sizes(float(spec["container_diameter_m"]), float(spec["capture_diameter_m"]))
    if errs:
        raise ValueError("; ".join(errs))
    return n


def quote_capture_test(spec):
    n = _trials(spec)
    return {"kind": "capture", "key": capture_key(spec), "facility": TEST_KIND["capture"]["facility"],
            "cost_usd": round(T["fixed_usd"] + n * T["per_trial_usd"], 0),
            "minutes": T["minutes_fixed"] + n * T["minutes_per_trial"]}


def run_capture_test(spec, test_id, secret):
    """The outcome of one capture test. Deterministic for (secret, test_id). Attempts are made near the centre of
    the envelope; a jam ends the test early (the article must be rebuilt)."""
    n = _trials(spec)
    truth = capture_truth(secret, spec)
    rng = _rng(secret, "test:" + str(test_id))
    results = []
    for _ in range(n):
        r = attempt(truth, rng, used=0.3)
        results.append(r)
        if r == "jam":
            break
    ok = results.count("captured")
    return {"kind": "capture", "key": truth["key"],
            "measured": {"attempts": len(results), "captured": ok, "rebound": results.count("rebound"),
                         "jam": results.count("jam")},
            "summary": f"{ok} of {len(results)} capture attempts worked"
                       + (" (the test ended at a jam)" if "jam" in results else "") + "."}


def flight_capture_truth(secret, design):
    """ctx.truth["capture"] for a design with a capture system and a sample container, else None."""
    els = design.get("elements") or []
    cap = next((e for e in els if e.get("kind") == "capture_system"), None)
    cont = next((e for e in els if e.get("kind") == "sample_container"), None)
    if cap is None or cont is None:
        return None
    return capture_truth(secret, {"capture_diameter_m": cap.get("capture_diameter_m"),
                                  "container_diameter_m": cont.get("diameter_m")})
