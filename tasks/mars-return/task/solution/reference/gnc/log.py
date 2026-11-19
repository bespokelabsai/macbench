"""Optional debug log. Set MF_LOG=/path/file to write one JSON line per message (never stdout/stderr,
which the simulator uses for the protocol and could block on)."""
import json
import os
import time

_PATH = os.environ.get("MF_LOG")
_F = None


def log(kind, **kw):
    global _F
    if not _PATH:
        return
    try:
        if _F is None:
            _F = open(_PATH, "a")
        kw["kind"] = kind
        kw["wall"] = round(time.time(), 3)
        _F.write(json.dumps(kw, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o)) + "\n")
        _F.flush()
    except Exception:  # noqa: BLE001
        pass
