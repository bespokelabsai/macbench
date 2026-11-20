"""Earth entry of the bare return capsule: ballistic entry (no trim, so no lift to steer), then the main
parachute low over the Utah range and a parachute landing. The aim was set by the last trajectory correction,
which predicts the capsule down to the parachute deployment height (gnc/predict.py capsule_entry)."""
from __future__ import annotations

import os

from .log import log

UTAH_GROUND_M = 1300.0      # m above the ellipsoid: the range's lakebed (manual, "Terrain and landing")
# The main parachute opens low, 1.5 km above the ground: a bare capsule tumbles below Mach 0.9 and falls at about
# 45 m/s, so the canopy opens at about 1.2 kPa, inside the ringsail's envelope (Mach 0.8, 4.5 kPa) and its 2 kPa
# design, and is fully open (Apollo's reefing, 10 s) about 400 m lower. Opened higher, the canopy would drift with
# the winds aloft (the jet stream near 12 km). The descent under the canopy is short for a second reason: in 6-DOF
# the canopy is its own body on a riser, and the simulator then needs about 2 s of wall clock per simulated
# second (a 5 km deployment took about 20 minutes of the verifier's 1,500 s per flight).
CHUTE_DEPLOY_AGL = 1500.0
CHUTE_DEPLOY_ALT = UTAH_GROUND_M + CHUTE_DEPLOY_AGL
CHUTE_MACH_MAX = 0.75
CHUTE_Q_FRAC = 0.8          # deploy at no more than this share of the canopy's design dynamic pressure


def capsule_chute(info):
    """The parachute that rides on the capsule (the first parachute above it in the stack, before any backshell),
    as (id, design_q_Pa), or None."""
    els = (info.get("design") or {}).get("elements") or []
    seen = False
    for e in els:
        if e.get("kind") == "capsule":
            seen = True
            continue
        if seen and e.get("kind") in ("backshell", "capsule", "heatshield"):
            return None
        if seen and e.get("kind") == "parachute":
            return e["id"], float(e.get("design_q_Pa", 1000.0))
    return None


class EarthEntry:
    def __init__(self, fl):
        self.fl = fl
        self.done = False
        self.chute = capsule_chute(fl.info)
        self.deployed = False

    def step(self, obs):
        if obs["mode"] == "landed" or "touchdown" in obs["events"]:
            log("earth_touchdown", t=obs["t"], events=obs["events"])
            return {"actions": ["end:mission"]}
        h = obs.get("alt", 1e9)
        if os.environ.get("MF_DIAG"):
            # development aid: the entry as flown, for the onboard entry predictor
            log("diag_entry", t=obs["t"], r=obs["r"], v=obs["v"], alt=h, lat=obs.get("lat"), lon=obs.get("lon"),
                q=obs.get("q"), mach=obs.get("mach"), acc=obs.get("sensed_accel"), aoa=obs.get("angle_of_attack_deg"),
                air=obs.get("airspeed_vector"), mass=obs.get("mass"))
        cmd = {"bank_deg": 0.0}
        if self.chute is not None and not self.deployed:
            cid, q_design = self.chute
            mach, q = obs.get("mach"), obs.get("q")
            if h <= CHUTE_DEPLOY_ALT + 50.0 and mach is not None and q is not None:
                if mach <= CHUTE_MACH_MAX and q <= CHUTE_Q_FRAC * q_design:
                    self.deployed = True
                    log("capsule_chute_deploy", t=obs["t"], alt=h, mach=mach, q=q)
                    cmd["actions"] = [f"deploy:{cid}"]
                    cmd["next"] = 10.0
                    return cmd
                cmd["next"] = 1.0       # too fast for the canopy yet: look again shortly
                return cmd
            if h > 20e3:
                cmd["next"] = 20.0
            else:
                cmd["next"] = 20.0
                cmd["wake"] = {"alt_below": CHUTE_DEPLOY_ALT}
            if os.environ.get("MF_DIAG"):
                cmd["next"] = 2.0
            return cmd
        cmd["next"] = 10.0
        return cmd
