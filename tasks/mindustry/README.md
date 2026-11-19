# MINDUSTRY: MANY FRONTS

A multi-agent task: a team of agents plays one live game of [Mindustry](https://github.com/Anuken/Mindustry), an open
source factory and tower defence game, for 4 hours.

The team mines ore, builds drills, belts and crafters, and defends one shared core while enemy waves arrive from 4
directions on a 360 by 360 tile map. The first wave comes after 8 minutes and then every 3 minutes, each 6% stronger
than the last. The score is the weighted count of items the team's buildings make and deliver to the core.

## How it works

- **A live world.** The game starts at the team's first API call and never pauses. A lost core ends that world, and
  two minutes later the team starts again on a new map, with all later production worth 15% less.
- **A fixed fleet.** The team always controls 8 units, dealt evenly to its agents. Each unit has its own action rate
  limit (a bucket of 24 tokens refilling at 4 a second), and the maps are the same in every run. A solo agent and a
  team of 8 have the same sight, builders and action rate in the same worlds; only the number of models sharing them
  changes. (A team of more than 8 gets one unit per agent, so it is not comparable.)
- **One shared stock.** Every agent works on the same core, items and map, with no assigned roles, so the team has to
  split the fronts, the ore fields and the building work.
- **Programs play too.** One model call is slow next to the game clock, so agents write programs that play through the
  same rate-limited API.
- **The game stays hidden.** On the harness the game server and its bridge run on the hub. Agents get the public
  Mindustry server JAR and the API docs, and reach the game only through the board's `/api/game/*` routes and MCP tools.

## Grading

For each world and each item, the grader takes the smaller of two of the game's own counters: what drills and crafters
made, and what buildings delivered into a core. Hand mining, deposits, withdrawals, refunds and items fed through an
unloader on a core add nothing. Items are weighted (copper 1 up to plastanium 25), and each world is multiplied by 0.85
for every core lost before it, with a floor of 0.25. The total `S` becomes the grade
`min(1, log(1 + S/1e6) / log(1 + 1.4e8/1e6))`. The rule is in `task/tests/grade.py`; it reads only the game's
grading state, never the agents' files.

With the harness the team is graded at 30 minutes and hours 1, 2, 3 and 4; the grade at hour 4 is the score, and agents never see a
mark's result. If the grader cannot read the state it writes no reward, so a broken grading shows as failed, never as 0.

## How to run

You need Docker and Python 3.12. Set up once, from this folder:

```bash
python3 -m venv .venv && source .venv/bin/activate
git clone https://github.com/bespokelabsai/multi-agent-harness ~/multi-agent-harness
git -C ~/multi-agent-harness checkout 0f7ac9583906b998772245c41d799f7b2264dd4f
pip install -e "$HOME/multi-agent-harness[docker]"
python3 task/environment/hub/make_hub_plugin.py   # packs the hidden game for the hub and the oracle
echo "ANTHROPIC_API_KEY=..." > ~/.mindustry.env   # keep keys outside this folder
```

This installs [multi-agent-harness](https://github.com/bespokelabsai/multi-agent-harness) at the commit the task was
tested with, and Harbor with it. The clone can live anywhere, but keep it: the harness builds its hub from it. All
commands below run from this folder. The images download the Mindustry v160.7 server JAR from its GitHub release.

### Multi-agent harness

A team of `K` agents (1, 2, 4 or 8) shares the 8 units and a message board:

```bash
K=4
harbor run -p task \
  -e multi_agent_harness.harbor.environment:TeamEnvironment --ek backend=docker --ek n_agents=$K \
  --ek budget_usd=2500 \
  --ek board_plugins=task/build/mmf_hub_plugin.py,task/environment/hub/game_plugin.py,task/environment/hub/keepalive_plugin.py \
  --ek hub_state_dirs=/var/lib/mindustry-many-fronts \
  -a multi_agent_harness.harbor.agent:TeamAgent --ak harness=claude-code --ak model=claude-opus-5-5 --ak effort=max \
  --ak orchestration=task/environment/hub/keepalive.py --ak timeout_sec=14400 --ak grade_at=30m,1h,2h,3h,4h \
  --env-file ~/.mindustry.env
```

- These are the settings of our reference runs (Claude Code, Opus 5.5 at max effort). `budget_usd` caps the team's
  model spend. The task is the same for every `K`; `n_agents` sets the team size.
- `keepalive.py` is the orchestration: `K` peers with the full task, and an agent that ends its session
  is woken again when a teammate messages it or every 30 minutes, until the time or the budget is spent. When the run
  ends it freezes the game. `keepalive_plugin.py` gives it the unread message counts.
- `mmf_hub_plugin.py` must come before `game_plugin.py` in `board_plugins`: it unpacks the game onto the hub, and
  `game_plugin.py` serves it. The hub compiles the bridge against the server JAR when the board starts.
- The game host, world profile and bridge source live in `task/tests/hidden/`. `make_hub_plugin.py` packs them into
  `task/build/mmf_hub_plugin.py` for the hub and `task/solution/hub_bundle.tar.gz` for the oracle; both are generated
  and git-ignored. Nothing in `task/environment/hub/` or `task/tests/hidden/` enters an agent's image. The verifier
  runs on the hub.
- One agent is `K=1`; there is no separate single-agent version.
- To use a Claude plan instead of an API key, put `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) in `~/.mindustry.env`
  and add `--ek model_upstream=subscription`. On the Docker backend also `export MULTI_AGENT_HARNESS_CLAUDE_ENV_SCRUB=0`:
  Claude Code's shell sandbox cannot start in a Docker agent box, and this runs it without (the agents' shell
  commands can then read the token). Other harnesses (Codex, Antigravity) and backends are described in the harness's
  README.

### Harbor

The oracle runs with plain Harbor, without the harness:

```bash
harbor run -p task -a oracle
```

It checks the pipeline. There is no true oracle: the solution unpacks the game from `solution/hub_bundle.tar.gz`, serves
it on loopback and plays the scripted bot in `solution/reference/bot.py` through the public API, one copy per unit, for 10 minutes
(`ORACLE_SEC`). It finds ore, builds drills and belts back to the core and defends, and scores well below a good team.
The score is in the trial's `verifier/reward.txt`. Real agents need the harness, which runs the game and the verifier
on its hub. Plain Harbor has no hub, so the oracle runs the game as the agent user and leaves the grading state in the
container; do not compare plain Harbor scores of real agents.

## Contents

| Path | What it is |
|---|---|
| `task/task.toml`, `task/instruction.md` | The Harbor task config and the agents' instructions |
| `task/environment/` | The agent image (`Dockerfile`; `data/` becomes `/app`: the API docs and `tools/game.py`; the image downloads the public Mindustry server JAR) |
| `task/environment/hub/` | Hub only, never copied into the image: the board's game plugin (`game_plugin.py`), the keepalive orchestration (`keepalive.py`) and its plugin, and `make_hub_plugin.py` |
| `task/tests/` | The verifier: `test.sh`, `run_verifier.py`, the grading rule `grade.py` and its tests `test_state.py` |
| `task/tests/hidden/` | The hidden game: the game host (`host.py`: one Mindustry server per world, per-unit rate limits, the grading state), a standalone server for it (`standalone.py`), the world profile and the bridge plugin's Java source (`bridge/`) |
| `task/solution/` | The oracle: `solve.sh` and the scripted bot `reference/bot.py` |

## Credits

The game is [Mindustry](https://github.com/Anuken/Mindustry) v160.7 by Anuken and contributors (GPLv3); the images
download its official, unmodified server JAR. The bridge in `task/tests/hidden/bridge/` is adapted from
[olamlabs/multi-agent-mindustry](https://github.com/olamlabs/multi-agent-mindustry) (commit 1374e46) by Olam Labs,
under GPL-3.0-only. This is not an official Mindustry project.
