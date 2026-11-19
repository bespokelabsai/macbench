"""Child process that hosts the team's flight software. Reads JSON lines on stdin, writes JSON lines
on stdout. Anything the flight software prints goes to stderr so it can't corrupt the protocol."""
import importlib.util
import json
import os
import sys
import traceback


def main():
    path = sys.argv[1]
    out = sys.stdout
    sys.stdout = sys.stderr
    spec = importlib.util.spec_from_file_location("flight_software", path)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, os.path.dirname(path))
    try:
        spec.loader.exec_module(mod)
    except Exception:  # noqa: BLE001
        out.write(json.dumps({"error": "import failed:\n" + traceback.format_exc()}) + "\n")
        out.flush()
        return
    for line in sys.stdin:
        try:
            msg = json.loads(line)
            if msg["kind"] == "reset":
                res = mod.reset(msg["data"]) if hasattr(mod, "reset") else None
            else:
                res = mod.step(msg["data"])
            out.write(json.dumps({"data": res}, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o)) + "\n")
        except Exception:  # noqa: BLE001
            out.write(json.dumps({"error": traceback.format_exc()}) + "\n")
        out.flush()


if __name__ == "__main__":
    main()
