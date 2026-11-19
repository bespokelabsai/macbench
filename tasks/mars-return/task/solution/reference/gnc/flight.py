"""Top-level state machine of the reference flight software."""
from __future__ import annotations

import os

from .log import log
from .vehicle_model import VehicleModel

DRIFT_S = 300.0   # s: a coast at least this long drifts in 6-DOF (see Flight.step)


class Flight:
    def __init__(self, info):
        self.info = info
        self.model = VehicleModel(info)
        self.t0 = info["t0"]
        self.sites = info["sites"]
        self.scenario = info.get("scenario")
        self.nominal = info.get("seed_is_nominal", False)
        self.stop_after = os.environ.get("MF_STOP_AFTER")   # dev: end the flight after this milestone
        self.handler = None
        self.calls = 0
        log("reset", t0=self.t0, scenario=self.scenario, t=info.get("t"))
        self._start()
        # heavy one-time setup here, not inside a step (each call has a 20 s limit on one core)
        import time as _time
        t_w = _time.time()
        from . import predict as P
        P.bodies()
        log("reset_setup", dt=_time.time() - t_w)

    def _start(self):
        sc = self.scenario
        if sc is None or sc == "earth_orbit":
            from .earth import EarthAscent
            self.handler = EarthAscent(self)
            if sc == "earth_orbit":
                self.handler.phase = "park"
                self.handler.make_plan(self.t0)
        elif sc in ("cruise_to_mars", "mars_approach"):
            from .cruise import Cruise
            self.handler = Cruise(self)
        elif sc == "mars_surface":
            from .mars_ascent import MarsSurface
            self.handler = MarsSurface(self)
        elif sc == "mars_orbit":
            # the state is saved at the mars_orbit milestone, during the MAV2 insertion burn
            from .mars_ascent import MarsAscent, MarsSurface
            surf = MarsSurface(self)
            surf.make_plan()
            self.handler = MarsAscent(self, surf.plan)
            self.handler.phase = "ascent"
        elif sc in ("cruise_to_earth", "earth_approach"):
            from .cruise import Cruise
            self.handler = Cruise(self, leg="back")
        else:
            raise RuntimeError(f"scenario {sc} not supported yet")

    def step(self, obs):
        import time as _time
        self.calls += 1
        if self.stop_after and self.stop_after in obs.get("milestones", []):
            return {"actions": ["end:mission"]}
        t_wall = _time.time()
        cmd = self.handler.step(obs)
        if cmd is None:
            # the handler finished without a command: hand over and let the next one answer now
            nxt = self.handler.next_handler()
            log("handover", frm=type(self.handler).__name__, to=type(nxt).__name__, t=obs["t"])
            self.handler = nxt
            cmd = self.handler.step(obs)
        if obs.get("dynamics") == "6dof" and obs.get("mode") == "flight" and isinstance(cmd, dict) and \
                not any(k in cmd for k in ("dir", "engines", "rcs", "attitude", "bank_deg", "settle")) and cmd.get("next", 0.0) >= DRIFT_S:
            # 6-DOF: a long coast drifts on a zero rate command instead of holding a direction, which the RCS pays
            # for in a limit cycle; each burn turns to its direction in a coast arc first
            cmd["attitude"] = {"rate_deg_s": [0.0, 0.0, 0.0]}
        dt_wall = _time.time() - t_wall
        if dt_wall > 0.5:
            log("slow_call", handler=type(self.handler).__name__, phase=getattr(self.handler, "phase", None), dt=dt_wall, t=obs["t"])
        if getattr(self.handler, "done", False):
            nxt = self.handler.next_handler() if hasattr(self.handler, "next_handler") else None
            if nxt is not None and getattr(nxt, "immediate", False):
                pass
            if nxt is None:
                from .cruise import Cruise
                nxt = Cruise(self)
            log("handover", frm=type(self.handler).__name__, to=type(nxt).__name__, t=obs["t"])
            self.handler = nxt
        return cmd
