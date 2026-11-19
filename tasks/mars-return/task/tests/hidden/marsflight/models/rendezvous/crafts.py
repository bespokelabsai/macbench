"""Crafts: a stack that splits into several independently flown vehicles (ported sim.py
``_release``, ``_new_craft``, ``_drop_craft``). The core flight loop still flies one craft; this module holds the
bookkeeping and the physics of a release so the loop can call it once it flies several (not wired yet).

A part is described by a small dict: {"id", "kind", "carrier" (for a parachute), "avionics" (bool), "mass"}.
"""
from __future__ import annotations

import numpy as np

from . import data as D


def split_release(stack, eid):
    """Split a stack (list of parts, bottom first) at element eid: eid and everything above leave. A parachute stays
    with its carrier. Returns (keep, part, why): why is None when the release is allowed."""
    ids = [p["id"] for p in stack]
    if eid not in ids:
        return None, None, f"no element {eid} on this craft"
    i = ids.index(eid)
    part, keep = list(stack[i:]), list(stack[:i])
    for p in list(part):
        if p["kind"] == "parachute" and p.get("carrier") in {k["id"] for k in keep}:
            part.remove(p)
            keep.append(p)
    for p in list(keep):
        if p["kind"] == "parachute" and p.get("carrier") in {q["id"] for q in part}:
            keep.remove(p)
            part.append(p)
    keep.sort(key=stack.index)
    part.sort(key=stack.index)
    if not keep or not part:
        return None, None, "nothing would stay on this craft"
    lone_container = len(part) == 1 and part[0]["kind"] == "sample_container"
    if not (lone_container or any(p.get("avionics") for p in part)):
        return None, None, "the released part has no avionics (a stage or capsule); use separate to drop it"
    return keep, part, None


def release_velocities(x, axis, m_keep, m_part, spring_mps=D.RELEASE_SPRING_MPS):
    """States of (the staying craft, the released part) after springs push them apart at spring_mps along axis, the
    upper part forward. Momentum is kept."""
    x = np.asarray(x, float)
    a = np.asarray(axis, float)
    a = a / np.linalg.norm(a)
    dv = spring_mps * a
    xk, xp = x.copy(), x.copy()
    xp[3:] += dv * m_keep / (m_part + m_keep)
    xk[3:] -= dv * m_part / (m_part + m_keep)
    return xk, xp


class CraftSet:
    """The crafts in flight: an id, its parts, whether it has avionics (it can be commanded), its state."""

    def __init__(self, first_id, parts, x=None):
        self.crafts = {first_id: {"parts": list(parts), "active": True, "x": None if x is None else np.asarray(x, float)}}
        self.order = [first_id]

    def release(self, cid, eid, axis):
        """release:eid from craft cid. Returns (new craft id, None) or (None, why)."""
        c = self.crafts.get(cid)
        if c is None:
            return None, f"no craft {cid}"
        if c["x"] is None:
            return None, "release works only in flight"
        keep, part, why = split_release(c["parts"], eid)
        if why:
            return None, why
        if len(self.order) >= D.MAX_CRAFTS:
            return None, f"at most {D.MAX_CRAFTS} crafts can fly at once"
        if eid in self.crafts:
            return None, f"a craft {eid} already flies"
        xk, xp = release_velocities(c["x"], axis, sum(p["mass"] for p in keep), sum(p["mass"] for p in part))
        c["parts"], c["x"] = keep, xk
        self.crafts[eid] = {"parts": part, "active": any(p.get("avionics") for p in part), "x": xp}
        self.order.append(eid)
        return eid, None

    def drop(self, cid):
        self.crafts.pop(cid, None)
        if cid in self.order:
            self.order.remove(cid)

    def mass(self, cid):
        return sum(p["mass"] for p in self.crafts[cid]["parts"])
