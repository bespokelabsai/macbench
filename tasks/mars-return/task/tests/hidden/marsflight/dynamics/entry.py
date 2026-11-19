"""6-DOF entry and capsule stability (ported onto dynamics/).

Switched on by MARSFLIGHT_ENTRY6DOF=1 (default off: the reference needs a larger S2 margin first, which the
reference owner is adding). With it off nothing here runs and flights are unchanged. With it on:

- An entry with a heatshield or capsule leading is a rigid-body arc. Its forces and its moment about the center
  of mass come from modified Newtonian theory on the sphere-cone and its afterbody (`blunt_table`), with the
  forebody's center of pressure and normal force corrected to flight data against Mach (`blunt_continuum`,
  entry_data.BLUNT_CP_SHIFT_*), bridged to free molecular flow by the Knudsen number (`rarefied_weight`), and
  with the pitch damping of MSL's (70 deg) or Genesis's (60 deg) ballistic range tests (entry_data.BLUNT_CMQ*).
- The trim ballast sits off the axis (`place_trim_ballast`, where vehicle.py puts it), so the capsule trims
  at an angle of attack and its lift points to that side. A center of mass off the axis also turns a vehicle
  under thrust, and the autopilot trims the gimbals for it.
- The RCS holds the commanded bank_deg in an entry (`_bank_demand`), and pays propellant for it.
- A deployed parachute is its own body (`canopy_begin`, `canopy_acc`), joined to the top of its carrier by a
  riser that pulls only when taut, so the vehicle swings under it.
- A heatshield's length in the stack layout is the depth of its sphere-cone (`forebody_depth`).

Entry and canopy arcs are flown with a variable-step integrator (`integrator`) and evaluate the controller at
every call (`_entry_control`) instead of in fixed control frames; the RCS torque is continuous there (no
minimum pulse), as.

SixDof (dynamics/rigid_body.py) inherits EntryMixin; the hook points are listed in the PR body.
"""
from __future__ import annotations

import math
import os
from types import SimpleNamespace

import numpy as np

from .. import data as D
from . import entry_data as ED


def entry_on():
    """MARSFLIGHT_ENTRY6DOF=1 switches the 6-DOF entry on (default off)."""
    return os.environ.get("MARSFLIGHT_ENTRY6DOF", "1") != "0"


def _rb():
    from . import rigid_body
    return rigid_body


def _interp(table, x):
    if x <= table[0][0]:
        return table[0][1]
    for (a, fa), (b, fb) in zip(table, table[1:]):
        if x <= b:
            return fa + (fb - fa) * (x - a) / (b - a)
    return table[-1][1]


def _unit(v):
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


# ---------------------------------------------------------------------- constants
NEWTON_CP_MAX = 1.85        # stagnation pressure coefficient, as core.common.newtonian_cd (the 3-DOF drag)
AFT_TOP_RADIUS = 0.7        # an afterbody narrows to 0.7 of the base radius, as vehicle.py sizes a backshell
ENTRY_RATE_DEADBAND = math.radians(2.0)   # rad/s: entry controller damps pitch and yaw rates above this
TRIM_ALPHA_MAX = 40.0       # deg: largest trim angle of attack a ballast offset is placed for
TRIM_MACH = 25.0            # ld_trim is the hypersonic trim: the ballast is placed for it at this Mach number
BANK_RATE_MAX = getattr(D, "BANK_RATE_MAX_DEG_S", 20.0)   # deg/s: MSL's profiled bank slews (Way 2013, NTRS 20130011617)
# Parachute and capsule as two bodies joined by a riser (6-DOF), as in MSL's flight simulation (Cruz et al.,
# "Parachute Models Used in the Mars Science Laboratory Entry, Descent, and Landing Simulation", AIAA 2013,
# NTRS 20130012762). The canopy sits at the skirt, its suspension lines (LINES_PER_D0 canopy diameters) plus
# its riser and bridle (RISER_PER_D entry-body diameters) behind the attach point: MSL's 36.6 m lines on a
# 21.35 m canopy, and 7.55 m riser plus 1.35 m bridle on a 4.5 m aeroshell (Tables 1 and 2, pp. 18 and 19),
# 45.5 m in all, the x/D of about 10 of its wind tunnel tests. The canopy carries an apparent mass
# K_APP rho D0^3 (Eq. 22 and 23, p. 11, nominal value), which gravity does not pull. The riser is a spring
# that pulls only when taut, as the model's triple bridle, stretching RISER_STRETCH at the canopy's rated load
# (an assumption, of the order of the stretch of nylon and Kevlar lines), with RISER_DAMPING of critical
# damping. Its stretch mode is held to RISER_MODE_HZ or below, so the integrator can take steps of a few
# hundredths of a second: this softens the snatch load of a small canopy.
LINES_PER_D0 = 1.713
RISER_PER_D = 8.895 / 4.5
K_APP = 0.035
RISER_STRETCH = 0.05
RISER_DAMPING = 0.2
RISER_MODE_HZ = 2.0
BANK_SIGN = -1.0            # the bank angle's sign, matched to the 3-DOF bank_deg (the entry test checks it)


# ---------------------------------------------------------------------- geometry
def forebody_depth(e):
    """Distance from the nose to the rim of a sphere-cone heatshield."""
    th = math.radians(e.half_angle)
    Rb = e.diameter / 2
    rn = min(e.nose_radius, Rb / math.cos(th))
    return rn * (1 - math.sin(th)) + (Rb - rn * math.cos(th)) / math.tan(th)


# ---------------------------------------------------------------------- blunt-body aerodynamics
def blunt_panels(half_angle, rn, Rb, L_aft, r_top, n_r=40, n_psi=72):
    """Surface panels of a sphere-cone forebody (nose at x = 0, body x aft) and a conical afterbody with a flat
    top: (centers, outward normals, areas, afterbody flags, x of the rim)."""
    th = math.radians(half_angle)
    rn = min(rn, Rb / math.cos(th))
    psi = (np.arange(n_psi) + 0.5) * 2 * math.pi / n_psi
    cs, sn, dpsi = np.cos(psi), np.sin(psi), 2 * math.pi / n_psi
    C, N, A, F = [], [], [], []

    def ring(x, r, nx, nr, dA, aft):
        C.append(np.column_stack([np.full(n_psi, x), r * cs, r * sn]))
        N.append(np.column_stack([np.full(n_psi, nx), nr * cs, nr * sn]))
        A.append(np.full(n_psi, dA))
        F.append(np.full(n_psi, aft))

    et = math.pi / 2 - th                     # spherical cap up to its tangency with the cone
    for k in range(n_r):
        e0, e1 = et * k / n_r, et * (k + 1) / n_r
        e = 0.5 * (e0 + e1)
        ring(rn * (1 - math.cos(e)), rn * math.sin(e), -math.cos(e), math.sin(e), rn * rn * (math.cos(e0) - math.cos(e1)) * dpsi, False)
    r_t, x_t = rn * math.cos(th), rn * (1 - math.sin(th))
    for k in range(n_r):                      # cone to the rim
        r0, r1 = r_t + (Rb - r_t) * k / n_r, r_t + (Rb - r_t) * (k + 1) / n_r
        r = 0.5 * (r0 + r1)
        ring(x_t + (r - r_t) / math.tan(th), r, -math.sin(th), math.cos(th), r * dpsi * (r1 - r0) / math.sin(th), False)
    x_rim = x_t + (Rb - r_t) / math.tan(th)
    if L_aft > 0:                             # afterbody frustum and its flat top
        d = math.atan2(Rb - r_top, L_aft)
        sl = math.hypot(Rb - r_top, L_aft)
        for k in range(n_r):
            s = (k + 0.5) / n_r
            r = Rb + (r_top - Rb) * s
            ring(x_rim + L_aft * s, r, math.sin(d), math.cos(d), r * dpsi * sl / n_r, True)
        for k in range(n_r // 2):
            r0, r1 = r_top * k / (n_r // 2), r_top * (k + 1) / (n_r // 2)
            ring(x_rim + L_aft, 0.5 * (r0 + r1), 1.0, 0.0, 0.5 * (r0 + r1) * dpsi * (r1 - r0), True)
    return np.vstack(C), np.vstack(N), np.concatenate(A), np.concatenate(F), x_rim


_BLUNT_TABLES = {}


def blunt_table(half_angle, rn, D_ref, L_aft):
    """Force and moment of a sphere-cone with an afterbody, against the total angle of attack (0 to 180 deg,
    every 0.5 deg). Continuum: modified Newtonian, pressure Cp_max sin^2 on every panel the flow meets, none on
    the lee side or on afterbody panels in the wake of the heatshield rim. For motion along (-cos a, 0, sin a)
    in body axes: (alpha_deg, Fx, Fz, My, x_rim, extra), with forces per q S and the moment about the nose per
    q S D. extra holds the half angle, the forebody's own Fz (for the center of pressure correction) and the
    free molecular force and moment (`fm`: the incident molecules, `fm_re`: their diffuse re-emission per
    unit of sqrt(2 pi R T_wall) / V). Cached per shape."""
    key = (round(half_angle, 3), round(rn, 4), round(D_ref, 4), round(L_aft, 3))
    if key in _BLUNT_TABLES:
        return _BLUNT_TABLES[key]
    Rb = D_ref / 2
    C, N, A, aft, x_rim = blunt_panels(half_angle, rn, Rb, L_aft, AFT_TOP_RADIUS * Rb)
    S = math.pi * Rb * Rb
    al = np.radians(np.arange(0.0, 180.0 + 1e-9, 0.5))
    rows = []
    for a in al:
        v = np.array([-math.cos(a), 0.0, math.sin(a)])
        c = N @ v
        hit = c > 0
        if v[0] < -1e-9:
            s = (x_rim - C[:, 0]) / v[0]      # upstream along v to the rim plane
            lat = np.hypot(C[:, 1] + s * v[1], C[:, 2] + s * v[2])
            hit = hit & ~(aft & (s > 0) & (lat < Rb))
        w = np.where(hit, c, 0.0) * A
        Fc = -(NEWTON_CP_MAX * w * c)[:, None] * N
        # free molecular, full accommodation: the incident momentum, 2 sin(theta) per q along the airflow
        # (-v), and the diffuse re-emission sqrt(pi) / s sqrt(T_wall / T) sin(theta) along the inward normal
        Fi = -2.0 * w[:, None] * v[None, :]
        Fr = -(math.sqrt(math.pi) * w)[:, None] * N
        row = []
        for F, sel in ((Fc, None), (Fc, ~aft), (Fi, None), (Fr, None)):
            if sel is not None:
                F = F * sel[:, None]
            row += [F[:, 0].sum() / S, F[:, 2].sum() / S, np.cross(C, F)[:, 1].sum() / (S * D_ref)]
        rows.append(row)
    r = np.array(rows)
    extra = {"half_angle": float(half_angle), "fz_fore": r[:, 4], "fm": (r[:, 6], r[:, 7], r[:, 8]),
             "fm_re": (r[:, 9], r[:, 10], r[:, 11])}
    out = (np.degrees(al), r[:, 0], r[:, 1], r[:, 2], x_rim, extra)
    _BLUNT_TABLES[key] = out
    return out


def _by_half_angle(half_angle, t60, t70, mach):
    """A correction table for a sphere-cone of this half angle: 60 and 70 deg data, interpolated between them
    and held outside."""
    f = min(1.0, max(0.0, (half_angle - 60.0) / 10.0))
    return (1 - f) * _interp(t60, mach) + f * _interp(t70, mach)


def blunt_continuum(tab, mach):
    """Continuum force and moment arrays (Fx, Fz, My, as blunt_table) at this Mach number, with the forebody's
    center of pressure moved forward and the normal force scaled to flight data (entry_data.BLUNT_CP_SHIFT_*,
    BLUNT_CN_FACTOR_*)."""
    _, fx, fz, my, _, ex = tab
    ha = ex["half_angle"]
    dd = _by_half_angle(ha, ED.BLUNT_CP_SHIFT_60, ED.BLUNT_CP_SHIFT_70, mach)
    cn = _by_half_angle(ha, ED.BLUNT_CN_FACTOR_60, ED.BLUNT_CN_FACTOR_70, mach)
    return fx, cn * fz, cn * (my + dd * ex["fz_fore"])


def rarefied_weight(kn):
    """Share of the free molecular value in a coefficient at Knudsen number kn (entry_data.RAREFIED_KN bridging)."""
    lo, hi = ED.RAREFIED_KN
    if kn <= lo:
        return 0.0
    if kn >= hi:
        return 1.0
    return math.sin(math.pi * (3.0 / 8.0 + math.log10(kn) / 8.0)) ** 2


def knudsen(central, rho, p, D_ref):
    """Knudsen number (mean free path over D_ref) from the density and pressure of `central`'s atmosphere."""
    g = ED.GAS.get(central)
    if g is None or rho <= 0 or p <= 0:
        return 0.0
    T = p / (rho * g["R"])
    mu = g["mu0"] * (T / g["T0"]) ** 1.5 * (g["T0"] + g["S"]) / (T + g["S"])
    return mu / p * math.sqrt(math.pi * g["R"] * T / 2.0) / D_ref


def blunt_coeffs(tab, vb, mach=30.0, w_fm=0.0, re=0.0):
    """Force (per q S) and moment about the nose (per q S D), body axes, for motion along the unit vector vb
    (body axes): the continuum value at this Mach number (blunt_continuum), bridged by w_fm to the free
    molecular one (re: the re-emission factor sqrt(2 pi R T_wall) / V)."""
    ad = tab[0]
    at = math.degrees(math.acos(max(-1.0, min(1.0, -vb[0]))))
    lat = math.hypot(vb[1], vb[2])
    ey, ez = (vb[1] / lat, vb[2] / lat) if lat > 1e-12 else (0.0, 1.0)
    i = min(int(at / 0.5), len(ad) - 2)
    f = (at - ad[i]) / 0.5
    fx, fz, my = blunt_continuum(tab, mach)
    Fx = fx[i] + f * (fx[i + 1] - fx[i])
    Fz = fz[i] + f * (fz[i + 1] - fz[i])
    My = my[i] + f * (my[i + 1] - my[i])
    if w_fm > 0:
        ex = tab[5]
        (gx, gz, gm), (hx, hz, hm) = ex["fm"], ex["fm_re"]
        Gx = gx[i] + f * (gx[i + 1] - gx[i]) + re * (hx[i] + f * (hx[i + 1] - hx[i]))
        Gz = gz[i] + f * (gz[i + 1] - gz[i]) + re * (hz[i] + f * (hz[i + 1] - hz[i]))
        Gm = gm[i] + f * (gm[i + 1] - gm[i]) + re * (hm[i] + f * (hm[i + 1] - hm[i]))
        Fx, Fz, My = Fx + w_fm * (Gx - Fx), Fz + w_fm * (Gz - Fz), My + w_fm * (Gm - My)
    return (Fx, Fz * ey, Fz * ez), (0.0, My * ez, -My * ey), at


def blunt_center_of_pressure(tab, D_ref, alpha_deg=2.0, mach=None, free_molecular=False):
    """Distance from the nose (m) of the center of pressure at a small angle of attack: a center of mass on
    the axis ahead of it is statically stable, one behind it is not. Newtonian (mach None), corrected at a
    Mach number, or free molecular."""
    i = int(round(alpha_deg / 0.5))
    if free_molecular:
        _, fz, my = tab[5]["fm"]
    elif mach is None:
        fz, my = tab[2], tab[3]
    else:
        _, fz, my = blunt_continuum(tab, mach)
    return -my[i] * D_ref / fz[i]


def _cmq_table(ms, aa, T, mach, alpha_deg):
    m = min(max(mach, ms[0]), ms[-1])
    a = min(max(alpha_deg, aa[0]), aa[-1])
    j = min(max(i for i in range(len(ms)) if ms[i] <= m), len(ms) - 2)
    k = min(max(i for i in range(len(aa)) if aa[i] <= a), len(aa) - 2)
    fm, fa = (m - ms[j]) / (ms[j + 1] - ms[j]), (a - aa[k]) / (aa[k + 1] - aa[k])
    return ((1 - fa) * ((1 - fm) * T[k][j] + fm * T[k][j + 1]) + fa * ((1 - fm) * T[k + 1][j] + fm * T[k + 1][j + 1]))


def blunt_cmq(mach, alpha_deg, half_angle=70.0):
    """Pitch damping Cmq + Cm_alpha_dot of a blunt capsule: BLUNT_CMQ (70 deg), or for a half angle of 65 deg or
    less BLUNT_CMQ_60. The 60 deg data end at 25 deg, where they still drive the motion at low Mach; from 25 to
    30 deg they blend into the 70 deg table's large angle values, which damp at every Mach, so a tumbling
    capsule does not spin up without end."""
    a70 = _cmq_table(ED.BLUNT_CMQ_MACH, ED.BLUNT_CMQ_ALPHA, ED.BLUNT_CMQ, mach,
                     max(alpha_deg, 30.0) if half_angle <= 65.0 else alpha_deg)
    if half_angle > 65.0:
        return a70
    a60 = _cmq_table(ED.BLUNT_CMQ_60_MACH, ED.BLUNT_CMQ_60_ALPHA, ED.BLUNT_CMQ_60, mach, alpha_deg)
    f = min(1.0, max(0.0, (alpha_deg - 25.0) / 5.0))
    return (1 - f) * a60 + f * a70


def blunt_shape(lay, inside_of=None):
    """The blunt body that leads: (lead element, x of its nose, afterbody length to the top of the outer mold
    line), or None if no heatshield or capsule leads."""
    rows = [r for r in lay if r[0].el.kind != "parachute"]
    if not rows or rows[0][0].el.kind not in ("heatshield", "capsule"):
        return None
    e, x0 = rows[0][0].el, rows[0][1]
    tab = blunt_table(e.half_angle, e.nose_radius, e.diameter, 0.0)
    segs = _rb().outer_segments(lay, inside_of)
    x_top = max(x1 for _, x1, _ in segs) if segs else x0 + e.length
    return e, x0, max(0.0, x_top - x0 - tab[4])


def entry_bank(v, up, com_lat):
    """Bank angle of an entry body (rad): the angle about the airspeed from the local vertical to the body's
    lift side, the direction of its lateral center of mass offset (body +z if it has none). All vectors in
    body axes; v is the airspeed direction. Zero is lift up; the sign follows the 3-DOF bank_deg (BANK_SIGN)."""
    v = np.asarray(v, float)
    ref = np.asarray(com_lat, float)
    if float(np.linalg.norm(ref)) < 1e-9:
        ref = np.array([0.0, 0.0, 1.0])
    u = np.asarray(up, float) - float(np.dot(up, v)) * v
    p = ref - float(np.dot(ref, v)) * v
    if np.linalg.norm(u) < 1e-9 or np.linalg.norm(p) < 1e-9:
        return 0.0
    return BANK_SIGN * math.atan2(float(np.dot(np.cross(u, p), v)), float(np.dot(u, p)))


def lateral_com(states, dropped=()):
    """Lateral center of mass (body y, z) of the elements present: a heatshield's trim ballast until it is
    jettisoned and a capsule's trim ballast, both at their ballast_offset_m along body +z (place_trim_ballast),
    and any packaging offset an element carries (com_offset_m, its whole dry mass)."""
    M = sum(s.dry + s.fuel + s.ox + s.rcs for s in states)
    if M <= 0:
        return 0.0, 0.0
    mz = 0.0
    for s in states:
        e = s.el
        r = getattr(e, "ballast_offset_m", 0.0)
        if r:
            if e.kind == "heatshield" and e.id not in dropped:
                mz += e.ballast_kg * r
            elif e.kind == "capsule":
                mz += e.mass_breakdown.get("trim_ballast", 0.0) * r
        mz += s.dry * getattr(e, "com_offset_m", 0.0)
    return 0.0, mz / M


def entry_trim(states, ld, inside_of=None):
    """Where a trim ballast must put the center of mass of an entry body for the trim L/D `ld` (modified
    Newtonian, blunt_table): {alpha_deg, ld, z_cg_m, x_cg_from_nose_m, mass_kg, stable}. alpha_deg is the
    smallest angle of attack with that L/D, at most TRIM_ALPHA_MAX; z_cg_m is the lateral center of mass
    (along the lift) that makes the pitching moment zero there; stable says whether the moment then turns the
    capsule back to that angle (center of mass ahead of the center of pressure)."""
    RB = _rb()
    lay = RB.layout(states, inside_of)
    M, xc, _, _ = RB.mass_properties(RB.mass_items(lay))
    shp = blunt_shape(lay, inside_of)
    if shp is None or M <= 0:
        return None
    e, x_nose, L_aft = shp
    tab = blunt_table(e.half_angle, e.nose_radius, e.diameter, L_aft)
    ad = tab[0]
    fx, fz, my = blunt_continuum(tab, TRIM_MACH)
    Dm = e.diameter
    best = None
    for i, a in enumerate(ad):
        if a <= 0 or a > TRIM_ALPHA_MAX:
            continue
        al = math.radians(a)
        drag = fx[i] * math.cos(al) - fz[i] * math.sin(al)
        lift = fx[i] * math.sin(al) + fz[i] * math.cos(al)
        if drag > 0 and lift / drag >= ld - 1e-9:
            best = i
            break
    if best is None:
        best = int(round(TRIM_ALPHA_MAX / 0.5))

    def z_for(i):
        return (my[i] * Dm - (x_nose - xc) * fz[i]) / fx[i]
    z = z_for(best)
    # static stability at that trim: the moment about the trimmed center of mass a little above the trim angle
    j = min(best + 2, len(ad) - 1)
    m_up = my[j] * Dm - z * fx[j] - (x_nose - xc) * fz[j]
    al = math.radians(ad[best])
    drag = fx[best] * math.cos(al) - fz[best] * math.sin(al)
    lift = fx[best] * math.sin(al) + fz[best] * math.cos(al)
    return {"alpha_deg": float(ad[best]), "ld": float(lift / drag), "z_cg_m": float(z), "x_cg_from_nose_m": float(xc - x_nose),
            "mass_kg": float(M), "stable": bool(m_up > 0)}


def place_trim_ballast(elements):
    """Where the trim ballast sits in 6-DOF (vehicle.build_vehicle did this; here it runs when a 6-DOF
    flight with MARSFLIGHT_ENTRY6DOF=1 starts, once per vehicle). Its mass is set by vehicle.py
    (BALLAST_FRACTION_PER_LD); 6-DOF also needs its place. It goes at the distance from the axis that moves the
    entry body's center of mass to where the modified Newtonian moments trim at ld_trim (entry_trim), and no
    farther out than 0.45 diameters. The entry body is a heatshield with its backshell and what they enclose,
    with the propellant loaded at launch, or a capsule alone. Sets e.ballast_offset_m and e.entry_trim."""
    if any(getattr(e, "_trim_placed", False) for e in elements):
        return
    inside = _rb().inside_map(elements)

    def st(x):
        return SimpleNamespace(el=x, dry=x.dry_mass, fuel=x.loaded.get("fuel", 0.0), ox=x.loaded.get("ox", 0.0), rcs=x.rcs_prop)

    for i, e in enumerate(elements):
        e._trim_placed = True
        if e.kind == "heatshield" and e.spec.get("_backshell"):
            j = next((k for k in range(i + 1, len(elements)) if elements[k].id == e.spec["_backshell"]), None)
            if j is None:
                continue
            body = elements[i:j + 1] + [x for x in elements[j + 1:] if x.kind == "parachute" and x.carrier == elements[j].id]
            m_ball = e.ballast_kg
        elif e.kind == "capsule":
            body = [e]
            m_ball = e.mass_breakdown.get("trim_ballast", 0.0)
        else:
            continue
        if e.ld_trim <= 0 or m_ball <= 0:
            continue
        try:
            states = [st(x) for x in body]
            tr = entry_trim(states, e.ld_trim, inside)
        except Exception:  # noqa: BLE001
            continue
        if tr is None:
            continue
        e.ballast_offset_m = max(0.0, min(0.45 * e.diameter, tr["z_cg_m"] * tr["mass_kg"] / m_ball))
        if e.ballast_offset_m < tr["z_cg_m"] * tr["mass_kg"] / m_ball - 1e-9:
            tr = dict(tr, ballast_at_rim=True)
        tr["ballast_offset_m"] = e.ballast_offset_m
        tr["z_cg_placed_m"] = lateral_com(states)[1]
        e.entry_trim = tr


def trim_report(e):
    """The 6-DOF trim row for `marsflight check` (vehicle.Vehicle.report), or None."""
    tr = getattr(e, "entry_trim", None)
    if not tr:
        return None
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in tr.items()}


# ---------------------------------------------------------------------- the SixDof part
class EntryMixin:
    """Methods SixDof inherits for 6-DOF entries, parachute canopies and lateral center of mass offsets."""

    def _entry_init(self):
        self.entry6 = entry_on()
        self.aoa_hold = None       # entry: total angle of attack the RCS holds (rad), or None for rate damping
        self._flow_t = []
        self.canopy = None
        self.riser = {"tension_N": 0.0, "angle_deg": 0.0, "tension_max_N": 0.0}
        self.riser_log = []        # (t, tension N, swing deg) every 0.05 s under a canopy, for validation
        if self.entry6:
            place_trim_ballast(self.m.vehicle.elements)

    def _entry_bodies(self, b, cb):
        if self.entry6:
            b.create_empty_body("CP6" + cb)    # a parachute canopy (canopy_begin)
            b.get("CP6" + cb).mass = 10.0

    def _entry_command(self, att):
        if self.entry6 and "aoa_deg" in att:
            # entry only: hold this total angle of attack with the RCS (None: back to rate damping)
            try:
                self.aoa_hold = None if att["aoa_deg"] is None else math.radians(min(90.0, max(0.0, float(att["aoa_deg"]))))
            except (TypeError, ValueError):
                self.m.cmd_errors.append(f"attitude.aoa_deg {att['aoa_deg']!r} is not a number")

    def integrator(self, span):
        """Integrator for an entry arc or an unpowered arc under a canopy: variable step, since a small capsule's
        pitch oscillation is fast (several hertz near peak dynamic pressure) while a large one's is slow, and a
        taut riser stretches and relaxes in a fraction of a second. At most 0.25 s, so the checks keep their
        samples. None for other arcs (the core's fixed step)."""
        if self.arc is None or not self.arc.get("var"):
            return None
        from tudatpy.dynamics import propagation_setup as ps
        ctrl = ps.integrator.step_size_control_elementwise_scalar_tolerance(1e-9, 1e-9)
        val = ps.integrator.step_size_validation(1e-5, 0.25)   # the snatch of a 60 m canopy needs steps just under 1e-4 s
        return ps.integrator.runge_kutta_variable_step(min(span, 0.05), ps.integrator.CoefficientSets.rkf_78,
                                                       step_size_control_settings=ctrl, step_size_validation_settings=val)

    def _entry_begin(self, lay, xc0, chute_x, in_atm, aero, entry, groups):
        """Add the entry and canopy parts to self.arc (called at the end of begin_arc)."""
        a, m = self.arc, self.m
        a.update(entry=None, canopy=False, k_rot=6, var=False, lat=(0.0, 0.0))
        self._flow_t = []
        if not self.entry6:
            return
        a["lat"] = lateral_com(m.present(), m.ballast_dropped)
        canopy = self.canopy_begin(lay, xc0, chute_x) if (chute_x is not None and not groups) else self.canopy_begin(None, 0.0, None)
        a.update(canopy=canopy, chutes=a["chutes"] and not canopy, chute_x=None if canopy else chute_x, k_rot=12 if canopy else 6)
        shp = blunt_shape(lay, self.inside_of) if (entry and aero is not None and aero.get("kind") == "blunt") else None
        if shp is not None:
            e, x_nose, L_aft = shp
            tr = (m.truth.get("aero") or {}).get(e.id) or {}
            yc, zc = a["lat"]
            a["entry"] = {"lead": e, "tab": blunt_table(e.half_angle, e.nose_radius, e.diameter, L_aft),
                          "x_nose": x_nose, "yc": yc, "zc": zc, "D": e.diameter,
                          "kF": m.disp.aero_cd_factor * tr.get("cd", 1.0), "kN": m.disp.aero_ld_factor * tr.get("ld", 1.0),
                          "central": m.bodies.get(m.central)}
        a["var"] = (a["entry"] is not None or canopy) and not groups

    # ------------------------------------------------------------------ parachute canopy and riser
    def canopy_begin(self, lay, xc, x_att):
        """The parachute canopy of this arc, as its own body: set up (or kept from the last arc) when a deployed
        parachute rides on the vehicle in the atmosphere, else None. x_att: the riser's attach point along the
        body axis. Returns True if the arc has a canopy. A canopy becomes a body when its lines have stretched
        (the parachutes model's _chute_area): until then it is packed on the vehicle and has no drag."""
        m = self.m
        chutes = [s for s in m.present() if s.el.kind == "parachute" and s.el.id in m.chute_deployed
                  and m.t >= (m.chute_open.get(s.el.id) or {}).get("t_ls", -math.inf) - 1e-9]
        if lay is None or not chutes or m.central not in ED.GAS:
            self.canopy = None
            return False
        key = tuple(sorted(s.el.id for s in chutes))
        D_body = max(s.el.diameter for s, _, _ in lay if s.el.kind != "parachute")
        L0 = LINES_PER_D0 * max(s.el.chute_diameter for s in chutes) + RISER_PER_D * D_body
        cp = self.canopy
        if cp is None or cp["key"] != key or cp["central"] != m.central:
            # a newly opened canopy: at the end of its lines, straight behind the attach point, at the
            # vehicle's speed (the riser then snatches it)
            R = _rb().q_to_R(self.q)
            y, z = lateral_com(m.present(), m.ballast_dropped)
            p_att = R @ np.array([x_att - xc, -y, -z])
            air = np.asarray(m.x[3:], float) - m.omega_cross(m.x[:3])
            rel = np.concatenate([p_att - _unit(air) * L0, np.zeros(3)])
            cp = {"key": key, "central": m.central, "rel": rel}
        T_rated = max(s.el.chute_design_q for s in chutes) * m._chute_area(0.5)
        mc, Rc = sum(s.dry for s in chutes), max(s.el.chute_diameter for s in chutes) / 2
        rho = float(m.last.get("rho") or 0.0)
        m_c = mc + K_APP * rho * (2 * Rc) ** 3
        M = sum(s.dry + s.fuel + s.ox + s.rcs for s in m.present())
        k_max = m_c * M / (m_c + M) * (2 * math.pi * RISER_MODE_HZ) ** 2
        cp.update({"name": "CP6" + m.central, "L0": L0, "x_att": x_att, "m": mc, "R": Rc,
                   "k": min(T_rated / (RISER_STRETCH * L0), k_max)})
        self.canopy = cp
        return True

    def canopy_state(self):
        """(body name, initial state relative to the central body) of this arc's canopy, or None."""
        if not (self.arc and self.arc.get("canopy")):
            return None
        return self.canopy["name"], np.asarray(self.m.x, float) + self.canopy["rel"]

    def _riser(self):
        """The riser now: (force on the vehicle, inertial; the same in body axes; attach point from the center of
        mass, body axes; tension N; riser direction, inertial)."""
        cp, a = self.canopy, self.arc
        b, c = a["bobj"], self.m.bodies.get(cp["name"])
        R = np.asarray(b.body_fixed_to_inertial_frame)
        w = np.ravel(b.body_fixed_angular_velocity)
        xc = self._props(float(b.mass))[0]
        r_b = np.array([cp["x_att"] - xc, -a["lat"][0], -a["lat"][1]])
        d = np.asarray(c.position) - (np.asarray(b.position) + R @ r_b)
        L = float(np.linalg.norm(d))
        u = d / L if L > 0 else np.zeros(3)
        T = 0.0
        if L > cp["L0"]:
            v_rel = np.asarray(c.velocity) - (np.asarray(b.velocity) + R @ np.cross(w, r_b))
            m_eff = cp["m"] + cp.get("m_air", 0.0)
            M = float(b.mass)
            cdamp = 2 * RISER_DAMPING * math.sqrt(cp["k"] * m_eff * M / (m_eff + M))
            T = max(0.0, cp["k"] * (L - cp["L0"]) + cdamp * float(np.dot(v_rel, u)))
        F = T * u
        return F, R.T @ F, r_b, T, u

    def canopy_acc(self, t):
        """Acceleration of the canopy (inertial) besides the central body's gravity: its drag along its own
        airspeed (with the world's wind) and the riser's pull, over its mass plus its apparent mass, less the
        gravity that apparent mass does not feel."""
        cp, m = self.canopy, self.m
        if cp is None or self.arc is None:
            return np.zeros(3)
        c, cen = m.bodies.get(cp["name"]), m.bodies.get(m.central)
        fc = self.arc["bobj"].flight_conditions
        rho, mach = float(fc.density), float(fc.mach_number)
        r = np.asarray(c.position) - np.asarray(cen.position)
        air = np.asarray(c.velocity) - np.asarray(cen.velocity) - m.omega_cross(r) - self.wind(r, float(t))
        Va = float(np.linalg.norm(air))
        F_drag = -0.5 * rho * Va * m._chute_area(mach, float(t)) * air   # opening as in 3-DOF (_chute_area)
        m_air = K_APP * rho * (2 * cp["R"]) ** 3
        cp["m_air"] = m_air
        F_r, _, _, T, u = self._riser()
        g = -m.gm(m.central) * r / float(np.linalg.norm(r)) ** 3
        acc = (F_drag - F_r - m_air * g) / (cp["m"] + m_air)
        # the riser's angle from straight behind the vehicle (the pendulum swing)
        b = self.arc["bobj"]
        rb = np.asarray(b.position) - np.asarray(cen.position)
        air_b = _unit(np.asarray(b.velocity) - np.asarray(cen.velocity) - m.omega_cross(rb) - self.wind(rb, float(t)))
        ang = math.degrees(math.acos(max(-1.0, min(1.0, -float(np.dot(air_b, u))))))
        self.riser.update({"tension_N": T, "angle_deg": ang, "tension_max_N": max(self.riser["tension_max_N"], T)})
        t = float(t)
        if t == t and (not self.riser_log or t >= self.riser_log[-1][0] + 0.05):
            self.riser_log.append((t, T, ang))
        return acc

    def _rarefied(self, ent):
        """(share of the free molecular value, re-emission factor) of a blunt entry body now."""
        fc = self.arc["bobj"].flight_conditions
        rho, p, V = float(fc.density), float(fc.pressure), float(fc.airspeed)
        w = rarefied_weight(knudsen(self.m.central, rho, p, ent["D"]))
        g = ED.GAS.get(self.m.central)
        re = math.sqrt(2 * math.pi * g["R"] * ED.RAREFIED_WALL_T) / V if (w > 0 and g and V > 1.0) else 0.0
        return w, re

    # ------------------------------------------------------------------ aerodynamic coefficients
    def _entry_coeffs(self, iv):
        """_coeffs for an entry or canopy arc: force coefficients times area, body axes (Tudat's negative
        body-fixed frame). Under a canopy the riser's pull is added here too, so the checks that read the
        aerodynamic acceleration see it."""
        m, arc = self.m, self.arc
        a = m.ctl["aero"]
        if a is None:
            c = [0.0, 0.0, 0.0]
        else:
            mach, al, be, t = float(iv[0]), float(iv[1]), float(iv[2]), float(iv[3])
            if arc["entry"] is not None:
                # Blunt body: modified Newtonian forces at the angle the airspeed makes with the axis, scaled by
                # the capsule drag ratio against Mach as in 3-DOF; plus any deployed parachute along the airspeed.
                en = arc["entry"]
                vb = (math.cos(al) * math.cos(be), math.sin(be), math.sin(al) * math.cos(be))
                F, _, _ = blunt_coeffs(en["tab"], vb, mach, *self._rarefied(en))
                k = en["kF"] * _interp(D.CAPSULE_CD_MACH, mach) * a["S"]
                kn = k * en["kN"]
                ch = m._chute_area(mach, t) if arc["chutes"] else 0.0
                c = [-k * F[0] + ch * vb[0], -kn * F[1] + ch * vb[1], -kn * F[2] + ch * vb[2]]
            elif a["kind"] == "slender" and arc["slender"]:
                ca = _interp(D.SLENDER_CA_MACH, mach) * arc["ca_k"] + (m._chute_area(mach, t) if arc["chutes"] else 0.0)
                cn = arc["cn"][0] * a["S"]
                c = [ca, cn * math.sin(be), cn * math.sin(al) * math.cos(be)]
            else:
                drag = m._coeffs_thrust([mach, 0.0, 0.0, t])[0] - (m._chute_area(mach, t) if arc["canopy"] else 0.0)
                cb = math.cos(be)
                c = [drag * math.cos(al) * cb, drag * math.sin(be), drag * math.sin(al) * cb]
        if arc.get("canopy"):
            qd = float(arc["bobj"].flight_conditions.dynamic_pressure)
            if qd > 0:
                Fb = self._riser()[1]
                c = [c[0] - Fb[0] / qd, c[1] - Fb[1] / qd, c[2] - Fb[2] / qd]
        return c

    # ------------------------------------------------------------------ the controller in an entry
    def _bank_demand(self, ent, v, up, w, Ix, It):
        """Torque demand of the entry controller (body axes): roll about the airspeed to the commanded bank, no
        faster than BANK_RATE_MAX nor than the RCS can stop within the angle left, and damp pitch and yaw rates
        above ENTRY_RATE_DEADBAND, or, if the flight software asked for it (aoa_hold), hold the angle of attack.
        v: airspeed direction, up: local vertical, w: gyro rates, all body axes."""
        RB = _rb()
        bank = entry_bank(v, up, (0.0, ent["yc"], ent["zc"]))
        a = self.arc
        err = (self.m.ctl["bank"] - bank + math.pi) % (2 * math.pi) - math.pi
        acc = a["rcs"][1] / Ix if a["rcs"] else 0.0
        lim = min(math.radians(BANK_RATE_MAX), 0.8 * math.sqrt(2.0 * acc * abs(err)))
        rate = max(-lim, min(lim, self.bw / (2 * RB.DAMPING) * err))
        w_v = float(np.dot(w, v))
        w_p = w - w_v * v
        n = float(np.linalg.norm(w_p))
        if self.aoa_hold is not None:
            # hold the airspeed at aoa_hold from the heatshield's axis (-x), on the lift side (the lateral center
            # of mass): turn about v_goal x v, no faster than the slew rate
            lat = np.array([0.0, ent["yc"], ent["zc"]])
            lat = lat / np.linalg.norm(lat) if np.linalg.norm(lat) > 1e-9 else np.array([0.0, 0.0, 1.0])
            v_goal = np.array([-math.cos(self.aoa_hold), 0.0, 0.0]) + math.sin(self.aoa_hold) * lat
            w_aoa = self.bw / (2 * RB.DAMPING) * np.cross(v_goal, v)
            w_aoa -= float(np.dot(w_aoa, v)) * v
            na = float(np.linalg.norm(w_aoa))
            lim_p = math.radians(BANK_RATE_MAX)
            w_goal = BANK_SIGN * rate * v + (w_aoa * lim_p / na if na > lim_p else w_aoa)
        else:
            w_goal = BANK_SIGN * rate * v + (w_p * ENTRY_RATE_DEADBAND / n if n > ENTRY_RATE_DEADBAND else w_p)
        kr = 2 * RB.DAMPING * self.bw
        # plus the gyroscopic torque w x (I w), so a fast roll does not couple into pitch and yaw
        return [Ix * kr * (w_goal[0] - w[0]), It * kr * (w_goal[1] - w[1]) + (Ix - It) * w[0] * w[2],
                It * kr * (w_goal[2] - w[2]) + (It - Ix) * w[0] * w[1]]

    def _entry_control(self, t, body, R, w):
        """_control for an entry or unpowered canopy arc (self.arc["var"]): no engines are lit, the RCS takes the
        whole torque demand, continuously, and the torques are the blunt-body moment with pitch damping, the
        pull of a riser or of a parachute on the vehicle, and the slender-body moment if a slender body leads.
        Evaluated at every call (variable step), with the answer kept until the state changes."""
        RB = _rb()
        a = self.arc
        _, vy, vz = np.ravel(w).tolist()   # true rates, for the aerodynamic damping
        Re = self.sensors.estimate(R, t)
        we = self.sensors.rates(w)
        key = (t, *we.tolist(), Re[0, 0], Re[1, 0], Re[2, 0], Re[0, 1])
        if key == self._key:
            return self._out
        (r00, r01, r02), (r10, r11, r12), (r20, r21, r22) = Re.tolist()
        wx, wy, wz = we.tolist()
        xc, Ix, It = self._props(float(body.mass))
        fc = body.flight_conditions
        ent = a["entry"]
        ang_c = fc.aerodynamic_angle_calculator
        al_, be_ = ang_c.get_angle(a["AA"][0]), ang_c.get_angle(a["AA"][1])
        vb = (math.cos(al_) * math.cos(be_), math.sin(be_), math.sin(al_) * math.cos(be_))
        if self.mode == "torque":
            td = [float(v) for v in self.torque_cmd]
        elif ent is not None and self.mode != "rate":
            # Entry: hold the commanded bank (a direction command does not apply), as the 3-DOF entry does.
            up = np.ravel(body.position - ent["central"].position)
            td = self._bank_demand(ent, np.array(vb), Re.T @ _unit(up), np.array([wx, wy, wz]), Ix, It)
        else:
            wcx = wcy = wcz = 0.0
            if self.mode == "rate":
                wcx, wcy, wcz = (float(v) for v in self.rate_cmd)
            elif self.target is not None:
                tx, ty_, tz_ = self.target
                cx, cy, cz = r10 * tz_ - r20 * ty_, r20 * tx - r00 * tz_, r00 * ty_ - r10 * tx
                s = math.sqrt(cx * cx + cy * cy + cz * cz)
                co = r00 * tx + r10 * ty_ + r20 * tz_
                ang = math.atan2(s, co)
                if s > 1e-9:
                    aby, abz = (r01 * cx + r11 * cy + r21 * cz) / s, (r02 * cx + r12 * cy + r22 * cz) / s
                else:
                    aby, abz = (1.0, 0.0) if co < 0 else (0.0, 0.0)
                k = self.bw / (2 * RB.DAMPING) * ang
                wcy, wcz = k * aby, k * abz
                acc = (a["rcs"][2] if a["rcs"] else 0.0) / It
                lim = min(a["slew"], 0.8 * math.sqrt(2.0 * acc * ang))
                n = math.hypot(wcy, wcz)
                if n > lim:
                    wcy, wcz = wcy * lim / n, wcz * lim / n
            kr = 2 * RB.DAMPING * self.bw
            td = [Ix * kr * (wcx - wx), It * kr * (wcy - wy) + (Ix - It) * wx * wz, It * kr * (wcz - wz) + (It - Ix) * wx * wy]
        # RCS takes the demand, within its torque
        trx = try_ = trz = flow = 0.0
        if a["rcs"]:
            Fr, cr, cp = a["rcs"]
            trx = min(cr, max(-cr, td[0]))
            try_ = min(cp, max(-cp, td[1]))
            trz = min(cp, max(-cp, td[2]))
            flow = (abs(trx) / cr + (abs(try_) + abs(trz)) / cp) * RB.RCS_SHARE * Fr / (D.RCS_ISP * D.G0)
        tax = tay = taz = 0.0
        qd, V = float(fc.dynamic_pressure), float(fc.airspeed)
        if (a["slender"] or a["chute_x"] is not None) and qd > 0 and V > 1.0:
            if a["slender"]:
                S0, S1, S2 = a["cn"]
                K1, K2 = S1 - xc * S0, S2 - 2 * xc * S1 + xc * xc * S0
                qs = qd * a["S"]
                tay += qs * (K1 * math.sin(al_) * math.cos(be_) - K2 * vy / V)
                taz += qs * (-K1 * math.sin(be_) - K2 * vz / V)
            if a["chute_x"] is not None:
                qa = qd * self.m._chute_area(float(fc.mach_number), t) * (a["chute_x"] - xc)
                tay += qa * math.sin(al_) * math.cos(be_)
                taz -= qa * math.sin(be_)
        if a.get("canopy"):
            _, Fb, r_b, _, _ = self._riser()
            tq_r = np.cross(r_b, Fb)
            tax, tay, taz = tax + float(tq_r[0]), tay + float(tq_r[1]), taz + float(tq_r[2])
        if ent is not None and qd > 0 and V > 1.0:
            # Blunt-body moment about the center of mass (the table's moment about the nose, plus the force at
            # the nose), and pitch damping Cmq q S d^2 / (2 V) on the true pitch and yaw rates.
            mach = float(fc.mach_number)
            F, Mn, at = blunt_coeffs(ent["tab"], vb, mach, *self._rarefied(ent))
            k = ent["kF"] * _interp(D.CAPSULE_CD_MACH, mach) * qd * a["S"]
            kn = k * ent["kN"]
            fx_, fy_, fz_ = k * F[0], kn * F[1], kn * F[2]
            rx_, ry_, rz_ = ent["x_nose"] - xc, -ent["yc"], -ent["zc"]
            Dm = ent["D"]
            tax += ry_ * fz_ - rz_ * fy_
            tay += rz_ * fx_ - rx_ * fz_ + kn * Dm * Mn[1]
            taz += rx_ * fy_ - ry_ * fx_ + kn * Dm * Mn[2]
            kd = qd * a["S"] * Dm * Dm / (2.0 * V) * blunt_cmq(mach, at, ent["lead"].half_angle)
            tay += kd * vy
            taz += kd * vz
        tq = np.array([[trx + tax], [try_ + tay], [trz + taz]])
        rcs_F = self.m.ctl["rcs"]
        mdot = rcs_F / (D.RCS_ISP * D.G0) if rcs_F else 0.0
        self._key, self._out = key, (np.array([[1.0], [0.0], [0.0]]), tq, rcs_F, mdot)
        self._flows.append(flow)
        self._flow_t.append(t)
        self._use.append(0.0)
        return self._out

    # ------------------------------------------------------------------ after an arc, observations, saved states
    def _entry_rcs_kg(self, span):
        """RCS propellant of the attitude control in this arc: time-weighted over a variable-step arc."""
        if self.arc.get("var") and len(self._flow_t) == len(self._flows) > 1:
            tf = sorted(zip(self._flow_t, self._flows))
            return sum(0.5 * (f0 + f1) * (t1 - t0) for (t0, f0), (t1, f1) in zip(tf, tf[1:]))
        return float(np.mean(self._flows)) * span

    def _entry_end(self, x):
        """The attitude state's place in the arc's state vector, and the canopy's place relative to the vehicle."""
        k = self.arc.get("k_rot", 6)
        if self.arc.get("canopy"):
            self.canopy["rel"] = x[6:12] - x[0:6]
        return k

    def _entry_observe(self, o, R, r, air):
        """A blunt body leads: its angle of attack (heatshield axis to airspeed) and bank, as estimated."""
        m = self.m
        if not self.entry6:
            return
        pres = [s for s in m.present() if s.el.kind != "parachute"]
        if self.q is not None and pres and pres[0].el.kind in ("heatshield", "capsule") and np.linalg.norm(air) > 1.0:
            vb = R.T @ _unit(air)
            o["angle_of_attack_deg"] = math.degrees(math.acos(max(-1.0, min(1.0, -float(vb[0])))))
            yc, zc = lateral_com(m.present(), m.ballast_dropped)
            o["bank_deg"] = math.degrees(entry_bank(vb, R.T @ _unit(r), (0.0, yc, zc)))

    def _entry_tele(self):
        if not self.canopy:
            return {}
        return {"riser_tension_N": round(self.riser["tension_N"], 1), "riser_angle_deg": round(self.riser["angle_deg"], 2),
                "riser_tension_max_N": round(self.riser["tension_max_N"], 1)}

    def _entry_save(self, sc):
        if self.canopy is not None:
            sc["canopy"] = {"key": list(self.canopy["key"]), "central": self.canopy["central"],
                            "rel": [float(v) for v in self.canopy["rel"]]}

    def _entry_load(self, sc):
        if sc.get("canopy"):
            c = sc["canopy"]
            self.canopy = {"key": tuple(c["key"]), "central": c["central"], "rel": np.array(c["rel"], float)}
