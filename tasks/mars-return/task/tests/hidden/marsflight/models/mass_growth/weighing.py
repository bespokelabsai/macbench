"""The program office's weighing test: build one element and weigh it.

It reveals the element's as-built dry mass to 0.1% (1 sigma), and so its hidden growth. The coordinator adds
``TEST_KIND`` to ``program.TEST_KINDS``, a branch for ``"weighing"`` in ``program.quote`` and ``program.run_test``
that call ``quote_weighing`` and ``run_weighing``, ``record_weighings`` in ``program.record_from_ledger``, and
``flight_growth`` in ``program.flight_truth`` (key ``"mass_growth"``).
"""
from __future__ import annotations

from .growth import TABLES, element_growth, element_key, growth_truth

W = TABLES["weighing"]

TEST_KIND = {
    "weighing": {
        "facility": "stage_stand",
        "target": "one element of a whole design: {'design': {...vehicle.json...}, 'element': 'S2'}. The element is "
                  "built as it sits in that stack (head reinforcement, interstage), so weigh it in the design you "
                  "will fly.",
        "params": {},
        "reveals": "the element's as-built dry mass, weighed to 0.1% (1 sigma). Real hardware comes out heavier "
                   "than its design; the AIAA S-120A mass growth allowance for a design at this maturity is "
                   "18% for structure and propulsion. Until an element is weighed only its design mass is known.",
    }
}


def _target(spec):
    """(element spec, engines, built vehicle, built element) of a weighing target."""
    from ...vehicle import build_vehicle
    if not isinstance(spec, dict) or not isinstance(spec.get("design"), dict) or not isinstance(spec.get("element"), str):
        raise ValueError("target needs {'design': {...a vehicle.json...}, 'element': 'element id'}")
    design, eid = spec["design"], spec["element"]
    els = [e for e in design.get("elements") or [] if isinstance(e, dict) and str(e.get("id")) == eid]
    if not els:
        raise ValueError(f"the design has no element {eid!r}")
    v = build_vehicle(design)
    if v.errors:
        raise ValueError("design is invalid: " + "; ".join(v.errors[:3]))
    el = next(e for e in v.elements if e.id == eid)
    return els[0], design.get("engines") or {}, v, el


def quote_weighing(spec):
    from ... import economics as E
    el_spec, engines, v, el = _target(spec)
    usd = E.cost_report(v)["by_element_usd"].get(str(el_spec.get("id")), 0.0)
    return {"kind": "weighing", "key": element_key(el_spec, engines), "facility": TEST_KIND["weighing"]["facility"],
            "cost_usd": round(W["cost_fraction_of_element"] * usd + W["fixed_usd"], 0), "minutes": W["minutes"],
            "note": "includes building a flight-like article at a quarter of the element's cost"}


def run_weighing(spec, test_id, secret):
    """The outcome of one weighing. Deterministic for (secret, test_id)."""
    from ...program import _rng
    el_spec, engines, _, el = _target(spec)
    g = element_growth(secret, el_spec, engines, el)
    z = float(_rng(secret, "test:" + str(test_id)).standard_normal())
    measured = (g["basic_kg"] + g["growth_kg"]) * (1.0 + W["sd"] * z)
    # Only the measured mass: the simulator's own design mass is hidden from team text, so the result does
    # not give it or the growth against it. The team compares the weight with its own estimate.
    return {"kind": "weighing", "key": g["key"],
            "measured": {"dry_mass_kg": round(measured, 1), "scale_sd_pct": 100 * W["sd"]},
            "summary": f"The element weighed {measured:,.1f} kg dry (scale accuracy 0.1%)."}


def record_weighings(ledger):
    """{element key: measured dry kg} from the finished weighings in a ledger (the latest one wins)."""
    out = {}
    for t in ledger or []:
        if t.get("status") == "done" and t.get("kind") == "weighing":
            m = (t.get("outcome") or {}).get("measured") or {}
            if m.get("dry_mass_kg"):
                out[t.get("key")] = float(m["dry_mass_kg"])
    return out


def flight_growth(secret, design, vehicle, weighed):
    """ctx.truth["mass_growth"] for the verifier: the hidden growth of every element, plus the weighed mass the
    flight software is told for elements that were weighed."""
    t = growth_truth(secret, design, vehicle)
    for eid, row in t.items():
        if row["key"] in (weighed or {}):
            row["weighed_kg"] = weighed[row["key"]]
    return t
