"""Black Start verifier: the team's controllers on the hidden blackout scenarios.

Runs on the hub (shared verifier mode), with the simulator in the image at /opt/blackstart (root-only) and the
hidden scenarios' salt in tests/hidden/salt. Each scenario runs
through gridsim.runner, the same runner the practice office uses: one process per area, each area as its own
unprivileged user, talking only through the simulator's message channel.

Reward: the mean scenario score. A scenario's raw score is the restoration index (the mean over the 16 simulated hours
of the squared priority-weighted share of demand served), multiplied down for island collapses, missed nuclear cooling
deadlines and equipment damage, and 0 for a closure forced out of phase; its score is the raw score divided by the
scenario's capacity bound (gridsim/bound.py), so 1.0 is the physical maximum.

Graded marks: a mark during the run (MULTI_AGENT_HARNESS_GRADE_APP set) is a quick grading on the first
BLACKSTART_MARK_SCENARIOS scenarios (when set), to see progress cheaply; the final grading uses
BLACKSTART_VERIFY_SCENARIOS. Every grading saves the solution it graded (solution.tar.gz, the repository without
.git) and solution.json next to its reward, so tests/test.sh can grade every mark in full after the final grading.

Environment: BLACKSTART_VERIFY_SCENARIOS (default 48), BLACKSTART_MARK_SCENARIOS (unset: marks use the full count),
BLACKSTART_VERIFY_WORKERS (default: half the container's usable CPUs), MULTI_AGENT_HARNESS_GRADE_APP (the commit to grade; default /app)
and MULTI_AGENT_HARNESS_GRADE_OUT (default /logs/verifier).
"""
import concurrent.futures
import json
import os
import subprocess
import sys
import tarfile
import time

HUB = "/opt/blackstart"
sys.path.insert(0, HUB)
from gridsim import scenario as SC  # noqa: E402

APP = os.environ.get("MULTI_AGENT_HARNESS_GRADE_APP") or "/app"
OUT = os.environ.get("MULTI_AGENT_HARNESS_GRADE_OUT") or "/logs/verifier"
QUICK = bool(os.environ.get("MULTI_AGENT_HARNESS_GRADE_APP")) and bool(os.environ.get("BLACKSTART_MARK_SCENARIOS"))
N = int(os.environ["BLACKSTART_MARK_SCENARIOS"] if QUICK else os.environ.get("BLACKSTART_VERIFY_SCENARIOS", "48"))
# Half as many scenarios at once as CPUs: each scenario runs 16 controller processes and the simulator, and a team's
# controllers may use far more CPU than the reference's, so packing a scenario per CPU starved them (a 150 s scenario
# took over 1,500 s with 7 at once on 8 CPUs).
def usable_cpus():
    """CPUs this container may use: the cgroup quota and the affinity mask, not the host's count (os.cpu_count()
    gives the host's count in some sandboxes, where packing scenarios by it ran 24 at once on 8 CPUs)."""
    n = os.cpu_count() or 2
    try:
        quota, period = open("/sys/fs/cgroup/cpu.max").read().split()
        if quota != "max":
            n = min(n, max(1, int(quota) // int(period)))
    except (OSError, ValueError):
        pass
    try:
        n = min(n, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        pass
    return n


WORKERS = int(os.environ.get("BLACKSTART_VERIFY_WORKERS") or max(1, usable_cpus() // 2))
# A scenario's wall-clock limit follows from the controller contract (MANUAL.md, The controller): 1,920 steps of at
# most 2 seconds each, the controllers' start-up, and a margin for the simulator. Controllers that keep to the
# contract cannot run into it, whatever else the machine is doing.
from gridsim import runner as RUN  # noqa: E402
PER_SCENARIO_S = int(SC.HORIZON_MIN * 60 / SC.STEP_S * RUN.STEP_TIMEOUT + RUN.INIT_TIMEOUT + 300)


def one(k, seed):
    out = os.path.join(OUT, "scenarios", f"s{k:02d}.json")
    t0 = time.time()
    try:
        # the seed and the output path (which names the scenario) go in the environment, not on the command line,
        # which every controller could read in /proc
        r = subprocess.run([sys.executable, "-m", "gridsim.runner", "--app", APP],
                           capture_output=True, text=True, timeout=PER_SCENARIO_S, cwd=HUB,
                           env=dict(os.environ, PYTHONPATH=HUB, BLACKSTART_SEED=str(seed), BLACKSTART_OUT=out))
        if r.returncode != 0:
            return dict(k=k, score=0.0, error=r.stderr[-1500:], wall_s=round(time.time() - t0, 1))
        with open(out) as f:
            res = json.load(f)
        res.pop("seed", None)
        res.pop("timeline", None)
        res["k"] = k
        return res
    except subprocess.TimeoutExpired:
        return dict(k=k, score=0.0, error=f"scenario took longer than {PER_SCENARIO_S} s", wall_s=PER_SCENARIO_S)


def save_solution():
    """The graded repository (without .git), so the mark can be graded again in full later."""
    path = os.path.join(OUT, "solution.tar.gz")
    with tarfile.open(path, "w:gz") as tf:
        tf.add(APP, arcname="app", filter=lambda ti: None if "/.git" in ti.name or ti.name.endswith("/.git")
               or "__pycache__" in ti.name else ti)
    os.chmod(path, 0o644)
    with open(os.path.join(OUT, "solution.json"), "w") as f:
        json.dump(dict(mark=os.environ.get("MULTI_AGENT_HARNESS_GRADE_MARK") or "final",
                       commit=os.environ.get("MULTI_AGENT_HARNESS_GRADE_COMMIT"), kind="quick" if QUICK else "full",
                       scenarios=N), f)



STUB = """
def make_controller(area, grid, private):
    return Controller(area, grid, private)


class Controller:
    def __init__(self, area, grid, private):
        self.area = area

    def step(self, obs, inbox):
        actions = []
        outbox = []
        return actions, outbox
"""


def own_controllers(app):
    """How many areas have a controller of their own, not the empty stub the repository starts with (docstrings and
    comments aside). Printed with the grade, so a run's log shows whether the team wrote anything."""
    import ast

    def shape(src):
        tree = ast.parse(src)
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                    and isinstance(getattr(body[0], "value", None), ast.Constant) and isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
        return ast.dump(tree)

    stub, n = shape(STUB), 0
    for i in range(1, 17):
        p = os.path.join(app, "areas", f"a{i:02d}", "controller.py")
        try:
            with open(p) as f:
                n += shape(f.read()) != stub
        except (OSError, SyntaxError, ValueError):
            n += os.path.exists(p)   # present but not the stub (unreadable or not Python)
    return n


def main():
    os.makedirs(os.path.join(OUT, "scenarios"), exist_ok=True)
    try:
        save_solution()
    except Exception as e:  # never let saving cost the grade
        print(f"could not save the solution: {e!r}")
    os.chmod(os.path.join(OUT, "scenarios"), 0o755)
    if not os.path.isdir(os.path.join(APP, "areas")):
        print(f"no areas/ folder in {APP}: reward 0")
        return
    print(f"submission: {own_controllers(APP)} of 16 areas have a controller of their own (not the empty stub)", flush=True)
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "hidden", "salt")) as f:
        seeds = SC.hidden_seeds(f.read().strip(), N)
    t0 = time.time()
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(one, k, s) for k, s in enumerate(seeds)]
        for f in concurrent.futures.as_completed(futs):
            r = f.result()
            results.append(r)
            print(f"scenario {r['k']:2d}: score {r['score']:.4f}"
                  + (f"  raw {r.get('raw_score', 0):.4f} of bound {r.get('bound', 0):.4f}, index {r.get('restoration_index', 0):.3f}, served {r.get('served_fraction', 0):.3f}, collapses {r.get('collapses')}, "
                     f"nuclear missed {len(r.get('nuclear_missed') or [])}, damage {r.get('damage')}"
                     if 'error' not in r else f"  ERROR {r['error'][-200:]}"), flush=True)
    results.sort(key=lambda r: r["k"])
    reward = sum(r["score"] for r in results) / len(results)
    details = dict(reward=round(reward, 6), kind="quick" if QUICK else "full", scenarios=len(results), wall_s=round(time.time() - t0, 1),
                   mean_raw_score=round(sum(r.get("raw_score", 0) for r in results) / len(results), 6),
                   mean_bound=round(sum(r.get("bound", 0) for r in results) / len(results), 6),
                   mean_restoration_index=round(sum(r.get("restoration_index", 0) for r in results) / len(results), 6),
                   mean_served_fraction=round(sum(r.get("served_fraction", 0) for r in results) / len(results), 6),
                   collapses=sum(r.get("collapses", 0) for r in results),
                   nuclear_missed=sum(len(r.get("nuclear_missed") or []) for r in results),
                   damage=sum(r.get("damage", 0) for r in results),
                   out_of_phase=sum(bool(r.get("out_of_phase")) for r in results),
                   errors=sum("error" in r for r in results),
                   per_scenario=[{k: r.get(k) for k in ("k", "score", "raw_score", "bound", "restoration_index", "served_fraction", "collapses", "nuclear_missed",
                                                         "damage", "out_of_phase", "wall_s", "controllers", "error")}
                                 for r in results])
    with open(os.path.join(OUT, "details.json"), "w") as f:
        json.dump(details, f, indent=1)
    with open(os.path.join(OUT, "reward.txt"), "w") as f:
        f.write(f"{reward:.6f}\n")
    print(f"reward {reward:.4f} ({'quick mark grading' if QUICK else 'full grading'}) over {len(results)} hidden scenarios in {details['wall_s']:.0f} s")


if __name__ == "__main__":
    main()
