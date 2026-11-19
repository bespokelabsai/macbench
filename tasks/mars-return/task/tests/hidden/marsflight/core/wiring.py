"""The merged area models, called from the flight loop, each behind a switch.

The area models came in as new files. This module is where the core calls them,
at the hook points their PRs give, so that every call into a model is in one place and each can be switched off
for a before-and-after comparison.

Every switch is read from the environment, ``MARSFLIGHT_<NAME>=1`` (on) or ``=0`` (off); unset, the default below
holds. A switch is on by default when the reference still flies its single worlds 1000, 1001 and 1002 (6-DOF)
without new failures, and off when it needs a reference change first; ``SWITCHES`` names that change. With every
switch off a flight is the same as before this module, to the last digit (``dev/test_wiring_core.py`` and
``results/wire-core/``).

Hidden values (drawn failure times, as-built growth, the weather year) stay inside the models; obs only gets what a
real vehicle would measure or what follows from public geometry and the team's own design.
"""
from __future__ import annotations

import copy
import math
import os

import numpy as np

from .model import Arc, Event, FlightContext

# name: (default, what it switches on, what has to happen before it can be on by default)
SWITCHES = {
    "POWER": (True, "power and thermal: RTG decay (Curiosity MMRTG), arrays by latitude, season, hour and dust "
                     "on Mars, arrays in space with pointing and ageing, the hourly Jezero sink and tank heaters, bus "
                     "survival heaters in space, the battery through the night with its depth of discharge, fade and "
                     "round trip",
              "the reference carries no battery; it needs battery_kWh 0.25 (about 1.2 kg) in reference/vehicle.json"),
    "ECLIPSE": (True, "shadows of Earth and Mars on every coast, and the battery through the longest one",
                "the same 0.25 kWh reference battery (its Earth parking orbit shadows need up to 122 Wh)"),
    "ISRU": (True, "MOXIE-fitted oxygen plant (power, warm-up, ambient and density) in place of 0.75 kg/sol/kW",
             "nothing in the reference: it carries no ISRU plant. Off only until a design with ISRU is flown"),
    # Storage ageing and the cruise burn uplink check are wired at each ignition as
    # MARSFLIGHT_STORAGE_AGEING and MARSFLIGHT_COMMS_CONJUNCTION (models/propulsion/switches.py).
    "COMMS": (True, "DSN contact in obs, EDL data coverage reported (entry_data_lost does not fail the flight), "
                    "telecom mass and power for a comms design field", ""),
    "RANGE": (True, "range safety: launch azimuth limits, the instantaneous impact point in powered ascent and "
                    "spent stage impacts against land within 2,000 km of the pad", ""),
    "LAUNCH_WEATHER": (True, "launch weather from 20 years of pad METARs with unseen_pov 0.15, a 21 day launch "
                              "period and a 1 hour window; a scrub holds the vehicle on the pad 24 h",
                       "the reference's Earth departure must be planned from its real liftoff time"),
    "FAILURES": (True, "in-flight part failures (avionics strings, RCS leaks, battery strings, ISRU "
                        "units, landing radar, separation) with redundancy, faults in obs and the verbs switch_string, "
                        "isolate_rcs",
                 "the reference's fault protection and redundancy (FaultProtection around its Flight)"),
    "FLOWN_FAILURES": (True, "the eight flown-failure replicas as latent flaws, with the verb restart",
                       "the reference's fault protection (touchdown debounce, negative height guard, restart)"),
    "SAMPLE_MASS": (True, "the loaded Orbiting Sample, 11 kg for 30 tubes, in place of 30 kg", ""),
    "SAMPLE_THERMAL": (True, "sample temperature from loading on Mars to Earth landing, +30 C limit",
                       "a sample temperature margin in the reference (it peaks at +30.0 C in the ascent transient)"),
    "PLANETARY_PROTECTION": (True, "Earth avoidance until 7 days out after Mars liftoff, and containment at "
                                    "Earth impact (3,500 g, and without the parachute)",
                             "reference Earth return corrections that miss Earth until the last week"),
    "SRP": (True, "supersonic retropropulsion drag from wind tunnel data (Mach 1 and above) in place of "
                  "exp(-1.2 C_T)", ""),
    "MASS_GROWTH": (True, "hidden as-built mass growth (median 0 with the fit error for modeled parts, the "
                           "S-120A allowance for masses the team enters) in place of the 1% dry mass factor, and the "
                           "weighing test",
                    "the reference S2 resize with a 2% reserve"),
    "ORBIT_DECAY": (True, "drag between the atmosphere interface and 1000 km around Earth and Mars (Mars-DTM, "
                          "the Earth body's atmosphere)", ""),
    "AIR_DATA": (True, "obs airspeed_vector from the navigated state and the wind (it was the true state and "
                       "no wind)", ""),
    # Wiring gaps found by the manual. Each default is the one its model's own switch already had.
    "PROPULSION_FAILURES": (True, "F3 to F7 latent propulsion flaws (models/failures/propulsion_gaps.py): feed "
                                  "line leak, stuck-open regulator with the verb isolate_pressurant, check valve "
                                  "migration (pyro_isolation_valves), stuck check valve, solid motor nozzle erosion "
                                  "and case burst, and their five program office tests", ""),
    "RECOVERY": (True, "booster recovery: the landing draw at first stage separation, booster_recovery in obs", ""),
    "RENDEZVOUS": (True, "rendezvous design checks, rendezvous_design_errors in obs", ""),
}

_FAILURE_MODELS = ("failures_inflight", "failures_flown", "failures_propulsion")
UNSEEN_POV = 0.15         # coordinator decision: weather the field reports do not show, per attempt
LAUNCH_PERIOD_DAYS = 21.0  # coordinator decision
LAUNCH_WINDOW_H = 1.0      # coordinator decision
_TRUE = ("1", "true", "on", "yes")
_FALSE = ("0", "false", "off", "no")


def switch(name: str) -> bool:
    """Whether a wired model is on: MARSFLIGHT_<NAME> if set, else its default."""
    v = os.environ.get("MARSFLIGHT_" + name)
    if v is None or not v.strip():
        return SWITCHES[name][0]
    v = v.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise ValueError(f"MARSFLIGHT_{name}={v!r}: use 1 or 0")


def switches() -> dict:
    return {k: switch(k) for k in SWITCHES}


def _data_dir():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


class _StateOutput:
    """The arc's state history read by name, as models/power/eclipse.py reads an ArcOutput: the craft's position
    relative to the Sun, Mars and Earth and its velocity relative to the central body, from the ephemeris. It needs
    no extra Tudat outputs, so the arc's dependent variables (and its flight) stay as they are."""

    def __init__(self, m, central, ts, sh, max_n=400):
        if len(ts) > max_n:
            idx = np.unique(np.linspace(0, len(ts) - 1, max_n).round().astype(int))
            ts = [ts[i] for i in idx]
        self.times = list(ts)
        rows = {"rel_sun": [], "rel_mars": [], "rel_earth": [], "rel_central_vel": []}
        for t in self.times:
            x = np.asarray(sh[t][:6], float)
            c = m.central_state(central, t) if central != "Sun" else np.zeros(6)
            sun = m.central_state("Sun", t) if central != "Sun" else np.zeros(6)
            # x is relative to the central body; the Sun-centred position of the craft:
            pos_sun = x[:3] + c[:3] - sun[:3]
            rows["rel_sun"].append(pos_sun)
            for name, body in (("rel_mars", "Mars"), ("rel_earth", "Earth")):
                b = m.central_state(body, t)[:3] - m.central_state("Sun", t)[:3]
                rows[name].append(pos_sun - b)
            rows["rel_central_vel"].append(x[3:])
        self._rows = rows
        self._central = central

    def series(self, name):
        return self.times, self._rows[name]

    def __contains__(self, name):
        return name in self._rows and (name != "rel_central_vel" or self._central != "Sun")


class WiringMixin:
    """Methods of Mission. ``self.sw`` (the switches) is set first thing in ``Mission.__init__``; ``self.w`` holds
    the configured models by name, empty when every switch is off."""

    # ------------------------------------------------------------------ setup
    def _w_dry(self, e):
        """The dry mass an element flies with. With MASS_GROWTH on, an element with a hidden growth draw flies at its
        basic mass plus that growth, which replaces the 1% as-built factor; without one (local runs of elements
        nobody weighed) it keeps the factor."""
        f = self.disp.dry_mass_factor.get(e.id, 1.0)
        if self.sw.get("MASS_GROWTH"):
            g = (self.truth.get("mass_growth") or {}).get(e.id)
            if g is not None:
                return e.dry_mass + float(g.get("growth_kg", 0.0))
        return e.dry_mass * f

    def _w_ballast_factor(self, eid):
        if self.sw.get("MASS_GROWTH") and eid in (self.truth.get("mass_growth") or {}):
            return 1.0
        return self.disp.dry_mass_factor.get(eid, 1.0)

    def _wire_setup(self):
        """Once per flight, after the element states exist and before the 6-DOF body is built (so the bodies see the
        mass the models add)."""
        sw = self.sw
        self.w = {}
        self.w_log = {}
        self.decay = None
        if sw["ORBIT_DECAY"]:
            from ..models.orbit_decay.decay import OrbitDecay
            self.decay = OrbitDecay(bodies=self.bodies)
        if not any(sw.get(k) for k in SWITCHES if k not in ("MASS_GROWTH", "SAMPLE_MASS", "AIR_DATA", "ORBIT_DECAY")):
            return
        ctx = FlightContext(seed=self.seed, design=self.design, vehicle=self.vehicle, world=self.disp,
                            truth=self.truth, data_dir=_data_dir(), six_dof=self.dynamics == "6dof")
        ctx.t_launch = self.t0
        ctx.record = (self.truth or {}).get("failure_record") or {}
        self.wctx = ctx
        w = self.w
        items = []
        if sw["COMMS"]:
            from ..models.comms.model import CommsModel
            w["comms"] = CommsModel()
            w["comms"].configure(ctx)
            items += w["comms"].mass_items(self.vehicle)
        if sw["RANGE"] or sw["LAUNCH_WEATHER"]:
            from ..models.range.model import RangeModel
            w["range"] = RangeModel()
            w["range"].configure(ctx)
            # Coordinator decisions: one launch period and window for every design, and the weather the
            # field reports do not see. The pad is the design's launch.site, one of the published pads (data.LAUNCH_SITES,
            # checked in vehicle.py), LC-39A unless the design names another; its azimuth limits are the range's.
            from .. import data as D
            w["range"].site = D.launch_site_id(self.design) if D.launch_site_id(self.design) in D.LAUNCH_SITES \
                else D.DEFAULT_LAUNCH_SITE
            w["range"].unseen_pov = UNSEEN_POV
            w["range"].period_days = LAUNCH_PERIOD_DAYS
            w["range"].window_h = LAUNCH_WINDOW_H
        if sw["FAILURES"]:
            from ..models.failures.inflight import InFlightFailures
            w["failures_inflight"] = InFlightFailures()
            w["failures_inflight"].configure(ctx)
            items += w["failures_inflight"].mass_items(self.vehicle, self.design)
        if sw["FLOWN_FAILURES"]:
            from ..models.failures.flown import FlownFailures
            w["failures_flown"] = FlownFailures()
            w["failures_flown"].configure(ctx)
        if sw["PROPULSION_FAILURES"]:
            from ..models.failures.propulsion_gaps import PropulsionGapFailures
            w["failures_propulsion"] = PropulsionGapFailures(enabled_override=True)
            w["failures_propulsion"].configure(ctx)
        if sw["RECOVERY"]:
            from ..models.recovery.model import RecoveryModel
            rec = RecoveryModel()
            try:
                rec.configure(ctx)
                w["recovery"] = rec
            except ValueError as ex:      # a bad recovery field: vehicle.py reports design errors; keep flying
                self.w_log["recovery_design_errors"] = str(ex)
        if sw["RENDEZVOUS"]:
            import os as _os
            from ..models.rendezvous import Rendezvous
            keep = _os.environ.get("MARSFLIGHT_RENDEZVOUS")
            _os.environ["MARSFLIGHT_RENDEZVOUS"] = "1"     # the model reads its own switch; this one decides
            try:
                w["rendezvous"] = Rendezvous()
                w["rendezvous"].configure(ctx)
            finally:
                if keep is None:
                    _os.environ.pop("MARSFLIGHT_RENDEZVOUS", None)
                else:
                    _os.environ["MARSFLIGHT_RENDEZVOUS"] = keep
        if sw["POWER"]:
            from ..models.thermal.surface import SurfaceThermal
            w["surface_thermal"] = SurfaceThermal()
            w["surface_thermal"].configure(ctx)
            self.array_dust = 0.0
            self.battery_cycles = 0.0
        if sw["ECLIPSE"]:
            from ..models.power.eclipse import EclipsePower
            w["eclipse"] = EclipsePower()
            w["eclipse"].configure(ctx)
        if sw["ISRU"]:
            from ..models.isru.moxie import MoxieIsru
            w["isru"] = MoxieIsru()
            w["isru"].configure(ctx)
        if sw["SAMPLE_THERMAL"]:
            from ..models.samples.thermal import SampleThermal
            w["sample_thermal"] = SampleThermal()
            w["sample_thermal"].configure(ctx)
        if sw["PLANETARY_PROTECTION"]:
            from ..models.samples.protection import PlanetaryProtection
            w["protection"] = PlanetaryProtection()
            w["protection"].configure(ctx)
        if sw["SRP"]:
            from ..models.plumes.srp import SupersonicRetropropulsion
            w["srp"] = SupersonicRetropropulsion()
            w["srp"].configure(ctx)
        for it in items:
            st = self.find(it.element)
            if st is not None and it.mass:
                st.dry += float(it.mass)
                self.w_log.setdefault("mass_items", []).append({"element": it.element, "item": it.name,
                                                               "kg": round(float(it.mass), 2)})

    # ------------------------------------------------------------------ hooks in the loop
    def _w_before_obs(self):
        for name in _FAILURE_MODELS:
            if name in self.w:
                self.w[name].before_obs(self)

    def _w_sense(self, o):
        """Fields the models add to obs, and the sensors a fault corrupts."""
        w = self.w
        if not w:
            return o
        if "comms" in w:
            try:
                o.update(w["comms"].obs(self))
            except Exception as ex:  # noqa: BLE001  (a geometry error must not end a flight)
                self.w_log.setdefault("comms_obs_errors", []).append(f"{type(ex).__name__}: {ex}")
        if "range" in w and w["range"].azimuth is not None:
            o.update(w["range"].obs(self))
        if "sample_thermal" in w:
            o.update(w["sample_thermal"].obs(self))
        # manual gaps: the backshell heat load, the booster landing, the rendezvous design checks
        bs = getattr(self, "_backshell_model", None)
        if bs is not None:
            o.update(bs.obs(self))
        for name in ("recovery", "rendezvous"):
            if name in w:
                o.update(w[name].obs(self))
        for name in _FAILURE_MODELS:
            if name in w:
                o = w[name].sense(o, self)
        return o

    def _w_command(self, cmd):
        """The failure models' own verbs, and commands blocked by a fault. Returns the command the vehicle acts on."""
        new = cmd
        for name in _FAILURE_MODELS:
            if name in self.w:
                new = self.w[name].command(new, self)
        if "failures_propulsion" in self.w and any(isinstance(a, str) and a.startswith("isolate_pressurant:")
                                                   for a in new.get("actions") or []):
            # the verb is always accepted, flaw or not, so that its answer does not tell whether a flaw is there
            new = dict(new, actions=[a for a in new.get("actions") or []
                                     if not (isinstance(a, str) and a.startswith("isolate_pressurant:"))])
        if new is not cmd:
            cmd.clear()
            cmd.update(new)
        return cmd

    def _w_after_step(self, t_before, sensed):
        if "failures_inflight" in self.w:
            self.w["failures_inflight"].after_step(self, t_before)
        if "failures_flown" in self.w:
            self.w["failures_flown"].after_step(self, t_before, sensed)
        if "failures_propulsion" in self.w:
            self.w["failures_propulsion"].after_step(self, t_before, sensed)

    def _w_event(self, kind, kw):
        """Called by Mission.event for every event."""
        w = self.w
        if "recovery" in w and kind == "separation":
            w["recovery"].on_event(Event(kind=kind, t=self.t, element=kw.get("element")), self)
        if kind == "liftoff" and kw.get("body") == "Mars" and "sample_thermal" in w:
            self._w_samples(w["sample_thermal"].ascent())
        elif kind == "milestone" and kw.get("name") == "mars_touchdown" and "comms" in w:
            self._w_edl_report()

    def _w_samples(self, msg):
        if msg:
            self.w_log["samples_lost"] = msg
            self.fail(msg)

    # ------------------------------------------------------------------ launch range and weather
    def _w_countdown(self, cmd):
        """On the Earth pad, before the engines start: the pad's weather decides when the launch goes. A scrub holds
        the vehicle on the pad until the next attempt (the engines are not started); the flight software sees each
        scrub as an event and the hold as a command error, and commands the ignition again."""
        r = self.w.get("range")
        if r is None or not self.sw["LAUNCH_WEATHER"] or self.mode != "pad" or self.central != "Earth":
            return 0.0
        wants = any(isinstance(i, dict) and float(i.get("throttle", 0.0) or 0.0) > 0
                    for c in (cmd.get("engines") or {}).values() for i in ([c] if isinstance(c, dict) else (c or [])))
        if not wants:
            return 0.0
        cd = self.w_log.get("countdown")
        if cd is None:
            res = r.countdown(self.t)
            cd = self.w_log["countdown"] = {"t_launch": res.get("t_launch"), "lost": bool(res.get("lost")),
                                            "scrubs": res.get("scrubs", []), "reason": res.get("reason")}
            for s in cd["scrubs"]:
                self.event("launch_scrubbed", utc=s["utc"], reasons=s["reasons"])
        if cd["lost"]:
            self.fail(cd["reason"])
        if cd["t_launch"] is not None and cd["t_launch"] > self.t + 1e-6:
            hold = cd["t_launch"] - self.t
            self.cmd_errors.append(f"launch held on the pad by weather: next attempt in {hold / 3600:.1f} h "
                                   f"({len(cd['scrubs'])} scrubs)")
            cmd["engines"] = {}
            return hold
        return 0.0

    def _w_range_after_arc(self, alt):
        r = self.w.get("range")
        if r is None or not self.sw["RANGE"]:
            return
        why = r.check_ascent(self.x[:3], self.x[3:], self.t, alt, powered=any(s.lit for s in self.present()))
        if why:
            self.fail(why)

    def _w_range_stage(self, e):
        r = self.w.get("range")
        if r is None or not self.sw["RANGE"] or self.x is None or self.central != "Earth" \
                or "earth_departure" in self.milestones:
            return
        why = r.check_stage(self.x[:3], self.x[3:], self.t, e.id)
        if why:
            self.fail(why)

    # ------------------------------------------------------------------ communications
    def _w_ignition_uplink(self, cmd):
        """A correction burn in heliocentric cruise needs an uplink: engine starts the ground could not command are
        taken out of the command, with the reason as a command error. Engines already lit are not touched."""
        c = self.w.get("comms")
        if c is None or self.central != "Sun":
            return
        eng = cmd.get("engines") or {}
        for eid in list(eng):
            st = self.find(eid)
            if st is None:
                continue
            items = eng[eid] if isinstance(eng[eid], list) else [eng[eid]]
            keep = []
            for it in items:
                if not isinstance(it, dict):
                    keep.append(it)
                    continue
                name = it.get("engine") or (st.el.engines[0][0].name if st.el.engines else None)
                starting = float(it.get("throttle", 0.0) or 0.0) > 0 and name not in st.lit
                why = c.check_ignition(self, f"{eid}/{name}") if starting else None
                if why:
                    self.cmd_errors.append(why)
                else:
                    keep.append(it)
            if len(keep) != len(items):
                eng[eid] = keep

    def _w_edl_sample(self, alt):
        c = self.w.get("comms")
        if c is None or self.central != "Mars" or self.mars_lifted or alt > 130e3 or self.x is None:
            return
        air = self.x[3:] - self.omega_cross(self.x[:3])
        c.edl_sample(self.t, self.x[:3], float(np.linalg.norm(air)), alt)

    def _w_edl_report(self):
        c = self.w.get("comms")
        if c is None or "edl" in self.w_log or not c.edl:
            return
        orbiters = []   # elements left in Mars orbit with a UHF radio (none fly separately yet)
        try:
            ev = c.edl_report(orbiters)
        except Exception as ex:  # noqa: BLE001
            self.w_log["edl"] = {"error": f"{type(ex).__name__}: {ex}"}
            return
        if ev:
            self.w_log["edl"] = dict(ev)
            kind = ev.pop("kind")
            self.event(kind, **ev)   # entry_data_lost is reported; it does not fail the flight (coordinator)

    # ------------------------------------------------------------------ samples and planetary protection
    def _w_sample_kg(self):
        """The samples loaded into the capsule on Mars: its sample containers, each a loaded Orbiting
        Sample of about 11 kg, or the older 30 kg with the switch off."""
        if self.sw.get("SAMPLE_MASS"):
            from ..models.samples.manifest import payload_mass
            return next((s.el.sample_mass for s in self.states if s.el.kind == "capsule" and s.el.sample_mass > 0),
                        payload_mass())
        from .. import data as D
        return D.SAMPLE_MASS_KG

    def _w_samples_loaded(self):
        if "sample_thermal" in self.w:
            self.w["sample_thermal"].loaded(self.disp.mars_ambient_T)

    def _w_protection_after_arc(self, central, x1, t1):
        p = self.w.get("protection")
        if p is None or not (self.mars_lifted and self.samples_loaded) or central not in ("Sun", "Earth"):
            return
        if "earth_arrival" in self.milestones and central == "Earth" and "protection_done" in self.w_log:
            return

        def earth_state(t):
            e = self.central_state("Earth", t) - self.central_state("Sun", t)
            return e[:3], e[3:]
        msg = p.after_arc(t1, central, np.asarray(x1[:3]), np.asarray(x1[3:]), earth_state if central == "Sun" else None)
        if msg:
            self.fail(msg)

    def _w_earth_touchdown(self, lead, impact_g, ground):
        """Earth landing: the samples' soak-back rise and containment (called by LandingMixin.touchdown)."""
        st = self.w.get("sample_thermal")
        if st is not None:
            shield = lead.id
            Q = self.heat_load.get(shield, 0.0) / 1e4
            cap = (self.track.get("heat_capacity_Jcm2") or {}).get(shield)
            if cap:
                self._w_samples(st.earth_entry(Q, cap))
        p = self.w.get("protection")
        if p is not None:
            impact_g = float(impact_g or 0.0)   # off the lakebed no impact_g is computed; the no-parachute case still is
            from .. import data as D
            rho = 1.225 * math.exp(-max(0.0, ground) / 8500.0)
            fail, sci, detail = p.at_touchdown(impact_g, lead.landing, self.total_mass(), lead.diameter, D.BLUFF_CD, rho)
            self.w_log["containment"] = detail
            if sci:
                self.w_log["samples_science_lost"] = sci
                self.event("samples_science_lost", reason=sci)
            if fail:
                self.fail(fail)

    # ------------------------------------------------------------------ power, thermal, ISRU, eclipses
    def _w_space_arc_power(self, central, ts, sh, t0, dt, thrusting, thrust_dir):
        """After a coast or burn out of the atmosphere: the arc's lit fraction (eclipse model), kept for the power
        balance that follows, then the battery through the arc's longest shadow."""
        e = self.w.get("eclipse")
        self.w_arc = {"thrusting": bool(thrusting), "dir": None if thrust_dir is None else np.asarray(thrust_dir, float),
                      "lit": 1.0}
        if e is None or len(ts) < 2:
            return None
        out = _StateOutput(self, central, ts, sh)
        arc = Arc(t0=t0, dt=dt, central=central, body="", in_atmosphere=False, thrusting=bool(thrusting),
                  six_dof=self.rb is not None)
        try:
            sh_ = e.arc_shadow(arc, out)
        except Exception as ex:  # noqa: BLE001
            self.w_log.setdefault("eclipse_errors", []).append(f"{type(ex).__name__}: {ex}")
            return None
        self.w_arc["lit"] = float(sh_["lit"])
        self.w_arc["arc"], self.w_arc["out"] = arc, out
        return arc

    def _w_eclipse_check(self):
        e = self.w.get("eclipse")
        a = getattr(self, "w_arc", None) or {}
        if e is None or "arc" not in a:
            return
        e.on_arc_end(a["arc"], a["out"], self)
        if e.log:
            self.w_log["eclipse_last"] = e.log[-1]
            self.w_log["eclipse_worst"] = max(e.log, key=lambda r: r["need_Wh"] - r["usable_Wh"])

    def _w_years(self):
        return max(0.0, (self.t - self.t0) / (365.25 * 86400.0))

    def _w_sun_dir(self):
        """Unit vector from the vehicle to the Sun (J2000)."""
        if self.x is None:
            r = self.central_state(self.central, self.t)[:3] - self.central_state("Sun", self.t)[:3]
        elif self.central == "Sun":
            r = self.x[:3]
        else:
            r = self.x[:3] + self.central_state(self.central, self.t)[:3] - self.central_state("Sun", self.t)[:3]
        return -r / max(float(np.linalg.norm(r)), 1.0)

    def _w_landing_lat(self):
        return float(self.surface_site[0]) if self.surface_site else 18.4447

    def _w_result(self, r):
        w = self.w
        for name in _FAILURE_MODELS:
            if name in w:
                r.setdefault("failure_models", {})[w[name].name] = w[name].summary()
        log = dict(self.w_log)
        if "protection" in w and w["protection"].closest is not None:
            t, t_ca, alt = w["protection"].closest
            log["earth_closest"] = {"t": t, "t_to_closest_s": t_ca, "perigee_alt_m": alt}
        if "sample_thermal" in w and w["sample_thermal"].node is not None:
            n = w["sample_thermal"].node
            log["sample_temperature"] = {"now_C": round(n.T, 2), "peak_C": round(n.peak, 2), "peak_phase": n.peak_phase}
        if "srp" in w:
            log["srp"] = {"peak_ct": round(w["srp"].peak_ct, 3), "samples_outside_data": w["srp"].outside}
        if "range" in w and w["range"].azimuth is not None:
            log["launch_azimuth_deg"] = round(w["range"].azimuth, 2)
        if "range" in w:
            log["launch_site"] = w["range"].site
        if "recovery" in w and w["recovery"].landed is not None:
            log["booster_recovery"] = {"mode": w["recovery"].mode, "landed": bool(w["recovery"].landed)}
        r["wired"] = {"on": sorted(k for k, v in self.sw.items() if v), "log": _plain(log)}
        return r


def _plain(x):
    """JSON-safe copy (numpy numbers to float)."""
    if isinstance(x, dict):
        return {str(k): _plain(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_plain(v) for v in x]
    if isinstance(x, (np.floating, np.integer)):
        return x.item()
    if isinstance(x, np.ndarray):
        return x.tolist()
    return x
