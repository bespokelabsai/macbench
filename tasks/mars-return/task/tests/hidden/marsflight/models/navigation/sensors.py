"""The sensor suite of one flight with MARSFLIGHT_NAV on: the IMU and the navigation filter.

Ported from the ``sensors.Sensors`` onto the new layout. The change: a DSN pass is taken only when
the vehicle is really in two-way contact (uplink and downlink, ``models/comms/dsn.py``), so passes
are lost behind a planet, in the solar conjunction and when no booked complex sees the vehicle.

``core/guidance_link.py`` calls ``observe`` once per obs. It carries the error from the last obs to now along
two-body motion of the true state and takes the DSN passes due in between. The inertial part over powered or
atmospheric arcs (``inertial``) needs the arc's force history from ``core/arcs.py`` and is not called yet.
"""
from __future__ import annotations

import math

import numpy as np

from ..avionics.imu import IMU
from .filter import EDL_PRIOR, NAV, Navigation, kepler, time_to_radius, walk

ENTRY_ALT = {"Earth": 150e3, "Mars": 130e3}


class NavSensors:
    def __init__(self, seed, scale, contact=None, small_forces=None):
        """contact(r, central, t) -> bool: two-way DSN contact. None means no DSN at all (no passes)."""
        self.rng = np.random.default_rng([2108, 0 if seed is None else int(seed) + 1])
        self.imu = IMU(self.rng, scale)
        self.nav = Navigation(self.rng, scale)
        self.contact = contact
        self.small = small_forces
        self.lost_passes = 0

    # ---------------------------------------------------------------- which measurement applies
    @staticmethod
    def gps(central, alt):
        g = NAV["gps"]
        if central != "Earth" or alt >= g["alt_max"]:
            return None
        return g["low"] if alt < g["low_alt"] else g["high"]

    def dsn(self, central, x, t, alt, mu, radius):
        """The pass size that applies at state x and time t, or None: within GPS range, in the last 6 hours before
        an entry, or with no two-way DSN contact."""
        if self.gps(central, alt) is not None:
            return None
        if central in ENTRY_ALT and radius is not None:
            te = time_to_radius(x, mu, radius + ENTRY_ALT[central])
            if te is not None and te < NAV["dsn"]["entry_cutoff_s"]:
                return None
        if self.contact is None:
            return None
        try:
            ok = bool(self.contact(np.asarray(x[:3], float), central, t))
        except Exception:  # noqa: BLE001  (a geometry error costs the pass, not the flight)
            ok = False
        if not ok:
            self.lost_passes += 1
            return None
        return NAV["dsn"]["cruise" if central == "Sun" else "planet"]

    def on_ground(self):
        """The vehicle stands on the pad or the Mars surface: the filter is off and its next start (at liftoff)
        takes the ground's knowledge of the site, not the state it carried before the landing."""
        self.nav.active = False
        self.from_ground = True

    def prior(self, central, alt, entering_mars):
        g = self.gps(central, alt)
        ground, self.from_ground = getattr(self, "from_ground", False), False
        if g is not None:
            sr, sv = g
        elif ground:
            sr, sv = NAV["surface"]["sigma_r"], NAV["surface"]["sigma_v"]
        elif entering_mars and alt < EDL_PRIOR["below_m"]:
            sr, sv = EDL_PRIOR["sigma_r"], EDL_PRIOR["sigma_v"]
        else:
            d = NAV["dsn"]["cruise" if central == "Sun" else "planet"]
            sr, sv = d["sigma_r"], d["sigma_v"]
        return np.diag([sr * sr] * 3 + [sv * sv] * 3)

    def q(self, mass_kg, t):
        q = NAV["process_noise"]
        if self.small is not None:
            q += self.small.process_noise(mass_kg, t)
        return q

    # ---------------------------------------------------------------- one step
    def observe(self, t, x, central, mu, alt, radius=None, coast=True, mass_kg=1000.0, entering_mars=False,
                frame_changed=False):
        """Carry the error to time t (true state x about ``central``), take the DSN passes due on the way (coast
        only) and the GPS fix if in range. Returns the error (position, velocity) to add to the true state."""
        nav = self.nav
        x = np.asarray(x, dtype=float)
        if not nav.active:
            nav.start(t, x, self.prior(central, alt, entering_mars), "start")
            return nav.e[:3].copy(), nav.e[3:].copy()
        if frame_changed or nav.t is None or t < nav.t:
            nav.ref, nav.t = x.copy(), t
        dt = t - nav.t
        q = self.q(mass_kg, t)
        if coast and dt > 0:
            nav.fold()
            step = NAV["dsn"]["cruise" if central == "Sun" else "planet"]["interval_s"]
            k = math.floor(nav.t / step) + 1
            while k * step < t:
                tp = k * step
                k += 1
                # the true state at the pass, two-body back from now (the arc between two obs is a coast)
                xp = _back(x, t - tp, mu)
                d = self.dsn(central, xp, tp, alt, mu, radius)
                if d is None:
                    continue
                nav.propagate(tp, xp, mu, q)
                sig = np.array([d["sigma_r"]] * 3 + [d["sigma_v"]] * 3)
                nav.update(np.eye(6), np.diag(sig * sig), sig, "dsn", tp)
        nav.propagate(t, x, mu, q)
        g = self.gps(central, alt)
        if g is not None:
            nav.fix(g[0], g[1], "gps", t)
        return nav.e[:3].copy(), nav.e[3:].copy()

    def inertial(self, ts, F, R_true, R_est):
        """The accelerometer errors integrated over an arc (times ts, true specific forces F in J2000)."""
        t = np.asarray(ts, dtype=float)
        dt = np.diff(t)[:, None]

        def integrate(a):
            v = np.vstack([np.zeros(3), np.cumsum(0.5 * (a[1:] + a[:-1]) * dt, axis=0)])
            return v, np.sum(0.5 * (v[1:] + v[:-1]) * dt, axis=0)

        F = np.asarray(F, dtype=float)
        dv_k, dr = integrate(np.array([self.imu.systematic(f, R_true, R_est) - f for f in F]))
        dV_k, S1 = integrate(F)
        C = walk(self.imu.noise ** 2, t[-1] - t[0])
        w = self.nav.scale * self.rng.multivariate_normal(np.zeros(2), C, 3)
        self.nav.inertial(dr + w[:, 0], dv_k[-1] + w[:, 1], dV_k[-1], S1, R_true, C)

    def trn(self, sigma_m, t):
        self.nav.position_fix(sigma_m, "trn", t)
        return self.nav.e[:3].copy(), self.nav.e[3:].copy()

    def report(self):
        sr, sv = self.nav.sigma()
        kind, t = self.nav.last
        return {"position_m": sr, "velocity_mps": sv}, {"kind": kind, "t": t}


def _back(x, dt, mu):
    """The two-body state dt seconds before x (time reversal: flip the velocity, go forward, flip back)."""
    y = np.asarray(x, dtype=float).copy()
    y[3:] *= -1.0
    z = kepler(y, dt, mu)[0]
    z[3:] *= -1.0
    return z


def dsn_contact(comms_model=None):
    """contact(r, central, t) from the comms model's booked DSN (models/comms), or a default 3-complex booking."""
    if comms_model is not None and getattr(comms_model, "contact", None) is not None:
        c = comms_model.contact
    else:
        from ..comms.dsn import Contact
        c = Contact()

    def contact(r, central, t):
        s = c.state(r, central, t)
        return bool(s["uplink"] and s["downlink"])
    return contact
