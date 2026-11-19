# CAVE RESCUE

A multi-agent task: run rescue control for a caving expedition whose trips are overdue in a real cave that is
flooding.

Eight trips (13 people) are missing in cave 1623/258 on the Loser plateau, Austria: a real 21 km, 912 m deep system
built from the Cambridge University Caving Club survey. A storm floods the lower cave over 36 simulated hours. The
agents fly a shared fleet of drones, read the drone photos (people, chalk notes and arrows, dropped gear, water),
place radio relays, chargers and survival kits, and send rescue teams along routes that are still passable when they
get there.

## How it works

- **A live world.** The sim clock starts at the first command and keeps running whether or not anyone acts. A run
  lasts 4 hours of real time, and the 36-hour storm plays out within it.
- **Shared resources.** Drones, batteries, relays, chargers, kits, rescue teams and the clock are shared by every
  agent, so the team has to split the work and agree on a plan.
- **Photos are the only way to see.** People and clues appear only in rendered photos, with no text ground truth.
- **The same problem at every team size.** One scenario and one instruction for k = 1, 2, 4 or 8 agents. Only a short
  paragraph naming the number of controllers changes.
- **The sim stays hidden.** It runs on the hub. The scenario, photos and seal key ship encrypted, and agents act only
  through the `cr` command.

## Grading

Each missing group earns credit, weighted by its head count:

- found in a photo and reported while alive: `0.25 × (1 − t_found / 36 h)`
- survival kit delivered before the group's cold limit: `0.25 × (1 − t_kit / cold limit)`
- reached alive by a rescue team: `0.50`

Each false report costs 0.05 and each lost drone costs 0.01. The total is divided by the score of a relaxed oracle
(0.8769) and clipped to [0, 1]. A run can be graded at any time by replaying its sealed order log. The rule is
`Engine.score` in `task/crsim/engine.py`.

## How to run

You need Docker, Python 3.12 and the DejaVu fonts (on Debian/Ubuntu: `apt install python3-venv fonts-dejavu-core`).
Set up once, from this folder:

```bash
python3 -m venv .venv && source .venv/bin/activate
git clone https://github.com/bespokelabsai/multi-agent-harness ~/multi-agent-harness
git -C ~/multi-agent-harness checkout 0f7ac9583906b998772245c41d799f7b2264dd4f
pip install -e "$HOME/multi-agent-harness[docker]"
pip install numpy Pillow       # for the photos
cd task
echo "ANTHROPIC_API_KEY=..." > .env
```

This installs [multi-agent-harness](https://github.com/bespokelabsai/multi-agent-harness) at the commit the task was
tested with, and Harbor with it. The clone can live anywhere, but keep it: the harness builds its hub from it. All commands below run from `task/`. The first build renders the photo library
(285 MB, about an hour) into `task/build/s5`; later builds reuse it.

### Multi-agent harness

A team of `K` agents (1, 2, 4 or 8) shares the drones over a message board:

```bash
./build.sh          # build-mah/caverescue-m{1,2,4,8} + build-mah/cr_board.py

K=4
harbor run -p build-mah/caverescue-m$K \
  -e multi_agent_harness.harbor.environment:TeamEnvironment --ek backend=docker --ek n_agents=$K \
  --ek hub_cpus=2 --ek hub_memory_mb=8192 --ek budget_usd=5000 \
  --ek board_plugins=build-mah/cr_board.py \
  -a multi_agent_harness.harbor.agent:TeamAgent --ak harness=claude-code --ak model=claude-opus-5-5 --ak effort=max \
  --ak orchestration=$([ $K = 1 ] && echo mah/solo.py || echo async) \
  --ak timeout_sec=14400 --ak grade_at=30m,1h,2h,3h,4h \
  --env-file .env
```

- These are the settings of our reference runs (Claude Code, Opus 5.5 at max effort). `budget_usd` caps the team's
  model spend.
- k = 1 uses `mah/solo.py` (one agent, its CLI's own subagents off); k > 1 uses the harness's `async`
  orchestration (a lead plus k − 1 subagent slots).
- `grade_at` grades the team at 30 minutes and hours 1–4; the last grade is the score. The verifier replays the
  sealed order log and plays out the rest of the 36-hour storm with no new orders.
- Every agent box runs as root, so `build.sh` ships the scenario, photos and seal key encrypted; only
  `build-mah/cr_board.py` (the hub's plugin, never in an agent image) has the passphrase. Don't publish
  `build-mah/`. The builds split 16 CPUs and 32 GB over the k agent boxes.
- To use a Claude plan instead of an API key, put `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) in `.env`
  and add `--ek model_upstream=subscription`. On the Docker backend also `export MULTI_AGENT_HARNESS_CLAUDE_ENV_SCRUB=0`:
  Claude Code's shell sandbox cannot start in a Docker agent box, and this runs it without (the agents' shell
  commands can then read the token). Other harnesses (Codex, Antigravity) and backends are described in
  the harness's README.

### Harbor

One agent, with plain Harbor:

```bash
./build_single.sh   # build-single/caverescue

harbor run -p build-single/caverescue -a claude-code -m anthropic/claude-opus-5-5 --ak reasoning_effort=max --env-file .env
```

The agent runs as an unprivileged user next to the rescue service. The verifier runs in a separate container with
no network and grades the sealed order log; the score is in the trial's `verifier/reward.txt`.

`harbor run -p build-single/caverescue -a oracle` checks the pipeline. There is no true oracle for this task,
since where the people are shows only in the photos: `solution/solve.sh` starts the service, reads the map and sends
six drones into the first leads, and scores 0. For a quick look at a real agent, add `--agent-timeout-multiplier 0.01`
(about 2.5 minutes instead of 4 hours).

### Rebuilding the cave graph

`task/caves/c258.json` is built from the raw survey (Survex `.3d` and the expedition's survey metadata, not
included), from `task/`:

```bash
cave/build_graph.py data/c258.3d caves/c258.json
cave/survey_meta.py data/loser/caves-1623/258 caves/c258.json
```

## Contents

| Path | What it is |
|---|---|
| `task/` | The Harbor task (`task.toml`, `instruction.md`, `environment/`, `tests/`, `solution/`) and its sources: the sim, data, the build scripts (`build_single.sh` for one agent, `build.sh` for teams), and the team's message-board plugin and k = 1 orchestration (`mah/`) |
| `task/crsim/` | The sim (drones, radio, batteries, kits, teams, flooding, scoring), its server, grader and `cr` CLI, and scenario and photo generation |
| `task/cave/` | Survey processing and the photo renderer |
| `task/caves/c258.json` | The cave graph |
| `task/scenarios/s5.json` | The scenario |
| `task/assets/fonts/` | Handwriting fonts for the chalk marks |

## Credits

Cave geometry: the Cambridge University Caving Club Austria expedition survey (expo.survex.com).
Chalk fonts: Google Fonts, under the SIL Open Font License.
