"""Load every area's controller and run it on the sample observations in docs/sample_obs/.

    python3 tools/check_controller.py [area ...]

It checks that make_controller() and step() run, that step() returns (actions, outbox) as two lists of dicts, and
how long a step takes. It does not simulate anything: use practice_run for that.
"""
import glob
import importlib.util
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main():
    grid = json.load(open(os.path.join(ROOT, "grid", "public_grid.json")))
    areas = sys.argv[1:] or sorted(os.listdir(os.path.join(ROOT, "areas")))
    samples = sorted(glob.glob(os.path.join(ROOT, "docs", "sample_obs", "*.json")))
    bad = 0
    for area in areas:
        path = os.path.join(ROOT, "areas", area, "controller.py")
        if not os.path.exists(path):
            print(f"{area}: no controller.py")
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"controller_{area}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            ctl = mod.make_controller(area, grid, {"area": area, "units": [], "lines": [], "crews": []})
        except Exception as e:
            print(f"{area}: make_controller failed: {e!r}")
            bad += 1
            continue
        worst = 0.0
        for s in samples:
            d = json.load(open(s))
            t = time.time()
            try:
                res = ctl.step(d["obs"], d["inbox"])
                actions, outbox = res
                assert isinstance(actions, list) and all(isinstance(a, dict) for a in actions), "actions must be a list of dicts"
                assert isinstance(outbox, list) and all(isinstance(m, dict) for m in outbox), "outbox must be a list of dicts"
            except Exception as e:
                print(f"{area}: step failed on {os.path.basename(s)}: {e!r}")
                bad += 1
                break
            worst = max(worst, time.time() - t)
        else:
            print(f"{area}: ok, slowest step {worst * 1000:.0f} ms (limit 2000 ms)")
    print("note: the samples are other areas' observations, so ids will not match an area's own equipment")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
