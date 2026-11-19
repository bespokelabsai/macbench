"""The MARSFLIGHT_NAV switch.

On: the navigated state in obs comes from the navigation filter of filter.py (IMU errors, GPS, DSN passes only in real
DSN contact, the landing radar, TRN), and the small cruise forces of small_forces.py are offered to the core.
Off (the default): navigation is the old per-regime error draw of core/guidance_link.py, to the last digit.

It stays off until the reference has been flown with it (an early port found one world where the capsule separation,
timed from the navigated state, came late; the reference now separates MAV2 before Earth entry).
"""
from __future__ import annotations

import os

DEFAULT = True
_TRUE = ("1", "true", "on", "yes")
_FALSE = ("0", "false", "off", "no")


def on() -> bool:
    v = os.environ.get("MARSFLIGHT_NAV")
    if v is None or not v.strip():
        return DEFAULT
    v = v.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise ValueError(f"MARSFLIGHT_NAV={v!r}: use 1 or 0")
