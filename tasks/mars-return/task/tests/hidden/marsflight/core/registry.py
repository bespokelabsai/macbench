"""Finds models and providers by itself, so adding one edits no shared file.

Every module under ``models/`` is imported once. A module registers what it defines by listing them:

    MODELS = [Slosh]               # Model subclasses
    PROVIDERS = [MarsGramAtmosphere]   # Provider subclasses

Which provider is active for a role: the environment variable ``MARSFLIGHT_<ROLE>`` (dots become
underscores, e.g. ``MARSFLIGHT_ATMOSPHERE_MARS=mcd``) if set, else the one marked ``default = True``.
Two defaults for one role is an error, which the dev tests catch. A model can be switched off with
``MARSFLIGHT_DISABLE=name,name``, for before-and-after comparisons.
"""
from __future__ import annotations

import importlib
import os
import pkgutil


class Registry:
    def __init__(self, package="marsflight.models"):
        self.package = package
        self._models = None
        self._providers = None

    def _load(self):
        if self._models is not None:
            return
        self._models, self._providers = [], {}
        try:
            pkg = importlib.import_module(self.package)
        except ModuleNotFoundError:
            return
        for info in sorted(pkgutil.walk_packages(pkg.__path__, pkg.__name__ + "."), key=lambda i: i.name):
            mod = importlib.import_module(info.name)
            self._models += list(getattr(mod, "MODELS", []))
            for p in getattr(mod, "PROVIDERS", []):
                self._providers.setdefault(p.role, []).append(p)

    def models(self):
        """Model classes in run order, minus any switched off."""
        self._load()
        off = {s for s in os.environ.get("MARSFLIGHT_DISABLE", "").split(",") if s}
        names = [m.name for m in self._models]
        dup = {n for n in names if names.count(n) > 1}
        if dup:
            raise RuntimeError(f"two models share a name: {sorted(dup)}")
        return sorted((m for m in self._models if m.name not in off), key=lambda m: (m.order, m.name))

    def providers(self, role):
        self._load()
        return list(self._providers.get(role, []))

    def provider(self, role):
        """The active provider class for a role."""
        options = self.providers(role)
        want = os.environ.get("MARSFLIGHT_" + role.upper().replace(".", "_"))
        if want:
            for p in options:
                if p.name == want:
                    return p
            raise RuntimeError(f"no provider {want!r} for {role}; have {[p.name for p in options]}")
        defaults = [p for p in options if p.default]
        if len(defaults) > 1:
            raise RuntimeError(f"{role} has two default providers: {[p.name for p in defaults]}")
        if defaults:
            return defaults[0]
        return None


registry = Registry()
