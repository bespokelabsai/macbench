"""The navigation filter, simulated by its error and its covariance (ported from the sensors.py).

Between measurements the error follows the vehicle's two-body motion about the central body and takes up a random
walk in velocity for the small forces no model knows exactly (small_forces.py sets its size). In powered flight and
in the air the accelerometer errors that inertial navigation integrates add to it. At a measurement the Kalman gain
weighs the measurement against the prediction and the error shrinks.

Measurement sizes (NAV) are the, set so that the Mars entry state comes out about as accurate as MSL's onboard
entry state (200 m and 0.11 m/s; Martin-Mur et al., "Mars Science Laboratory Navigation Results", ISSFD 2012).
"""
from __future__ import annotations

import math

import numpy as np

from ..avionics.imu import LN200S

NAV = {
    # On the ground the site is known to 30 m (the manual), and it moves only with the planet's rotation: 0.01 m/s.
    "surface": {"sigma_r": 30.0, "sigma_v": 0.01},
    "gps": {"alt_max": 2.0e7, "low_alt": 2.0e6, "low": (5.0, 0.05), "high": (10.0, 0.01)},
    # A DSN pass (two-way range and Doppler, delta-DOR) is one measurement of the whole state. Passes are booked
    # once a day in cruise and every 6 hours inside a planet's sphere of influence, and taken only when the
    # vehicle is in two-way contact (models/comms/dsn.py). None in the last 6 hours before an entry: MSL's last
    # entry state update used tracking up to entry minus 6 hours.
    "dsn": {"cruise": {"interval_s": 86400.0, "sigma_r": 3000.0, "sigma_v": 0.003},
            "planet": {"interval_s": 21600.0, "sigma_r": 200.0, "sigma_v": 0.001},
            "entry_cutoff_s": 6 * 3600.0},
    "process_noise": 1.0e-12,   # m^2/s^3 floor; small_forces.SmallForces.process_noise adds the known sources
    # MSL's terminal descent radar locked on at 8.3 km (Way et al., AAS 13-420)
    "radar": {"range_m": 8000.0, "height_frac": 0.01, "speed_frac": 0.01, "speed_floor": 0.05},
}
EDL_PRIOR = {"below_m": 130e3, "sigma_r": 200.0, "sigma_v": 1.0}


def time_to_radius(x, mu, radius):
    """Seconds until the two-body path from x, on its way in, comes down to radius; None if it goes out or passes above."""
    r, v = np.asarray(x[:3], dtype=float), np.asarray(x[3:6], dtype=float)
    rn, rv = float(np.linalg.norm(r)), float(np.dot(r, v))
    if rv >= 0 or rn <= radius:
        return None
    h = np.cross(r, v)
    p = float(np.dot(h, h)) / mu
    e = float(np.linalg.norm(((np.dot(v, v) - mu / rn) * r - rv * v) / mu))
    if p / (1 + e) >= radius or e < 1e-9:
        return None

    def mean_anomaly(rad):
        nu = -math.acos(max(-1.0, min(1.0, (p / rad - 1) / e)))
        if e < 1:
            E = 2 * math.atan(math.sqrt((1 - e) / (1 + e)) * math.tan(nu / 2))
            return E - e * math.sin(E)
        F = 2 * math.atanh(math.sqrt((e - 1) / (e + 1)) * math.tan(nu / 2))
        return e * math.sinh(F) - F

    a = abs(p / (1 - e * e))
    return (mean_anomaly(radius) - mean_anomaly(rn)) / math.sqrt(mu / a ** 3)


def body_frame(x_axis):
    """A body frame whose x axis is x_axis: columns x, y, z in J2000 (body to J2000)."""
    x = np.asarray(x_axis, dtype=float)
    x = x / np.linalg.norm(x)
    hint = np.array([0.0, 0.0, 1.0]) if abs(x[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    y = np.cross(hint, x)
    y /= np.linalg.norm(y)
    return np.column_stack([x, y, np.cross(x, y)])


def _stumpff(z):
    c2, c3 = np.empty_like(z), np.empty_like(z)
    pos, neg = z > 1e-6, z < -1e-6
    small = ~(pos | neg)
    s = np.sqrt(z[pos])
    c2[pos] = (1.0 - np.cos(s)) / z[pos]
    c3[pos] = (s - np.sin(s)) / s ** 3
    s = np.sqrt(-z[neg])
    c2[neg] = (np.cosh(s) - 1.0) / -z[neg]
    c3[neg] = (np.sinh(s) - s) / s ** 3
    zs = z[small]
    c2[small] = 0.5 - zs / 24.0 + zs * zs / 720.0
    c3[small] = 1.0 / 6.0 - zs / 120.0 + zs * zs / 5040.0
    return c2, c3


def kepler(X, dt, mu):
    """Two-body propagation of the states X (n x 6) forward by dt >= 0 s, universal variables (Vallado, algorithm 8),
    with Newton steps kept inside a bracket of the root."""
    X = np.atleast_2d(np.asarray(X, dtype=float))
    r0, v0 = X[:, :3], X[:, 3:]
    rn = np.linalg.norm(r0, axis=1)
    sq = math.sqrt(mu)
    rdv = np.sum(r0 * v0, axis=1)
    rv = rdv / sq
    alpha = 2.0 / rn - np.sum(v0 * v0, axis=1) / mu
    chi = sq * dt / rn
    ell, hyp = alpha > 1e-12, alpha < -1e-12
    chi[ell] = sq * dt * alpha[ell]
    if np.any(hyp) and dt > 0:
        a = 1.0 / alpha[hyp]
        arg = (-2.0 * mu * alpha[hyp] * dt) / (rdv[hyp] + np.sqrt(-mu * a) * (1.0 - rn[hyp] * alpha[hyp]))
        chi[hyp] = np.where(arg > 1.0, np.sqrt(-a) * np.log(np.maximum(arg, 1.0)), chi[hyp])
    lo, hi = np.zeros_like(chi), np.full_like(chi, np.inf)
    with np.errstate(over="ignore", invalid="ignore"):
        for _ in range(100):
            z = alpha * chi * chi
            c2, c3 = _stumpff(z)
            r = chi * chi * c2 + rv * chi * (1.0 - z * c3) + rn * (1.0 - z * c2)
            f = chi ** 3 * c3 + rv * chi * chi * c2 + rn * chi * (1.0 - z * c3) - sq * dt
            big = ~np.isfinite(f) | (f > 0)
            hi = np.where(big, chi, hi)
            lo = np.where(big, lo, chi)
            new = chi - f / r
            bad = ~np.isfinite(new) | (new <= lo) | (new >= hi)
            new = np.where(bad, np.where(np.isfinite(hi), 0.5 * (lo + hi), 2.0 * chi + 1.0), new)
            done = np.abs(new - chi) <= 1e-13 * np.maximum(np.abs(chi), 1.0)
            chi = new
            if np.all(done):
                break
    z = alpha * chi * chi
    c2, c3 = _stumpff(z)
    f = 1.0 - chi * chi * c2 / rn
    g = dt - chi ** 3 * c3 / sq
    r1 = f[:, None] * r0 + g[:, None] * v0
    r1n = np.linalg.norm(r1, axis=1)
    fd = sq / (r1n * rn) * chi * (z * c3 - 1.0)
    gd = 1.0 - chi * chi * c2 / r1n
    return np.hstack([r1, fd[:, None] * r0 + gd[:, None] * v0])


def transition(x, dt, mu):
    """6 x 6 two-body state transition matrix over dt from x, by central differences (100 m and 1 cm/s steps)."""
    h = np.array([100.0] * 3 + [0.01] * 3)
    X = np.repeat(np.asarray(x, dtype=float)[None, :], 12, axis=0)
    for j in range(6):
        X[2 * j, j] += h[j]
        X[2 * j + 1, j] -= h[j]
    Y = kepler(X, dt, mu)
    return np.column_stack([(Y[2 * j] - Y[2 * j + 1]) / (2.0 * h[j]) for j in range(6)])


def walk(q, dt):
    """Per-axis (position, velocity) covariance of a velocity random walk of spectral density q over dt."""
    return np.array([[q * dt ** 3 / 3.0, q * dt * dt / 2.0], [q * dt * dt / 2.0, q * dt]])


def expand(c2):
    return np.kron(c2, np.eye(3))


class Navigation:
    """The filter's error e (navigated minus true: position, velocity, J2000) and covariance P. ``ref`` is the true
    state at ``t`` for the two-body motion of the error. The inertial part (accelerometer scale factor and
    misalignment acting on the sensed delta-v dV and its integral S1) is kept as two vectors until a fix or a coast."""

    def __init__(self, rng, scale):
        self.rng, self.scale = rng, scale
        self.active = False
        self.t, self.ref = None, None
        self.e, self.P = np.zeros(6), np.zeros((6, 6))
        self.dV, self.S1, self.Rb = np.zeros(3), np.zeros(3), np.eye(3)
        self.last = ("none", None)
        self.history = []

    def save(self):
        """The filter's state for a scenario file (core/mission.py _save_state). Its random stream is not saved."""
        return {"active": bool(self.active), "t": self.t, "ref": None if self.ref is None else self.ref.tolist(),
                "e": self.e.tolist(), "P": self.P.tolist(), "dV": self.dV.tolist(), "S1": self.S1.tolist(),
                "Rb": self.Rb.tolist(), "last": list(self.last)}

    def load(self, d):
        self.active, self.t = bool(d.get("active")), d.get("t")
        self.ref = None if d.get("ref") is None else np.asarray(d["ref"], dtype=float)
        self.e, self.P = np.asarray(d["e"], dtype=float), np.asarray(d["P"], dtype=float)
        self.dV, self.S1, self.Rb = (np.asarray(d[k], dtype=float) for k in ("dV", "S1", "Rb"))
        self.last = tuple(d.get("last") or ("none", None))

    def start(self, t, x, P0, kind):
        self.active, self.t, self.ref = True, t, np.asarray(x, dtype=float).copy()
        self.P = np.array(P0, dtype=float)
        self.e = self.scale * self.rng.multivariate_normal(np.zeros(6), self.P)
        self.dV, self.S1 = np.zeros(3), np.zeros(3)
        self.last = (kind, t)

    def _inertial_cov(self):
        a, b = self.Rb.T @ self.S1, self.Rb.T @ self.dV
        s2, m2 = LN200S["scale_factor"] ** 2, LN200S["misalignment_rad"] ** 2

        def c(x, y):
            return self.Rb @ np.diag(s2 * x * y + m2 * (float(x @ y) - x * y)) @ self.Rb.T
        return np.block([[c(a, a), c(a, b)], [c(b, a), c(b, b)]])

    def cov(self):
        return self.P + self._inertial_cov()

    def fold(self):
        if np.any(self.dV) or np.any(self.S1):
            self.P = self.cov()
            self.dV, self.S1 = np.zeros(3), np.zeros(3)

    def propagate(self, t1, x1, mu, q):
        dt = t1 - self.t
        if dt > 0:
            F = transition(self.ref, dt, mu)
            Q = expand(walk(q, dt))
            self.e = F @ self.e + self.scale * self.rng.multivariate_normal(np.zeros(6), Q)
            self.P = F @ self.P @ F.T + Q
            self.S1 = self.S1 + self.dV * dt
        self.t, self.ref = t1, np.asarray(x1, dtype=float).copy()

    def inertial(self, dr, dv, dV, S1, Rb, noise_cov):
        self.e[:3] += dr
        self.e[3:] += dv
        self.S1 = self.S1 + S1
        self.dV = self.dV + dV
        self.Rb = Rb
        self.P = self.P + expand(noise_cov)

    def _state(self):
        sr, sv = self.sigma()
        return sr, float(np.linalg.norm(self.e[:3])), sv, float(np.linalg.norm(self.e[3:]))

    def _note(self, kind, t, before):
        self.last = (kind, t)
        if len(self.history) < 5000:
            self.history.append((t, kind, before, self._state()))

    def update(self, H, R, z_noise_sigma, kind, t):
        """Kalman update for a measurement of H x with noise covariance R."""
        before = self._state()
        P = self.cov()
        K = P @ H.T @ np.linalg.inv(H @ P @ H.T + R)
        nu = self.scale * self.rng.normal(0.0, 1.0, len(z_noise_sigma)) * z_noise_sigma
        self.e = self.e + K @ (nu - H @ self.e)
        I_KH = np.eye(6) - K @ H
        self.P = I_KH @ P @ I_KH.T + K @ R @ K.T
        self.dV, self.S1 = np.zeros(3), np.zeros(3)
        self._note(kind, t, before)

    def fix(self, sigma_r, sigma_v, kind, t):
        """A complete fix (GPS): the navigated state is the fix, with its own error."""
        before = self._state()
        self.P = np.diag([sigma_r ** 2] * 3 + [sigma_v ** 2] * 3)
        self.e = self.scale * np.concatenate([self.rng.normal(0.0, sigma_r, 3), self.rng.normal(0.0, sigma_v, 3)])
        self.dV, self.S1 = np.zeros(3), np.zeros(3)
        self._note(kind, t, before)

    def position_fix(self, sigma_r, kind, t):
        """A position-only fix (terrain-relative navigation) as a Kalman update."""
        H = np.hstack([np.eye(3), np.zeros((3, 3))])
        sig = np.full(3, float(sigma_r))
        self.update(H, np.diag(sig * sig), sig, kind, t)

    def sigma(self):
        P = self.cov()
        return math.sqrt(max(0.0, float(np.trace(P[:3, :3])))), math.sqrt(max(0.0, float(np.trace(P[3:, 3:]))))
