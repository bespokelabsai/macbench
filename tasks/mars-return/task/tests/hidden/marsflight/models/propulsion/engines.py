"""Engines: lit groups, thrust, Isp, liftoff and propellant use.

Methods of Mission, moved verbatim from sim.py. Mission inherits them from PropulsionMixin.

Wired in by the propulsion wiring PR, each behind a switch (models/propulsion/switches.py, MARSFLIGHT_<NAME>=0/1):
- engine start and shutdown transients, chilldown, mixture ratio and throttle Isp (ENGINE_TRANSIENTS),
- turbopump power balance, spin-up and start-by-start combustion stability (TURBOPUMP),
- with the transients: the nozzle separation check takes the atmosphere's pressure (NOZZLE_SEPARATION_PRESSURE),
- propellant gauging, load spread, low level cutoff and running dry (PROPELLANT_GAUGING),
- propulsion ageing in storage, at each ignition (STORAGE_AGEING),
- the cruise burn conjunction and uplink check, at each ignition (COMMS_CONJUNCTION).
With every switch off the methods do exactly what they did before. The hooks the core calls are the methods named
prop_* below; until the core calls them, prop_init runs at the first engine_groups call.
"""
from __future__ import annotations

import math
import os
import time
import zlib
from dataclasses import dataclass, field
import numpy as np
from ... import data as D
from ... import vibro
from ... import terrain
from ...environment import EARTH_SOI, MARS_SOI, MARS_REF_RADIUS, Dispersions, World
from ...core.common import (  # noqa: F401
    MILESTONES, MILESTONE_WEIGHTS, ATM_TOP, EARTH_R_EQ, PARTIAL_APPROACH_D0, SCENARIO_PHASES, STATE_AFTER, FlightFailure, interp, unit, rotate_towards, newtonian_cd, parachute_peak_load, great_circle_km, ElementState,
)
from . import switches as SW
from .engine import CHILL_RECIRC_S, CHILL_RECIRC_WH, chilldown_loss_s, profile_integral, profile_value

TRANSIENT_STEP_S = 0.05     # integrator step while an engine starts or tails off (core/arcs.py uses it)


def _running(s):
    """Engine name -> what it was last commanded (count, throttle, mixture ratio, factors, first thrust time)."""
    r = s.__dict__.get("running")
    if r is None:
        r = s.running = {}
    return r


def _batches(s):
    """Engine name -> its lit engines, one batch per ignition: [count, first thrust time, the installed
    engines' indices, start profile time]. A batch is valid only while the engine name is in s.lit."""
    r = s.__dict__.get("lit_batches")
    if r is None:
        r = s.lit_batches = {}
    return r


def _last_cold(s):
    """Engine name -> when it was last cold (its last cutoff)."""
    r = s.__dict__.get("last_cold")
    if r is None:
        r = s.last_cold = {}
    return r


class PropulsionMixin:
    # ------------------------------------------------------------------ setup (wiring PR)
    def prop_init(self):
        """Once per flight: read the switches and set up the wired models. The core should call it at the end of
        Mission.__init__, before _apply_scenario, so that the load spread is in the tanks before the first obs."""
        if "_prop_cfg" in self.__dict__:
            return self._prop_cfg
        cfg = SW.all_switches()
        self._prop_cfg = cfg
        self.__dict__.setdefault("tails", [])            # engines tailing off after a cutoff
        # the first liftoff; upper stage engines were last chilled on the pad. A scenario starts its engines as if
        # they were chilled at its start time.
        self.__dict__.setdefault("liftoff_t", self.t if self.scenario else None)
        # Each cutoff's impulse spread, and each start's draw against the real chance of combustion instability,
        # each from its own stream so that the other dispersions stay as they were.
        self.sd_rng = None if self.seed is None else np.random.default_rng([int(self.seed), 19])
        self.stab_rng = np.random.default_rng([0 if self.seed is None else int(self.seed), 21])
        self.prop_models = {}
        if cfg["PROPELLANT_GAUGING"] or cfg["STORAGE_AGEING"] or cfg["COMMS_CONJUNCTION"]:
            from ...core.model import FlightContext
            ctx = FlightContext(seed=self.seed, design=self.design, vehicle=self.vehicle, world=self.disp,
                                truth=self.truth, data_dir=os.path.join(os.path.dirname(__file__), "..", "..", "data"),
                                bodies=self.bodies, six_dof=self.dynamics == "6dof")
            if cfg["PROPELLANT_GAUGING"]:
                from ..propellant.gauging import PropellantGauging
                self.prop_models["gauging"] = PropellantGauging()
            if cfg["STORAGE_AGEING"]:
                from ..thermal.aging import StorageAgeing
                self.prop_models["ageing"] = StorageAgeing()
            if cfg["COMMS_CONJUNCTION"]:
                from ..comms.model import CommsModel
                self.prop_models["comms"] = CommsModel()
            for mdl in self.prop_models.values():
                mdl.configure(ctx)
        g = self.prop_models.get("gauging")
        if g is not None and not self.scenario:
            # each tank holds its planned load times its hidden load factor (a scenario keeps its saved masses)
            for s in self.states:
                s.fuel *= g.load_factor(s.el.id, "fuel")
                s.ox *= g.load_factor(s.el.id, "ox")
        return cfg

    def _pon(self, name):
        return self.prop_init()[name]

    # ------------------------------------------------------------------ callbacks
    def _p_amb(self):
        try:
            fc = self.bodies.get(self.ctl.get("vt", "VTEarth")).flight_conditions
            return float(fc.pressure) if fc is not None and self.ctl.get("atm") else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    def _lit(self, g, t):
        """Whether a group's thrust is on at time t. An arc is split where thrust starts (see propagate), so the
        thrust is on or off for a whole part, set by the part's start time: an RK4 stage at the very end of the
        part before the start must not see it, and times since J2000 (about 1e9 s) cannot resolve 1e-9 s."""
        t_seg = self.ctl.get("t_seg")
        return g["t_on"] <= (t_seg if t_seg is not None else t) + 1e-6

    @staticmethod
    def group_thrust(g, t, p):
        """Thrust (N) of a group of engines at time t and ambient pressure p. With the start and shutdown transients on, along its start or
        shutdown profile; otherwise its full thrust at its throttle."""
        if "profile" not in g:
            return g["n"] * g["factor"] * g["engine"].thrust(g["throttle"], p)
        u = profile_value(g["profile"], t - g["t0"])
        return g["n"] * g["factor"] * max(0.0, u * g["fvac"] - p * g["engine"].Ae) if u > 0 else 0.0

    @staticmethod
    def group_flow(g, t):
        """Propellant flow (kg/s) of a group at time t, along its profile when the start and shutdown transients are on."""
        if "profile" not in g:
            return g["mdot"]
        return g["mdot"] * profile_value(g["profile"], t - g["t0"])

    def group_used(self, g, t_a, t_b):
        """Propellant (kg) a group uses from t_a to t_b: along its profile with the start and shutdown transients, else at full flow from t_on."""
        if "profile" not in g:
            return g["mdot"] * max(0.0, t_b - max(t_a, g["t_on"]))
        return g["mdot"] * profile_integral(g["profile"], t_a - g["t0"], t_b - g["t0"])

    @staticmethod
    def prop_arc_edges(g):
        """Times where an arc is split for a group: where its thrust starts and, with the start and shutdown transients, where its start or
        tail-off ends (core/arcs.py propagate)."""
        if "profile" not in g:
            return (g["t_on"],)
        return g["t_on"], g["t0"] + g["profile"][-1][0]

    @staticmethod
    def prop_fine(groups, t0, t1):
        """The integrator step for a piece of an arc from t0 to t1: TRANSIENT_STEP_S when a start or a tail-off
        covers the whole piece, else None (the core's usual step)."""
        for g in groups:
            if "profile" in g and g["t0"] <= t0 + 1e-6 and t1 <= g["t0"] + g["profile"][-1][0] + 1e-6:
                return TRANSIENT_STEP_S
        return None

    def prop_engine_obs(self, s, en):
        """Extra obs fields of one engine: with the start and shutdown transients, the mixture ratio it runs at."""
        if not self._pon("ENGINE_TRANSIENTS"):
            return {}
        return {"mixture_ratio": _running(s).get(en.name, {}).get("of", en.of)}

    def _thrust_mag(self, t):
        p = self._p_amb() if self.ctl.get("atm") else 0.0
        F = 0.0
        for g in self.ctl["thrust_groups"]:
            if self._lit(g, t):
                F += self.group_thrust(g, t, p)
        F += self.ctl["rcs"]
        self.ctl["F_now"] = F
        return F

    def _isp(self, t):
        F = self.ctl.get("F_now", 0.0)
        mdot = 0.0
        for g in self.ctl["thrust_groups"]:
            if self._lit(g, t):
                mdot += self.group_flow(g, t)
        mdot += self.ctl["rcs"] / (D.RCS_ISP * D.G0) if self.ctl["rcs"] else 0.0
        if F <= 0 or mdot <= 0:
            return 300.0
        return F / (D.G0 * mdot)

    # ------------------------------------------------------------------ cutoff and chilldown
    def _cutoff(self, s, en, n_off, t0=None):
        """n_off engines of a running group are cut off now: their thrust tails off along the shutdown profile, with
        its impulse scaled by a new draw in a dispersed world. t0 is when their start profile began (its batch's)."""
        run, t_on = _running(s).get(en.name), s.lit.get(en.name)
        t0 = run["t0"] if t0 is None and run else t0
        if not run or "n" not in run or n_off <= 0 or t_on is None or self.t <= t0:
            return
        u0 = profile_value(en.start_profile, self.t - t0)   # a start cut short tails off from where it got to
        k, sd = 1.0, en.shutdown_sd
        if self.sd_rng is not None and sd > 0:
            k = float(np.clip(self.sd_rng.normal(1.0, sd), 1 - 3 * sd, 1 + 3 * sd))
        F, md = en.operating_point(run["throttle"], run["of"])
        self.tails.append({"state": s, "engine": en, "n": n_off, "throttle": run["throttle"], "of": run["of"],
                           "factor": run["factor"], "fvac": u0 * F, "mdot": n_off * u0 * md * run["factor"] / run["ispf"],
                           "t0": self.t, "t_on": self.t, "profile": tuple((t * k, f) for t, f in en.shutdown_profile),
                           "kind": "tail"})

    def chilldown_loss(self, s, en):
        """Seconds of full flow that a start of this engine loses to chilldown (engine.py, CHILL_DUMP). On the Earth
        pad the ground chills the engines and tops up the tanks, so the flight load loses nothing. An engine that
        recirculates its chill flow loses its fuel lead and draws the chilldown pumps' energy from its stage's
        battery; without enough charge, it dumps its chill flow instead."""
        if self.mode == "pad" and self.central == "Earth":
            return 0.0
        if en.chilldown == "recirculation":
            if s.battery_Wh >= CHILL_RECIRC_WH:
                s.battery_Wh -= CHILL_RECIRC_WH
                return CHILL_RECIRC_S
            self.cmd_errors.append(f"{s.el.id}/{en.name}: battery too low for the chilldown pumps "
                                   f"({CHILL_RECIRC_WH:.0f} Wh), chill flow dumped overboard")
        t_cold = _last_cold(s).get(en.name, self.liftoff_t if self.liftoff_t is not None else self.t0)
        return chilldown_loss_s(self.t - t_cold)

    # ------------------------------------------------------------------ the empty point and running dry
    def prop_floor(self, s):
        """(fuel, ox) left in a stage's tanks when its engines stop: the low level cutoff's trip point, or 1e-6
        kg as before."""
        g = self.prop_models.get("gauging") if self._pon("PROPELLANT_GAUGING") else None
        if g is None:
            return 1e-6, 1e-6
        ff, fo = g.cutoff_floor(s.el.id)
        return max(ff, 1e-6), max(fo, 1e-6)

    def prop_empty(self, s):
        ff, fo = self.prop_floor(s)
        return s.fuel <= ff or s.ox <= fo

    def prop_on_burnout(self, s):
        """A stage whose engines were lit reached its empty point. The core's burnout check in core/arcs.py should
        call this in place of its own two lines (see the PR); engine_groups calls it too, so it also works before."""
        if not s.lit:
            return
        for name in s.lit:
            _last_cold(s)[name] = self.t
        s.lit.clear()
        _running(s).clear()
        self.tails = [g for g in self.__dict__.get("tails", []) if g["state"] is not s]
        self.event("burnout", element=s.el.id)
        self._prop_depletion(s)

    def _prop_depletion(self, s):
        """what the engines did when the tank ran out (only with gauging on). A pump that ran dry may have
        destroyed its engine; the stage then cannot restart."""
        g = self.prop_models.get("gauging") if self._pon("PROPELLANT_GAUGING") else None
        if g is None or s.el.id in g.depleted:
            return
        ff, fo = self.prop_floor(s)
        tank = "fuel" if s.fuel - ff <= s.ox - fo else "ox"
        out = g.on_depletion(s.el.id, tank)
        self.event("propellant_depleted", element=s.el.id, tank=tank, cutoff=out["cutoff"], restart=out["restart"])
        if out["failure"]:
            self.fail(out["failure"])

    def prop_obs(self, o):
        """gauge readings in place of the true propellant masses in obs. The core should call it at the end of
        GuidanceMixin.obs (core/guidance_link.py); nothing changes with gauging off."""
        g = self.prop_models.get("gauging") if self._pon("PROPELLANT_GAUGING") else None
        if g is None:
            return o
        acc = o.get("sensed_accel")
        a = float(np.linalg.norm(acc)) if acc is not None else 0.0
        on_pad = self.mode in ("pad", "landed")
        by_id = {s.el.id: s for s in self.present()}
        for d in o.get("elements") or []:
            s = by_id.get(d.get("id"))
            if s is None:
                continue
            settled = g.settled(s.el.id, self.t, a, bool(s.lit), on_pad)
            d.update(g.gauged(s.el.id, s.fuel, s.ox, s.rcs, settled))
        return o

    # ------------------------------------------------------------------ nozzle separation pressure
    def nozzle_ambient_pressure(self, alt, rho, t):
        """Ambient pressure (Pa) for the nozzle flow separation check at altitude alt and time t: the active
        atmosphere model's pressure (Tudat's AtmosphereModel.get_pressure at the vehicle's latitude and longitude)
        when NOZZLE_SEPARATION_PRESSURE is on; otherwise the old rho R T with a fixed lapse rate. The core's check in
        models/structures/loads.py should take its p_amb from here."""
        central = self.central
        if self._pon("NOZZLE_SEPARATION_PRESSURE"):
            try:
                atm = self.bodies.get(central).atmosphere_model
                key = (central, self.t)
                ll = self.__dict__.get("_prop_latlon")
                if ll is None or ll[0] != key:   # the latitude and longitude at the arc's start, once per arc
                    lat = lon = 0.0
                    if self.x is not None:
                        lat, lon, _ = self.latlon(self.x[:3], self.t)
                        lat, lon = math.radians(lat), math.radians(lon)
                    ll = self._prop_latlon = (key, lat, lon)
                p = float(atm.get_pressure(float(alt), ll[2], ll[1], float(t)))
                if math.isfinite(p) and p >= 0.0:
                    return p
            except Exception:  # noqa: BLE001
                pass
        return rho * (287.0 if central == "Earth" else 191.8) * self._temp_guess(alt)

    def check_nozzle_separation(self, groups, alt, rho, t):
        """Nozzle flow separation for engines firing in the atmosphere (moved from check_atmospheric)."""
        p_amb = self.nozzle_ambient_pressure(alt, rho, t)
        for gr in groups:
            if gr.get("kind", "run") == "run" and t >= gr["t_on"] and gr["engine"].separated(gr["throttle"], p_amb):
                self.fail(f"engine {gr['engine'].name} on {gr['state'].el.id}: nozzle flow separated at "
                          f"{alt / 1e3:.1f} km (ambient {p_amb / 1e3:.1f} kPa); side loads broke it")
        return p_amb

    def _burn_through(self, groups, t1):
        """The first moment in the arc from self.t to t1 (its firing already booked) at which an engine with a
        cooling flaw passes 20 s of firing over all its burns, as in a hot-fire test, and the failure; or None."""
        first = None
        for gr in groups:
            flaw = ((self.truth.get("engines") or {}).get(gr["engine"].name) or {}).get("flaw")
            fired = gr["state"].burn_s.get(gr["engine"].name, 0.0)
            if flaw == "cooling" and gr.get("kind", "run") == "run" and fired > 20.0:
                start = max(self.t, gr["t_on"])
                t_bt = start + max(0.0, 20.0 - (fired - max(0.0, t1 - start)))
                if first is None or t_bt < first[0]:
                    first = (t_bt, f"engine {gr['engine'].name} on {gr['state'].el.id}: the chamber wall overheated and "
                                   "burned through after 20 s of firing (its real cooling margin was below 1; "
                                   "a hot-fire test reveals it)")
        return first

    # ------------------------------------------------------------------ engine commands
    # ------------------------------------------------------------------ engine-out checks
    def engine_out_time(self):
        """When the engine-out case shuts its engine down: at "met_s", or "after_ignition_s" after its element
        first fires (known once it has). None when there is no case, or it is done."""
        eo = self.engine_out
        if not eo or "t" in eo:
            return None
        if "met_s" in eo:
            return self.t0 + float(eo["met_s"])
        if eo.get("element") not in self.stage_lit_t:
            return None
        return self.stage_lit_t[eo["element"]] + float(eo.get("after_ignition_s", 0.0))

    def engine_out_now(self, eid, engine):
        """Is it time to shut down one of these engines (lit now) for the engine-out case?"""
        eo = self.engine_out
        t_out = self.engine_out_time()
        if t_out is None or (eo.get("element"), eo.get("engine")) != (eid, engine) or self.t < t_out - 1e-6:
            return False
        eo["t"] = self.t
        eo["at_met_s"] = round(self.t - self.t0, 3)
        return True

    def engine_out_idle(self):
        """The case's engine was not lit when its shutdown came due: it fails idle, and any later command gets one
        fewer. Called by the core after engine_groups."""
        eo, t_out = self.engine_out, self.engine_out_time()
        if t_out is None or self.t < t_out - 1e-6:
            return
        key = (eo.get("element"), eo.get("engine"))
        self.engines_out[key] = self.engines_out.get(key, 0) + 1
        eo["t"], eo["at_met_s"], eo["idle"] = self.t, round(self.t - self.t0, 3), True
        self.event("engine_out", element=key[0], engine=key[1], idle=True)

    # ------------------------------------------------------------------ failure checks
    def _failure_now(self, kind, eid):
        """Is this the failure the failure check forces, and has it not happened yet? Marks it as happened. Also used
        by models/parachutes/chutes.py for a parachute that never opens."""
        fc = self.failure_check
        if not fc or "t" in fc or fc.get("kind") != kind or fc.get("element") != eid:
            return False
        fc["t"] = self.t
        fc["at_met_s"] = round(self.t - self.t0, 3)
        return True

    def _no_light_this_call(self, eid):
        """The forced no-light of this stage happened in this guidance call: every engine on the stage fails to light
        with it, so a stage of two engine kinds cannot light on the second kind in the same command."""
        fc = self.failure_check
        return bool(fc) and fc.get("kind") == "no_light" and fc.get("element") == eid and fc.get("t") == self.t

    def engine_groups(self, cmd, sensed):
        cfg = self.prop_init()
        T = cfg["ENGINE_TRANSIENTS"]
        gauging = self.prop_models.get("gauging")
        groups = []
        eng_cmd = cmd.get("engines") or {}
        if T:
            self.tails = [g for g in self.tails if self.t < g["t0"] + g["profile"][-1][0]]
        for s in self.present():
            e = s.el
            if gauging is not None and e.is_stage:
                # the core's burnout check fires at 1e-6 kg; the low level cutoff stops the engines earlier
                if s.lit and self.prop_empty(s):
                    self.prop_on_burnout(s)
                elif not s.lit and s.__dict__.get("_prop_was_lit") and self.prop_empty(s):
                    self._prop_depletion(s)
                s._prop_was_lit = bool(s.lit)
            c = eng_cmd.get(e.id)
            want = {}
            if isinstance(c, dict):
                c = [c]
            for item in c or []:
                if not isinstance(item, dict):
                    continue
                name = item.get("engine") or (e.engines[0][0].name if e.engines else None)
                want[name] = item
            for en, n_inst in e.engines:
                if en.name not in s.lit:   # shut down, here or by the core (burnout, separation, a failure)
                    _batches(s).pop(en.name, None)
                item = want.get(en.name)
                thr = float(item.get("throttle", 0.0)) if item else 0.0
                n = int(item.get("count", n_inst)) if item else 0
                n_ok = n_inst - self.engines_out.get((e.id, en.name), 0)   # engines shut down for good
                if thr <= 0 or n <= 0 or n_ok <= 0:
                    if en.name in s.lit:
                        if T:
                            for b in _batches(s).get(en.name, []):
                                self._cutoff(s, en, b[0], b[3])
                            _last_cold(s)[en.name] = self.t
                            _running(s).pop(en.name, None)
                        del s.lit[en.name]
                        _batches(s).pop(en.name, None)
                        self.event("shutdown", element=e.id, engine=en.name)
                    continue
                if n > n_inst:
                    self.cmd_errors.append(f"{e.id}: only {n_inst} {en.name} engines installed")
                    n = n_inst
                n = min(n, n_ok)
                if thr < en.min_throttle:
                    self.cmd_errors.append(f"{e.id}/{en.name}: throttle {thr:.2f} below minimum {en.min_throttle}, set to minimum")
                    thr = en.min_throttle
                thr = min(thr, 1.0)
                of = en.of
                if T and item.get("mixture_ratio") is not None:
                    try:
                        of = float(item["mixture_ratio"])
                    except (TypeError, ValueError):
                        of = float("nan")
                    if not (en.of_min - 1e-9 <= of <= en.of_max + 1e-9):
                        clamped = en.of if math.isnan(of) else min(max(of, en.of_min), en.of_max)
                        self.cmd_errors.append(f"{e.id}/{en.name}: mixture ratio {item['mixture_ratio']} outside "
                                               f"[{en.of_min:.3f}, {en.of_max:.3f}], set to {clamped:.3f}")
                        of = clamped
                if self.prop_empty(s):
                    self.cmd_errors.append(f"{e.id}: no propellant")
                    continue
                if gauging is not None and not gauging.depleted.get(e.id, {}).get("restart", True) and en.name not in s.lit:
                    self.cmd_errors.append(f"{e.id}/{en.name}: the engine cannot restart after its tank ran dry")
                    continue
                # the engines lit, one batch per ignition. A lower count shuts the last lit engines down (with the start and shutdown transients
                # their thrust tails off); a higher one lights more, and every engine that lights uses a start.
                batches = _batches(s).setdefault(en.name, [])
                n_lit = sum(b[0] for b in batches)
                while n_lit > n:
                    b = batches[-1]
                    cut = min(b[0], n_lit - n)
                    if T:
                        self._cutoff(s, en, cut, b[3])
                    b[0] -= cut
                    del b[2][len(b[2]) - cut:]
                    n_lit -= cut
                    if not b[0]:
                        batches.pop()
                add = n - n_lit
                if add > 0:
                    if not batches:
                        comms = self.prop_models.get("comms")
                        if comms is not None:
                            why = comms.check_ignition(self, f"{e.id}/{en.name}")
                            if why:
                                self.cmd_errors.append(why)
                                continue
                    starts = s.engine_starts.get(en.name)
                    if starts is None or len(starts) != n_inst:
                        starts = s.engine_starts[en.name] = [s.starts_used.get(en.name, 0)] * n_inst
                    on = {i for b in batches for i in b[2]}
                    # the unlit engines with the most starts left light first
                    free = sorted((i for i in range(n_inst) if i not in on and starts[i] < en.max_starts),
                                  key=lambda i: starts[i])[:add]
                    pump = en.cycle != "pressure_fed"
                    freefall = self.mode == "flight" and (sensed is None or np.linalg.norm(sensed) < 1e-3)
                    if len(free) < add:
                        self.cmd_errors.append(f"{e.id}/{en.name}: no restarts left")
                        add = 0
                    else:
                        for i in free:
                            starts[i] += 1
                        s.starts_used[en.name] = max(starts)
                        if e.id not in self.track.get("stages_lit", []) and (self._failure_now("no_light", e.id)
                                                                             or self._no_light_this_call(e.id)):
                            # A failure check: the stage's first ignition does not light; the starts are spent.
                            self.event("ignition_failed", element=e.id, engine=en.name,
                                       reason="the stage did not light (no-light)")
                            add = 0
                        elif pump and freefall and self.t > self.settle_until:
                            self.event("ignition_failed", element=e.id, engine=en.name,
                                       reason="pump-fed engine started in free fall without ullage settling")
                            add = 0
                    if not add and not batches:
                        continue
                if add > 0:
                    if en.cryogenic:
                        loss = (self.chilldown_loss(s, en) if T else 0.5) * en.mdot * add
                        lf = loss / (1 + en.of)
                        s.fuel = max(0.0, s.fuel - lf)
                        s.ox = max(0.0, s.ox - (loss - lf))
                    t_on = self.t + (en.start_delay if T else 1.0)
                    if not batches:
                        s.lit[en.name] = t_on
                        if T:
                            _running(s)[en.name] = {"t0": t_on}
                    batches.append([add, t_on, free, t_on])
                    self.event("ignition", element=e.id, engine=en.name, count=add)
                    self.exposures.append((self.t, "start", en.name, add, e.id))
                    lit_before = self.track.setdefault("stages_lit", [])
                    if e.id not in lit_before:
                        lit_before.append(e.id)
                        self.stage_lit_t[e.id] = self.t
                        self.exposures.append((self.t, "stage_ignition", e.id, 1))
                    ageing = self.prop_models.get("ageing")
                    if ageing is not None:
                        from ...core.model import Event
                        ageing.on_event(Event("ignition", self.t, e.id, {"engine": en.name}), self)
                    tr_e = (self.truth.get("engines") or {}).get(en.name, {})
                    p_unst = tr_e.get("p_unstable")
                    if cfg["TURBOPUMP"] and p_unst is not None:
                        # each engine of each start draws against the real chance of instability
                        if p_unst > 0 and any(self.stab_rng.uniform() < p_unst for _ in range(add)):
                            self.fail(f"engine {en.name} on {e.id}: combustion instability at ignition destroyed the "
                                      "engine and its stage (its real stability margin was low; hot-fire tests show it)")
                    elif tr_e.get("flaw") == "unstable":
                        self.fail(f"engine {en.name} on {e.id}: combustion instability at ignition destroyed the "
                                  "engine and its stage (a design flaw that a hot-fire test reveals)")
                if self.engine_out_now(e.id, en.name):
                    # the last lit engine shuts down for good; the others keep burning
                    self.engines_out[(e.id, en.name)] = n_inst - n_ok + 1
                    self.event("engine_out", element=e.id, engine=en.name, engines_left=n_ok - 1)
                    b = batches[-1]
                    if T:
                        self._cutoff(s, en, 1, b[3])
                    b[0] -= 1
                    del b[2][-1]
                    if not b[0]:
                        batches.pop()
                    if not batches:
                        if T:
                            _last_cold(s)[en.name] = self.t
                            _running(s).pop(en.name, None)
                        del s.lit[en.name]
                        _batches(s).pop(en.name, None)
                        self.event("shutdown", element=e.id, engine=en.name)
                        continue
                if en.burn_limit_s < float("inf") and s.burn_s.get(en.name, 0.0) >= en.burn_limit_s:
                    self.cmd_errors.append(f"{e.id}/{en.name}: ablative chamber burn-time limit reached")
                    continue
                tr = (self.truth.get("engines") or {}).get(en.name) or {}
                fac = self.disp.thrust_factor.get(e.id, 1.0) * tr.get("thrust", 1.0)
                ispf = self.disp.isp_factor.get(e.id, 1.0) * tr.get("isp", 1.0)
                # one group per lit batch, each with its own start time; "part" numbers them, and the 6-DOF model
                # places batch k's engines from "ring_at" on the whole lit cluster's ring
                n_lit = sum(b[0] for b in batches)
                if T:
                    run = _running(s).setdefault(en.name, {"t0": batches[0][3]})
                    F, md = en.operating_point(thr, of)
                at = 0
                for k, b in enumerate(batches):
                    g = {"state": s, "engine": en, "n": b[0], "throttle": thr, "t_on": b[1], "factor": fac,
                         "part": k, "cluster": n_lit, "ring_at": at}
                    at += b[0]
                    if not T:
                        g["mdot"] = b[0] * thr * en.mdot * fac / ispf
                    else:
                        g.update({"of": of, "fvac": F, "mdot": b[0] * md * fac / ispf, "t0": b[3],
                                  "profile": en.start_profile, "kind": "run"})
                    groups.append(g)
                if T:
                    run.update({"n": n_lit, "throttle": thr, "of": of, "factor": fac, "ispf": ispf})
        if T:
            return groups + [g for g in self.tails if g["state"] in self.states]
        return groups

    def prop_holddown(self, groups, body):
        """(full thrust N, release time) of the groups on a pad: with the start and shutdown transients on, the vehicle is held down until every
        engine has reached full thrust (core/mission.py surface_step should use this, see the PR)."""
        p = 101325.0 if body == "Earth" else 600.0
        run = [gr for gr in groups if gr.get("kind", "run") == "run"]
        if not run:
            return 0.0, self.t
        if "profile" not in run[0]:
            return (sum(gr["n"] * gr["factor"] * gr["engine"].thrust(gr["throttle"], p) for gr in run),
                    max(gr["t_on"] for gr in run))
        return (sum(self.group_thrust(gr, gr["t0"] + gr["profile"][-1][0], p) for gr in run),
                max(gr["t0"] + gr["profile"][-1][0] for gr in run))

    def liftoff(self, thrust_dir):
        if self.central == "Earth" and self.truth.get("tanks"):
            # The as-built tanks see flight pressure at last: a wall weaker than its design (weld
            # efficiency below the model's) bursts. Only the hidden truth knows the welds.
            for s in self.states:
                e = s.el
                w = ((self.truth.get("tanks") or {}).get(e.id) or {}).get("weld")
                if not (e.is_stage and e.tanks and w):
                    continue
                for role, tk in e.tanks.items():
                    hoop = e.tank_pressure * tk.radius / tk.wall_t
                    if hoop > D.MATERIALS[e.material]["sy"] * w:
                        self.fail(f"stage {e.id} {role} tank burst at flight pressure: hoop stress {hoop / 1e6:.0f} MPa "
                                  f"is above what its welds hold ({D.MATERIALS[e.material]['sy'] * w / 1e6:.0f} MPa)")
        if self.central in ("Earth", "Mars"):   # Mars liftoff noise too
            self.check_acoustics()
        self.x = self.site_state()
        self.mode = "flight"
        if self.__dict__.get("liftoff_t") is None:
            self.liftoff_t = self.t
        self.event("liftoff", body=self.central)
        if self.central == "Mars":
            self.mars_lifted = True
        if self.rb is not None:
            self.rb.on_liftoff()

    def _unused_liftoff(self, thrust_dir):
        from tudatpy.dynamics import environment
        from tudatpy.astro import element_conversion
        lat, lon, alt = self.surface_site
        body = self.central
        if body == "Earth":
            f = 1 / 298.257223563
            Rs = EARTH_R_EQ * (1 - f * math.sin(math.radians(lat)) ** 2)
        else:
            Rs = MARS_REF_RADIUS
        fixed = element_conversion.spherical_to_cartesian_elementwise(Rs + alt + 1.0, math.radians(lat), math.radians(lon), 0.0, 0.0, 0.0)
        self.x = np.array(environment.transform_to_inertial_orientation(fixed, self.t, self.bodies.get(body).rotation_model))
        self.mode = "flight"
        self.event("liftoff", body=body)
        if body == "Mars":
            self.mars_lifted = True

    def consume(self, gr, dt, t_b=None):
        """Propellant, burn time and pump power a group uses. Called as consume(gr, dt) (dt seconds of burning, the
        core's form) or consume(gr, t_a, t_b) (from t_a to t_b along the group's profile, the form). With the start and shutdown transients on,
        the old form means the time from the group's start of burning or the arc start to there plus dt, so both
        give the same answer along the profile."""
        s, en = gr["state"], gr["engine"]
        if "profile" not in gr:
            if t_b is not None:   # the burning time from t_a (in dt) to t_b, booked as the core's old form did
                dt = max(0.0, t_b - max(dt, gr["t_on"]))
            md = gr["mdot"]
            if dt > 0:
                self.exposures.append((self.t + dt, "burn", en.name, gr["n"] * dt, s.el.id))
            lf = md * dt / (1 + en.of)
            s.fuel = max(0.0, s.fuel - lf)
            s.ox = max(0.0, s.ox - (md * dt - lf))
            if not gr.get("part"):   # burn time counts once for the engine type, not once per lit batch
                s.burn_s[en.name] = s.burn_s.get(en.name, 0.0) + dt
            if en.pump_power_W:
                s.battery_Wh -= en.pump_power_W * gr["n"] * gr["throttle"] * dt / 3600.0
            return
        if t_b is None:
            t_a, t_b = self.t, max(self.t, gr["t_on"]) + dt
        else:
            t_a = dt
        w = profile_integral(gr["profile"], t_a - gr["t0"], t_b - gr["t0"])   # seconds of full flow
        on = max(0.0, t_b - max(t_a, gr["t_on"])) if gr["kind"] == "run" else 0.0
        if on > 0:
            self.exposures.append((t_b, "burn", en.name, gr["n"] * on, s.el.id))
        md = gr["mdot"] * w
        lf = md / (1 + gr["of"])
        s.fuel = max(0.0, s.fuel - lf)
        s.ox = max(0.0, s.ox - (md - lf))
        if not gr.get("part"):
            s.burn_s[en.name] = s.burn_s.get(en.name, 0.0) + on
        if en.pump_power_W:
            s.battery_Wh -= en.pump_power_W * gr["n"] * gr["throttle"] * w / 3600.0

    def time_to_depletion(self, groups, rcs_F, rcs_state, horizon=float("inf")):
        tmin = float("inf")
        if not any("profile" in gr for gr in groups):
            per = {}
            for gr in groups:
                s = gr["state"]
                r = per.setdefault(id(s), [s, 0.0, 0.0, 0.0])
                r[1] += gr["mdot"] / (1 + gr["engine"].of)
                r[2] += gr["mdot"] * gr["engine"].of / (1 + gr["engine"].of)
                if gr["engine"].pump_power_W:
                    r[3] += gr["engine"].pump_power_W * gr["n"] * gr["throttle"]
                if gr["engine"].burn_limit_s < float("inf"):
                    tmin = min(tmin, gr["engine"].burn_limit_s - s.burn_s.get(gr["engine"].name, 0.0))
            for s, rf, ro, pw in per.values():
                ff, fo = self.prop_floor(s) if self._pon("PROPELLANT_GAUGING") else (0.0, 0.0)
                if rf > 0:
                    tmin = min(tmin, max(0.0, s.fuel - ff) / rf)
                if ro > 0:
                    tmin = min(tmin, max(0.0, s.ox - fo) / ro)
                if pw > 0:
                    tmin = min(tmin, max(0.0, s.battery_Wh) * 3600.0 / pw)
        else:
            per = {}
            for gr in groups:
                s = gr["state"]
                r = per.setdefault(id(s), [s, [], 0.0])
                r[1].append(gr)
                if gr["engine"].pump_power_W and gr["kind"] == "run":
                    r[2] += gr["engine"].pump_power_W * gr["n"] * gr["throttle"]
                if gr["engine"].burn_limit_s < float("inf") and gr["kind"] == "run":
                    tmin = min(tmin, gr["engine"].burn_limit_s - s.burn_s.get(gr["engine"].name, 0.0))
            for s, grs, pw in per.values():
                ff, fo = self.prop_floor(s) if self._pon("PROPELLANT_GAUGING") else (0.0, 0.0)

                def left(T, s=s, grs=grs, ff=ff, fo=fo):
                    """The fuel and oxidizer above the empty point after T more seconds of these groups' flow."""
                    f, o = s.fuel - ff, s.ox - fo
                    for gr in grs:
                        md = gr["mdot"] * profile_integral(gr["profile"], self.t - gr["t0"], self.t + T - gr["t0"])
                        f -= md / (1 + gr["of"])
                        o -= md * gr["of"] / (1 + gr["of"])
                    return min(f, o)
                T_hi = min(horizon, 1e7)
                if left(T_hi) <= 0:
                    T_lo = 0.0
                    for _ in range(60):
                        T_mid = 0.5 * (T_lo + T_hi)
                        if left(T_mid) > 0:
                            T_lo = T_mid
                        else:
                            T_hi = T_mid
                        if T_hi - T_lo < 1e-4:
                            break
                    tmin = min(tmin, T_hi)
                if pw > 0:
                    tmin = min(tmin, max(0.0, s.battery_Wh) * 3600.0 / pw)
        if rcs_F and rcs_state is not None:
            tmin = min(tmin, rcs_state.rcs / (rcs_F / (D.RCS_ISP * D.G0)))
        return tmin

    # ------------------------------------------------------------------ scenarios
    def prop_save(self, sc):
        """Add what a saved state needs for the start and shutdown transients (core/mission.py _save_state should call it)."""
        sc["last_cold"] = {s.el.id: dict(_last_cold(s)) for s in self.states if _last_cold(s)}
        sc["liftoff_t"] = self.__dict__.get("liftoff_t")

    def prop_load(self, sc, t):
        """Read it back (core/mission.py _apply_scenario should call it after the propellant loop)."""
        for s in self.states:
            s.last_cold = {k: float(v) for k, v in ((sc.get("last_cold") or {}).get(s.el.id) or {}).items()}
        self.liftoff_t = sc.get("liftoff_t", t)
