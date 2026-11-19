"""Two contractors with separate budgets.

The program is split into two contractors that meet at the sample container:
- lander: the launch to Mars, the entry system, the lander, the Mars ascent vehicle and the sample container;
- orbiter: the orbiter, the capture system and the Earth return capsule.

Each contractor has its own hardware budget and its own R&D budget, a share (SHARES) of the program's budgets
in force. A design marks each element's contractor with `"contractor": "lander"` or `"orbiter"`. Items not tied
to one element (launch operations) are split by the same shares.

Handover rules:
1. Every element has exactly one contractor. An element without one, or with an unknown one, is an error.
2. The interface is the sample container. The design lists it as `"interface": {"element": ID, ...}`. The
   element must belong to the lander contractor (it builds and launches the container), and it is handed to
   the orbiter at capture. Only one element may be handed over, and only from lander to orbiter.
3. Both contractors state the interface they design to, under `"interface": {"lander": {...}, "orbiter":
   {...}}`. Each field in INTERFACE_FIELDS that both state must agree within its tolerance. A field only one
   side states is reported as not agreed.
4. Budget may move between contractors only by a transfer both sign (`"budget_transfers": [{"from", "to",
   "kind": "hardware" or "rnd", "usd", "signed_by": [both]}]`), and never more than the giver has left.

The whole feature is behind MARSFLIGHT_CONTRACTORS (default off). This module only reports; it does not
change the score. The verifier and the board plugin can call split() once they adopt it.
"""
from __future__ import annotations

import os

SWITCH = "CONTRACTORS"
_TRUE = ("1", "true", "on", "yes", "")
_FALSE = ("0", "false", "off", "no")

CONTRACTORS = ("lander", "orbiter")
SHARES = {"lander": 0.6, "orbiter": 0.4}     # of each budget in force; the lander side carries the launch
SHARED_ELEMENTS = ("launch",)                 # cost items that belong to no element, split by SHARES
HANDOVER = ("lander", "orbiter")             # the only allowed direction of a handover
# Interface fields both sides must agree on, with the tolerance (absolute) for numbers; None means equal.
INTERFACE_FIELDS = {
    "container_mass_kg": 0.05,
    "container_diameter_m": 0.005,
    "orbit_altitude_km": 5.0,
    "orbit_inclination_deg": 0.5,
    "release_utc": None,
    "capture_utc": None,
}


def enabled() -> bool:
    """Whether the split is on: MARSFLIGHT_CONTRACTORS=1 or 0, default off."""
    v = (os.environ.get("MARSFLIGHT_" + SWITCH) or "").strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise ValueError(f"MARSFLIGHT_{SWITCH}={v!r}: use 1 or 0")


def budgets(hardware_usd: float, rnd_usd: float) -> dict:
    """Each contractor's share of the program's budgets in force (before transfers)."""
    return {c: {"hardware_budget_usd": round(hardware_usd * SHARES[c], 0), "rnd_budget_usd": round(rnd_usd * SHARES[c], 0)}
            for c in CONTRACTORS}


def assign(design: dict) -> tuple:
    """({element id: contractor}, errors). Rule 1."""
    owners, errors = {}, []
    for el in (design or {}).get("elements") or []:
        if not isinstance(el, dict):
            continue
        eid, c = el.get("id"), el.get("contractor")
        if c is None:
            errors.append(f"element {eid}: no contractor; set \"contractor\" to one of {list(CONTRACTORS)}")
        elif c not in CONTRACTORS:
            errors.append(f"element {eid}: unknown contractor {c!r}; use one of {list(CONTRACTORS)}")
        else:
            owners[eid] = c
    return owners, errors


def check_handover(design: dict, owners: dict) -> list:
    """Problems with the handover and the interface (rules 2 and 3)."""
    out = []
    iface = (design or {}).get("interface")
    used = set(owners.values())
    if not isinstance(iface, dict):
        if len(used) > 1:
            out.append("both contractors have elements but the design names no interface")
        return out
    eid = iface.get("element")
    if eid is None:
        out.append("interface: no element named as the sample container")
    elif eid not in owners:
        out.append(f"interface: element {eid!r} is not in the design or has no contractor")
    elif owners[eid] != HANDOVER[0]:
        out.append(f"interface: element {eid} belongs to {owners[eid]}; the container is handed over from "
                   f"{HANDOVER[0]} to {HANDOVER[1]}, so the {HANDOVER[0]} contractor must build it")
    extra = [el.get("id") for el in (design or {}).get("elements") or []
             if isinstance(el, dict) and el.get("handover_to") and el.get("id") != eid]
    for x in extra:
        out.append(f"element {x}: only the interface element may be handed over")
    sides = {c: iface.get(c) if isinstance(iface.get(c), dict) else {} for c in CONTRACTORS}
    for field, tol in INTERFACE_FIELDS.items():
        a, b = sides[CONTRACTORS[0]].get(field), sides[CONTRACTORS[1]].get(field)
        if a is None and b is None:
            continue
        if a is None or b is None:
            out.append(f"interface {field}: only {CONTRACTORS[0] if b is None else CONTRACTORS[1]} states it; not agreed")
        elif tol is None:
            if a != b:
                out.append(f"interface {field}: {CONTRACTORS[0]} says {a!r}, {CONTRACTORS[1]} says {b!r}")
        else:
            try:
                if abs(float(a) - float(b)) > tol:
                    out.append(f"interface {field}: {CONTRACTORS[0]} says {a}, {CONTRACTORS[1]} says {b} "
                               f"(tolerance {tol})")
            except (TypeError, ValueError):
                out.append(f"interface {field}: not a number")
    return out


def apply_transfers(b: dict, transfers, spent: dict | None = None) -> tuple:
    """(budgets after the signed transfers, errors). Rule 4. spent: {contractor: {"hardware": usd, "rnd": usd}},
    used to refuse a transfer of more than the giver has left."""
    b = {c: dict(v) for c, v in b.items()}
    spent = spent or {}
    errors = []
    for i, t in enumerate(transfers or ()):
        if not isinstance(t, dict):
            errors.append(f"transfer {i}: not an object")
            continue
        src, dst, kind = t.get("from"), t.get("to"), t.get("kind")
        key = {"hardware": "hardware_budget_usd", "rnd": "rnd_budget_usd"}.get(kind)
        try:
            usd = float(t.get("usd"))
        except (TypeError, ValueError):
            usd = -1.0
        if src not in CONTRACTORS or dst not in CONTRACTORS or src == dst:
            errors.append(f"transfer {i}: from and to must be the two different contractors")
        elif key is None:
            errors.append(f"transfer {i}: kind must be hardware or rnd")
        elif usd <= 0:
            errors.append(f"transfer {i}: usd must be a positive number")
        elif set(t.get("signed_by") or ()) != set(CONTRACTORS):
            errors.append(f"transfer {i}: both contractors must sign it")
        elif usd > b[src][key] - float((spent.get(src) or {}).get(kind, 0.0)):
            errors.append(f"transfer {i}: {src} has less than ${usd / 1e6:,.1f}M of {kind} budget left")
        else:
            b[src][key] = round(b[src][key] - usd, 0)
            b[dst][key] = round(b[dst][key] + usd, 0)
    return b, errors


def hardware_by_contractor(report: dict, owners: dict) -> dict:
    """Each contractor's hardware spend from a cost report's by_element_usd. Items of elements without a
    contractor and SHARED_ELEMENTS are split by SHARES."""
    out = {c: 0.0 for c in CONTRACTORS}
    for eid, usd in (report.get("by_element_usd") or {}).items():
        c = owners.get(eid)
        if c is None:
            for k in CONTRACTORS:
                out[k] += float(usd) * SHARES[k]
        else:
            out[c] += float(usd)
    return {c: round(v, 0) for c, v in out.items()}


def rnd_by_contractor(tests, owners: dict) -> dict:
    """Each contractor's R&D committed from the test ledger. A test is charged to the contractor of the element
    it names (params.element, spec.element or the ledger's target_name); cancelled tests count nothing; others (engine and material tests) are split by SHARES."""
    out = {c: 0.0 for c in CONTRACTORS}
    for t in tests or ():
        if not isinstance(t, dict) or t.get("status") == "cancelled":
            continue
        usd = float(t.get("cost_usd") or t.get("usd") or 0.0)
        eid = ((t.get("params") or {}).get("element") or (t.get("spec") or {}).get("element") or t.get("element")
               or t.get("target_name"))
        c = owners.get(eid)
        if c is None:
            for k in CONTRACTORS:
                out[k] += usd * SHARES[k]
        else:
            out[c] += usd
    return {c: round(v, 0) for c, v in out.items()}


def split(design: dict, report: dict, hardware_budget_usd: float, rnd_budget_usd: float, tests=()) -> dict:
    """The contractor report for a design: owners, both budgets after transfers, each side's spending, whether
    each side is over budget, and every rule broken. It does not score anything."""
    owners, errors = assign(design)
    errors += check_handover(design, owners)
    hw = hardware_by_contractor(report, owners)
    rnd = rnd_by_contractor(tests, owners)
    spent = {c: {"hardware": hw[c], "rnd": rnd[c]} for c in CONTRACTORS}
    b, terr = apply_transfers(budgets(hardware_budget_usd, rnd_budget_usd), (design or {}).get("budget_transfers"), spent)
    errors += terr
    per = {}
    for c in CONTRACTORS:
        per[c] = {"elements": sorted(e for e, o in owners.items() if o == c),
                  "hardware_usd": hw[c], "hardware_budget_usd": b[c]["hardware_budget_usd"],
                  "rnd_committed_usd": rnd[c], "rnd_budget_usd": b[c]["rnd_budget_usd"],
                  "over_hardware_budget": hw[c] > b[c]["hardware_budget_usd"],
                  "over_rnd_budget": rnd[c] > b[c]["rnd_budget_usd"]}
    return {"enabled": enabled(), "contractors": per, "errors": errors,
            "any_over_budget": any(p["over_hardware_budget"] or p["over_rnd_budget"] for p in per.values())}
