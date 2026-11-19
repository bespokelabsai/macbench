"""Tudat set-up and the propagation of one arc, with its crossings.

Methods of Mission, moved verbatim from sim.py. Mission inherits them from ArcsMixin."""
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


# The bodies added to the Sun-centred arcs beyond Tudat's default Sun, Venus, Earth, Moon and Mars: name in this
# simulation -> (SPICE name in DE440, GM in m^3/s^2). The GMs are DE440's own constants (Park et al. 2021, AJ 161:105,
# the de440 header; Orekit 13.1.9 reads the same numbers). For the giant planets DE440 gives only the system
# barycentre, so the GM is the system's (planet plus moons). Mercury has no moons.
SUN_ARC_BODIES = {
    "Mercury_DE440": ("MERCURY BARYCENTER", 2.20318685514e13),
    "Jupiter_Barycenter": ("JUPITER BARYCENTER", 1.267127641e17),
    "Saturn_Barycenter": ("SATURN BARYCENTER", 3.79405848418e16),
    "Uranus_Barycenter": ("URANUS BARYCENTER", 5.7945564e15),
    "Neptune_Barycenter": ("NEPTUNE BARYCENTER", 6.83652710058e15),
}


class ArcsMixin:
    # ------------------------------------------------------------------ setup
    def _build_vehicle_bodies(self):
        from tudatpy.dynamics import environment_setup as es, propagation_setup as ps
        b = self.bodies
        self.ctl = {"thrust_groups": [], "rcs": 0.0, "mdot": 0.0, "aero": None, "bank": 0.0, "dir": np.array([1.0, 0, 0])}
        # One powered-flight body per central body. A body's flight conditions stay tied to the planet it
        # first flew near, and Tudat cannot evaluate them once another body is central.
        for cb in ("Earth", "Mars", "Sun"):
            name = "VT" + cb
            b.create_empty_body(name)
            b.get(name).mass = 1000.0
            coeff_t = es.aerodynamic_coefficients.custom_aerodynamic_force_coefficients(
                self._coeffs_thrust, 1.0,
                [es.aerodynamic_coefficients.mach_number_dependent, es.aerodynamic_coefficients.angle_of_attack_dependent,
                 es.aerodynamic_coefficients.sideslip_angle_dependent, es.aerodynamic_coefficients.time_dependent],
                force_coefficients_frame=es.aerodynamic_coefficients.negative_body_fixed_frame_coefficients)
            es.add_aerodynamic_coefficient_interface(b, name, coeff_t)
            es.add_rotation_model(b, name, es.rotation_model.custom_inertial_direction_based(
                lambda t: self.ctl["dir"].reshape(3, 1), "J2000", name + "_Fixed"))
            es.add_engine_model(name, "E", ps.thrust.custom_thrust_magnitude(self._thrust_mag, self._isp), b)
        for cb in ("Earth", "Mars"):
            name = "VE" + cb
            b.create_empty_body(name)
            b.get(name).mass = 1000.0
            coeff_e = es.aerodynamic_coefficients.custom_aerodynamic_force_coefficients(
                self._coeffs_entry, 1.0, [es.aerodynamic_coefficients.mach_number_dependent,
                                          es.aerodynamic_coefficients.time_dependent],
                force_coefficients_frame=es.aerodynamic_coefficients.negative_aerodynamic_frame_coefficients)
            es.add_aerodynamic_coefficient_interface(b, name, coeff_e)
            es.add_rotation_model(b, name, es.rotation_model.aerodynamic_angle_based(
                cb, "J2000", name + "_Fixed", lambda t: np.array([[0.0], [0.0], [self.bank_at(t)]])))

    def _accels(self, body, central, atm, canopy=None):
        key = (body, central, atm, canopy)
        if key in self._acc_cache:
            return self._acc_cache[key]
        from tudatpy.dynamics import propagation_setup as ps
        pm = ps.acceleration.point_mass_gravity()
        if central == "Sun":
            # Every planet pulls on a coast around the Sun (checked against Orekit and GMAT): the
            # outer planets and Mercury move a 150 to 170 day coast by 440 to 680 km. The giant planets act
            # through their system barycentres with the system GMs, as in DE440.
            self._add_sun_arc_bodies()
            acc = {"Sun": [pm]}
            for p in ("Venus", "Earth", "Moon", "Mars") + tuple(SUN_ARC_BODIES):
                acc[p] = [pm]
        else:
            # Mars: the gravity.mars provider's degree (JGMRO_120F at 30 by default); Earth: 12
            n, mo = self.world.mars_gravity_degree if central == "Mars" else (12, 12)
            planet = [ps.acceleration.spherical_harmonic_gravity(n, mo)]
            if atm:
                planet.append(ps.acceleration.aerodynamic())
            acc = {central: planet, "Sun": [pm], "Jupiter": [pm], "Venus": [pm]}
            acc["Mars" if central == "Earth" else "Earth"] = [pm]
            if central == "Earth":
                acc["Moon"] = [pm]
        if body.startswith("VT"):
            acc[body] = [ps.acceleration.thrust_from_all_engines()]
        decay = getattr(self, "decay", None)
        if decay is not None and not atm and central in ("Earth", "Mars"):
            # drag between the atmosphere interface and 1000 km (MARSFLIGHT_ORBIT_DECAY)
            acc.setdefault(body, []).append(decay.tudat_acceleration(body, central))
        if canopy:
            # a parachute canopy flown as its own body (6-DOF entry): the central body's gravity, and its drag
            # and riser pull (rb.canopy_acc)
            accs = {body: acc, canopy: {central: [pm], canopy: [ps.acceleration.custom_acceleration(self.rb.canopy_acc)]}}
            models = ps.create_acceleration_models(self.bodies, accs, [body, canopy], [central, central])
        else:
            models = ps.create_acceleration_models(self.bodies, {body: acc}, [body], [central])
        mr = None
        if body.startswith("VT"):
            mr = ps.create_mass_rate_models(self.bodies, {body: [ps.mass_rate.from_thrust()]}, models)
        self._acc_cache[key] = (models, mr)
        return models, mr

    def _add_sun_arc_bodies(self):
        """Create the extra point masses of the Sun-centred arcs in this flight's system of bodies, once: each
        follows its DE440 ephemeris through SPICE (environment.load_kernels has loaded de440.bsp) and has its DE440
        GM. Tudat's own "Jupiter" is the planet's centre with the planet's GM; it stays as it is for the arcs
        near Earth and Mars."""
        from tudatpy.dynamics import environment_setup as es
        b = self.bodies
        for name, (spice_name, gm) in SUN_ARC_BODIES.items():
            if b.does_body_exist(name):
                continue
            b.create_empty_body(name)
            b.get(name).ephemeris = es.create_body_ephemeris(es.ephemeris.direct_spice("SSB", "J2000", spice_name), name)
            es.add_gravity_field_model(b, name, es.gravity_field.central(gm))

    def flight_step(self, dt, groups, rcs_F, rcs_state, thrust_dir, cmd):
        from tudatpy.dynamics import propagation_setup as ps, simulator

        central = self.central
        alt0 = self.altitude(self.x[:3], self.t) if central != "Sun" else float("inf")
        in_atm = central in ATM_TOP and alt0 < ATM_TOP[central] + 5e3
        thrusting = bool(groups) or rcs_F > 0   # tail-offs included
        # With MARSFLIGHT_ENGINE_TRANSIENTS off every group is a running one, so powered is the same as thrusting.
        powered = rcs_F > 0 or any(g.get("kind", "run") == "run" for g in groups)
        # arc length limits
        if in_atm:
            dt = min(dt, 10.0 if powered else 20.0)
        elif powered:
            dt = min(dt, 120.0)
        dt = min(max(dt, 0.1), 86400.0 * 60)
        t_dep = self.time_to_depletion(groups, rcs_F, rcs_state, dt)
        burnout = False
        if t_dep < dt:
            dt, burnout = max(t_dep, 1e-3), True
        self._plan_bank(dt)

        air = self.x[3:] - self.omega_cross(self.x[:3]) if central != "Sun" else self.x[3:]
        retro = float(np.dot(unit(thrust_dir), unit(air))) < -0.5 if (thrusting or self.rb is not None) else False
        aero = self.aero_config(retro) if in_atm else None
        blunt = aero is not None and aero["kind"] == "blunt"
        use_entry = in_atm and blunt and not thrusting and cmd.get("mode", "entry") != "thrust"
        if use_entry and central == "Earth" and False:
            pass
        six = self.rb.arc_body(central, thrusting, in_atm, use_entry) if self.rb is not None else None
        # A 6-DOF arc flies the rigid body, also a lifting entry when MARSFLIGHT_ENTRY6DOF is on; the rotational
        # propagator is set up for that body, so the translational one must use it too.
        body = six or (("VE" + central) if use_entry else ("VT" + central))
        vt = body.startswith("VT")
        if vt:
            self.ctl["vt"] = body
        self.ctl.update({"thrust_groups": groups if vt else [], "rcs": rcs_F if vt else 0.0,
                         "aero": aero, "dir": unit(thrust_dir), "atm": in_atm, "entry": bool(use_entry and six)})
        if not use_entry and blunt and in_atm:
            # Powered flight with a blunt body leading: drag as a bluff body (no lift).
            self.ctl["aero"] = {"kind": "bluff", "S": aero["S"], "D": aero["D"]}
        m0 = self.total_mass()
        self.bodies.get(body).mass = m0
        if six:
            self.rb.begin_arc(body, groups, rcs_F, dt, in_atm, self.ctl["aero"], rcs_state, entry=use_entry)

        if getattr(self, "decay", None) is not None:
            els = [s.el for s in self.present()]
            held = any(getattr(e, "rcs_thrust", 0.0) > 0 for e in els)   # it can hold the commanded attitude
            self.decay.set_stack(els, m0, axis=thrust_dir if held else None)
        canopy = self.rb.canopy_state() if six else None   # a parachute canopy flown as its own body
        models, mr = self._accels(body, central, in_atm, canopy[0] if canopy else None)
        dv = []
        if central != "Sun":
            dv += [ps.dependent_variable.altitude(body, central), ps.dependent_variable.relative_distance(body, central)]
            if in_atm:
                dv += [ps.dependent_variable.mach_number(body, central), ps.dependent_variable.dynamic_pressure(body, central),
                       ps.dependent_variable.density(body, central), ps.dependent_variable.airspeed(body, central),
                       ps.dependent_variable.single_acceleration_norm(ps.acceleration.aerodynamic_type, body, central)]
                if body.startswith("VT"):
                    # angle of attack (7), sideslip (8) and the aerodynamic acceleration vector with its lift and
                    # side force (9 to 11)
                    dv += [ps.dependent_variable.angle_of_attack(body, central), ps.dependent_variable.sideslip_angle(body, central),
                           ps.dependent_variable.single_acceleration(ps.acceleration.aerodynamic_type, body, central)]
            if body.startswith("VT"):
                dv += [ps.dependent_variable.single_acceleration_norm(ps.acceleration.thrust_acceleration_type, body, body)]
        else:
            dv += [ps.dependent_variable.relative_distance(body, "Earth"), ps.dependent_variable.relative_distance(body, "Mars")]
            if body.startswith("VT"):
                dv += [ps.dependent_variable.single_acceleration_norm(ps.acceleration.thrust_acceleration_type, body, body)]
        k6 = None
        if six:
            # The 6-DOF outputs go before the last one, so the indices the checks below use stay valid.
            dv[len(dv) - 1:len(dv) - 1] = self.rb.extra_dependent_variables(body, central, in_atm)
            # k6 indexes the flattened output, where the aerodynamic acceleration vector (9 to 11) takes three
            k6 = len(dv) - 2 + (2 if in_atm and central != "Sun" and body.startswith("VT") else 0)

        # Boundaries. A plain termination stops at the end of the integrator step that crosses one. Tudat's
        # exact (root-finder) termination is not used: it probes states far outside the physical range, where
        # the Mars table and NRLMSISE-00 fail. Crossings that need precision are refined below instead.
        limits = []   # (dependent variable index, limit, lower, name)
        if central != "Sun":
            limits.append((1, EARTH_SOI if central == "Earth" else MARS_SOI, False, "soi_exit"))
            if central in ATM_TOP:
                limits.append((0, self.ground_floor(), True, "surface"))
                if not in_atm:
                    limits.append((0, ATM_TOP[central], True, "atmosphere_interface"))
            wake = cmd.get("wake") or {}
            for key, lower in (("alt_below", True), ("alt_above", False)):
                if key in wake:
                    try:
                        lim = float(wake[key])
                        if (lower and alt0 > lim) or (not lower and alt0 < lim):
                            limits.append((0, lim, lower, key))
                    except (TypeError, ValueError):
                        pass
        else:
            limits.append((0, 0.98 * EARTH_SOI, True, "soi_enter_Earth"))
            limits.append((1, 0.98 * MARS_SOI, True, "soi_enter_Mars"))

        def limit_variable(idx):
            if central != "Sun":
                return (ps.dependent_variable.altitude(body, central) if idx == 0
                        else ps.dependent_variable.relative_distance(body, central))
            return ps.dependent_variable.relative_distance(body, "Earth" if idx == 0 else "Mars")

        def propagate(t_stop, with_limits=True):
            """Fly the arc to t_stop. A fixed-step arc is split where an engine's thrust starts, so that no RK4
            step straddles the jump: each of a step's stages would see the thrust either off or on, and a burn
            would get up to a third of a step of full thrust too much or too little."""
            cuts = []
            if in_atm or thrusting:
                # also split where a start or a tail-off ends: those pieces fly with TRANSIENT_STEP_S steps
                cuts = sorted({x for g in groups for x in self.prop_arc_edges(g) if self.t + 1e-6 < x < t_stop - 1e-6})
            # With a canopy (rb.canopy_begin) the translational state is the vehicle's, then the canopy's.
            x0 = np.concatenate([self.x, canopy[1]]) if canopy else self.x
            n_tr, t0, mass0, rot0 = len(x0), self.t, m0, None
            sh_all, dvh_all = {}, {}
            for t_end in cuts + [t_stop]:
                sh, dvh = propagate_part(t0, x0, mass0, t_end, with_limits, rot0, self.prop_fine(groups, t0, t_end))
                sh_all.update(sh)
                dvh_all.update(dvh)
                t_last = max(sh)
                if t_last < t_end - 1e-6:   # a boundary stopped it
                    break
                t0, x0 = t_last, np.array(sh[t_last][:n_tr])
                if six:
                    # A 6-DOF state is position and velocity (6, or 12 with a canopy), quaternion and body rates
                    # (7), then the mass.
                    rot0 = np.array(sh[t_last][n_tr:n_tr + 7])
                if body.startswith("VT"):
                    mass0 = float(sh[t_last][n_tr + 7 if six else n_tr])
            return sh_all, dvh_all

        def propagate_part(t_start, x_start, m_start, t_stop, with_limits, rot_start=None, fine=None):
            self.ctl["t_seg"] = t_start
            terms = [ps.propagator.time_termination(t_stop, terminate_exactly_on_final_condition=True),
                     # "next": 1e12 s in Earth orbit hung the simulator in one arc, past the flight's wall-clock limit
                     ps.propagator.custom_termination(lambda _t: time.time() - self.wall0 > self.max_wall_s)]
            if with_limits:
                terms += [ps.propagator.dependent_variable_termination(limit_variable(i), lim, use_as_lower_limit=lo)
                          for i, lim, lo, _ in limits]
            term = ps.propagator.hybrid_termination(terms, fulfill_single_condition=True)
            span = max(t_stop - t_start, 1e-3)
            # A 6-DOF vehicle arc always takes fixed steps (the rotational propagator needs the step), also in a
            # tail-off in vacuum, which the start and shutdown transients otherwise flies with the variable step.
            n = 1
            var6 = self.rb.integrator(span) if six else None
            if var6 is not None:
                integ = var6   # a 6-DOF entry or unpowered canopy arc: variable step
            elif fine or in_atm or powered or (six and body.startswith("VT")):
                hmax = fine if fine else (self.rb.step_size(in_atm) if six else (0.25 if in_atm else 1.0))
                n = max(1, math.ceil(span / hmax))
                integ = ps.integrator.runge_kutta_fixed_step(span / n, coefficient_set=ps.integrator.CoefficientSets.rk_4)
            else:
                ctrl = ps.integrator.step_size_control_elementwise_scalar_tolerance(1e-10, 1e-10)
                val = ps.integrator.step_size_validation(1e-3, 86400.0 * 2)
                integ = ps.integrator.runge_kutta_variable_step(min(span, 10.0), ps.integrator.CoefficientSets.rkf_78,
                                                                 step_size_control_settings=ctrl, step_size_validation_settings=val)
            if canopy:
                tr = ps.propagator.translational([central, central], models, [body, canopy[0]],
                                                 x_start, t_start, integ, term, output_variables=dv)
            else:
                tr = ps.propagator.translational([central], models, [body], x_start, t_start, integ, term, output_variables=dv)
            if body.startswith("VT"):
                ms = ps.propagator.mass([body], mr, [m_start], t_start, integ, term)
                parts = [tr, self.rb.rotational(body, integ, term, span / n, t_start, rot_start), ms] if six else [tr, ms]
                prop = ps.propagator.multitype(parts, integ, t_start, term, dv)
            else:
                prop = tr
            prop.print_settings.print_initial_and_final_conditions = False
            try:
                sim = simulator.create_dynamics_simulator(self.bodies, prop)
            except Exception as ex:  # noqa: BLE001
                raise FlightFailure(f"propagation failed: {ex}")
            r = sim.propagation_results
            sh, dvh = r.state_history, r.dependent_variable_history
            self._check_propagation(r, sh, dvh, t_start)
            return sh, dvh

        sh, dvh = propagate(self.t + dt)
        ts = sorted(sh.keys())
        # A long coast step can end far past the atmosphere interface, a wake altitude or a sphere of influence.
        # Re-fly the arc to the crossing, by false position between the two samples around it. Every trial
        # stays inside that bracket, so every state it visits is physical.
        cross = None
        for idx, lim, lower, name in limits:
            if name in ("surface", "soi_exit"):
                continue   # fixed 0.25 s steps near the ground; the edge of a sphere of influence needs no precision
            vals = [float(dvh[t][idx]) for t in ts]
            for k in range(1, len(ts)):
                fa, fb = vals[k - 1] - lim, vals[k] - lim
                if (lower and fa > 0 >= fb) or (not lower and fa < 0 <= fb):
                    if cross is None or ts[k - 1] < cross[0]:
                        cross = (ts[k - 1], fa, ts[k], fb, idx, lim)
                    break
        # Over a terrain map the ground under the path is not one altitude: find where the path meets it.
        gc = self._ground_crossing(ts, sh, dvh) if central in ATM_TOP else None
        if gc is not None and (cross is None or gc[0] < cross[0]):
            cross = (*gc, "ground", None)
        if cross is not None:
            ta, fa, tb, fb, idx, lim = cross
            ground = idx == "ground"
            tol = 0.05 if ground else (50.0 if (central != "Sun" and idx == 0) else 1.0e5)
            if ground and abs(fb) <= tol:
                sh = {t: sh[t] for t in ts if t <= tb}   # the arc ends at its first sample on the ground
                dvh = {t: dvh[t] for t in ts if t <= tb}
            elif abs(fb) > tol:
                fc = fb
                for _ in range(8):
                    tc = ta + (tb - ta) * fa / (fa - fb)
                    tc = min(max(tc, ta + 1e-3), tb)
                    sh, dvh = propagate(tc, with_limits=False)
                    fc = self._agl(sh[max(sh)], max(sh)) if ground else float(dvh[max(dvh)][idx]) - lim
                    if abs(fc) < tol or tb - ta < 1e-2:
                        break
                    if (fc > 0) == (fa > 0):
                        ta, fa = tc, fc
                    else:
                        tb, fb = tc, fc
                if ground and fc > tol:
                    sh, dvh = propagate(tb, with_limits=False)   # end on the ground side, so the arc is a touchdown
            ts = sorted(sh.keys())
        t1 = ts[-1]
        x1 = np.array(sh[t1][:6])
        if not np.all(np.isfinite(np.asarray(sh[t1], dtype=float))):   # position, velocity, mass, 6-DOF attitude
            self.fail("the propagated state is not finite (NaN or infinity)")
        reason = self._arc_end_reason(central, x1, t1, alt0, in_atm, cmd)
        if self.w:
            self._w_protection_after_arc(central, x1, t1)   # Earth avoidance with Mars material aboard
        real_dt = t1 - self.t
        if real_dt < 1e-6:
            self.track["stalled_arcs"] = self.track.get("stalled_arcs", 0) + 1
            if self.track["stalled_arcs"] > 200:
                self.fail("the simulation stalled: 200 arcs in a row advanced no time")
        else:
            self.track["stalled_arcs"] = 0
        # bookkeeping
        before = {id(gr["state"]): (gr["state"].fuel, gr["state"].ox) for gr in groups}
        for gr in groups:
            if body.startswith("VT"):
                self.consume(gr, self.t, t1)   # from self.t to t1; along the start or tail-off profile with the start and shutdown transients
        if body.startswith("VT") and groups:
            self.check_pogo(groups, before)
        if rcs_F and rcs_state is not None and body.startswith("VT"):
            rcs_state.rcs = max(0.0, rcs_state.rcs - rcs_F / (D.RCS_ISP * D.G0) * real_dt)
            if real_dt > 0:
                self.exposures.append((self.t + real_dt, "rcs", rcs_state.el.id, rcs_F / 1e3 * real_dt))
        sensed = np.zeros(3)
        # post-arc physics checks on the dependent-variable history
        if central != "Sun" and in_atm:
            self.check_atmospheric(ts, dvh, body, aero, groups, thrust_dir)
            self.check_aero_noise(ts, dvh, body)
        elif body.startswith("VT") and thrusting and dvh:
            # Above the air the payload and the tanks still carry the thrust load, which peaks as the propellant
            # runs out: the same g limits and stage structure check as in the atmosphere.
            tk = max(ts, key=lambda t: float(dvh[t][-1]))
            g_vac = float(dvh[tk][-1]) / D.G0
            self.peak["g"] = max(self.peak["g"], g_vac)
            self.check_g_limits(g_vac, f"at {float(dvh[tk][0]) / 1e3:.1f} km, above the atmosphere"
                                if central != "Sun" else "in a burn in solar orbit")
            self.check_structure(0.0, 0.0, g_vac, None)
        if body.startswith("VT"):
            k_thr = -1
            a_thr = float(dvh[t1][k_thr]) if dvh else 0.0
            sensed = unit(thrust_dir) * a_thr
        if in_atm and dvh:
            if body.startswith("VT"):
                sensed = sensed + np.array(dvh[t1][9:12], dtype=float)   # drag, lift and side force
            else:
                a_aero = float(dvh[t1][6])
                sensed = sensed - unit(x1[3:] - self.omega_cross(x1[:3])) * a_aero
        if six:
            sensed = self.rb.end_arc(sh, dvh, ts, k6, in_atm)
        elif self.rb is not None:
            self.rb.coast(real_dt, entry=use_entry, x_end=x1)
        dmin_earth = dmin_mars = None
        if central == "Sun" and dvh:
            dmin_earth = min(float(dvh[t][0]) for t in ts)
            dmin_mars = min(float(dvh[t][1]) for t in ts)
        if self.telemetry is not None and len(ts) > 1:
            # Sub-arc states for replays: about 1 s in the atmosphere, 2 s under power, 40 per coast.
            gap = 1.0 if in_atm else (2.0 if thrusting else max(1.0, (ts[-1] - ts[0]) / 40.0))
            last_t = -1e30
            for t in ts[:-1]:
                if t - last_t >= gap:
                    last_t = t
                    self._tele({"kind": "state", "met": round(t - self.t0, 3), "body": central,
                                "x": [float(v) for v in sh[t][:6]]})
        if getattr(self, "live", None) is not None and len(ts) > 2:
            self.live.arc(self, central, ts, sh)   # points along a long arc, for the flight viewer (marsflight.live)
        self._end_bank(t1)
        self.t = t1
        self.x = x1
        if self.rb is not None:
            self.rb.track(ts, sh, dvh, k6, "6dof" if six else ("entry" if use_entry else "coast"), in_atm)
        if self.log_arcs:
            self.traj.append([self.t - self.t0, central, *self.x.tolist(), self.total_mass()])
        burnt = self._burn_through(groups, t1)   # over all its burns (models/propulsion/engines.py)
        if burnt:
            self.fail(burnt[1])
        if not (self.mars_lifted and central == "Mars") and central == "Mars" and not self.mars_entry_started and in_atm:
            self.mars_entry_started = True
        if not in_atm and real_dt > 0:
            if self.w:
                self._w_space_arc_power(central, ts, sh, t1 - real_dt, real_dt, thrusting, thrust_dir)
            self.resources(real_dt, "space")
            if self.w:
                self._w_eclipse_check()
        # Burnout: a stage whose engines were lit and whose propellant is gone, by burning or by boiloff.
        for s in self.present():
            if s.lit and self.prop_empty(s):   # empty point: 1e-6 kg, or the low level cutoff
                self.prop_on_burnout(s)
        self.after_arc(reason, alt0)
        self._track(dmin_earth, dmin_mars)
        if self.telemetry is not None:
            self._arc_row(groups, cmd, reason)
        return sensed

    def _check_propagation(self, r, sh, dvh, t_start):
        """End the flight with a clear failure when Tudat stopped an arc on a state that is not a finite number. Tudat
        then returns the history up to that state, whose dependent variables it could not compute: reading on gave a
        KeyError, or, when the bad state was the arc's first, an arc that advanced no time, again and again until the
        stall check (sim-robust)."""
        from tudatpy.dynamics import propagation
        nan_stop = False
        try:
            nan_stop = (r.termination_details.termination_reason
                        == propagation.PropagationTerminationReason.nan_or_inf_detected_in_state)
        except (AttributeError, RuntimeError):
            pass
        t_bad = None
        if nan_stop or not sh:
            t_bad = max(sh) if sh else t_start
        else:
            missing = [t for t in sh if t not in dvh]
            if missing:
                t_bad = min(missing)
            else:
                t_last = max(sh)
                if not np.all(np.isfinite(np.asarray(sh[t_last], dtype=float))):
                    t_bad = t_last
        if t_bad is not None:
            self.fail(f"the vehicle's state became invalid (not a finite number) at mission time {t_bad - self.t0:.1f} s: "
                      "the flight dynamics diverged and the simulation cannot continue")

    def _arc_end_reason(self, central, x1, t1, alt0, in_atm, cmd):
        """Why an arc ended, decided from the final state. Tudat's list of met conditions is unreliable when
        a root finder stops the arc exactly on a boundary, and an event at the arc's end must outrank time."""
        r = float(np.linalg.norm(x1[:3]))
        if central != "Sun":
            if central in ATM_TOP:
                alt = self.altitude(x1[:3], t1)
                if alt <= self.ground_alt(x1[:3], t1) + 0.05:   # root finding stops within 1 mm; liftoff starts 1 m up
                    return "surface"
            soi = EARTH_SOI if central == "Earth" else MARS_SOI
            if r >= soi - 10.0:
                return "soi_exit"
            if central in ATM_TOP:
                if not in_atm and alt <= ATM_TOP[central] + 100.0:
                    return "atmosphere_interface"
                wake = cmd.get("wake") or {}
                try:
                    if "alt_below" in wake and alt0 > float(wake["alt_below"]) and alt <= float(wake["alt_below"]) + 100.0:
                        return "alt_below"
                    if "alt_above" in wake and alt0 < float(wake["alt_above"]) and alt >= float(wake["alt_above"]) - 100.0:
                        return "alt_above"
                except (TypeError, ValueError):
                    pass
            return "time"
        for planet, soi, nm in (("Earth", EARTH_SOI, "soi_enter_Earth"), ("Mars", MARS_SOI, "soi_enter_Mars")):
            d = float(np.linalg.norm(x1[:3] - (self.central_state(planet, t1)[:3] - self.central_state("Sun", t1)[:3])))
            if d <= 0.98 * soi + 2.0e5:
                return nm
        return "time"
