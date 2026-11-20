"""The recovery model the core calls. Behind MARSFLIGHT_RECOVERY, off by default.

Design field, on the first stage element (the first element of kind "stage"):

    {"id": "S1", "kind": "stage", ..., "recovery": "rtls"}      # or "droneship", or "none" (the default)

What it does when the switch is on and the mode is not "none":

- ``plan(dry_kg, propellant_kg, isp_sl_s, isp_vac_s)``: the propellant held back for the boostback, entry and
  landing burns, and the recovery hardware mass. The core takes ``reserve_kg`` out of the propellant the ascent may
  burn and adds ``hardware_kg`` to the stage dry mass. A reserve above half the load is a design error.
- ``on_event(separation of the first stage)``: draws the landing from the model's own stream at the Falcon 9 rate.
  The result shows in ``obs`` as ``booster_recovery`` (the flight software would hear it from the booster).
- ``credit(stage_cost_usd)``: the reuse cost credit for this flight, zero if the landing failed.

Not wired into the core yet: the core does not call ``plan`` at stage build, and ``economics.cost_report`` does not
take ``credit``. Those two lines belong to the owners of ``vehicle/`` and ``economics.py``.
The return flight itself (the stage as a second flown body with its own guidance) is not modeled: the burns are a
propellant budget and the landing is a draw.
"""
from __future__ import annotations

import os

from ...core.model import Model
from . import budget

SWITCH = "RECOVERY"
DEFAULT_ON = True
MAX_RESERVE_FRACTION = 0.5


def switch_on() -> bool:
    v = os.environ.get("MARSFLIGHT_" + SWITCH)
    if v is None or not v.strip():
        return DEFAULT_ON
    return v.strip().lower() not in ("0", "false", "off", "no")


def first_stage(design: dict | None):
    for e in (design or {}).get("elements", []):
        if e.get("kind") == "stage":
            return e
    return None


def check_design(design: dict | None) -> list[str]:
    """Design errors for the recovery field: a bad value, or the field on any stage but the first."""
    errs = []
    s1 = first_stage(design)
    for e in (design or {}).get("elements", []):
        if "recovery" not in e:
            continue
        try:
            mode = budget.check_mode(e["recovery"])
        except ValueError as ex:
            errs.append(f"{e.get('id', '?')}: {ex}")
            continue
        if mode != "none" and e is not s1:
            errs.append(f"{e.get('id', '?')}: recovery is only for the first stage")
    return errs


class RecoveryModel(Model):
    name = "recovery"
    area = "recovery"
    order = 60

    def configure(self, ctx):
        self.ctx = ctx
        self.active = switch_on()
        s1 = first_stage(ctx.design)
        self.stage_id = s1.get("id") if s1 else None
        self.mode = "none"
        if self.active:
            errs = check_design(ctx.design)
            if errs:
                raise ValueError("; ".join(errs))
            self.mode = budget.check_mode(s1.get("recovery")) if s1 else "none"
        self.rng = ctx.rng(self.name)
        self.landed = None

    def design_fields(self):
        return ["elements[kind=stage][0].recovery"]

    # ------------------------------------------------------------ stage build
    def plan(self, dry_kg: float, propellant_kg: float, isp_sl_s: float, isp_vac_s: float) -> dict:
        if self.mode == "none":
            return {"mode": "none", "reserve_kg": 0.0, "hardware_kg": 0.0, "usable_kg": float(propellant_kg)}
        r = budget.reserve_kg(dry_kg, self.mode, isp_sl_s, isp_vac_s)
        if r["total_kg"] > MAX_RESERVE_FRACTION * propellant_kg:
            raise ValueError(f"{self.stage_id}: the {self.mode} recovery reserve of {r['total_kg']:.0f} kg is more "
                             f"than half the stage's {propellant_kg:.0f} kg load")
        return {"mode": self.mode, "reserve_kg": r["total_kg"], "hardware_kg": r["hardware_kg"],
                "usable_kg": float(propellant_kg) - r["total_kg"], "burns_kg": r}

    # ------------------------------------------------------------ in flight
    def on_event(self, event, state):
        if self.mode == "none" or self.landed is not None:
            return
        if event.kind == "separation" and event.element == self.stage_id:
            self.landed = budget.draw_landing(self.rng, self.mode)

    def obs(self, state) -> dict:
        if self.mode == "none" or self.landed is None:
            return {}
        return {"booster_recovery": {"mode": self.mode, "landed": bool(self.landed)}}

    # ------------------------------------------------------------ cost
    def credit(self, stage_cost_usd: float) -> dict:
        return budget.reuse_credit_usd(stage_cost_usd, self.mode, landed=bool(self.landed))


MODELS = [RecoveryModel]
