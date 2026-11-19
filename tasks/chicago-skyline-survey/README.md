# CHICAGO SKYLINE SURVEY

A multi-agent task: plan one 12-hour survey day for a fleet of 8 camera drones over a 3D model of downtown Chicago.

The city is built from real OpenStreetMap buildings. About 1,900 targets are worth photographing: rooftop equipment,
facade panels, bridge spans, and the L trains and river water taxis that move through the area. Mapping 12 landmarks
in 3D and holding light-show formations also score. Every plan must respect firm airspace rules (altitude band,
building clearance, drone separation, no-fly zones), battery and charging-pad limits, and riverside noise budgets.

The team delivers one waypoint plan per drone, `plans/D1.csv` to `plans/D8.csv`.

## How it works

- **One fleet, one set of plans.** Every agent has the same instruction, data and flight checker, and can edit any
  drone's plan. The team works in one shared git repository; the plans on `main` are what gets graded.
- **The drones depend on each other.** Trains and boats count only when two drones photograph them together. A
  landmark counts only when at least 3 drones scan it within 10 seconds of each other. A light show pays only when
  every slot is filled on time. The four charging pads hold two drones each, and drones must keep their distance.
  Plans written in isolation collide, queue at the pads or miss their partners.
- **The same problem at every team size.** One task and one instruction for 1, 2, 4 or 8 agents. Every agent gets
  8 CPUs and 16 GB.
- **Graded during the run.** `main` is graded at minute 30 and at hours 1, 2, 3 and 4. The agents know the schedule
  but never see a grade. The grade at hour 4 is the score.

## Grading

The plans are flown on 24 hidden wind days. These differ from the forecast in the same way as the 32 practice days the
agents get. A photo counts only if its drone lands afterwards, and every lost drone costs 500 points.

The score is the mean points per day divided by 18,924.4. That number is the most a day could give if every target
were photographed at its best light with no flight limits, so no real plan comes close to 1.0.

`task/tests/test.sh` runs `task/tests/verify.py`. It flies the plans with a clean copy of the checker
(`task/tests/skyline/`) on the hidden days (`task/tests/hidden/`) and writes `reward.txt`, plus `details.json` with
each day's points and each drone's outcome. The point rules are in `task/tests/skyline/scoring.py` and in section 7
of `task/environment/app/docs/MANUAL.md`.

## How to run

You need Docker and Python 3.12. Set up once, from this folder:

```bash
python3 -m venv .venv && source .venv/bin/activate
git clone https://github.com/bespokelabsai/multi-agent-coordination-bench ~/mac-bench
git -C ~/mac-bench checkout cd6c6ce43168d531033699d1c3f3aefcbae791b9
pip install -e "$HOME/mac-bench/harness[docker]"
echo "ANTHROPIC_API_KEY=..." > .env
```

This installs [multi-agent-harness](https://github.com/bespokelabsai/multi-agent-coordination-bench/tree/cd6c6ce43168d531033699d1c3f3aefcbae791b9/harness) (this repository's `harness/`) at the commit the task was
tested with, and Harbor with it. Keep the clone: the harness builds its hub from it. All commands below run from
this folder.

### Multi-agent harness

A team of `K` agents (1, 2, 4 or 8) plans the fleet together over a message board and the shared git repository.
One agent is `K=1`.

```bash
K=4
harbor run -p task \
  -e multi_agent_harness.harbor.environment:TeamEnvironment --ek backend=docker --ek n_agents=$K \
  --ek budget_usd=2500 --ek board_plugins=task/environment/hub/keepalive_plugin.py \
  -a multi_agent_harness.harbor.agent:TeamAgent --ak harness=claude-code --ak model=claude-opus-5-5 --ak effort=max \
  --ak orchestration=task/environment/hub/keepalive.py --ak timeout_sec=14400 --ak grade_at=30m,1h,2h,3h,4h \
  --env-file .env
```

- These are the settings we used for our published runs: Claude Code with Opus 5.5 at max effort, 4 hours, and a $2,500 model
  budget for the team.
- `keepalive.py` wakes an agent that ended its session (when a teammate messages it, or every 30 minutes) until the
  time is up, so a run lasts its full 4 hours. `keepalive_plugin.py` tells it which agents have unread messages.
  The CLIs' own subagents are off, so a team of K is K agents. Nothing in `environment/hub/` reaches an agent.
- Each scheduled grading (`grade_at`) is saved in the trial's `marks/<time>/` folder; the final grade is the trial's
  `verifier/reward.txt`.
- The task gives agents no network beyond the hub (`no-network`). The harness's cloud sandbox backend enforces that.
  Docker cannot, so on Docker the agents have internet access and the harness logs `network_not_enforced`. With
  internet, agents could read this repository, which holds the hidden wind days and the reference plans. For results
  meant to be compared, use a backend that enforces it (see the [harness's README](https://github.com/bespokelabsai/multi-agent-coordination-bench/blob/cd6c6ce43168d531033699d1c3f3aefcbae791b9/harness/README.md)).
- To use a Claude plan instead of an API key, put `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) in `.env`
  and add `--ek model_upstream=subscription`. On the Docker backend also `export MULTI_AGENT_HARNESS_CLAUDE_ENV_SCRUB=0`:
  Claude Code's shell sandbox cannot start in a Docker agent box, and this runs it without (the agents' shell
  commands can then read the token). Other harnesses and backends are described in the [harness's README](https://github.com/bespokelabsai/multi-agent-coordination-bench/blob/cd6c6ce43168d531033699d1c3f3aefcbae791b9/harness/README.md).

### Harbor

Plain Harbor runs the task without a hub, so an agent there has no message board or team repository. Run agents with
the multi-agent harness (above). Plain Harbor is for checking the pipeline:

```bash
harbor run -p task -a oracle
```

There is no 1.0 oracle for this task. `solution/solve.sh` copies a set of reference plans into `/app/plans`, which
the verifier grades like any submission. They score about 0.153 (2,901 points a day): a baseline, not an optimum.
The score is in the trial's `verifier/reward.txt`.

## Contents

| Path | What it is |
|---|---|
| `task/task.toml`, `task/instruction.md` | The task: 4 hours, 8 CPUs and 16 GB per agent |
| `task/environment/` | The image. `app/` becomes `/app`: the city and target data, 32 practice wind days, the manual (`docs/MANUAL.md`), the flight checker (`skyline/`) and `plans/`, which holds only a format note. `hub/` is for the hub only and is not copied into the image: the keepalive orchestration and its board plugin |
| `task/tests/` | The verifier: `test.sh`, `verify.py`, a clean copy of the checker and data, and the 24 hidden wind days (`hidden/`) |
| `task/solution/` | `solve.sh` and the reference plans in `reference/` (about 0.153), to check the pipeline |
| `NOTICE.md` | Licenses and notices for the bundled data |

## Credits

- Buildings, river and rail: © OpenStreetMap contributors, under the Open Database License 1.0.
- Wind forecast: Open-Meteo.com (CC BY 4.0), containing modified Copernicus Climate Change Service information
  (ERA5).
- L train schedules: derived from the Chicago Transit Authority's GTFS feed.
- Sun positions: NOAA solar-position equations.
- Drone power model: Y. Zeng, J. Xu and R. Zhang, "Energy minimization for wireless communication with rotary-wing
  UAV", IEEE Transactions on Wireless Communications, 2019.

Details and license links are in [NOTICE.md](NOTICE.md).
