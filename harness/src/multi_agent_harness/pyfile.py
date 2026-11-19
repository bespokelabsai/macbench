"""Load a user's .py file by location: orchestration strategies, board policies and board plugins.

One set of rules for all three: the spec is a path to a .py file, relative to the working
directory; the file is imported under a module name of its own (importlib, by file location), so
two files with the same stem don't collide; any error while importing it is a config error.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import sys
from pathlib import Path
from types import ModuleType


def locate(spec: str, what: str) -> Path:
    """The absolute path of `spec`, which must name an existing .py file. Raises ValueError."""
    path = Path(spec.strip()).expanduser()
    if path.suffix != ".py":
        raise ValueError(f"{what} {spec!r}: not a path to a .py file")
    path = path.resolve()
    if not path.is_file():
        raise ValueError(f"{what} {spec!r}: no such file {path}")
    return path


def import_file(path: Path, what: str) -> ModuleType:
    """Import the file at `path` as a fresh module. Raises ValueError with the import error."""
    tag = what.replace(" ", "_")
    mod_name = f"multi_agent_harness_{tag}_{path.stem}_{hashlib.sha1(str(path).encode()).hexdigest()[:8]}"
    try:
        spec = importlib.util.spec_from_file_location(mod_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
    except Exception as e:  # noqa: BLE001 - any failure loading the file is a config error
        sys.modules.pop(mod_name, None)
        raise ValueError(f"{what} {path} failed to load: {type(e).__name__}: {e}") from e
    return module


def one_subclass(module: ModuleType, base: type, base_name: str, path: Path, what: str) -> type:
    """The one subclass of `base` the module itself defines (imported classes don't count)."""
    found = [c for _, c in inspect.getmembers(module, inspect.isclass)
             if c.__module__ == module.__name__ and issubclass(c, base)]
    if len(found) != 1:
        names = ", ".join(c.__name__ for c in found) or "none"
        raise ValueError(f"{what} {path} must define exactly one subclass of "
                         f"{base_name}, found {len(found)}: {names}")
    return found[0]
