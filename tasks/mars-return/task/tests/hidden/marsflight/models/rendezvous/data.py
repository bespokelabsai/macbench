"""Constants of Mars orbit rendezvous (ported from the data.py; capture hardware truth).

The Mars ascent vehicle puts a passive sample container in orbit, and an Earth return orbiter captures it. Sizes
follow the Mars Sample Return orbiting sample (a 0.28 m sphere of about 16 kg for 0.5 kg of tubes; here the
container holds the 30 kg of samples, so it is larger) and its capture and containment system. Engineering estimates.
"""
from __future__ import annotations

SAMPLE_CONTAINER_D_RANGE = (0.4, 1.2)      # m: 0.4 m is the smallest sphere that holds the 30 kg of samples
SAMPLE_CONTAINER_BASE_KG = 5.0             # lid, latches, tube holders
SAMPLE_CONTAINER_KG_PER_M2 = 25.0          # shell, per m^2 of sphere surface
CAPTURE_D_RANGE = (0.5, 3.0)               # m: opening of the capture cone
CAPTURE_BASE_KG = 40.0                     # door, sensors, transfer arm into the capsule
CAPTURE_KG_PER_M2 = 30.0                   # per m^2 of capture diameter squared
RELEASE_SPRING_MPS = 0.3                   # separation springs: relative speed of two parts after a release
RENDEZVOUS_RANGE_M = 1000.0                # the rendezvous milestone: within this range ...
RENDEZVOUS_SPEED_MPS = 1.0                 # ... at under this relative speed
RENDEZVOUS_PARTIAL_D0 = 1.0e7              # m: an effective distance this far or more earns no rendezvous credit

# Capture envelope, from the NASA Docking System's initial contact conditions (JSC 65795 Rev H, NASA Docking
# System Interface Definitions Document, 2013, Table 3.8.1.1-1; 3-sigma limits applied together): closing rate
# 0.03 to 0.06 m/s, lateral rate 0.04 m/s, angular rate 0.15 deg/s. In place of the docking ring's 110 mm
# lateral misalignment, the capture cone takes the container anywhere inside its opening: a lateral offset
# from its axis up to (capture diameter - container diameter) / 2, and no deeper than its depth.
CAPTURE_CLOSING_MPS = (0.03, 0.06)
CAPTURE_LATERAL_RATE_MPS = 0.04
CAPTURE_ATT_RATE_DEG_S = 0.15
CAPTURE_DEPTH_PER_D = 0.8                  # cone depth / capture diameter (the capture system is 0.8 d + 0.3 m long)

# Relative navigation sensors: range, range rate and line of sight to another craft. The published accuracies
# are 3-sigma limits, so one sigma is a third of them. Each measurement gets its own error.
# Apollo LM rendezvous radar (Grumman, Apollo News Reference, "Rendezvous radar and transponder", p. GN-5):
# range 80 ft to 400 nmi; range accuracy 1% or 80 ft (the larger) out to 5 nmi, 300 ft beyond; range rate
# accuracy 1 ft/s; angular accuracy 0.12 deg at 5 nmi to 0.24 deg at 400 nmi (0.12 deg closer in). Gimballed:
# it tracks a target in any direction.
RADAR = {"range_m": (24.384, 740800.0), "range_frac": 0.01, "range_floor_m": 24.384, "range_far_m": 91.44,
         "far_from_m": 9260.0, "rate_mps": 0.3048, "angle_deg": (0.12, 0.24)}
# Laser docking sensor (NASA TM-108597, Automated Rendezvous and Capture Review, 1991, p. 7): 0.3 to 150 m,
# range accuracy 3 mm, range rate accuracy 3 mm/s, field of regard 20 deg around its boresight, which is the
# body axis; bearing accuracy 0.075 deg, the requirement of the TRAC docking sensor in the same review.
LASER = {"range_m": (0.3, 150.0), "range_acc_m": 0.003, "rate_mps": 0.003, "angle_deg": 0.075, "fov_deg": 20.0}
SENSOR_SIGMA = 3.0
# Beyond the radar's reach (or without a craft in flight to measure from) the other craft's state comes from
# ground tracking, one sigma per axis, held for a day.
GROUND_TRACK_POS_M = 200.0
GROUND_TRACK_VEL_MPS = 0.02
GROUND_TRACK_HOLD_S = 86400.0
MAX_CRAFTS = 6

# Capture hardware truth. Each capture system design has a hidden chance that one capture attempt inside the
# envelope fails. No capture and containment system of this kind has flown, so the prior is wide. Engineering
# estimate: the median per-attempt failure chance is 3%, with a lognormal spread of a factor of 2 (one sigma),
# clipped to 0.5% to 30%. A failed attempt is either a rebound (the container hits the cone and drifts out; the
# orbiter may try again) or a jam (the door or the transfer arm sticks; the capture system is lost). The split is
# 80% rebound, 20% jam. Near the edge of the envelope the chance grows: it is multiplied by (1 + EDGE_GAIN * u^2),
# with u the largest used fraction of any envelope limit (0 at the centre, 1 at a limit).
CAPTURE_FAIL_MEDIAN = 0.03
CAPTURE_FAIL_SIGMA_LN = 0.69               # ln 2
CAPTURE_FAIL_CLIP = (0.005, 0.30)
CAPTURE_REBOUND_SHARE = 0.8
CAPTURE_EDGE_GAIN = 2.0
# The program office's capture test: a flight-like capture system on an air-bearing floor (a flat floor on which
# a mock container floats on air pads, as for docking mechanism tests), N capture attempts inside the envelope.
CAPTURE_TEST = {"fixed_usd": 4.0e6, "per_trial_usd": 0.15e6, "minutes_per_trial": 2.0, "minutes_fixed": 20.0,
                "trials": (1, 50)}
