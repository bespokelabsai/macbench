"""Earth's atmosphere as marsflight flies it today: Tudat's NRLMSISE-00, the ``atmosphere.earth``
role's default.

This is the code of ``environment.write_space_weather`` and of ``World._earth_density_scale``, moved here as
``docs/LAYOUT.md`` maps it, so that switching to NRLMSIS 2.1 (``earth_msis21``) is a one-line change of
``default``. Until the move lands, ``environment.py`` still holds the live copy; ``dev/test_earth_env.py``
checks that the two write the same space weather file, byte for byte, for every seed it tries.

NRLMSISE-00: Picone, Hedin, Drob and Aikin, JGR 107 (A12), 1468, 2002, in Tudat's C++ (tudatpy 1.0.0).
Space weather: Tudat's copy of CelesTrak's file (observed to mid 2025, then a monthly flux forecast), turned
into one daily record per world by ``write_space_weather`` below.
"""
from __future__ import annotations

import os
import tempfile

import numpy as np

from ...core.model import Provider

# Space weather for NRLMSISE-00. Tudat's file has observed days to July 2025 and then only a monthly solar flux
# forecast with no geomagnetic index, so Tudat read Ap = 0 for every mission date and warned that the day was
# missing. Each world gets its own daily file instead: the solar flux follows the forecast's 81-day level, and
# the geomagnetic indices (Kp, Ap) and the day-to-day flux variation are copied from 27-day blocks (one solar
# rotation) of the observed record at a similar level of solar activity, drawn from the world's seed.
SPACE_WEATHER_DAYS = ((2029, 1, 1), (2041, 10, 1))
_SPACE_WEATHER = None


def _space_weather_record():
    """(observed rows, monthly forecast rows) of Tudat's space weather file, split into fields, read once."""
    global _SPACE_WEATHER
    if _SPACE_WEATHER is None:
        import tudatpy.data
        obs, monthly, section = [], [], None
        with open(os.path.join(tudatpy.data.get_space_weather_path(), "sw19571001.txt")) as f:
            for line in f:
                if line.startswith(("BEGIN ", "END ")):
                    section = line.split()[1] if line.startswith("BEGIN ") else None
                    continue
                p = line.split()
                if section == "OBSERVED" and len(p) == 33 and int(p[0]) >= 1964:
                    obs.append(p)
                elif section == "MONTHLY_PREDICTED" and len(p) == 12:
                    monthly.append(p)
        _SPACE_WEATHER = (obs, monthly)
    return _SPACE_WEATHER


def write_space_weather(path, seed):
    """A daily space weather file in Tudat's (CelesTrak) fixed-width format for SPACE_WEATHER_DAYS."""
    import datetime as _dt
    obs, monthly = _space_weather_record()
    t_f = np.array([_dt.date(int(p[0]), int(p[1]), int(p[2])).toordinal() for p in monthly], dtype=float)
    # forecast columns: adjusted F10.7, its 81-day centered and trailing means, the same observed, sunspot number
    f_cols = np.array([[float(p[i]) for i in (6, 7, 8, 9, 10, 11, 5)] for p in monthly])
    o_ctr = np.array([float(p[31]) for p in obs])                     # observed 81-day centered flux
    first, last = (_dt.date(*d).toordinal() for d in SPACE_WEATHER_DAYS)
    days = np.arange(first, last + 1)
    fc = np.column_stack([np.interp(days, t_f, f_cols[:, j]) for j in range(f_cols.shape[1])])
    rng = np.random.default_rng([0 if seed is None else seed, 4])
    bartels0 = _dt.date(2025, 6, 27).toordinal()                      # Bartels rotation 2617 began that day
    rows, i = [], 0
    while i < len(days):
        level = fc[i, 4]
        near = np.nonzero(np.abs(o_ctr[:-27] / level - 1.0) < 0.15)[0]
        if len(near) < 27:
            near = np.argsort(np.abs(o_ctr[:-27] - level))[:200]
        j0 = int(rng.choice(near))
        for k in range(min(27, len(days) - i)):
            src, (fa, ca, la, fo, co, lo, isn) = obs[j0 + k], fc[i]
            d = _dt.date.fromordinal(int(days[i]))
            kp = [int(x) for x in src[5:13]]
            ap = [int(x) for x in src[14:22]]
            # the day's flux over its 81-day mean in the record, at the forecast's 81-day level
            fa, fo = ca * float(src[26]) / float(src[28]), co * float(src[30]) / float(src[31])
            n = int(days[i]) - bartels0
            rows.append(f"{d.year:4d}{d.month:3d}{d.day:3d}{2617 + n // 27:5d}{n % 27 + 1:3d}" + "".join(f"{x:3d}" for x in kp)
                        + f"{int(src[13]):4d}" + "".join(f"{x:4d}" for x in ap) + f"{int(src[22]):4d}{float(src[23]):4.1f}"
                        f"{int(src[24]):2d}{int(round(isn)):4d}{fa:6.1f}{0:2d}{ca:6.1f}{la:6.1f}{fo:6.1f}{co:6.1f}{lo:6.1f}")
            i += 1
    with open(path, "w") as f:
        f.write("DATATYPE CssiSpaceWeather\n# marsflight: daily space weather for the mission years (environment.write_space_weather)\n")
        f.write(f"NUM_OBSERVED_POINTS {len(rows)}\nBEGIN OBSERVED\n" + "\n".join(rows) + "\nEND OBSERVED\n")


class EarthNrlmsise00(Provider):
    """Tudat's NRLMSISE-00 with the world's space weather file and density factor: today's behavior."""
    role = "atmosphere.earth"
    name = "earth_nrlmsise00"
    default = False

    def configure(self, ctx):
        self.ctx = ctx
        self.seed = ctx.seed
        self.factor = getattr(ctx.world, "earth_density", 1.0) if ctx.world is not None else 1.0
        self.space_weather = None
        self._tudat = None

    def _file(self):
        if self.space_weather is None:
            self.space_weather = os.path.join(tempfile.mkdtemp(prefix="marsflight_"), "space_weather.txt")
            write_space_weather(self.space_weather, self.seed)
        return self.space_weather

    def scale(self, t):
        return self.factor

    def tudat_settings(self):
        from tudatpy.dynamics import environment_setup as es
        return es.atmosphere.scaled_by_function(es.atmosphere.nrlmsise00(self._file()), self.scale)

    # The role's methods (angles in radians). Density from Tudat's own NRLMSISE-00 class on the same file.
    def density(self, h, lat, lon, t):
        if self._tudat is None:
            import tudatpy.data as tdata
            from tudatpy.dynamics import environment_setup as es
            self._tudat = es.atmosphere.NRLMSISE00Atmosphere(tdata.read_solar_activity_data(self._file()))
        return self.factor * self._tudat.get_density(h, lon, lat, t)


PROVIDERS = [EarthNrlmsise00]
