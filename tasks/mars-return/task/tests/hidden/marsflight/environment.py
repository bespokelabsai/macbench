"""The physical world, built on Tudat (TU Delft).

- Ephemerides: Tudat's standard SPICE kernels (JPL DE-series planetary ephemerides).
- Earth: GGM02C spherical-harmonic gravity (truncated to degree 12 in flight), WGS84 oblate shape,
  NRLMSISE-00 atmosphere with daily space weather for the mission years (write_space_weather), IAU
  rotation, jet-stream winds.
- Mars: JGMRO-120D spherical-harmonic gravity (MRO radio science, degree 12 in flight), IAU rotation,
  a hydrostatic CO2 atmosphere built from a Viking/MSL-like temperature profile with the seasonal
  pressure cycle (Viking lander record), dust-storm density changes by height (fitted to six flown
  entries) and altitude-correlated density perturbations, plus winds. Mars-GRAM and the Mars Climate Database can't be redistributed, so this
  model stands in for them; our validation notes (not included) says what it covers and what it doesn't.
- Point-mass Sun, Moon, Venus, Jupiter, Saturn.

Dispersions: every uncertain quantity is drawn from a seed. Seed None gives the nominal world.
"""
from __future__ import annotations

import math
import os
import tempfile
from dataclasses import dataclass, field

import numpy as np

from . import data as D

MARS_R_GAS = 191.8
MARS_GAMMA = 1.30
# Annual-mean pressure at the 3389.5 km datum, fitted with the profile below to REMS surface pressure at Gale
# and to the MSL, Pathfinder, Spirit and Opportunity entry reconstructions (our validation notes, not included).
MARS_P0 = 549.9          # Pa at the reference radius, annual mean
MARS_REF_RADIUS = 3389.5e3
# Temperature profile (altitude km, K): one mean profile fitted to four entry reconstructions (MSL, Pathfinder,
# Spirit, Opportunity), with a smooth thermosphere rise above the data (128 km). Across six entries (adding
# Phoenix and InSight) its density bias is within 7% in every 20 km band. A seasonal temperature term fitted the
# four but failed both held-out entries, so the seasons are carried by the dispersion instead (Dispersions.draw).
MARS_T_PROFILE = [(-10, 237.0), (0, 221.9), (10, 207.1), (20, 193.7), (30, 181.2), (40, 169.4), (50, 156.9),
                  (60, 143.4), (80, 132.3), (100, 129.4), (110, 121.9), (120, 115.6), (140, 140), (160, 175),
                  (200, 210), (300, 215)]
# One-sigma scatter of log density between flown Mars entries, by altitude (km), from the same six entries.
MARS_DENSITY_SIGMA = [(0, 0.05), (20, 0.05), (30, 0.08), (50, 0.24), (70, 0.38), (90, 0.59), (130, 0.60)]
# Rarer, harder worlds. In some, a global dust storm is under way when the vehicle reaches Mars: it began
# up to 30 sols before, lasts 60 to 150 sols in all at optical depth 3 to 5 above the background, so it covers
# the landing and the first sols on the surface. In others, winds of 25 to 40 m/s blow over Jezero during the
# landing. Both come from a random stream of their own, so a world without them is the world v2 drew.
ARRIVAL_STORM_P = 0.10
HIGH_WIND_P = 0.10
# Dust. Dust warms the middle atmosphere, which expands: density rises with height and falls a little near
# the ground. d log(density) / d(optical depth) by height above the datum (km), fitted to the reconstructed
# densities of the six flown entries against the dust opacity at each (results/issue-3/dust_fit.py). Dust
# heating saturates in thick dust, so does the effect (DUST_TAU_SAT): in a global storm the air above 70 km
# is about 3.6 times as dense, at 50 km about 2.3 times, and near the ground about 11% thinner.
DUST_LOG_DENSITY_SLOPE = [(0, -0.15), (12, -0.15), (27, 0.27), (50, 1.05), (70, 1.6), (90, 1.65)]
DUST_TAU_SAT = 0.8
EARTH_SOI = 9.24e8
MARS_SOI = 5.77e8


def _interp_table(table, x):
    if x <= table[0][0]:
        return table[0][1]
    for (a, fa), (b, fb) in zip(table, table[1:]):
        if a <= x <= b:
            return fa + (fb - fa) * (x - a) / (b - a)
    return table[-1][1]


def mars_ls(t_tdb: float) -> float:
    """Areocentric solar longitude (deg), Allison & McEwen (2000) without planetary perturbations."""
    dt = t_tdb / 86400.0
    M = math.radians(19.3871 + 0.52402073 * dt)
    alpha = 270.3871 + 0.524038496 * dt
    eoc = (10.691 + 3.0e-7 * dt) * math.sin(M) + 0.623 * math.sin(2 * M) + 0.050 * math.sin(3 * M) \
        + 0.005 * math.sin(4 * M) + 0.0005 * math.sin(5 * M)
    return (alpha + eoc) % 360.0


def mars_seasonal_pressure_factor(ls_deg: float) -> float:
    """The annual pressure cycle from CO2 condensing on the polar caps: Tillman's Viking Lander 1 year-1
    harmonic model refitted in Ls (max residual 1.1%). Minimum 0.858 at Ls 148, maximum 1.134 at Ls 260."""
    ls = math.radians(ls_deg)
    return (1.0 + 0.0879 * math.cos(ls - math.radians(309.8)) + 0.0718 * math.cos(2 * (ls - math.radians(66.7)))
            + 0.0035 * math.cos(3 * (ls - math.radians(24.9))))


def dust_density_factor(tau: float, h_km: float) -> float:
    """Density at h_km in dust of optical depth tau, over the density at the background optical depth 0.5."""
    x = tau - 0.5
    if x <= 0:
        return 1.0
    return math.exp(_interp_table(DUST_LOG_DENSITY_SLOPE, h_km) * DUST_TAU_SAT * (1.0 - math.exp(-x / DUST_TAU_SAT)))


def mars_temperature(h_km: float) -> float:
    pts = MARS_T_PROFILE
    if h_km <= pts[0][0]:
        return pts[0][1]
    for (a, ta), (b, tb) in zip(pts, pts[1:]):
        if a <= h_km <= b:
            return ta + (tb - ta) * (h_km - a) / (b - a)
    return pts[-1][1]


@dataclass
class Dispersions:
    seed: int | None
    rng: np.random.Generator = field(default=None, repr=False)
    earth_density: float = 1.0
    earth_jet_speed: float = 30.0
    earth_jet_heading: float = 270.0          # direction the wind blows towards, deg from north
    mars_density_profile: list = field(default_factory=list)   # (alt_km, multiplier)
    mars_storm_start_sol: float = 1e9         # sols after epoch 0 of the run
    mars_storm_length_sols: float = 0.0
    mars_storm_tau: float = 0.0
    mars_wind_speed: float = 10.0
    mars_wind_heading: float = 90.0
    mars_ambient_T: float = 205.0
    heating_factor: float = 1.0
    aero_cd_factor: float = 1.0
    aero_ld_factor: float = 1.0
    chute_cd_factor: float = 1.0
    dry_mass_factor: dict = field(default_factory=dict)       # element id -> factor
    thrust_factor: dict = field(default_factory=dict)         # (element id) -> factor
    isp_factor: dict = field(default_factory=dict)
    pointing_sigma_deg: float = 0.0
    nav_scale: float = 0.0
    solar_factor: float = 1.0
    arrival_storm: tuple = None               # (sols it began before arrival, length in sols, tau) or None
    arrival_sol: float = None                 # when the vehicle reached Mars, once it has
    high_winds: bool = False

    @staticmethod
    def draw(seed, element_ids):
        d = Dispersions(seed)
        d.rng = np.random.default_rng(seed if seed is not None else 0)
        if seed is None:
            d.mars_density_profile = [(h, 1.0) for h in range(-10, 301, 10)]
            d.dry_mass_factor = {e: 1.0 for e in element_ids}
            d.thrust_factor = {e: 1.0 for e in element_ids}
            d.isp_factor = {e: 1.0 for e in element_ids}
            return d
        r = d.rng
        n = lambda s: float(r.normal(0.0, s))  # noqa: E731
        d.earth_density = max(0.7, 1.0 + n(0.05))
        d.earth_jet_speed = float(np.clip(30.0 + n(15.0), 0.0, 90.0))
        d.earth_jet_heading = float(r.uniform(0, 360))
        # Mars density: a smooth random field in altitude (an AR(1) walk with unit stationary variance and
        # about 45 km correlation) scaled by the scatter measured between flown entries (MARS_DENSITY_SIGMA).
        alts = list(range(-10, 301, 10))
        walk, prof = n(1.0), []   # start in the walk's stationary state, so the surface gets its full scatter
        for h in alts:
            sigma = _interp_table(MARS_DENSITY_SIGMA, h)
            walk = 0.8 * walk + 0.6 * n(1.0)
            prof.append((h, float(math.exp(sigma * walk))))
        d.mars_density_profile = prof
        # A global dust storm happens in about one Mars year in three.
        if r.uniform() < 0.35:
            d.mars_storm_start_sol = float(r.uniform(0, 700))
            d.mars_storm_length_sols = float(r.uniform(40, 120))
            d.mars_storm_tau = float(r.uniform(2.0, 5.0))
        d.mars_wind_speed = float(np.clip(10.0 + n(8.0), 0.0, 40.0))
        d.mars_wind_heading = float(r.uniform(0, 360))
        d.mars_ambient_T = 205.0 + n(5.0)
        d.heating_factor = max(0.6, 1.0 + n(0.08))
        d.aero_cd_factor = 1.0 + n(0.02)
        d.aero_ld_factor = 1.0 + n(0.04)
        d.chute_cd_factor = 1.0 + n(0.03)
        d.dry_mass_factor = {e: 1.0 + n(0.01) for e in element_ids}
        d.thrust_factor = {e: 1.0 + n(0.005) for e in element_ids}
        d.isp_factor = {e: 1.0 + n(0.002) for e in element_ids}
        d.pointing_sigma_deg = 0.05
        d.nav_scale = 1.0
        d.solar_factor = 1.0 + n(0.03)
        # The rarer, harder draws, from their own stream: every value is drawn whether it is used or not.
        h = np.random.default_rng([seed, 3])
        u_storm, began, length, tau = h.uniform(), h.uniform(0, 30), h.uniform(60, 150), h.uniform(3.0, 5.0)
        u_wind, wind = h.uniform(), h.uniform(25.0, 40.0)
        if u_storm < ARRIVAL_STORM_P:
            d.arrival_storm = (float(began), float(length), float(tau))
        if u_wind < HIGH_WIND_P:
            d.high_winds = True
            d.mars_wind_speed = float(wind)
        return d

    def reach_mars(self, sol):
        """The vehicle reached Mars (entered its sphere of influence) at this sol. In a world with an arrival storm,
        the storm is under way from now on: it replaces any other storm of the run."""
        if self.arrival_sol is not None:
            return
        self.arrival_sol = float(sol)
        if self.arrival_storm:
            began, length, tau = self.arrival_storm
            self.mars_storm_start_sol = sol - began
            self.mars_storm_length_sols = length
            self.mars_storm_tau = tau

    def summary(self):
        """The world's rare draws and its storm, for analysis after a flight."""
        storm = None
        if self.mars_storm_tau:
            storm = {"start_sol": round(self.mars_storm_start_sol, 1), "length_sols": round(self.mars_storm_length_sols, 1),
                     "tau": round(self.mars_storm_tau, 2)}
        return {"arrival_storm": bool(self.arrival_storm), "high_winds": self.high_winds, "dust_storm": storm,
                "mars_wind_mps": round(self.mars_wind_speed, 1),
                "arrival_sol": None if self.arrival_sol is None else round(self.arrival_sol, 2)}

    def mars_density_multiplier(self, h_km):
        prof = self.mars_density_profile
        if h_km <= prof[0][0]:
            return prof[0][1]
        for (a, fa), (b, fb) in zip(prof, prof[1:]):
            if a <= h_km <= b:
                return fa + (fb - fa) * (h_km - a) / (b - a)
        return prof[-1][1]

    def mars_tau(self, sol):
        base = 0.5
        if self.mars_storm_start_sol <= sol <= self.mars_storm_start_sol + self.mars_storm_length_sols:
            return base + self.mars_storm_tau
        return base


def write_mars_table(path, disp: Dispersions):
    """Hydrostatic integration of the temperature profile, times the dispersed density field."""
    hs = np.arange(-10e3, 300e3 + 1, 500.0)
    mu = 4.282837e13
    p = MARS_P0
    rows = []
    # integrate up from 0 km and down to -10 km
    def g(h):
        return mu / (MARS_REF_RADIUS + h) ** 2
    pvals = {}
    pvals[0.0] = p
    up = [h for h in hs if h >= 0]
    for a, b in zip(up, up[1:]):
        Tm = 0.5 * (mars_temperature(a / 1e3) + mars_temperature(b / 1e3))
        p = p * math.exp(-g(0.5 * (a + b)) * (b - a) / (MARS_R_GAS * Tm))
        pvals[b] = p
    p = MARS_P0
    down = [h for h in hs if h <= 0][::-1]
    for a, b in zip(down, down[1:]):
        Tm = 0.5 * (mars_temperature(a / 1e3) + mars_temperature(b / 1e3))
        p = p * math.exp(-g(0.5 * (a + b)) * (b - a) / (MARS_R_GAS * Tm))
        pvals[b] = p
    for h in hs:
        T = mars_temperature(h / 1e3)
        P = max(pvals[h], 1e-12)
        rho = P / (MARS_R_GAS * T) * disp.mars_density_multiplier(h / 1e3)
        rows.append((h, max(rho, 1e-15), P, T))
    # Tudat evaluates the atmosphere on every Mars-centered arc, coasts included, and at every integrator
    # stage, so the table must cover any state a trial step can reach: past the sphere of influence
    # (577,000 km) above, where it is a near vacuum, and down through the planet below, where nothing flies.
    # A trial step can be flung far out: one ending a 4.8-day coast in low Mars orbit asked for 1.06e12 m
    # and crashed the simulator when the table stopped at 1e12 m, so the table goes on to 1e20 m.
    _, rho_top, P_top, T_top = rows[-1]
    for h in (350e3, 500e3, 1e6, 3e6, 1e7, 3e7, 1e8, 3e8, 1e9, 1e10, 1e12, 1e15, 1e20):
        rows.append((h, min(rho_top, 1e-15), min(P_top, 1e-12), T_top))
    h0, rho0, P0, T0 = rows[0]
    rows[:0] = [(h, rho0, P0, T0) for h in (-4.0e6, -1.0e6, -1.0e5, -2.0e4)]
    with open(path, "w") as f:
        for h, rho, P, T in rows:
            f.write(f"{h:.1f} {rho:.6e} {P:.6e} {T:.3f}\n")


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


KERNELS = ["naif0012.tls", "pck00010.tpc", "inpop19a_TDB_m100_p100_spice.tpc", "NOE-4-2020.tpc",
           "de440.bsp", "tudat_merged_spk_kernel.bsp"]   # the tudat_merged provider's order (the older default)
_KERNELS_LOADED = None
SPICE_CENTRES = {}   # body -> the SPICE name it flies on, when the kernels lack its centre (DE440: Mars, Jupiter)


def ephemeris_provider():
    """The active ``ephemeris`` provider (core/registry.py): ``de440`` by default, ``tudat_merged`` for the order
    flown before the area models were wired in."""
    # Imported directly, not through the registry: flight software calls load_kernels too, and importing every
    # model module there costs seconds of its call time.
    from .models.gravity.ephemeris_de440 import PROVIDERS
    want = os.environ.get("MARSFLIGHT_EPHEMERIS")
    for p in PROVIDERS:
        if (want and p.name == want) or (not want and p.default):
            return p
    raise RuntimeError(f"no provider {want!r} for ephemeris; have {[p.name for p in PROVIDERS]}")


def load_kernels():
    """The SPICE kernels of the active ephemeris provider: DE440 alone by default (to 2650), the IAU PCK and masses.
    ``MARSFLIGHT_EPHEMERIS=tudat_merged`` loads the old order, with Tudat's merged kernel (DE438, ends 2040)."""
    global _KERNELS_LOADED
    P = ephemeris_provider()
    name = P.name if P is not None else "kernels"
    if _KERNELS_LOADED == name:
        return
    from tudatpy.interface import spice
    if _KERNELS_LOADED is not None:
        spice.clear_kernels()
    SPICE_CENTRES.clear()
    if P is not None:
        P().load()
        SPICE_CENTRES.update(getattr(P, "centres", {}) or {})
    else:
        kdir = os.environ.get("MARSFLIGHT_KERNELS", os.path.expanduser("~/.tudat/resource/spice_kernels"))
        for k in KERNELS:
            spice.load_kernel(os.path.join(kdir, k))
    _KERNELS_LOADED = name


def spice_name(body):
    """The SPICE name to ask for a body's position: its barycentre when the kernels lack its centre."""
    return SPICE_CENTRES.get(body, body)


class World:
    """Tudat system of bodies for one flight, with the dispersions of one seed."""

    def __init__(self, disp: Dispersions, epoch0: float):
        from tudatpy.interface import spice
        from tudatpy.dynamics import environment_setup as es

        self.disp = disp
        self.epoch0 = epoch0
        self._mars_alt_km = 0.0   # height of the latest Mars atmosphere evaluation (see _mars_wind)
        load_kernels()
        self.tmp = tempfile.mkdtemp(prefix="marsflight_")
        table = os.path.join(self.tmp, "mars_atm.dat")
        write_mars_table(table, disp)
        names = ["Sun", "Venus", "Earth", "Moon", "Mars", "Jupiter"]
        bs = es.get_default_body_settings(names, "SSB", "J2000")
        for body, sp in SPICE_CENTRES.items():   # DE440 has no centre for these: fly the system barycentre
            bs.get(body).ephemeris_settings = es.ephemeris.direct_spice("SSB", "J2000", sp)
        # Earth
        bs.get("Earth").shape_settings = es.shape.oblate_spherical(6378137.0, 1.0 / 298.257223563)
        # Earth's atmosphere and winds come from the active providers of the roles atmosphere.earth and
        # winds.earth (core/registry.py). The original providers (earth_nrlmsise00, earth_jet) keep the
        # original code below, so selecting them flies exactly as before.
        from .core.model import FlightContext
        from .core.registry import registry
        ctx = FlightContext(seed=disp.seed, design={}, vehicle=None, world=disp, truth={},
                            data_dir=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
        ctx.epoch0 = epoch0   # the Mars providers count sols and seasons from the launch epoch
        self.ctx = ctx
        A, W = registry.provider("atmosphere.earth"), registry.provider("winds.earth")
        self.earth_atm = A() if A is not None and A.name != "earth_nrlmsise00" else None
        self.earth_wind_model = W() if W is not None and W.name != "earth_jet" else None
        for p in (self.earth_atm, self.earth_wind_model):
            if p is not None:
                p.configure(ctx)
        if self.earth_atm is not None:
            bs.get("Earth").atmosphere_settings = self.earth_atm.tudat_settings(epoch0)
        else:
            space_weather = os.path.join(self.tmp, "space_weather.txt")
            write_space_weather(space_weather, disp.seed)
            earth_atm = es.atmosphere.nrlmsise00(space_weather)
            bs.get("Earth").atmosphere_settings = es.atmosphere.scaled_by_function(earth_atm, self._earth_density_scale)
        bs.get("Earth").atmosphere_settings.wind_settings = es.atmosphere.custom_wind_model(self._earth_wind)
        # Mars: gravity, dust, atmosphere and terrain from the active providers of gravity.mars, dust.mars,
        # atmosphere.mars and terrain.mars. Selecting jgmro120d, storm_draw and jezero_50m flies exactly as
        # before the area models were wired in (the mars_table atmosphere is the same table either way).
        G = registry.provider("gravity.mars")
        self.mars_gravity = G() if G is not None else None
        self.mars_gravity_degree = self.mars_gravity.acceleration_degree() if self.mars_gravity is not None else (12, 12)
        if self.mars_gravity is not None and G.name != "jgmro120d":
            bs.get("Mars").gravity_field_settings = self.mars_gravity.tudat_settings()
        else:
            bs.get("Mars").gravity_field_settings = es.gravity_field.predefined_spherical_harmonic(
                es.gravity_field.PredefinedSphericalHarmonicsModel.jgmro120d, 12)
        Dst = registry.provider("dust.mars")
        self.mars_dust = Dst() if Dst is not None and Dst.name != "storm_draw" else None
        if self.mars_dust is not None:
            self.mars_dust.configure(ctx)
        self._mars_lat = self._mars_lon = None   # where the latest Mars atmosphere evaluation was (deg)
        T = registry.provider("terrain.mars")
        self.terrain_mars = T() if T is not None else None
        bs.get("Mars").shape_settings = es.shape.spherical(MARS_REF_RADIUS)
        AM = registry.provider("atmosphere.mars")
        if AM is not None and AM.name != "mars_table":
            # another atmosphere (MCD, Mars-GRAM): its own Tudat settings with the dust provider
            self.mars_atm = AM()
            self.mars_atm.configure(ctx, dust=self.mars_dust)
            bs.get("Mars").atmosphere_settings = self.mars_atm.tudat_settings(self._mars_density_scale_provider)
        else:
            self.mars_atm = None
            mars_atm = es.atmosphere.tabulated(table, specific_gas_constant=MARS_R_GAS, ratio_of_specific_heats=MARS_GAMMA)
            bs.get("Mars").atmosphere_settings = es.atmosphere.scaled_by_function(mars_atm, self._mars_density_scale)
        bs.get("Mars").atmosphere_settings.wind_settings = es.atmosphere.custom_wind_model(self._mars_wind)
        self.body_settings = bs
        self.bodies = es.create_system_of_bodies(bs)

    # -- atmosphere scaling and winds --------------------------------------------------------
    def _earth_density_scale(self, t):
        atm = getattr(self, "earth_atm", None)
        return atm.scale(t) if atm is not None else self.disp.earth_density

    def mars_sol(self, t):
        return (t - self.epoch0) / 88775.244

    def mars_tau(self, t, lat=None, lon=None):
        """Column dust optical depth at (lat, lon) in degrees, Jezero by default, at TDB time t: the active
        dust.mars provider (Montabone's observed Mars years by default), or the world's storm draw."""
        if self.mars_dust is None:
            return self.disp.mars_tau(self.mars_sol(t))
        if lat is None or lon is None:
            lat, lon = D.SITES["mars_landing"]["lat"], D.SITES["mars_landing"]["lon"]
        return self.mars_dust.optical_depth(lat, lon, t)

    def _mars_density_scale(self, t):
        f = mars_seasonal_pressure_factor(mars_ls(t))
        tau = self.mars_tau(t, self._mars_lat, self._mars_lon) if self.mars_dust is not None \
            else self.disp.mars_tau(self.mars_sol(t))
        if tau > 0.5:
            f *= dust_density_factor(tau, self._mars_alt_km)
        return f

    def _mars_density_scale_provider(self, t):
        lat = self._mars_lat if self._mars_lat is not None else D.SITES["mars_landing"]["lat"]
        lon = self._mars_lon if self._mars_lon is not None else D.SITES["mars_landing"]["lon"]
        return self.mars_atm.scale(self._mars_alt_km * 1e3, lat, lon, t)

    def _earth_wind(self, alt, lon, lat, t):
        # Angles in radians, as Tudat passes them. Tudat calls the wind model before it evaluates the density,
        # so an atmosphere provider that depends on position notes it here.
        atm = getattr(self, "earth_atm", None)
        if atm is not None:
            atm.note_position(alt, lat, lon)
        model = getattr(self, "earth_wind_model", None)
        if model is not None:
            return model.tudat_wind(alt, lon, lat, t)
        d = self.disp
        s = d.earth_jet_speed * math.exp(-((alt - 12000.0) / 5000.0) ** 2) + 5.0 * min(1.0, alt / 1000.0)
        if alt > 60000:
            s = 0.0
        hd = math.radians(d.earth_jet_heading)
        return np.array([[s * math.cos(hd)], [s * math.sin(hd)], [0.0]])

    def _mars_wind(self, alt, lon, lat, t):
        # Tudat passes the density scale only the time. Each update of the flight conditions calls this wind
        # model at the current altitude before it evaluates the density, so the dust factor takes the height here.
        if math.isfinite(alt):
            self._mars_alt_km = alt / 1e3
        if getattr(self, "mars_dust", None) is not None and math.isfinite(lat) and math.isfinite(lon):
            self._mars_lat, self._mars_lon = math.degrees(lat), math.degrees(lon)
        d = self.disp
        h = alt - D.SITES["mars_landing"]["alt"]
        s = d.mars_wind_speed * min(1.0, max(0.0, h, 0.0) / 2000.0 + 0.3) if h < 40000 else 0.0
        hd = math.radians(d.mars_wind_heading)
        return np.array([[s * math.cos(hd)], [s * math.sin(hd)], [0.0]])
