# CHICAGO SKYLINE SURVEY

A multi-agent task: plan one 12-hour survey day for a fleet of 8 camera drones over a 3D model of the Chicago Loop,
built from real OpenStreetMap buildings. About 1,900 targets are worth photographing: rooftop equipment, facade
panels, bridge spans, and the L trains and river water taxis that move through the area. Mapping 12 landmarks in 3D
and holding light-show formations also score. Firm airspace rules (altitude band, building clearance, drone
separation, no-fly zones), battery and charging-pad limits and riverside noise budgets constrain every plan.

The deliverable is one waypoint plan per drone, `plans/D1.csv` to `plans/D8.csv`. The plans are flown on 24 hidden
wind days that differ from the forecast the way the 32 practice days in the task do, and a photo counts only if its
drone lands afterwards. A local checker (`python -m skyline check`) flies the plans on the forecast or on any
practice day.

## How it works

- **One fleet, one plan set.** Every agent has the same instruction, data and checker and can edit every drone's
  plan. The team's work is the plan files on the shared git `main`.
- **The drones depend on each other.** Trains and boats count only when two drones photograph them together, a
  landmark is mapped only by a rig of at least 3 drones scanning within 10 s of each other, a light show pays only
  when every slot is filled on time, the four pads hold two drones each, and drones must keep their separation.
  Plans written apart collide, queue at pads or miss their partners.
- **The same problem at every team size.** One task and one instruction for k = 1, 2, 4 or 8 agents. Every agent
  gets 8 CPUs and 16 GB at every team size.
- **The run lasts its full time.** An agent that ends its session is woken again (`task/keepalive.py`): when a
  teammate messages it, every 30 minutes otherwise, and when nobody on the team is working. A run ends when its
  4 hours are up or its model budget is spent. The CLIs' own subagents are off, so a team of k is k agents.
- **Graded while it works.** `main` is graded at 30 minutes and at hours 1, 2, 3 and 4. The instruction states the
  schedule, but the agents never see a grade. The grade at 4 hours is the score.

## Grading

Each hidden day's points are the photo, landmark and show points the plans earn (a photo counts only if its drone
lands afterwards), minus 500 for every drone lost (never below 0). The score is the mean points over the 24 hidden
days divided by 18,924.4, the theoretical maximum of a day (every target shot at its best light, with no flight
limits), so no plan comes near 1. A malformed plan file leaves its drone on its pad, and any grader error leaves the
reward at 0.

`task/tests/test.sh` runs `task/tests/grade.py`, which flies the plans with the pristine checker in `tests/skyline/`,
the world data in `tests/data/` and the hidden days in `tests/hidden/`. The point rules are in
`tests/skyline/scoring.py` and section 7 of `environment/app/docs/MANUAL.md`. The grader writes `reward.txt`,
`reward.json` and `details.json` (points per day, each drone's outcome and why it was lost) and prints
`plans: N drone files, M waypoints` and `points P  score S  days [...]`.

## How to run

You need Docker and Python 3.12. Set up once, from this folder:

```bash
python3 -m venv .venv && source .venv/bin/activate
git clone https://github.com/bespokelabsai/multi-agent-harness ~/multi-agent-harness
git -C ~/multi-agent-harness checkout 0f7ac9583906b998772245c41d799f7b2264dd4f
pip install -e "$HOME/multi-agent-harness[docker]"
cd task
echo "ANTHROPIC_API_KEY=..." > .env     # and OPENAI_API_KEY / GEMINI_API_KEY for the other models
```

This installs [multi-agent-harness](https://github.com/bespokelabsai/multi-agent-harness) at the commit the task was
tested with, and Harbor with it. The clone can live anywhere, but keep it: the harness builds its hub from it. All
commands below run from `task/`.

### Multi-agent harness

A team of `K` agents (1, 2, 4 or 8) plans the fleet together over a message board and a shared git `main`:

```bash
./build.sh          # build-mah/chicago-skyline-survey, for every team size

for K in 1 2 4 8; do
  harbor run -p build-mah/chicago-skyline-survey \
    -e multi_agent_harness.harbor.environment:TeamEnvironment --ek backend=docker --ek n_agents=$K \
    --ek budget_usd=2500 --ek board_plugins=keepalive_plugin.py \
    -a multi_agent_harness.harbor.agent:TeamAgent --ak harness=claude-code --ak model=claude-opus-5-5 --ak effort=max \
    --ak orchestration=keepalive.py --ak timeout_sec=14400 --ak grade_at=30m,1h,2h,3h,4h \
    --agent-setup-timeout-multiplier 3 --env-file .env
done
```

- These are the settings of our published runs: 4 hours, a $2,500 model budget per team (API prices), graded marks
  at 30 minutes and hours 1 to 4, and the keepalive orchestration at every team size, k = 1 included. Our three
  models ran as Claude Opus 5.5 at max effort (above), GPT-6.1 Sol at xhigh effort and Gemini 4 Argon at high
  thinking. For the other two, swap the harness and model options:

  ```bash
  --ak harness=codex --ak model=gpt-6.1-sol --ak effort=xhigh          # OPENAI_API_KEY
  --ak harness=antigravity --ak model=gemini-4-argon-high              # GEMINI_API_KEY; the slug sets the thinking level
  ```

- `keepalive.py` wakes agents that ended their session until the time or budget is spent, and `keepalive_plugin.py`
  is the board route it reads unread messages from. Without the plugin agents are woken on the timers only.
- Each mark's grade is in the trial's `marks/<mark>/` folder; the final grade (hour 4) is the trial's
  `verifier/reward.txt`.
- The team copy leaves out the reference plans and opens the agents' and verifier's network (`public`): on the
  Docker backend the agents reach the hub (board, model proxy, team repository) over it, and they also have
  internet access, as the harness warns.
- To use a Claude plan instead of an API key, put `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) in `.env`
  and add `--ek model_upstream=subscription`. On the Docker backend also `export MULTI_AGENT_HARNESS_CLAUDE_ENV_SCRUB=0`:
  Claude Code's shell sandbox cannot start in a Docker agent box, and this runs it without (the agents' shell
  commands can then read the token). Other backends and harnesses are described in the harness's README.

### Harbor

One agent, with plain Harbor:

```bash
./build_single.sh   # build-single/chicago-skyline-survey

harbor run -p build-single/chicago-skyline-survey -a claude-code -m anthropic/claude-opus-5-5 \
  --ak reasoning_effort=max --env-file .env
```

The agent works as an unprivileged user in `/app` for 4 hours and may reach only the model APIs (Anthropic, OpenAI
and Google). It is graded once, at the end, so the single-agent instruction leaves out the paragraph on graded
marks. The score is in the trial's `verifier/reward.txt`.

`harbor run -p build-single/chicago-skyline-survey -a oracle` runs `solution/solve.sh`, which copies a set of
reference plans into `/app/plans`. They score about 0.153 (2,901 points a day). They are a baseline, not an optimum:
the published agent runs score about twice as much. For a quick look at a real agent, add
`--agent-timeout-multiplier 0.01` (about 2.5 minutes instead of 4 hours); a run that short will usually score 0.

## Contents

| Path | What it is |
|---|---|
| `task/` | The Harbor task folder |
| `task/instruction.md`, `task/task.toml` | What the agents are told, and the task settings (4 hours, 8 CPUs and 16 GB per agent) |
| `task/environment/` | The image: `app/` is what lands in `/app` (world data, 32 practice wind days, the manual, the checker package `skyline/` and an empty `plans/`) |
| `task/tests/` | The grader: `test.sh`, `grade.py`, the pristine checker and data, and the 24 hidden wind days with their normalisation (`hidden/`) |
| `task/solution/` | `solve.sh` and the reference plans |
| `task/build.sh`, `task/build_single.sh` | Build the team task and the single-agent task |
| `task/keepalive.py`, `task/keepalive_plugin.py` | What the harness loads: the keepalive orchestration and its board plugin |
| `NOTICE.md` | Licences and notices for the bundled data |

## Credits

- Buildings, river and rail: © OpenStreetMap contributors, under the Open Database License 1.0.
- Wind forecast: Open-Meteo.com (CC BY 4.0), containing modified Copernicus Climate Change Service information
  (ERA5).
- L train schedules: derived from the Chicago Transit Authority's GTFS feed.
- Sun positions: NOAA solar-position equations.
- Drone power model: Y. Zeng, J. Xu and R. Zhang, "Energy minimization for wireless communication with rotary-wing
  UAV", IEEE Transactions on Wireless Communications, 2019.

Details and licence links are in [NOTICE.md](NOTICE.md).
