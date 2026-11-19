"""Holdout verifier: the reward is the game's own grade, read from the game's grading state.

The game host (on the hub in a team run, or the one solution/solve.sh starts for the oracle) rewrites
/var/lib/holdout/score.json every 2 s, atomically. The grade is the best build-ladder progress the team has reached
(0..1, never decreasing; 1.0 = rescued). Nothing a team writes is read: not the repository, not any file in a sandbox.

A state that cannot be read writes no reward, so the harness records the grading as failed, not as 0. On the hub no
agent can read this folder. The final grading also copies the replay and the run's logs next to the reward
(replay.jsonl, perf.jsonl, messages.jsonl with every message in full, strangers.jsonl with one line per stranger), so
the trial folder holds the whole game; otherwise it would be lost with the hub.
"""
import json
import os
import shutil
import time

STATE = os.environ.get("HOLDOUT_STATE_DIR", "/var/lib/holdout")
OUT = os.environ.get("MULTI_AGENT_HARNESS_GRADE_OUT") or "/logs/verifier"
MARK = bool(os.environ.get("MULTI_AGENT_HARNESS_GRADE_OUT"))

with open(os.path.join(STATE, "score.json")) as f:
    st = json.load(f)
grade = float(st.get("grade") or 0.0)
grade = max(0.0, min(1.0, grade))
details = {"grade": grade, "age_s": round(time.time() - float(st.get("written_at", time.time())), 1), "mark": MARK, **st}
with open(os.path.join(OUT, "details.json"), "w") as f:
    json.dump(details, f, indent=1)
if not MARK:
    for name in ("replay.jsonl", "perf.jsonl", "messages.jsonl", "strangers.jsonl"):
        p = os.path.join(STATE, name)
        if os.path.exists(p):
            shutil.copy(p, os.path.join(OUT, name))
with open(os.path.join(OUT, "reward.txt"), "w") as f:
    f.write(f"{grade:.6f}\n")
calls = sum((st.get("calls") or {}).values())   # game API calls by every survivor: shows the team played
print(f"holdout grade {grade:.4f} (day {st.get('day')} {st.get('clock')}, rescued={st.get('rescued')}, "
      f"survivors {st.get('seats')}, game calls {calls})")
