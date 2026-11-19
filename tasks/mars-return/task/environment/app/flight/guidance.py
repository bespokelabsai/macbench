"""Team flight software. See docs/REFERENCE.md for the interface.

The verifier calls reset(info) once, then step(obs) every arc, in its own process.
"""


def reset(info):
    pass


def step(obs):
    # Placeholder: sits on the pad. Replace with the mission's flight software.
    return {"next": 86400.0}
