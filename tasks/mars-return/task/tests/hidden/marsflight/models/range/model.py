"""The launch range model the core calls. Not wired in yet: the hooks below say where.

Design fields (mission level, all optional):

    "launch": {"site": "LC-39A", "window_h": 1.0, "period_days": 21, "ground_wind_limit_ms": 17.5}

- ``countdown(t0)``: before liftoff. Replays the pad's real weather from the planned launch time and returns
  the time the launch goes (the core moves ``t0`` there) or the reason the launch period closed. Each scrub is
  an event the team sees.
- ``check_ascent(state)``: after each Earth ascent arc. Once the vehicle is above 5 km its ground-track heading
  is the launch azimuth, checked once against the range's limits; in powered flight the instantaneous impact
  point must not fall on land within 2,000 km of the pad.
- ``check_stage(r, v, t, element)``: at a separation during Earth ascent. A spent stage that would fall on land
  ends the flight: the range would not have approved the trajectory.

Each check returns None or the reason, which the core turns into ``fail(reason)``.
"""
from __future__ import annotations

import math

import numpy as np

from ...core.model import Model
from . import safety, weather

AZIMUTH_CHECK_ALT_M = 5000.0


def earth_rotation(t):
    from ..comms.dsn import rotation
    return rotation("IAU_Earth", "J2000", t)


class RangeModel(Model):
    name = "range_safety"
    area = "range"
    order = 5

    def configure(self, ctx):
        self.ctx = ctx
        la = (ctx.design or {}).get("launch") or {}
        self.site = la.get("site", "LC-39A")
        if self.site not in safety.sites()["sites"]:
            raise ValueError(f"launch site {self.site!r} is not one of {sorted(safety.sites()['sites'])}")
        self.window_h = float(la.get("window_h", 1.0))
        self.period_days = float(la.get("period_days", 21.0))
        self.wind_limit = la.get("ground_wind_limit_ms")
        self.unseen_pov = float(la.get("_unseen_pov", 0.0))   # set by the core, not the team (see weather.py)
        self.azimuth = None
        self.rng = ctx.rng(self.name)
        self.winds = self._winds(ctx)

    @staticmethod
    def _winds(ctx):
        prov = getattr(ctx, "providers", None) or {}
        if "winds.earth" in prov:
            return prov["winds.earth"]
        try:
            from ...core.registry import registry
            cls = registry.provider("winds.earth")
        except Exception:  # noqa: BLE001
            return None
        if cls is None:
            return None
        p = cls()
        p.configure(ctx)
        return p

    def design_fields(self):
        return ["launch.site", "launch.window_h", "launch.period_days", "launch.ground_wind_limit_ms"]

    # ------------------------------------------------------------ before liftoff
    def countdown(self, t0):
        res = weather.countdown(self.site, weather.utc(t0), self.rng, self.window_h, self.period_days,
                                winds=self.winds, wind_limit_ms=self.wind_limit, unseen_pov=self.unseen_pov)
        if res["launch_utc"] is not None:
            res["t_launch"] = t0 + (res["launch_utc"] - weather.utc(t0)).total_seconds()
        else:
            res["reason"] = (f"launch period of {self.period_days:g} days closed after {len(res['scrubs'])} weather "
                             f"scrubs at {safety.sites()['sites'][self.site]['name']}")
        return res

    # ------------------------------------------------------------ ascent
    def check_ascent(self, r, v, t, alt, powered):
        R = earth_rotation(t)
        if self.azimuth is None and alt >= AZIMUTH_CHECK_ALT_M:
            rf, vf = safety.fixed_from_inertial(r, v, R)
            lat, lon = safety.latlon(rf)
            self.azimuth = safety.heading_deg(lat, lon, vf)
            why = safety.check_azimuth(self.site, self.azimuth)
            if why:
                return why
        if powered:
            return safety.check_impact(self.site, safety.iip(r, v, R))
        return None

    def check_stage(self, r, v, t, element):
        return safety.check_impact(self.site, safety.iip(r, v, earth_rotation(t)),
                                   what=f"spent stage {element} impact point")

    def obs(self, state):
        return {"launch_azimuth_deg": None if self.azimuth is None else round(self.azimuth, 2)}


def inclination_deg(lat_deg, azimuth_deg):
    """Orbit inclination reached by a launch at an inertial azimuth: cos i = sin(Az) cos(lat)."""
    return math.degrees(math.acos(math.sin(math.radians(azimuth_deg)) * math.cos(math.radians(lat_deg))))


MODELS = [RangeModel]
