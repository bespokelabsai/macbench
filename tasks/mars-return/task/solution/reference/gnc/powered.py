"""Powered-flight prediction and targeting in vacuum.

A fast RK4 integrator (plain floats) for point mass + J2 gravity with constant thrust and mass flow,
and a Newton shooting solver for linear pitch / yaw steering laws defined in the local
radial / horizontal / plane-normal frame. Used for orbit insertion on Earth and Mars ascent.
"""
from __future__ import annotations

import math

import numpy as np

from .util import J2, MU, RREF



def _rk4(state, F, mdot, steer, T, mu, j2k, pz, h):
    """state = (x, y, z, vx, vy, vz, m). steer(tau, x, y, z, vx, vy, vz) -> (ux, uy, uz) unit.
    j2k = 1.5 * J2 * mu * R^2 (0 to disable). Returns the state after T seconds."""
    px, py, pzz = pz
    n = max(1, int(math.ceil(T / h)))
    h = T / n
    x, y, z, vx, vy, vz, m = state
    tau = 0.0

    def acc(tau, x, y, z, vx, vy, vz, m):
        r2 = x * x + y * y + z * z
        r = math.sqrt(r2)
        ir3 = 1.0 / (r2 * r)
        ax, ay, az = -mu * x * ir3, -mu * y * ir3, -mu * z * ir3
        if j2k:
            zz = x * px + y * py + z * pzz
            k = j2k / (r2 * r2 * r)
            s = 5.0 * zz * zz / r2
            ax -= k * ((1 - s) * x + 2 * zz * px)
            ay -= k * ((1 - s) * y + 2 * zz * py)
            az -= k * ((1 - s) * z + 2 * zz * pzz)
        if F > 0 and m > 0:
            ux, uy, uz = steer(tau, x, y, z, vx, vy, vz)
            a = F / m
            ax += a * ux
            ay += a * uy
            az += a * uz
        return ax, ay, az

    for _ in range(n):
        a1 = acc(tau, x, y, z, vx, vy, vz, m)
        m2 = m - mdot * h / 2
        x2, y2, z2 = x + vx * h / 2, y + vy * h / 2, z + vz * h / 2
        v2x, v2y, v2z = vx + a1[0] * h / 2, vy + a1[1] * h / 2, vz + a1[2] * h / 2
        a2 = acc(tau + h / 2, x2, y2, z2, v2x, v2y, v2z, m2)
        x3, y3, z3 = x + v2x * h / 2, y + v2y * h / 2, z + v2z * h / 2
        v3x, v3y, v3z = vx + a2[0] * h / 2, vy + a2[1] * h / 2, vz + a2[2] * h / 2
        a3 = acc(tau + h / 2, x3, y3, z3, v3x, v3y, v3z, m2)
        m4 = m - mdot * h
        x4, y4, z4 = x + v3x * h, y + v3y * h, z + v3z * h
        v4x, v4y, v4z = vx + a3[0] * h, vy + a3[1] * h, vz + a3[2] * h
        a4 = acc(tau + h, x4, y4, z4, v4x, v4y, v4z, m4)
        x += h / 6 * (vx + 2 * v2x + 2 * v3x + v4x)
        y += h / 6 * (vy + 2 * v2y + 2 * v3y + v4y)
        z += h / 6 * (vz + 2 * v2z + 2 * v3z + v4z)
        vx += h / 6 * (a1[0] + 2 * a2[0] + 2 * a3[0] + a4[0])
        vy += h / 6 * (a1[1] + 2 * a2[1] + 2 * a3[1] + a4[1])
        vz += h / 6 * (a1[2] + 2 * a2[2] + 2 * a3[2] + a4[2])
        m -= mdot * h
        tau += h
    return (x, y, z, vx, vy, vz, m)


def lt_steer(params, nhat, tangent=False):
    """Pitch theta(tau) = th0 + thd*tau above the local horizontal in the target plane, yaw
    psi(tau) = ps0 + psd*tau out of the plane (along nhat)."""
    th0, thd, ps0, psd = params
    nx, ny, nz = nhat

    def steer(tau, x, y, z, vx, vy, vz):
        r = math.sqrt(x * x + y * y + z * z)
        rx, ry, rz = x / r, y / r, z / r
        # horizontal in-plane direction: n x r_hat
        hx, hy, hz = ny * rz - nz * ry, nz * rx - nx * rz, nx * ry - ny * rx
        hn = math.sqrt(hx * hx + hy * hy + hz * hz)
        hx, hy, hz = hx / hn, hy / hn, hz / hn
        th = math.atan(math.tan(th0) + thd * tau) if tangent else th0 + thd * tau
        ps = ps0 + psd * tau
        c, s = math.cos(th), math.sin(th)
        ux, uy, uz = c * hx + s * rx + ps * nx, c * hy + s * ry + ps * ny, c * hz + s * rz + ps * nz
        un = math.sqrt(ux * ux + uy * uy + uz * uz)
        return ux / un, uy / un, uz / un

    return steer


def lt_dir(params, nhat, r, tau=0.0, tangent=False):
    s = lt_steer(params, nhat, tangent)
    return np.array(s(tau, *r, 0.0, 0.0, 0.0))


class Insertion:
    """Closed-loop orbit insertion: hit radius r_t, radial speed vr_t, horizontal speed vh_t, in the
    plane with normal nhat. Unknowns: pitch, pitch rate, yaw, yaw rate, burn time."""

    def __init__(self, body, r_t, vr_t, vh_t, nhat, h=4.0, j2=True, tangent=False):
        self.tangent = tangent
        self.body = body
        self.mu = MU[body]
        self.j2k = 1.5 * J2[body] * MU[body] * RREF[body] ** 2 if j2 else 0.0
        self.r_t, self.vr_t, self.vh_t = r_t, vr_t, vh_t
        self.n = np.asarray(nhat, float)
        self.h = h
        self.x = None   # [th0, thd, ps0, psd, T]
        self.pz = (0.0, 0.0, 1.0)

    def resid(self, state, F, mdot, x):
        T = max(x[4], 1.0)
        s = _rk4(state, F, mdot, lt_steer(x[:4], self.n, self.tangent), T, self.mu, self.j2k, self.pz, self.h)
        r = np.array(s[:3])
        v = np.array(s[3:6])
        rn = np.linalg.norm(r)
        rh = r / rn
        hh = np.cross(self.n, rh)
        hh /= np.linalg.norm(hh)
        return np.array([(rn - self.r_t) / 1e3, float(np.dot(v, rh)) - self.vr_t, float(np.dot(v, hh)) - self.vh_t,
                         float(np.dot(r, self.n)) / 1e3, float(np.dot(v, self.n))]), s

    def guess(self, r, v, F, mdot, m, isp_g0):
        rh = r / np.linalg.norm(r)
        hh = np.cross(self.n, rh)
        hh /= np.linalg.norm(hh)
        vh = float(np.dot(v, hh))
        vr = float(np.dot(v, rh))
        dv = max(50.0, self.vh_t - vh) + 0.3 * abs(vr - self.vr_t)
        T = m / mdot * (1 - math.exp(-dv / isp_g0))
        return np.array([0.0, 0.0, 0.0, 0.0, max(T, 5.0)])

    # bounds keep the solver on the physical, near-minimum-fuel branch
    LO = np.array([-1.0, -0.02, -0.15, -0.003, 0.5])
    HI = np.array([1.5, 0.02, 0.15, 0.003, 1500.0])

    def solve(self, r, v, m, F, mdot, iters=2, shift=0.0, isp_g0=3500.0, pz=None):
        from scipy.optimize import least_squares
        if pz is not None:
            self.pz = tuple(float(c) for c in pz)
        state = tuple(float(c) for c in r) + tuple(float(c) for c in v) + (float(m),)
        if self.x is None:
            self.x = self.guess(np.asarray(r, float), np.asarray(v, float), F, mdot, m, isp_g0)
            iters = max(iters, 12)
        else:
            x = self.x.copy()
            x[0] = math.atan(math.tan(x[0]) + x[1] * shift) if self.tangent else x[0] + x[1] * shift
            x[2] += x[3] * shift
            if not getattr(self, "stage1", None):
                x[4] -= shift
            self.x = x
        self.x = np.clip(self.x, self.LO + 1e-9, self.HI - 1e-9)
        steps = np.array([1e-4, 1e-6, 1e-4, 1e-6, 0.05])

        def fun(x):
            return self.resid(state, F, mdot, x)[0]

        def jac(x):
            g0 = fun(x)
            J = np.zeros((5, 5))
            for k in range(5):
                xp = x.copy()
                xp[k] += steps[k]
                J[:, k] = (fun(xp) - g0) / steps[k]
            return J

        sol = least_squares(fun, self.x, jac=jac, bounds=(self.LO, self.HI), method="trf", max_nfev=iters,
                            x_scale=np.array([0.05, 0.0005, 0.02, 0.0002, 10.0]))
        self.x = sol.x
        g, _ = self.resid(state, F, mdot, self.x)
        return self.x.copy(), g

    def direction(self, r, tau):
        return lt_dir(self.x[:4], self.n, r, tau, self.tangent)


class StagedInsertion(Insertion):
    """Insertion with a first stage that burns for a known time t1, drops m_drop, a gap of t_gap
    seconds (ignition delay), then the final stage burns for the free time T."""

    def __init__(self, body, r_t, vr_t, vh_t, nhat, h=4.0, j2=True, tangent=True):
        super().__init__(body, r_t, vr_t, vh_t, nhat, h, j2, tangent)
        self.stage1 = None      # dict(F, mdot, t1, m_drop, t_gap)

    def resid(self, state, F, mdot, x):
        if not self.stage1 or self.stage1["t1"] <= 0.5:
            return super().resid(state, F, mdot, x)
        s1 = self.stage1
        steer = lt_steer(x[:4], self.n, self.tangent)
        st = _rk4(state, s1["F"], s1["mdot"], steer, s1["t1"], self.mu, self.j2k, self.pz, self.h)
        st = st[:6] + (st[6] - s1["m_drop"],)
        t_off = s1["t1"]
        if s1.get("t_gap", 0.0) > 0:
            st = _rk4(st, 0.0, 0.0, steer, s1["t_gap"], self.mu, self.j2k, self.pz, 1.0)
            t_off += s1["t_gap"]
        th0, thd, ps0, psd = x[:4]
        th_b = math.atan(math.tan(th0) + thd * t_off) if self.tangent else th0 + thd * t_off
        x2 = (th_b, thd, ps0 + psd * t_off, psd)
        T = max(x[4], 1.0)
        s = _rk4(st, F, mdot, lt_steer(x2, self.n, self.tangent), T, self.mu, self.j2k, self.pz, self.h)
        r = np.array(s[:3])
        v = np.array(s[3:6])
        rn = np.linalg.norm(r)
        rh = r / rn
        hh = np.cross(self.n, rh)
        hh /= np.linalg.norm(hh)
        return np.array([(rn - self.r_t) / 1e3, float(np.dot(v, rh)) - self.vr_t, float(np.dot(v, hh)) - self.vh_t,
                         float(np.dot(r, self.n)) / 1e3, float(np.dot(v, self.n))]), s
