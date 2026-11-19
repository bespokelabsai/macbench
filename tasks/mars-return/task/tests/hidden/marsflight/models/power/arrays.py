"""Power sources: RTGs and solar arrays, in space and on the Mars surface, each fitted to flight data.

- **RTGs** lose power as Curiosity's MMRTG did on Mars: plutonium-238 decay times a thermocouple loss that levels off
  over the years. Fitted to 2,809 daily means of Curiosity's MMRTG (Whiting and Woerner 2023, Zenodo
  10.5281/zenodo.7342671, CC0): within 0.25% rms over 7.7 years.
- **Arrays on the Mars surface** see the Sun at its real distance, the site's latitude, season and hour, the dust in
  the air (optical depth tau) and the dust that settles on the arrays, faster in a storm. Fitted to InSight's array
  energy (5% rms) and Opportunity's 2018 storm. Basilisk's ``simpleSolarPanel`` gives the same output hour by hour
  (``models/power/basilisk.py``); this file gives the sol's mean and the hour-by-hour profile in closed form, which is
  what the battery needs.
- **Arrays in space** give the flux at their distance from the Sun times the cosine of the Sun's angle from the array
  normal (Basilisk ``simpleSolarPanel``'s rule), less a small ageing a year. Checked against Mars Odyssey (1,500 W
  near Earth, 750 W at Mars: within 5%). A correction for the cells' temperature (colder cells are more efficient,
  Spectrolab's UTJ datasheet) is available but off: with it Odyssey's ratio is 18% too high, so the flown arrays'
  low-light and ageing losses at Mars cancel the cold gain at least as well as the plain rule assumes
  (results/issue-73/README.md). v3's rule took 0.9 of the flux at all times; now a coasting craft keeps its arrays
  on the Sun and loses output only in shadow (``eclipse.py``) and when a burn points them away.
- **Pointing**: in a coast the arrays track the Sun; in a burn the vehicle points its thrust, and body-fixed arrays
  facing along the long axis (as on the MSL and MER cruise stages) see the cosine of the angle between the thrust axis
  and the Sun, or nothing when the Sun is behind them.

The surface hour-by-hour helpers (``solar_by_hour``, ``night_deficit_Wh``) are used by the battery and ISRU code
(``models/power/eclipse.py`` and ``models/isru/moxie.py``).
"""
from __future__ import annotations

import math

from ...core.model import Model

SOL_S = 88775.244
SOL_H = SOL_S / 3600.0
YEAR_S = 365.25 * 86400.0
SOLAR_CONSTANT = 1361.0            # W/m^2 at 1 AU (Kopp and Lean 2011)
SIGMA = 5.670374419e-8
MARS_OBLIQUITY_DEG = 25.19         # Allison and McEwen (2000)
STEPS = 48                                                          # steps of a sol for its power and heat
HOURS = [(i + 0.5) * 24.0 / STEPS for i in range(STEPS)]            # local true solar time of each step (h)

# Cell efficiency x packing in space at 28 C (data.SOLAR_EFFICIENCY, unchanged).
SOLAR_EFFICIENCY = 0.28

# ---- RTG decay ----------------------------------------------------------------------------------------------------
# Fitted to Curiosity's MMRTG on Mars: 2,809 daily means over 7.7 years from landing (C. E. Whiting and D. F. Woerner,
# "Historical RTG performance data through 2020", Zenodo 10.5281/zenodo.7342671, CC0; JPL mission control telemetry).
# Pu-238 decays with its 87.7 year half-life, and the thermocouples lose a further 30.6% that levels off with a
# 7.0 year time constant. Within 0.25% rms of the record (largest error 1.3%). The record starts at landing, so the
# law runs from launch here. NASA and DOE give 4.8% a year as representative (Zakrajsek et al., LPSC 2016, NTRS
# 20170000017); JPL's own fit to the first years was 4.62% a year (Wood et al., NETS 2016, paper 6024).
RTG_POWER_W = 110.0                # per unit at launch (NASA MMRTG fact sheet NF-2020-05-619-HQ; data.RTG)
RTG_DECAY = {"half_life_years": 87.7, "couple_loss": 0.306, "couple_years": 7.0}
RTG_WASTE_HEAT_W = 2000.0          # thermal output per MMRTG at launch (Bhandari et al., ICES 2005, SAE 2005-01-2828)

# ---- Arrays on the Mars surface ------------------------------------------------------------------
# Suspended dust passes exp(-0.35 tau) of a sol's sunlight. Settled dust leaves exp(-dust) of the output; dust grows
# 0.18% a sol, and 0.4% a sol more per unit of tau above 1.5. On Mars an array gives 0.875 of its rating in space
# (reddened light, low sun). Fitted to InSight's array energy: 4,588 Wh on sol 1 (JPL, 30 Nov 2018) and 21 values
# from NASA's InSight blog, June to December 2022, at tau 0.8 to 1.5, within 5% rms (largest 10%). The tau term
# matches Opportunity's 2018 storm, tau 0.6 to 10.8 (Staab et al. 2020, Table 1), and the storm term Opportunity's
# 1.2% a sol at tau 3 to 4.7 in the 2007 storm (Lorenz et al. 2021, Planetary and Space Science 207, 105337).
SOLAR_TAU_K = 0.35
SOLAR_DUST_LOSS_PER_SOL = 0.0018
SOLAR_STORM_TAU = 1.5
SOLAR_STORM_DUST_PER_TAU = 0.004
MARS_SURFACE_ARRAY_FACTOR = 0.875

# ---- Arrays in space ------------------------------------------------------------------------------------------------
# Spectrolab UTJ cell datasheet (28.3% UTJ, rev. 10/5/2010): at 28 C and AM0, Vmp 2.350 V falling 6.5 mV/C and Jmp
# 16.30 mA/cm^2 rising 1.2 uA/cm^2/C (BOL, 15 to 80 C), so maximum power changes by -0.27% per kelvin. Cover glass
# solar absorptance 0.92 and normal emittance 0.85 (same sheet); the panel's back is taken at emittance 0.85 too.
CELL_T_REF_K = 301.15
CELL_POWER_PER_K = -0.0027
CELL_ABSORPTANCE = 0.92
PANEL_EMITTANCE_TWO_SIDES = 1.70
# Ageing of arrays in space, a fraction a year: 1-MeV-electron-equivalent fluence of an interplanetary cruise is small
# beside an Earth orbit's; Tianwen-1's in-orbit analysis at Mars (Yang et al. 2023, Solar RRL, doi:10.1002/
# solr.202300001) gives a yearly factor of 0.998 for its standard case. Treated as a nominal value (the full paper was
# not read, results/issue-73/README.md).
ARRAY_AGEING_PER_YEAR = 0.002


def rtg_power_W(units: int, years: float) -> float:
    """Electrical power of `units` MMRTG-class RTGs `years` after launch."""
    if not units:
        return 0.0
    r = RTG_DECAY
    y = max(0.0, years)
    fuel = 0.5 ** (y / r["half_life_years"])
    couples = 1.0 - r["couple_loss"] * (1.0 - math.exp(-y / r["couple_years"]))
    return units * RTG_POWER_W * fuel * couples


# ---- surface ---------------------------------------------------------------------------------------------------------
def solar_declination_deg(ls_deg: float) -> float:
    """The Sun's declination seen from Mars at solar longitude Ls."""
    return math.degrees(math.asin(math.sin(math.radians(MARS_OBLIQUITY_DEG)) * math.sin(math.radians(ls_deg))))


def _sunset_hour_angle(lat_deg: float, dec_deg: float) -> float:
    x = -math.tan(math.radians(lat_deg)) * math.tan(math.radians(dec_deg))
    return math.acos(max(-1.0, min(1.0, x)))


def mean_cos_zenith(lat_deg: float, ls_deg: float) -> float:
    """Mean over a sol of the cosine of the Sun's zenith angle, the night counted as zero."""
    dec = solar_declination_deg(ls_deg)
    h0 = _sunset_hour_angle(lat_deg, dec)
    p, d = math.radians(lat_deg), math.radians(dec)
    return max(0.0, (h0 * math.sin(p) * math.sin(d) + math.cos(p) * math.cos(d) * math.sin(h0)) / math.pi)


def dust_transmission(tau: float) -> float:
    """Fraction of a sol's sunlight that reaches level ground through dust of optical depth tau."""
    return math.exp(-SOLAR_TAU_K * max(0.0, tau))


def dust_settling_per_sol(tau: float) -> float:
    """How fast settling dust dims the arrays (per sol) under a sky of optical depth tau."""
    return SOLAR_DUST_LOSS_PER_SOL + SOLAR_STORM_DUST_PER_TAU * max(0.0, tau - SOLAR_STORM_TAU)


def array_dust_factor(dust: float) -> float:
    """Output of a dusty array as a fraction of a clean one, for `dust` gathered so far."""
    return math.exp(-max(0.0, dust))


def surface_solar(lat_deg: float, ls_deg: float, r_au: float, tau: float, dust: float) -> dict:
    """Output of a level array on the Mars surface per m^2 (W/m^2): the mean over a sol, and the output with the Sun
    overhead, which the hour-by-hour output scales with the cosine of the zenith angle."""
    per_m2 = (SOLAR_CONSTANT / r_au ** 2 * dust_transmission(tau) * array_dust_factor(dust)
              * SOLAR_EFFICIENCY * MARS_SURFACE_ARRAY_FACTOR)
    return {"mean": per_m2 * mean_cos_zenith(lat_deg, ls_deg), "per_m2_normal": per_m2}


def solar_by_hour(normal_W: float, lat_deg: float, ls_deg: float) -> list:
    """Array output (W) at each step of a sol (HOURS): normal_W times the cosine of the Sun's zenith angle."""
    dec = solar_declination_deg(ls_deg)
    p, d = math.radians(lat_deg), math.radians(dec)
    a, b = math.sin(p) * math.sin(d), math.cos(p) * math.cos(d)
    return [normal_W * max(0.0, a + b * math.cos(math.radians(15.0 * (h - 12.0)))) for h in HOURS]


def night_deficit_Wh(load_W, normal_W: float, lat_deg: float, ls_deg: float) -> float:
    """Energy (Wh) the batteries must give over one sol: the load the arrays do not cover at each step. load_W is one
    load for the whole sol or a list with one for each step of HOURS."""
    loads = load_W if isinstance(load_W, (list, tuple)) else [load_W] * STEPS
    if max(loads) <= 0:
        return 0.0
    solar = solar_by_hour(normal_W, lat_deg, ls_deg) if normal_W > 0 else [0.0] * STEPS
    return sum(max(0.0, w - s) for w, s in zip(loads, solar)) / STEPS * SOL_H


# ---- space -----------------------------------------------------------------------------------------------------------
def cell_temperature_K(r_au: float, cos_sun: float) -> float:
    """Equilibrium temperature of a thin array lit at cos_sun, radiating from both faces, with the electrical output
    taken out of the absorbed heat."""
    q = SOLAR_CONSTANT / r_au ** 2 * max(0.0, cos_sun) * (CELL_ABSORPTANCE - SOLAR_EFFICIENCY)
    return max(3.0, (q / (PANEL_EMITTANCE_TWO_SIDES * SIGMA)) ** 0.25) if q > 0 else 3.0


def space_array_W(area_m2: float, r_au: float, cos_sun: float = 1.0, years: float = 0.0, temperature=False) -> float:
    """Output (W) of an array in space at r_au, lit at cos_sun (the cosine of the Sun's angle from the array normal),
    `years` after launch. temperature=True adds the cell temperature correction (off by default, see above)."""
    if area_m2 <= 0 or cos_sun <= 0:
        return 0.0
    p = area_m2 * SOLAR_EFFICIENCY * SOLAR_CONSTANT / r_au ** 2 * cos_sun
    if temperature:
        p *= max(0.0, 1.0 + CELL_POWER_PER_K * (cell_temperature_K(r_au, cos_sun) - CELL_T_REF_K))
    return p * (1.0 - ARRAY_AGEING_PER_YEAR) ** max(0.0, years)


def v3_space_array_W(area_m2: float, r_au: float) -> float:
    """sim.py's rule on v3 before this change: 0.9 of the flux at all times."""
    return area_m2 * SOLAR_EFFICIENCY * SOLAR_CONSTANT / r_au ** 2 * 0.9


def pointing_cos(thrusting: bool, thrust_axis=None, sun_dir=None) -> float:
    """Cosine of the Sun's angle from body-fixed arrays that face along the long axis. In a coast the vehicle keeps the
    arrays on the Sun (1); in a burn it points the thrust axis, so the arrays see the Sun at the angle between that
    axis and the Sun, or not at all when the Sun is behind them."""
    if not thrusting or thrust_axis is None or sun_dir is None:
        return 1.0
    n = math.sqrt(sum(a * a for a in thrust_axis)) * math.sqrt(sum(s * s for s in sun_dir))
    if n <= 0:
        return 1.0
    return max(0.0, sum(a * s for a, s in zip(thrust_axis, sun_dir)) / n)


class PowerSources(Model):
    """RTG decay and the array output rules, for the core to call from the power balance (sim.py ``resources``).
    Holds no state; the battery and eclipse energy balance is ``models/power/eclipse.py``."""
    name = "power_sources"
    area = "power"
    order = 55

    def configure(self, ctx):
        self.ctx = ctx

    def design_fields(self):
        return ["elements[].solar_array_m2", "elements[].rtg_units"]

    @staticmethod
    def rtg_W(units, years):
        return rtg_power_W(units, years)

    @staticmethod
    def space_W(area_m2, r_au, cos_sun=1.0, years=0.0):
        return space_array_W(area_m2, r_au, cos_sun, years)

    @staticmethod
    def surface(lat_deg, ls_deg, r_au, tau, dust):
        return surface_solar(lat_deg, ls_deg, r_au, tau, dust)


MODELS = [PowerSources]
