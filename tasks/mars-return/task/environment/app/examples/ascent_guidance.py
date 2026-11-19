"""Example flight software for examples/ascent_vehicle.sysml: flies through first-stage burnout with an
open-loop pitch program, then ends the flight. It shows the interface, not a good way to fly.

The pitch program runs on time since the countdown that lifted off, not on `met`: a weather scrub holds the vehicle
on the pad and moves `t` (and `met`) on to the next attempt, by 24 hours or more.
"""
import numpy as np

_countdown_t = 0.0   # t when the countdown that lifted off began: the launch time, or the attempt after the last scrub


def reset(info):
    global _countdown_t
    _countdown_t = info["t0"]


def step(obs):
    global _countdown_t
    if obs["mode"] == "pad" and any("held on the pad by weather" in e for e in obs["command_errors"]):
        _countdown_t = obs["t"]   # t has moved on to the next attempt, where the countdown starts again
    r = np.array(obs["r"])
    up = r / np.linalg.norm(r)
    east = np.cross([0.0, 0.0, 1.0], up)
    east /= np.linalg.norm(east)
    t = obs["t"] - _countdown_t
    names = [e["id"] for e in obs["elements"]]
    if "S1" in names and not any(ev.startswith("burnout:S1") for ev in obs["events"]):
        pitch = np.radians(min(75.0, max(0.0, (t - 10.0) * 0.45)))
        actions = ["separate:FAIR"] if "FAIR" in names and obs.get("alt", 0) > 115e3 else []
        return {"engines": {"S1": {"throttle": 1.0}}, "dir": (np.cos(pitch) * up + np.sin(pitch) * east).tolist(),
                "next": 2.0, "actions": actions}
    return {"actions": ["end:mission"]}
