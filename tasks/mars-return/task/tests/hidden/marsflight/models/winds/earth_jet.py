"""Earth winds as marsflight flies them today: one jet at 12 km plus 5 m/s near the ground.

The code of ``environment.World._earth_wind``, moved here as ``docs/LAYOUT.md`` maps it, so that switching to
measured winds (``earth_soundings``) is a one-line change of ``default``. The jet's speed and heading are the
world's draws (``Dispersions.earth_jet_speed``, ``earth_jet_heading``). Until the move lands,
``environment.py`` still holds the live copy; ``dev/test_earth_env.py`` checks that the two agree exactly.
To be deleted once the measured winds are the default.
"""
from __future__ import annotations

import math

import numpy as np

from ...core.model import Provider


class EarthJet(Provider):
    role = "winds.earth"
    name = "earth_jet"
    default = True

    def configure(self, ctx):
        w = ctx.world
        self.speed = getattr(w, "earth_jet_speed", 30.0) if w is not None else 30.0
        self.heading = getattr(w, "earth_jet_heading", 270.0) if w is not None else 270.0

    def _speed(self, alt):
        s = self.speed * math.exp(-((alt - 12000.0) / 5000.0) ** 2) + 5.0 * min(1.0, alt / 1000.0)
        return 0.0 if alt > 60000 else s

    def wind(self, h, lat, lon, t):
        """(east, north, up) m/s."""
        s, hd = self._speed(h), math.radians(self.heading)
        return (s * math.sin(hd), s * math.cos(hd), 0.0)

    def tudat_wind(self, alt, lon, lat, t):
        """For ``custom_wind_model``: the same column ``World._earth_wind`` returns (north, east, down)."""
        s, hd = self._speed(alt), math.radians(self.heading)
        return np.array([[s * math.cos(hd)], [s * math.sin(hd)], [0.0]])


PROVIDERS = [EarthJet]
