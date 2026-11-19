"""What the flight software sees (navigation) and how its commands are applied.

Methods of Mission, moved verbatim from sim.py. Mission inherits them from GuidanceMixin."""
from __future__ import annotations

import math
import os
import time
import zlib
from dataclasses import dataclass, field
import numpy as np
from .. import data as D
from .. import vibro
from .. import terrain
from ..environment import EARTH_SOI, MARS_SOI, MARS_REF_RADIUS, Dispersions, World
from ..core.common import (  # noqa: F401
    MILESTONES, MILESTONE_WEIGHTS, ATM_TOP, EARTH_R_EQ, PARTIAL_APPROACH_D0, SCENARIO_PHASES, STATE_AFTER, FlightFailure, interp, unit, rotate_towards, newtonian_cd, parachute_peak_load, great_circle_km, ElementState,
)


def check_command(cmd):
    """Raise FlightFailure with a command error for a field of the wrong type, which the simulator would otherwise
    crash on (dev/test_any_design.py: e.g. "next": "soon", "dir": "up", "engines": [1], "actions": 7, "rcs": "on").
    Values float() or int() accept (numbers, numeric strings) pass as before; unknown keys are ignored."""
    def num(d, k, conv=float):   # absent, or a value conv() accepts ("throttle": null crashed float())
        if k not in d or (k == "mixture_ratio" and d[k] is None):
            return True
        try:
            conv(d[k])
            return not isinstance(d[k], (list, dict))
        except (TypeError, ValueError, OverflowError):
            return False

    def vec(x):
        if x is None:
            return True
        try:
            return np.asarray(x, dtype=float).shape == (3,)
        except (TypeError, ValueError):
            return False

    def bad(what):
        raise FlightFailure(f"flight software command error: {what}")

    for k in ("next", "bank_deg"):
        if not num(cmd, k):
            bad(f"{k} must be a number, not {cmd.get(k)!r}")
    if not vec(cmd.get("dir")):
        bad(f"dir must be a list of 3 numbers, not {cmd.get('dir')!r}")
    if not isinstance(cmd.get("actions") or [], list):
        bad(f"actions must be a list of strings, not {cmd.get('actions')!r}")
    eng = cmd.get("engines") or {}
    if not isinstance(eng, dict):
        bad(f"engines must be an object keyed by element id, not {eng!r}")
    for eid, c in eng.items():
        for item in ([c] if isinstance(c, dict) else c if isinstance(c, list) else []):
            if isinstance(item, dict) and not (num(item, "throttle") and num(item, "count", int)
                                               and num(item, "mixture_ratio")):
                bad(f"engines.{eid}: throttle, count and mixture_ratio must be numbers, not {item!r}")
    rcs = cmd.get("rcs") or {}
    if not isinstance(rcs, dict) or not num(rcs, "throttle"):
        bad(f"rcs must be an object with a numeric throttle, not {rcs!r}")
    # attitude is checked field by field in dynamics/rigid_body.py (6-DOF) and ignored in 3-DOF


class GuidanceMixin:
    # ------------------------------------------------------------------ observations
    def nav_sigma(self, alt):
        if self.mode in ("pad", "landed"):
            return 30.0, 0.0
        if self.central == "Earth":
            if alt < 2e6:
                return 5.0, 0.05
            if alt < 2e7:
                return 10.0, 0.01
            return 1000.0, 0.005
        if self.central == "Mars":
            if alt < ATM_TOP["Mars"] and self.mars_entry_started and not self.mars_lifted:
                return None
            if self.mars_lifted and alt < 2e6:
                return 30.0, 0.1
            return 1000.0, 0.01
        return 2000.0, 0.01

    def _nav_filter(self):
        """The navigation filter (models/navigation), made at the first obs with MARSFLIGHT_NAV on, else None."""
        if not hasattr(self, "_navf"):
            from ..models.navigation import switch
            self._navf = None
            if switch.on():
                from ..models.navigation.sensors import NavSensors, dsn_contact
                from ..models.navigation.small_forces import SmallForces
                from .model import FlightContext
                ctx = FlightContext(seed=self.seed, design=getattr(self, "design", None) or {}, vehicle=None,
                                    world=self.disp, truth={}, data_dir="")
                ctx.t_launch = self.t0
                small = SmallForces().configure(ctx)
                self._navf = NavSensors(self.seed, self.disp.nav_scale,
                                        contact=dsn_contact((getattr(self, "w", None) or {}).get("comms")),
                                        small_forces=small)
                self._navf_central = None
                saved = getattr(self, "_nav_restore", None)
                if saved:   # a scenario file saved with the filter's state (core/mission.py _save_state)
                    self._navf.nav.load(saved)
                    self._navf.from_ground = bool(saved.get("from_ground"))
                    self._navf_central = saved.get("central")
        return self._navf

    def ground_nav(self):
        """On the ground: the filter is off, and its next start (at liftoff) uses the ground's position knowledge."""
        nf = self._nav_filter()
        if nf is not None:
            nf.on_ground()

    def update_nav(self, alt):
        nf = self._nav_filter()
        if nf is not None:
            # the filter carries the error from the last obs; DSN passes only in real two-way contact
            if self.mode in ("pad", "landed"):
                nf.on_ground()
                return
            central = self.central
            changed = central != self._navf_central
            self._navf_central = central
            coast = not any(s.lit for s in self.present()) and alt >= ATM_TOP.get(central, 0)
            radius = {"Earth": EARTH_R_EQ, "Mars": MARS_REF_RADIUS}.get(central)
            self.nav_err = nf.observe(self.t, self.x, central, self.gm(central), alt, radius=radius, coast=coast,
                                      mass_kg=self.total_mass(), frame_changed=changed,
                                      entering_mars=central == "Mars" and self.mars_entry_started and not self.mars_lifted)
            return
        sig = self.nav_sigma(alt)
        regime = (self.central, None if sig is None else sig)
        if sig is None:
            return  # EDL: errors from entry interface persist (inertial navigation)
        if regime != self.nav_regime or self.t - self.nav_redraw_t > 86400.0:
            r = self.disp.rng
            sc = self.disp.nav_scale
            self.nav_err = (sc * r.normal(0, sig[0], 3), sc * r.normal(0, sig[1], 3))
            self.nav_regime = regime
            self.nav_redraw_t = self.t

    def trn_fix(self):
        """Terrain-relative navigation: once the heat shield is off, at the first call between TRN h_max_m
        and h_min_m above the ground, the descent camera matches the ground to the map and the navigated
        position error drops to sigma_m per axis. The velocity error stays."""
        if self.x is None or self.central != "Mars" or self.mars_lifted or "trn_fix" in self.track:
            return
        pres = self.present()
        if not any(s.el.trn for s in pres) or any(s.el.kind == "heatshield" for s in pres):
            return
        h = self._agl(self.x, self.t)
        # only on the inertial navigation of entry, descent and landing (see nav_sigma)
        if D.TRN["h_min_m"] <= h <= D.TRN["h_max_m"] and self.nav_sigma(self.altitude(self.x[:3], self.t)) is None:
            nf = self._nav_filter()
            if nf is not None and nf.nav.active:
                self.nav_err = nf.trn(D.TRN["sigma_m"], self.t)   # a Kalman update, the velocity error stays
            else:
                self.nav_err = (self.disp.nav_scale * self.disp.rng.normal(0, D.TRN["sigma_m"], 3), self.nav_err[1])
            self.track["trn_fix"] = self.t
            self.event("trn_fix", height_m=round(h, 1), sigma_m=D.TRN["sigma_m"])

    def obs(self, sensed=None):
        self.trn_fix()
        o = {"t": self.t, "met": self.t - self.t0, "mode": self.mode, "body": self.central,
             "events": self.pending_events, "milestones": list(self.milestones), "command_errors": self.cmd_errors}
        self.pending_events, self.cmd_errors = [], []
        if self.x is not None:
            alt = self.altitude(self.x[:3], self.t) if self.central != "Sun" else float("inf")
            self.update_nav(alt)
            if getattr(self, "_navf", None) is not None and self._navf.nav.active:
                o["nav_sigma"], o["nav_update"] = self._navf.report()   # what the filter says about itself
            er, ev = self.nav_err
            r = self.x[:3] + er
            v = self.x[3:] + ev
            o["r"] = r.tolist()
            o["v"] = v.tolist()
            if self.central != "Sun":
                lat, lon = self.map_latlon(r, self.t)   # geodetic at Earth, areocentric at Mars
                o["lat"], o["lon"] = lat, lon
                a_nav = self.altitude(r, self.t)
                ground = self.ground_alt(self.x[:3], self.t)
                h_true = alt - ground
                if h_true < 20000 and self.mode == "flight":
                    # the radar altimeter measures the height above the ground under the vehicle
                    a_nav = alt + (self.disp.rng.normal(0, 0.01 * max(h_true, 1.0)) if self.disp.nav_scale else 0.0)
                    o["height_above_surface"] = a_nav - ground
                else:
                    o["height_above_surface"] = a_nav - self.ground_alt(r, self.t)
                o["alt"] = a_nav
                if alt < ATM_TOP.get(self.central, 0):
                    air = self.x[3:] - self.omega_cross(self.x[:3])
                    o["airspeed_vector"] = air.tolist()
                    o["airspeed"] = float(np.linalg.norm(air))
                    o["mach"] = self.last.get("mach", 0.0)
                    o["q"] = self.last.get("q", 0.0)
            else:
                o["sun_distance_au"] = float(np.linalg.norm(self.x[:3])) / D.AU
        if self.x is None:
            # On the ground (the pad or the Mars surface) the position is known to 30 m (the manual, "Navigation
            # filter"). update_nav is only reached in flight, so switch the filter off here: it restarts from the
            # ground's knowledge at liftoff instead of carrying its touchdown error through the surface stay (a
            # 458-day-old Mars landing state put the MAV's nav_sigma at 280 to 800 km at liftoff).
            self.ground_nav()
            lat, lon, salt = self.surface_site
            o["surface"] = {"lat": lat, "lon": lon, "alt": salt}
            xs = self.site_state()
            o["r"], o["v"] = xs[:3].tolist(), xs[3:].tolist()
            o["lat"], o["lon"], o["alt"], o["height_above_surface"] = lat, lon, salt, 0.0
        o["attitude"] = None if self.dir is None else self.dir.tolist()
        o["bank_deg"] = (math.degrees(self.bank) + 180.0) % 360.0 - 180.0
        o["sensed_accel"] = None if sensed is None else sensed.tolist()
        o["mass"] = self.total_mass() * (1.0 + (self.disp.rng.normal(0, 0.003) if self.disp.nav_scale else 0.0))
        els = []
        for s in self.present():
            e = s.el
            d = {"id": e.id, "kind": e.kind}
            if e.is_stage:
                d.update({"fuel_kg": s.fuel, "ox_kg": s.ox, "battery_Wh": s.battery_Wh,
                          "engines": [{"engine": en.name, "count": n, "starts_left": en.max_starts - s.starts_used.get(en.name, 0),
                                       "lit": en.name in s.lit, "out": self.engines_out.get((e.id, en.name), 0),
                                       **self.prop_engine_obs(s, en)}   # mixture ratio
                                      for en, n in e.engines]})
            if e.rcs_prop:
                d["rcs_kg"] = s.rcs
            if e.kind == "parachute":
                d["deployed"] = e.id in self.chute_deployed or e.id in self.chute_failed
                d["inflated"] = e.id in self.chute_deployed and self.t >= (self.chute_open.get(e.id) or {}).get("t_fi", -1e30)
                d["failed"] = e.id in self.chute_failed
            els.append(d)
        o["elements"] = els
        # with PROPELLANT_GAUGING on, fuel_kg/ox_kg/rcs_kg are the gauge readings, not the true tank contents
        # (before, prop_obs was never called and the true masses showed each tank's hidden load spread).
        self.prop_obs(o)
        o["samples_loaded"] = self.samples_loaded
        if self.rb is not None:
            self.rb.observe(o)
        if "airspeed_vector" in o and (getattr(self, "sw", None) or {}).get("AIR_DATA"):
            # the air data come from the navigated state, less the wind there, in 3-DOF and 6-DOF alike
            r, v = np.asarray(o["r"], float), np.asarray(o["v"], float)
            air = v - self.omega_cross(r) - self.wind_inertial(r, self.t)
            o["airspeed_vector"], o["airspeed"] = air.tolist(), float(np.linalg.norm(air))
        return o

    def wind_inertial(self, r, t):
        """The world's wind (J2000, m/s) at r: the wind model Tudat reads in the vertical frame (north, east, down),
        turned into J2000. Leaves the Mars density scale's noted height and place as they were."""
        fn = {"Earth": "_earth_wind", "Mars": "_mars_wind"}.get(self.central)
        if fn is None:
            return np.zeros(3)
        lat, lon, _ = self.latlon(r, t)
        w = self.world
        keep = (getattr(w, "_mars_alt_km", None), getattr(w, "_mars_lat", None), getattr(w, "_mars_lon", None))
        n, e, dn = np.ravel(getattr(w, fn)(self.altitude(r, t), math.radians(lon), math.radians(lat), t)).tolist()
        if keep[0] is not None:
            w._mars_alt_km = keep[0]
        if hasattr(w, "_mars_lat"):
            w._mars_lat, w._mars_lon = keep[1], keep[2]
        la, lo = math.radians(lat), math.radians(lon)
        north = np.array([-math.sin(la) * math.cos(lo), -math.sin(la) * math.sin(lo), math.cos(la)])
        east = np.array([-math.sin(lo), math.cos(lo), 0.0])
        down = -np.array([math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)])
        Rm = np.array(self.bodies.get(self.central).rotation_model.inertial_to_body_fixed_rotation(t))
        return Rm.T @ (n * north + e * east + dn * down)

    # ------------------------------------------------------------------ commands
    def apply_actions(self, cmd):
        for a in cmd.get("actions", []) or []:
            if not isinstance(a, str) or ":" not in a:
                self.cmd_errors.append(f"bad action {a!r}")
                continue
            verb, eid = a.split(":", 1)
            st = self.find(eid)
            if verb in ("separate", "deploy", "jettison_ballast") and st is None:
                self.cmd_errors.append(f"{a}: no element {eid} on the vehicle")
                continue
            if verb == "separate":
                if self.rb is not None and st.lit and not self._wants_burn(cmd, st):
                    # 6-DOF: a shutdown in the same command comes before the separation (the thrust then tails off)
                    for name in list(st.lit):
                        del st.lit[name]
                        self.event("shutdown", element=st.el.id, engine=name)
                self.separate(st)
            elif verb == "deploy":
                self.deploy(st)
            elif verb == "jettison_ballast":
                if st.el.kind != "heatshield" or st.el.id in self.ballast_dropped:
                    self.cmd_errors.append(f"{a}: no ballast to drop")
                    continue
                self.ballast_dropped.add(eid)
                st.dry -= st.el.ballast_kg * self._w_ballast_factor(eid)
                self.event("ballast_jettisoned", element=eid)
            elif verb == "isru":
                if st is None or not st.el.is_stage or st.el.ox != "LOX":
                    self.cmd_errors.append(f"{a}: ISRU oxygen needs a stage with a LOX tank")
                else:
                    self.isru_target = eid
            elif verb == "end":
                raise StopIteration
            else:
                self.cmd_errors.append(f"unknown action {verb}")

    @staticmethod
    def _wants_burn(cmd, st):
        """Whether this command keeps any of the stage's engines burning (as engine_groups reads it)."""
        c = (cmd.get("engines") or {}).get(st.el.id)
        items = [c] if isinstance(c, dict) else (c or [])
        return any(isinstance(i, dict) and float(i.get("throttle", 0.0) or 0.0) > 0 and int(i.get("count", 1) or 0) > 0
                   for i in items)
