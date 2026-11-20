"""What the return capsule carries home: sample tubes in Orbiting Sample containers.

MSR planned up to 30 titanium tubes of about 55 g, each with about 15 g of rock (0.45 kg of samples in all), in an
Orbiting Sample container of about 11 kg loaded (data/samples/msr.json). ``payload_mass`` gives one container's
mass: 11 kg for 30 tubes, less with fewer (the container's own mass stays). A capsule brings home
SampleContainmentAssembly.sampleContainers of them, full (vehicle.py); the simulator loads them on Mars.
"""
from __future__ import annotations

import json
import os

_DATA = json.load(open(os.path.join(os.path.dirname(__file__), "..", "..", "data", "samples", "msr.json")))
MAX_TUBES = _DATA["tubes"]["count"]
TUBE_KG = _DATA["tubes"]["tube_mass_kg"]
SAMPLE_KG = _DATA["tubes"]["sample_mass_kg"]
OS_LOADED_KG = _DATA["orbiting_sample"]["loaded_mass_kg"]
OS_EMPTY_KG = OS_LOADED_KG - MAX_TUBES * (TUBE_KG + SAMPLE_KG)


def payload_mass(n_tubes=MAX_TUBES):
    """Mass (kg) of the loaded Orbiting Sample container with n filled tubes."""
    n = int(n_tubes)
    if not 1 <= n <= MAX_TUBES:
        raise ValueError(f"the Orbiting Sample container holds 1 to {MAX_TUBES} tubes, not {n}")
    return OS_EMPTY_KG + n * (TUBE_KG + SAMPLE_KG)


def sample_mass(n_tubes=MAX_TUBES):
    """Mars material alone (kg)."""
    return int(n_tubes) * SAMPLE_KG
