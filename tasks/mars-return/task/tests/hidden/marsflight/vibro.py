"""Moved to `models/vibration/vibro.py`. `vibro` stays an alias of the moved module (the same module object), so
imports and patches of its names reach the code that runs."""
import sys

from .models.vibration import vibro as _moved

sys.modules[__name__] = _moved
