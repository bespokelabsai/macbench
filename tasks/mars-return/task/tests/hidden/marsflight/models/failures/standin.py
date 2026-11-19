"""A stand-in for the core hooks until the core wires the failure models into the flight loop.

``FailureMission`` is ``sim.Mission`` with four calls added, exactly where the core will make them:

- ``before_obs(mission)``: apply the faults that are due (start of ``Mission.obs``);
- ``sense(obs, mission)``: telemetry of the faults, and sensors that a fault corrupts (end of ``Mission.obs``);
- ``command(cmd, mission)``: the models' own actions and blocked commands (start of ``Mission.apply_actions``,
  so ``Mission.step`` acts on the same filtered command);
- ``after_step(mission, t_before, sensed)``: leaks and load checks (end of ``Mission.step``).

It also adds each model's ``mass_items`` to the element's dry mass and flies a private copy of the vehicle,
because faults change element fields (RCS thrust, battery and ISRU capacity). ``sim.py`` is not edited.
"""
from __future__ import annotations

import copy

from ...core.model import FlightContext
from .flown import FlownFailures
from .inflight import InFlightFailures

_CLS = None


def FailureMission(vehicle, design, guidance, models=None, record=None, **kw):
    """Build the stand-in mission. sim is imported here, not at module import, because the registry imports
    every module under models/ and the core will import the registry."""
    global _CLS
    if _CLS is None:
        from ...core.mission import Mission
        # Copy the hooks, not the class's own __dict__/__weakref__ slots, which clash with Mission's.
        _CLS = type("FailureMission", (Mission,),
                    {k: v for k, v in _Hooks.__dict__.items() if k not in ("__dict__", "__weakref__")})
    return _CLS(vehicle, design, guidance, models=models, record=record, **kw)


class _Hooks:
    def __init__(self, vehicle, design, guidance, models=None, record=None, **kw):
        vehicle = copy.deepcopy(vehicle)
        super(_CLS, self).__init__(vehicle, design, guidance, **kw)
        self.fmodels = models if models is not None else [InFlightFailures(), FlownFailures()]
        ctx = FlightContext(seed=kw.get("seed"), design=design, vehicle=vehicle, world=self.disp, truth=self.truth,
                            data_dir="", six_dof=self.dynamics == "6dof")
        ctx.record = record or {}
        ctx.t_launch = self.t0
        for mdl in self.fmodels:
            mdl.configure(ctx)
            items = mdl.mass_items(vehicle, design) if isinstance(mdl, InFlightFailures) else mdl.mass_items(vehicle)
            for it in items:
                st = self.find(it.element)
                if st is not None:
                    st.dry += it.mass

    def obs(self, sensed=None):
        for mdl in self.fmodels:
            mdl.before_obs(self)
        o = super(_CLS, self).obs(sensed)
        for mdl in self.fmodels:
            o = mdl.sense(o, self)
        return o

    def apply_actions(self, cmd):
        new = cmd
        for mdl in self.fmodels:
            new = mdl.command(new, self)
        if new is not cmd:
            cmd.clear()
            cmd.update(new)
        return super(_CLS, self).apply_actions(cmd)

    def step(self, cmd, sensed):
        t_before = self.t
        out = super(_CLS, self).step(cmd, sensed)
        for mdl in self.fmodels:
            if isinstance(mdl, FlownFailures):
                mdl.after_step(self, t_before, out)
            else:
                mdl.after_step(self, t_before)
        return out

    def result(self):
        r = super(_CLS, self).result()
        r["failure_models"] = {mdl.name: mdl.summary() for mdl in self.fmodels}
        return r
