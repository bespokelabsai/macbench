"""Physical data tables used by every model in marsflight.

Each table states where its numbers come from. Values marked "engineering correlation" are fitted to
flown hardware, not first-principles physics; our validation notes (not included) lists how well each one reproduces the
hardware it was fitted to.
"""
from __future__ import annotations

G0 = 9.80665
SIGMA_SB = 5.670374419e-8
AU = 1.495978707e11

# Propellants. CEA v3 names, storage temperature (K), liquid density (kg/m^3), heat of vaporization
# (J/kg), freezing point (K). Densities and boiling points are NIST/standard handbook values at the
# storage temperature.
PROPELLANTS = {
    "LOX":  {"cea": "O2(L)",    "T": 90.17,  "rho": 1141.0, "hvap": 213e3, "freeze": 54.4,  "cryo": True,  "role": "ox"},
    "LH2":  {"cea": "H2(L)",    "T": 20.27,  "rho": 70.85,  "hvap": 446e3, "freeze": 14.0,  "cryo": True,  "role": "fuel"},
    "LCH4": {"cea": "CH4(L)",   "T": 111.64, "rho": 422.6,  "hvap": 510e3, "freeze": 90.7,  "cryo": True,  "role": "fuel"},
    "RP1":  {"cea": "RP-1",     "T": 298.15, "rho": 810.0,  "hvap": 0.0,   "freeze": 225.0, "cryo": False, "role": "fuel"},
    "NTO":  {"cea": "N2O4(L)",  "T": 298.15, "rho": 1440.0, "hvap": 0.0,   "freeze": 261.9, "cryo": False, "role": "ox"},
    "MMH":  {"cea": "CH6N2(L)", "T": 298.15, "rho": 875.0,  "hvap": 0.0,   "freeze": 220.8, "cryo": False, "role": "fuel"},
}
# Allowed bipropellant pairs (fuel, oxidizer) and the c* efficiency of a good injector at high chamber
# pressure. Engineering correlation, fitted so that the full loss model below reproduces RS-25,
# RL10B-2, Raptor Vacuum, Merlin 1D Vacuum and AJ10-190 (our validation notes, not included).
PAIRS = {
    ("LH2", "LOX"): {"eta_cstar": 0.995, "hypergolic": False, "isp_cal": 0.9901},
    ("LCH4", "LOX"): {"eta_cstar": 0.990, "hypergolic": False, "isp_cal": 0.9899},
    ("RP1", "LOX"): {"eta_cstar": 0.975, "hypergolic": False, "isp_cal": 0.9841},
    ("MMH", "NTO"): {"eta_cstar": 0.970, "hypergolic": True, "isp_cal": 0.9943},
}
# isp_cal: a second calibration against 17 flown regen-cooled engines (our validation notes, not included). The
# first fit to five engines left every out-of-sample large engine 0.6 to 3.9% optimistic; this factor
# removes that bias per propellant pair. Leave-one-out vacuum Isp error after it: 1.2% mean absolute.
# Monopropellant hydrazine RCS thrusters (MR-106 class): fixed vacuum Isp.
RCS_ISP = 225.0
RCS_PROP_RHO = 1004.0
# RCS limits per element, at the scale of flown systems. Thrust: at most the largest flown RCS, the Space Shuttle
# orbiter's 38 primary thrusters of 870 lbf (3.87 kN), 147 kN (NASA, Shuttle RCS overview, NTRS 19910018886).
# In 6-DOF a fixed-nozzle upper stage steers on its RCS alone, which needs tens of kN on a 200 t stage. Propellant:
# the Apollo lunar module tanked about 280 kg of RCS propellant (605 to 633 lb by source; Apollo 11 Mission Report
# Supplement 5, NTRS 19720018196) on a 4.7 t ascent stage, 6%. RCS propellant is at most 25% of the element's own
# mass without it (dry plus main propellant), or 50 kg on small elements. That is about 490 m/s on the element
# alone; more than that is main propulsion.
RCS_THRUST_MAX_N = 147000.0
RCS_PROP_MAX_FRACTION = 0.25
RCS_PROP_MIN_KG = 50.0

# Engine cycles. pc_max (bar) is the highest chamber pressure the cycle has flown or been qualified at
# with margin; tw is the sea-level-class thrust-to-weight of the engine at 1 MN (scaled by thrust^0.08);
# min_throttle without a pintle injector. Turbine inlet temperature (K) and pressure ratio for the
# gas-generator power balance. Sources: published engine data (RS-25, RD-180, Raptor 2, Merlin 1D,
# RL10, Vinci, AJ10, Rutherford); engineering correlation.
CYCLES = {
    "pressure_fed":   {"pc_max": 25.0,  "tw": 30.0,  "min_throttle": 0.6, "pump": False},
    "electric_pump":  {"pc_max": 60.0,  "tw": 55.0,  "min_throttle": 0.6, "pump": True},
    "gas_generator":  {"pc_max": 120.0, "tw": 100.0, "min_throttle": 0.6, "pump": True, "T_turb": 900.0, "PR_turb": 20.0},
    "expander":       {"pc_max": 110.0, "tw": 55.0,  "min_throttle": 0.3, "pump": True},
    "staged_fuel":    {"pc_max": 230.0, "tw": 75.0,  "min_throttle": 0.5, "pump": True},
    "staged_ox":      {"pc_max": 270.0, "tw": 80.0,  "min_throttle": 0.5, "pump": True},
    "full_flow":      {"pc_max": 350.0, "tw": 120.0, "min_throttle": 0.4, "pump": True},
}
# Cycle/propellant restrictions that real hardware has: oxidizer-rich staged combustion needs
# oxidizer-compatible turbine materials (flown with LOX/RP-1 and LOX/CH4); expanders need a fuel that
# picks up enough heat (H2 or CH4) and are limited in thrust by chamber surface area.
EXPANDER_FUELS = {"LH2": {"pc_max": 110.0, "thrust_max_kN": 300.0}, "LCH4": {"pc_max": 60.0, "thrust_max_kN": 150.0}}
STAGED_OX_FUELS = {"RP1", "LCH4"}
FULL_FLOW_FUELS = {"LCH4", "LH2"}
# Gas-generator turbine gas heat capacity (J/kg/K) by fuel (fuel-rich gas), and the effective Isp of
# the dumped turbine exhaust as a fraction of main-chamber Isp.
GG_CP = {"LH2": 7500.0, "LCH4": 3200.0, "RP1": 2400.0, "MMH": 2200.0}
GG_EXHAUST_ISP_FRACTION = 0.35
PUMP_EFFICIENCY = 0.70
TURBINE_EFFICIENCY = 0.60
# Electric pump: motor+inverter efficiency, battery specific energy (Wh/kg) and power (W/kg).
EPUMP_EFFICIENCY = 0.85
BATTERY_WH_PER_KG = 200.0
BATTERY_W_PER_KG = 3000.0

# Ignition systems. "hypergolic" only for NTO pairs; "tea_teb" uses one cartridge per start;
# "spark_torch" unlimited starts with cryogenic fuels only.
IGNITERS = {
    "hypergolic":  {"mass_kg": 0.0, "starts_per_unit": 1000},
    "tea_teb":     {"mass_kg": 1.5, "starts_per_unit": 1},
    "spark_torch": {"mass_kg": 6.0, "starts_per_unit": 1000},
}

# Throat heat flux reference for the regen-cooling check: RS-25, 160 MW/m^2 at 206.4 bar and a
# 0.262 m throat (Bartz scaling q ~ pc^0.8 Dt^-0.2). Coolant temperature limits (K): RP-1 coking,
# methane coking; hydrogen has no practical limit here.
Q_THROAT_REF = 160e6
PC_REF_BAR = 206.4
DT_REF = 0.262
COOLANT = {
    "LH2":  {"cp": 14000.0, "T_max": 900.0},
    "LCH4": {"cp": 3500.0,  "T_max": 950.0},
    "RP1":  {"cp": 2100.0,  "T_max": 560.0},
    "MMH":  {"cp": 2900.0,  "T_max": 500.0},
}
ABLATIVE_MAX_BURN_S = 900.0
MIN_FILM_UNCOOLED = 0.10    # radiative and ablative chambers: with no film cooling, flown thrusters come out 6 to 9% below the model
ABLATIVE_PC_MAX_BAR = 30.0

# Structural materials: density kg/m^3, yield strength Pa at the service temperature, Young's modulus
# Pa. Ti-6Al-4V is not allowed with LOX (impact ignition); CFRP tanks need a liner for LOX/LH2,
# included as +20% mass. Sources: MIL-HDBK-5J / MMPDS typical values.
MATERIALS = {
    "Al2219":  {"rho": 2840.0, "sy": 393e6, "E": 73e9,  "lox_ok": True},
    "Al2195":  {"rho": 2710.0, "sy": 490e6, "E": 76e9,  "lox_ok": True},
    "SS301":   {"rho": 7900.0, "sy": 965e6, "E": 193e9, "lox_ok": True},
    "Ti6Al4V": {"rho": 4430.0, "sy": 880e6, "E": 114e9, "lox_ok": False},
    "CFRP":    {"rho": 1550.0, "sy": 600e6, "E": 70e9,  "lox_ok": True, "liner_factor": 1.2},
}
MIN_GAUGE_M = 0.0008
# Pressure-stabilized balloon tanks, as on Atlas and Centaur: cold-rolled 301 stainless steel skins down to
# 0.014 in (0.36 mm, the Centaur cylinder skins; Titan 3E/Centaur D-1T Systems Summary, NASA NTRS 19750004937).
# The skin carries no compression, so the tank pressure must carry every axial and bending load.
BALLOON_MIN_GAUGE_M = 0.00035
BALLOON_MATERIALS = ("SS301",)
# Stiffened tank walls: stringers along the axis and rings around it, on the inside of the skin, as the S-IC's
# integrally machined T-section stringers or the Shuttle External Tank's orthogrid. The whole wall, and the bay
# between two rings, buckle as in NASA SP-8007 section 4.3.1 (eq. 37 to 43, with the stiffnesses of eq. 69 to 78),
# times its factor 0.75 for closely spaced stiffeners. The skin between stiffeners and the stiffeners themselves
# are checked as flat plates with simply supported edges, with one edge free on a blade or a flange.
STIFFENED_KNOCKDOWN = 0.75
POISSON = 0.3
# Factors of safety. A tank design qualified by a structural test may use the tested factors; one that
# was never tested must use the no-test factor of NASA-STD-5001 (1.6 on yield for metallic structure).
PROOF_FACTOR = 1.25          # tested: tank wall must hold max tank pressure x this at yield
BUCKLING_SF = 1.25           # tested: flight loads x this must stay below the knocked-down buckling load
PROOF_FACTOR_UNTESTED = 1.6
BUCKLING_SF_UNTESTED = 1.6

# Thermal protection. q_max: highest stagnation heat flux (W/m^2) the material has flown or been
# arc-jet qualified at; k_mass: areal mass (kg/m^2) per (heat load J/cm^2)^0.51575, from the
# Laub & Venkatapathy (2003) heat-shield mass-fraction correlation calibrated on Stardust (PICA),
# MER/Phoenix (SLA-561V) and Apollo (Avcoat). Engineering correlation.
TPS = {
    "SLA561V":         {"q_max": 2.0e6,  "k_mass": 0.065, "rho": 260.0},
    "PICA":            {"q_max": 12.0e6, "k_mass": 0.080, "rho": 270.0},
    "Avcoat":          {"q_max": 10.0e6, "k_mass": 0.095, "rho": 510.0},
    "carbon_phenolic": {"q_max": 300e6,  "k_mass": 0.200, "rho": 1450.0},
}
TPS_EXPONENT = 0.51575

# Sutton-Graves convective stagnation heating constants (SI, kg^0.5/m), and the Tauber-Sutton
# radiative heating velocity function for Earth air (V m/s -> f), Tauber & Sutton (1991).
SUTTON_GRAVES_K = {"Earth": 1.7415e-4, "Mars": 1.9027e-4}
# Calibration of the convective term against flight (our validation notes, not included; "Heating"). The heating stands
# for the hottest point of the heat shield, which the TPS must survive. Earth: 1.11, from Stardust's as-flown
# reconstruction, 817 W/cm2 of convective heating at the stagnation point (Parcero et al. 2022, NASA's entry
# vehicle quick reference guide, NTRS 20220010761). Mars: 1.0, since the model's 68.6 W/cm2 on MSL lies inside
# the 63 to 77 W/cm2 of its hottest plug (Bose et al. 2013, AAS 13-311; the leeward flank turned turbulent).
HEATING_CONVECTIVE_FACTOR = {"Earth": 1.11, "Mars": 1.0}
# A deliberate margin on all heating, over the calibrated best estimate. 1.0 is none: the flight-to-flight
# dispersion (x(1 +- 8%)) and the hidden TPS truth carry the uncertainty.
HEATING_MARGIN = 1.0
TAUBER_SUTTON_EARTH = [
    (9000, 1.5), (9250, 4.3), (9500, 9.7), (9750, 19.5), (10000, 35.0), (10250, 55.0), (10500, 81.0),
    (10750, 115.0), (11000, 151.0), (11500, 238.0), (12000, 359.0), (12500, 495.0), (13000, 660.0),
    (13500, 850.0), (14000, 1065.0), (14500, 1313.0), (15000, 1550.0), (15500, 1780.0), (16000, 2040.0),
]
TAUBER_SUTTON_MARS = [(6000, 0.2), (6150, 1.0), (6300, 1.95), (6500, 3.42), (6700, 5.1), (6900, 7.1),
                      (7000, 8.1), (7200, 10.2), (7400, 12.5), (7600, 14.8), (7800, 17.1), (8000, 19.2),
                      (8200, 21.4), (8400, 24.1), (8600, 26.0), (8800, 28.9), (9000, 31.8)]

# Landing on terrain (terrain.py holds the Jezero and Utah maps). A legged lander tips over on ground
# steeper than LANDING_SLOPE_MAX_DEG at its own scale, and a rock taller than LANDING_ROCK_MAX_M under it
# breaks a leg or the belly. InSight's limits (Golombek et al. 2017, Space Sci. Rev. 211): slopes under 15 deg
# at a 2 m scale, and rocks up to 45 cm under a footpad, 10 cm less when the legs crush.
LANDING_SLOPE_MAX_DEG = 15.0
LANDING_ROCK_MAX_M = 0.35
# Sizes the program builds. Only mass and cost bounded them before, and designs grew to 18-20 m heat shields
# and fairings, clusters of three 45-60 m supersonic parachutes and a 103-engine launcher. The limits sit at the
# reference design's scale (11.2 m stages and fairing, an 11 m heat shield, one 60 m DGB, 11 engines on S1), already
# past flown hardware: stages up to 10.1 m (Saturn V; Starship 9 m), rigid aeroshells up to 4.5 m (MSL; Orion's 5 m),
# one supersonic canopy per entry (Viking, MSL, Mars 2020; no supersonic cluster has flown), and 33 engines on one
# stage (Super Heavy).
MAX_LAUNCH_DIAMETER_M = 12.0      # stages and fairings
MAX_SHELL_DIAMETER_M = 12.0       # rigid heat shields and capsules
MAX_SUPERSONIC_CHUTES = 1         # disk gap band canopies on one vehicle
MAX_ENGINES_PER_STAGE = 33
# A capsule landing on Utah's dry lakebed sinks into its sandy clay. Its peak deceleration (g) is
# SOIL_IMPACT_G_COEF x diameter (m) x speed^2 (m/s) / mass (kg), the fit to the rigid penetrometers and the
# two full-scale MSR Earth entry vehicles dropped there (Kellas, NTRS 20170002221, Eq. 2; 1,100 g measured
# against 967 g from the fit at 39.7 m/s). terrain.py says where the lakebed is.
SOIL_IMPACT_G_COEF = 30.0
# Terrain-relative navigation (`"trn": true` on an element): a descent camera and vision computer that
# match the ground below to the onboard map, as the Mars 2020 Lander Vision System did. Once the heat
# shield is off, between h_max_m and h_min_m above the ground, it fixes the navigated position to sigma_m
# per axis. Mars 2020: matching from 4,200 m above the ground, estimates down to 500 m, required to be within
# 40 m 99% of the time, which is 13 m per axis (Johnson et al. 2022, AIAA 2022-1214; Mohan et al. 2021, AAS
# 21-541); in flight about 3 to 5 m. The camera is 0.88 kg (Maki et al. 2020); the vision computer's mass is not
# published, so 15 kg is an allowance for it, the camera and their harness. The price is an estimate.
TRN = {"mass_kg": 15.0, "usd": 10e6, "h_max_m": 4200.0, "h_min_m": 500.0, "sigma_m": 13.0}

# Parachutes. Deploy envelopes (Mach range, dynamic pressure range Pa) from the Viking/MSL DGB
# qualification and Earth subsonic ringsail practice. Mass correlation fitted to MSL (21.5 m, 750 Pa
# design, ~47 kg).
PARACHUTES = {
    "dgb":      {"mach": (0.0, 2.2), "q": (0.0, 850.0),   "cd": [(0.0, 0.55), (0.8, 0.60), (1.5, 0.52), (2.2, 0.48), (5.0, 0.40)],
                 "proj_area_ratio": 0.539},
    "ringsail": {"mach": (0.0, 0.8), "q": (0.0, 4500.0),  "cd": [(0.0, 0.80), (0.8, 0.75), (5.0, 0.50)],
                 "proj_area_ratio": 0.578},
}
PARACHUTE_MASS_K = 1.2e-4   # kg per (m^2 of diameter^2 x Pa of design q): the old rule, until vehicle.py uses
                            # models.parachutes.chutes.canopy_mass_kg (below)
PARACHUTE_BASE_KG = 5.0     # the mortar and fittings
# Canopy mass (models.parachutes.chutes.canopy_mass_kg): fabric over the nominal area plus the strength members,
# which grow with the load the canopy is rated for: m = CANOPY_AREAL_KG_M2 S0 + CANOPY_KG_PER_N F_rated, plus
# PARACHUTE_BASE_KG. Fitted to two flown canopies: the Apollo main, an 83.5 ft ringsail of 138.5 lb rated 23,800 lbf
# reefed (NASA TN D-7437, Fig. A-15 and Table I), and the MSL DGB, 21.35 m, 57.5 kg with its bag and bridles
# (Cruz et al. 2013, NTRS 20130012763), less 5.5% for the bag (Knacke, NWC TP 6575 sec. 6.7), rated 289 kN. The
# areal term, 0.114 kg/m2, sits below the whole canopies' 0.123 (Apollo), 0.143 (Orion's 116 ft, 310 lb main, NASA
# Orion parachute fact sheet) and 0.152 (MSL) kg/m2, as it should, since their lines and tapes are in the load term.
CANOPY_AREAL_KG_M2 = 0.1135
CANOPY_KG_PER_N = 4.80e-5
# Clustered canopies (models.parachutes.chutes._chute_area). Canopies flying together interfere and fly at an angle
# to the airflow, so a cluster's drag area is less than the sum of its canopies'. Apollo's ringsails, measured in
# free flight: drag coefficient ratio 0.97 for two canopies and 0.945 for three (Knacke, NWC TP 6575, sec. 5.10.2
# and Fig. 5-110). Beyond three the same slope continues (0.0275 per canopy, down to 0.80 at most), as Fig. 5-110's
# larger airdrop clusters lose more drag. Orion's three mains measured 0.951 against 0.92 to 0.93 for two (Ray and
# Morris 2011, AIAA 2011-2545), so three canopies can do as well as two; Apollo's numbers are used.
CHUTE_CLUSTER_CD = {1: 1.0, 2: 0.97, 3: 0.945}
CHUTE_CLUSTER_CD_SLOPE = 0.0275
CHUTE_CLUSTER_CD_MIN = 0.80
# Reefing (models.parachutes.chutes). Large Earth landing canopies are always reefed (Knacke sec. 5.10.3): a reefing
# line holds the skirt closed, so the canopy opens in stages and each opening load stays small. A ringsail opens in
# Apollo's two reefed stages: drag area 285 ft^2 and 1,080 ft^2 against 4,200 ft^2 full open, disreefed by cutters
# 6 s and 10 s after line stretch (NASA TN D-7437, Fig. A-15 and "Reefing-Cutter Modifications"). Each stage fills
# over CHUTE_FILL_DIAMETERS x D0 x sqrt(added drag area / full drag area) at the speed it starts at (Knacke sec.
# 7.3.7.4), its drag area growing as the fourth power of time up to 2 kP S_proj times its drag area ratio at full
# inflation (data.CHUTE_KP scaled to the stage: an opening load factor of 1.09 on the mean kP and 1.36 on the rated
# kP, against Knacke's 1.2 for a ringsail at infinite mass, Table 5-2 and sec. 6.2.4). The opening load is the
# largest drag on the canopy during the fill, flown forward from the stage's starting speed: at full inflation for
# a heavy, fast vehicle, earlier and lower when the canopy slows a light vehicle as it fills (Knacke's finite mass
# case, sec. 5.4.6). Apollo's
# design limit loads were 22,000, 23,800 and 22,900 lbf for the first stage, second stage and full open; a canopy is
# rated for its first-stage load at its design q times 23,800 / 22,000, the margin Apollo kept for the stages after
# it. A DGB is not reefed (none of the Mars DGBs were).
CHUTE_REEFING = {"ringsail": {"stages": [(285.0 / 4200.0, 6.0), (1080.0 / 4200.0, 10.0)], "rated_over_first": 23800.0 / 22000.0}}
# Parachute opening (sim.Mission.deploy and _chute_opened). After mortar fire the lines pay out for
# CHUTE_LINE_STRETCH_S_PER_M x the nominal diameter D0. MSL's 21.35 m DGB took 1.135 s on Mars, and then 0.635 s
# more to its first peak force (Cruz et al. 2013, NTRS 20130012763). The canopy fills while it travels
# CHUTE_FILL_DIAMETERS x D0, the filling distance of Knacke's Parachute Recovery Systems Design Manual (1992). The
# flights give 12.0 (MSL) and 13.5, 13.3 and 11.2 (the ASPIRE tests SR01 to SR03, Dutta et al. 2020, NTRS
# 20205001100) from their inflation times and speeds, and 12.5 is their mean. The drag area grows as the fourth
# power of the time since line stretch (the MSL model, Cruz et al. 2013, NTRS 20130012762) up to the peak opening
# load at full inflation, F = 2 kP q S_proj (Way 2018, NTRS 20180006292), with q the dynamic pressure then.
# S_proj is the inflated canopy's projected area, proj_area_ratio of its nominal area (Way's Table 2: 0.539 for the
# Viking, Phoenix and MSL DGBs, 0.578 for the LDSD ringsail). kP is drawn for each opening from the ten flown
# openings of Way's Table 2, nine DGBs and the LDSD ringsail (mean 0.752, sigma 0.080). A canopy holds the peak
# load of a hard opening, kP at its 99th percentile, at its design_q_Pa. For MSL's 21.5 m canopy at 750 Pa that
# is 275 kN, against its 289 kN flight limit load (Cruz et al. 2013).
CHUTE_LINE_STRETCH_S_PER_M = 1.135 / 21.35
CHUTE_FILL_DIAMETERS = 12.5
CHUTE_KP = {"mean": 0.752, "sigma": 0.080, "rated": 0.938}
# Bank angle control of a lifting capsule (sim.Mission._plan_bank). MSL flew its bank reversals as "20 deg/s
# profiled slews" (Way 2013, NTRS 20130011617). In its reconstructed attitude, averaged over 1 s, the three
# reversals peaked at 20.5 to 22.1 deg/s and 5.1 to 7.9 deg/s^2, and took 5.25 to 6.40 s from 10% to 90% of the
# roll (our validation notes, not included). With these limits the same rolls take 2.8% to 4.4% less.
BANK_RATE_MAX_DEG_S = 20.0
BANK_ACCEL_MAX_DEG_S2 = 5.0

# Hypersonic sphere-cone drag ratio vs Mach (Cd / Cd_hypersonic), from Viking/MSL/Stardust
# aerodatabases, and the maximum trim L/D per half-angle.
CAPSULE_CD_MACH = [(0.0, 0.62), (0.6, 0.66), (0.9, 0.75), (1.2, 0.85), (2.0, 0.91), (3.0, 0.95), (5.0, 1.0), (50.0, 1.0)]
CAPSULE_LD_MAX = {45: 0.45, 55: 0.38, 60: 0.35, 65: 0.32, 70: 0.30}
# Trim L/D against Mach number, as a ratio to the hypersonic design value (ld_trim). With a fixed center of
# mass offset the trim angle of attack grows as the capsule slows, from 16 deg at Mach 24 to 21 deg at Mach 2
# on MSL, so L/D grows from 0.243 to 0.340: MSL's preflight aerodynamic database, Schoenenberger et al. 2013
# (AAS 13-306, NTRS 20130010088) Figure 12b. MSL's flight reconstruction and Mars 2020's follow it within
# 0.01 (our validation notes, not included). A constant L/D left the MSL replay 2.5 km low at parachute deploy.
CAPSULE_LD_MACH = [(0.0, 1.40), (2.0, 1.40), (2.5, 1.325), (3.0, 1.317), (4.0, 1.263), (5.0, 1.218), (10.0, 1.107),
                   (15.0, 1.058), (20.0, 1.012), (24.0, 1.0), (50.0, 1.0)]
BALLAST_FRACTION_PER_LD = 0.08 / 0.24   # entry ballast mass fraction per unit L/D (MSL: ~8% for 0.24)
# Slender launch-stack axial force coefficient vs Mach (Saturn/Atlas class), and normal-force slope.
SLENDER_CA_MACH = [(0.0, 0.30), (0.8, 0.35), (1.05, 0.55), (1.2, 0.55), (1.5, 0.48), (2.0, 0.40),
                   (3.0, 0.32), (5.0, 0.27), (10.0, 0.25), (50.0, 0.25)]
# Normal-force slope on the area of the widest diameter: Apollo/Saturn V wind tunnel data without fins and
# engine shrouds, NASA TM X-53770 (Walker, MSFC 1968), Figures 25 and 26. It is 0.044 to 0.046 per degree from
# Mach 0.5 to 2 (2.5 to 2.6 per radian), and 0.042 to 0.045 for the clean body at Mach 0.7 to 1.4. It rises to
# about 2.8 per radian at Mach 3 to 5, which this constant leaves out. The finned Saturn V has 0.08 to 0.10.
SLENDER_CN_ALPHA = 2.5      # per radian
BLUFF_CD = 1.05             # unshielded lander / bare stage falling
SRP_DRAG_DECAY = 1.2        # supersonic retropropulsion: Cd_eff = Cd * exp(-k C_T), Langley SRP data trend
FREE_MOLECULAR_HEATING_MAX = 1135.0   # W/m^2, standard payload fairing jettison limit

# Mass correlations (engineering correlations fitted to flown stages; our validation notes, not included).
THRUST_STRUCTURE_KG_PER_KN = 0.5
# Secondary structure and stage systems (skirts, feed and pressurization lines, valves, wiring, weld lands
# and other tank structure beyond the bare walls): STAGE_SYSTEMS_KG x V^0.75, with V the tank volume in m^3
# including ullage. The exponent is that of NASA's historical tank mass regression (Heineman, JSC-26098,
# 1994). With the thrust structure above, the intertank, LH2 insulation and common bulkhead terms below and
# the propellant head (vehicle._head_reinforcement), fitted to the published dry masses of S-IC, S-II, S-IVB
# and Falcon 9 S1 and S2 with their real engine masses: leave-one-out stage dry mass error 6.6%, against
# 14.0% for the earlier 13.7 kg per m^3. See our validation notes (not included)
STAGE_SYSTEMS_KG = 60.8
STAGE_SYSTEMS_EXPONENT = 0.75
# Separate tanks are joined by an intertank: a skirt as wide as the tanks that spans their two facing domes.
# It carries the stack above it without help from tank pressure, and is sized for that axial load
# (vehicle._size_intertank).
# A common bulkhead between LH2 and the other propellant is an insulating sandwich: a second face, a
# honeycomb core and two bond lines (S-II and S-IVB: 2014 aluminum faces on fiberglass-phenolic honeycomb).
# This is its areal mass on top of the structural dome. Between other propellants it is a plain dome.
COMMON_BULKHEAD_CORE_KG_PER_M2 = 6.0
# Every LH2 tank needs insulation, or air liquefies on it: 2.88 kg per m^2 of tank area, the historical
# mass estimating relation in D. Akin, "Mass Estimating Relations", UMD ENAE 483/788D, 2024.
LH2_INSULATION_KG_PER_M2 = 2.88
INTERSTAGE_KG_PER_M2 = 25.0
AVIONICS_PER_ELEMENT_KG = 60.0
# An Earth return capsule's avionics (sequencer, G-switches, timers, beacon, batteries) grow with its frontal
# area, up to the 60 kg above: 3.9 kg for a 0.81 m Stardust-class capsule, 14 kg at Genesis's 1.52 m, 60 kg
# from 3.16 m. Flown capsule avionics masses are not published; this is an estimate (our validation notes, not included).
CAPSULE_AVIONICS_KG_PER_M2 = 6.0
SOFI_KG_PER_M2 = 1.2        # spray-on foam on cryogenic launch-stage tanks
MLI_BASE_KG_PER_M2 = 0.2
MLI_KG_PER_M2_PER_LAYER = 0.015
FAIRING_KG_PER_M2 = 12.0
LEG_MASS_FRACTION = 0.03    # landing legs, per landed mass, at 2 m/s touchdown design
COPV_PV_OVER_W = 25000.0    # m (pressure x volume / weight) for helium COPVs
HELIUM_T = 300.0

# Power. Solar cell efficiency x packing, array specific mass, dust loss per sol, RTG and fission
# units. MMRTG: 110 W at 45 kg; Kilopower-class 10 kWe at 1800 kg. Batteries as above.
SOLAR_EFFICIENCY = 0.28
SOLAR_KG_PER_M2 = 3.0
SOLAR_DUST_LOSS_PER_SOL = 0.002
RTG = {"power_W": 110.0, "mass_kg": 45.0, "max_units": 3}
FISSION = {"power_W": 10000.0, "mass_kg": 1800.0}
BASE_POWER_W = 300.0         # avionics, comms and housekeeping per powered spacecraft
SURFACE_OPS_SOLS = 30        # sample collection campaign before samples are loaded
# The samples come home in Orbiting Sample containers (models/samples/manifest.py): 30 tubes, about 11 kg
# loaded. A capsule may carry several (SampleContainmentAssembly.sampleContainers); each beyond the first adds
# SAMPLE_CONTAINER_KG_PER_KG of containment structure per kg of container (its own primary and secondary vessels).
SAMPLE_CONTAINERS_MAX = 20
SAMPLE_MASS_KG = 30.0        # the samples of the older sample model, flown only with the SAMPLE_MASS switch off
SAMPLE_CONTAINER_KG_PER_KG = 0.5
# Cryocooler specific power (W input per W lifted) and mass (kg per kW input) by cold temperature.
CRYOCOOLER = {"LH2": {"w_per_w": 60.0}, "LCH4": {"w_per_w": 10.0}, "LOX": {"w_per_w": 12.0}}
CRYOCOOLER_KG_PER_KW = 50.0
# ISRU oxygen (MOXIE-class solid oxide electrolysis incl. liquefaction): kg O2 per sol per kW, kg/kW.
ISRU_KG_PER_SOL_PER_KW = 0.75
ISRU_KG_PER_KW = 60.0

# Sites. Latitude/longitude in degrees (east positive); surface altitude above the body's reference
# radius in m (Mars: MOLA areoid elevation; Earth: above the WGS84 ellipsoid). radius_km is the landing
# target: 3 km at Jezero (Perseverance's landing ellipse was 7.7 by 6.6 km) and 10 km in Utah. range_km is
# the Utah range itself: a capsule that lands farther out does not reach earth_touchdown (no full success).
SITES = {
    "earth_launch": {"body": "Earth", "lat": 28.6082, "lon": -80.6041, "alt": 3.0, "name": "Kennedy LC-39A"},
    "mars_landing": {"body": "Mars", "lat": 18.4447, "lon": 77.4508, "alt": -2550.0, "radius_km": 3.0, "name": "Jezero crater"},
    "earth_landing": {"body": "Earth", "lat": 40.36, "lon": -113.52, "alt": 1300.0, "radius_km": 10.0, "range_km": 50.0,
                      "name": "Utah Test and Training Range"},
}

# Launch sites a design may name in "launch": {"site": ...}: the published pads of the range model
# (data/range/sites.json, with their azimuth limits and weather). Unset, the pad is LC-39A as before.
LAUNCH_SITES = ("LC-39A", "SLC-40", "LC-46", "SLC-4E")
DEFAULT_LAUNCH_SITE = "LC-39A"


def launch_site_id(design) -> str:
    la = (design or {}).get("launch") if isinstance(design, dict) else None
    s = la.get("site") if isinstance(la, dict) else None
    return s if isinstance(s, str) and s else DEFAULT_LAUNCH_SITE


def launch_site(design) -> dict:
    """The Earth pad of a design, in the form of SITES["earth_launch"]. LC-39A keeps SITES["earth_launch"] exactly."""
    sid = launch_site_id(design)
    if sid == DEFAULT_LAUNCH_SITE or sid not in LAUNCH_SITES:
        return dict(SITES["earth_launch"], site=DEFAULT_LAUNCH_SITE)
    import json as _json
    import os as _os
    with open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "data", "range", "sites.json")) as f:
        s = _json.load(f)["sites"][sid]
    return {"body": "Earth", "lat": float(s["lat"]), "lon": float(s["lon"]), "alt": SITES["earth_launch"]["alt"],
            "name": s["name"], "site": sid}
