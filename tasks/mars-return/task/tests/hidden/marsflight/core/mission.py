"""The mission loop: state, milestones, scenarios, phases and the step through the flight.

Mission is assembled from mixins, one per area, so each area's methods live in their own file.
The methods are moved verbatim from sim.py; the class, its methods and its behavior are unchanged."""
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
    MILESTONES, MILESTONE_WEIGHTS, ATM_TOP, EARTH_R_EQ, PARTIAL_APPROACH_D0, SCENARIO_PHASES, STATE_AFTER, FlightFailure, interp, unit, rotate_towards, newtonian_cd, parachute_peak_load, great_circle_km, ElementState, clamp01,
    MAX_GUIDANCE_CALLS, TARGET_GATES, TARGET_SCALE_KM, target_credit,
)
from ..core.frames import FramesMixin
from ..core.arcs import ArcsMixin
from ..core.guidance_link import GuidanceMixin, check_command
from ..guidance_io import GuidanceError
from ..core.wiring import WiringMixin, switches
from ..models.propulsion.engines import PropulsionMixin
from ..models.aero.coefficients import AeroMixin
from ..models.parachutes.chutes import ParachuteMixin
from ..models.separation.separate import SeparationMixin
from ..models.landing.surface import LandingMixin
from ..models.structures.loads import LoadsMixin
from ..models.entry.heating import HeatingMixin
from ..models.vibration.checks import VibrationMixin
from ..models.power.resources import ResourcesMixin


class Mission(FramesMixin, ArcsMixin, GuidanceMixin, PropulsionMixin, AeroMixin, ParachuteMixin, SeparationMixin, LandingMixin, LoadsMixin, HeatingMixin, VibrationMixin, ResourcesMixin, WiringMixin):
    def __init__(self, vehicle, design: dict, guidance, seed=None, max_wall_s=3600.0, log_arcs=True,
                 truth=None, scenario=None, telemetry=False, save_states=None, dynamics=None, engine_out=None,
                 failure_check=None, live=None):
        """live: a marsflight.live.LiveLog that gets every event and a sampled state after each arc (the flight
        viewer's live progress; it never changes the flight). truth: the hidden as-built hardware (program.flight_truth); None flies the design model as is.
        scenario: start from a saved state instead of the pad (development only; see SCENARIO_PHASES).
        telemetry: keep a detailed per-step log for replays. save_states: a directory where the state
        at each milestone is written as a scenario file. dynamics: "3dof" (the default, a slew-limited
        attitude) or "6dof" (rigid-body attitude dynamics and separation tracking, see sixdof.py); None
        reads MARSFLIGHT_DYNAMICS. engine_out: {"element", "engine", "after_ignition_s"} shuts one of that
        element's engines down that long after the element first fires (an engine-out check,
        grading.engine_out_cases), or at "met_s" instead of "after_ignition_s". failure_check: {"kind", "element"}
        forces one survivable failure for a failure check (grading.failure_check_cases): "chute_fail", that
        parachute deploys but never opens, so a backup has to land the vehicle; or "no_light", that stage's
        first ignition does not light, so the flight software has to try again with a start left."""
        from tudatpy.astro import time_representation as tr
        import datetime as _dt
        self.live = live
        self.sw = switches()      # the wired models' switches (core/wiring.py)
        self.w, self.w_log = {}, {}
        if self.sw["FAILURES"]:
            # faults change element fields (RCS thrust, battery and ISRU capacity): fly a private copy
            import copy as _copy
            vehicle = _copy.deepcopy(vehicle)
        self.vehicle = vehicle
        if not isinstance(design.get("elements"), list) or not all(isinstance(e, dict) for e in design["elements"]):
            # "elements": {...} or ["x"] crashed the wired models' setup; build_vehicle already reports the error
            design = dict(design, elements=[e for e in design.get("elements") or [] if isinstance(e, dict)]
                          if isinstance(design.get("elements"), list) else [])
        self.design = design
        self.guidance = guidance
        self.seed = seed
        self.max_wall_s = max_wall_s
        self.log_arcs = log_arcs
        self.truth = truth or {}
        self.telemetry = [] if telemetry else None
        self.save_states = save_states
        self.exposures = []       # (t, kind, key, amount[, element]): hazards for the reliability model
        self.engine_out = dict(engine_out) if engine_out else None   # the engine-out checks, models/propulsion/engines.py
        self.failure_check = dict(failure_check) if failure_check else None   # models/propulsion/engines.py
        self.chute_failed = set()   # parachutes that deployed and never opened (a failure check)
        self.engines_out = {}     # (element, engine) -> engines shut down
        self.stage_lit_t = {}     # element -> time it first fired
        self.partial = {}         # milestone -> best fraction of the way there, 0 to 1
        self.partial_t = {}
        self.track = {}           # running values the partial credit needs
        self.scenario = None
        self.events = []
        self.traj = []
        self.milestones = {}
        self.landing_km = {}      # body -> distance from the landing site at touchdown (km)
        self.failure = None
        ids = [e.id for e in vehicle.elements]
        self.disp = Dispersions.draw(seed, ids)
        le = design.get("launch_epoch_utc", "2031-01-15T00:00:00")
        try:
            dt0 = _dt.datetime.fromisoformat(str(le).replace("Z", ""))
        except ValueError:
            raise FlightFailure(f"launch_epoch_utc {le!r} is not an ISO date")
        if not (_dt.datetime(2030, 1, 1) <= dt0 <= _dt.datetime(2035, 12, 31)):
            raise FlightFailure("launch_epoch_utc must fall between 2030-01-01 and 2035-12-31")
        self.t0 = tr.julian_day_to_seconds_since_epoch(tr.DateTime.from_python_datetime(dt0).to_julian_day())
        self.world = World(self.disp, self.t0)
        # The last date a mission may reach: DE440 runs to 2650 and the daily space weather to 2041-10-01; with the
        # old merged kernel (MARSFLIGHT_EPHEMERIS=tudat_merged) the ephemeris ends in 2040, so 2039-07-01 as before.
        from .. import environment as _env
        self.end_date = (2039, 7, 1) if _env._KERNELS_LOADED == "tudat_merged" else (2041, 10, 1)
        self.t_end = tr.julian_day_to_seconds_since_epoch(tr.DateTime.from_python_datetime(_dt.datetime(*self.end_date)).to_julian_day())
        self.bodies = self.world.bodies
        self.states = []
        for e in vehicle.elements:
            self.states.append(ElementState(
                e, self._w_dry(e),
                e.loaded.get("fuel", 0.0), e.loaded.get("ox", 0.0), e.rcs_prop, e.battery_Wh))
        self.chute_deployed = set()
        self.chute_open = {}      # parachute id -> its opening: line stretch, full inflation, peak drag area
        self.ballast_dropped = set()
        self.bank, self.bank_rate, self.bank_cmd, self.bank_plan = 0.0, 0.0, 0.0, None   # rad, rad/s
        self.t = self.t0
        self.central = "Earth"
        self.mode = "pad"
        site = self.launch_pad = D.launch_site(self.design)   # the design's launch.site (LC-39A unless it names another)
        self.surface_site = (site["lat"], site["lon"], site["alt"])
        self.x = None
        self.dir = None
        self.prev_alt = site["alt"]
        self.cmd_errors = []
        self.pending_events = []
        self.nav_err = (np.zeros(3), np.zeros(3))
        self.nav_regime = None
        self.nav_redraw_t = -1e30
        self.peak = {"q": 0.0, "g": 0.0, "heat_flux": 0.0}
        self.heat_load = {}       # shield element id -> J/m^2
        self.landed_t = None
        self.surface_ops_ok_s = 0.0
        self.samples_loaded = False
        self.power_deficit_s = 0.0
        self.freeze_deficit_s = 0.0
        self.isru_target = None
        self.settle_until = -1e30
        self.mars_lifted = False
        self.capsule_entry_started = False
        self.mars_entry_started = False
        self.n_calls = 0
        self.wall0 = time.time()
        self._acc_cache = {}
        self.last = {}
        self.dynamics = str(dynamics or os.environ.get("MARSFLIGHT_DYNAMICS") or "3dof").lower()
        if self.dynamics not in ("3dof", "6dof"):
            raise FlightFailure(f"dynamics {self.dynamics!r} is not 3dof or 6dof")
        self.rb = None
        self._wire_setup()
        self._build_vehicle_bodies()
        if self.dynamics == "6dof":
            from ..sixdof import SixDof
            self.rb = SixDof(self)
        self.milestone("design_valid")
        if scenario:
            self._apply_scenario(scenario)

    # ------------------------------------------------------------------ bookkeeping
    def present(self):
        return self.states

    def find(self, eid):
        for s in self.states:
            if s.el.id == eid:
                return s
        return None

    def total_mass(self):
        m = sum(s.mass for s in self.states)
        if self.samples_loaded:
            m += 0.0  # samples are added to the capsule's dry mass
        return m

    def event(self, kind, at=None, **kw):
        t = self.t if at is None else at   # at: a time inside the arc being checked
        ev = {"t": t, "met_s": round(t - self.t0, 3), "type": kind}
        ev.update(kw)
        self.events.append(ev)
        self.pending_events.append(kind if not kw.get("element") else f"{kind}:{kw['element']}")
        if self.live is not None:
            self.live.event(ev)
        if self.w:
            self._w_event(kind, kw)

    def milestone(self, name):
        if name in self.milestones:
            return
        # sequential: every earlier non-optional milestone must be done
        for k, w, optional in MILESTONES:
            if k == name:
                break
            if not optional and k not in self.milestones:
                return
        self.milestones[name] = self.t
        self.event("milestone", name=name)
        if self.save_states and name in STATE_AFTER:
            self._save_state(STATE_AFTER[name])

    def fail(self, reason):
        raise FlightFailure(reason)

    def progress(self):
        """Weights of the milestones reached, plus partial credit towards the next required milestone
        and towards an optional landing target passed on the way (see _track for the measures)."""
        total = 0.0
        for k, w, optional in MILESTONES:
            gate, body = TARGET_GATES.get(k, (None, None))
            if gate in self.milestones and self.landing_km.get(body) is not None:   # a landing target: by distance
                km = self.landing_km[body]
                if body == "Mars" or km <= D.SITES["earth_landing"]["range_km"]:
                    total += w * target_credit(km, TARGET_SCALE_KM[k])
                continue
            if k in self.milestones:
                total += w
                continue
            total += w * self.partial.get(k, 0.0)
            if not optional:
                break
        return total

    # ------------------------------------------------------------------ partial credit
    def _set_partial(self, name, f, t=None):
        if name in self.milestones:
            return
        f = clamp01(f)
        if f > self.partial.get(name, 0.0) + 1e-9:
            self.partial[name] = round(f, 6)
            self.partial_t[name] = self.t if t is None else t

    def _specific_energy(self, body):
        r, v = self.x[:3], self.x[3:]
        return float(np.dot(v, v)) / 2 - self.gm(body) / float(np.linalg.norm(r))

    def _orbit_frac(self, body, target_alt):
        """How far an ascent is towards a circular orbit at target_alt: the mean of the orbital energy
        gained since standing on the surface, and the periapsis height reached."""
        mu = self.gm(body)
        R = EARTH_R_EQ if body == "Earth" else MARS_REF_RADIUS
        e_surf, e_orb = -mu / R, -mu / (2 * (R + target_alt))
        fe = (self._specific_energy(body) - e_surf) / (e_orb - e_surf)
        energy, _, rp = self.orbit_elements()
        fp = 1.0 if energy >= 0 else (rp - R) / target_alt
        return 0.5 * clamp01(fe) + 0.5 * clamp01(fp)

    def _escape_frac(self, body, from_alt):
        """Orbital energy gained from a circular orbit at from_alt towards escape (zero energy)."""
        R = EARTH_R_EQ if body == "Earth" else MARS_REF_RADIUS
        e_orb = -self.gm(body) / (2 * (R + from_alt))
        return (self._specific_energy(body) - e_orb) / -e_orb

    @staticmethod
    def _approach_frac(d_min, soi, d0=PARTIAL_APPROACH_D0):
        """Closest approach to the target planet on a log scale: the distance at the start of the cruise
        (d0) earns 0, the edge of its sphere of influence earns 1."""
        if d_min <= soi:
            return 1.0
        if d0 <= d_min:
            return 0.0
        return math.log(d0 / d_min) / math.log(d0 / soi)

    def _start_cruise(self):
        """Note the distance to the target planet as a heliocentric cruise starts (once per target)."""
        target = "Earth" if self.mars_lifted else "Mars"
        try:
            p = self.central_state(target, self.t)[:3] - self.central_state("Sun", self.t)[:3]
            self.track.setdefault(f"{target.lower()}_d0", float(np.linalg.norm(self.x[:3] - p)))
        except Exception:  # noqa: BLE001
            pass

    def _track(self, dmin_earth=None, dmin_mars=None):
        """Update partial credit after an arc. Never ends a flight."""
        try:
            m, c = self.milestones, self.central
            if self.x is None:
                return
            alt = self.altitude(self.x[:3], self.t) if c != "Sun" else float("inf")
            if c == "Earth" and "earth_departure" not in m:
                self._set_partial("max_q_passed", alt / 50e3)
                if self.mode == "flight":
                    self._set_partial("earth_orbit", self._orbit_frac("Earth", 150e3))
                if "earth_orbit" in m:
                    self._set_partial("earth_departure", self._escape_frac("Earth", 150e3))
            if c == "Sun":
                if "earth_departure" in m and dmin_mars is not None:
                    self._set_partial("mars_arrival", self._approach_frac(
                        dmin_mars, MARS_SOI, self.track.get("mars_d0", PARTIAL_APPROACH_D0)))
                if "mars_departure" in m and dmin_earth is not None:
                    self._set_partial("earth_arrival", self._approach_frac(
                        dmin_earth, EARTH_SOI, self.track.get("earth_d0", PARTIAL_APPROACH_D0)))
            if c == "Mars" and self.mars_lifted:
                self._set_partial("mars_orbit", self._orbit_frac("Mars", 100e3))
                if "mars_orbit" in m and self.samples_loaded and any(s.el.kind == "capsule" for s in self.present()):
                    self._set_partial("mars_departure", self._escape_frac("Mars", 100e3))
            if c == "Mars" and not self.mars_lifted and "mars_entry" in m and "mars_entry_agl" in self.track:
                agl0 = self.track["mars_entry_agl"]
                agl = alt - self.ground_alt(self.x[:3], self.t)
                if agl0 > 2000.0:
                    self._set_partial("mars_descent", (agl0 - agl) / (agl0 - 2000.0))
        except Exception:  # noqa: BLE001
            pass

    def _track_entry(self, alt, V, t):
        """Entry partial credit, per integration step at time t: the share of the entry kinetic energy
        removed while the vehicle is intact (down to 1 km/s at Mars, 500 m/s for the capsule at Earth)."""
        c, m = self.central, self.milestones
        if c == "Mars" and not self.mars_lifted and "mars_arrival" in m and alt < ATM_TOP["Mars"]:
            ve = self.track.setdefault("mars_ve", V)
            if alt < 125e3:
                self._set_partial("mars_entry", (ve * ve - V * V) / max(1.0, ve * ve - 1000.0 ** 2), t)
        elif c == "Earth" and "earth_arrival" in m and alt < 120e3:
            ve = self.track.setdefault("earth_ve", V)
            self._set_partial("earth_entry", (ve * ve - V * V) / max(1.0, ve * ve - 500.0 ** 2), t)

    # ------------------------------------------------------------------ replay log and saved states
    def phase(self):
        m, c = self.milestones, self.central
        if self.mode == "pad":
            return "pad"
        if self.mode == "landed":
            return "mars_surface"
        if c == "Sun":
            return "cruise_to_earth" if "mars_departure" in m else "cruise_to_mars"
        if c == "Mars":
            if not self.mars_lifted:
                return "mars_edl" if self.mars_entry_started else "mars_approach"
            return "mars_orbit" if "mars_orbit" in m else "mars_ascent"
        if "mars_departure" in m:
            return "earth_entry" if self.capsule_entry_started else "earth_approach"
        return "earth_orbit" if "earth_orbit" in m else "earth_ascent"

    def _tele(self, row):
        if self.telemetry is not None and len(self.telemetry) < 300000:
            row.setdefault("met", round(self.t - self.t0, 3))
            self.telemetry.append(row)

    def _save_state(self, phase):
        """Write the state at a milestone as a scenario file that `marsflight fly --scenario` can start from."""
        try:
            import json as _json
            import os as _os
            sc = {"phase": phase, "t": self.t, "central": self.central, "met_s": round(self.t - self.t0, 3),
                  "elements": [s.el.id for s in self.states],
                  "propellant": {s.el.id: {"fuel": s.fuel, "ox": s.ox, "rcs": s.rcs, "battery_Wh": s.battery_Wh,
                                           "dry": s.dry} for s in self.states},
                  "starts_used": {s.el.id: dict(s.starts_used) for s in self.states if s.starts_used},
                  "engine_starts": {s.el.id: {k: list(v) for k, v in s.engine_starts.items()}
                                    for s in self.states if s.engine_starts},
                  "chutes_deployed": sorted(self.chute_deployed), "chutes_failed": sorted(self.chute_failed),
                  "ballast_dropped": sorted(self.ballast_dropped),
                  "samples_loaded": self.samples_loaded, "surface_ops_ok_s": self.surface_ops_ok_s,
                  "dir": None if self.dir is None else self.dir.tolist()}
            if self.x is not None:
                sc["r"], sc["v"] = self.x[:3].tolist(), self.x[3:].tolist()
            else:
                lat, lon, _ = self.surface_site
                sc["site"] = {"lat": lat, "lon": lon}
            if self.rb is not None:
                self.rb.save(sc)
            nf = getattr(self, "_navf", None)
            if nf is not None:   # the navigation filter, so a scenario start carries its error and covariance
                sc["nav_filter"] = dict(nf.nav.save(), from_ground=bool(getattr(nf, "from_ground", False)),
                                        central=getattr(self, "_navf_central", None))
            _os.makedirs(self.save_states, exist_ok=True)
            with open(_os.path.join(self.save_states, f"{phase}.json"), "w") as f:
                _json.dump(sc, f, indent=1)
        except Exception:  # noqa: BLE001
            pass

    def _apply_scenario(self, sc):
        """Start from a saved state (development only: the verifier always flies from the pad).
        Milestones before the scenario's phase count as reached at its start time."""
        phase = sc.get("phase")
        if phase not in SCENARIO_PHASES:
            raise FlightFailure(f"scenario phase {phase!r} is not one of {sorted(SCENARIO_PHASES)}")
        t = float(sc["t"])
        last = SCENARIO_PHASES[phase]
        done = []
        for k, _, optional in MILESTONES:
            if not optional:
                done.append(k)
            if k == last:
                break
        self.t = t
        self.milestones = {k: t for k in done}
        keep = sc.get("elements")
        if keep is not None:
            ids = set(keep)
            missing = ids - {s.el.id for s in self.states}
            if missing:
                raise FlightFailure(f"scenario lists elements that are not in the design: {sorted(missing)}")
            self.states = [s for s in self.states if s.el.id in ids]
        for s in self.states:
            p = (sc.get("propellant") or {}).get(s.el.id) or {}
            s.fuel = float(p.get("fuel", s.fuel))
            s.ox = float(p.get("ox", s.ox))
            s.rcs = float(p.get("rcs", s.rcs))
            s.battery_Wh = float(p.get("battery_Wh", s.battery_Wh))
            s.dry = float(p.get("dry", s.dry))
            s.starts_used = dict((sc.get("starts_used") or {}).get(s.el.id) or {})
            s.engine_starts = {k: list(v) for k, v in ((sc.get("engine_starts") or {}).get(s.el.id) or {}).items()}
        self.chute_deployed = set(sc.get("chutes_deployed") or [])
        self.chute_failed = set(sc.get("chutes_failed") or [])   # spent canopies cannot be deployed again
        self.ballast_dropped = set(sc.get("ballast_dropped") or [])
        self.mars_lifted = phase in ("mars_orbit", "cruise_to_earth", "earth_approach")
        if phase in ("mars_approach", "mars_surface"):
            self.disp.reach_mars(self.world.mars_sol(t))
        self.samples_loaded = bool(sc.get("samples_loaded", self.mars_lifted))
        cap = next((s for s in self.states if s.el.kind == "capsule"), None)
        if self.samples_loaded and cap is not None and "dry" not in ((sc.get("propellant") or {}).get(cap.el.id) or {}):
            cap.dry += self._w_sample_kg()
        if phase == "mars_surface":
            site = sc.get("site") or D.SITES["mars_landing"]
            self.central, self.mode, self.x = "Mars", "landed", None
            h = self._t_height("Mars", float(site["lat"]), float(site["lon"]))
            self.surface_site = (float(site["lat"]), float(site["lon"]), D.SITES["mars_landing"]["alt"] if h is None else h)
            self.landed_t = t
            self.surface_ops_ok_s = float(sc.get("surface_ops_ok_s", 0.0))
        else:
            self.central = sc.get("central") or {"earth_orbit": "Earth", "cruise_to_mars": "Sun", "mars_approach": "Mars",
                                                   "mars_orbit": "Mars", "cruise_to_earth": "Sun",
                                                   "earth_approach": "Earth"}[phase]
            self.mode = "flight"
            self.x = np.array(list(sc["r"]) + list(sc["v"]), dtype=float)
            if self.central == "Sun":
                self._start_cruise()
        if sc.get("dir"):
            self.dir = unit(np.array(sc["dir"], dtype=float))
        if self.rb is not None:
            self.rb.load(sc)
        # the navigation filter is made at the first obs (guidance_link._nav_filter), which loads this
        self._nav_restore = sc.get("nav_filter")
        self.scenario = phase
        self.event("scenario_start", phase=phase)

    # ------------------------------------------------------------------ main loop
    def run(self):
        try:
            if self.vehicle.errors:
                self.milestones.pop("design_valid", None)
                raise FlightFailure("design invalid: " + "; ".join(self.vehicle.errors[:5]))
            self.guidance.reset({"design": self.design, "vehicle": self.vehicle.report(), "t0": self.t0,
                                 "sites": dict(D.SITES, earth_launch={k: v for k, v in self.launch_pad.items()
                                                                       if k != "site"}) if self.launch_pad.get("site") != D.DEFAULT_LAUNCH_SITE else D.SITES,
                                 "seed_is_nominal": self.seed is None, "scenario": self.scenario,
                                 "t": self.t, "dynamics": self.dynamics})
            self.last = {}
            sensed = np.zeros(3)
            while True:
                if time.time() - self.wall0 > self.max_wall_s:
                    raise FlightFailure("simulation wall-clock limit reached")
                if self.t > self.t_end:
                    raise FlightFailure("mission ran past {:04d}-{:02d}-{:02d} (end of the simulated mission years)"
                                        .format(*self.end_date))
                if self.w:
                    self._w_before_obs()
                o = self.obs(sensed)
                if self.w:
                    o = self._w_sense(o)
                self.n_calls += 1
                if self.n_calls > MAX_GUIDANCE_CALLS:
                    raise FlightFailure(f"flight software called more than {MAX_GUIDANCE_CALLS:,} times (a loop?)")
                cmd = self.guidance.step(o)
                if not isinstance(cmd, dict):
                    raise FlightFailure("guidance returned a non-object command")
                check_command(cmd)   # wrongly typed fields ("next": "soon", "actions": 7, ...) crashed the step
                if self.w:
                    cmd = self._w_command(cmd)
                try:
                    self.apply_actions(cmd)
                except StopIteration:
                    self.event("mission_ended_by_guidance")
                    break
                t_before = self.t
                sensed = self.step(cmd, sensed)
                if self.w:
                    self._w_after_step(t_before, sensed)
                if self.live is not None:
                    self.live.sample(self)
                if "earth_touchdown" in self.milestones:
                    break
        except FlightFailure as ex:
            self.failure = str(ex)
            self.event("failure", reason=self.failure)
        except GuidanceError as ex:   # a NaN reply, an exception or an exit in the flight software read as a simulator error
            self.failure = f"flight software error: {ex}"
            self.event("failure", reason=self.failure)
        except Exception as ex:  # noqa: BLE001
            self.failure = f"simulator error: {type(ex).__name__}: {ex}"
            self.event("failure", reason=self.failure)
        finally:
            if self.failure and self.w and self.central == "Mars" and not self.mars_lifted:
                self._w_edl_report()   # an entry that ended in a failure still reports what reached the ground
            try:
                self.guidance.close()
            except Exception:  # noqa: BLE001
                pass
        return self.result()

    def result(self):
        r = self._result()
        return self._w_result(r) if (self.w or any(self.sw.values())) else r

    def _result(self):
        return {"seed": self.seed, "progress": round(self.progress(), 6), "milestones": self.milestones,
                "landing_errors_km": {b: round(km, 3) for b, km in self.landing_km.items()},
                "partial": self.partial, "partial_t": self.partial_t, "failure": self.failure,
                "failure_t": self.t if self.failure else None, "end_t": self.t, "t0": self.t0,
                "scenario": self.scenario, "liftoff_mass_kg": self.vehicle.liftoff_mass, "guidance_calls": self.n_calls,
                "wall_s": round(time.time() - self.wall0, 1), "peaks": self.peak, "events": self.events,
                "exposures": self.exposures, "world": self.disp.summary(), "dynamics": self.dynamics,
                "engine_out": self.engine_out, "failure_check": self.failure_check}

    # ------------------------------------------------------------------ one arc
    def step(self, cmd, sensed):
        dt = float(cmd.get("next", 1.0))
        if not (dt > 0) or math.isnan(dt):
            dt = 1.0
        dt = min(dt, max(1.0, self.t_end - self.t + 1.0))   # "next": 1e12 in orbit propagated for hours past the mission's end
        if self.w:
            hold = self._w_countdown(cmd)      # launch weather: hold on the pad until the next attempt
            if hold > 0:
                dt = hold
            # cruise corrections needing an uplink and storage ageing are checked at each ignition by
            # models/propulsion/engines.py (COMMS_CONJUNCTION, STORAGE_AGEING)
        t_out = self.engine_out_time()
        if t_out is not None and self.t < t_out < self.t + dt:
            dt = t_out - self.t
        groups = self.engine_groups(cmd, sensed)
        t_out = self.engine_out_time()
        if t_out is not None and self.t < t_out < self.t + dt:
            dt = t_out - self.t   # an element that first fired in this call: its shutdown time is known only now
        else:
            self.engine_out_idle()
        rcs_cmd = cmd.get("rcs") or {}
        rcs_F, rcs_state = 0.0, None
        if rcs_cmd:
            rs = self.find(rcs_cmd.get("element", ""))
            if rs and rs.el.rcs_thrust > 0 and rs.rcs > 0:
                rcs_F = rs.el.rcs_thrust * float(np.clip(float(rcs_cmd.get("throttle", 1.0)), 0.0, 1.0))   # "throttle": "1" crashed np.clip
                rcs_state = rs
        settle = False
        if cmd.get("settle"):
            rs = next((s for s in self.present() if s.el.rcs_thrust > 0 and s.rcs > 0), None)
            if rs is not None:
                if rcs_F == 0:
                    rcs_F, rcs_state = 0.1 * rs.el.rcs_thrust, rs
                self.settle_until = self.t + dt + 5.0   # as asked; _fly cuts it to the arc actually flown
                settle = True
        want_dir = cmd.get("dir")
        if want_dir is not None:
            want_dir = unit(np.array(want_dir, dtype=float))
            if not np.all(np.isfinite(want_dir)) or np.linalg.norm(want_dir) < 0.5:
                want_dir = None
        if self.rb is not None:
            # 6-DOF: the command sets the onboard attitude controller; the attitude itself is propagated.
            thrust_dir = self.rb.command(cmd, want_dir)
            self.ctl["bank"] = math.radians(float(cmd.get("bank_deg", 0.0)))
            self.bank_cmd = self.ctl["bank"]   # a lifting entry rolls towards it (_plan_bank), as in 3-DOF
            return self._fly(dt, groups, rcs_F, rcs_state, thrust_dir, cmd, settle)
        if self.dir is None:
            self.dir = want_dir if want_dir is not None else np.array([1.0, 0.0, 0.0])
        elif want_dir is not None:
            slew = max([s.el.slew_rate for s in self.present() if (s.el.rcs_thrust > 0 or any(en.gimbal_deg > 0 for en, _ in s.el.engines))] + [0.5])
            self.dir = rotate_towards(self.dir, want_dir, math.radians(slew) * max(dt, 0.5))
        if self.disp.pointing_sigma_deg:
            perturb = self.disp.rng.normal(0, math.radians(self.disp.pointing_sigma_deg), 3)
            thrust_dir = unit(self.dir + perturb - np.dot(perturb, self.dir) * self.dir)
        else:
            thrust_dir = self.dir
        self.bank_cmd = math.radians(float(cmd.get("bank_deg", 0.0)))
        return self._fly(dt, groups, rcs_F, rcs_state, thrust_dir, cmd, settle)

    def _fly(self, dt, groups, rcs_F, rcs_state, thrust_dir, cmd, settle):
        if self.mode in ("pad", "landed"):
            sensed = self.surface_step(dt, groups, rcs_F, rcs_state, thrust_dir)
        else:
            sensed = self.flight_step(dt, groups, rcs_F, rcs_state, thrust_dir, cmd)
        if settle:   # the propellant stays settled 5 s past the end of the arc actually flown
            self.settle_until = self.t + 5.0
        return sensed

    def surface_step(self, dt, groups, rcs_F, rcs_state, thrust_dir):
        body = self.central
        lat, lon, alt = self.surface_site
        m = self.total_mass()
        g = self.gm(body) / (({"Earth": EARTH_R_EQ, "Mars": MARS_REF_RADIUS}[body] + alt) ** 2)
        F, t_rel = self.prop_holddown(groups, body)
        if groups and F > 1.02 * m * g:
            # Hold-down: the vehicle is released once every engine has reached thrust (with the start and shutdown transients, full thrust).
            if t_rel > self.t:
                for gr in groups:
                    self.consume(gr, self.t, t_rel)
                self.t = t_rel
            if not self._pon("ENGINE_TRANSIENTS"):
                for gr in groups:
                    gr["t_on"] = self.t
                    gr["state"].lit[gr["engine"].name] = self.t
            for gr in groups:
                if gr.get("kind", "run") == "run" and gr["engine"].separated(gr["throttle"], 101325.0 if body == "Earth" else 640.0):
                    self.fail(f"engine {gr['engine'].name} on {gr['state'].el.id} has separated nozzle flow at liftoff")
            self.liftoff(thrust_dir)
            return self.flight_step(dt, groups, rcs_F, rcs_state, thrust_dir, {})
        # On the surface: time passes, burning engines that can't lift the vehicle just waste propellant.
        dt = min(dt, 86400.0 * 30)
        for gr in groups:
            self.consume(gr, dt)
        if self.mode != "landed":
            self.t += dt
            return np.zeros(3)
        left = dt
        while left > 0:
            h = min(left, 88775.244)
            self.t += h
            left -= h
            self.resources(h, "mars_surface")
            if any(s.el.kind == "capsule" for s in self.present()):
                self._set_partial("samples_loaded", self.surface_ops_ok_s / (D.SURFACE_OPS_SOLS * 88775.244))
            if self.telemetry is not None:
                self._tele({"kind": "sol", "body": "Mars", "phase": "mars_surface", **(self.track.get("power") or {}),
                            "propellant": {s.el.id: round(s.fuel + s.ox, 2) for s in self.states if s.el.is_stage},
                            "progress": round(self.progress(), 6)})
            if not self.samples_loaded and self.surface_ops_ok_s >= D.SURFACE_OPS_SOLS * 88775.244:
                cap = next((s for s in self.present() if s.el.kind == "capsule"), None)
                if cap is not None:
                    cap.dry += self._w_sample_kg()
                    self.samples_loaded = True
                    self._w_samples_loaded()
                    self.event("samples_loaded", element=cap.el.id)
                    self.milestone("samples_loaded")
        return np.zeros(3)

    def site_state(self):
        """Inertial state of the vehicle standing 1 m above its surface site. Earth sites are geodetic (map)
        latitudes with heights above the WGS84 ellipsoid (data.SITES); Mars sites are areocentric, with heights
        above the reference sphere."""
        from tudatpy.dynamics import environment
        from tudatpy.astro import element_conversion
        from .frames import geodetic_to_fixed
        lat, lon, alt = self.surface_site
        if self.central == "Earth":
            fixed = np.concatenate([geodetic_to_fixed(lat, lon, alt + 1.0), np.zeros(3)])
        else:
            fixed = element_conversion.spherical_to_cartesian_elementwise(MARS_REF_RADIUS + alt + 1.0, math.radians(lat),
                                                                         math.radians(lon), 0.0, 0.0, 0.0)
        return np.array(environment.transform_to_inertial_orientation(fixed, self.t, self.bodies.get(self.central).rotation_model))

    def _arc_row(self, groups, cmd, reason):
        """One telemetry row at the end of every arc: the full state and what every part is doing."""
        try:
            if self.x is None:   # the arc ended in a landing
                lat, lon, salt = self.surface_site
                self._tele({**(self.rb.tele() if self.rb is not None else {}),
                            "kind": "arc", "t": self.t, "body": self.central, "phase": self.phase(), "reason": reason,
                            "x": None, "alt": salt, "lat": lat, "lon": lon, "mass": self.total_mass(),
                            "dir": None if self.dir is None else self.dir.tolist(), "elements": [s.el.id for s in self.states],
                            "propellant": {s.el.id: round(s.fuel + s.ox, 2) for s in self.states if s.el.is_stage},
                            "chutes_deployed": sorted(self.chute_deployed), "progress": round(self.progress(), 6)})
                return
            alt = self.altitude(self.x[:3], self.t) if self.central != "Sun" else None
            lat = lon = None
            if self.central != "Sun":
                lat, lon = self.map_latlon(self.x[:3], self.t)   # geodetic (WGS84) at Earth, areocentric at Mars
            thr, lit = {}, {}
            for gr in groups:
                eid = gr["state"].el.id
                thr[eid] = max(thr.get(eid, 0.0), gr["throttle"])
                lit[eid] = lit.get(eid, 0) + gr["n"]
            self._tele({
                **(self.rb.tele() if self.rb is not None else {}),
                "kind": "arc", "t": self.t, "body": self.central, "phase": self.phase(), "reason": reason,
                "x": self.x.tolist(), "alt": alt, "lat": lat, "lon": lon, "mass": self.total_mass(),
                "dir": None if self.dir is None else self.dir.tolist(), "cmd_dir": cmd.get("dir"),
                "bank_deg": cmd.get("bank_deg", 0.0), "throttle": thr, "engines_lit": lit,
                "elements": [s.el.id for s in self.states],
                "propellant": {s.el.id: round(s.fuel + s.ox, 2) for s in self.states if s.el.is_stage},
                "rcs": {s.el.id: round(s.rcs, 2) for s in self.states if s.el.rcs_prop},
                "chutes_deployed": sorted(self.chute_deployed),
                "heat_load_Jcm2": {k: round(v / 1e4, 2) for k, v in self.heat_load.items()},
                "heat_capacity_Jcm2": {k: round(v, 2) for k, v in (self.track.get("heat_capacity_Jcm2") or {}).items()},
                "mach": self.last.get("mach"), "q": self.last.get("q"), "progress": round(self.progress(), 6)})
        except Exception:  # noqa: BLE001
            pass

    def after_arc(self, reason, alt0):
        central = self.central
        if central != "Sun":
            alt = self.altitude(self.x[:3], self.t)
        else:
            alt = float("inf")
        # milestones along the way
        if central == "Earth" and not ("mars_departure" in self.milestones):
            if self.w:
                self._w_range_after_arc(alt)
            if alt > 100:
                self.milestone("liftoff")
            if alt > 50e3:
                self.milestone("max_q_passed")
            if alt > 150e3:
                energy, e, rp = self.orbit_elements()
                if energy >= 0 or rp - EARTH_R_EQ > 150e3:
                    self.milestone("earth_orbit")
        if central == "Mars" and not self.mars_lifted:
            if self.w:
                self._w_edl_sample(alt)
            air = self.x[3:] - self.omega_cross(self.x[:3])
            if self.mars_entry_started and np.linalg.norm(air) < 1000 and alt < 125e3:
                self.milestone("mars_entry")
                if "mars_entry" in self.milestones and "mars_entry_agl" not in self.track:
                    self.track["mars_entry_agl"] = alt - self.ground_alt(self.x[:3], self.t)
            if "mars_entry" in self.milestones and alt - self.ground_alt(self.x[:3], self.t) < 2000:
                self.milestone("mars_descent")
        if central == "Mars" and self.mars_lifted and alt > 100e3:
            energy, e, rp = self.orbit_elements()
            if energy >= 0 or rp - MARS_REF_RADIUS > 100e3:
                self.milestone("mars_orbit")
        if central == "Earth" and "earth_arrival" in self.milestones:
            pres = self.present()
            if alt < 120e3:
                self.capsule_entry_started = True
            air = self.x[3:] - self.omega_cross(self.x[:3])
            if self.capsule_entry_started and np.linalg.norm(air) < 500 and pres and pres[0].el.kind == "capsule":
                self.milestone("earth_entry")
        # discrete events
        if reason == "soi_exit":
            if central == "Earth":
                self.switch_central("Sun")
                self._start_cruise()
                self.milestone("earth_departure")
            elif central == "Mars":
                self.switch_central("Sun")
                self._start_cruise()
                if any(s.el.kind == "capsule" for s in self.present()) and self.samples_loaded:
                    self.milestone("mars_departure")
            self.event("soi_exit", body=central)
        elif reason == "soi_enter_Mars":
            self.switch_central("Mars")
            self.disp.reach_mars(self.world.mars_sol(self.t))
            self.milestone("mars_arrival")
            self.event("soi_enter", body="Mars")
        elif reason == "soi_enter_Earth":
            self.switch_central("Earth")
            if "mars_departure" in self.milestones:
                self.milestone("earth_arrival")
            self.event("soi_enter", body="Earth")
        elif reason == "surface":
            self.touchdown()
        elif reason == "atmosphere_interface":
            self.event("atmosphere_interface", body=central)
        elif reason in ("alt_below", "alt_above"):
            self.event(reason, alt=alt)
