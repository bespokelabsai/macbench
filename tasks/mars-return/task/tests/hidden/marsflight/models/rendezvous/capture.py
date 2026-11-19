"""The sample container, the capture system and the capture envelope (ported vehicle.py
``_build_rendezvous_part`` and sim.py ``_capture_envelope``)."""
from __future__ import annotations

import math

import numpy as np

from . import data as D


def container_size(diameter_m):
    """(shell mass kg, length m) of a sample container sphere."""
    return D.SAMPLE_CONTAINER_BASE_KG + D.SAMPLE_CONTAINER_KG_PER_M2 * math.pi * diameter_m ** 2, diameter_m


def capture_size(capture_d_m):
    """(mechanism mass kg, outer diameter m, length m) of a capture system with a cone opening capture_d_m."""
    return (D.CAPTURE_BASE_KG + D.CAPTURE_KG_PER_M2 * capture_d_m ** 2, capture_d_m + 0.3,
            D.CAPTURE_DEPTH_PER_D * capture_d_m + 0.3)


def check_sizes(container_d=None, capture_d=None):
    """Errors for diameters outside their ranges, or a container that does not fit the cone."""
    errs = []
    lo, hi = D.SAMPLE_CONTAINER_D_RANGE
    if container_d is not None and not lo <= container_d <= hi:
        errs.append(f"sample container diameter_m {container_d} is outside {lo} to {hi}")
    lo, hi = D.CAPTURE_D_RANGE
    if capture_d is not None and not lo <= capture_d <= hi:
        errs.append(f"capture_diameter_m {capture_d} is outside {lo} to {hi}")
    if container_d is not None and capture_d is not None and container_d >= capture_d:
        errs.append("the sample container must be smaller than the capture cone's opening")
    return errs


def envelope(axis, rel, att_rate_deg_s, capture_d, container_d):
    """The capture envelope, after the NASA Docking System's initial contact conditions (data.CAPTURE_*).
    The capture cone opens along the body axis. The container (rel: its state relative to the orbiter) must be
    inside the cone: in front, no deeper than the cone, and off its axis by at most the opening less the
    container's radius; close at 0.03 to 0.06 m/s along the axis; move sideways at under 0.04 m/s; and the body
    axis must turn at under 0.15 deg/s. Returns (measures, used, why): used is the largest fraction of any limit
    taken (0 at the centre of the envelope, 1 at its edge), why is None inside the envelope."""
    rel = np.asarray(rel, float)
    a = np.asarray(axis if axis is not None else rel[:3], float)
    a = a / np.linalg.norm(a)
    p, v = rel[:3], rel[3:]
    depth = float(np.dot(p, a))
    lateral = float(np.linalg.norm(p - depth * a))
    closing = -float(np.dot(v, a))
    lat_rate = float(np.linalg.norm(v + closing * a))
    env = {"depth_m": depth, "lateral_m": lateral, "closing_mps": closing, "lateral_rate_mps": lat_rate,
           "att_rate_deg_s": float(att_rate_deg_s)}
    max_depth = D.CAPTURE_DEPTH_PER_D * capture_d
    max_lat = (capture_d - container_d) / 2
    lo, hi = D.CAPTURE_CLOSING_MPS
    why = []
    if not 0.0 <= depth <= max_depth:
        why.append(f"the container is {depth:.2f} m along the capture axis; the cone takes it from 0 to {max_depth:.2f} m")
    if lateral > max_lat:
        why.append(f"it is {lateral:.3f} m off the capture axis; the cone takes up to {max_lat:.3f} m")
    if not lo <= closing <= hi:
        why.append(f"it closes at {closing:.3f} m/s; capture needs {lo} to {hi} m/s along the axis")
    if lat_rate > D.CAPTURE_LATERAL_RATE_MPS:
        why.append(f"it moves sideways at {lat_rate:.3f} m/s; capture needs under {D.CAPTURE_LATERAL_RATE_MPS} m/s")
    if att_rate_deg_s > D.CAPTURE_ATT_RATE_DEG_S:
        why.append(f"the body axis turns at {att_rate_deg_s:.2f} deg/s; capture needs under {D.CAPTURE_ATT_RATE_DEG_S} deg/s")
    mid, half = (lo + hi) / 2, (hi - lo) / 2
    used = max(lateral / max_lat if max_lat > 0 else 1.0, abs(closing - mid) / half,
               lat_rate / D.CAPTURE_LATERAL_RATE_MPS, att_rate_deg_s / D.CAPTURE_ATT_RATE_DEG_S)
    return env, float(used), ("; ".join(why) if why else None)
