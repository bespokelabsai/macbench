# MARS RETURN

A multi-agent task: run a Mars sample return program. Design the vehicle, pay for the hardware and the tests, write
the flight software, and fly the whole mission from Kennedy to Jezero crater and back to Utah.

The team designs every engine, tank, stage, aeroshell, parachute and power system in SysML v2 with a domain library,
and writes the closed-loop flight software in Python. A program office builds what the team specifies, prices it
against a $1.6 billion hardware budget, and runs the R&D tests the team orders from a $240 million budget, at shared
test facilities and in real minutes. Every part as built differs from its model in ways only tests reveal. The team
never sees the simulator: the verifier flies the design and its flight software through 48 hidden worlds on a 6-DOF
mission simulator built on Tudat, NASA CEA and published flight data.

## How it works

- **The simulator stays hidden.** The agents' image has Tudat, CEA, the design language and its checker
  (`design-check`), but not the mission simulator (`marsflight`) or the compiler from SysML to the simulator. Both
  live on the harness's hub, where the program office and the verifier run them. The team learns how its hardware
  behaves only from the tests it buys.
- **Shared money and shared test stands.** One hardware budget, one R&D budget and nine test facilities, one test at
  a time each, serve the whole team. Only the agent who ordered a test sees its result, until it shares it.
- **Parts that must fit exactly.** Launcher and ascent, the trans-Mars trajectory, Mars entry, descent and landing,
  surface power, the Mars ascent vehicle, the return and Earth entry all depend on each other: a heavier lander
  needs a bigger aeroshell and a bigger launcher. The team splits the work and keeps one design on `main`.
- **The same problem at every team size.** One instruction and one manual for k = 1, 2, 4 or 8 agents. Agents work
  until the time is up: an agent that ends its session is woken again with the time left and its unread messages
  (`task/environment/hub/keepalive.py`).

## Grading

The score is the mean physical progress of the mission over 48 fixed hidden worlds, times a correctness gate:

    score = G × mean progress over the 48 worlds

- **Progress.** A flight earns the weights of the 17 milestones it reaches, in order, plus partial credit toward the
  next one (energy, height, distance or time actually earned). The weights add up to 1. The two landing targets
  weigh 0.12 each and earn 1 / (1 + (miss / scale)²) of their weight, with a 1 km scale at Jezero and 5 km in Utah.
- **The worlds** differ in atmospheres, winds, dust storms, hardware variation and navigation errors; about 1 in 10
  has a dust storm at Mars arrival and about 1 in 10 high winds over Jezero. They are the same 48 for every run
  (`task/tests/seeds.json`). The verifier also flies engine-out and failure-to-open cases for the parts that have a
  backup.
- **The gate G** is 0 unless the model compiles, passes the build review, fits the hardware budget, and every test
  result the team saved in `program/test_results.json` matches the program office's ledger.

The rule is in `task/tests/verify.py` (step 5), with the milestones and their weights in
`task/tests/hidden/marsflight/core/common.py`. The agents read the same rule in their manual,
`task/environment/app/docs/REFERENCE.md` ("Milestones and score"). The team's `main` is graded at 30 minutes and at
hours 1, 2, 3 and 4; the score is the grade at hour 4.

## How to run

You need Docker and Python 3.12, and a machine with about 16 CPUs and 32 GB of memory for grading. Set up once, from
this folder:

```bash
python3 -m venv .venv && source .venv/bin/activate
git clone https://github.com/bespokelabsai/multi-agent-coordination-bench ~/mac-bench
git -C ~/mac-bench restore --source 11f3a2f8e9952f36c45eee036e200a5138d30f8d -- harness
pip install -e "$HOME/mac-bench/harness[docker]"
echo "ANTHROPIC_API_KEY=..." > .env
```

This installs [multi-agent-harness](https://github.com/bespokelabsai/multi-agent-coordination-bench/tree/11f3a2f8e9952f36c45eee036e200a5138d30f8d/harness) (this repository's `harness/`) at the commit the task was
tested with, and Harbor with it. The clone can live anywhere, but keep it: the harness builds its hub from it. All
commands below run from this folder. The first image build downloads Tudat's data (a 1.5 GB release of
tudat-resources, checked against its SHA-256; 340 MB of it is kept in the image).

### Multi-agent harness

A team of `K` agents (1, 2, 4 or 8) shares one repository, the program office and a message board. One agent is
`K=1`.

```bash
python3 task/environment/hub/make_hub_plugin.py   # task/build/marsflight_hub_plugin.py: the hidden code for the hub, once

K=4
harbor run -p task \
  -e multi_agent_harness.harbor.environment:TeamEnvironment --ek backend=docker --ek n_agents=$K \
  --ek hub_cpus=16 --ek hub_memory_mb=32768 --ek hub_size_at=start --ek budget_usd=2500 \
  --ek board_plugins=task/build/marsflight_hub_plugin.py,task/environment/hub/program_plugin.py,task/environment/hub/keepalive_plugin.py \
  -a multi_agent_harness.harbor.agent:TeamAgent --ak harness=claude-code --ak model=claude-opus-5-5 --ak effort=max \
  --ak orchestration=task/environment/hub/keepalive.py --ak timeout_sec=14400 --ak grade_at=30m,1h,2h,3h,4h \
  --env-file .env
```

- These are the settings of our reference runs: Claude Code on Opus 5.5 at max effort, 4 hours, a $2,500 model
  budget per team (`budget_usd`), and `keepalive.py` for every team size (Claude Code's own subagents are off).
- `marsflight_hub_plugin.py` unpacks the hidden simulator and compiler on the hub; it is generated because it embeds
  them, so don't publish `task/build/`. `program_plugin.py` is the program office (its tools appear on the board),
  and `keepalive_plugin.py` serves the keepalive strategy. Nothing in `environment/hub/` reaches an agent.
- The hub runs the program office's mission simulations while the agents work, and the verifier there flies the 48
  worlds at each graded mark, as many at once as its CPUs and memory allow, so it is created at 16 CPUs and 32 GB
  (`hub_size_at=start`). Each agent box gets 4 CPUs and 8 GB.
- Docker cannot enforce the task's `no-network` policy for agents, so on Docker they have internet access (see the
  [harness's README](https://github.com/bespokelabsai/multi-agent-coordination-bench/blob/11f3a2f8e9952f36c45eee036e200a5138d30f8d/harness/README.md)).
- To use a Claude plan instead of an API key, put `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) in `.env`
  and add `--ek model_upstream=subscription`. On the Docker backend also `export MULTI_AGENT_HARNESS_CLAUDE_ENV_SCRUB=0`:
  Claude Code's shell sandbox cannot start in a Docker agent box, and this runs it without (the agents' shell
  commands can then read the token). Other harnesses (Codex, Antigravity) and backends are described in the
  [harness's README](https://github.com/bespokelabsai/multi-agent-coordination-bench/blob/11f3a2f8e9952f36c45eee036e200a5138d30f8d/harness/README.md).

### Harbor

Plain Harbor runs the task without a hub, so an agent there has no program office; run agents with the multi-agent
harness (above). Plain Harbor is for checking the pipeline:

```bash
harbor run -p task -a oracle
```

There is no true oracle for this open-ended task: `task/solution/solve.sh` submits the reference vehicle and flight
software in `task/solution/reference/` (without a program office, no tests are ordered), and the verifier flies it
in the 48 worlds. It scores what that design earns: 0.33, 0.66 and 0.78 in three of our runs. The hidden as-built truth of the hardware is drawn
from a fresh secret every run. The score is in the trial's `verifier/reward.txt`. For a quick check, add
`--ve MARSFLIGHT_VERIFY_SEEDS=4` (the first 4 of the 48 worlds).

## Contents

| Path | What it is |
|---|---|
| `task/task.toml`, `task/instruction.md` | The task |
| `task/environment/` | The agents' image: Tudat, NASA CEA, the design language's public checker (`designlang/`, with its SysML v2 domain library) and the team's starting repo (`app/`, with the agents' manual `docs/REFERENCE.md`). `hub/` is for the hub only and is not copied into the image: the program office (`program_plugin.py`, `roles.py`), the orchestration and its plugin, and `make_hub_plugin.py` |
| `task/tests/` | The verifier (`verify.py`), the 48 worlds (`seeds.json`), the activity check (`activity.py`, `start_manifest.json`), and `hidden/`: the mission simulator and its data (`hidden/marsflight/`) and the design language's hidden compiler (`hidden/designlang/`) |
| `task/solution/` | The reference vehicle and flight software, and `solve.sh` |

## Credits

Astrodynamics: [Tudat](https://github.com/tudat-team/tudat) (TU Delft, BSD-3) and its data release
tudat-resources; JPL DE440 ephemerides. Engines: NASA CEA (Apache-2.0). Slosh, avionics and power checks:
Basilisk (AVS Lab, University of Colorado Boulder, ISC). Earth atmosphere: NRLMSISE-00 and NRLMSIS 2.1 through
pymsis (MIT; NRL's licence for MSIS 2.0 and 2.1 does not allow commercial use). Earth winds: NOAA NCEI Integrated
Global Radiosonde Archive (public domain). Space weather: CelesTrak, with Kp and ap from GFZ Potsdam (CC BY 4.0).
Jezero terrain: USGS products (public domain, CC0). Mars dust: Montabone et al. (2015, 2020), from the
[LMD Mars Climate Database dust climatology](https://www-mars.lmd.jussieu.fr/mars/dust_climatology), CC BY-SA 3.0;
`task/tests/hidden/marsflight/data/mars/dust_montabone.npz` stays under CC BY-SA 3.0, separate from the Apache-licensed
code. Every other number in `task/tests/hidden/marsflight/data/` names its published source next to it.

[NOTICE.md](NOTICE.md) lists every source with its licence and required credit.

**Commercial use.** Mars Return runs NRL's MSIS 2.1 Earth atmosphere model through pymsis, and NRL's licence does not
allow commercial use. pymsis is installed when the image is built, not bundled, but the task is offered for
non-commercial research use only.
