"""The public Mars data the flight software needs, kept in flight/ because the verifier does not let flight
software import the simulator (and the flight isolation that followed). These are the published models
the simulator also uses: the Mars datum and gas constants, the mean temperature profile fitted to flown
entries, Allison and McEwen's solar longitude, Tillman's seasonal pressure cycle, the density change in a dust
storm, and the SPICE kernels that ship with Tudat."""
from __future__ import annotations

import math
import os

MARS_R_GAS = 191.8
MARS_GAMMA = 1.30
MARS_P0 = 549.9          # Pa at the 3389.5 km datum, annual mean
MARS_REF_RADIUS = 3389.5e3
# Mean temperature profile (altitude km, K) fitted to the MSL, Pathfinder, Spirit and Opportunity entries.
MARS_T_PROFILE = [(-10, 237.0), (0, 221.9), (10, 207.1), (20, 193.7), (30, 181.2), (40, 169.4), (50, 156.9),
                  (60, 143.4), (80, 132.3), (100, 129.4), (110, 121.9), (120, 115.6), (140, 140), (160, 175),
                  (200, 210), (300, 215)]
KERNELS = ["naif0012.tls", "pck00010.tpc", "inpop19a_TDB_m100_p100_spice.tpc", "NOE-4-2020.tpc",
           "de440.bsp", "tudat_merged_spk_kernel.bsp"]
_KERNELS_LOADED = False


def mars_ls(t_tdb: float) -> float:
    """Areocentric solar longitude (deg), Allison & McEwen (2000) without planetary perturbations."""
    dt = t_tdb / 86400.0
    M = math.radians(19.3871 + 0.52402073 * dt)
    alpha = 270.3871 + 0.524038496 * dt
    eoc = (10.691 + 3.0e-7 * dt) * math.sin(M) + 0.623 * math.sin(2 * M) + 0.050 * math.sin(3 * M) \
        + 0.005 * math.sin(4 * M) + 0.0005 * math.sin(5 * M)
    return (alpha + eoc) % 360.0


def mars_seasonal_pressure_factor(ls_deg: float) -> float:
    """Tillman's Viking Lander 1 annual pressure cycle, refitted in Ls."""
    ls = math.radians(ls_deg)
    return (1.0 + 0.0879 * math.cos(ls - math.radians(309.8)) + 0.0718 * math.cos(2 * (ls - math.radians(66.7)))
            + 0.0035 * math.cos(3 * (ls - math.radians(24.9))))


def mars_temperature(h_km: float) -> float:
    pts = MARS_T_PROFILE
    if h_km <= pts[0][0]:
        return pts[0][1]
    for (a, ta), (b, tb) in zip(pts, pts[1:]):
        if a <= h_km <= b:
            return ta + (tb - ta) * (h_km - a) / (b - a)
    return pts[-1][1]


def load_kernels():
    """JPL DE440 plus Tudat's merged planet-centre kernel, the IAU PCK and the leap seconds, from Tudat's data."""
    global _KERNELS_LOADED
    if _KERNELS_LOADED:
        return
    from tudatpy.interface import spice
    kdir = os.environ.get("MARSFLIGHT_KERNELS", os.path.expanduser("~/.tudat/resource/spice_kernels"))
    for k in KERNELS:
        spice.load_kernel(os.path.join(kdir, k))
    _KERNELS_LOADED = True


# How a dust storm of optical depth tau changes the density at each height: log density slope per unit of
# saturated optical depth (the manual's dust model; the same numbers the simulator uses).
DUST_LOG_DENSITY_SLOPE = [(0, -0.15), (12, -0.15), (27, 0.27), (50, 1.05), (70, 1.6), (90, 1.65)]
DUST_TAU_SAT = 0.8


def _interp_table(table, x):
    if x <= table[0][0]:
        return table[0][1]
    for (a, fa), (b, fb) in zip(table, table[1:]):
        if a <= x <= b:
            return fa + (fb - fa) * (x - a) / (b - a)
    return table[-1][1]


def dust_density_factor(tau: float, h_km: float) -> float:
    """Density at h_km in dust of optical depth tau, over the density at the background optical depth 0.5."""
    x = tau - 0.5
    if x <= 0:
        return 1.0
    return math.exp(_interp_table(DUST_LOG_DENSITY_SLOPE, h_km) * DUST_TAU_SAT * (1.0 - math.exp(-x / DUST_TAU_SAT)))
