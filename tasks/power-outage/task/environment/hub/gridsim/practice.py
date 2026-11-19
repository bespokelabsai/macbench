"""One practice restoration for the operations office: the team's controllers on a practice scenario.

    python3 -m gridsim.practice --app DIR --scenario K --out FILE [--only a01,a02] [--hours H]
"""
import argparse
import json

from . import runner
from . import scenario as SC

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", required=True)
    ap.add_argument("--scenario", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="")
    ap.add_argument("--hours", type=int, default=16)
    a = ap.parse_args()
    only = set(x for x in a.only.split(",") if x) or None
    res, _ = runner.run(a.app, SC.practice_seed(a.scenario), only=only, collect_events=True, hours=a.hours)
    res["scenario"] = a.scenario
    res["hours"] = a.hours
    res.pop("seed", None)
    with open(a.out, "w") as f:
        json.dump(res, f)
