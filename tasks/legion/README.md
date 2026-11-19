# 0 A.D. (LEGION)

A multi-agent task: write, from scratch, a game AI for 0 A.D. that beats Petra, the game's built-in AI.

0 A.D. Release 28 is an open-source historical real-time strategy game. The agents write an AI in JavaScript against
the game's own AI API, in a 4-hour run. The grader plays hidden 1v1 conquest matches against Petra, from Very Easy
up to four Elite levels above Very Hard and with each of its three personalities, and scores the playing strength
(Elo) the results imply.

## How it works

- **The same problem at every team size.** One instruction, one pool of practice matches and one grader for
  k = 1, 2, 4 or 8 agents. Only the number of agents changes.
- **A shared practice pool.** Petra is not in the agents' image. The only way to play against it is the practice
  office on the team's hub (board tools `practice_info`, `practice_play`, `practice_result`, `practice_status`):
  one pool of 30 matches for the whole team and the whole run, shared by every agent. Spending it well is part of
  the task. Matches between two of the team's own AIs (`legion.py spar`) are local and unlimited.
- **One repository, one AI.** The team works in one git repository; the AI in `legion/` and its description in
  `AI.md` on `main` are graded. The message board is how agents split the work and share practice results.
- **Graded during the run.** `main` is graded at minute 30 and hours 1, 2, 3 and 4. The agents are told the
  schedule but never see a grade; the grade at hour 4 is the score.
- **Petra never reaches an agent.** It is removed from the image that the hub and every agent sandbox share, and
  reaches only the hub, inside a board plugin, and the verifier. (With plain Harbor the verifier runs in the agent's
  container, after the agent has finished.)

## Grading

The verifier plays 120 hidden matches: a fixed subset of the 240-match gauntlet in `task/tests/gauntlet.json`, in
its shares of levels and personalities, on maps, seeds and civilizations the agents have not seen. Before playing
it checks the AI: imports, game-file integrity, and two Petra copy detectors (by statement and by runs of 12
canonical tokens); an AI over the limits scores 0. Every match the AI does not lose is played again from its logged
commands, and one that does not reproduce counts as a loss. A match still going at 60 game-minutes is decided on the
value of enemy units killed and structures destroyed.

The results give a maximum-likelihood Elo rating against fixed ratings for each Petra level and personality
(`task/tests/harness/ladder.json`, from Petra-vs-Petra games), with a prior of one virtual draw against Very Easy.
The score is that rating on a straight line from Petra Very Easy (0) to Petra Elite 4 (1.0), with no cap above; at
1.0 an AI would take about 94% of the points. The rule is `score_results` in `task/tests/harness/legion.py`, called
by `task/tests/verify.py`; the full match rules the agents get are in `task/environment/app/MATCH.md`.

## How to run

You need Docker, Python 3.12 and about 15 GB of disk for the image (the game's data is most of it). A team run
grades on an 8-CPU hub next to k agent sandboxes of 4 CPUs and 8 GB each. Set up once, from this folder:

```bash
python3 -m venv .venv && source .venv/bin/activate
git clone https://github.com/bespokelabsai/multi-agent-harness ~/multi-agent-harness
git -C ~/multi-agent-harness checkout 0f7ac9583906b998772245c41d799f7b2264dd4f
pip install -e "$HOME/multi-agent-harness[docker]"
echo "ANTHROPIC_API_KEY=..." > .env
```

This installs [multi-agent-harness](https://github.com/bespokelabsai/multi-agent-harness) at the commit the task was
tested with, and Harbor with it. The clone can live anywhere, but keep it: the harness builds its hub from it. All
commands below run from this folder.

### Multi-agent harness

A team of `K` agents (1, 2, 4 or 8) works in one repository with a message board and the practice office. One agent
is `K=1`.

```bash
python3 task/environment/hub/make_hub_plugin.py    # task/build/legion_hub_plugin.py: Petra for the hub, once

K=4
harbor run -p task \
  -e multi_agent_harness.harbor.environment:TeamEnvironment --ek backend=docker --ek n_agents=$K \
  --ek hub_cpus=8 --ek hub_memory_mb=16384 --ek budget_usd=2500 --ek hub_state_dirs=/var/lib/legion-hub \
  --ek board_plugins=task/build/legion_hub_plugin.py,task/environment/hub/practice_plugin.py,task/environment/hub/keepalive_plugin.py \
  -a multi_agent_harness.harbor.agent:TeamAgent --ak harness=claude-code --ak model=claude-opus-5-5 --ak effort=max \
  --ak orchestration=task/environment/hub/keepalive.py --ak timeout_sec=14400 --ak grade_at=30m,1h,2h,3h,4h \
  --env-file .env
```

- These are the settings of our reference runs (Claude Code, Opus 5.5 at max effort, $2,500 model budget for the
  team).
- `legion_hub_plugin.py` carries Petra and the trusted match harness to the hub, for the practice office
  (`practice_plugin.py`, `practice_run.py`): one pool of 30 matches against Petra for the whole team, 3 at once. It
  is generated because it embeds Petra; don't publish `task/build/`. The office keeps its pool and ledger in
  `/var/lib/legion-hub` on the hub, which `hub_state_dirs` adds to every checkpoint. Nothing in
  `environment/hub/` reaches an agent.
- `keepalive.py` runs K peers with the full task and wakes an agent that ended its session (on a message to it, or
  every 30 minutes) until the time is up; the CLIs' own subagents are off. `keepalive_plugin.py` gives it the
  board's unread counts.
- `grade_at` grades `main` at minute 30 and hours 1-4. The marks before the last are quick gradings on 24 of the 120
  matches during the run (in the trial's `marks/<label>/`); the last is the full grading and the score. After it, the
  final verifier grades each earlier mark again on all 120 matches from the AI it saved, as our checkpoint scores
  are: 30 to 60 minutes each on an 8-CPU hub, longer for stronger AIs. Those full grades are in the trial's
  `verifier/marks/<label>/`, with a summary in `verifier/marks/rewards.txt`. `LEGION_FULL_MARKS_S` in `task.toml`
  caps their time at 4.5 hours (0 turns them off), so the verifier always ends before its timeout and the score is
  kept.
- The task gives agents no network beyond the hub (`no-network`). The harness's cloud sandbox backend enforces that;
  Docker cannot, so on Docker the harness runs the agents with internet access and logs `network_not_enforced`. With
  internet, agents could read this repository, which holds the hidden gauntlet and Petra: for results meant to be
  compared, use a backend that enforces it (see the harness's README), as our reference runs did. The instruction
  forbids using the internet either way.
- To use a Claude plan instead of an API key, put `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) in `.env`
  and add `--ek model_upstream=subscription`. On the Docker backend also `export MULTI_AGENT_HARNESS_CLAUDE_ENV_SCRUB=0`:
  Claude Code's shell sandbox cannot start in a Docker agent box, and this runs it without (the agents' shell
  commands can then read the token). Other harnesses (Codex, Antigravity) and backends are described in the
  harness's README.

### Harbor

Plain Harbor runs the task without a hub, so an agent there has no board or practice office; run agents with the
multi-agent harness (above). Plain Harbor is for checking the pipeline:

```bash
harbor run -p task -a oracle
```

There is no true oracle for this task: `solution/solve.sh` installs a small original rush AI, which the verifier
grades like any submission (120 hidden matches against Petra; about 30 minutes, longer for a stronger AI, since every
match it does not lose is played twice). It scores close to 0 (0.0 in our check, Elo 560, below Petra Very Easy). The
scale's 1.0 is Petra Elite 4, and a Petra boosted above Elite 4 playing in the AI's seat scored above 1.0 when the
scale was built; no submitted AI comes near it (the best run in our published results scored 0.57). The score is in the trial's `verifier/reward.txt`, with the details
(Elo and its 95% interval, wins, draws and losses per level and personality) in `verifier/checks.json`.

## Contents

| Path | What it is |
|---|---|
| `task/task.toml`, `task/instruction.md` | The task |
| `task/environment/` | The image: Debian trixie with 0 A.D. 0.28.0 pinned from trixie-backports and Petra removed. `app/` becomes `/app`: `MATCH.md` (the match rules), `harness/` (match code, referee mod, dev gauntlet, ladder) and `legion/` (a starter bot that only gathers). `hub/` is for the hub only and is not copied into the image: the practice office, the orchestration and its plugin, and `make_hub_plugin.py` |
| `task/tests/` | The verifier: `test.sh`, `verify.py`, the trusted match harness, the hidden gauntlet, the game-file hashes, and `hidden/petra.zip` (Petra's 32 original files from the pinned `0ad-data` package, byte for byte, with its sha256 and licence) |
| `task/solution/` | `solve.sh` and the reference solution, a small original rush AI in `reference/main.js` (it scores close to 0), to check the pipeline |

## Credits

[0 A.D.](https://play0ad.com) is made by Wildfire Games. The game is installed in the image from Debian's `0ad` and
`0ad-data` packages (Release 28, version 0.28.0): its engine and code are under the GNU GPL version 2 or later, and
its data under CC BY-SA 3.0 (the packages' copyright files list the details). `task/tests/hidden/petra.zip` holds the
unmodified files of Petra, the game's built-in AI, from `0ad-data`, which Debian ships under CC BY-SA 3.0, Copyright
Wildfire Games; its licence is in `task/tests/hidden/LICENSE-CC-BY-SA-3.0.txt`. The match harness, referee, verifier,
starter bot and reference AI were written for this task.
