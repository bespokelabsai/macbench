"""Public designations: the names a model may use for materials, propellants and procured hardware.

This file holds names and the documents that define them, and nothing about how the hardware behaves.
The properties behind each name (density, strength, as-built scatter) belong to the simulator and stay
hidden. Each entry maps a designation to the simulator's internal name.
"""
from __future__ import annotations

import re

# Metals by their MMPDS designation (alloy and temper), composites by their CMH-17 material system.
METALS = {
    "AISI 301 full hard": "SS301",
    "2219-T87": "Al2219",
    "2195-T84": "Al2195",
    "Ti-6Al-4V annealed": "Ti6Al4V",
}
COMPOSITES = {
    "IM7/8552": "CFRP",
    "AS4/3501-6": "CFRP",
}
STRUCTURAL = {**METALS, **COMPOSITES}

# Thermal protection materials by name.
TPS = {
    "SLA-561V": "SLA561V",
    "PICA": "PICA",
    "AVCOAT 5026-39": "Avcoat",
    "carbon phenolic": "carbon_phenolic",
}

# Propellants by name, with the procurement specification that defines each grade. Liquid methane
# has no military specification; it is named by grade only.
PROPELLANTS = {
    "LOX": ("LOX", "MIL-PRF-25508"),
    "LH2": ("LH2", "MIL-PRF-27201"),
    "LCH4": ("LCH4", None),
    "RP-1": ("RP1", "MIL-DTL-25576"),
    "MMH": ("MMH", "MIL-PRF-27404"),
    "NTO": ("NTO", "MIL-PRF-26539"),
}
RCS_PROPELLANTS = {"N2H4": ("N2H4", "MIL-PRF-26536")}

# Flight-proven engines a team may buy by designation.
PROCURED_ENGINES = ["Merlin 1D", "Merlin 1D Vacuum", "Raptor 2", "Raptor Vacuum", "RS-25", "RL10B-2", "AJ10-190"]

# Engine hardware materials by cooling. Regenerative chambers are copper alloys or superalloys;
# ablative chambers have an ablative liner; radiation-cooled chambers are refractory metals.
CHAMBER_MATERIALS = {
    "regenerative": {"NARloy-Z", "GRCop-42", "GRCop-84", "OFHC copper", "Inconel 718", "AISI 347"},
    "ablative": {"silica phenolic", "carbon phenolic"},
    "radiative": {"C-103", "Re/Ir", "Pt-Rh"},
}
NOZZLE_MATERIALS = {"Inconel 718", "AISI 347", "C-103", "carbon-carbon", "Ti-6Al-4V annealed",
                    "silica phenolic", "carbon phenolic", "NARloy-Z", "GRCop-42", "GRCop-84"}

_PROP = re.compile(r"^\s*([A-Za-z0-9\-]+)\s*(?:\(\s*([A-Za-z0-9\-]+)\s*\))?\s*$")


def propellant(text, table=PROPELLANTS):
    """Internal name for a propellant designation such as "LOX (MIL-PRF-25508)", or raise ValueError."""
    m = _PROP.match(text or "")
    if not m or m.group(1) not in table:
        raise ValueError(f"propellant {text!r} is not one of {', '.join(_prop_names(table))}")
    internal, spec = table[m.group(1)]
    if m.group(2) and m.group(2) != spec:
        raise ValueError(f"propellant {m.group(1)} is procured to {spec or 'no military specification'}, not {m.group(2)}")
    return internal


def _prop_names(table):
    return [f"{k} ({s})" if s else k for k, (_, s) in table.items()]


_PLY = re.compile(r"^\[(?:[+-]?\d{1,2}|±\d{1,2})(?:/(?:[+-]?\d{1,2}|±\d{1,2}))*\](?:\d*s?|s\d*)$")


def layup_ok(text):
    """A laminate in the usual short form, e.g. "[45/-45/0/90]s" or "[±45/0/90]2s"."""
    return bool(_PLY.match((text or "").replace(" ", "")))
