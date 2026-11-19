"""Material behavior in structures (ported at 1bd7100): bending and transverse
shear in shell walls (with the internal pressure credit in shear that the earlier draft lacked), cryogenic contraction, the
cold soak of a bonded heat shield, crack growth in tanks that wait on Mars, and the clearance of nozzles leaving an
interstage. models/structures/loads.py calls them; nothing here draws a random number, so the hidden spread of
built hardware comes in from the caller.

Factors of safety are NASA-STD-5001B (with Change 3, 2022): Table 1 for metallic structure (ultimate 1.4),
Table 2 for bonded joints (2.0 at a discontinuity, prototype approach), section 4.4 (FSR 51) for fatigue and
crack growth (service life factor 4.0), and Table 1's 1.05 acceptance proof for every propellant tank.

Switch: MARSFLIGHT_MATERIALS=0 turns every check here off and flies the axial-only buckling check of v3.
"""
import math
import os

from ... import data as D

FS_ULTIMATE_METAL = 1.4        # 5001B Table 1
FS_BOND_DISCONTINUITY = 2.0    # 5001B Table 2, prototype, discontinuity area
SERVICE_LIFE_FACTOR = 4.0      # 5001B 4.4, FSR 51
ACCEPTANCE_PROOF = 1.05        # 5001B Table 1, propellant tanks


def enabled():
    return os.environ.get("MARSFLIGHT_MATERIALS", "1") != "0"


# --- Thermal contraction --------------------------------------------------------------------------------
# NIST cryogenic material properties (trc.nist.gov/cryogenics/materials): (L - L293)/L293 x 1e5 as a
# polynomial in T (K). 6061-T6 stands for the aluminum alloys (no open fit exists for 2219 or 2195; ET-137
# in validate.py is 2195), 304 stainless for 301. Valid 4 to 300 K (6061, 304) and 24 to 300 K (Ti-6Al-4V).
# CFRP has none here: a quasi-isotropic carbon laminate barely contracts, and it is left at zero.
NIST_CONTRACTION = {
    "Al2219": (-412.77, -0.30389, 8.7696e-3, -9.9821e-6, 0.0),
    "Al2195": (-412.77, -0.30389, 8.7696e-3, -9.9821e-6, 0.0),
    "SS301": (-295.54, -0.39811, 9.2683e-3, -2.0261e-5, 1.7127e-8),
    "Ti6Al4V": (-171.1, -0.2140, 4.807e-3, -7.111e-6, 0.0),
}


def contraction(material, T):
    """Linear thermal strain (negative when cold) from 293 K to T, for a structural material."""
    c = NIST_CONTRACTION.get(material)
    if c is None:
        return 0.0
    T = min(max(T, 4.0), 293.0)
    return sum(k * T ** i for i, k in enumerate(c)) * 1e-5 - sum(k * 293.0 ** i for i, k in enumerate(c)) * 1e-5


def cold_volume_factor(material, T):
    """What a tank drawn at room temperature holds when its wall is at T: (1 + eps)^3."""
    return (1.0 + contraction(material, T)) ** 3


# --- Shell walls: axial, bending and transverse shear ---------------------------------------------------
# NASA SP-8007 (1968; Rev. 2, 2020, NTRS 20205011530, keeps the same equations): axial gamma (eq. 9 left),
# bending gamma (eq. 9 right), torsion (1968 eq. 26 and 27b; Rev. 2 eq. 34 and 36), the pressure gain dgamma
# (Figure 6, Rev. 2 Figure 4-5) for axial load and bending, and the linear interaction of eq. 32 (Rev. 2 section
# 4.1.1.5, which calls it conservative for bending plus torsion). Transverse shear buckles at 1.25 times the
# torsion stress (Lundquist, NACA TN 523, eq. 5); its shear failures reach 0.90 to 1.00 of eq. 26 with gamma = 1,
# so the shear term uses 0.90, the lowest of them.
SHEAR_GAMMA34 = 0.90
# Internal pressure in shear (new in this port; the earlier draft had none). Neither SP-8007 edition gives a pressure term for
# torsion. The test source is Crate, Batdorf and Baab, NACA Wartime Report L-67 (ARR L4E27, 1944, NTRS
# 19930090928, conclusion p. 7, appendix A): (tau/tau0)^2 + p/p_cr = 1, with p_cr the buckling pressure under
# external pressure alone (negative for internal pressure), so tau = tau0 sqrt(1 + p/p_cr). Gerard and Becker
# repeat it (NACA TN 3783, 1957, eq. 53, p. 44). p_cr is the classical one (gamma = 1): SP-8007 Rev. 2 eq. 25
# (moderate length) or eq. 27 (long), whichever is larger. A knocked-down p_cr would give more credit, so the
# classical value is the safe side. Against the L-67 tests (24S-T, r/t 469, L/r 0.36 to 2.87, up to pbar 0.15)
# the rule is within +15% and -4% (dev/test_structures_wiring.py). The tests stop at pbar = 0.15, so the credit is capped at its value there:
# outside the tested range it would reach ten times the unpressurized stress on a thin tank.
SHEAR_PBAR_CAP = 0.15
# NACA WR L-67 Fig. 5, read from the scanned figure (r = 15 in, t = 0.032 in): (L/r, pbar, tau/tau0 measured).
L67_TESTS = [(2.87, 0.075, 2.13), (2.87, 0.149, 3.04), (1.43, 0.149, 2.33), (0.72, 0.149, 1.95), (0.36, 0.099, 1.36)]


def _dgamma(p, E, R, t):
    pbar = p / E * (R / t) ** 2
    return 1.194 * pbar ** 0.945 / (1 + 4.363 * pbar ** 0.945) if pbar > 0 else 0.0


def torsion_stress(R, t, L, E, gamma34=0.67, nu=D.POISSON):
    """SP-8007 eq. 26 (moderate length) or 27b (long), whichever applies, in Pa."""
    Z = L * L / (R * t) * math.sqrt(1 - nu * nu)
    if Z <= 78 * (R / t) ** 2 * (1 - nu * nu):
        return 0.747 * gamma34 * E / ((R / t) ** 1.25 * (L / R) ** 0.5)
    return gamma34 ** (4 / 3) * E / (3 * math.sqrt(2) * (1 - nu * nu) ** 0.75) * (t / R) ** 1.5


def external_buckling_pressure(R, t, L, E, nu=D.POISSON):
    """Classical buckling pressure (Pa) of a cylinder under external pressure: SP-8007 Rev. 2 eq. 25 (moderate
    length, 0.926 E (t/R)^2.5 R/L) or eq. 27 (long, E (t/R)^3 / (4 (1 - nu^2))), the larger."""
    return max(0.926 * E * (t / R) ** 2.5 * (R / L), E * (t / R) ** 3 / (4 * (1 - nu * nu)))


def shear_pressure_factor(p, R, t, L, E):
    """tau(p) / tau(0) for internal pressure p (NACA WR L-67), with pbar capped at the tested 0.15."""
    if p <= 0:
        return 1.0
    p_eff = min(p, SHEAR_PBAR_CAP * E * (t / R) ** 2)
    return math.sqrt(1.0 + p_eff / external_buckling_pressure(R, t, L, E))


def shear_allowable(R, t, L, mat, p, knockdown=True, r_tank=None):
    """Transverse shear stress (Pa) an unstiffened cylinder holds: 1.25 x torsion (TN 523) times the pressure
    factor, and never more than von Mises yield allows next to the hoop stress p r / t (r the tank's own radius,
    which is smaller than R for a tank of two domes)."""
    E, sy = mat["E"], mat["sy"]
    L = max(L, 0.1 * R)
    tau = 1.25 * torsion_stress(R, t, L, E, SHEAR_GAMMA34 if knockdown else 1.0) * shear_pressure_factor(p, R, t, L, E)
    hoop = max(0.0, p) * (R if r_tank is None else r_tank) / t
    return min(tau, math.sqrt(max(sy * sy - hoop * hoop, 0.0) / 3.0))


def interaction(ra, rb, rs):
    """Axial and bending ratios add linearly (SP-8007 eq. 32); shear enters squared, R_c + R_s^2 = 1, the
    interaction for cylinders in compression and torsion (Gerard and Becker, NACA TN 3783, 1957; Bruhn, Analysis
    and Design of Flight Vehicle Structures, C8). SP-8007 Rev. 2 section 4.1.1.5 calls the all-linear sum
    conservative for compression or bending with torsion. The wall buckles when this passes 1."""
    return ra + rb + rs * rs


def shell_ratios(R, t, L, mat, p, N, M, V, sf, knockdown=True, r_tank=None):
    """Load ratios of an unstiffened cylinder (R, wall t, length L between supports, internal pressure p)
    under axial compression N, bending moment M and transverse shear V, each times the factor sf. The
    pressure's end load relieves the axial load first and what is left of it the bending. knockdown=False
    is the classical shell (gamma = 1). Returns (axial, bending, shear); interaction() of them must stay below 1."""
    E, sy = mat["E"], mat["sy"]
    phi = math.sqrt(R / t) / 16
    ga = 1 - 0.901 * (1 - math.exp(-phi)) if knockdown else 1.0
    gb = 1 - 0.731 * (1 - math.exp(-phi)) if knockdown else 1.0
    dg = _dgamma(p, E, R, t)
    s_ax = min((ga * 0.605 + dg) * E * t / R, sy)
    s_b = min((gb * 0.605 + dg) * E * t / R, sy)
    relief = p * math.pi * R * R
    n_net = sf * N - relief
    m_net = max(0.0, sf * M - max(0.0, -n_net) * R / 2)
    if V == 0:
        rs = 0.0
    else:
        tau = shear_allowable(R, t, L, mat, p, knockdown, r_tank)
        rs = sf * abs(V) / (math.pi * R * t * tau) if tau > 0 else float("inf")
    return (max(0.0, n_net) / (2 * math.pi * R * t * s_ax), m_net / (math.pi * R * R * t * s_b), rs)


# --- Bonded heat shield, cold soak ----------------------------------------------------------------------
# A thin ablator layer bonded to a stiffer structure takes the structure's contraction: its stress is
# E (alpha_s - alpha_a) dT / (1 - nu), valid below about 0.5 in of ablator (Dow, Langley, 1969). As a strain
# against the ablator's strain to failure it needs no ablator modulus. PICA: in-plane strain to failure 1.0
# to 1.2% (NTRS 20110015019). No open source gives PICA's thermal expansion; a carbon preform barely
# contracts, so the mismatch is taken as the whole contraction of the structure, an upper bound. The other
# ablators have no open strain to failure, and are not checked. The structure under the ablator is taken
# as aluminum honeycomb.
TPS_STRAIN_TO_FAILURE = {"PICA": (0.010, 0.012)}
TPS_POISSON = 0.25                 # Dow's upper value
BOND_CURE_T = 293.0                # stress free at room temperature
COLD_SOAK_T = 143.0                # -130 C, the MSL cold qualification limit (NPO-48500, winter cycles)
BOND_SUBSTRATE = "Al2219"


def bond_strain(tps, T=COLD_SOAK_T, substrate=BOND_SUBSTRATE):
    """The ablator's strain from the structure's contraction between the cure and T, times its factor of
    safety. None for an ablator with no open strain to failure."""
    if tps not in TPS_STRAIN_TO_FAILURE:
        return None
    mismatch = abs(contraction(substrate, T) - contraction(substrate, BOND_CURE_T))
    return FS_BOND_DISCONTINUITY * mismatch / (1 - TPS_POISSON)


# --- Crack growth ---------------------------------------------------------------------------------------
# Forman's law with NASA TN D-7377's constants for welded 2219-T87 (Table XI, unaged electron beam welds):
# dA/dN = CA dK^S / ((1 - R) K_IC - dK), A in nm per cycle, K in MPa sqrt(m). A semicircular surface flaw,
# dK = 1.12 dsigma sqrt(pi A / Q) with Q = 2.467 - 0.212 (sigma/sy)^2. The starting flaw is the smaller of
# NASA-STD-5009's standard penetrant flaw (depth 0.075 in, length 0.150 in) and the largest flaw the
# acceptance proof lets through (NASA CR-135036's proof test logic). Other metals use the same law; the
# report gives no constants for them.
FORMAN = {"S": 4.5, "KIC": 55.7, "CA": 0.213}     # MPa sqrt(m), nm/cycle per (MPa sqrt m)^(S-1)
NDE_FLAW_M = 0.075 * 0.0254
MARS_HOT_T = 313.0                 # +40 C, the MSL warm qualification limit (NPO-48500, summer cycles)


def _q(sigma, sy):
    return max(1.0, (math.pi / 2) ** 2 - 0.212 * (sigma / sy) ** 2)


def cycles_to_leak(t, sigma_max, R_ratio, sy, kic=FORMAN["KIC"], a0=None, S=FORMAN["S"], CA=FORMAN["CA"]):
    """Cycles for a surface flaw to grow through a wall t (m) at peak hoop stress sigma_max (Pa) and stress
    ratio R_ratio. Returns (cycles, starting depth, how it ends: 'leak' or 'burst')."""
    s = sigma_max / 1e6
    Q = _q(sigma_max, sy)
    a_proof = Q / math.pi * (kic / (1.12 * ACCEPTANCE_PROOF * s)) ** 2
    a = a_start = min(NDE_FLAW_M, a_proof) if a0 is None else a0
    n, steps = 0.0, 400
    if a >= t:
        return 0.0, a, "leak"
    da = (t - a) / steps
    for _ in range(steps):
        am = a + da / 2
        kmax = 1.12 * s * math.sqrt(math.pi * am / Q)
        if kmax >= kic:
            return n, a_start, "burst"
        dk = (1 - R_ratio) * kmax
        rate = CA * dk ** S / ((1 - R_ratio) * kic - dk) * 1e-9 if dk > 0 else 0.0
        if rate <= 0:
            return float("inf"), a_start, "leak"
        n += da / rate
        a += da
    return n, a_start, "leak"


def diurnal_ratio(keep_T):
    """Stress ratio of one sol for a heated tank held at keep_T at night: the ullage gas warms to the hot
    limit by day, so the pressure swings by the temperature ratio."""
    return min(1.0, keep_T / MARS_HOT_T)


# --- Separation: nozzles leaving an interstage ----------------------------------------------------------
# The stage below keeps the interstage. The upper stage's nozzle cluster must leave it without touching:
# the gap is the interstage radius less the cluster's outer radius, and while the nozzles travel out the
# upper stage turns at its tip-off rate. The relative acceleration is the Saturn V S-II second plane
# average (AS-502 flight evaluation report, MPR-SAT-FE-68-3, section 12: 4.98 m in 1.06 s). Tip-off rates
# up to 1 deg/s per axis (3 sigma) are the usual specification (Holemans, SSC06-IX-7, p. 5).
SEPARATION_ACCEL = 2 * 4.98 / 1.06 ** 2
TIPOFF_3SIGMA_DEG_S = 1.0
# Densest packing of n equal circles in a circle: container radius / circle radius (Graham, Lubachevsky
# et al., Discrete Math. 181, 1998, and E. Friedman's table). Above 20, sqrt(n / 0.8) fits the table.
PACKING = [1.0, 2.0, 2.1547, 2.4142, 2.7013, 3.0, 3.0, 3.3048, 3.6131, 3.8130, 3.9238, 4.0296, 4.2360,
           4.3284, 4.5213, 4.6154, 4.7920, 4.8637, 4.8637, 5.1223]


def cluster_radius(n, exit_diameter):
    if n <= 0:
        return 0.0
    k = PACKING[n - 1] if n <= len(PACKING) else math.sqrt(n / 0.8)
    return k * exit_diameter / 2


def nozzle_arm(stack):
    """Distance from the nozzle exits at the base of a stack to its center of mass. stack is (mass, length)
    of each element from the bottom up; fairings are left out by the caller."""
    z = mz = m = 0.0
    for mass, length in stack:
        mz += mass * (z + length / 2)
        m += mass
        z += length
    return mz / m if m else 0.0


def separation_clearance(R_interstage, n, exit_diameter, nozzle_length, arm, rate_deg_s):
    """Least gap (m) between the nozzle cluster and the interstage while the nozzles travel out:
    the static gap less the swing of the nozzle exits at the tip-off rate over the extraction time."""
    gap = R_interstage - cluster_radius(n, exit_diameter)
    t_x = math.sqrt(2 * max(nozzle_length, 0.0) / SEPARATION_ACCEL)
    return gap - math.radians(rate_deg_s) * t_x * arm, gap, t_x
