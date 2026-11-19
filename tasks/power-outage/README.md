# POWER OUTAGE

A national grid has gone completely dark. The team runs its sixteen control rooms and writes the restoration: a
controller for every control area, `areas/<area>/controller.py`, and the protocol between the areas. The grader
plays the controllers on 48 hidden blackout scenarios for 16 simulated hours each, in 30-second steps, on a
simulator of the grid's power flow, generators, protection and loads.

Restoring a grid from nothing ("black start") is done area by area: a few units that can start without outside power
energise their own area, pick up load in steps the frequency can bear, and only then join neighbouring islands
across tie lines. Every scenario draws its own hour, weather, faulted lines, failed starts and events, so a
controller has to read the grid rather than replay a plan.

## How it works

- **16 fixed seats.** Every run has the same 16 control areas whatever the team size. Seat `aNN` goes to the agent
  at position (NN − 1) mod N in the team's sorted names, so a team of 16 has an area each and a single agent holds
  all 16.
- **Private data per seat.** Each area's plant start-up data, line data, relay settings, cold load pickup and crews
  are readable only by the agent holding it (the board tool `area_data`), as in a real control room. Two items of
  local equipment per area exist nowhere else, and their exact values are known only as ranges.
- **Decentralised grading.** At grading time each area's controller runs in its own process and sees only its own
  area and messages, on a channel modelled on control-room hotlines: every recipient costs a credit, messages arrive a
  minute later and are cut at 600 characters, and a tie line closes only if both areas command it in the same step.
- **Scarce truth.** The team shares one git repository and a message board. A low-fidelity study simulator in the
  repository runs as often as the team likes; drills on the true simulator (`practice_run`) come from one pool of 40
  full drills for the whole team and the whole run.
- **Real time.** The run lasts 4 hours, and the team's `main` is graded at 30 minutes and at hours 1, 2, 3 and 4.

## Grading

The verifier (`task/tests/verify.py`) plays the team's controllers on the 48 hidden scenarios, each area in its own
process as its own user. A scenario's raw score is its restoration index: the mean over the 16 simulated hours of the
squared, priority-weighted share of demand served. It is multiplied down for island collapses, missed nuclear cooling
deadlines and equipment damage, and is 0 for a closure forced out of phase. The scenario's score is the raw score
divided by its capacity bound (`task/environment/hub/gridsim/bound.py`), the most its generation would allow, so 1.0
is the physical maximum. The reward is the mean over the 48 scenarios. The simulator is in
`task/environment/hub/gridsim/` (`sim.py` has the score); the hidden scenarios come from a salt that only the
verifier has (`task/tests/hidden/salt`).

During the run, each graded mark before the end plays 8 of the scenarios, so it takes minutes rather than an hour,
and saves the solution it graded. The final grading plays all 48, and then grades every earlier mark again on all 48,
as our checkpoint scores are (see Multi-agent harness).

## How to run

You need Docker and Python 3.12. Set up once, from this folder:

```bash
python3 -m venv .venv && source .venv/bin/activate
git clone https://github.com/bespokelabsai/multi-agent-harness ~/multi-agent-harness
git -C ~/multi-agent-harness checkout 0f7ac9583906b998772245c41d799f7b2264dd4f
pip install -e "$HOME/multi-agent-harness[docker]"
echo "ANTHROPIC_API_KEY=..." > .env
```

This installs [multi-agent-harness](https://github.com/bespokelabsai/multi-agent-harness) at the commit the task was
tested with, and Harbor with it. Keep the clone: the harness builds its hub from it. All commands below run from
this folder.

### Multi-agent harness

A team of `K` agents (1, 2, 4 or 8) shares the sixteen areas, the board and the drill pool. One agent is `K=1`.

```bash
K=4
harbor run -p task \
  -e multi_agent_harness.harbor.environment:TeamEnvironment --ek backend=docker --ek n_agents=$K \
  --ek hub_cpus=8 --ek hub_memory_mb=16384 --ek budget_usd=2500 --ek hub_state_dirs=/var/lib/blackstart-hub \
  --ek board_plugins=task/environment/hub/operations_plugin.py,task/environment/hub/keepalive_plugin.py \
  -a multi_agent_harness.harbor.agent:TeamAgent --ak harness=claude-code --ak model=claude-opus-5-5 --ak effort=max \
  --ak orchestration=task/environment/hub/keepalive.py --ak timeout_sec=14400 --ak grade_at=30m,1h,2h,3h,4h \
  --env-file .env
```

- These are the settings of our reference runs (Claude Code, Opus 5.5 at max effort, a $2,500 model budget, agents
  at 4 CPUs and 8 GB, the hub at 8 CPUs and 16 GB).
- `operations_plugin.py` is the operations office on the hub: the board tools `grid_info` and `area_data`, and the
  drills on the true simulator. `keepalive.py` gives every agent the full task and wakes an agent that has ended its
  session when a message arrives for it or on a timer, until the 4 hours are up or the budget is spent;
  `keepalive_plugin.py` tells it what is unread. The hub's plugins never reach an agent; the simulator is in the
  image, root-only, where agents cannot read it.
- `grade_at` grades the team's `main` at 30 minutes and hours 1, 2, 3 and 4; the 4-hour grade is the score. The
  earlier marks play 8 of the 48 scenarios during the run (in the trial's `marks/<label>/`). After the 4 hours, the
  final verifier grades the team's `main`, then grades each earlier mark again on all 48 from the solution it saved,
  about 30 minutes each on an 8-CPU hub. Those full grades are in the trial's `verifier/marks/<label>/`, with a
  summary in `verifier/marks/rewards.txt`. `BLACKSTART_FULL_MARKS_S` in `task.toml` caps their time at 3 hours (0
  turns them off), so the verifier always ends before its timeout and the score is kept.

- The task gives agents no network beyond the hub (`no-network`). The harness's cloud sandbox backend enforces that;
  Docker cannot, so on Docker the harness runs the agents with internet access and logs `network_not_enforced`. With
  internet, agents could read this repository, which holds the hidden scenarios' salt and the simulator: for results
  meant to be compared, use a backend that enforces it (see the harness's README), as our reference runs did.
- To use a Claude plan instead of an API key, put `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) in `.env`
  and add `--ek model_upstream=subscription`. On the Docker backend also `export MULTI_AGENT_HARNESS_CLAUDE_ENV_SCRUB=0`:
  Claude Code's shell sandbox cannot start in a Docker agent box, and this runs it without (the agents' shell
  commands can then read the token). Other harnesses (Codex, Antigravity) and backends are described in the
  harness's README.

### Harbor

Plain Harbor runs the task without a hub, so an agent there has no board tools or drills; run agents with the
multi-agent harness (above). Plain Harbor is for checking the pipeline:

```bash
harbor run -p task -a oracle
```

The task is open-ended and has no true oracle: `solution/solve.sh` installs a reference controller (bottom-up
restoration with a protocol for the tie lines, untuned) in all 16 areas, which the verifier grades on all 48
scenarios; it scores close to 0. The score is in the trial's `verifier/reward.txt`.

## Contents

- `task/task.toml`, `task/instruction.md`: the task.
- `task/environment/`: the agents' image. `app/` is the team repository the agents work in, with `MANUAL.md` (the
  full rules), the public grid, the controller stubs, a controller checker and the study simulator; `host/` runs one
  area's controller. `hub/` is for the hub only: the simulator (`gridsim/`, copied into the image root-only), the
  operations office plugin and the orchestration with its plugin.
- `task/tests/`: the verifier and `hidden/salt`, the hidden scenarios' salt.
- `task/solution/`: `solve.sh` and the reference controller (`reference/oracle.py`).

## Credits

The grid is synthetic, generated from a fixed seed (`task/environment/hub/gridsim/grid.py`). The task bundles no
external data, fonts or third-party code.
