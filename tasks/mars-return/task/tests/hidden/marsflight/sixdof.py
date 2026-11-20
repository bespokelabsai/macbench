"""Moved to `dynamics/rigid_body.py`. `sixdof` stays an alias of the moved module (the same module object), so
imports and patches of its names reach the code that runs."""
import sys

from .dynamics import rigid_body as _moved

sys.modules[__name__] = _moved
