"""Coasting-trajectory predictor with the simulator's own force models (Tudat in the flight software
process): central-body spherical harmonics 12x12 near Earth and Mars, point-mass Sun, Moon, Venus,
Jupiter and the other planet, RKF7(8) at 1e-10, central-body switching at the spheres of influence."""
from __future__ import annotations

import math

import numpy as np

from .util import kernels

EARTH_SOI = 9.24e8
MARS_SOI = 5.77e8
_B = None
_ACC = {}
# Every planet pulls on a coast around the Sun (manual, "Planet positions and gravity": the Sun, every planet and
# the Moon at their DE440 positions). Mercury and the outer planets move a 150 to 200 day cruise by several hundred
# km, which the Utah landing cannot afford. The giant planets act through their DE440 system barycentres with the
# system GMs (DE440 header constants, Park et al. 2021).
SUN_ARC_BODIES = {
    "Mercury_DE440": ("MERCURY BARYCENTER", 2.20318685514e13),
    "Jupiter_Barycenter": ("JUPITER BARYCENTER", 1.267127641e17),
    "Saturn_Barycenter": ("SATURN BARYCENTER", 3.79405848418e16),
    "Uranus_Barycenter": ("URANUS BARYCENTER", 5.7945564e15),
    "Neptune_Barycenter": ("NEPTUNE BARYCENTER", 6.83652710058e15),
}


def bodies():
    global _B
    if _B is not None:
        return _B
    kernels()
    from tudatpy.dynamics import environment_setup as es
    bs = es.get_default_body_settings(["Sun", "Venus", "Earth", "Moon", "Mars", "Jupiter"], "SSB", "J2000")
    # Mars and Jupiter on their DE440 barycentres, as the simulator flies them: Tudat's merged kernel,
    # the only source of Mars's centre, ends in 2040, and a slipped mission ran past it. Mars's centre is 0.2 m away.
    for body, sp in (("Mars", "MARS BARYCENTER"), ("Jupiter", "JUPITER BARYCENTER")):
        bs.get(body).ephemeris_settings = es.ephemeris.direct_spice("SSB", "J2000", sp)
    bs.get("Earth").atmosphere_settings = None
    bs.get("Earth").shape_settings = es.shape.oblate_spherical(6378137.0, 1.0 / 298.257223563)
    bs.get("Mars").gravity_field_settings = es.gravity_field.predefined_spherical_harmonic(
        es.gravity_field.PredefinedSphericalHarmonicsModel.jgmro120d, 12)
    bs.get("Mars").shape_settings = es.shape.spherical(3389.5e3)
    _B = es.create_system_of_bodies(bs)
    for name, (spice_name, gm) in SUN_ARC_BODIES.items():
        _B.create_empty_body(name)
        _B.get(name).ephemeris = es.create_body_ephemeris(es.ephemeris.direct_spice("SSB", "J2000", spice_name), name)
        es.add_gravity_field_model(_B, name, es.gravity_field.central(gm))
    _B.create_empty_body("P")
    _B.get("P").mass = 1000.0
    return _B


def _accels(central):
    if central in _ACC:
        return _ACC[central]
    from tudatpy.dynamics import propagation_setup as ps
    b = bodies()
    pm = ps.acceleration.point_mass_gravity()
    if central == "Sun":
        acc = {"Sun": [pm]}
        for p in ("Venus", "Earth", "Moon", "Mars") + tuple(SUN_ARC_BODIES):
            acc[p] = [pm]
    else:
        acc = {central: [ps.acceleration.spherical_harmonic_gravity(12, 12)], "Sun": [pm], "Jupiter": [pm], "Venus": [pm]}
        acc["Mars" if central == "Earth" else "Earth"] = [pm]
        if central == "Earth":
            acc["Moon"] = [pm]
    models = ps.create_acceleration_models(b, {"P": acc}, ["P"], [central])
    _ACC[central] = models
    return models


def _secant():
    from tudatpy.math import root_finders
    return root_finders.secant(1e-3, 1e-3, 1e-6, 30)


def central_state(body, t):
    return np.array(bodies().get(body).ephemeris.cartesian_state(t))


def to_central(x, frm, to, t):
    if frm == to:
        return np.array(x, float)
    return np.array(x, float) + central_state(frm, t) - central_state(to, t)


def _segment(t, x, central, t_stop, extra_terms=(), tol=1e-10, dense=False, alt_r=None):
    """One propagation leg. Returns (t1, x1, reason, history). alt_r: radius of the "alt" stop condition."""
    from tudatpy.dynamics import propagation_setup as ps, simulator
    terms = [ps.propagator.time_termination(t_stop, terminate_exactly_on_final_condition=True)]
    names = ["time"]
    if central != "Sun":
        soi = EARTH_SOI if central == "Earth" else MARS_SOI
        terms.append(ps.propagator.dependent_variable_termination(
            ps.dependent_variable.relative_distance("P", central), soi, use_as_lower_limit=False))
        names.append("soi_exit")
    else:
        terms.append(ps.propagator.dependent_variable_termination(ps.dependent_variable.relative_distance("P", "Earth"),
                                                                  0.98 * EARTH_SOI, use_as_lower_limit=True))
        names.append("soi_enter_Earth")
        terms.append(ps.propagator.dependent_variable_termination(ps.dependent_variable.relative_distance("P", "Mars"),
                                                                  0.98 * MARS_SOI, use_as_lower_limit=True))
        names.append("soi_enter_Mars")
    for nm, tm in extra_terms:
        terms.append(tm)
        names.append(nm)
    term = ps.propagator.hybrid_termination(terms, fulfill_single_condition=True)
    ctrl = ps.integrator.step_size_control_elementwise_scalar_tolerance(tol, tol)
    val = ps.integrator.step_size_validation(1e-3, 86400.0 * 2)
    dt0 = 10.0 if t_stop > t else -10.0
    integ = ps.integrator.runge_kutta_variable_step(dt0, ps.integrator.CoefficientSets.rkf_78,
                                                     step_size_control_settings=ctrl, step_size_validation_settings=val)
    prop = ps.propagator.translational([central], _accels(central), ["P"], np.array(x, float), t, integ, term)
    prop.print_settings.print_initial_and_final_conditions = False
    sim = simulator.create_dynamics_simulator(bodies(), prop)
    res = sim.propagation_results
    sh = res.state_history
    ts = sorted(sh.keys())
    reason = "time"
    t1, x1 = ts[-1], np.array(sh[ts[-1]])
    hist = [(tt, np.array(sh[tt])) for tt in ts] if dense else None
    # Decide why the leg ended from the final state. Asking Tudat for the met conditions after an exact
    # (root finder) stop prints a warning to stderr each time, so it is only the last resort.
    if abs(t1 - t_stop) < 1e-4:
        return t1, x1, "time", hist
    if alt_r is not None and np.linalg.norm(x1[:3]) <= alt_r + 1.0:
        return t1, x1, "alt", hist
    if central != "Sun":
        if np.linalg.norm(x1[:3]) >= (EARTH_SOI if central == "Earth" else MARS_SOI) * (1 - 1e-9):
            return t1, x1, "soi_exit", hist
    else:
        for pl, soi in (("Earth", EARTH_SOI), ("Mars", MARS_SOI)):
            if np.linalg.norm(to_central(x1, "Sun", pl, t1)[:3]) <= 0.98 * soi * (1 + 1e-9):
                return t1, x1, "soi_enter_" + pl, hist
    try:
        for nm, ok in zip(names, res.termination_details.was_condition_met_when_stopping):
            if ok:
                reason = nm
                break
    except Exception:  # noqa: BLE001
        pass
    return t1, x1, reason, hist


def coast(t, x, central, t_stop, stop_alt=None, stop_body=None, tol=1e-10, max_legs=8, stop_soi=None):
    """Coast like the simulator, switching central bodies. Stops at t_stop, or when the altitude over
    stop_body (Earth or Mars) falls below stop_alt. Returns (t, x, central, reason)."""
    from tudatpy.dynamics import propagation_setup as ps
    for _ in range(max_legs):
        extra = []
        if stop_alt is not None and central == stop_body:
            extra.append(("alt", ps.propagator.dependent_variable_termination(
                ps.dependent_variable.relative_distance("P", central),
                stop_alt + (3389.5e3 if central == "Mars" else 6378137.0), use_as_lower_limit=True,
                terminate_exactly_on_final_condition=True,
                termination_root_finder_settings=_secant())))
        alt_r = stop_alt + (3389.5e3 if central == "Mars" else 6378137.0) if extra else None
        t, x, reason, _ = _segment(t, x, central, t_stop, extra, tol, alt_r=alt_r)
        if reason == "time":
            return t, x, central, reason
        if reason == "alt":
            return t, x, central, reason
        if reason == "soi_exit":
            x = to_central(x, central, "Sun", t)
            central = "Sun"
        elif reason == "soi_enter_Mars":
            x = to_central(x, "Sun", "Mars", t)
            central = "Mars"
            if stop_soi == "Mars":
                return t, x, central, "soi"
        elif reason == "soi_enter_Earth":
            x = to_central(x, "Sun", "Earth", t)
            central = "Earth"
            if stop_soi == "Earth":
                return t, x, central, "soi"
        if t >= t_stop - 1e-6:
            return t, x, central, "time"
    return t, x, central, "legs"


def bplane(r, v, mu):
    """B-plane coordinates of a hyperbolic approach: (B.T, B.R, S_hat, vinf). T is in the equator of
    the J2000 frame (T = S x z normalised), R = S x T."""
    r = np.asarray(r, float)
    v = np.asarray(v, float)
    rn = np.linalg.norm(r)
    v2 = float(v @ v)
    vinf = math.sqrt(max(v2 - 2 * mu / rn, 1e-9))
    h = np.cross(r, v)
    e_vec = ((v2 - mu / rn) * r - float(r @ v) * v) / mu
    e = np.linalg.norm(e_vec)
    p_hat = e_vec / e
    q_hat = np.cross(h, p_hat)
    q_hat /= np.linalg.norm(q_hat)
    S = (1.0 / e) * p_hat + math.sqrt(max(0.0, 1 - 1 / (e * e))) * q_hat
    T = np.cross(S, [0.0, 0.0, 1.0])
    T /= np.linalg.norm(T)
    R = np.cross(S, T)
    B = np.cross(S, h) / vinf
    return float(B @ T), float(B @ R), S, vinf


# --------------------------------------------------------------------- capsule entry predictor (Earth)
_CB = None
CAPSULE_CD_MACH = [(0.0, 0.62), (0.6, 0.66), (0.9, 0.75), (1.2, 0.85), (2.0, 0.91), (3.0, 0.95), (5.0, 1.0), (50.0, 1.0)]


def _interp(tab, x):
    if x <= tab[0][0]:
        return tab[0][1]
    for (a, fa), (b, fb) in zip(tab, tab[1:]):
        if x <= b:
            return fa + (fb - fa) * (x - a) / (b - a)
    return tab[-1][1]


class _Cap:
    cdA = 1.0
    scale = 1.0


# The capsule entry as it is flown (manual: "6-DOF entry and parachute", "Earth atmosphere and space weather"):
# - CAP_CD_FACTOR: the capsule's real drag against the modified Newtonian model. The program office's wind_tunnel
#   test of CAP measures it (to 1%); the reference's CAP reads 0.95.
# - MSIS21_OVER_MSISE00: Tudat has NRLMSISE-00 built in, and the simulator flies NRLMSIS 2.1, which is 1.5% to 4.5%
#   thinner over Utah from 40 to 90 km at the return date (measured from the onboard q of the nominal entry, which the
#   manual says is the world's own model at the navigated point; a team gets the same ratio from pymsis). A constant
#   0.97 stands for it.
# - OSCILLATION_DRAG: a bare capsule is dynamically unstable below about Mach 3 (the manual, from MSL's and Genesis's
#   ballistic range tests). It oscillates up to 30 to 40 deg through the transonic range, and below Mach 0.9 it
#   tumbles. Its drag then averages this share of the nose-first drag: the ratio of measured (sensed_accel / q) to
#   modelled drag in the nominal entry of the 2.4 m reference capsule, by Mach number. A team gets the same from its
#   own 6-DOF model of the capsule with those damping data.
CAP_CD_FACTOR = 0.95
MSIS21_OVER_MSISE00 = 0.97
OSCILLATION_DRAG = [(0.0, 0.45), (0.9, 0.45), (0.95, 0.75), (1.0, 0.68), (1.2, 0.66), (1.5, 0.8), (2.0, 0.92),
                    (3.0, 1.0), (50.0, 1.0)]


def entry_bodies():
    """A separate system of bodies with Earth's NRLMSISE-00 atmosphere (as the simulator uses) and a
    ballistic capsule body."""
    global _CB
    if _CB is not None:
        return _CB
    kernels()
    from tudatpy.dynamics import environment_setup as es
    bs = es.get_default_body_settings(["Sun", "Earth", "Moon"], "SSB", "J2000")
    bs.get("Earth").atmosphere_settings = es.atmosphere.nrlmsise00()
    bs.get("Earth").shape_settings = es.shape.oblate_spherical(6378137.0, 1.0 / 298.257223563)
    b = es.create_system_of_bodies(bs)
    b.create_empty_body("CAPP")
    b.get("CAPP").mass = 200.0

    def coeffs(iv):
        mach = float(iv[0])
        k = CAP_CD_FACTOR * MSIS21_OVER_MSISE00 * _interp(OSCILLATION_DRAG, mach)
        return [_Cap.cdA * _interp(CAPSULE_CD_MACH, mach) * _Cap.scale * k, 0.0, 0.0]

    c = es.aerodynamic_coefficients.custom_aerodynamic_force_coefficients(
        coeffs, 1.0, [es.aerodynamic_coefficients.mach_number_dependent],
        force_coefficients_frame=es.aerodynamic_coefficients.negative_aerodynamic_frame_coefficients)
    es.add_aerodynamic_coefficient_interface(b, "CAPP", c)
    es.add_rotation_model(b, "CAPP", es.rotation_model.aerodynamic_angle_based(
        "Earth", "J2000", "CAPP_Fixed", lambda t: np.array([[0.0], [0.0], [0.0]])))
    _CB = b
    return b


_EACC = None


def capsule_entry(t, x, mass, cdA_hyper, stop_alt=1300.0, dt=0.5, t_max=3000.0, density_scale=1.0):
    """Propagate a ballistic capsule from (t, x) Earth-centered J2000 to the ground (altitude stop_alt
    above the WGS84 ellipsoid). Returns (t_land, x_land, info)."""
    global _EACC
    from tudatpy.dynamics import propagation_setup as ps, simulator
    b = entry_bodies()
    b.get("CAPP").mass = float(mass)
    _Cap.cdA = float(cdA_hyper)
    _Cap.scale = float(density_scale)
    if _EACC is None:
        acc = {"Earth": [ps.acceleration.spherical_harmonic_gravity(12, 12), ps.acceleration.aerodynamic()],
               "Sun": [ps.acceleration.point_mass_gravity()], "Moon": [ps.acceleration.point_mass_gravity()]}
        _EACC = ps.create_acceleration_models(b, {"CAPP": acc}, ["CAPP"], ["Earth"])
    term = ps.propagator.hybrid_termination([
        ps.propagator.time_termination(t + t_max),
        ps.propagator.dependent_variable_termination(ps.dependent_variable.altitude("CAPP", "Earth"), stop_alt,
                                                     use_as_lower_limit=True)], fulfill_single_condition=True)
    integ = ps.integrator.runge_kutta_fixed_step(dt, coefficient_set=ps.integrator.CoefficientSets.rk_4)
    dv = [ps.dependent_variable.altitude("CAPP", "Earth"), ps.dependent_variable.mach_number("CAPP", "Earth"),
          ps.dependent_variable.dynamic_pressure("CAPP", "Earth"), ps.dependent_variable.density("CAPP", "Earth"),
          ps.dependent_variable.airspeed("CAPP", "Earth"),
          ps.dependent_variable.single_acceleration_norm(ps.acceleration.aerodynamic_type, "CAPP", "Earth")]
    prop = ps.propagator.translational(["Earth"], _EACC, ["CAPP"], np.array(x, float), t, integ, term, output_variables=dv)
    prop.print_settings.print_initial_and_final_conditions = False
    sim = simulator.create_dynamics_simulator(b, prop)
    res = sim.propagation_results
    sh = res.state_history
    dh = res.dependent_variable_history
    ts = sorted(sh.keys())
    # interpolate to the exact crossing of stop_alt between the last two samples
    t1, t0_ = ts[-1], ts[-2] if len(ts) > 1 else ts[-1]
    a1, a0 = dh[t1][0], dh[t0_][0]
    f = (stop_alt - a0) / (a1 - a0) if a1 != a0 else 1.0
    xl = np.array(sh[t0_]) + f * (np.array(sh[t1]) - np.array(sh[t0_]))
    tl = t0_ + f * (t1 - t0_)
    gmax = max(dh[k][5] for k in ts) / 9.80665
    return tl, xl, {"g_max": gmax, "hist": [(k, dh[k]) for k in ts[::10]], "hist_all": [(k, dh[k]) for k in ts],
                    "states": [(k, sh[k]) for k in ts[::10]],
                    "V_land": float(dh[t1][4])}
