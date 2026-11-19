"""Build the copy of the task that the multi-agent harness runs, and the hub plugin that carries the hidden live day.

    python3 task/environment/hub/make_team_task.py

writes, under task/build/ (git-ignored):
- dxb-atc-live/: the task for a team. On the harness the hub serves the live day, so the image carries no live day
  (the Dockerfile's live-day block is removed) and the verifier runs on the hub (shared mode). The copy holds no
  solution/ and no hub/. The instruction gives the run's grading schedule, and the agent timeout leaves room for the
  orchestration to stop the day when the 4 hours are up.
- atc_hub_plugin.py: a board plugin that unpacks the live day and the simulator engine on the hub (hub_bundle.py).

The settings are those of the published runs: 4 hours, graded at 30 minutes and at hours 1, 2, 3 and 4.
"""
import os
import re
import shutil
import sys

HUB = os.path.dirname(os.path.abspath(__file__))
TASK = os.path.dirname(os.path.dirname(HUB))
BUILD = os.path.join(TASK, "build")
TEAM = os.path.join(BUILD, "dxb-atc-live")
HOURS = 4
SCHEDULE = ("The day is graded at minute 30, hour 1, hour 2, hour 3 and hour 4 of the run, in the same way each time. "
            "Your score is the grade at hour 4, when the run ends. The team does not see the earlier grades.")

sys.path.insert(0, HUB)
import hub_bundle  # noqa: E402


def edit(path, old, new, count=1, regex=False):
    with open(path) as f:
        s = f.read()
    s, n = re.subn(old, new, s, flags=re.S) if regex else (s.replace(old, new), s.count(old))
    if n != count:
        sys.exit(f"{os.path.relpath(path, TASK)}: expected {count} match(es) of {old!r}, found {n}")
    with open(path, "w") as f:
        f.write(s)


def main():
    shutil.rmtree(BUILD, ignore_errors=True)
    skip = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")
    for part in ("instruction.md", "task.toml", "environment", "tests"):
        src, dst = os.path.join(TASK, part), os.path.join(TEAM, part)
        if os.path.isdir(src):
            shutil.copytree(src, dst, ignore=skip)
        else:
            os.makedirs(TEAM, exist_ok=True)
            shutil.copy2(src, dst)
    shutil.rmtree(os.path.join(TEAM, "environment", "hub"))

    edit(os.path.join(TEAM, "instruction.md"), "You have 4 hours. The day stops when your session ends.",
         f"You have 4 hours. {SCHEDULE}")
    edit(os.path.join(TEAM, "environment", "Dockerfile"), r"# >>> live day\n.*?# <<< live day\n", "", regex=True)
    with open(os.path.join(TEAM, "tests", "run.json"), "w") as f:
        f.write('{"hours": %g}\n' % HOURS)
    toml = os.path.join(TEAM, "task.toml")
    # The harness ends the run through the orchestration (--ak timeout_sec), which stops the day; Harbor's own timeout
    # comes 10 minutes later.
    edit(toml, r"(\[agent\][^\[]*?timeout_sec = )[0-9.]+", rf"\g<1>{HOURS * 3600 + 600:.1f}", regex=True)
    # No agent sandbox reaches the hub's record of the day, so the verifier grades it on the hub.
    edit(toml, 'environment_mode = "separate"', 'environment_mode = "shared"')

    hub_bundle.main(BUILD)
    print(f"team task: {os.path.relpath(TEAM, os.path.dirname(TASK))}")


if __name__ == "__main__":
    main()
