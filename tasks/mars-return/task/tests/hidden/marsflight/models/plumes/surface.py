"""Plume surface interaction on Mars: the crater an engine digs under itself while it fires near the ground,
and whether that crater tips the lander over.

No open simulator covers this (the NASA plume surface interaction codes are not released), so this is a small
engineering model fitted to flight data, in four steps:

1. Ground pressure under one plume. The plume spreads at a fixed half-angle in Mars's thin air, so its momentum
   F lands on a disk of radius r_e + h tan(alpha) at height h: p = F / (pi (r_e + h tan alpha)^2). alpha = 10 deg
   puts Phoenix's pulse at 0.5 m at 6.4 kPa, against the "about one tenth of sea level" (10.1 kPa) Mehta's team
   measured on the half-scale rig (data/plumes/psi.json).
2. Erosion. Below a pressure p_bcf the plume only shears grains off the top (viscous shear erosion); above it
   the ground fails under the load (bearing capacity failure), which erodes about ten times faster (Vizcaino and
   Mehta 2015, Section V). The crater deepens at K sqrt(p / rho_soil), a tenth of that below p_bcf. Pulsed engines
   dig 5 to 20 times faster than steady ones (Mehta et al. 2011, "explosive erosion"); 10 is used.
   p_bcf is MSL's pressure at 18 m, where its ground vanished in dust. K is fitted so that MSL's own descent
   track (Vizcaino and Mehta 2015, Figure 3) digs the mean depth of its two craters in loose soil (Hepburn
   7.3 cm, Sleepy Dragon 10.2 cm). Digging stops at the bottom of the loose layer (bedrock or ice).
3. Crater size. Radius = RADIUS_FACTOR x the plume footprint, weighted by how much each moment dug, but never
   so narrow that the walls stand steeper than loose sand does (30 deg); volume = VOLUME_FACTOR x pi r^2 depth.
   Both factors fitted to MSL's craters.
4. Damage. A crater that reaches under a footpad puts that pad on a wall sloping atan(depth / radius); the lander
   tips if that is steeper than it stands on (data.LANDING_SLOPE_MAX_DEG). The largest rock the outflow can
   throw, C_LIFT p / (rho_rock g) with C_LIFT set by the 12 cm rock Phoenix's plume dragged, is reported but
   decides nothing: no published threshold ties debris to a failed lander (MSL took about 80 kg of ejecta and
   broke one wind sensor).

The check that matters for the issue: an engine that hovers low digs deep, while a sky crane (engines high
above the payload) or an engine cutoff a metre or two up digs little. dev/test_plumes_samples.py checks it.
"""
from __future__ import annotations

import json
import math
import os

from ...core.model import Model

_DATA = json.load(open(os.path.join(os.path.dirname(__file__), "..", "..", "data", "plumes", "psi.json")))
G_MARS = 3.711
TAN_ALPHA = math.tan(math.radians(10.0))
REPOSE_DEG = 30.0         # loose martian sand stands at about 30 deg (angle of repose)
VSE_FRACTION = 0.1        # viscous shear erosion against bearing capacity failure (Vizcaino and Mehta 2015)
PULSE_FACTOR = 10.0       # pulsed against steady jets, 5 to 20 (Mehta et al. 2011)
SOIL = _DATA["soils"]["jezero_floor"]
RHO_SOIL = SOIL["bulk_density_kg_m3"]
RHO_ROCK = SOIL["rock_density_kg_m3"]


def footprint_radius(h, r_exit):
    """Radius (m) of one plume where it meets the ground, h m below the nozzle exit."""
    return r_exit + max(h, 0.0) * TAN_ALPHA


def ground_pressure(F, h, r_exit):
    """Mean pressure (Pa) one plume of thrust F (N) puts on the ground h m below its exit."""
    if F <= 0:
        return 0.0
    return F / (math.pi * footprint_radius(h, r_exit) ** 2)


def _rate_shape(p, pulsed, p_bcf):
    """Erosion rate divided by K."""
    if p <= 0:
        return 0.0
    r = math.sqrt(p / RHO_SOIL) * (1.0 if p >= p_bcf else VSE_FRACTION)
    return r * (PULSE_FACTOR if pulsed else 1.0)


def _track(points, dt=0.01):
    """Samples (t, h) every dt along a piecewise-linear track [(t, h), ...]."""
    out = []
    for (t0, h0), (t1, h1) in zip(points, points[1:]):
        n = max(1, int(round((t1 - t0) / dt)))
        for k in range(n):
            f = (k + 0.5) / n
            out.append((t0 + f * (t1 - t0), h0 + f * (h1 - h0), (t1 - t0) / n))
    return out


def _dig(samples, F, r_exit, pulsed, p_bcf, K, depth_cap):
    """Integrate one engine over samples [(t, h, dt)] with a constant thrust. Returns depth, the dig-weighted
    footprint radius, and the highest ground pressure."""
    z, w, wr, pmax = 0.0, 0.0, 0.0, 0.0
    for _, h, dt in samples:
        p = ground_pressure(F, h, r_exit)
        pmax = max(pmax, p)
        dz = K * _rate_shape(p, pulsed, p_bcf) * dt
        if z < depth_cap:
            dz = min(dz, depth_cap - z)
            z += dz
            w += dz
            wr += dz * footprint_radius(h, r_exit)
    return z, (wr / w if w > 0 else footprint_radius(samples[-1][1], r_exit) if samples else 0.0), pmax


def _fit():
    """K from MSL's descent track and its two craters in loose soil; radius and volume factors from all four."""
    m = _DATA["msl_sky_crane"]
    samples = [s for s in _track(m["track_t_s_h_m"]) if s[1] <= 23.0]   # four engines lit below 23 m
    F, re = m["thrust_per_engine_N"], m["exit_diameter_m"] / 2
    p_bcf = ground_pressure(F, m["bcf_onset_h_m"], re)
    loose = [c for c in m["craters"].values() if not c["bedrock"]]
    target = sum(c["depth_m"] for c in loose) / len(loose)
    z1, rbar, _ = _dig(samples, F, re, False, p_bcf, 1.0, float("inf"))
    K = target / z1
    radius_factor = sum(c["diameter_m"] / 2 for c in m["craters"].values()) / len(m["craters"]) / rbar
    vol = [c["volume_m3"] / (math.pi * (c["diameter_m"] / 2) ** 2 * c["depth_m"]) for c in m["craters"].values()]
    return {"K": K, "p_bcf": p_bcf, "radius_factor": radius_factor, "volume_factor": sum(vol) / len(vol),
            "msl_depth_target": target}


FIT = _fit()
P_BCF = FIT["p_bcf"]       # 92 Pa: MSL's ground pressure at 18 m
K_EROSION = FIT["K"]
RADIUS_FACTOR = FIT["radius_factor"]
VOLUME_FACTOR = FIT["volume_factor"]
# The largest rock the outflow throws: C_LIFT p / (rho_rock g), with C_LIFT set by Phoenix's 12 cm rock at the
# measured mean impingement pressure.
C_LIFT = _DATA["phoenix"]["rock_dragged_m"] * RHO_ROCK * G_MARS / _DATA["phoenix"]["impingement_pressure_Pa"]


class Crater:
    """One engine's crater, built up sample by sample."""

    def __init__(self, r_exit, offset_m, pulsed=False, depth_cap=float("inf"), p_bcf=None):
        self.r_exit, self.offset, self.pulsed, self.cap = r_exit, offset_m, pulsed, depth_cap
        self.p_bcf = P_BCF if p_bcf is None else p_bcf
        self.depth, self.w, self.wr, self.p_max, self.fire_s = 0.0, 0.0, 0.0, 0.0, 0.0

    def add(self, F, h, dt):
        """The engine fired at thrust F (N), h m above the ground, for dt s."""
        if F <= 0 or dt <= 0:
            return
        p = ground_pressure(F, h, self.r_exit)
        self.p_max = max(self.p_max, p)
        self.fire_s += dt
        dz = K_EROSION * _rate_shape(p, self.pulsed, self.p_bcf) * dt
        if self.depth < self.cap:
            dz = min(dz, self.cap - self.depth)
            self.depth += dz
            self.w += dz
            self.wr += dz * footprint_radius(h, self.r_exit)

    @property
    def radius(self):
        """The fitted footprint radius, or wider when the walls would otherwise be steeper than loose soil stands
        (angle of repose, REPOSE_DEG)."""
        if self.w <= 0:
            return 0.0
        return max(RADIUS_FACTOR * self.wr / self.w, self.depth / math.tan(math.radians(REPOSE_DEG)))

    @property
    def volume(self):
        return VOLUME_FACTOR * math.pi * self.radius ** 2 * self.depth

    def summary(self):
        return {"depth_m": round(self.depth, 4), "radius_m": round(self.radius, 3), "volume_m3": round(self.volume, 5),
                "eroded_kg": round(self.volume * RHO_SOIL, 2), "peak_ground_pressure_Pa": round(self.p_max, 1),
                "largest_rock_thrown_m": round(C_LIFT * self.p_max / (RHO_ROCK * G_MARS), 3),
                "offset_m": round(self.offset, 3)}


def engine_layout(n, stage_diameter):
    """Where a group's engines sit, as a distance from the stage axis: one on the axis, several on a ring at 0.3
    of the stage diameter (an assumption: the design gives no engine positions)."""
    return [0.0] if n <= 1 else [0.3 * stage_diameter] * n


def footpad_check(craters, footpad_radius, slope_max_deg):
    """Does any crater reach under a footpad? If it does, that pad stands on a wall sloping atan(depth / radius).
    Returns (worst wall slope in degrees, failure message or None)."""
    worst = 0.0
    for c in craters:
        if c.depth <= 0 or c.radius <= 0:
            continue
        if c.offset + c.radius >= footpad_radius:
            worst = max(worst, math.degrees(math.atan2(c.depth, c.radius)))
    if worst > slope_max_deg:
        deepest = max(craters, key=lambda c: c.depth)
        return worst, (f"the landing engines dug a crater {deepest.depth * 100:.0f} cm deep and {deepest.radius:.2f} m in radius "
                       f"under a footpad; the pad slid down a {worst:.0f} deg wall and the lander tipped over")
    return worst, None


class PlumeSurfaceInteraction(Model):
    """Craters under engines that fire within PSI_HEIGHT_M of the Martian ground.

    Wiring: ``models/structures/loads.py`` ``check_atmospheric`` (its per-sample loop, beside the
    nozzle separation check,) calls ``fire(element_id, groups, h, dt)`` for every sample of a
    powered arc on Mars below PSI_HEIGHT_M, with ``groups`` = [(thrust_per_engine_N, exit_radius_m, n_engines,
    stage_diameter_m, pulsed)] lit on the element whose engines face the ground and h the height of their exits
    above the ground. ``models/landing/surface.py`` ``LandingMixin.touchdown`` (Mars branch, before
    ``self.mode = "landed"`` at line 140) calls ``touchdown(lead.diameter / 2, D.LANDING_SLOPE_MAX_DEG)``; a returned
    message is a flight failure. ``obs`` shows nothing: the crater is not measured in flight.
    """
    name = "plume_surface"
    area = "plumes"
    order = 55
    PSI_HEIGHT_M = 100.0

    def configure(self, ctx):
        self.ctx = ctx
        lo, hi = SOIL["loose_depth_m"]
        self.loose_depth = float(ctx.rng(self.name).uniform(lo, hi))   # hidden: the world's soil
        self.craters = {}
        self.report = None

    def fire(self, element_id, groups, h, dt):
        for gi, (F, r_exit, n, diameter, pulsed) in enumerate(groups):
            for k, off in enumerate(engine_layout(n, diameter)):
                key = (element_id, gi, k)
                c = self.craters.get(key)
                if c is None:
                    c = self.craters[key] = Crater(r_exit, off, pulsed, self.loose_depth)
                c.add(F, h, dt)

    def touchdown(self, footpad_radius, slope_max_deg):
        cs = list(self.craters.values())
        slope, msg = footpad_check(cs, footpad_radius, slope_max_deg)
        self.report = {"craters": [c.summary() for c in cs], "footpad_wall_deg": round(slope, 2),
                       "eroded_kg": round(sum(c.volume for c in cs) * RHO_SOIL, 1)}
        return msg

    def design_fields(self):
        return ["engines.<name>.pulsed"]


MODELS = [PlumeSurfaceInteraction]
