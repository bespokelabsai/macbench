"""Check a vehicle model: syntax, names, types, units, required attributes, designations and joints.

This is the checker a team's image would carry. It says whether the program office can read the
model, and lists every problem with its file and line. It computes nothing about the vehicle: no
mass, size, strength, aerodynamics or performance.

    python3 -m designlang.check MODEL.sysml [MORE.sysml ...] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys

from .walk import walk


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("model", nargs="+")
    ap.add_argument("--json", action="store_true", help="print the checked description as JSON")
    a = ap.parse_args(argv)
    desc, errors, warnings = walk(a.model)
    for w in warnings:
        print(f"warning: {w}")
    if errors:
        for e in errors:
            print(f"error: {e}")
        print(f"{len(errors)} error(s)")
        return 1
    if a.json:
        print(json.dumps(desc, indent=1))
    else:
        print(f"OK: launch {desc['launch_epoch_utc']}, {len(desc['elements'])} stack elements from the pad up:")
        for e in desc["elements"]:
            extra = ""
            if e["kind"] == "stage" and e["engines"]:
                extra = ", engines " + ", ".join(f"{x['count']} x {x['engine']}" for x in e["engines"])
            print(f"  {e['id']:8s} {e['kind']:10s} ({e['type']}){extra}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
