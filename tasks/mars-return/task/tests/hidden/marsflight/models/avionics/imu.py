"""Accelerometers with the errors of the Northrop Grumman LN-200S (ported from the sensors.py).

The LN-200S flew entry, descent and landing on the Mars Exploration Rovers and rides on Curiosity and Perseverance.
Data sheet DS-474-JYC-0213 (2013) and CS-23015 (2023), one sigma per axis: bias repeatability 300 ug, scale factor
300 ppm, noise 35 ug/sqrt(Hz), input axis alignment 0.1 mrad, largest input 40 g.
"""
from __future__ import annotations

import math

import numpy as np

G0 = 9.80665
LN200S = {"bias_g": 300e-6, "scale_factor": 300e-6, "noise_g_rthz": 35e-6, "misalignment_rad": 0.1e-3,
          "max_g": 40.0}
SENSED_AVERAGE_S = 1.0   # the sensed acceleration in obs is the IMU's mean over its last second


class IMU:
    """Errors drawn once per flight in the body frame. ``scale`` is the world's nav_scale (0 in the nominal world)."""

    def __init__(self, rng, scale):
        s = LN200S
        self.rng, self.scale = rng, scale
        self.bias = scale * rng.normal(0.0, s["bias_g"] * G0, 3)
        self.sf = scale * rng.normal(0.0, s["scale_factor"], 3)
        mis = scale * rng.normal(0.0, s["misalignment_rad"], (3, 3))
        np.fill_diagonal(mis, 0.0)
        self.M = np.eye(3) + mis
        self.noise = s["noise_g_rthz"] * G0   # m/s^2 per sqrt(Hz), or m/s per sqrt(s) of velocity random walk
        self.max = s["max_g"] * G0

    def systematic(self, f, R_true, R_est):
        """Measured specific force (J2000) without bias or noise: scale factor, misalignment, 40 g saturation."""
        fb = R_true.T @ np.asarray(f, dtype=float)
        return R_est @ np.clip(self.M @ ((1.0 + self.sf) * fb), -self.max, self.max)

    def measure(self, f, R_true, R_est, T=SENSED_AVERAGE_S):
        """What the IMU reports: its mean specific force over T seconds, in J2000."""
        fb = R_true.T @ np.asarray(f, dtype=float)
        mb = self.M @ ((1.0 + self.sf) * fb) + self.bias \
            + self.scale * self.rng.normal(0.0, self.noise / math.sqrt(max(T, 1e-3)), 3)
        return R_est @ np.clip(mb, -self.max, self.max)
