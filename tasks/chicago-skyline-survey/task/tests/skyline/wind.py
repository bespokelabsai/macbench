"""Wind model.

A wind day holds hourly 10 m and 100 m wind components. At height z and time t:

1. u10, v10, u100, v100 are interpolated linearly in t, clamped at the ends;
2. ze = max(z, 10);
3. u(z) = u10 + (u100 - u10) * ln(ze/10) / ln(10), and the same for v.

Wind is horizontal and uniform in x, y. u is eastward, v northward (the
direction the air moves toward).
"""

from __future__ import annotations

import json
import math
import os
from bisect import bisect_right


class Wind:
    """One wind day. Call :meth:`at` for the wind vector at (z, t)."""

    def __init__(self, t_s, u10, v10, u100, v100, source: str = "", date: str = "", label: str = ""):
        n = len(t_s)
        if n == 0 or not all(len(a) == n for a in (u10, v10, u100, v100)):
            raise ValueError("wind arrays must be non-empty and of equal length")
        self.t_s = [float(v) for v in t_s]
        if any(b <= a for a, b in zip(self.t_s, self.t_s[1:])):
            raise ValueError("wind t_s must be strictly increasing")
        self.u10 = [float(v) for v in u10]
        self.v10 = [float(v) for v in v10]
        self.u100 = [float(v) for v in u100]
        self.v100 = [float(v) for v in v100]
        self.source, self.date, self.label = source, date, label

    def components(self, t: float) -> tuple[float, float, float, float]:
        """(u10, v10, u100, v100) at time t, linear in t and clamped at the ends."""
        ts = self.t_s
        if t <= ts[0]:
            return self.u10[0], self.v10[0], self.u100[0], self.v100[0]
        if t >= ts[-1]:
            return self.u10[-1], self.v10[-1], self.u100[-1], self.v100[-1]
        i = bisect_right(ts, t) - 1
        f = (t - ts[i]) / (ts[i + 1] - ts[i])
        g = 1.0 - f
        return (self.u10[i] * g + self.u10[i + 1] * f, self.v10[i] * g + self.v10[i + 1] * f,
                self.u100[i] * g + self.u100[i + 1] * f, self.v100[i] * g + self.v100[i + 1] * f)

    def at(self, z: float, t: float) -> tuple[float, float]:
        """Wind vector (u, v) in m/s at height z (m) and time t (s)."""
        u10, v10, u100, v100 = self.components(t)
        ze = z if z > 10.0 else 10.0
        k = math.log(ze / 10.0) / math.log(10.0)
        return u10 + (u100 - u10) * k, v10 + (v100 - v10) * k

    def speed(self, z: float, t: float) -> float:
        """Wind speed |w| in m/s at (z, t)."""
        u, v = self.at(z, t)
        return math.hypot(u, v)


def load_wind(path: str) -> Wind:
    """Load one wind day (``wind_forecast.json`` or ``wind_dayN.json``).

    ``wind.label`` is the file stem without a leading ``wind_`` (for example
    ``"forecast"`` or ``"day1"``); the flight log uses it as the day name.
    """
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    stem = os.path.splitext(os.path.basename(path))[0]
    label = stem[5:] if stem.startswith("wind_") else stem
    return Wind(d["t_s"], d["u10"], d["v10"], d["u100"], d["v100"],
                source=str(d.get("source", "")), date=str(d.get("date", "")), label=label)
