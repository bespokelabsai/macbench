"""Recovery budget of a first stage: propellant held back for the return burns, recovery hardware mass, the
landing draw and the reuse cost credit. Pure functions; no Tudat, no flight state.

The numbers and their sources are in ``data/recovery/falcon9.json``.

Propellant reserve. The stage flies three burns after separation: a boostback burn (large for a return to the
launch site, small or none for a drone ship), an entry burn and a landing burn. Each burn has a delta-v from the
data file. The reserve follows from the rocket equation, worked backwards from the landed stage (dry mass plus
recovery hardware): the landing burn at sea level Isp, then the entry and boostback burns at vacuum Isp. With
Falcon 9's own stage this gives about 11.6% of the load for RTLS and about 4.8% for a drone ship.

Cost credit. A recovered stage is flown again until it is lost or reaches the reuse limit L. With a landing
success probability p, the expected number of flights per stage built is E = (1 - p^L) / (1 - p). Each flight
after the first pays a refurbishment of r times the new stage cost C. The cost per flight is
(C + r C (E - 1)) / E, and the credit is C minus that, paid only if this flight's landing succeeds.
"""
from __future__ import annotations

import json
import math
import os

G0 = 9.80665
MODES = ("none", "rtls", "droneship")

_DATA = None


def data_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(os.path.dirname(here)), "data", "recovery", "falcon9.json")


def data() -> dict:
    global _DATA
    if _DATA is None:
        with open(data_path()) as f:
            _DATA = json.load(f)
    return _DATA


def check_mode(mode) -> str:
    """The recovery mode, or ValueError with a message the team can read."""
    m = "none" if mode is None else str(mode).strip().lower()
    if m not in MODES:
        raise ValueError(f"recovery must be one of {', '.join(MODES)}, not {mode!r}")
    return m


def hardware_kg(dry_kg: float, mode: str) -> float:
    """Landing legs, grid fins, cold gas thrusters and recovery avionics, scaled with the stage dry mass."""
    if check_mode(mode) == "none":
        return 0.0
    return float(dry_kg) * data()["hardware"]["fraction_of_dry"]


def burns(mode: str) -> dict:
    m = check_mode(mode)
    if m == "none":
        return {"boostback_mps": 0.0, "entry_mps": 0.0, "landing_mps": 0.0}
    return {k: float(v) for k, v in data()["burns"][m].items()}


def reserve_kg(dry_kg: float, mode: str, isp_sl_s: float, isp_vac_s: float, include_hardware: bool = True) -> dict:
    """Propellant held back in the first stage for its return, burn by burn.

    ``dry_kg`` is the stage's dry mass without recovery hardware; the hardware is added when
    ``include_hardware`` is true. Returns the burns' propellant and the total, in kg.
    """
    m = check_mode(mode)
    if dry_kg <= 0 or isp_sl_s <= 0 or isp_vac_s <= 0:
        raise ValueError("dry mass and both Isp values must be positive")
    hw = hardware_kg(dry_kg, m) if include_hardware else 0.0
    landed = dry_kg + hw
    b = burns(m)
    m_landing_start = landed * math.exp(b["landing_mps"] / (G0 * isp_sl_s))
    m_entry_start = m_landing_start * math.exp(b["entry_mps"] / (G0 * isp_vac_s))
    m_boostback_start = m_entry_start * math.exp(b["boostback_mps"] / (G0 * isp_vac_s))
    return {
        "mode": m,
        "hardware_kg": hw,
        "landing_kg": m_landing_start - landed,
        "entry_kg": m_entry_start - m_landing_start,
        "boostback_kg": m_boostback_start - m_entry_start,
        "total_kg": m_boostback_start - landed,
    }


def falcon9_reserve_fraction(mode: str) -> float:
    """The reserve as a fraction of the load for Falcon 9's own first stage, the calibration check."""
    s = data()["stage"]
    r = reserve_kg(s["dry_kg"], mode, s["isp_sl_s"], s["isp_vac_s"], include_hardware=False)
    return r["total_kg"] / s["propellant_kg"]


def success_probability(mode: str) -> float:
    m = check_mode(mode)
    if m == "none":
        return 0.0
    return float(data()["landing"]["success_probability"][m])


def draw_landing(rng, mode: str) -> bool:
    """One landing attempt, from the model's own random stream."""
    if check_mode(mode) == "none":
        return False
    return bool(rng.random() < success_probability(mode))


def expected_flights(p: float, limit: int) -> float:
    """Expected flights of one stage built: it flies until a landing fails or it reaches the limit."""
    if limit < 1:
        raise ValueError("reuse limit must be at least 1")
    if p >= 1.0:
        return float(limit)
    if p <= 0.0:
        return 1.0
    return (1.0 - p ** limit) / (1.0 - p)


def reuse_credit_usd(stage_cost_usd: float, mode: str, landed: bool = True, limit: int | None = None,
                     refurbish_fraction: float | None = None) -> dict:
    """The cost credit for a recovered first stage on this flight. Zero for 'none' or a failed landing."""
    m = check_mode(mode)
    reuse = data()["reuse"]
    L = int(reuse["reuse_limit_flights"] if limit is None else limit)
    r = float(reuse["refurbish_fraction"] if refurbish_fraction is None else refurbish_fraction)
    if m == "none" or not landed or stage_cost_usd <= 0:
        return {"mode": m, "credit_usd": 0.0, "expected_flights": 1.0, "cost_per_flight_usd": float(stage_cost_usd)}
    p = success_probability(m)
    E = expected_flights(p, L)
    per_flight = (stage_cost_usd + r * stage_cost_usd * (E - 1.0)) / E
    return {"mode": m, "credit_usd": stage_cost_usd - per_flight, "expected_flights": E,
            "cost_per_flight_usd": per_flight}
