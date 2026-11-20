"""Liquid rocket engine model.

Thermochemistry comes from NASA CEA v3 (shifting equilibrium, infinite-area combustor). Real engines
fall short of CEA; the loss model below (c* efficiency, nozzle divergence, boundary layer and kinetic
losses, gas-generator exhaust, film cooling) is an engineering correlation fitted to five flown
engines. Feed-cycle power balance, regenerative cooling capacity, chamber-pressure envelopes, mass,
throttling and restart limits are checked here too, so an engine that could not be built is rejected
before it ever flies.
"""
from __future__ import annotations

import functools
import math
from dataclasses import dataclass, field

import numpy as np

from ... import data as D
from . import switches as SW
from . import engine_fit as FIT

_CEA = None


def _cea():
    global _CEA
    if _CEA is None:
        import cea  # NASA CEA v3
        cea.set_log_level(0) if hasattr(cea, "set_log_level") else None
        _CEA = cea
    return _CEA


@functools.lru_cache(maxsize=4096)
def cea_rocket(fuel: str, ox: str, of_ratio: float, pc_bar: float, eps: float, frozen: bool = False):
    """Ideal vacuum performance from CEA v3. Returns dict or raises ValueError. frozen: the composition
    freezes at the throat instead of shifting with equilibrium through the nozzle."""
    cea = _cea()
    pf, po = D.PROPELLANTS[fuel], D.PROPELLANTS[ox]
    names = [pf["cea"], po["cea"]]
    reac = cea.Mixture(names)
    prod = cea.Mixture(names, products_from_reactants=True)
    solver = cea.RocketSolver(prod, reactants=reac)
    sol = cea.RocketSolution(solver)
    w = reac.of_ratio_to_weights(np.array([0.0, 1.0]), np.array([1.0, 0.0]), float(of_ratio))
    hc = reac.calc_property(cea.ENTHALPY, w, np.array([pf["T"], po["T"]])) / cea.R
    solver.solve(sol, w, float(pc_bar), [10.0], supar=[float(eps)], iac=True, hc=hc, **({"n_frz": 2} if frozen else {}))
    if hasattr(sol, "converged") and not sol.converged:
        raise ValueError("CEA did not converge")
    isp_vac = float(sol.Isp_vacuum[-1]) / D.G0
    if not (50.0 < isp_vac < 600.0):
        raise ValueError(f"CEA returned an unphysical Isp {isp_vac:.1f} s")
    return {
        "isp_vac": isp_vac,
        "cstar": float(sol.c_star[-1]),
        "Tc": float(sol.T[0]),
        "pe_bar": float(sol.P[-1]),
        "gamma": float(sol.gamma_s[0]),
        "mw": float(sol.MW[0]),
    }


# ------------------------------------------------------------------ off-design operation
# How Isp moves away from the design point. Real nozzle flow lies between CEA's shifting equilibrium and a
# composition frozen at the throat. Their mean follows the J-2 on Apollo 11 (AS-506 flight evaluation report
# MPR-SAT-FE-69-9). The S-II's Isp rose about 1.2% when its mixture ratio fell from 5.5 to 4.34 (Figure 6-3, 4.13 to
# 4.18 kN s/kg), and the mean gives 1.3%; shifting equilibrium alone gives 0.5%. The S-IVB engine at 4.93 made 0.94%
# more Isp than the S-II engines at 5.56 (Tables 6-1 and 7-1), and the mean gives 0.95%. The same mean sets how Isp
# falls at low chamber pressure, that is at low throttle.
KINETIC_BLEND = 0.5
# Throttling. Chamber pressure follows thrust, and the CEA Isp above falls with it. Real injectors also mix worse at
# low flow, once their pressure drop falls: on top of CEA, Isp falls by L / (1 + exp((throttle - t0) / w)), a smooth
# step. Fitted to the Apollo lunar module descent engine's acceptance tests before throat erosion (TRW data in
# Casiano, Hulka and Yang, AIAA 2009-5135, Figure 27): 297.1 s at 14% of rated thrust, 297.4 s at 29%, 298.1 s at 38%,
# then 300.7 s at 48% and 304.5 s at 57%, and 304.4 s at 92.5%. The real curve is flat to about 40% and rises
# from 43% to 57%, which the step follows (results/issue-19/figures/throttle_fit.py): within 0.47% at every point,
# where a power law in (1 - throttle) was within 0.69%. Out of sample, the RL10A-1 lost about 3% of Isp at 33% of
# chamber pressure and a TR202 prediction 3.7% at 18.8% of thrust; the step gives 2.3% and 3.0%.
THROTTLE_LOSS = (0.011, 0.46, 0.01)   # L, t0, w
# Mixture ratio commands. An engine runs at its design mixture ratio or below it, down to 78% of it, the way the
# J-2's propellant utilization valve moved the AS-506 S-II engines from 5.5 to 4.3 (Figure 6-3); the RL10 on Centaur
# could shift 12% either side of 5.0 (NASA TN D-4848), 79% to 100% of its top ratio. The valve takes
# oxidizer flow away, and the fuel flow follows the mixture ratio to the power 0.42: on AS-506 the S-II's four
# engines fell from 4,093,107 N to 3,082,769 N with the shift to an average of 4.34 (section 6.3), at 1.2% more Isp,
# so their total flow fell to 74.4% (990 to 735 kg/s in Figure 6-3) and their fuel flow to 90.6%. The chamber
# pressure follows the flow through the fixed throat, and Isp comes from CEA at the new mixture ratio and pressure.
MIXTURE_RATIO_RANGE = (0.78, 1.0)
PU_FUEL_FLOW_EXP = 0.42
# Start and shutdown transients: thrust as a fraction of the commanded thrust, against seconds since the first
# thrust (start, which comes delay_s after the start command) or since the cutoff command (shutdown). Propellant flow
# follows thrust. Each cutoff's impulse is scaled by a draw of normal(1, sd), new for every cutoff in a dispersed
# world, with sd the scatter of the flown cutoffs. An engine's curves are set by two numbers, its build-up time
# (first thrust to full thrust) and its tail-off impulse (s of the thrust at cutoff). transient_profiles stretches
# the flown curve of its kind in time to give them. Each kind follows a flown engine, whose values are the defaults:
# - pump_fed (gas generator, staged combustion, full flow and electric pump cycles): the J-2 on the S-IVB, in the
#   Saturn V flight evaluation reports (https://ibiblio.org/apollo/Documents/lvfea-AS5NN-ApolloNN.pdf). The first
#   thrust comes 1.0 s after the engine start command, when the start tank discharge valve is commanded (AS-506 S-II:
#   163.0 s and 164.0 s, Table 2-2). The start curve is the mean of the digitized first-burn curves of Apollo 8
#   (AS-503 Figure 7-5) and Apollo 16 (AS-511 Figure 7-2); the S-IVB's first burns delivered 0.91 to 1.05 s of full
#   thrust (0.95 on average over nine flights) in the 2.5 s after the valve command. The shutdown curve is the mean
#   of the digitized cutoffs of Apollo 8 (Figures 7-11 and 7-24) and Apollo 16 (Figure 7-4), with a slightly fuller
#   low tail to zero thrust at 2.3 s (AS-501): the digitized curves fall 5% to 8% short of the printed impulses.
#   Twenty-four S-IVB cutoffs from Apollo 4 to 17 delivered 0.203 to 0.267 s of full thrust, 0.224 on average, and
#   scatter 6% about it; the curve gives 0.220.
# - expander: the RL10 on Atlas-Centaur AC-9 (NASA TM X-1647). 90% thrust 1.42 to 1.60 s after the start signal;
#   8,780 to 10,400 lbf s in the first 2 s at 15,000 lbf; 1,464 to 1,555 lbf s per engine at cutoff (three cutoffs,
#   AC-9 and AC-16), 3% apart.
# - pressure_fed: the Apollo service propulsion engine, AJ10-137 (Apollo 7 and 16 SPS evaluations, NTRS 19700025288
#   Table 6, which also gives Apollo 6's first firing, and 19740022190 Table 7). 90% thrust 0.41 to 0.66 s after the ignition signal, with 112 to 1,114 lbf s to 90% (about
#   400 on average); 10,550 to 14,464 lbf s from cutoff to zero thrust at 20,500 lbf (12,500 specified), with thrust
#   down to 10% 0.76 to 1.04 s after cutoff. Twelve cutoffs of Apollo 6, 7 and 16 scatter 9%.
# Chilldown: the propellant an engine loses at each start to cool its pumps and lines. In seconds of the engine's
# full flow, against the time since the engine was last cold (its last cutoff, or for its first start the liftoff,
# since upper stage pumps are chilled on the pad). Fitted to Centaur's RL10, which dumps its chill flow overboard
# (results/issue-19/figures/chilldown_fit.py): 0.81 s for the first start of Atlas-Centaur, whose pumps were chilled
# with helium until 8 s before liftoff (22 lb LOX and 6 lb LH2, Pratt & Whitney FR-19545-1 = NASA CR-183198, section
# 1.1); prestarts of 17 s after the 24 min coast of AC-9 (NASA TM X-1647 p. 19) and after a 1 h coast, and 24 s
# after a 3 h coast on TC-2 (NASA TM X-71821), against 8 s for the first start; and 2.7 to 3.3 s for an engine that
# was never chilled (Shuttle Centaur G-Prime, FR-19545-1 Table 4-3). The fit is within 0.4 s of each.
CHILL_DUMP = (0.988, 1.958, 1.93 * 3600.0)   # a, b, tau of a + b (1 - exp(-dt / tau))
# An engine with "chilldown": "recirculation" returns its chill flow to the tanks with small pumps, as the S-IVB's J-2
# did, and loses only its fuel lead: Apollo 10's restart lost 10 kg of LH2 (AS-505 report, Table 20-9), 0.05 s of
# the J-2's flow. The pumps run on the stage battery: two 1,500 VA chilldown inverters, switched on about 4 minutes
# before a restart (Saturn V flight manual MSFC-MAN-503: S-IVB electrical system, "chilldown inverters", and restart preparations).
CHILL_RECIRC_S = 0.05
CHILL_RECIRC_WH = 2 * 1500.0 * 240.0 / 3600.0


def chilldown_loss_s(dt: float) -> float:
    """Seconds of full flow lost to chilldown at a start of an engine that dumps its chill flow, dt s after it was
    last cold."""
    a, b, tau = CHILL_DUMP
    return a + b * (1.0 - math.exp(-max(0.0, dt) / tau))


TRANSIENTS = {
    "pump_fed": {"delay_s": 1.0, "sd": 0.06,
                 "start": ((0.0, 0.0), (0.85, 0.03), (1.0, 0.15), (1.25, 0.31), (1.5, 0.5), (1.75, 0.64), (2.0, 0.79),
                           (2.25, 0.885), (2.5, 0.925), (3.0, 0.95), (3.45, 0.97), (4.0, 1.0)),
                 "shutdown": ((0.0, 1.0), (0.07, 0.97), (0.1, 0.9), (0.14, 0.74), (0.17, 0.52), (0.2, 0.23), (0.25, 0.1),
                              (0.5, 0.05), (1.0, 0.025), (1.6, 0.01), (2.3, 0.0))},
    "expander": {"delay_s": 0.5, "sd": 0.03,
                 "start": ((0.0, 0.0), (0.5, 0.05), (0.8, 0.3), (1.0, 0.9), (1.3, 1.0)),
                 "shutdown": ((0.0, 1.0), (0.04, 0.85), (0.12, 0.2), (0.25, 0.03), (0.4, 0.0))},
    "pressure_fed": {"delay_s": 0.5, "sd": 0.09,
                     "start": ((0.0, 0.0), (0.045, 0.9), (0.245, 1.0)),
                     "shutdown": ((0.0, 1.0), (0.25, 0.95), (0.55, 0.55), (0.9, 0.1), (1.3, 0.02), (1.8, 0.0))},
}


# Turbopump power balance. Each pump raises its propellant from the tank to PUMP_RATIO x chamber pressure, at the
# pump efficiency of flown engines of the same cycle (fuel, oxidizer):
# - gas generator: F-1 1,856 and 1,600 psia at 1,125 psia, 72.6% and 74.6%; J-2 1,238 and 1,114 psia at 787 psia,
#   73% and 80% (NASA SP-8107 Tables I and II; Oefelein and Yang 1993 Table 2).
# - fuel-rich staged: RS-25 HPFTP 5,950 psia, and the oxidizer 4,045 psia (89%) and 6,970 psia (11%, to the
#   preburners) at 2,871 psia, 75.0% and 71.8% (SSME Orientation, Block IIA at 104.5%).
# - expander: RL10A-3-3A 1,067 and 621 psia at 475.5 psia, 57.7% and 64.1% (NASA TM-107318 Table 6.1.1).
# Oxidizer-rich staged and full-flow engines have no published pump data; they take the RS-25's numbers with the
# high-pressure side on the turbine-drive propellant(s). Electric pumps take the gas generator numbers.
PUMP = {"gas_generator": (1.61, 1.42, 0.73, 0.77), "electric_pump": (1.61, 1.42, 0.73, 0.77),
        "staged_fuel": (2.07, 1.52, 0.75, 0.72), "staged_ox": (1.52, 2.07, 0.72, 0.75),
        "full_flow": (2.07, 2.07, 0.75, 0.75), "expander": (2.245, 1.306, 0.577, 0.641)}
# Turbines: inlet temperature (K), pressure ratio and efficiency of flown engines. The drive gas comes from CEA at the
# preburner or gas generator mixture ratio that gives that temperature.
# - gas generator, RP-1: F-1, 1,450 F, 16.4, 60.5% (SP-8107 Table III). LH2: J-2, 1,200 F, 7.3 x 2.5 in series,
#   57% power-weighted (same table). LCH4 and MMH have no flown gas generator in these sources and take the F-1's.
# - staged: RS-25 HPFTP and HPOTP, 1,310 F and 870 F, 1.50 and 1.53, 81.1% and 74.6% (SSME Orientation p. 65 and
#   73); the fuel turbine's numbers, 983 K, 1.55 (schematic, 4,793 to 3,091 psia) and 79% power-weighted.
# - expander: RL10A-3-3A, hydrogen from the cooling jacket, pressure ratio 1.402, 73.5%, 4% of the fuel bypassed.
TURBINE = {"gas_generator": {"RP1": (1061.0, 16.4, 0.605), "LH2": (922.0, 18.25, 0.574), "LCH4": (1061.0, 16.4, 0.605),
                             "MMH": (1061.0, 16.4, 0.605)},
           "staged": (983.0, 1.55, 0.79), "expander": (1.402, 0.735, 0.96)}
# Expander jacket heat: the cooling-jacket heat load of the regenerative cooling model, times JACKET_HEAT_CAL, heats the
# turbine hydrogen. 2.04 makes the RL10A-3-3A pick up 7,714 BTU/s (TM-107318), its jacket running into the nozzle.
JACKET_HEAT_CAL = 2.04
_GAS = {}


def turbine_gas(fuel: str, ox: str, T: float, fuel_rich: bool = True) -> tuple:
    """(cp J/kg/K, gamma) of the gas a preburner or gas generator makes at temperature T, from CEA at the mixture ratio
    that gives T (found by bisection on the fuel-rich side, or the oxidizer-rich side)."""
    key = (fuel, ox, round(T), fuel_rich)
    if key not in _GAS:
        r = cea_rocket(fuel, ox, _pb_ratio(fuel, ox, T, fuel_rich), 50.0, 2.0)
        g, mw = r["gamma"], r["mw"]
        _GAS[key] = (g / (g - 1.0) * 8314.46 / mw, g)
    return _GAS[key]


def _pb_ratio(fuel: str, ox: str, T: float, fuel_rich: bool) -> float:
    """The preburner mixture ratio (O/F) that gives temperature T."""
    lo, hi = (0.05, 1.5) if fuel_rich else (8.0, 80.0)
    for _ in range(40):
        mid = math.sqrt(lo * hi)
        t = cea_rocket(fuel, ox, mid, 50.0, 2.0)["Tc"]
        if (t < T) == fuel_rich:
            lo = mid
        else:
            hi = mid
    return math.sqrt(lo * hi)


def turbine_work(cp: float, gamma: float, T: float, pr: float, eta: float) -> float:
    """Shaft work (J per kg of turbine gas)."""
    return cp * T * eta * (1.0 - pr ** (-(gamma - 1.0) / gamma))


# Liquid hydrogen gets denser as a pump compresses it, which lowers the work per kg. Normal hydrogen at 21 K from
# saturation to 251 bar, then the 24 K isotherm's steps to 401 bar (at 21 K it would freeze near 300 bar, and the
# pumped liquid warms): NIST Chemistry WebBook, fluid properties of hydrogen, isotherms at 21 K and 24 K.
LH2_RHO = ((1.215e5, 70.115), (51e5, 75.609), (101e5, 79.342), (151e5, 82.263), (201e5, 84.703), (251e5, 86.817),
           (301e5, 88.823), (351e5, 90.626), (401e5, 92.272))


def pump_work(fuel: str, rho: float, p_in: float, p_out: float) -> float:
    """Ideal pump work per kg, the integral of dp / rho from p_in to p_out (J/kg)."""
    if p_out <= p_in:
        return 0.0
    if fuel != "LH2":
        return (p_out - p_in) / rho
    w, x = 0.0, LH2_RHO
    for (pa, ra), (pb, rb) in zip(x, x[1:] + ((1e9, x[-1][1] + (x[-1][1] - x[-2][1]) * (1e9 - x[-1][0]) / (x[-1][0] - x[-2][0])),)):
        lo, hi = max(p_in, pa), min(p_out, pb)
        if hi > lo:
            ra_, rb_ = (ra + (rb - ra) * (lo - pa) / (pb - pa), ra + (rb - ra) * (hi - pa) / (pb - pa))
            w += (hi - lo) / (0.5 * (ra_ + rb_))
    if p_in < x[0][0]:
        w += (min(p_out, x[0][0]) - p_in) / x[0][1]
    return w


def pump_power(cycle: str, mdot_f: float, mdot_o: float, pc: float, rho_f: float, rho_o: float, p_in: float = 3.0e5,
               fuel: str = "", ox: str = "") -> float:
    """Shaft power (W) the pumps of a cycle need to feed the chamber at pc (Pa)."""
    rf, ro, ef, eo = PUMP[cycle]
    return mdot_f * pump_work(fuel, rho_f, p_in, rf * pc) / ef + mdot_o * pump_work(ox, rho_o, p_in, ro * pc) / eo


# Combustion stability: Hewitt's correlation. The Strouhal number S = f d / V of the least volatile propellant's
# injection (orifice diameter d, injection velocity V) against the chamber's first tangential mode, f = 1.8412 c / (pi
# Dc), sets the margin against high-frequency instability: "For stability, fd/v must be greater than 0.1 cycle"
# (Rocker and Nesman, NASA Fastrac report, fitted to LOX/RP-1 like-on-like injectors). The F-1 record (Oefelein and
# Yang 1993, Tables 4, 5 and 8) ranks the same way: three early injectors at S = 0.025 to 0.040 were unstable in every
# test; the PFRT injector at 0.125 damped bombs in 108 ms; the flight injector at 0.21 had no instability in 38,798 s.
# The correlation has no term for baffles. Baffled chambers and small chambers need S >= 0.1 (F-1 PFRT and FRT, both
# baffled; Fastrac, 0.34 m, stable at 0.110). An unbaffled chamber needs S >= 0.1 x Dc / 0.34 m: the unbaffled F-1
# unit X004 with the flight injector's 7.14 mm fuel orifices (S near 0.2 in a 1.0 m chamber) showed "no tendency
# toward dynamic stability". A pintle injector counts double: the Apollo descent engine's pintle had "no major
# combustion-stability problems", while its fixed-area competitor had first tangential instability (NASA TN D-7143
# p. 16 to 17).
# The chance that a start goes unstable, against the margin m = S / S_required: half at most (20 of the first 44
# full-scale F-1 tests, Oefelein and Yang p. 662), falling through m = 0.85 to under 1% at m = 1.25 (PFRT: none in
# 11,040 s) and nil at m = 2 (flight injector).
STABILITY_S_NEUTRAL = 0.1
# The mode frequency uses an effective sound speed: the F-1's first tangential instability ran at 454 to 538 Hz in its
# 1.0 m chamber (Oefelein and Yang, Table 4), where CEA's burned gas gives 722 Hz; the gas near the injector, where
# the mode lives, is cooler and not fully burned. 0.69 x CEA's sound speed gives 500 Hz.
STABILITY_C_FACTOR = 0.69
STABILITY_DC_REF = 0.34
STABILITY_P = (0.5, 0.85, 0.1)     # p_max, m at half of p_max, width
DEFAULT_CONTRACTION = 3.0          # chamber over throat area when the design gives no chamber diameter
DEFAULT_STABILITY_MARGIN = 2.0     # a design without injector details: the F-1 flight injector (2.1) after its program


def stability_p_start(margin: float) -> float:
    """Chance that one start goes unstable, at stability margin m = S / S_required."""
    pmax, m50, w = STABILITY_P
    return pmax / (1.0 + math.exp(min(60.0, (margin - m50) / w)))


# Turbopump spin-up. A pump-fed engine's time from its first thrust to 90% thrust grows with the power its turbopumps
# deliver, t90 = SPINUP_A x P^SPINUP_B, with P the model's own turbopump power (W). Fitted to three flown engines, each
# within 2%: the RL10A-3 on Atlas-Centaur AC-9 (90% thrust 1.42 to 1.60 s after the start signal, NASA TM X-1647
# p. 19, first thrust about 0.5 s after it; model power 0.31 MW), the J-2 on the S-IVB (the curve above reaches 90%
# 2.34 s after its first thrust; 6.5 MW) and the F-1 on Apollo 10 (engine start at -6.4 s, 90% of 1.52 Mlbf near
# -2.4 s, AS-505 report Table 2-2 and Figure 5-2; 37.7 MW). The model's powers are within 10% of the published ones
# (SP-8107 Table III) except the RL10's, whose gear-driven pumps the model lacks. Staged combustion engines bootstrap
# through their preburners and start faster: the RS-25 primes its main chamber 1.4 s after the start command and
# reaches 90% thrust by 3.0 s (Bradley 1997; NSTS 1988 News Reference Manual) with 67 MW, so they take their own
# constant from it. The start curve keeps its kind's shape and is stretched to the 90% time (transient_profiles).
SPINUP_B = 0.288
SPINUP_A = {"open": 0.02611, "staged": 0.008954}
STAGED_CYCLES = ("staged_fuel", "staged_ox", "full_flow")


def spinup_t90(cycle: str, turbine_power_W: float) -> float:
    """Seconds from first thrust to 90% thrust of a pump-fed engine whose turbines deliver turbine_power_W."""
    a = SPINUP_A["staged" if cycle in STAGED_CYCLES else "open"]
    return a * max(turbine_power_W, 1e3) ** SPINUP_B


def profile_t90(profile) -> float:
    """Time at which a start profile first reaches 90% thrust."""
    for (a, fa), (b, fb) in zip(profile, profile[1:]):
        if fb >= 0.9:
            return a + (b - a) * (0.9 - fa) / (fb - fa) if fb > fa else b
    return profile[-1][0]


def isp_kinetic(fuel: str, ox: str, of_ratio: float, pc_bar: float, eps: float) -> float:
    """Ideal vacuum Isp (s) with finite-rate recombination in the nozzle: the KINETIC_BLEND of CEA's shifting and
    frozen-at-throat expansion."""
    sh = cea_rocket(fuel, ox, of_ratio, pc_bar, eps)["isp_vac"]
    fz = cea_rocket(fuel, ox, of_ratio, pc_bar, eps, frozen=True)["isp_vac"]
    return fz + KINETIC_BLEND * (sh - fz)


def eta_cstar(pair: dict, pc_bar: float) -> float:
    """c* efficiency of a good injector; it falls below 20 bar (engineering correlation, from our validation notes, not included)."""
    return pair["eta_cstar"] - 0.01 * max(0.0, (20.0 - pc_bar) / 15.0)


def profile_value(table, x: float) -> float:
    """Piecewise-linear profile [(x, y), ...]; flat beyond its ends."""
    if x <= table[0][0]:
        return table[0][1]
    for (a, fa), (b, fb) in zip(table, table[1:]):
        if x <= b:
            return fa + (fb - fa) * (x - a) / (b - a)
    return table[-1][1]


def profile_integral(table, a: float, b: float) -> float:
    """Integral of profile_value(table, x) from a to b (a <= b)."""
    if b <= a:
        return 0.0
    xs = [a] + [x for x, _ in table if a < x < b] + [b]
    return sum(0.5 * (profile_value(table, x0) + profile_value(table, x1)) * (x1 - x0) for x0, x1 in zip(xs, xs[1:]))


def transient_profiles(kind: str, build_up_s: float = None, shutdown_impulse_s: float = None):
    """(start, shutdown) profiles of an engine of this kind of TRANSIENTS. The flown curve keeps its shape and is
    stretched in time, so that full thrust comes build_up_s after the first thrust and the tail-off gives
    shutdown_impulse_s seconds of the thrust at cutoff. Left out, each is the flown engine's value."""
    start, shutdown = TRANSIENTS[kind]["start"], TRANSIENTS[kind]["shutdown"]
    if build_up_s is not None:
        k = build_up_s / start[-1][0]
        start = tuple((t * k, f) for t, f in start)
    if shutdown_impulse_s is not None:
        k = shutdown_impulse_s / profile_integral(shutdown, 0.0, shutdown[-1][0])
        shutdown = tuple((t * k, f) for t, f in shutdown)
    return start, shutdown


@dataclass
class Engine:
    name: str
    spec: dict
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    fuel: str = ""
    ox: str = ""
    cycle: str = ""
    pc: float = 0.0            # Pa
    of: float = 0.0
    eps: float = 0.0
    At: float = 0.0
    Ae: float = 0.0
    isp_vac: float = 0.0       # effective, whole engine (incl. turbine exhaust)
    mdot: float = 0.0          # total propellant flow at full thrust, kg/s
    thrust_vac: float = 0.0    # N at full thrust
    pe: float = 0.0            # Pa
    mass: float = 0.0
    min_throttle: float = 1.0
    max_starts: int = 1
    cooling_margin: float = float("inf")
    pump_power_W: float = 0.0  # for electric pumps (battery drain at full thrust)
    turbine_power_W: float = 0.0  # shaft power of the turbopumps at full thrust
    balance: dict = field(default_factory=dict)   # the turbopump power balance at full thrust
    stability: dict = field(default_factory=dict)  # the combustion stability margin (Hewitt)
    tank_pressure_min: float = 2.5e5
    ideal: dict = field(default_factory=dict)
    cryogenic: bool = False
    gimbal_deg: float = 0.0
    burn_limit_s: float = float("inf")
    catalog: str | None = None    # name in economics.CATALOG_ENGINES for flight-proven engines
    of_min: float = 0.0           # the mixture ratios the flight software may command
    of_max: float = 0.0
    start_delay: float = 1.0      # s from the start command to the first thrust
    shutdown_sd: float = 0.0      # scatter of the shutdown impulse from cutoff to cutoff
    chilldown: str = "dump"       # "dump" (chill flow overboard) or "recirculation" (back to the tanks, on battery power)
    start_profile: tuple = ((0.0, 1.0),)      # thrust fraction against s since the first thrust
    shutdown_profile: tuple = ((0.0, 0.0),)   # thrust fraction against s since the cutoff command
    _ops: dict = field(default_factory=dict, repr=False)   # off-design points, as ratios to the design point

    def thrust(self, throttle: float, p_amb: float) -> float:
        """Thrust (N) at a throttle setting and ambient pressure. The nozzle exit pressure scales with
        chamber pressure, so pressure thrust loss grows at low throttle. operating_point gives the flow,
        which falls less than the thrust at low throttle."""
        if throttle <= 0.0:
            return 0.0
        return max(0.0, throttle * self.thrust_vac - p_amb * self.Ae)

    @property
    def gimbal_rate_deg_s(self) -> float:
        """Fastest gimbal motion. Large engines have heavier nozzles to swing: the F-1's actuators moved it at
        5 deg/s (Saturn V Flight Manual, MSFC-MAN-503, p. 4-7), the J-2's at 8 deg/s on the S-II (p. 5-11) and
        on the S-IVB (Decher, NASA TM X-53384, Table III, p. 42)."""
        return 5.0 if self.thrust_vac >= 4e6 else 8.0

    def separated(self, throttle: float, p_amb: float) -> bool:
        """Separation criterion for contoured bells: the jet separates in the nozzle when exit
        pressure falls below about 0.15 of ambient (Summerfield's 0.4 is for conical nozzles; RS-25
        runs at sea level at 0.21 and Vulcain 2 at 0.154). Separated flow gives side loads that break
        nozzles in flight."""
        return p_amb > 0.0 and throttle * self.pe < 0.15 * p_amb

    def _isp_model(self, of: float, pc_bar: float) -> float:
        """The part of the Isp model that changes with mixture ratio and chamber pressure: CEA's kinetic Isp times
        the c* efficiency. Nozzle divergence, boundary layer, film cooling and the calibration do not change."""
        return isp_kinetic(self.fuel, self.ox, round(of, 3), round(pc_bar, 3), round(self.eps, 3)) \
            * eta_cstar(D.PAIRS[(self.fuel, self.ox)], pc_bar)

    def mixture_ratio_point(self, of: float) -> tuple:
        """(vacuum thrust, flow, chamber pressure) at full throttle and mixture ratio `of`, as ratios to the design
        point (MIXTURE_RATIO_RANGE and PU_FUEL_FLOW_EXP)."""
        key = ("mr", round(of, 3))
        if key not in self._ops:
            if abs(of - self.of) < 5e-4:
                self._ops[key] = (1.0, 1.0, 1.0)
            else:
                pair = D.PAIRS[(self.fuel, self.ox)]
                q = (1 + of) / (1 + self.of) * (of / self.of) ** PU_FUEL_FLOW_EXP
                pc0 = self.pc / 1e5
                cs0 = cea_rocket(self.fuel, self.ox, round(self.of, 4), round(pc0, 3), round(self.eps, 3))["cstar"] * eta_cstar(pair, pc0)
                pc = pc0 * q
                for _ in range(3):   # c* changes a little with pressure
                    cs = cea_rocket(self.fuel, self.ox, round(of, 3), round(pc, 3), round(self.eps, 3))["cstar"] * eta_cstar(pair, pc)
                    pc = pc0 * q * cs / cs0
                isp = self._isp_model(of, pc) / self._isp_model(self.of, pc0)
                self._ops[key] = (q * isp, q, pc / pc0)
        return self._ops[key]

    def isp_throttle_ratio(self, throttle: float, of: float | None = None) -> float:
        """Vacuum Isp at a throttle setting over vacuum Isp at full thrust, at the same mixture ratio. Chamber
        pressure is proportional to thrust."""
        t = round(min(max(float(throttle), 0.05), 1.0), 3)
        if t >= 0.9995:
            return 1.0
        of = self.of if of is None else of
        key = ("thr", round(of, 3), t)
        if key not in self._ops:
            pc = self.pc / 1e5 * self.mixture_ratio_point(of)[2]
            try:
                r = self._isp_model(of, max(t * pc, 0.3)) / self._isp_model(of, pc)
            except (ValueError, RuntimeError):   # CEA can fail far below any flown chamber pressure
                r = self._isp_model(of, max(0.5 * pc, 0.3)) / self._isp_model(of, pc)
            L, t0, w = THROTTLE_LOSS
            self._ops[key] = r * (1.0 - L / (1.0 + math.exp((t - t0) / w)))
        return self._ops[key]

    def operating_point(self, throttle: float, of: float | None = None) -> tuple:
        """(vacuum thrust N, propellant flow kg/s) of one engine at a throttle setting and mixture ratio."""
        fr, qr = (1.0, 1.0) if of is None or abs(of - self.of) < 5e-4 else self.mixture_ratio_point(of)[:2]
        return throttle * fr * self.thrust_vac, throttle * qr * self.mdot / self.isp_throttle_ratio(throttle, of)

    def summary(self) -> dict:
        d = {
            "name": self.name, "propellants": f"{self.fuel}/{self.ox}", "cycle": self.cycle,
            "pc_bar": self.pc / 1e5, "mixture_ratio": self.of, "expansion_ratio": self.eps,
            "throat_diameter_m": math.sqrt(4 * self.At / math.pi), "exit_diameter_m": math.sqrt(4 * self.Ae / math.pi),
            "isp_vac_s": round(self.isp_vac, 2), "isp_sl_s": round(self.thrust(1.0, 101325.0) / (self.mdot * D.G0), 2) if self.mdot else 0,
            "isp_ideal_cea_s": round(self.ideal.get("isp_vac", 0.0), 2),
            "thrust_vac_kN": round(self.thrust_vac / 1e3, 2), "thrust_sl_kN": round(self.thrust(1.0, 101325.0) / 1e3, 2),
            "mdot_kg_s": round(self.mdot, 3), "exit_pressure_kPa": round(self.pe / 1e3, 3),
            "mass_kg": round(self.mass, 1), "thrust_to_weight_vac": round(self.thrust_vac / (self.mass * D.G0), 1) if self.mass else 0,
            "min_throttle": self.min_throttle, "max_starts": self.max_starts, "gimbal_rate_deg_s": self.gimbal_rate_deg_s,
            "cooling_margin": None if math.isinf(self.cooling_margin) else round(self.cooling_margin, 3),
            "pump_power_kW": round(self.pump_power_W / 1e3, 1), "tank_pressure_min_bar": round(self.tank_pressure_min / 1e5, 2),
            "burn_limit_s": None if math.isinf(self.burn_limit_s) else self.burn_limit_s,
            "sea_level_separates": self.separated(1.0, 101325.0),
            "errors": self.errors, "warnings": self.warnings,
        }
        if self.balance or self.stability:   # the turbopump model, MARSFLIGHT_TURBOPUMP
            d.update({"turbine_power_kW": round(self.turbine_power_W / 1e3, 1), "power_balance": self.balance,
                      "stability": self.stability})
        if not SW.on("ENGINE_TRANSIENTS"):
            return d
        T = self.start_profile[-1][0]
        d.update({"start_delay_s": self.start_delay, "start_full_thrust_s": round(self.start_delay + T, 3),
                  "start_profile": [[0.0, 0.0]] + [[round(self.start_delay + t, 3), f] for t, f in self.start_profile],
                  "start_impulse_s": round(profile_integral(self.start_profile, 0.0, T), 4),
                  "shutdown_profile": [list(p) for p in self.shutdown_profile],
                  "shutdown_impulse_s": round(profile_integral(self.shutdown_profile, 0.0, self.shutdown_profile[-1][0]), 4),
                  "shutdown_impulse_sd": self.shutdown_sd,
                  "mixture_ratio_range": [round(self.of_min, 4), round(self.of_max, 4)]})
        if self.cryogenic:
            d["chilldown"] = self.chilldown
            if self.chilldown == "recirculation":
                d["chilldown_loss_s"] = CHILL_RECIRC_S
                d["chilldown_battery_Wh_per_start"] = round(CHILL_RECIRC_WH, 1)
            else:
                d["chilldown_loss_s"] = {"first_start_after_liftoff": round(chilldown_loss_s(250.0), 3),
                                         "after_1_h": round(chilldown_loss_s(3600.0), 3),
                                         "after_6_h": round(chilldown_loss_s(6 * 3600.0), 3),
                                         "warm": round(chilldown_loss_s(1e9), 3)}
        if self.ideal and not self.errors:
            fr, qr, _ = self.mixture_ratio_point(self.of_min)
            d["at_min_mixture_ratio"] = {"thrust_vac_kN": round(fr * self.thrust_vac / 1e3, 2), "mdot_kg_s": round(qr * self.mdot, 3),
                                         "isp_vac_s": round(fr / qr * self.isp_vac, 2)}
            d["isp_vac_s_at_min_throttle"] = round(self.isp_vac * self.isp_throttle_ratio(self.min_throttle), 2)
        return d


def _num(spec, key, lo, hi, errors, default=None):
    v = spec.get(key, default)
    try:
        v = float(v)
    except (TypeError, ValueError):
        errors.append(f"{key} must be a number")
        return lo
    if not (lo <= v <= hi) or math.isnan(v):
        errors.append(f"{key}={v} outside [{lo}, {hi}]")
        return min(max(v, lo), hi) if not math.isnan(v) else lo
    return v


def _stability(e: Engine, spec: dict, ideal: dict, dt: float, pc_bar: float, injector: str):
    """Combustion stability margin (Hewitt; STABILITY_* above), the turbopump model."""
    try:
        Dc = float(spec.get("chamber_diameter_m") or dt * math.sqrt(DEFAULT_CONTRACTION))
        c_sound = STABILITY_C_FACTOR * math.sqrt(ideal["gamma"] * 8314.46 / ideal["mw"] * ideal["Tc"])
        f1t = 1.8412 * c_sound / (math.pi * Dc)
        baffles = bool(spec.get("baffles", False))
        s_req = STABILITY_S_NEUTRAL * (1.0 if baffles else max(1.0, Dc / STABILITY_DC_REF))
        d_mm, v = spec.get("orifice_diameter_mm"), spec.get("injection_velocity_mps")
        if d_mm is None or v is None:
            # Undeclared injector details: an injector designed to DEFAULT_STABILITY_MARGIN at this chamber pressure.
            # Its velocity grows with the square root of chamber pressure, so pushing the chamber pressure of a given
            # injector lowers its margin.
            v_ref = 20.0 * math.sqrt(pc_bar / 77.6)   # the F-1 flight injector: 17.1 m/s fuel at 77.6 bar
            d_mm = float(d_mm) if d_mm is not None else 1e3 * DEFAULT_STABILITY_MARGIN * s_req * v_ref / f1t
            v = float(v) if v is not None else v_ref
        S = f1t * float(d_mm) * 1e-3 / float(v)
        m = S / s_req * (2.0 if injector == "pintle" else 1.0)
        e.stability = {"chamber_diameter_m": round(Dc, 3), "first_tangential_Hz": round(f1t, 1), "strouhal": round(S, 4),
                       "strouhal_required": round(s_req, 4), "margin": round(m, 3),
                       "p_unstable_per_start": round(stability_p_start(m), 6), "baffles": baffles}
    except (TypeError, ValueError, KeyError, ZeroDivisionError) as ex:
        e.errors.append(f"stability inputs: {ex}")


def build_engine(name: str, spec: dict) -> Engine:
    e = Engine(name=name, spec=spec)
    err = e.errors
    e.fuel, e.ox = spec.get("fuel"), spec.get("oxidizer")
    if (e.fuel, e.ox) not in D.PAIRS:
        err.append(f"propellant pair {e.fuel}/{e.ox} not supported; use one of " + ", ".join(f"{f}/{o}" for f, o in D.PAIRS))
        return e
    pair = D.PAIRS[(e.fuel, e.ox)]
    e.cycle = spec.get("cycle")
    if e.cycle not in D.CYCLES:
        err.append(f"cycle {e.cycle!r} not one of {sorted(D.CYCLES)}")
        return e
    cyc = D.CYCLES[e.cycle]
    pc_bar = _num(spec, "pc_bar", 3.0, 400.0, err)
    e.of = _num(spec, "mixture_ratio", 0.5, 9.0, err)
    e.eps = _num(spec, "expansion_ratio", 2.0, 300.0, err)
    dt = _num(spec, "throat_diameter_m", 0.004, 1.2, err)
    film = _num(spec, "film_cooling_fraction", 0.0, 0.15, err, 0.0)
    e.gimbal_deg = _num(spec, "gimbal_deg", 0.0, 15.0, err, 0.0)
    cooling = spec.get("cooling", "regen")
    injector = spec.get("injector", "standard")
    igniter = spec.get("igniter", "hypergolic" if pair["hypergolic"] else "spark_torch")
    e.cryogenic = D.PROPELLANTS[e.fuel]["cryo"] or D.PROPELLANTS[e.ox]["cryo"]

    # Cycle envelopes and compatibility.
    pc_max = cyc["pc_max"]
    if e.cycle == "expander":
        if e.fuel not in D.EXPANDER_FUELS:
            err.append("expander cycle needs LH2 or LCH4 as coolant/turbine drive")
        else:
            pc_max = min(pc_max, D.EXPANDER_FUELS[e.fuel]["pc_max"])
    if e.cycle == "staged_ox" and e.fuel not in D.STAGED_OX_FUELS:
        err.append("oxidizer-rich staged combustion is only qualified with RP1 or LCH4")
    if e.cycle == "full_flow" and e.fuel not in D.FULL_FLOW_FUELS:
        err.append("full-flow staged combustion needs a gaseous-fuel-rich preburner (LCH4 or LH2)")
    if e.cycle == "staged_fuel" and e.fuel == "RP1":
        err.append("fuel-rich staged combustion with RP-1 cokes the turbine; not allowed")
    if pc_bar > pc_max:
        err.append(f"chamber pressure {pc_bar:.0f} bar exceeds the {e.cycle} envelope of {pc_max:.0f} bar")
    e.pc = pc_bar * 1e5
    e.At = math.pi * dt * dt / 4
    e.Ae = e.At * e.eps

    try:
        ideal = cea_rocket(e.fuel, e.ox, round(e.of, 4), round(pc_bar, 3), round(e.eps, 3))
    except Exception as ex:  # noqa: BLE001
        err.append(f"thermochemistry failed: {ex}")
        return e
    e.ideal = ideal

    # Loss model (engineering correlation, from our validation notes, not included).
    eta_cstar = pair["eta_cstar"] - 0.01 * max(0.0, (20.0 - pc_bar) / 15.0)
    eta_div = 0.985
    eta_bl = 1.0 - 0.00078 * math.sqrt(e.eps)
    eta_film = 1.0 - 0.5 * film
    cstar_eff = ideal["cstar"] * eta_cstar
    fit = SW.on("ENGINE_FIT")   # refits: Isp line in ln(eps), mass per cycle, MMH heat load, expander cap
    cal = FIT.isp_cal(e.eps) if fit else pair.get("isp_cal", 1.0)
    isp_chamber = ideal["isp_vac"] * eta_cstar * eta_div * eta_bl * eta_film * cal
    mdot_c = e.pc * e.At / cstar_eff
    e.pe = ideal["pe_bar"] * 1e5

    rho_f, rho_o = D.PROPELLANTS[e.fuel]["rho"], D.PROPELLANTS[e.ox]["rho"]
    mdot_f_c = mdot_c / (1 + e.of)
    mdot_o_c = mdot_c - mdot_f_c
    p_tank = 3.0e5
    turbopump = SW.on("TURBOPUMP")
    f_turb = 0.0
    if e.cycle == "pressure_fed":
        e.tank_pressure_min = e.pc * 1.25 + 2.0e5
    else:
        e.tank_pressure_min = 1.8e5
    if not turbopump:
        # Before the turbopump model: pump discharge at chamber pressure plus injector drop (>=15% for stability) plus cooling-jacket
        # drop, one pump efficiency, one turbine gas for every cycle.
        dp_pump = e.pc * 1.35 - p_tank
        pump_power_old = (mdot_f_c * dp_pump / rho_f + mdot_o_c * dp_pump / rho_o) / D.PUMP_EFFICIENCY
        if e.cycle == "gas_generator":
            g = 1.25
            w_t = D.GG_CP[e.fuel] * cyc["T_turb"] * (1 - cyc["PR_turb"] ** (-(g - 1) / g)) * D.TURBINE_EFFICIENCY
            # turbine flow fraction of total flow: P = f * mdot_total * w_t, mdot_total = mdot_c / (1 - f)
            x = pump_power_old / (w_t * mdot_c)          # = f / (1 - f)
            f_turb = x / (1 + x)
            if f_turb > 0.15:
                err.append(f"gas-generator turbine needs {100 * f_turb:.1f}% of the flow; above 15% the cycle does not close")
        if e.cycle == "electric_pump":
            e.pump_power_W = pump_power_old / D.EPUMP_EFFICIENCY
    # Turbopump power balance (PUMP and TURBINE above), the turbopump model (MARSFLIGHT_TURBOPUMP).
    pump_power_W = 0.0
    if turbopump and cyc["pump"]:
        pump_power_W = pump_power(e.cycle, mdot_f_c, mdot_o_c, e.pc, rho_f, rho_o, p_tank, e.fuel, e.ox)
    if turbopump and cyc["pump"] and e.cycle != "electric_pump":
        e.turbine_power_W = pump_power_W
    if not turbopump:
        pass
    elif e.cycle == "gas_generator":
        T_t, pr, eta = TURBINE["gas_generator"][e.fuel]
        try:
            cp, g = turbine_gas(e.fuel, e.ox, T_t)
        except Exception:  # noqa: BLE001
            cp, g = D.GG_CP[e.fuel], 1.25
        w_t = turbine_work(cp, g, T_t, pr, eta)
        # The pumps feed the chamber and the gas generator, mdot_c / (1 - f) in all, and the turbine takes f of that:
        # P_c / (1 - f) = f mdot_c w_t / (1 - f), so f = P_c / (mdot_c w_t).
        f_turb = min(0.99, pump_power_W / (w_t * mdot_c))
        pump_power_W = pump_power_W / (1 - f_turb)
        e.turbine_power_W = pump_power_W
        e.balance = {"turbine_inlet_K": T_t, "turbine_flow_fraction": round(f_turb, 4)}
        if f_turb > 0.15:
            err.append(f"gas-generator turbine needs {100 * f_turb:.1f}% of the flow; above 15% the cycle does not close")
    elif e.cycle in STAGED_CYCLES:
        T_t, pr, eta = TURBINE["staged"]
        rich_fuel = e.cycle != "staged_ox"
        try:
            cp, g = turbine_gas(e.fuel, e.ox, T_t, fuel_rich=rich_fuel)
            w_t = turbine_work(cp, g, T_t, pr, eta)
            # The preburner can pass at most the drive propellant plus what of the other one it burns to reach T_t.
            if e.cycle == "staged_fuel":
                avail = mdot_f_c * (1.0 + _pb_ratio(e.fuel, e.ox, T_t, True))
            elif e.cycle == "staged_ox":
                avail = mdot_o_c * (1.0 + 1.0 / _pb_ratio(e.fuel, e.ox, T_t, False))
            else:
                avail = mdot_c
            need = pump_power_W / w_t
            e.balance = {"turbine_inlet_K": T_t, "turbine_flow_needed_kg_s": round(need, 2),
                         "turbine_flow_available_kg_s": round(avail, 2), "margin": round(avail / need, 3)}
            if need > avail:
                err.append(f"the preburner gas cannot drive the pumps at {pc_bar:.0f} bar: the turbines need {need:.0f} kg/s "
                           f"at {T_t:.0f} K, and the preburner can make {avail:.0f} kg/s")
        except Exception:  # noqa: BLE001
            pass
    elif e.cycle == "expander" and e.fuel in D.COOLANT:
        pr, eta, frac = TURBINE["expander"]
        q_throat_x = D.Q_THROAT_REF * (pc_bar / D.PC_REF_BAR) ** 0.8 * (dt / D.DT_REF) ** -0.2 * max(0.3, (ideal["Tc"] - 800) / 2800)
        Q = JACKET_HEAT_CAL * q_throat_x * 0.3 * 30.0 * e.At * (1 - min(0.8, 5 * film))
        cp = D.COOLANT[e.fuel]["cp"]
        g = 1.40 if e.fuel == "LH2" else 1.30
        T_t = D.PROPELLANTS[e.fuel]["T"] + Q / (mdot_f_c * cp)
        avail = frac * mdot_f_c * turbine_work(cp, g, T_t, pr, eta)
        e.balance = {"turbine_inlet_K": round(T_t, 1), "jacket_heat_kW": round(Q / 1e3, 1),
                     "turbine_power_available_kW": round(avail / 1e3, 1), "margin": round(avail / pump_power_W, 3)}
        # Reported, not enforced: at the RL10's turbine pressure ratio the RL10B-2 (margin 0.83) and Vinci (0.57) would
        # not close, so flown expanders that are larger than the RL10A use higher turbine pressure ratios or longer
        # chambers, which no source gives. The chamber pressure and thrust caps of data.EXPANDER_FUELS stay.
    if turbopump and e.cycle == "electric_pump":
        e.pump_power_W = pump_power_W / D.EPUMP_EFFICIENCY
    mdot_total = mdot_c / (1 - f_turb)
    thrust_vac = mdot_c * isp_chamber * D.G0 + (mdot_total - mdot_c) * D.GG_EXHAUST_ISP_FRACTION * isp_chamber * D.G0
    e.mdot = mdot_total
    e.thrust_vac = thrust_vac
    e.isp_vac = thrust_vac / (mdot_total * D.G0)
    if fit and e.cycle == "expander" and e.fuel in D.EXPANDER_FUELS and thrust_vac > 1e3 * D.EXPANDER_FUELS[e.fuel]["thrust_max_kN"]:
        # A rule: above the largest flown closed expander, Vinci at 180 kN; no methane expander has flown.
        err.append(f"an expander cycle with {e.fuel} is limited to {D.EXPANDER_FUELS[e.fuel]['thrust_max_kN']:.0f} kN, because the "
                   f"chamber wall cannot pick up enough heat to drive a bigger turbine; this design makes {thrust_vac / 1e3:.0f} kN")
    if turbopump:
        _stability(e, spec, ideal, dt, pc_bar, injector)

    # Cooling.
    q_throat = D.Q_THROAT_REF * (pc_bar / D.PC_REF_BAR) ** 0.8 * (dt / D.DT_REF) ** -0.2 * max(0.3, (ideal["Tc"] - 800) / 2800)
    q_total = q_throat * 0.3 * 30.0 * e.At * (1 - min(0.8, 5 * film))
    if cooling in ("ablative", "radiative") and film < D.MIN_FILM_UNCOOLED:
        err.append(f"{cooling} chambers need fuel film cooling of at least {D.MIN_FILM_UNCOOLED:.2f} of the flow "
                   "(flown radiation-cooled and ablative thrusters use 10 to 15%); set film_cooling_fraction")
    if cooling == "regen":
        if e.fuel not in D.COOLANT:
            err.append(f"{e.fuel} cannot regeneratively cool")
        else:
            c = D.COOLANT[e.fuel]
            cap = mdot_f_c * c["cp"] * (c["T_max"] - D.PROPELLANTS[e.fuel]["T"])
            e.cooling_margin = cap / (q_total * (FIT.COOLANT_Q_CAL.get(e.fuel, 1.0) if fit else 1.0))
            if e.cooling_margin < 1.0:
                err.append(f"regenerative cooling cannot absorb the wall heat load (capacity/load = {e.cooling_margin:.2f}); "
                           "lower chamber pressure, add film cooling, or change propellants")
    elif cooling == "ablative":
        if pc_bar > D.ABLATIVE_PC_MAX_BAR:
            err.append(f"ablative chambers are limited to {D.ABLATIVE_PC_MAX_BAR:.0f} bar")
        e.burn_limit_s = D.ABLATIVE_MAX_BURN_S
    elif cooling == "radiative":
        if thrust_vac > 25e3 or pc_bar > 15:
            err.append("radiation-cooled chambers are limited to 25 kN and 15 bar")
    else:
        err.append(f"cooling {cooling!r} not one of regen, ablative, radiative")

    # Mass.
    tw = (FIT.CYCLE_TW.get(e.cycle, cyc["tw"]) if fit else cyc["tw"]) * (max(thrust_vac, 1e3) / 1e6) ** 0.08
    mass = thrust_vac / (D.G0 * tw) * (FIT.ENGINE_MASS_FUEL_FACTOR.get(e.fuel, 1.0) if fit else 1.0)
    if e.eps > 40:
        mass += 15.0 * (e.Ae - 40 * e.At)
    if injector == "pintle":
        mass *= 1.10
    elif injector != "standard":
        err.append(f"injector {injector!r} not one of standard, pintle")
    if igniter not in D.IGNITERS:
        err.append(f"igniter {igniter!r} not one of {sorted(D.IGNITERS)}")
        igniter = "spark_torch"
    if igniter == "hypergolic" and not pair["hypergolic"]:
        err.append(f"{e.fuel}/{e.ox} is not hypergolic")
    if igniter == "spark_torch" and not e.cryogenic:
        err.append("spark/torch igniters need a cryogenic propellant pair")
    units = int(_num(spec, "igniter_units", 1, 50, err, 1)) if igniter == "tea_teb" else 1
    mass += D.IGNITERS[igniter]["mass_kg"] * units
    e.max_starts = D.IGNITERS[igniter]["starts_per_unit"] * units
    e.mass = mass

    base_min = cyc["min_throttle"]
    if injector == "pintle":
        base_min = 0.1 if e.cycle == "pressure_fed" else 0.3
    e.min_throttle = base_min
    if e.separated(1.0, 101325.0):
        e.warnings.append("nozzle flow separates at sea level: firing it low in the atmosphere will break it")
    e.of_min, e.of_max = MIXTURE_RATIO_RANGE[0] * e.of, MIXTURE_RATIO_RANGE[1] * e.of
    kind = e.cycle if e.cycle in ("pressure_fed", "expander") else "pump_fed"
    e.start_delay, e.shutdown_sd = TRANSIENTS[kind]["delay_s"], TRANSIENTS[kind]["sd"]
    e.start_profile, e.shutdown_profile = transient_profiles(kind)
    if turbopump and e.cycle not in ("pressure_fed", "electric_pump") and e.turbine_power_W > 0:
        # the start is stretched to the turbopump spin-up time (used only with MARSFLIGHT_ENGINE_TRANSIENTS)
        start = TRANSIENTS[kind]["start"]
        build_up = spinup_t90(e.cycle, e.turbine_power_W) * start[-1][0] / profile_t90(start)
        e.start_profile, e.shutdown_profile = transient_profiles(kind, build_up_s=build_up)
    chill = spec.get("chilldown", "dump")
    if chill not in ("dump", "recirculation"):
        err.append('chilldown must be "dump" or "recirculation"')
    elif chill == "recirculation" and not e.cryogenic:
        err.append("only a cryogenic engine has a chilldown to recirculate")
    else:
        e.chilldown = chill
    return e
