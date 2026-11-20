"""Hidden mass growth from design to flight, revealed only by weighing.

Today the built mass matches the design within 1% (``environment.Dispersions.dry_mass_factor``), so a team can
design to the exact mass and carry no margin. Real hardware grows between the early design and flight. The AIAA
mass properties standard (R-020A-1999, S-120-2006, S-120A-2015) sets a mass growth allowance (MGA) for each kind
of hardware and each design maturity; ``mga_tables.json`` holds the table and its source.

**Hidden truth.** Each part's growth is drawn from the program secret, keyed by the part (the same HMAC streams
as ``program.engine_truth`` and ``tank_truth``), one draw per part and per kind of hardware:

- a stage's own hardware (tanks, structure, insulation, systems) is keyed by the stage's ``stage_key``; its
  engines by each engine's ``engine_key``, so every copy of one engine design grows alike;
- heat shields and capsules by ``shell_key``, parachutes by ``chute_key``, other elements by a hash of their spec.

The growth of one kind of hardware is lognormal: its median is the 'layout' row of the table and its 90th
percentile the 'estimated' row (sigma_ln 0.316). Existing hardware (catalog engines, RTGs) uses the 'existing
hardware' row. Propellant, pressurant gas and ballast do not grow. The same design under the same secret always
has the same growth; a changed design is new hardware and draws again.

**Weighing** (``weighing.py``) is a program office test that builds one element and weighs it, revealing its
as-built dry mass to 0.1%. Nothing else shows the growth before flight. ``predicted_report`` gives the basic mass,
the standard's allowance and any weighed mass per element, for analysis and for the flight software's vehicle
report; it never holds the hidden draw. Simulator-computed masses are themselves hidden from team text (dev/test_leaks.py), so it is not for the design review.

**Margin policy** is the team's: size to the basic mass, to basic plus the allowance, or weigh first.

Hooks the core calls (see the PR for the places in today's sim.py):
- ``as_built_dry(element)``: the dry mass the element really flies with (multiply by the world's
  ``dry_mass_factor`` as today).
- ``believed_dry(eid)``: what the team knows: the weighed mass if the element was weighed, else the design's.
- ``mass_items(vehicle)``: the same growth as point masses at each element's origin, for a core that adds mass
  items instead of scaling the dry mass.
"""
from __future__ import annotations

import hashlib
import json
import math
import os

from ...core.model import MassItem, Model

TABLES = json.load(open(os.path.join(os.path.dirname(__file__), "mga_tables.json")))
_MGA = TABLES["mga_percent"]
COLUMNS = _MGA["columns"]
CATEGORY = {k: v for k, v in TABLES["category_of_item"].items() if not k.startswith("_")}
UNKNOWN = TABLES["unknown_item_category"]
DIST = TABLES["growth_distribution"]
Z90 = 1.2815515655446004
SIGMA_LN = math.log(_MGA["E"][COLUMNS.index("structure")] / _MGA["L"][COLUMNS.index("structure")]) / Z90


def mga(row, column):
    """The standard's allowance, as a fraction of basic mass."""
    return _MGA[row][COLUMNS.index(column)] / 100.0


def category(item):
    """(column, existing hardware?) of a mass breakdown item, or (None, False) for mass that does not grow."""
    if item in CATEGORY:
        c = CATEGORY[item]
    elif item.startswith("trim_ballast"):
        c = None
    else:
        c = UNKNOWN
    if c is None:
        return None, False
    if c.startswith("existing:"):
        return c.split(":", 1)[1], True
    return c, False


def _rng(secret, key):
    from ...program import _rng as program_rng
    return program_rng(secret, key)


def growth_draw(secret, key, column, existing=False):
    """Hidden growth of one kind of hardware of one part: a fraction of its basic mass."""
    z = float(_rng(secret, f"mass:{key}:{column}").standard_normal())
    median = mga(DIST["existing_median_row"] if existing else DIST["median_row"], column)
    return median * math.exp(SIGMA_LN * z)


# Design decision: growth must not be counted twice. Masses that come from a
# fitted model already carry the growth flown hardware saw, so they grow with median 0 and a spread equal to the model's
# own fit error; only masses the team enters directly carry the full S-120A allowance (growth_draw above). This growth
# replaces Dispersions.dry_mass_factor for every element that has a draw (core/wiring.py _w_dry).
TEAM_ENTERED = {"structure_and_containment", "extra"}   # capsule containment assembly, EquipmentItem masses
FIT_ERROR = {
    # leave-one-out error of the stage mass model against flown stages (results/issue-10): tanks, structure,
    # insulation, stage systems, thrust structure, interstages, aeroshells, legs, parachutes
    "default": 0.066,
    # catalog engines and RTGs: flown units of known mass (S-120A 'existing hardware' row, 2%)
    "existing": 0.02,
}


def fitted_draw(secret, key, column, existing=False):
    """Hidden growth of a modeled mass: lognormal, median 0, with the model's fit error as its spread."""
    z = float(_rng(secret, f"mass:{key}:{column}").standard_normal())
    s = math.log(1.0 + FIT_ERROR["existing" if existing else "default"])
    return math.exp(s * z) - 1.0


def element_key(spec, engines):
    """The part key of an element's own hardware (its engines have their own)."""
    from ... import program as P
    k = spec.get("kind")
    if k == "stage":
        return P.stage_key(spec, engines or {})
    if k in ("heatshield", "capsule"):
        return P.shell_key(spec)
    if k == "parachute":
        return P.chute_key(spec)
    s = json.dumps({a: b for a, b in spec.items() if a != "id"}, sort_keys=True, separators=(",", ":"), default=str)
    return f"{k}:{hashlib.sha1(s.encode()).hexdigest()[:14]}"


def element_growth(secret, spec, engines, el):
    """Hidden growth of one built element: {"key", "basic_kg", "growth_kg", "items": {item: kg}}."""
    from ... import program as P
    key = element_key(spec, engines)
    items, total = {}, 0.0
    bd = dict(getattr(el, "mass_breakdown", {}) or {})
    eng_mass = bd.pop("engines", 0.0)
    for item, m in bd.items():
        col, existing = category(item)
        if col is None or m <= 0:
            continue
        g = m * (growth_draw(secret, key, col, existing) if item in TEAM_ENTERED
                 else fitted_draw(secret, key, col, existing))
        items[item] = g
        total += g
    if eng_mass > 0:
        # each engine design grows alike wherever it is used; catalog engines are existing hardware
        g = 0.0
        for en, n in getattr(el, "engines", []) or []:
            espec = (engines or {}).get(en.name) or {}
            frac = fitted_draw(secret, P.engine_key(espec), "propulsion", existing=bool(espec.get("catalog")))
            g += en.mass * n * frac
        items["engines"] = g
        total += g
    return {"key": key, "basic_kg": float(el.dry_mass), "growth_kg": float(total), "items": items}


def growth_truth(secret, design, vehicle):
    """The hidden growth of every element of a design, by element id. For program.flight_truth."""
    engines = design.get("engines") or {}
    specs = {str(e.get("id")): e for e in design.get("elements") or [] if isinstance(e, dict)}
    return {el.id: element_growth(secret, specs.get(el.id, {"kind": el.kind}), engines, el)
            for el in vehicle.elements}


def predicted_report(design, vehicle, weighed=None, maturity="E"):
    """Public-rule numbers (no hidden draw): per element the basic mass, the standard's allowance at a maturity (the 'estimated'
    row for a design not yet built), the predicted mass, and the weighed mass where there is one. ``weighed``:
    {element key: measured dry kg}, from the ledger."""
    engines = design.get("engines") or {}
    specs = {str(e.get("id")): e for e in design.get("elements") or [] if isinstance(e, dict)}
    out = {}
    for el in vehicle.elements:
        spec = specs.get(el.id, {"kind": el.kind})
        allow = 0.0
        for item, m in (el.mass_breakdown or {}).items():
            if item == "engines":
                for en, n in el.engines or []:
                    ex = bool(((engines.get(en.name) or {}).get("catalog")))
                    allow += en.mass * n * mga("X" if ex else maturity, "propulsion")
                continue
            col, existing = category(item)
            if col is not None:
                allow += m * mga("X" if existing else maturity, col)
        key = element_key(spec, engines)
        row = {"basic_kg": round(el.dry_mass, 1), "mga_kg": round(allow, 1),
               "mga_pct": round(100 * allow / el.dry_mass, 2) if el.dry_mass else 0.0,
               "predicted_kg": round(el.dry_mass + allow, 1), "key": key}
        if weighed and key in weighed:
            row["weighed_kg"] = round(float(weighed[key]), 1)
        out[el.id] = row
    return out


class MassGrowth(Model):
    """Flies each element at its as-built mass. ``ctx.truth["mass_growth"]`` is {eid: {"growth_kg", "weighed_kg"?}}:
    the hidden draw (program.flight_truth, from growth_truth) in the verifier, and only weighed elements in local
    runs (local_growth). Without it nothing grows."""
    name = "mass_growth"
    area = "mass"
    order = 10

    def __init__(self):
        self.growth = {}
        self.weighed = {}
        self.basic = {}

    def configure(self, ctx):
        truth = (ctx.truth or {}).get("mass_growth") or {}
        for el in ctx.vehicle.elements:
            t = truth.get(el.id) or {}
            self.basic[el.id] = float(el.dry_mass)
            self.growth[el.id] = float(t.get("growth_kg", 0.0))
            if t.get("weighed_kg") is not None:
                self.weighed[el.id] = float(t["weighed_kg"])

    def as_built_dry(self, el):
        return float(el.dry_mass) + self.growth.get(el.id, 0.0)

    def believed_dry(self, eid):
        return self.weighed.get(eid, self.basic.get(eid, 0.0))

    def mass_items(self, vehicle):
        return [MassItem(el.id, "as_built_growth", g) for el in vehicle.elements
                for g in [self.growth.get(el.id, 0.0)] if g > 0]

    def obs(self, state):
        return {}


def local_growth(design, vehicle, results):
    """Growth for local runs, from weighings the team saved (program/test_results.json): weighed elements fly at
    their measured mass, the rest at the design mass."""
    engines = design.get("engines") or {}
    specs = {str(e.get("id")): e for e in design.get("elements") or [] if isinstance(e, dict)}
    by_key = {}
    for r in results or []:
        if isinstance(r, dict) and r.get("kind") == "weighing" and (r.get("measured") or {}).get("dry_mass_kg"):
            by_key[r.get("key")] = float(r["measured"]["dry_mass_kg"])
    out = {}
    for el in vehicle.elements:
        k = element_key(specs.get(el.id, {"kind": el.kind}), engines)
        if k in by_key:
            out[el.id] = {"growth_kg": by_key[k] - float(el.dry_mass), "weighed_kg": by_key[k]}
    return out


MODELS = [MassGrowth]
