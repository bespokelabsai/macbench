"""Grade the live game's state on the hub. Reads only /var/lib/mindustry-many-fronts/score.json. No team file is read,
imported or run, and MULTI_AGENT_HARNESS_GRADE_APP is ignored on purpose: the grade is of the game state, not a commit.

S = sum over worlds of mult(world) * sum over items of weight(item) * min(source(item), core_in(item))
mult(world) = max(0.25, 0.85 ** losses before the world)
grade = min(1, log(1 + S / S0) / log(1 + SMAX / S0))
"""
import json
import math
import os

STATE = os.environ.get("MMF_STATE_FILE", "/var/lib/mindustry-many-fronts/score.json")
S0, SMAX = 1e6, 1.4e8
PENALTY, FLOOR = 0.85, 0.25
# Thorium, phase fabric and surge alloy are not listed: no world has thorium ore, and every way to make them needs thorium
# to build (disassembler, phase weaver, surge smelter), so they cannot be had.
WEIGHTS = {"copper": 1, "lead": 1, "sand": 1, "scrap": 1, "spore-pod": 1, "coal": 2, "titanium": 4, "metaglass": 4,
           "graphite": 5, "silicon": 8, "pyratite": 8, "blast-compound": 15, "plastanium": 25}


def multiplier(losses):
    return max(FLOOR, PENALTY ** losses)


def world_points(w):
    return sum(weight * min(w["source"].get(i, 0), w["core_in"].get(i, 0)) for i, weight in WEIGHTS.items())


def score(state):
    return sum(multiplier(w["losses_before"]) * world_points(w) for w in state["worlds"])


def grade(s):
    return min(1.0, math.log1p(max(0.0, s) / S0) / math.log1p(SMAX / S0))


def check(state):
    """Refuse a state this grader did not expect. A failure must never look like a grade of 0."""
    if not isinstance(state, dict) or not isinstance(state.get("worlds"), list) or not isinstance(state.get("losses"), int):
        raise ValueError("score.json has no worlds and losses")
    for w in state["worlds"]:
        if not isinstance(w.get("losses_before"), int):
            raise ValueError("a world has no losses_before")
        for key in ("source", "core_in"):
            if not isinstance(w.get(key), dict) or not all(isinstance(n, (int, float)) and n >= 0 for n in w[key].values()):
                raise ValueError(f"a world has a bad {key}")


def perf(path):
    """The game's own clock against the wall, from the host's timeline: 1.0 is real time."""
    try:
        with open(path) as f:
            lines = [json.loads(x) for x in f]
        out = []
        for a, b in zip(lines, lines[1:]):
            if a["world"] == b["world"] and a.get("sim_ticks") is not None and b.get("sim_ticks") is not None and b["t"] > a["t"]:
                out.append({"t": b["t"], "world": b["world"], "realtime": round((b["sim_ticks"] - a["sim_ticks"]) / 60 / (b["t"] - a["t"]), 3),
                            "updates_per_s": round((b["tick"] - a["tick"]) / (b["t"] - a["t"]), 1),
                            **{k: b.get(k) for k in ("heap_mb", "jvm_rss_mb", "jvm_cpu_s", "load1", "mem_available_mb")}})
        return out
    except Exception:  # noqa: BLE001 - diagnostics only: whatever the timeline holds, it must not cost the run its grade
        return None


def report(state):
    check(state)
    s = score(state)
    return {"grade": grade(s), "score": s, "losses": state["losses"],
            "worlds": [{k: w[k] for k in ("index", "losses_before", "ended", "tick", "wave", "source", "core_in")}
                       | {"core_in_tainted": w.get("core_in_tainted", {}), "points": world_points(w), "multiplier": multiplier(w["losses_before"])} for w in state["worlds"]],
            "fleet": state.get("fleet"), "calls": state.get("calls", {}), "refused": state.get("refused", {}), "frozen_at": state.get("frozen_at"),
            "perf": perf(os.path.join(os.path.dirname(STATE), "timeline.jsonl"))}


def main():
    out = os.environ.get("MULTI_AGENT_HARNESS_GRADE_OUT") or "/logs/verifier"
    os.makedirs(out, exist_ok=True)
    try:
        with open(STATE) as f:
            details = report(json.load(f))
    except FileNotFoundError:
        details = {"grade": 0.0, "score": 0.0, "losses": 0, "worlds": [], "note": "no game was played"}
    with open(os.path.join(out, "details.json"), "w") as f:
        json.dump(dict(details, mark=os.environ.get("MULTI_AGENT_HARNESS_GRADE_MARK")), f, indent=1)
    with open(os.path.join(out, "reward.txt"), "w") as f:
        f.write(f"{details['grade']:.6f}\n")
    print(f"grade {details['grade']:.6f} score {details['score']:.1f} worlds {len(details['worlds'])} losses {details['losses']}")


if __name__ == "__main__":
    main()
