"""Data of the 6-DOF blunt-body entry (ported from the data.py). Each table names its source.

Used only when MARSFLIGHT_ENTRY6DOF=1 (dynamics/entry.py)."""

# 6-DOF blunt-body pitch damping, Cmq + Cm_alpha_dot (per radian, reference length the diameter), against
# Mach (columns) and total angle of attack in degrees (rows): the MSL capsule's ballistic range results
# (Arrow Tech fit, x_cg/d = 0.30), Schoenenberger, Yates and Hathaway, "Dynamic Stability Testing of the Mars
# Science Laboratory Entry Capsule", AIAA 2009-3917, Table 3 (NTRS 20090026361, p. 9). Positive is
# destabilizing: the capsule is dynamically unstable at small angles below about Mach 3, more so as it slows.
# Outside the table the nearest Mach column and the 30 deg row hold.
BLUNT_CMQ_MACH = (1.35, 1.5, 1.75, 2.0, 2.25, 2.5, 3.0, 3.5, 4.0)
BLUNT_CMQ_ALPHA = (0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0, 18.0, 20.0, 25.0, 30.0)
BLUNT_CMQ = (
    (0.9878, 0.9093, 0.7246, 0.5652, 0.4442, 0.3231, 0.1098, -0.0900, -0.2898),
    (0.8882, 0.8127, 0.6402, 0.4914, 0.3753, 0.2592, 0.0441, -0.1422, -0.3285),
    (0.6438, 0.5765, 0.4321, 0.3078, 0.2052, 0.1026, -0.1107, -0.2655, -0.4212),
    (0.3650, 0.3066, 0.1950, 0.0990, 0.0113, -0.0765, -0.2700, -0.3933, -0.5175),
    (0.1457, 0.0945, 0.0087, -0.0648, -0.1409, -0.2169, -0.3789, -0.4806, -0.5823),
    (0.0153, -0.0315, -0.1023, -0.1629, -0.2318, -0.3006, -0.4329, -0.5238, -0.6147),
    (-0.0456, -0.0903, -0.1542, -0.2088, -0.2745, -0.3402, -0.4536, -0.5400, -0.6273),
    (-0.0693, -0.1134, -0.1741, -0.2259, -0.2903, -0.3546, -0.4590, -0.5445, -0.6309),
    (-0.0757, -0.1197, -0.1799, -0.2313, -0.2952, -0.3591, -0.4599, -0.5454, -0.6309),
    (-0.0771, -0.1208, -0.1809, -0.2322, -0.2961, -0.3600, -0.4608, -0.5463, -0.6318),
    (-0.0779, -0.1218, -0.1814, -0.2322, -0.2966, -0.3609, -0.4608, -0.5463, -0.6318),
    (-0.0779, -0.1218, -0.1814, -0.2322, -0.2966, -0.3609, -0.4608, -0.5463, -0.6318),
    (-0.0779, -0.1218, -0.1814, -0.2322, -0.2966, -0.3609, -0.4608, -0.5463, -0.6318),
)
# The same for a 60 deg sphere-cone (Stardust and Genesis class, half angle 65 deg or less): the Genesis
# capsule's ballistic range multiple fits, Cheatwood, Winchenbach, Hathaway and Chapman, "Dynamic Stability
# Testing of the Genesis Sample Return Capsule", AIAA 2000-1009 (NTRS 20000027509), Fig. 10 on p. 5, read
# from the curves for Mach 1.5 to 3.5. Unstable at small angles at every Mach tested, less so as it speeds
# up. Mitcheltree et al. (AIAA 97-2304, p. 8) found no clear change with Mach at hypersonic speed, so the
# Mach 3.5 column holds above it.
BLUNT_CMQ_60_MACH = (1.5, 2.0, 2.5, 3.0, 3.5)
BLUNT_CMQ_60_ALPHA = (0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0, 25.0)
BLUNT_CMQ_60 = (
    (0.88, 0.82, 0.72, 0.61, 0.47),
    (0.84, 0.78, 0.68, 0.57, 0.43),
    (0.69, 0.63, 0.53, 0.42, 0.28),
    (0.43, 0.37, 0.28, 0.18, 0.05),
    (0.12, 0.05, -0.08, -0.20, -0.35),
    (-0.08, -0.12, -0.25, -0.35, -0.48),
    (-0.06, -0.10, -0.22, -0.33, -0.46),
    (0.07, 0.02, -0.08, -0.20, -0.33),
    (0.09, 0.03, -0.07, -0.18, -0.31),
    (0.09, 0.03, -0.07, -0.18, -0.31),
)
# Corrections of the 6-DOF modified Newtonian model of a sphere-cone to flight data, against Mach number.
# Newtonian theory puts a blunt forebody's center of pressure too far aft once the sonic line leaves the
# spherical nose for the shoulder (Mitcheltree et al., "Aerodynamics of Stardust Sample Return Capsule",
# AIAA 97-2304, NTRS 20040105538, p. 8). Each table gives how far forward of the Newtonian one the
# forebody's center of pressure is, in diameters (BLUNT_CP_SHIFT_*), and a factor on the Newtonian normal
# force (BLUNT_CN_FACTOR_*). Half angles in between are interpolated, outside held.
# 70 deg (MSL): fitted so MSL's measured center of mass (x/d 0.295, z/d 0.0221, AAS 13-306 Fig. 2) trims at
# its preflight predicted trim angle, which its flight matched (Schoenenberger et al., AAS 13-306, NTRS
# 20130010088, Fig. 12a, p. 14: 16.0 deg at Mach 25.8, 15.8 at 22, 16.2 at 17.8, 16.7 at 13.3, 17.2 at
# 8.9, 18.0 at 6.7, 19.0 at 4.4, 19.7 at 3.3 and 20.2 at 2.2), with the L/D of data.CAPSULE_LD_MACH (Fig. 12b).
# The normal force factor is smoothed over Mach, since a 0.2 deg reading error moves the fitted L/D 0.003.
BLUNT_CP_SHIFT_70 = [(2.2, 0.0040), (3.3, 0.1060), (4.4, 0.1080), (6.7, 0.0847), (8.9, 0.0519), (13.3, 0.1195),
                     (17.8, 0.1513), (22.0, 0.1378), (25.8, 0.1457)]
BLUNT_CN_FACTOR_70 = [(2.0, 0.60), (3.0, 0.74), (5.0, 0.80), (9.0, 0.80), (13.0, 0.91), (18.0, 1.0)]
# 60 deg (Stardust): CFD puts the center of pressure at 0.72 d behind the nose above about Mach 12, and at
# 0.58 d below, after the sonic line shift (AIAA 97-2304, p. 8); the Newtonian one is 0.753 d.
BLUNT_CP_SHIFT_60 = [(10.0, 0.173), (14.0, 0.033)]
BLUNT_CN_FACTOR_60 = [(0.0, 1.0)]
# Rarefied flow on a blunt body. Free molecular forces with full accommodation and diffuse re-emission at the
# wall temperature, bridged to the continuum values by
#   C = C_cont + (C_fm - C_cont) sin^2(pi (3/8 + log10(Kn) / 8)),  Kn from 0.001 (continuum) to 10,
# Wilmoth, Mitcheltree and Moss, "Low-Density Aerodynamics of the Stardust Sample Return Capsule",
# AIAA 97-2510 (NTRS 20040105522), Eq. 1 and 2, p. 3. Kn is the mean free path over the diameter, the mean
# free path from the viscosity, lambda = mu / p sqrt(pi R T / 2), with Sutherland's law (White, Viscous
# Fluid Flow, 3rd ed., Table 1-2). Wall temperature: 400 to 1,000 K in Wilmoth's Table 1 above 100 km.
RAREFIED_KN = (1e-3, 10.0)
RAREFIED_WALL_T = 500.0
GAS = {"Earth": {"R": 287.05, "mu0": 1.716e-5, "T0": 273.15, "S": 110.4},
       "Mars": {"R": 188.92, "mu0": 1.370e-5, "T0": 273.15, "S": 222.0}}
