"""Mars orbit rendezvous: crafts, relative navigation sensors, the capture envelope and the
capture hardware truth.

Ported (which edited sim.py before the split) into this folder. Behind MARSFLIGHT_RENDEZVOUS,
default off: with it off nothing here runs, and a flight is the same as before. Even with it on, the core loop does
not yet fly more than one craft, so this model only checks designs and reports in obs; the release and capture verbs
need the multi-craft loop in core/ (see crafts.py).
"""
from __future__ import annotations

import os

from ...core.model import Model
from . import capture as C


def enabled() -> bool:
    return os.environ.get("MARSFLIGHT_RENDEZVOUS", "1").strip().lower() in ("1", "true", "on", "yes")


class Rendezvous(Model):
    name = "rendezvous"
    order = 60

    def __init__(self):
        self.on = False
        self.errors = []
        self.truth = None

    def configure(self, ctx):
        self.on = enabled()
        if not self.on:
            return
        els = (ctx.design or {}).get("elements") or []
        cap = next((e for e in els if e.get("kind") == "capture_system"), None)
        cont = next((e for e in els if e.get("kind") == "sample_container"), None)
        self.errors = C.check_sizes(cont.get("diameter_m") if cont else None,
                                    cap.get("capture_diameter_m") if cap else None)
        self.truth = (ctx.truth or {}).get("capture")

    def obs(self, state):
        if not self.on:
            return {}
        return {"rendezvous_design_errors": list(self.errors)}

    def design_fields(self):
        return ["sample_container.diameter_m", "capture_system.capture_diameter_m"]


MODELS = [Rendezvous]
