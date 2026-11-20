"""Supersonic retropropulsion: what thrusting into a supersonic flow does to a blunt body's drag and pitching
moment.

Today sim.py multiplies the drag of a bluff body by exp(-1.2 C_T) whenever its engines fire, at any Mach number
(data.SRP_DRAG_DECAY). The wind tunnel data say otherwise (data/plumes/srp.json):

- One central nozzle: the plume pushes the bow shock off the body and the drag collapses, to about 20% of its
  no-jet value by C_T = 0.5 and about 1% above C_T = 1.1 (Jarvinen and Adams 1970, via Korzun 2012 Fig. 22).
  Langley Test 1853 at Mach 4.6 and C_T = 2: 0.018 / 1.491 = 1.2% (Korzun 2012 Table 22).
- Nozzles near the rim of an aeroshell leave the middle of the bow shock alone and keep most of the drag up to
  C_T = 1 (85%), then lose it by C_T = 3 (Jarvinen and Adams).
- Nozzles under a stage, closer to the axis (Langley's three nozzles at half the radius), keep 3.8% at C_T = 2
  (0.057 / 1.491). No integrated data exist below C_T = 2 for that layout; the central curve is used there.
- Static stability: one central jet makes the pitching moment slope steeper (more stable) up to C_T of about 1,
  then it fades towards zero (Jarvinen and Adams, Korzun 2012 Fig. 15). The model gives the change from the
  no-jet slope, as a restoring torque for 6-DOF.
- The flow is unsteady (bow shock oscillation at a Strouhal number of 0.3 to 0.4), far too fast for the vehicle
  to feel; it is not flown.

Range of validity: Mach 1.05 to 6 and C_T 0 to 10 (the data). Outside it the nearest data are used and the model
says so (``in_range``). Below Mach 1 there are no drag data, and today's exp(-1.2 C_T) is kept until the aero
tables cover powered flight.
"""
from __future__ import annotations

import bisect
import json
import math
import os

from ...core.model import Model

_DATA = json.load(open(os.path.join(os.path.dirname(__file__), "..", "..", "data", "plumes", "srp.json")))
_DF = _DATA["drag_fraction"]
_L = _DATA["langley_1853"]
TRI_CT2 = _L["cd_jet"]["tri_half_radius"]["alpha_0"] / _L["cd_no_jet"]["alpha_0"]
CURVES = {
    "central": _DF["central"],
    "peripheral": _DF["peripheral"],
    # half-radius cluster: the central curve up to C_T = 1.12, Langley's tri-nozzle point at 2, then down
    "cluster": [p for p in _DF["central"] if p[0] <= 1.12] + [[2.0, TRI_CT2], [4.0, 0.005], [10.0, 0.005]],
}
VALID_MACH = tuple(_DF["valid_mach"])
VALID_CT = tuple(_DF["valid_ct"])
_PS = _DATA["pitch_slope_central"]
PITCH_MACHS = [(0.60, _PS["mach_0.60"]), (1.05, _PS["mach_1.05"]), (2.00, _PS["mach_2.00"])]


def _interp(table, x):
    xs = [p[0] for p in table]
    if x <= xs[0]:
        return table[0][1]
    if x >= xs[-1]:
        return table[-1][1]
    i = bisect.bisect_right(xs, x)
    (x0, y0), (x1, y1) = table[i - 1], table[i]
    return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def layout_of(n_engines, at_rim=False):
    """Which data apply: one engine on the axis is 'central'; several under a stage 'cluster'; nozzles on an
    aeroshell's rim 'peripheral' (only if the design says so)."""
    if at_rim:
        return "peripheral"
    return "central" if n_engines <= 1 else "cluster"


def thrust_coefficient(F, q, S):
    return F / (max(q, 1e-9) * S)


def drag_fraction(ct, layout="central"):
    """Aerodynamic drag kept with the jets on, as a share of the no-jet drag (thrust not included)."""
    if ct <= 0:
        return 1.0
    return max(0.0, _interp(CURVES[layout], ct))


def old_rule(ct):
    """sim.py today: exp(-1.2 C_T)."""
    return math.exp(-1.2 * ct)


def in_range(mach, ct):
    return VALID_MACH[0] <= mach <= VALID_MACH[1] and VALID_CT[0] <= ct <= VALID_CT[1]


def pitch_slope(ct, mach):
    """dCm/dalpha per degree of a blunt cone with one central jet (Jarvinen and Adams), interpolated in Mach
    between 0.6, 1.05 and 2.0 and held beyond."""
    ms = [m for m, _ in PITCH_MACHS]
    if mach <= ms[0]:
        return _interp(PITCH_MACHS[0][1], ct)
    if mach >= ms[-1]:
        return _interp(PITCH_MACHS[-1][1], ct)
    i = bisect.bisect_right(ms, mach)
    (m0, t0), (m1, t1) = PITCH_MACHS[i - 1], PITCH_MACHS[i]
    f = (mach - m0) / (m1 - m0)
    return (1 - f) * _interp(t0, ct) + f * _interp(t1, ct)


def pitch_slope_change(ct, mach):
    """Extra pitching moment slope (per degree, negative = more stable) from the jet, against no jet."""
    return pitch_slope(ct, mach) - pitch_slope(0.0, mach)


def restoring_torque(ct, mach, q, S, d, alpha_rad, beta_rad=0.0):
    """Body-frame torque (N m) from the jet's extra static stability: pitch and yaw moments that turn the axis
    back to the airflow. Tudat's body frame: x along the axis, alpha in the x-z plane, beta in x-y."""
    k = pitch_slope_change(ct, mach) * q * S * d   # N m per degree, negative
    return (0.0, k * math.degrees(alpha_rad), -k * math.degrees(beta_rad))


class SupersonicRetropropulsion(Model):
    """Wiring: the bluff-body drag in ``models/aero`` multiplies its no-jet drag coefficient by
    ``cd_factor(F, q, S, mach, n_engines)`` instead of exp(-1.2 C_T) (``models/aero/coefficients.py``,
    ``_coeffs_thrust``, line 54). In 6-DOF, ``forces`` gives the restoring torque from ``state.srp`` =
    (C_T, mach, q, S, diameter, alpha, beta) while a bluff body thrusts in supersonic flow. ``on_arc_end`` keeps a
    count of samples flown outside the data's range."""
    name = "srp"
    area = "plumes"
    order = 30

    def configure(self, ctx):
        self.ctx = ctx
        self.outside = 0
        self.peak_ct = 0.0

    def cd_factor(self, F, q, S, mach, n_engines, at_rim=False):
        if F <= 0:
            return 1.0
        ct = thrust_coefficient(F, q, S)
        self.peak_ct = max(self.peak_ct, ct)
        if not in_range(mach, ct) and mach >= 1.0:
            self.outside += 1
        if mach < 1.0:
            return old_rule(ct)   # subsonic: no SRP data here; keep today's rule until the aero tables
        return drag_fraction(ct, layout_of(n_engines, at_rim))

    def forces(self, t, state, arc):
        s = getattr(state, "srp", None)
        if not s or not arc.six_dof:
            return None
        ct, mach, q, S, d, al, be = s
        if ct <= 0 or mach < VALID_MACH[0]:
            return None
        return (0.0, 0.0, 0.0), restoring_torque(ct, mach, q, S, d, al, be)

    def design_fields(self):
        return ["elements[].engines[].count"]


MODELS = [SupersonicRetropropulsion]
