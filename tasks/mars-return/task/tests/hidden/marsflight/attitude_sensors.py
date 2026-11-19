"""Moved to `models/avionics/attitude_sensors.py`. `attitude_sensors` stays an alias of the moved module (the same module object), so
imports and patches of its names reach the code that runs."""
import sys

from .models.avionics import attitude_sensors as _moved

sys.modules[__name__] = _moved
