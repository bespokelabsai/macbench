"""Mission design constants for the reference flight (dates, orbits, tuning)."""
import numpy as np

from .interplanetary import epoch
from .util import geocentric_lat_earth

# Interplanetary legs (TDB seconds since J2000). Earth departure 2030-12-28, Mars arrival 2031-10-10
# (C3 about 10.3 km^2/s^2, arrival vinf about 3.5 km/s).
T_DEP = epoch("2030-12-28T06:00:00")
T_ARR = epoch("2031-10-10T12:00:00")
# Mars departure and Earth arrival for the return (vinf 2.45 km/s out, 3.6 km/s in).
T_MDEP = epoch("2033-02-11T00:00:00")
T_EARR = epoch("2033-09-17T00:00:00")

PARK_ALT = 200e3          # parking orbit altitude (circular)
T_VERT = 7.0              # s of vertical rise after liftoff
T_KICK = 6.0              # s of pitch-over
S1_BANDWIDTH = 4.0        # rad/s: 6-DOF attitude loop during the S1 burn (the simulator's largest)
BANDWIDTH = 1.5           # rad/s: 6-DOF attitude loop from staging on (the simulator's default)
Q_TARGET = 40e3           # Pa: max-q the pitch kick is chosen for
KICK_SCALE_6DOF = 1.4     # 6-DOF: the chosen kick times this (a scan of 1.0 to 1.6 in the nominal world)
FPA_FB_DELAY_6DOF = 0.0   # 6-DOF: s after the kick before the flight path angle feedback starts (20 s in 3-DOF)
S1_DT_6DOF = 0.5          # s: 6-DOF S1 command interval. With rate-limited, lagging gimbals a new direction every 2 s
                          # is a step the vehicle chases and lags behind, which lofts the ascent; the Saturn V's computer
                          # updated its attitude commands 25 times a second (MSFC-MAN-503 p. 7-20)
FPA_MIN = 8.0             # deg: lowest flight path angle at S1 burnout
YAW_TAU = 40.0            # s: time constant of the S1 plane-hold yaw steering
YAW_LIM = 6.0             # deg: largest yaw offset from the velocity vector
YAW_LIM_HIGHQ = 2.0       # deg: same, while q > 10 kPa
# S1 flight path angle tracking. After the kick, S1 follows its Earth-relative velocity, and that gravity
# turn is sensitive: 1% more thrust lofts the trajectory by about 3 deg and costs S2 about 5 t of
# propellant. So S1 tilts a little off its velocity to follow the nominal flight path angle against
# Earth-relative speed (from the nominal flight), minus a small bias late in the burn.
S1_FPA_REF = [(150, 80.38), (200, 76.06), (250, 71.91), (300, 67.98), (400, 60.85), (500, 55.22), (600, 50.77),
              (800, 44.22), (1000, 39.61), (1250, 35.45), (1500, 32.41), (1750, 30.1), (2000, 28.31), (2500, 25.77),
              (3000, 24.12), (3500, 23.03), (3800, 22.55), (5000, 22.0)]
# deg added to S1_FPA_REF. The 11-engine S1 of task 2 burns longer and lofts; a flatter path
# from 1.5 to 2.5 km/s gave S2 840 kg more reserve in world 1040 (-1 deg: 3,949 kg, -3: 4,452, -5: 4,790).
S1_FPA_BIAS = [(0, 0.0), (1000, 0.0), (1500, -1.0), (2500, -5.0), (5000, -5.0)]
S1_FPA_GAIN = 1.0         # deg of angle of attack per deg of flight path angle error
S1_AOA_MAX = [(20e3, 2.0), (5e3, 4.0), (0.0, 6.0)]   # (dynamic pressure above which, largest tilt in deg)
# Launch-day winds: the pre-launch balloon (obs "launch_site_wind", released about 2 h before liftoff)
# biases S1's steering towards the airflow near max q, and an upper-wind launch commit criterion slips the launch a
# day when the balloon's winds through the planned ascent would still load S1 too much.
WIND_BIAS_Q = (2e3, 12e3)  # Pa: S1's turn towards the balloon's airflow fades in over this dynamic pressure
WIND_BIAS_MAX_DEG = 6.0    # deg: the largest turn towards the balloon's airflow
BALLOON_LEAD_S = 7200.0    # s: the balloon is judged this long before liftoff
UPPER_WIND_QALPHA_MAX = 150e3  # Pa deg: largest q-alpha left after the relief that the launch commits to
LAUNCH_SLIP_MAX = 5       # days the launch may slip for upper winds (the trans-Mars burn still reaches Mars)
TMI_G_MAX = 4.0          # throttle the TMI burn to stay under this many g
TMI_VINF_BIAS = np.zeros(3)

JEZERO = (18.4447, 77.4508)
# Column dust optical depth expected at Jezero at arrival (Ls 227): the median of the ordinary (no global storm)
# Mars years 24 to 36 of Montabone's maps: 0.91, range 0.46 to 1.34; 0.42 to 2.36 with the storm years
#. MSL likewise flew a dust forecast in Mars-GRAM.
MARS_DUST_TAU_FORECAST = 0.9
# Mars entry design (aim point): entry flight path angle at 130 km, nominal downrange angle from
# entry interface to touchdown and the time it takes (from the EDL design runs).
EI_FPA = -12.0
EDL_RANGE_DEG = 17.03
EDL_TIME = 442.0
CRUISE_RCS = "BS"         # element whose RCS flies the Earth-Mars TCMs
TCM_MAX = 20.0            # m/s: a larger correction means something upstream failed; clip it

# EDL
MARS_CHUTE = "CHUTE"
BALLAST_MACH = 2.6        # drop the trim ballast (no more lift) below this Mach number
CHUTE_MACH = 2.05         # deploy the DGB at or below this Mach number ...
CHUTE_MACH_MIN = 1.4      # the range trigger holds the DGB no lower than this Mach number (a choice of this reference)
CHUTE_Q = 830.0           # ... and at or below this dynamic pressure (Pa), inside the DGB's 850 Pa limit
DEPLOY_AGL_MIN = 8000.0   # m: the entry bank keeps the predicted parachute opening at least this high above the ground
# ... from this speed on (m/s). Applied from entry interface, the floor acted on early predictions with the
# density still unknown; in 6-DOF (deploy at Mach 1.9) it held the bank near lift up and flew 120 to 320 km long.
DEPLOY_FLOOR_V = 2000.0
STAGE_COAST = 2.0         # s from a burnout or cutoff to the separation of the spent stage (thrust tail-off)
HS_SEP_MACH = 0.75        # heat shield separation (simulator limit 0.9)
PD_PREDICT_AGL = 2000.0   # the entry predictor aims the point 2 km above the ground over the target
NPC_DT = 1.0              # s, predictor integration step
NPC_V_MIN = 900.0         # m/s: below this the bank magnitude is held
REV_RATE_6DOF = 5.0       # deg/s: 6-DOF only, the commanded bank moves no faster than this (edl.slow_reversal)
REV_V_MIN = 900.0        # m/s: no bank reversals below this; heading alignment from here on
HA_BANK_MAX = 45.0        # deg: largest bank in heading alignment (lift mostly up), a choice of this reference
BANK_MIN = 0.0
BANK_MAX = 150.0
CR_MIN_KM = 1.0           # crossrange corridor: CR_MIN + CR_K * (V / 6 km/s)^2
CR_K = 8.0
ENTRY_ARC = 4.0
PD_THROTTLE = 0.85        # braking throttle with all lander engines
PD_MARGIN_M = 450.0       # m added to the stopping distance when lighting the engines
PD_DECEL = 2.2            # m/s^2: reference deceleration of the braking phase
GATE_H = 40.0             # m: end of braking, start of the terminal descent
GATE_V = 4.0              # m/s down at the gate
TD_V = 1.5                # m/s down at touchdown
TAU_V = 2.0               # s, vertical speed loop
TAU_H = 4.0               # s, horizontal speed loop
HOLD_V_MAX = 0.5          # m/s: below the ZEM phase the lander moves towards its aim at most this fast (less than TD_VH_OK)
TILT_MAX = 35.0           # deg, largest thrust tilt during braking
TILT_TERMINAL = 15.0      # deg, largest tilt below the gate (touchdown limit is 15 deg on the body axis, checked below 3 m with 5 deg)
ZEM_MIN_S_6DOF = 8.0      # s: in 6-DOF the ZEM/ZEV law hands over to the speed loop at this tgo (2 s in 3-DOF)
CHUTE_MACH_MARGIN_6DOF = 0.15  # 6-DOF: deploy this far below CHUTE_MACH (a 33 m/s wind the onboard model misses is 0.15)
H_LEAD_S = 8.0            # s: horizontal speed reaches zero this long before the gate
TD_VH_OK = 0.6            # m/s: sideways speed allowed before the final touchdown descent
DIVERT_MAX_M = 300.0      # m: the powered descent moves its aim at most this far from the natural stopping point (more destabilizes the terminal phase)
# A divert of D m with t s left in the ZEM phase needs a peak sideways acceleration of about 6 D / t^2. In the
# 3 worlds with the least time among seeds 1000 to 1063, a divert forced straight back against the lander's
# motion at 1.75 m/s^2 landed safely, and one of 300 m crashed.
DIVERT_ACCEL = 1.75       # m/s^2: so the divert is also at most DIVERT_ACCEL t^2 / 6
# The landing cell is chosen on the parachute, right after the TRN fix, as Mars 2020's Lander Vision System
# chose its safe target before the powered descent (Johnson et al., AAS 17-038). The powered descent lights high
# enough that its zero effort miss phase lasts the sqrt(6 DIVERT_MAX_M / DIVERT_ACCEL) s that a DIVERT_MAX_M divert
# needs (32 s), on a gentler braking profile (gnc/edl.py divert_decel). Before, it lit at about 1 km and could move
# about 72 m.
PD_DECEL_MIN = 1.0        # m/s^2: the braking profile is never slower than this
PD_AIM_DELAY_S = 6.0      # s from the powered-descent start to its first steering call, where the aim is set
# Hazard avoidance after the terrain-relative navigation fix (gnc/hazard.py, gnc/jezero_hazard.npz)
LAND_SIGMA_M = 15.0       # m per axis: touchdown scatter about the aim, the 13 m position error after the trn_fix plus a margin
P_SAFE = 1e-4             # a cell with a lower failure chance is safe: the descent keeps or takes the nearest safe cell

# Surface, Mars ascent and TEI
MARS_STAY = ["DL", "SRF"]          # elements left on Mars at MAV liftoff
# The filter restarts at liftoff from the ground's 30 m, so the MAV steers on it from liftoff (dead reckoning
# is only the fallback). 200 km, chosen for the dead reckoning error, left MAV2 13 to 38 kg after the TEI on seeds
# 1000 to 1011 from their surface states; 160 km leaves 63 to 89 kg, with navigated periapses of 135 to 149 km.
MARS_PARK_ALT = 160e3
MAV_T_VERT = 8.0                   # s of vertical rise after MAV liftoff
MAV_DOWNRANGE_DEG = 30.0           # insertion point, degrees downrange of Jezero (launch planning)
TEI_G_MAX = 3.0
# MAV steering in the air (gnc/mars_ascent.py _limit_aoa): the bending load on the MAV tanks grows with q alpha
# A safety net that rarely binds: seeds 1000 to 1011 fly at most about 1,900 Pa deg (5 deg at the 430 Pa max q) without
# it, and a tighter 1,000 Pa deg lofted the ascent (max q 250 Pa) and left MAV2 a worse insertion.
MAV_QALPHA_MAX = 2500.0            # Pa deg
MAV_AOA_MAX = 15.0                 # deg: never further than this from the airflow while q is above MAV_Q_LIMIT_ON
MAV_Q_LIMIT_ON = 100.0             # Pa: below this the limit is off (the airflow there is mostly the wind's)
MAV_TRIM_MAX_S = 40.0              # s: the most MAV2 burns to raise a low periapsis before the TEI
MAV_Q_SHORT_ARC = 30.0             # Pa: above this, command every MAV_AIR_ARC s
MAV_AIR_ARC = 2.0
MAV_HOLD_S = 0.8                  # s: on the liftoff arc the MAV stands on the ground this long (engines coming up)
MAV_NAV_TRUST_M = 2000.0          # m: the MAV uses the filter's r and v again after a fresh fix this good
MAV_RP_MIN = 120e3                # m: after the insertion, a navigated periapsis below this is raised before the TEI
# Earth return: capsule entry design (aim point): flight path angle at 120 km, downrange angle from
# 120 km to touchdown and the time it takes (from the capsule entry design runs).
# The Utah target, geodetic on WGS84 as the simulator places Earth sites. The landing corrections
# compare it with the predicted landing's geodetic latitude on the ellipsoid (util.geodetic_latlon_earth; the
# capsule predictor already stops at a height above the ellipsoid). The B-plane aim of the earlier corrections,
# which works on a sphere, takes the target's geocentric latitude, UTTR_GC.
UTTR = (40.36, -113.52)
UTTR_GC = (geocentric_lat_earth(UTTR[0], 1300.0), UTTR[1])
EARTH_EI_FPA = -10.0
EARTH_RANGE_DEG = 6.49
EARTH_ENTRY_TIME = 470.0
RETURN_RCS = "MAV2"        # element whose RCS flies the Mars-Earth TCMs (dropped before entry)
CAP_SEP_BEFORE_EI = 4 * 3600.0
# In 6-DOF a separation pushes the two parts apart at 0.5 m/s, shared by mass (the manual, "Separation"). The capsule
# leaves the return stage along its own axis, so the last landing TCM aims for that push along SEP_AXIS and the
# stack holds that axis until it separates. Without it the push moves the Utah landing by 10 to 40 km.
SEP_PUSH = 0.5            # m/s
SEP_ALIGN_DEG = 3.0       # separate once the stack points within this of the planned axis ...
SEP_RATE_MAX = 0.05       # deg/s: ... and has stopped turning (each body rate below this)
SEP_WAIT_MAX_S = 1800.0   # s: separate anyway after waiting this long for the axis (entry is CAP_SEP_BEFORE_EI away)
SEP_RCS_MIN_KG = 0.05     # kg: or at once when the return stage's RCS is this low, since the stack cannot turn
CAPSULE = "CAP"
# Planetary protection: the return must miss Earth until EARTH_INTERCEPT_DAYS before arrival, a week as in
# ESA's Earth Return Orbiter plan. rtcm1 to rtcm3 aim at a perigee EARTH_SAFE_PERIGEE_M above the ground.
EARTH_AVOID_S = 7.5 * 86400.0       # s: corrections planned more than this before perigee aim at the safe miss
# days before T_EARR: the move onto the entry corridor. Perigee (below the ground, as the rule predicts it) comes
# 0.3 days before T_EARR, so this is 6.5 days before it, half a day inside the 7-day window. At 7.6 days the rule
# refused the burn (7.3 days before perigee, runs/t5b_*).
EARTH_INTERCEPT_DAYS = 6.8
# m: perigee of the safe miss. The rule refuses anything below 120 km, but far out it predicts with a Sun-centred
# conic that put a 500 km miss at -2,800 km 100 days out, and a 6,000 km miss at 1,400 km,
# so the miss is wide.
EARTH_SAFE_PERIGEE_M = 6000e3
# m/s: finite-difference step of the intercept's B-plane Jacobian; 0.01 m/s moved B by only a few km at 6.5 days,
# where the predictor's own scatter made the Jacobian useless in world 1035 (ref-b)
INTERCEPT_FD_MPS = 0.5
INTERCEPT_TCM_MAX = 35.0           # m/s: the intercept correction moves the B-plane aim about 9,500 km (15 to 22 m/s)
LANDING_TCMS = ("rtcm4", "rtcm5", "rtcm6")   # these return corrections target the predicted landing point directly
MARS_LANDING_TCMS = ()   # these approach corrections target the predicted Mars landing point
BANK_REF = 45.0           # deg: reference bank of the entry predictor used for approach targeting
TCM_W_TIME = 0.5          # km per second of arrival-time error in the B-plane TCM solver (Mars rotates 0.24 km/s at Jezero)
