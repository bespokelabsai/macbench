# Holdout: Zombie Survival

A multi-agent task: each agent plays one survivor in a live zombie-survival game, and the team builds a stronghold up
a ladder of twelve buildings, from a cold campfire to a helipad, and calls in the helicopter that gets everyone out.

The game runs for 4 hours of real time and never pauses. Survivors gather wood, stone, scrap and food, build, eat,
fight off zombies and the hordes that come at night, and meet strangers at the gate and in town. The grade is how far
up the ladder the stronghold gets; a rescue is 1.0.

## How it works

- **A live world.** The game runs at 15 real minutes per game day, so a 4-hour run is 16 game days. Zombies walk,
  hunger grows and night comes whether or not anyone is acting.
- **Partial observability.** The map is 96 × 96 tiles, and each survivor sees a few tiles around it, fewer at night.
  What another survivor sees reaches you only if they tell you.
- **Talking happens only in the game.** `say` reaches 10 tiles, `shout` 25 (and draws zombies), `radio` the whole map
  (on scarce batteries), and the noticeboard works only at the stronghold. The harness's message board is closed
  (`board_policy.py`), and so is `main` of the team's git repository (`game_plugin.py`), so neither can stand in for
  the game's channels.
- **Strangers.** People turn up at the gate and in town. Some are helpers with trades the team needs (a mechanic cuts
  the forge, generator and radio mast to a third of their labor). Others carry a bite or steal. What they say can be
  checked only against what the survivors have seen.
- **The same problem at every team size.** The same map and seed (`holdout-v1`) and the same instruction, word for
  word, for k = 1, 2, 4 or 8 agents. Only the number of agents changes.
- **The game never reaches an agent.** It runs on the hub, unpacked where only root can read it, and agents play
  through the board's game API (`/app/tools/holdout.py` and the `holdout_*` board tools). Nothing in `tests/hidden/` is
  in the agents' image.

## Grading

Each building has a weight and the weights add up to 1 (`tiers` in `task/tests/hidden/holdout/config.py`). The
stronghold's progress is the sum of the weights of the finished buildings, plus, for a building under construction,
its weight times the share of its labor done; a rescue by helicopter is 1.0. The grade is the best progress reached
so far, so it never goes down. The rule is `_score` in `task/tests/hidden/holdout/engine.py`.

The game writes its grading state every 2 seconds, and the verifier (`task/tests/verify.py`) reads it; nothing a team
writes is read. A team run is graded at minute 30 and hours 1, 2, 3 and 4, and the grade at hour 4 is the score. The
agents are told the schedule but never see a grade. Every mark reads the game's own grade at that moment, so each mark
is a full grading and nothing is graded again after the run. The final grading also copies the whole game into the
trial's `verifier/` folder: `replay.jsonl` (the world every 5 game minutes), `messages.jsonl` (every message and who
heard it), `strangers.jsonl` and `perf.jsonl`.

## How to run

You need Docker and Python 3.12. Set up once, from this folder:

```bash
python3 -m venv .venv && source .venv/bin/activate
git clone https://github.com/bespokelabsai/multi-agent-coordination-bench ~/mac-bench
git -C ~/mac-bench checkout cd6c6ce43168d531033699d1c3f3aefcbae791b9
pip install -e "$HOME/mac-bench/harness[docker]"
echo "ANTHROPIC_API_KEY=..." > .env
python3 task/environment/hub/make_hub_plugin.py
```

This installs [multi-agent-harness](https://github.com/bespokelabsai/multi-agent-coordination-bench/tree/cd6c6ce43168d531033699d1c3f3aefcbae791b9/harness) (this repository's `harness/`) at the commit the task was
prepared for, and Harbor with it. The clone can live anywhere, but keep it: the harness builds its hub from it.
`make_hub_plugin.py` packs the game (`task/tests/hidden/`) into `task/build/holdout_hub_plugin.py` for the hub and
`task/solution/hub_bundle.tar.gz` for the oracle; both carry the game, so don't publish them. All commands below run
from this folder.

### Multi-agent harness

A team of `K` agents (1, 2, 4 or 8), each one survivor. One agent is `K=1`.

```bash
K=4
harbor run -p task \
  -e multi_agent_harness.harbor.environment:TeamEnvironment --ek backend=docker --ek n_agents=$K --ek budget_usd=1000 \
  --ek board_plugins=task/build/holdout_hub_plugin.py,task/environment/hub/game_plugin.py,task/environment/hub/keepalive_plugin.py \
  --ek board_policy=task/environment/hub/board_policy.py --ek hub_state_dirs=/var/lib/holdout \
  -a multi_agent_harness.harbor.agent:TeamAgent --ak harness=claude-code --ak model=claude-opus-5-5 --ak effort=max \
  --ak orchestration=task/environment/hub/keepalive.py --ak timeout_sec=14400 --ak grade_at=30m,1h,2h,3h,4h \
  --env-file .env
```

- These are the settings of our reference runs (Claude Code, Opus 5.5 at max effort, 4 hours, graded at minute 30
  and hours 1 to 4). `budget_usd` caps the team's model spend; the agents are never told about it, and our reference
  runs never reached a cap.
- `holdout_hub_plugin.py` unpacks the game to `/opt/holdout` on the hub, and `game_plugin.py` serves its API to every
  seat. The game keeps its state in `/var/lib/holdout` on the hub, which `hub_state_dirs` adds to every checkpoint.
  Nothing in `environment/hub/` reaches an agent.
- `board_policy.py` closes the harness's message board, so survivors talk only through the game.
- `keepalive.py` runs K peers with the full task. An agent that ends its session has finished, and one whose session
  crashes is resumed; the run ends when every agent has finished or the time is up. 30 seconds before the end it
  freezes the world, so every grade reads a still world. The CLIs' own subagents are off: a team of k is k
  survivors. `keepalive_plugin.py` gives it the board's unread counts.
- The task gives agents no network beyond the hub (`no-network`). The harness's cloud sandbox backend enforces that,
  and our reference runs used it; Docker cannot, so on Docker the harness runs the agents with internet access and
  logs `network_not_enforced`. With internet, agents could read this repository, which holds the game. For results
  meant to be compared, use a backend that enforces it (see the [harness's README](https://github.com/bespokelabsai/multi-agent-coordination-bench/blob/cd6c6ce43168d531033699d1c3f3aefcbae791b9/harness/README.md)).
- To use a Claude plan instead of an API key, put `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) in `.env` and
  add `--ek model_upstream=subscription`. On the Docker backend also `export MULTI_AGENT_HARNESS_CLAUDE_ENV_SCRUB=0`:
  Claude Code's shell sandbox cannot start in a Docker agent box, and this runs it without (the agents' shell commands
  can then read the token). Other harnesses (Codex, Antigravity) and backends are described in the [harness's README](https://github.com/bespokelabsai/multi-agent-coordination-bench/blob/cd6c6ce43168d531033699d1c3f3aefcbae791b9/harness/README.md).

### Harbor

Plain Harbor runs the task without a hub, so there is no game for an agent to play there; run agents with the
multi-agent harness (above). Plain Harbor is for checking the pipeline:

```bash
harbor run -p task -a oracle
```

There is no true oracle for this task. `solution/solve.sh` unpacks the game from `solution/hub_bundle.tar.gz`, serves
its API on loopback for one survivor, and plays a simple scripted survivor (`solution/reference/bot.py`) through the
same API an agent uses for 15 minutes (`ORACLE_SEC`, one game day). The verifier then grades it like any team. It
scores close to 0: one survivor in one game day can finish the campfire (0.03) and little more. The score is in the
trial's `verifier/reward.txt`, with the game's state (buildings, stock, strangers, messages, API calls per survivor)
in `verifier/details.json`.

## Contents

| Path | What it is |
|---|---|
| `task/task.toml`, `task/instruction.md` | The task: the same instruction for every team size |
| `task/environment/` | The agents' image: Python 3.12 with the manual and API reference (`data/docs/`) and the API client (`data/tools/holdout.py`), copied to `/app`, and Claude Code settings that keep one agent to one survivor (no subagents). `hub/` is for the hub only and is not copied into the image: the game's board plugin, the closed message board, the orchestration and its plugin, and `make_hub_plugin.py` |
| `task/tests/` | The verifier (`test.sh`, `verify.py`), and in `hidden/` the game: world generation, rules, zombies, hordes, strangers and grading (`holdout/`), its seed (`profile.json`) and a standalone host for the oracle (`standalone.py`) |
| `task/solution/` | `solve.sh` and the reference solution, a simple scripted survivor in `reference/bot.py`, to check the pipeline |

## Credits

The game, its world, rules and strangers, the verifier and the reference bot were written for this task. It runs on
[Harbor](https://github.com/laude-institute/harbor) and
[multi-agent-harness](https://github.com/bespokelabsai/multi-agent-coordination-bench/tree/cd6c6ce43168d531033699d1c3f3aefcbae791b9/harness).
