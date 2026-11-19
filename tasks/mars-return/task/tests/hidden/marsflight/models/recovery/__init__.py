"""Optional first stage recovery with a propulsive landing, in the Falcon 9 style.

Behind MARSFLIGHT_RECOVERY, off by default. The design field is ``"recovery": "none" | "rtls" | "droneship"`` on
the first stage element. See ``budget.py`` for the numbers and ``model.py`` for the hooks the core calls.
The model registers itself in ``model.py`` only (the registry imports every module, so a second ``MODELS`` here
would register it twice).
"""
