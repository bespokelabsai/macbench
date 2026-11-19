# SEED TO PROFIT

A multi-agent task: start an online consumer goods company with $2,000,000 of seed money and run it. The team picks
products from a catalog of 30, writes the software that runs the company every day (prices, ads, stock, money,
growth), buys research from a market office, and changes the software as it learns.

After a launch phase (the first hour), the company is live: its three simulated years run while the team works, one
month about every 4.7 minutes, each month on the software that is on `main` at that moment. The team watches its
books and fixes what goes wrong. The score is what the company is worth at the end, and how well the software the
team leaves behind starts and runs 24 new companies on its own.

## How it works

- **The market stays hidden.** The agents never see the market simulator. It runs on the harness's hub, where the
  market office sells research (market reports, surveys, ad tests, practice markets for the team's software) from a
  fixed research budget, and where the live company runs. The manual (`task/environment/app/docs/REFERENCE.md`)
  lists every model the market uses, but not its numbers.
- **One company, shared money.** The team shares one company, one bank account, one research budget and one
  `main`. Only the agent who ordered a piece of research sees its result, until it shares it.
- **The company runs while the team works.** Each month runs the software on `main` when the month starts, so
  broken or late software costs real money. Halfway through, the market changes in ways the team is told about,
  and in others it is not.
- **The same problem at every team size.** One instruction and one manual for k = 1, 2, 4 or 8 agents. Agents work
  until the time is up: an agent that ends its session is woken again with the time left and its unread messages
  (`task/environment/hub/keepalive.py`).

## Grading

```
score = 0.5 x live score + 0.5 x playbook score
m     = company value / $2,000,000 - research spent / $2,000,000
a company's score = 0 if bankrupt, else min(1, max(0, log2(m) / 5))
```

- **Live score.** The company the team ran, valued when its three years end: cash, stock at liquidation value,
  minus debt, tax and refunds still to come, plus 3 times the last year's operating profit if it is positive. The
  grader also replays the company from its record (the commit each month ran) in a fresh copy of the world, and
  the lower of the two values counts.
- **Playbook score.** After the run, the team's final `company/plan.yaml` and `ops/` start 24 new companies in fresh
  markets and run each for three years on their own. Three quarters of the markets have the shapes the office's
  practice markets use, a quarter shapes the team never saw.

Each doubling of the seed money adds 0.2. The rule is in `task/tests/verify.py` (`grade_live`) and
`task/tests/hidden/marketsim/marketsim/scoring.py`; the agents read it in their manual ("How you are scored"). The
team's `main` is graded at 30 minutes and at hours 1, 2, 3 and 4; a graded mark during the run reads the company's
value so far, and the score is the grade at hour 4.

## How to run

You need Docker and Python 3.12, and a machine with about 8 CPUs and 16 GB of memory for the hub. Set up once, from
this folder:

```bash
python3 -m venv .venv && source .venv/bin/activate
git clone https://github.com/bespokelabsai/multi-agent-coordination-bench ~/mac-bench
git -C ~/mac-bench checkout cd6c6ce43168d531033699d1c3f3aefcbae791b9
pip install -e "$HOME/mac-bench/harness[docker]"
echo "ANTHROPIC_API_KEY=..." > .env
```

This installs [multi-agent-harness](https://github.com/bespokelabsai/multi-agent-coordination-bench/tree/cd6c6ce43168d531033699d1c3f3aefcbae791b9/harness) (this repository's `harness/`) at the commit the task was
tested with, and Harbor with it. The clone can live anywhere, but keep it: the harness builds its hub from it. All
commands below run from this folder.

### Multi-agent harness

A team of `K` agents (1, 2, 4 or 8) shares one repository, the company, the market office and a message board. One
agent is `K=1`.

```bash
python3 task/environment/hub/make_hub_plugin.py   # task/build/seedprofit_hub_plugin.py: the hidden simulator for the hub, once

K=4
harbor run -p task \
  -e multi_agent_harness.harbor.environment:TeamEnvironment --ek backend=docker --ek n_agents=$K \
  --ek hub_cpus=8 --ek hub_memory_mb=16384 --ek hub_size_at=start --ek budget_usd=500 \
  --ek hub_state_dirs=/var/lib/seedprofit-hub \
  --ek board_plugins=task/build/seedprofit_hub_plugin.py,task/environment/hub/market_plugin.py,task/environment/hub/keepalive_plugin.py \
  -a multi_agent_harness.harbor.agent:TeamAgent --ak harness=claude-code --ak model=claude-opus-5-5 --ak effort=max \
  --ak orchestration=task/environment/hub/keepalive.py --ak timeout_sec=14400 --ak grade_at=30m,1h,2h,3h,4h \
  --env-file .env
```

- These are the settings of our reference runs: Claude Code on Opus 5.5 at max effort, 4 hours, a $500 model
  budget per team (`budget_usd`), and `keepalive.py` for every team size (Claude Code's own subagents are off).
- `seedprofit_hub_plugin.py` unpacks the hidden simulator on the hub and starts a fresh market for each run; it is
  generated because it embeds the simulator, so don't publish `task/build/`. `market_plugin.py` is the market
  office and the live company (its tools appear on the board), and `keepalive_plugin.py` serves the keepalive
  strategy. Nothing in `environment/hub/` reaches an agent.
- `hub_state_dirs` keeps the market office's secret, ledger and the live company in the harness's checkpoints.
- For the board-off comparison (every message refused, the prompt unchanged), add
  `--ek board_policy=task/environment/hub/no_messages_policy.py`.
- On Docker, installing the agents' CLIs can take longer than Harbor's default setup limit on a small machine; add
  `--agent-setup-timeout-multiplier 3` if setup times out.
- Docker cannot enforce the task's `no-network` policy for agents, so on Docker they have internet access (see the
  [harness's README](https://github.com/bespokelabsai/multi-agent-coordination-bench/blob/cd6c6ce43168d531033699d1c3f3aefcbae791b9/harness/README.md)).
- To use a Claude plan instead of an API key, put `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) in `.env`
  and add `--ek model_upstream=subscription`. On the Docker backend also `export MULTI_AGENT_HARNESS_CLAUDE_ENV_SCRUB=0`:
  Claude Code's shell sandbox cannot start in a Docker agent box, and this runs it without (the agents' shell
  commands can then read the token). Other harnesses (Codex, Antigravity) and backends are described in the
  [harness's README](https://github.com/bespokelabsai/multi-agent-coordination-bench/blob/cd6c6ce43168d531033699d1c3f3aefcbae791b9/harness/README.md).

### Harbor

Plain Harbor runs the task without a hub, so there is no market office and no live company: the live score is 0
and only the playbook is graded. Run agents with the multi-agent harness (above). Plain Harbor is for checking the
pipeline:

```bash
harbor run -p task -a oracle
```

There is no true oracle for this open-ended task: `task/solution/solve.sh` commits a plain rule-based company
(`task/solution/reference/`: ten products picked from the public catalog, typical prices, fixed ads and reorder
rules), and the verifier runs its playbook in 24 fresh markets. It scores what that company earns, 0.03 to 0.06 in our checks
(half of a playbook score of 0.06 to 0.13; the markets are fresh every run), as a baseline, not a ceiling. The score is in the trial's
`verifier/reward.txt`, with the details in `verifier/details.json`.

## Contents

| Path | What it is |
|---|---|
| `task/task.toml`, `task/instruction.md` | The task |
| `task/environment/` | The agents' image (Python with the usual data tools) and the team's starting repo (`app/`: the manual `docs/REFERENCE.md`, the public catalog `docs/catalog.json`, an example plan and autopilot, and `tools/check_ops.py`). `hub/` is for the hub only and is not copied into the image: the market office and live company (`market_plugin.py`), the orchestration and its plugin, the board-off policy, and `make_hub_plugin.py` |
| `task/tests/` | The verifier (`test.sh`, `verify.py`) and `hidden/marketsim/`: the market simulator, the market office and the live company's engine |
| `task/solution/` | The reference plan and software, and `solve.sh` |

## Credits

The market's numbers are calibrated against public sources: US Census retail series and FRED for seasonality and
the economy; marketplace seller fee schedules and industry benchmarks (Jungle Scout, Helium 10, Flexport and
others) for fees, fulfillment, ads and supplier terms; SEC filings and Carta data for financing terms; and published
studies (NBER, MSI, HBS) for demand, reviews and advertising response. No third-party data files are shipped.
