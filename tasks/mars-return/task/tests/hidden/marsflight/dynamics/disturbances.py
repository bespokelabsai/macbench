"""Coast disturbance torques and fins. Both are off by default.

Coast disturbances (MARSFLIGHT_COAST_DISTURBANCES): in a coast held in the deadband, the RCS must also cancel
the torques of gravity gradient, solar radiation pressure and propellant venting. A torque that keeps one sign
over the hold costs its angular impulse over the RCS lever arm and the exhaust speed. A torque that swings
both ways about the target (gravity gradient inside the deadband) averages out and costs nothing extra.

Fins (MARSFLIGHT_FINS): a stage with a "fins" entry in its spec gets the fins' normal-force slope at their
center of pressure, from Barrowman's equations (J. Barrowman, The Practical Calculation of the Aerodynamic
Characteristics of Slender Finned Vehicles, 1967), with his fin and body interference factor.
"""
from __future__ import annotations

import math

import numpy as np

COAST_DISTURBANCES_DEFAULT = True
FINS_DEFAULT = True

P_SUN_1AU = 4.56e-6        # N/m^2: solar radiation pressure on an absorbing surface at 1 AU
AU_M = 1.495978707e11


def _switch(name, default):
    import os
    v = os.environ.get("MARSFLIGHT_" + name, "").strip().lower()
    if v in ("1", "true", "on", "yes"):
        return True
    if v in ("0", "false", "off", "no"):
        return False
    return default


def coast_on():
    return _switch("COAST_DISTURBANCES", COAST_DISTURBANCES_DEFAULT)


def fins_on():
    return _switch("FINS", FINS_DEFAULT)


# ---------------------------------------------------------------------- coast torques
def gravity_gradient(mu, r_body, inertia):
    """Gravity gradient torque (N m, body axes): 3 mu / r^5 (r x I r), with r the position of the vehicle from
    the center of the planet in body axes and inertia a 3x3 matrix (or its diagonal)."""
    r = np.asarray(r_body, float)
    I = np.diag(inertia) if np.ndim(inertia) == 1 else np.asarray(inertia, float)
    n = float(np.linalg.norm(r))
    return 3.0 * mu / n ** 5 * np.cross(r, I @ r)


def gravity_gradient_peak(mu, r, Ix, It):
    """Largest gravity gradient torque on an axisymmetric body, with its long axis 45 deg off the vertical."""
    return 1.5 * mu / r ** 3 * abs(It - Ix)


def solar_pressure(area_m2, arm_m, dist_m=AU_M, reflect=0.3, cos_incidence=1.0):
    """Solar radiation pressure torque (N m): the force on the lit area, (1 + reflectivity) times the pressure,
    at the arm between the center of pressure and the center of mass."""
    return P_SUN_1AU * (AU_M / dist_m) ** 2 * (1.0 + reflect) * area_m2 * cos_incidence * arm_m


def vent_torque(thrust_n, misalign_deg, arm_m, offset_m=0.0):
    """Torque of a propulsive vent (N m): its thrust at an angle off the stack axis, at the arm from the vent to
    the center of mass, plus the thrust times any lateral offset of its line of action."""
    return thrust_n * (math.sin(math.radians(misalign_deg)) * arm_m + math.cos(math.radians(misalign_deg)) * offset_m)


def hold_propellant(torque_nm, duration_s, lever_m, isp_s, g0=9.80665):
    """RCS propellant (kg) to cancel a torque of one sign for duration_s: angular impulse / lever / exhaust speed."""
    return abs(torque_nm) * duration_s / lever_m / (isp_s * g0)


def coast_torque(mu, r_body, Ix, It, vents=(), srp=None, sunlit=1.0):
    """The torques of one sign that the RCS must cancel in a hold, body axes (N m). r_body: the position from the
    planet in body axes. vents: [(thrust, misalignment deg, arm, offset)]. srp: (area, arm, distance from the
    Sun) or None; sunlit is the share of the hold in sunlight.

    The gravity gradient counts at the hold's mean attitude: in a hold the vehicle swings across the deadband,
    so the part due to the swing averages out. Vent and solar torques are taken about the pitch axis (their worst
    case), since the model does not know the vents' clocking."""
    tq = gravity_gradient(mu, r_body, [Ix, It, It])
    tq = np.array(tq, float)
    side = sum(vent_torque(*v) for v in vents)
    if srp is not None:
        side += sunlit * solar_pressure(*srp)
    tq[1] += side
    return tq


# ---------------------------------------------------------------------- fins (Barrowman)
def fin_geometry(f):
    """(count, semispan, root chord, tip chord, sweep of the tip's leading edge behind the root's) from a stage's
    fins spec: {"count", "span_m", "root_chord_m", "tip_chord_m", "sweep_m"}. span_m is one fin's span from the
    body out."""
    return (int(f.get("count", 4)), float(f["span_m"]), float(f["root_chord_m"]), float(f.get("tip_chord_m", 0.0)),
            float(f.get("sweep_m", 0.0)))


def fin_area(f):
    n, s, cr, ct, _ = fin_geometry(f)
    return 0.5 * (cr + ct) * s


def barrowman_cn_alpha(f, body_d, ref_d=None):
    """Normal-force slope (per rad) of a fin set on a body of diameter body_d, on the area of ref_d, with the fin
    and body interference factor 1 + R / (s + R). Subsonic (Barrowman 1967, eq. 3-35 and 3-60)."""
    n, s, cr, ct, xr = fin_geometry(f)
    ref_d = ref_d or body_d
    lf = math.hypot(s, xr + 0.5 * ct - 0.5 * cr)          # length of the mid-chord line
    cn = 4.0 * n * (s / ref_d) ** 2 / (1.0 + math.sqrt(1.0 + (2.0 * lf / (cr + ct)) ** 2))
    R = body_d / 2
    return cn * (1.0 + R / (s + R))


def supersonic_cn_alpha(f, body_d, mach, ref_d=None):
    """Normal-force slope (per rad) of a fin set at a supersonic Mach number: Ackeret's thin plate, 4 / beta on the
    fin area, for the n / 2 fins that lie across the flow, with the same interference factor."""
    n, s, cr, ct, _ = fin_geometry(f)
    ref_d = ref_d or body_d
    beta = math.sqrt(max(mach * mach - 1.0, 1e-6))
    R = body_d / 2
    return (n / 2.0) * 4.0 / beta * fin_area(f) / (math.pi * ref_d ** 2 / 4) * (1.0 + R / (s + R))


def fin_cp_from_root_le(f):
    """Distance of the fins' center of pressure behind the root's leading edge (Barrowman eq. 3-36)."""
    _, _, cr, ct, xr = fin_geometry(f)
    return xr * (cr + 2 * ct) / (3 * (cr + ct)) + (cr + ct - cr * ct / (cr + ct)) / 6.0


def fin_point(f, x_bottom, body_d, S_ref):
    """(x, CN_alpha on S_ref) of a fin set whose root's trailing edge sits at x_bottom (body x, up the stack)."""
    cr = fin_geometry(f)[2]
    x = x_bottom + cr - fin_cp_from_root_le(f)
    cn = barrowman_cn_alpha(f, body_d) * (math.pi * body_d ** 2 / 4) / S_ref
    return x, cn
