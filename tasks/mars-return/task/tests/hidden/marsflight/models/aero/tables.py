"""Aerodynamic tables built offline. The live code only reads them.

Entry capsules and aeroshells (``data/aero/entry_capsule.json``): axial force, normal force and pitching
moment coefficients against gas (CO2 or air), cone half-angle, nose radius over base radius, Mach number and
angle of attack. Built by our table builder (not included) from SU2 8.5 runs (axisymmetric and 3-D
Euler with an effective ratio of specific heats from Mutation++ equilibrium shocks), carried to other
shapes with modified Newtonian ratios. See ``docs/reference/aero-tables.md``.

Launch stack (``data/aero/launch_stack.json``): the normal-force slope against Mach number.

Conventions (MSL's aerodynamic database, Schoenenberger et al. 2013): reference area pi D^2 / 4 and
length D; CA along the axis, positive aft; a negative angle of attack gives a negative CN and positive
lift; Cm about the nose apex, positive nose up, so that Cm = -CN x_cp / D.
"""
from __future__ import annotations

import bisect
import functools
import json
import math
import os

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "aero")


def _bracket(axis, x):
    """Index i and weight w so that x ~ axis[i] (1 - w) + axis[i+1] w, clamped to the ends."""
    if x <= axis[0]:
        return 0, 0.0
    if x >= axis[-1]:
        return len(axis) - 2, 1.0
    i = bisect.bisect_right(axis, x) - 1
    return i, (x - axis[i]) / (axis[i + 1] - axis[i])


def _interp_nd(axes, values, point):
    """Multilinear interpolation on a regular grid given as nested lists."""
    br = [_bracket(a, p) for a, p in zip(axes, point)]

    def rec(v, k):
        if k == len(br):
            return v
        i, w = br[k]
        lo = rec(v[i], k + 1)
        if w == 0.0:
            return lo
        return lo * (1 - w) + rec(v[i + 1], k + 1) * w

    return rec(values, 0)


@functools.lru_cache(maxsize=None)
def _load(name):
    with open(os.path.join(DATA, name)) as f:
        return json.load(f)


class EntryAeroTable:
    """CA, CN, Cm of a sphere-cone forebody. Angles in degrees for the table axes."""

    def __init__(self, name="entry_capsule.json"):
        self.t = _load(name)
        self.axes = [self.t["axes"][k] for k in ("half_angle_deg", "rn_over_rb", "mach", "alpha_deg")]

    def coefficients(self, gas, half_angle_deg, rn_over_rb, mach, alpha_deg):
        """(CA, CN, Cm). alpha may be negative (MSL's sign): CA is even in alpha, CN and Cm odd."""
        g = self.t["gases"][gas]
        a = abs(alpha_deg)
        s = -1.0 if alpha_deg < 0 else 1.0
        p = (half_angle_deg, rn_over_rb, mach, a)
        ca = _interp_nd(self.axes, g["CA"], p)
        cn = _interp_nd(self.axes, g["CN"], p)
        cm = _interp_nd(self.axes, g["Cm"], p)
        return ca, s * cn, s * cm

    def lift_drag(self, gas, half_angle_deg, rn_over_rb, mach, alpha_deg):
        """(CD, CL, L/D) from the body-axis coefficients."""
        ca, cn, _ = self.coefficients(gas, half_angle_deg, rn_over_rb, mach, alpha_deg)
        al = math.radians(alpha_deg)
        cd = ca * math.cos(al) + cn * math.sin(al)
        cl = cn * math.cos(al) - ca * math.sin(al)
        return cd, cl, cl / cd

    def trim_alpha(self, gas, half_angle_deg, rn_over_rb, mach, xcg_over_d, zcg_over_d):
        """Angle of attack (deg, MSL's sign) where the pitching moment about the centre of mass is zero.
        xcg: distance of the centre of mass behind the apex; zcg: its offset from the axis, positive towards
        the side that becomes the windward side at negative alpha (MSL's convention, where a cg offset trims
        the capsule to a negative alpha and positive lift). Searches 0 to the edge of the table."""
        def cm_cg(al):
            ca, cn, cm = self.coefficients(gas, half_angle_deg, rn_over_rb, mach, al)
            return cm + cn * xcg_over_d - ca * zcg_over_d
        lo, hi = 0.0, -max(self.axes[3])
        f_lo, f_hi = cm_cg(lo), cm_cg(hi)
        if f_lo * f_hi > 0:
            return hi if abs(f_hi) < abs(f_lo) else lo
        for _ in range(60):
            mid = 0.5 * (lo + hi)
            f_mid = cm_cg(mid)
            if f_lo * f_mid <= 0:
                hi, f_hi = mid, f_mid
            else:
                lo, f_lo = mid, f_mid
        return 0.5 * (lo + hi)


class LaunchNormalForce:
    """Normal-force slope (per radian, on the widest section) of the launch stack against Mach."""

    def __init__(self, name="launch_stack.json"):
        self.t = _load(name)

    def cn_alpha(self, mach, source=None):
        src = self.t["sources"][source or self.t["default"]]
        return _interp_nd([src["mach"]], src["cn_alpha_per_rad"], (mach,))


try:   # registers with the model registry once the core is in place
    from ...core.model import Model

    class AeroTables(Model):
        """Holds the table readers for the aerodynamic models. The core wires them into the force models."""
        name = "aero_tables"
        area = "aero"
        order = 20

        def configure(self, ctx):
            self.entry = EntryAeroTable()
            self.launch = LaunchNormalForce()

    MODELS = [AeroTables]
except ImportError:   # core/ not present (it is still untracked on v3-issue-110-core)
    MODELS = []
