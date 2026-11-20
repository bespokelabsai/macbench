# multi-agent-harness

Run N coding agents (Codex, Claude Code or Google Antigravity), each in its own sandbox, coordinating over a shared message board. A whole team is one Harbor trial, started with a plain `harbor run`, so it gets Harbor's verifier, job dirs and trajectories for free. Sandboxes come from the [Universal Sandbox Interface](https://github.com/bespokelabsai/sandbox-router) (`bespokelabs-sandbox`); it runs on Daytona or local Docker (see Docker below).

[MAC Bench](https://benchmarks.bespokelabs.ai/mac-bench/) was run with this harness; see its release for how to reproduce its results. Options added since are off by default.

```
host: harbor run
   |
   |  Universal Sandbox Interface (Daytona or Docker)
   v
hub sandbox (Harbor's main service, verifier runs here)
   gateway :8080  ->  board, proxy (--> model APIs), git
   ^
   |  the gateway's exposed URL, private, access header required
   |  (Daytona: https://8080-<hub id>.<proxy domain> + preview token)
   |
agent-1   agent-2   ...   agent-N   (one CLI each)
```

Any Harbor task Dockerfile works unmodified. Every sandbox is built from the task's `environment/Dockerfile` through the SDK; the hub installs its own runtime and each agent gets its CLI the way vanilla Harbor installs it. Agents reach the hub through the URL the backend exposes for one hub port. With the usual `[agent] network_mode = "no-network"`, agents can reach that one host and nothing else while they work. They talk on the board (MCP), share code through the hub's git remote, and call the model through the proxy, which meters spend per agent and stops at a budget. Design notes are in `docs/architecture.md`.

The hub is the one extra sandbox in each trial. It holds the board, the model proxy and the team git remote behind one gateway port. In Harbor's terms it is the trial's main environment, so a shared verifier runs there. It runs no agent.

## Layout

```
src/multi_agent_harness/  the harness, one package
  board/          message board server and CLI (runs on the hub)
  proxy/          model API proxy with metering and budget (runs on the hub)
  gitserver.py    the team git remote, smart HTTP with per-agent push rules (runs on the hub)
  gateway.py      the hub's one exposed port, routing to board, proxy and git (runs on the hub)
  workdir.py      task workdir: resolve it, seed the team repo, check main out before verify (runs on the hub)
  harbor/         TeamEnvironment, SandboxMember, TeamAgent, multi-agent-harness-cleanup (runs on the host)
  orchestrations/ the bundled orchestration strategies, one file each (runs on the host)
tests/            unit tests, no network
docs/             architecture, board and proxy reference
examples/         peripheral: the hello-team tasks and an example board policy
scripts/          peripheral: coordination metrics
```

## Requirements

- Python 3.12
- A key for the sandbox backend: for Daytona, `DAYTONA_API_KEY` with sandbox create/delete scopes
- An OpenAI key (Codex), an Anthropic key (Claude Code) and/or a Gemini key (Antigravity), or a ChatGPT or Claude subscription login (see Model upstreams)
- For the Docker backend instead of Daytona: a local Docker daemon

## Install

In a Python 3.12 virtualenv, from the repo root:

```
pip install -e ".[harbor]"
```

With uv, `uv venv -p 3.12 --seed` gives the venv its own pip, or use `uv pip install -e ".[harbor]"`. That gives you the Harbor plugin (`harbor==0.23.0`, pinned: the environment implements that version's per-phase network contract), the Universal Sandbox Interface (`bespokelabs-sandbox`) with its Daytona backend (pinned to commit `a96416d` of [bespokelabsai/sandbox-router](https://github.com/bespokelabsai/sandbox-router)), `multi-agent-harness-cleanup` and what the scripts need. For the local Docker backend (`--ek backend=docker`) install `".[docker]"` instead, which adds the SDK's docker-py client. The `hub` extra (board and proxy) only has to exist inside the hub image, and `dev` is both plus pytest. To run the tests:

```
pip install -e ".[dev]"
python -m pytest tests
```

## Images

Hub and agents are each built from the task's `environment/Dockerfile` (or its prebuilt `docker_image`) through the SDK's `dockerfile=`, with the task's dir as the build context. The hub is built first and agents after it, so they reuse the backend's build; on Daytona that is a declarative build. The harness creates no named snapshots and never deletes or changes one. `--ek backend_options='{...}'` is passed to every sandbox's create as the SDK's `backend_options`. Networking keys are refused there by the SDK; the policy comes from `task.toml`. `auto_snapshot` is accepted and ignored.

## What gets installed where

The hub installs git, curl, a uv-managed Python and the `multi-agent-harness[hub]` wheel into `/opt/multi-agent-harness` at start; each agent gets its CLI through Harbor's own installed-agent class, as in a vanilla trial (`--ak cli_version=<v>` pins it). Nothing is baked into the image. Details in [docs/architecture.md, Hub runtime](docs/architecture.md#hub-runtime).

## Network policy

Set Harbor's usual `network_mode` per phase in `task.toml`: keep `[environment]` `public` so installs work, and use `no-network` for `[agent]` to let agents reach only the hub. Every switch is checked from inside the sandboxes, and a failed check on a restricted policy stops the trial. Allowlists take hostnames only. Details in [docs/architecture.md, Network policy](docs/architecture.md#network-policy).

## Running hello-team

hello-team is a toy task: every agent appends a distinct number to a shared file, and the team elects a leader. Put `DAYTONA_API_KEY` and `OPENAI_API_KEY` (or `ANTHROPIC_API_KEY`) in `.env`, then:

```
harbor run -p examples/tasks/hello-team \
  -e multi_agent_harness:TeamEnvironment --ek backend=daytona \
  --ek n_agents=3 --ek budget_usd=5 \
  -a multi_agent_harness:TeamAgent --ak harness=codex --ak model=gpt-6-luna \
  --env-file .env -o jobs
```

### Docker

The same run on the local Docker daemon instead of Daytona, after `pip install -e ".[docker]"`:

```
harbor run -p examples/tasks/hello-team \
  -e multi_agent_harness:TeamEnvironment --ek backend=docker \
  --ek n_agents=3 --ek budget_usd=5 \
  -a multi_agent_harness:TeamAgent --ak harness=codex --ak model=gpt-6-luna \
  --env-file .env -o jobs
```

No extra flags are needed. The hub's gateway port is published on the Docker bridge's gateway IP (read from `docker network inspect bridge`, else `172.17.0.1`; never `0.0.0.0`), and agents on the bridge reach the hub there. Docker has no allow-lists, so it can't keep the hub reachable under a restricted policy: a `no-network` (or allow-list) phase runs with public networking instead, the harness logs a warning once and writes a `network_not_enforced` line to the sandbox ledger. Agents on Docker therefore have internet access and can reach each other by IP; use Daytona when the task's network isolation matters. Both defaults apply only when you don't set them: `--ek backend_options` with its own `ports` and a task with a `public` agent phase are used exactly as given, e.g. `--ek 'backend_options={"ports": {"8080/tcp": {"HostIp": "172.17.0.1", "HostPort": null}}}'`.

### Model upstreams

`--ek model_upstream` picks where the hub's proxy sends model calls. Keys are read from `.env` by name and stay on the hub.

- `direct` (the default): the provider APIs, with `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` and `GEMINI_API_KEY`, each used only if set.
- `subscription`: ChatGPT logins for Codex, or a Claude Pro/Max token for Claude Code (below).
- `<url>`: any gateway that serves the provider formats under that root (`/v1/messages`, `/v1/responses`, `/v1beta/models`), with the provider keys as in direct, e.g. `--ek model_upstream=https://gateway.example.com`.

The team budget holds on every upstream: each call is priced from `src/multi_agent_harness/proxy/pricing.json`, and a model without a price is refused. Details in [docs/proxy.md](docs/proxy.md#upstreams).

Codex agents can also run on ChatGPT subscription logins instead of an API key: run `codex login --device-auth` once on the host, then add `--ek model_upstream=subscription`. The login stays on the hub, spend is still metered at API prices against the budget, and several logins can share the agents. See [docs/proxy.md](docs/proxy.md#chatgpt-subscription).

With `--ak harness=claude-code` the same `--ek model_upstream=subscription` runs Claude Code on your own Pro/Max plan: run `claude setup-token` once, put the token in `.env` as `CLAUDE_CODE_OAUTH_TOKEN`, and each agent's unmodified Claude Code uses it to call Anthropic directly. The hub never sees it, and no budget cap applies; usage is priced at API rates from the transcripts after the run. See [docs/proxy.md](docs/proxy.md#claude-subscription).

Google Antigravity runs with `--ak harness=antigravity`, direct with `GEMINI_API_KEY` and an agy slug (`agy models`, default `gemini-3.8-flash-low`), or through a gateway URL. agy is pinned to 1.4.4. See [docs/architecture.md, Antigravity](docs/architecture.md#antigravity).

`--ak effort` sets every agent's reasoning effort: `claude --effort` for Claude Code (low, medium, high, xhigh, max) and `model_reasoning_effort` for Codex (minimal, low, medium, high, xhigh, max; not ultra, which delegates to subagents). Left out, each CLI uses its own default; a value a harness in the team doesn't take is refused, and Antigravity takes none: its thinking level comes from the model slug (`gemini-4-argon-high` is Argon at high thinking, the API's top level).

A team can mix harnesses and models. `--ak team` is a list of groups, handed out to agent-1..N in order; one group may leave out its count and takes the rest. This team of 8 has four Codex agents on gpt-6-astra and four Claude Code agents on claude-opus-5-5:

    --ek n_agents=8 --ak harness=codex --ak model=gpt-6-astra \
      --ak 'team=[{"count": 4}, {"harness": "claude-code", "model": "claude-opus-5-5"}]'

With `--ek model_upstream=subscription` the Codex agents use the ChatGPT logins and the Claude Code agents use `CLAUDE_CODE_OAUTH_TOKEN`, so `budget_usd` caps only the Codex agents. See [docs/architecture.md, Mixed teams](docs/architecture.md#mixed-teams).

Use `-p` with a local path. `-t` means a task from the Harbor registry. Add `--dry-run` to check the config without creating anything. For Claude Code use `--ak harness=claude-code` and a Claude model. `budget_usd` is a hard cap on model spend for the whole team; `agent_budget_usd` caps each agent. `examples/tasks/hello-team-separate` is the same task with the verifier in its own sandbox and runs the same way. `backend=daytona` is the default; `multi_agent_harness.harbor.environment:DaytonaTeamEnvironment` still works as an alias. `multi_agent_harness:TeamEnvironment` and `multi_agent_harness:TeamAgent` are short for `multi_agent_harness.harbor.environment:TeamEnvironment` and `multi_agent_harness.harbor.agent:TeamAgent`, which keep working.

### GPU agent sandboxes

Add `--ek n_gpus=K` to give **one GPU each to K of the existing agent sandboxes**. The default is zero, and K must be an integer between zero and `n_agents`. CPU, RAM, and disk settings still apply to every sandbox. For example, add these options to a normal run:

```sh
--ek backend=daytona --ek n_agents=6 --ek n_gpus=2 --ek gpu_type=H100
```

This allocates GPUs to `agent-5` and `agent-6`. Allocation always selects the last K slots in numeric order, keeping the standard lead on CPU unless every agent has a GPU. `gpu_type` is optional; omit it for Daytona's default NVIDIA GPU selection. Currently supported types are H100, H200, B200, B300, RTX-PRO-6000, RTX-4090, and RTX-5090, subject to provider capacity. GPU allocation currently requires Daytona.

Use a task image containing the required CUDA/framework tools. The harness checks that exactly one GPU is visible through `nvidia-smi` before starting agents and records its model and memory. A missing or wrong GPU fails setup and cleans up the team; it never silently falls back to CPU. The image must also build and start on the CPU hub.

Only GPU-enabled teams receive resource context and an instruction to inspect CPU, RAM, disk and GPU availability and post a board summary. Ordinary wakes reuse that context; new identities and checkpoint-restored sandboxes get fresh context. CPU-only prompts are unchanged.

With `--ak orchestration=async`, `spawn_agent(instructions, gpu=True)` requires a GPU slot; the default `gpu=False` uses CPU slots in mixed pools, preserving GPU capacity. When the worker pool has no CPU slots at all, ordinary work can use GPUs; queued GPU-required work takes priority. Requests queue while their resource class is busy and fail before registration when none exists. Idle subagents retain their slots until the lead terminates them; `agent_status(detail=True)` shows assignments and capacity.

The hub and both verifier modes stay CPU-only. Harbor's per-environment `gpus`/`--override-gpus` are not the team count. Checkpoint resume requires the same GPU plan; legacy checkpoints are CPU-only. Task images supply CUDA/framework dependencies. GPUs remain allocated while idle until teardown; `budget_usd` caps model spend, **not infrastructure charges**.

### Hub size

Every sandbox, the hub included, gets the task's `[environment]` size. A shared verifier runs on the hub, and it may need much more than an agent does. `--ek hub_cpus=N --ek hub_memory_mb=M` give the hub a size of its own, for example `--ek hub_cpus=32 --ek hub_memory_mb=65536`. The agents keep the task's size.

By default (`--ek hub_size_at=verify`) the hub is created at the task's size and grown to that size once the agents are done, right before the verifier runs. So the hub stays small while the agents work. On Docker this is `docker update` of the hub container's CPU quota and memory limit (memory plus swap is set to twice the memory, as Docker does at create), and the processes in it keep running. On Daytona it is a hot resize, which can only grow a sandbox, and the org's per-sandbox limits apply. A resize that fails is logged and written to the ledger (`hub_resize_failed`), and the verifier then runs at the old size. A resize that works is ledgered as `hub_resized`, with the cgroup limits read back from inside the hub. `--ek hub_size_at=start` creates the hub at the given size instead; backends other than Docker and Daytona need it.

## Outputs

Each trial lands in `jobs/<job>/<task>__<id>/`:

- `agent/agent-i/`: that agent's CLI logs and `trajectory.json`
- `agent/board-events.jsonl`: every board event, the record of who said what
- `agent/proxy-usage.jsonl` and `proxy-usage.json`: every model call, and spend per agent
- `artifacts/logs/artifacts/multi-agent-harness/`: `team.bundle` (the team repo, all refs) and `board-events.jsonl`, exported from the hub for the verifier
- `team.bundle`: the team repo, downloaded straight to the trial dir before the hub is deleted, even when the trial fails
- `verifier/reward.txt`: the score
- `marks/<label>/`: with `--ak grade_at`, each graded mark's verifier output (see Graded marks)
- `sandbox-ledger.jsonl`: every sandbox id created and deleted (with the backend, its resume state and exec call counts), each network policy switch, and `agent_lost` / `agent_gone` lines for agents whose sandbox disappeared
- `subscription-usage.json`: on Claude subscription runs, API-equivalent usage priced from the agents' transcripts

Trial cost and token counts in `result.json` come from the proxy. The run metadata lists `lost_agents`, and `resumed_from` on a resumed run.

If the backend loses an agent's sandbox mid-run, the harness logs it (`agent-2 lost: ...`), the other agents carry on, and the trial still goes to the verifier. The hub is the exception: losing it fails the trial, and the latest checkpoint is what still has the team repo.

## Checkpoint and resume

While agents work, the harness checkpoints the team every 15 minutes (`--ak checkpoint_every_min=N`, 0 turns it off), and once more when the agent phase ends or is interrupted. Agents are never paused for it. To resume an interrupted trial, run the same command again with the checkpoint dir added:

```
harbor run -p examples/tasks/hello-team ... --ak checkpoint_every_min=15 -o jobs
# interrupted; then
harbor run -p examples/tasks/hello-team ... --ek resume_from=jobs/<job>.checkpoints/<trial>/<n>-<stamp> -o jobs
```

Checkpoints live next to the job dir, in `<job dir>.checkpoints/<trial>/<n>-<utc stamp>/`: the team repo (`team.bundle`), the board and proxy logs, each agent's CLI session files (token files left out, secrets redacted) and a `manifest.json`. The last 3 are kept, plus the final one. The resumed run must match the checkpoint's task, harness, `n_agents`, and GPU allocation (`n_gpus` and `gpu_type`). It starts from the checkpoint's main and branches, carries spend over against the budget, gets the original agent timeout minus the time already used (it refuses with under 2 minutes left), and each agent continues its CLI session, or starts fresh with a note if that fails.

What a resume loses: anything after the last checkpoint, work that was not pushed (each agent gets a fresh clone of main), and the board's state such as inbox cursors. Earlier board messages are in each agent's sandbox as a file, not on the board. With `orchestration=async` only the lead restarts. Details in [docs/architecture.md, Checkpoint and resume](docs/architecture.md#checkpoint-and-resume).

Do not use `harbor jobs resume` on an interrupted multi-agent job. It deletes trial dirs without a result, and cancelled trials, and reruns them from scratch.

## Graded marks

`--ak grade_at=1h,2h,4h,8h,16h` grades the team's `main` at those times while the agents keep working. Times count agent time from when the agents start; `m` and `s` work too (`--ak grade_at=2m,4m,8m`). At each mark the harness pins main's commit, takes a checkpoint labelled with the mark (retention never deletes these), and queues a grading. One grading runs at a time; a mark that comes while the last one is still grading waits for it. Agents are never paused or told anything by the harness; the task decides what agents hear about the schedule. Strategies can read `team.next_mark()`.

A grading runs the task's own verifier on the hub, the same script with the same `[verifier]` env, user and timeout as the final verifier, under the agent phase's network policy. It runs against a fresh checkout of the pinned commit, not the hub's workdir, and writes to a folder of its own. The task's test script must read two environment variables to do that:

- `MULTI_AGENT_HARNESS_GRADE_APP`: the checkout of the pinned commit (for the final verifier, unset: grade the workdir)
- `MULTI_AGENT_HARNESS_GRADE_OUT`: where to write `reward.txt` or `reward.json` and anything else (for the final verifier, unset: `/logs/verifier`)

`MULTI_AGENT_HARNESS_GRADE_MARK` (the mark's label, such as `h04`) and `MULTI_AGENT_HARNESS_GRADE_COMMIT` are set too. With `--ek hub_cpus` / `hub_memory_mb`, the hub grows for each grading and goes back to its size after it (on Daytona, which only grows, it stays at grading size). Only shared-verifier tasks can use marks.

A mark at the end of the run is the final verifier: it is not graded twice, and the trial's reward is the final verifier's, as always. Each mark's output lands in `<trial>/marks/<label>/` (`reward.txt`, `details.json`, `test-stdout.txt`, ...; the final mark's is a copy of `verifier/`). `result.json` lists them in the agent metadata as `marks`: `{mark, mark_s, elapsed_s, commit, reward, verifier_s, status, final, dir, checkpoint}`, and the ledger has `mark` and `mark_graded` lines. A grading that fails or times out is recorded with its `status` and `error`, and the run goes on. Gradings still queued when the agents end finish before the final verifier starts. The last mark is labelled `end` when the run ends before it. Checkpoint manifests carry the records, so a run resumed with `--ek resume_from` keeps the marks already graded (their folders are copied into the new trial) and grades again any mark whose grading had not finished.

The checkpoint taken at a mark is linked as `<job dir>.checkpoints/<trial>/mark-<label>`, and once grading ends the mark's grade goes into its manifest and into every other checkpoint of the same run that carries the mark. A resumed run never writes to the checkpoints it came from. `--ek resume_from=<...>/mark-h02` starts a new run from it. The clock goes on from there: the agents get the run's time minus what was used, the later marks fire at their usual times (h04 and h08 here), and each agent's resume note names the next mark.

A task that keeps state of its own on the hub (outside the team repo, the board and the proxy) names those folders with `--ek hub_state_dirs=/var/lib/my-task[,/other]`. Every checkpoint packs them as root, with their modes, into `hub-state.tgz`, and a resumed run unpacks them on the hub before any board plugin loads. They never reach the agents.

## Cleanup

`stop()` deletes every sandbox it created, by id. If a run dies first, run `multi-agent-harness-cleanup jobs/<job> --dry-run`, then without `--dry-run`. It only touches ids in that run's ledgers, never lists sandboxes and never touches snapshots; sandboxes also carry a TTL as a backstop. Details in [docs/architecture.md, Teardown and the id ledger](docs/architecture.md#teardown-and-the-id-ledger).

## Writing your own task

A task is a normal Harbor task. `instruction.md` is given to every agent behind a short preamble (its name, teammates, the board tools, that its workdir is a clone of the team repo and what it may push).

The task workdir (`[environment] workdir`, else the Dockerfile's `WORKDIR`, else the sandbox's) seeds the team repo: the image's starting files there become the first commit on `main`, respecting `.gitignore`, and every agent works in a clone of it at the same path. Right before verification, the hub's workdir is replaced by a clean checkout of `main`, so `tests/test.sh` and the task's `artifacts` see exactly what the team pushed; Harbor does the rest in both verifier modes. Details in `docs/architecture.md`. `[agent] timeout_sec` in `task.toml` sets how long agents get and the sandbox TTL.

The environment is any `environment/Dockerfile`, on any base. Hub and agents both start from it. The examples use `python:3.12-slim-bookworm` and `ubuntu:24.04`. Images need bash, and a package manager unless git and curl are already there.

The verifier runs on the hub by default (`shared`), where it sees the team's `main`, the team repo and the board log; with `[verifier] environment_mode = "separate"` it gets its own sandbox and the team's work through `/logs/artifacts`. Examples: `hello-team` and `hello-team-separate`. Details in [docs/architecture.md, Contracts between components](docs/architecture.md#contracts-between-components).

## Orchestration

A strategy decides who runs, with what prompt, and when the run ends; `TeamAgent` does the rest. Three are bundled in `src/multi_agent_harness/orchestrations/`, matching the harnesses in section 8.12.4 of the Opus 5.5 system card:

- `fixed` (the default): N peers, all given the full task; the run ends when all finish. The card's fixed team without a lead.
- `fixed-lead`: the same team with `agent-1` as lead; the run ends when the lead finishes and the others are stopped. The card's fixed team with a lead.
- `async`: `agent-1` starts alone and spawns, wakes and terminates subagents through board tools, using the other sandboxes as their pool. The card's async subagents.

Agent names. Sandboxes are slots `agent-1` .. `agent-N` internally (logs, metrics, checkpoints, the lead and GPU slot choice above), but agents never see those. On the board, in git and in prompts each agent has a word name such as `mango-fern-humming` (two nouns and a verb from neutral word lists, with no rank, role or mission words), derived from the trial id so a rerun gets the same names, and every teammate list comes in an order of its own per agent. With numbered names and ascending lists, the agent called `agent-1` ended up leading every team unprompted. The slot to name mapping is in the ledger and the metadata (`agent_names`), and checkpoints keep it. `--ak agent_names=numbered` brings back `agent-N` names and ascending lists, for ablations and comparing with older runs.

`--ak time_reminders=N` (off by default) sends N time-remaining reminders through the board inbox, evenly spaced across the agent time budget: to the lead, or to every running agent when the run has no lead. Each gives the time left and encourages continued work and submission before the deadline. Agents read them with `inbox()`; they do not interrupt CLI execution or restart a finished agent. Under `keepalive`-style strategies that wake an idle agent on a direct message, a reminder counts as one. Runs without a known time budget send none. Resumes skip elapsed reminder slots, and delayed ticks coalesce missed slots into one current reminder. Notifications are recorded as messages from `@harness` in the board log. Left at 0, prompts and runs are exactly as without the option.

Pick one with `--ak orchestration=fixed-lead`. For your own, copy a bundled file as the starting point and pass its path: `--ak orchestration=./my_strategy.py`. The interface, how `async` works and where it differs from the card are in `docs/architecture.md`.

## What agents can do on the board

Each agent gets the board as an MCP server named `board` with three tools: `send(text, to=None)` to everyone, one teammate or several, `inbox(wait_seconds=0)` for new messages, and `history()`. Every action lands in the board log. Details, limits and the REST mirror in [docs/board.md](docs/board.md).

## Rate limits

Each hub service limits every agent on its own. Set them per run with `--ek`, or with the same names in upper case in the host environment:

- `board_rate_per_sec` (default 5) and `board_rate_burst` (30): board ops per agent.
- `proxy_rate_per_sec` (4) and `proxy_rate_burst` (20): model requests per agent.
- `proxy_max_concurrent` (12): model requests one agent may have in flight.

0 turns a limit off.

## Extending the board

`--ek board_policy=<path.py>` loads a file defining one `multi_agent_harness.board.Policy` subclass that can refuse actions; `--ek board_plugins=<a.py>,<b.py>` loads files exposing `register(board, api, mcp)` to add tools, routes or subscribers. Paths are relative to the working directory. Example: `--ek board_policy=examples/policies/hierarchy.py`. Details in [docs/board.md, Extending](docs/board.md#extending).

## Team git remote

Agents share code through `<hub URL>/git/team.git`, each pushing as itself: agent-i may fast-forward `main` and push `agent-i/*` branches, nothing else. Details in [docs/architecture.md, Contracts between components](docs/architecture.md#contracts-between-components).

A task can also give paths on `main` to particular agents, such as one folder per subsystem owner. Something on the hub, usually a board plugin, writes the rules to `path-rules.json` in the bare repo (`/srv/git/team.git`), and the hook reads them on every push, so they can change during a run. A push to `main` that changes a path the pusher may not change is refused with the reason. Branches are never checked. The format is in `src/multi_agent_harness/gitserver.py`.

## Measuring coordination

`python scripts/metrics.py <trial or job dir> [--out FILE]` reads the board log, proxy usage, verifier details and team git bundle, prints a summary and writes `metrics.json`. Per agent it gives sends (broadcast, direct and to several agents), inbox and history calls, rate-limited and refused ops, model USD, commits and files touched. Per team it gives the same totals, sends per minute, duplicate messages, Gini of sends and commits, and how often commits touch files other agents also touched. A job dir also gets reward mean and sd and cost across trials.

N=1 baseline: same task and budget, many attempts, then compare jobs:

```
harbor run -p examples/tasks/hello-team -e multi_agent_harness:TeamEnvironment \
  --ek backend=daytona --ek n_agents=1 --ek budget_usd=5 -k 10 \
  -a multi_agent_harness:TeamAgent --ak harness=codex --ak model=gpt-6-luna --env-file .env -o jobs
python scripts/metrics.py jobs/<job>
```

Repeat with `--ek n_agents=k`. `-k` is `--n-attempts`.

## Known limitations

- Every trial installs the agent CLI in each agent sandbox at setup, as vanilla Harbor does. That is about two minutes per trial, and N agents download in parallel.
- A resume loses anything after the last checkpoint (see Checkpoint and resume).
- Sandboxes live until the trial ends, including agents that finished early.
- Hub and agents share one image, so a task's tools land on the hub too.
- TeamAgent installs any Harbor installed agent, but only knows how to configure and launch Claude Code, Codex and Antigravity.
- Allowlists take hostnames only (see Network policy).
- Docker doesn't enforce network policy: a restricted phase runs public (see Docker), so agents have internet access and can reach each other by IP. The hub's port is published on the bridge gateway IP, which processes on the host can reach too (it still needs the agents' tokens). Daytona is the tested backend.
- Agent traffic to the hub goes out through the backend's exposed URL (Daytona's preview proxy), so it costs a public round trip rather than a local link.
- The hub's exposed URL answers anyone holding its access header. Each service still needs the agent's own token, and admin endpoints are not routed.

## Troubleshooting

- `Credentials store error` while building an image on Docker: the build uses your Docker client config, and a broken credential helper there (for example `docker-credential-gcloud` with an expired login) fails every build, even for public base images. Point `DOCKER_CONFIG` at a folder with an empty `config.json` for the run, or fix the helper.

## Results

Codex on gpt-6-luna, Daytona backend, current board:

```
task                          N    reward   model USD   wall     env setup   agent setup   agents ran
hello-team                    3    1.0      0.017       6.7 m    48 s        140 s         3.2 m
hello-team-separate           3    1.0      0.014       5.4 m    44 s        134 s         1.7 m
hello-team, lead-only policy  3    1.0      0.017       6.4 m    44 s        135 s         3.0 m
hello-team                    50   1.0      1.20        10.7 m   40 s        161 s         6.8 m
```

At N=50 the board handled 428 sends (224 broadcast, 204 direct), peaking at 111 a minute, with nothing rate limited. Under the lead-only policy, 11 sends were refused and logged.

Agent setup is mostly the Codex install, done in every sandbox in parallel. Each SDK exec is one request of about 0.4 s; an agent sandbox makes about 110 of them in a trial, 60 of which are polls of the detached agent run.

## Citation

If you use this harness, please cite it (the same entry is in `CITATION.cff`):

```bibtex
@software{bespokelabs_multi_agent_harness_2026,
  author = {{Bespoke Labs}},
  title  = {multi-agent-harness},
  year   = {2026},
  url    = {https://github.com/bespokelabsai/multi-agent-coordination-bench/tree/main/harness}
}
```

For results on MAC Bench, cite [MAC Bench](https://benchmarks.bespokelabs.ai/mac-bench/) as well.

## License

Apache License 2.0, see [LICENSE](LICENSE) and [NOTICE](NOTICE).
