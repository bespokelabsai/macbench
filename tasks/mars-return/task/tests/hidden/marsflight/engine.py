"""Moved to `models/propulsion/engine.py`. `engine` stays an alias of the moved module (the same module object), so
imports and patches of its names reach the code that runs."""
import sys

from .models.propulsion import engine as _moved

sys.modules[__name__] = _moved
