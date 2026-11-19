# DXB ATC LIVE

A team of agents runs air traffic control at Dubai International (OMDB) for one live day. The day is 11 to 12
January 2020, with the real weather reports of that night, which brought thunderstorms. The schedule is built from
the real flight lists of the same weekday one week earlier, with some arrivals removed to lighten the traffic. A medical emergency and a bird strike that closes a runway for an inspection also happen during the
day. The agents give instructions to the aircraft through the airport's ATC system, and simulated pilots answer.

The day runs at three times real speed, so a 4-hour run plays 12 hours of the airport, 18:00Z to 06:00Z, with 420
scheduled flights. The aircraft fly in the BlueSky simulator with the OpenAP performance model, on the airport layout
from OpenStreetMap. The day does not wait for the agents: aircraft keep flying while the agents read, plan and write
code.

## How it works

- **Eleven positions.** The airport has eleven control positions: Arrivals, Director, two towers, two ground
  positions, Delivery, two departure positions, flow management and a supervisor. Each radio position has its own
  frequency. Only the controller who has opened a position can instruct its aircraft, and a position nobody has
  opened has no controller. The team divides the positions among its agents.
- **Handoffs.** An aircraft moves from position to position as it lands or departs, under standard handoff
  conditions. One controller's work sets up the next one's, so the agents have to agree on how they hand over traffic.
- **Real time.** The simulator runs on the harness hub and keeps its own clock. Radio time is limited: every
  instruction and readback uses time on the frequency. Agents can work by hand or write programs that call the API.
- **Shared tools.** The team shares a git repository and the harness message board. The agents' manual and API
  reference are in `/app/docs`.
- **Graded during the run.** The day is graded at 30 minutes and at hours 1, 2, 3 and 4 of the run. The grade at
  4 hours is the score. The agents know the schedule but never see the earlier grades.

## Grading

The verifier (`task/tests/grade.py`) grades the hub's record of the day, which only the simulator can write. Each
flight scheduled up to 30 minutes before the grading time earns credit when it is completed: an arrival when it
lands, a departure when it is airborne. Credit is 1 for a delay of 15 minutes or less and falls linearly to 0 at 4
hours 15 minutes late. A flight not completed by the grading time, a cancelled flight and a diverted arrival whose
return has not landed earn 0. The service score is the mean credit over the graded flights.

Safety events lower the score. Each accident multiplies it by 0.5. A near collision multiplies it by 0.9, a serious
incident by 0.97 and a loss of separation by 0.995. The product of these factors is raised to the power
100 / max(N, 100) for N graded flights. A team that does nothing scores 0. A day whose clock stopped before the
grading time also scores 0.

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
tested with, and Harbor with it. Keep the clone, because the harness builds its hub from it. All commands below run
from this folder. For Codex put `OPENAI_API_KEY` in `.env`, and for Antigravity put `GEMINI_API_KEY`.

### Multi-agent harness

First build the team's copy of the task and the hub plugin that carries the live day:

```bash
python3 task/environment/hub/make_team_task.py
```

This writes `task/build/dxb-atc-live/` and `task/build/atc_hub_plugin.py`. Then run a team of `K` agents (1, 2, 4 or
8). One agent is `K=1`.

```bash
K=4
harbor run -p task/build/dxb-atc-live \
  -e multi_agent_harness.harbor.environment:TeamEnvironment --ek backend=docker --ek n_agents=$K \
  --ek hub_cpus=3 --ek hub_memory_mb=8192 --ek hub_size_at=start --ek budget_usd=5000 \
  --ek hub_state_dirs=/var/lib/dxb-atc \
  --ek board_plugins=task/build/atc_hub_plugin.py,task/environment/hub/plugins/atc_plugin.py,task/environment/hub/plugins/keepalive_plugin.py \
  -a multi_agent_harness.harbor.agent:TeamAgent --ak harness=claude-code --ak model=claude-opus-5-5 --ak effort=max \
  --ak orchestration=task/environment/hub/keepalive.py --ak timeout_sec=14400 --ak grade_at=30m,1h,2h,3h,4h \
  --env-file .env
```

- These are the settings of our reference runs. Each agent has 1 CPU and 4 GB at every team size, and the hub has
  3 CPUs and 8 GB.
- The reference runs used three models:
  - Claude Code with `--ak model=claude-opus-5-5 --ak effort=max` and a $5,000 budget.
  - Codex with `--ak harness=codex --ak model=gpt-6.1-sol --ak effort=max` and a $5,000 budget.
  - Antigravity with `--ak harness=antigravity --ak model=gemini-4-argon-high` and no effort option, because the
    slug sets the thinking level. Its budgets were $80, $144, $240 and $440 for 1, 2, 4 and 8 agents.
- The budget caps the team's model spend, priced at API rates.
- `atc_hub_plugin.py` unpacks the live day and the simulator on the hub. `atc_plugin.py` is the controllers' API on
  the hub. Agents reach it at `$MULTI_AGENT_HARNESS_BOARD_URL/api/atc/`. Neither the live day nor the simulator engine
  is in an agent's sandbox.
- `keepalive.py` wakes an agent that has ended its session, when a message arrives for it or on a timer, until the 4
  hours are up or the budget is spent. Then it stops the day. `keepalive_plugin.py` tells it what is unread.
- `grade_at` grades the day at 30 minutes and hours 1, 2, 3 and 4. Each grade is in the trial's `marks/<label>/`,
  and the 4-hour grade is the trial's reward. The day's state is in `/var/lib/dxb-atc` on the hub, and
  `hub_state_dirs` keeps it in every checkpoint.
- The task gives agents no network beyond the hub (`no-network`). The harness's cloud sandbox backend enforces that.
  Docker cannot, so on Docker the harness runs the agents with internet access and logs `network_not_enforced`. With
  internet, agents could read this repository, which holds the live day. For results you want to compare, use a
  backend that enforces it (see the [harness's README](https://github.com/bespokelabsai/multi-agent-coordination-bench/blob/cd6c6ce43168d531033699d1c3f3aefcbae791b9/harness/README.md)), as our reference runs did.
- To use a Claude plan instead of an API key, put `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) in `.env`
  and add `--ek model_upstream=subscription`. On the Docker backend also run
  `export MULTI_AGENT_HARNESS_CLAUDE_ENV_SCRUB=0`. Claude Code's shell sandbox cannot start in a Docker agent box,
  and this setting runs it without one, so the agents' shell commands can then read the token. Codex can use a
  ChatGPT login the same way. See the [harness's README](https://github.com/bespokelabsai/multi-agent-coordination-bench/blob/cd6c6ce43168d531033699d1c3f3aefcbae791b9/harness/README.md).

### Harbor

Plain Harbor runs `task/` as a single-agent task without the harness. The image serves the live day itself, from a
root-only copy, on a port inside the container. Its entrypoint starts the server, so use a backend that runs the
image's ENTRYPOINT, such as Docker. The verifier runs in a container of its own and grades the record that Harbor
copies out of the run. Plain Harbor is for checking the pipeline. The scores of the reference runs come from the
multi-agent harness.

```bash
harbor run -p task -a oracle
```

The task is open-ended, so there is no oracle that scores 1.0 on the day. In the oracle run, the server's rule-based
controller (`task/environment/hub/server/standin.py`) works every position, and `solution/solve.sh` plays the flow
management position through the API for 30 minutes. The verifier then checks the day instead of scoring it. The run
grades 1.0 when the day ran for at least an hour of airport time with no simulator error, flights landed and took
off, and no aircraft crashed. The grade is in the trial's `verifier/reward.txt`.

A short run with a real agent works the same way, for example
`harbor run -p task -a claude-code -m anthropic/claude-haiku-5-5 --agent-timeout-multiplier 0.04 --env-file .env`.
A run shorter than 4 hours stops the day early, so it grades 0. The verifier's output line still shows the number of
API calls the agent made.

## Contents

- `task/task.toml`, `task/instruction.md`: the task.
- `task/environment/`: the agents' image. `data/docs/` becomes `/app/docs`, with the manual, the API reference and
  the airport's data. `engine/atcsim/` is the simulator engine: the airport, the ground, the pilots, the radio and the
  safety rules. `entry.sh` serves the live day in a run without the harness.
- `task/environment/hub/`: for the hub only. `server/` is the live day: the server, its schedule, weather and events
  (`scenarios/live.json`), and the rule-based controller. `plugins/` and `keepalive.py` are the board plugins and the
  orchestration. `make_team_task.py` and `hub_bundle.py` build the team's copy of the task and the hub plugin.
- `task/tests/`: the verifier. `grade.py` scores the day.
- `task/solution/`: `solve.sh` and the reference flow controller (`bot.py`, `atc.py`).

## Credits

See `NOTICE.md` for the licences.

- Flights: OpenSky Network flight lists, Zenodo record 7923702 (CC BY 4.0).
- Weather: the Iowa Environmental Mesonet METAR and SPECI archive, and Ogimet for the TAFs.
- Airport layout: OpenStreetMap contributors, through Overpass (ODbL).
- Runways, holds and separation: the UAE AIP (AD 2 OMDB) and ICAO Doc 4444.
- Aircraft performance: OpenAP (TU Delft, LGPL-3.0), installed in the image.
- Flight simulator: BlueSky (TU Delft, GPL-3.0), installed in the image.
- The thunderstorm cells are synthetic, placed to agree with the weather reports. The terminal procedures are
  simplified, with made-up point names.
