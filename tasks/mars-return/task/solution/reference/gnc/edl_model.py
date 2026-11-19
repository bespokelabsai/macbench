"""Point-mass entry dynamics over a rotating Mars, mirroring the simulator's entry model: Newtonian
sphere-cone drag with the Mach table, trimmed lift banked about the air-relative velocity, parachute
drag, J2 gravity and the nominal atmosphere times a density factor. Plain-float RK4 for speed."""
from __future__ import annotations

import math

import numpy as np

from . import marsatm as A

MU = 4.282837581575610e13
J2 = 8.750220924537e-4 * math.sqrt(5.0)
RJ = 3396000.0
R_REF = 3389.5e3
OMEGA = 7.088218e-5
CAPSULE_CD_MACH = [(0.0, 0.62), (0.6, 0.66), (0.9, 0.75), (1.2, 0.85), (2.0, 0.91), (3.0, 0.95), (5.0, 1.0), (50.0, 1.0)]
# trim L/D against Mach, as a ratio to the hypersonic design value (the simulator's data.CAPSULE_LD_MACH)
CAPSULE_LD_MACH = [(0.0, 1.40), (2.0, 1.40), (2.5, 1.325), (3.0, 1.317), (4.0, 1.263), (5.0, 1.218), (10.0, 1.107),
                   (15.0, 1.058), (20.0, 1.012), (24.0, 1.0), (50.0, 1.0)]
DGB_CD = [(0.0, 0.55), (0.8, 0.60), (1.5, 0.52), (2.2, 0.48), (5.0, 0.40)]
BANK_SIGN = -1.0     # calibrated: in the simulator a positive bank tilts lift to the left of the velocity (toward r x v)


def interp(tab, x):
    if x <= tab[0][0]:
        return tab[0][1]
    for (a, fa), (b, fb) in zip(tab, tab[1:]):
        if x <= b:
            return fa + (fb - fa) * (x - a) / (b - a)
    return tab[-1][1]


def newtonian_cd(half_angle_deg, rn, Rb):
    th = math.radians(half_angle_deg)
    s2, c2 = math.sin(th) ** 2, math.cos(th) ** 2
    k = min(1.0, (rn / Rb)) ** 2
    return 1.85 * ((1 - s2 * s2) * k / 2 + s2 * (1 - k * c2))


class Vehicle:
    def __init__(self, m, S, cd_h, ld, chute_area=0.0, chute_tab=None, cd_scale=1.0, ld_scale=1.0, ld_drag=None):
        self.m, self.S, self.cd_h, self.ld = m, S, cd_h, ld
        self.ld_drag = ld if ld_drag is None else ld_drag   # the trimmed-drag penalty uses the design trim
        self.chute_area = chute_area          # pi (D/2)^2 of a deployed chute (0 if none)
        self.chute_tab = chute_tab or DGB_CD
        self.cd_scale, self.ld_scale = cd_scale, ld_scale
        self.wind = None                      # inertial wind vector at full strength (m/s), or None
        self.site_alt = -2550.0


# The manual's Mars wind profile near Jezero ("Mars atmosphere, dust and winds"): one direction, full speed from
# about 1.4 to 2 km above the ground up to 40 km, 30% of it at the ground, none above. The flight software
# measures the wind itself from its air data (EDL.update_wind) and assumes this shape.
WIND_TOP_AGL = 40e3
WIND_FULL_AGL = 1400.0


def wind_factor(h, site_alt):
    agl = h - site_alt
    if agl >= WIND_TOP_AGL:
        return 0.0
    return min(1.0, 0.3 + 0.7 * max(agl, 0.0) / WIND_FULL_AGL)


def deriv(x, y, z, vx, vy, vz, veh, bank, k_rho, w, pz):
    wx, wy, wz = w
    r2 = x * x + y * y + z * z
    r = math.sqrt(r2)
    # gravity with J2 about the pole pz
    px, py, pzz = pz
    zz = x * px + y * py + z * pzz
    ir3 = 1.0 / (r2 * r)
    k = 1.5 * J2 * MU * RJ * RJ / (r2 * r2 * r)
    s = 5.0 * zz * zz / r2
    ax = -MU * x * ir3 - k * ((1 - s) * x + 2 * zz * px)
    ay = -MU * y * ir3 - k * ((1 - s) * y + 2 * zz * py)
    az = -MU * z * ir3 - k * ((1 - s) * z + 2 * zz * pzz)
    h = r - R_REF
    if h < 140e3:
        rho = A.density(h) * k_rho
        # air-relative velocity v - w x r - wind
        ux = vx - (wy * z - wz * y)
        uy = vy - (wz * x - wx * z)
        uz = vz - (wx * y - wy * x)
        if veh.wind is not None:
            f = wind_factor(h, veh.site_alt)
            ux, uy, uz = ux - f * veh.wind[0], uy - f * veh.wind[1], uz - f * veh.wind[2]
        V = math.sqrt(ux * ux + uy * uy + uz * uz)
        if V > 1e-3 and rho > 0:
            mach = V / A.sound_speed(h)
            # trimmed at an angle of attack the drag is below the zero-angle value: x (1 - 0.5 L/D)
            cd = veh.cd_h * (1.0 - 0.5 * (veh.ld_drag if veh.ld else 0.0)) * interp(CAPSULE_CD_MACH, mach) * veh.cd_scale
            q = 0.5 * rho * V * V
            dA = cd * veh.S + (interp(veh.chute_tab, mach) * veh.chute_area if veh.chute_area else 0.0)
            D = q * dA / veh.m
            ax -= D * ux / V
            ay -= D * uy / V
            az -= D * uz / V
            if veh.ld:
                L = q * cd * veh.ld * veh.ld_scale * interp(CAPSULE_LD_MACH, mach) * veh.S / veh.m
                # lift direction: radial up perpendicular to v, rotated by bank about v
                rx, ry, rz = x / r, y / r, z / r
                vxh, vyh, vzh = ux / V, uy / V, uz / V
                d = rx * vxh + ry * vyh + rz * vzh
                ex, ey, ez = rx - d * vxh, ry - d * vyh, rz - d * vzh
                en = math.sqrt(ex * ex + ey * ey + ez * ez)
                ex, ey, ez = ex / en, ey / en, ez / en
                # side = v x up_perp (to the right of the velocity)
                sx, sy, sz = vyh * ez - vzh * ey, vzh * ex - vxh * ez, vxh * ey - vyh * ex
                cb, sb = math.cos(bank), BANK_SIGN * math.sin(bank)
                ax += L * (cb * ex + sb * sx)
                ay += L * (cb * ey + sb * sy)
                az += L * (cb * ez + sb * sz)
    return ax, ay, az


TAUBER_SUTTON_MARS = [(6000, 0.2), (6150, 1.0), (6300, 1.95), (6500, 3.42), (6700, 5.1), (6900, 7.1),
                      (7000, 8.1), (7200, 10.2), (7400, 12.5), (7600, 14.8), (7800, 17.1), (8000, 19.2)]


def heat_flux(rho, V, rn):
    """Stagnation heat flux (W/m^2) as the simulator computes it at Mars."""
    if V <= 1500 or rho <= 0:
        return 0.0
    qc = 1.9027e-4 * math.sqrt(rho / rn) * V ** 3
    qr = 2.35e4 * rn ** 0.526 * rho ** 1.19 * interp(TAUBER_SUTTON_MARS, V) * 1e4 if V > 6000 else 0.0
    return qc + qr


def air_state(x, w):
    r = x[:3]
    v = x[3:]
    return np.asarray(v) - np.cross(w, r)


def fly(x0, veh, bank, k_rho, w, pz, stop, dt=1.0, t_max=900.0, bank_fn=None, on_step=None):
    """Integrate from state x0 (inertial, Mars-centered). bank: constant bank (rad) unless bank_fn(t, x)
    is given. stop(t, x, h, V, mach, q) -> True ends. Returns (t, state, info)."""
    x, y, z, vx, vy, vz = (float(c) for c in x0)
    t = 0.0
    peak_q, peak_g, heat = 0.0, 0.0, 0.0
    wx, wy, wz = w
    while t < t_max:
        b = bank if bank_fn is None else bank_fn(t, (x, y, z, vx, vy, vz))
        a1 = deriv(x, y, z, vx, vy, vz, veh, b, k_rho, w, pz)
        h2 = dt / 2
        a2 = deriv(x + vx * h2, y + vy * h2, z + vz * h2, vx + a1[0] * h2, vy + a1[1] * h2, vz + a1[2] * h2, veh, b, k_rho, w, pz)
        v2x, v2y, v2z = vx + a1[0] * h2, vy + a1[1] * h2, vz + a1[2] * h2
        a3 = deriv(x + v2x * h2, y + v2y * h2, z + v2z * h2, vx + a2[0] * h2, vy + a2[1] * h2, vz + a2[2] * h2, veh, b, k_rho, w, pz)
        v3x, v3y, v3z = vx + a2[0] * h2, vy + a2[1] * h2, vz + a2[2] * h2
        a4 = deriv(x + v3x * dt, y + v3y * dt, z + v3z * dt, vx + a3[0] * dt, vy + a3[1] * dt, vz + a3[2] * dt, veh, b, k_rho, w, pz)
        v4x, v4y, v4z = vx + a3[0] * dt, vy + a3[1] * dt, vz + a3[2] * dt
        x += dt / 6 * (vx + 2 * v2x + 2 * v3x + v4x)
        y += dt / 6 * (vy + 2 * v2y + 2 * v3y + v4y)
        z += dt / 6 * (vz + 2 * v2z + 2 * v3z + v4z)
        vx += dt / 6 * (a1[0] + 2 * a2[0] + 2 * a3[0] + a4[0])
        vy += dt / 6 * (a1[1] + 2 * a2[1] + 2 * a3[1] + a4[1])
        vz += dt / 6 * (a1[2] + 2 * a2[2] + 2 * a3[2] + a4[2])
        t += dt
        r = math.sqrt(x * x + y * y + z * z)
        h = r - R_REF
        ux = vx - (wy * z - wz * y)
        uy = vy - (wz * x - wx * z)
        uz = vz - (wx * y - wy * x)
        if veh.wind is not None:
            f = wind_factor(h, veh.site_alt)
            ux, uy, uz = ux - f * veh.wind[0], uy - f * veh.wind[1], uz - f * veh.wind[2]
        V = math.sqrt(ux * ux + uy * uy + uz * uz)
        rho = A.density(h) * k_rho if h < 140e3 else 0.0
        q = 0.5 * rho * V * V
        mach = V / A.sound_speed(h)
        peak_q = max(peak_q, q)
        if on_step:
            on_step(t, (x, y, z, vx, vy, vz), h, V, mach, q)
        if stop(t, (x, y, z, vx, vy, vz), h, V, mach, q) or h < -2600.0:
            break
    return t, np.array([x, y, z, vx, vy, vz]), {"peak_q": peak_q, "h": h, "V": V, "mach": mach, "q": q}
