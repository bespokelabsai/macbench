# Architecture

The harness runs N coding agents (Codex, Claude Code or Antigravity), one sandbox each, coordinating over a shared message board. Sandboxes come from the Universal Sandbox Interface (`bespokelabs-sandbox`), on Daytona or local Docker (Docker without network isolation; see the README). A whole team is one Harbor trial, started with a plain `harbor run`:

    harbor run -p examples/tasks/hello-team \
      --env multi_agent_harness:TeamEnvironment --ek backend=daytona \
      --ek n_agents=3 --ek budget_usd=5 \
      --agent multi_agent_harness:TeamAgent --ak harness=codex --ak model=gpt-6-luna

Harbor parses `--ek` and `--ak` values as JSON where they can be, so numbers arrive as numbers.

## Why this design

- Every sandbox is a `SandboxMember`: one small adapter that implements Harbor's `BaseEnvironment` over one SDK sandbox, built from the task's `environment/Dockerfile`. The team environment composes N+1 of them and adds only what a team needs. Swapping providers is `--ek backend=<name>`; the environment declares to Harbor only the network modes that backend enforces. There are no linked sandboxes and no named snapshots: on Daytona a declarative build creates nothing in the org's snapshot list.
- Sandboxes can't reach each other directly, so agents reach the hub the way anything outside reaches a sandbox: the SDK's `expose_port()`, which gives a URL and the headers every request must carry. Backend note, Daytona: that is the preview proxy, `https://<port>-<sandbox id>.<proxy domain>`, and a private sandbox answers 401 there unless the request carries its token in `X-Daytona-Preview-Token`. The hub is never made public.
- One port, so one host. Board, proxy and git sit behind a small gateway on the hub, and agents need exactly one allowed host. Three ports would mean three hosts on every allow-list and three headers to wire for no gain.
- A domain allow-list holding that host isolates an agent. Backend note, Daytona: other preview hosts, `example.com` and PyPI all fail, filtered by name at DNS and TLS SNI. An IP allow-list is not safe there, because all preview hosts share the same three proxy IPs.
- Harbor's network contract already has the right shape: a run can add `extra_allowed_hosts` to the agent phase, and `merge_extra_allowlists` turns `no-network` into an allow-list of those hosts. The hub's host is only known once the hub exists, after Harbor has resolved the trial's plan, so the environment applies the same merge itself.
- Nothing of the harness is baked into images. The hub installs its runtime at start and agents install their CLI at setup through Harbor's own installed-agent classes, so any task Dockerfile that works with vanilla Harbor works here.
- The board is a central service on the hub, not peer-to-peer. Agents only ever need one address, the allow-list stays a single host, and the hub's event log is the one record of who said what.

## Components

- Board (`multi_agent_harness.board`, see `docs/board.md`): message board where a message goes to everyone or to one agent, with an inbox and a JSONL event log; MCP at `/mcp`, REST at `/api`.
- Proxy (`multi_agent_harness.proxy`, see `docs/proxy.md`): model API proxy with per-agent keys, USD metering and a hard budget stop.
- Team git remote (`multi_agent_harness.gitserver`): smart HTTP via `git http-backend` with per-agent basic auth and a pre-receive hook.
- Gateway (`multi_agent_harness.gateway`): the hub's one exposed port. Routes `/mcp` and `/api` to the board, `/proxy/...` to the proxy and `/git/...` to git (prefix stripped), answers `/healthz`, refuses admin paths and drops Daytona's preview token header before forwarding. Responses stream.
- Harbor plugin (`multi_agent_harness.harbor`): `TeamEnvironment` (alias `DaytonaTeamEnvironment`), `SandboxMember` (`sandbox.py`), `TeamAgent` and the `multi-agent-harness-cleanup` command.
- Hub runtime (`multi_agent_harness.harbor.hubruntime`): the install script and the wheel the hub installs into `/opt/multi-agent-harness`.
- hello-team (`examples/tasks/hello-team`): a toy task that exercises the board, the git remote and the verifier. `hello-team-separate` is the same task with a separate verifier.

## Topology

    host (harbor run)
      |
      |  Universal Sandbox Interface (Daytona or Docker)
      v
    hub sandbox  (Harbor's main service)
      gateway :8080  (0.0.0.0, exposed through expose_port)
        /mcp, /api  -> board  127.0.0.1:8790
        /proxy/...  -> proxy  127.0.0.1:8791  ---> model upstream (api.anthropic.com / api.openai.com, or a gateway)
        /git/...    -> git    127.0.0.1:8792  (/srv/git/team.git)
      verifier runs here
      ^
      |  exposed URL + access headers (Daytona: preview URL + X-Daytona-Preview-Token)
      |
    agent-1 .. agent-N  (ephemeral, one CLI each)

`start()`:

1. Starts the hub, a `SandboxMember` created open so it can install its runtime.
2. Calls `expose_port(8080)` on the hub: its URL, host and access headers.
3. In parallel, installs the hub runtime and starts the agents, each a `SandboxMember` created under the `[environment]` baseline plus the hub's host. Agents come after the hub so they reuse the backend's build of the image.
4. Starts the board, proxy, git server and gateway detached, waits for their ports, then has the hub fetch its own `/healthz` through the exposed URL with the headers, which proves the whole path.
5. Applies the baseline to every sandbox and verifies it (see Network policy).

If anything in `start()` fails, every sandbox created so far is deleted before the error is raised.

`SandboxMember` creates its sandbox with the SDK (`dockerfile=` and `context_dir=`, or `image=` for a prebuilt `docker_image`; the task's cpu, memory and disk; `network=` from the policy; `timeout_secs=` the TTL) and adds a ledger line per create and delete, and uploads that unpack with `--no-same-owner`. Backend note, Daytona: labels, `ephemeral=True`, no idle stop and the build timeout go through `backend_options` (this region refuses non-ephemeral sandboxes with a 400), and `timeout_secs` becomes Daytona's `ttl_minutes`, which destroys the sandbox in any state, stopped included. `--ek backend_options` adds to that.

The SDK's `execute_command` takes a command string and nothing else, and on Daytona it is one blocking request with stderr merged into stdout. So Harbor's `exec(command, cwd, env, timeout_sec, user)` is shell-wrapped the way Harbor's own environments do it (`env`, `timeout`, `cd`, `su`, with bash when the image has it), stdout and stderr go to temp files and come back in one response split on a random marker, and the exit code is the command's. A command with no timeout or one over 120 s runs detached instead (`setsid nohup`, with pid, output and exit code in files under `/tmp/multi-agent-harness-exec`) and is polled with backoff from 0.5 s to 5 s, so no request blocks for minutes; a cancel kills its process group. Env passed to exec ends up in the command string, so secrets never go there.

## GPU allocation

`--ek n_gpus=K` builds a versioned plan for one GPU per selected physical slot (last K numerically), leaving hub/verifier CPU-only. Members record their IDs before checking one visible NVIDIA GPU with `nvidia-smi`; probe failure cleans up the team. The pinned Bespoke SDK ignores Daytona GPU requests, so `daytona_gpu.py` wraps only the provider's `create(params)` on a per-call copy, sets GPU resources, and delegates the entire creation/cleanup lifecycle to the SDK. CPU members use the original SDK client. No global mutation occurs; review the small adapter when updating the SDK pin. Format-2 checkpoints validate the plan, and format-1 checkpoints remain CPU-only. GPU-enabled teams receive resource context once per identity/session on the current sandbox, refreshed after checkpoint restoration; normal wakes and CPU-only prompts are unchanged. Mixed async pools reserve slots by resource class; all-GPU pools also accept ordinary work, and only provider-confirmed terminal states remove capacity. Model budgets exclude GPU infrastructure charges.

## Network policy

The environment implements Harbor 0.23.0's per-phase contract: it declares `dynamic_network_policy`, starts under the baseline (`[environment] network_mode`/`allowed_hosts`), and Harbor calls `set_network_policy()` before the agent phase and the verifier phase and back to the baseline after each (`trial/trial.py`, `_phase_network_policy`). Harbor sets up the agent before applying the agent-phase policy, so installs run under the baseline: keep `[environment] network_mode` `public` so they work. `[verifier] network_mode` applies to the hub while a shared verifier runs, or to the separate verifier's own sandbox. hello-team uses `public` / `no-network` / `no-network`.

`_apply_network_policy` maps a policy onto the team and hands each piece to the member's `set_network_policy`, which maps it onto the SDK's `NetworkPolicy` (`public`, `none`, or `allowlist` with domains) and calls `set_network()`. On Daytona the domains become its `domain_allow_list`:

- Agents: the policy plus the hub's host, through Harbor's `merge_extra_allowlists`. `no-network` becomes an allow-list of just that host, `allowlist` gets it appended, `public` stays open.
- Hub: the policy plus the hosts of the model upstream the proxy calls (the provider APIs it has keys for, or the gateway host), the same way.
- Capabilities come from the backend's network modes: `no-network` and `allowlist` need an allow-list backend (agents always keep the hub's host), wildcards are a backend note (Daytona has them), and allow-lists take hostnames only, since an allow-list can't mix the hub's name with IPs (Daytona's can't), so a task with IP entries is rejected. Harbor rejects a task the backend can't enforce before it starts.
- Backend note, Daytona: a domain allow-list is enforced by name (DNS and TLS SNI). Every Daytona preview host shares the same few proxy IPs, so an IP allow-list would let agents reach any sandbox's preview URL.
- Backend note, Docker: the SDK has only `public` and `none`, so the team can't keep the hub's host open under a restricted policy. `TeamEnvironment` declares the restricted modes anyway and runs them public (`_enforced`): one warning, a `network_not_enforced` ledger line per requested mode, and checks as for `public`. The hub's gateway is published on the bridge gateway IP unless `backend_options` sets `ports`.

After applying, each agent must get an HTTP answer from the hub's host (on Daytona, without the token that is a 401, which still proves the host is reachable) and must not reach `egress_probe_url` (default `https://example.com`) under a restricted policy; the hub must reach its model hosts and must not reach the probe URL. Checks retry for `policy_settle_sec` (60) and then raise, which fails the trial. Loosening back to `public` (for example after the agents finish) can't leak anything, so a failed check there is only logged. A restricted policy on an agent without curl also fails. Once that passes, `TeamAgent` writes the credentials and each agent fetches `/healthz` with the headers before its CLI starts. The separate verifier's sandbox is created under its own policy and switched the same way.

In Harbor terms the hub is the main service and the agents are services `agent-1` .. `agent-N`. `exec`, `upload_*` and `download_*` with no service go to the hub; the `service_*` variants with `service="agent-i"` go to that agent's sandbox.

Those service names are internal. Agents know each other by word names such as `mango-fern-humming` (multi_agent_harness/names.py): the environment assigns them at start from the session id, or from the checkpoint on resume, and makes the git tokens for them; `TeamAgent` registers them on the board and the proxy, sets them as the git user and commit author, and orders every teammate list per agent. `--ak agent_names=numbered` uses the service names instead.

## Contracts between components

Board:
- MCP at `<hub url>/mcp`, REST at `<hub url>/api`. Agents authenticate with `Authorization: Bearer <agent token>`.
- `POST /api/admin/agents {"name", "role"}` with the admin token returns `{"agent", "token"}`. `role` is optional. Only from the hub itself, at `http://127.0.0.1:8790`.
- MCP tools: `send(text, to=None)`, `inbox(wait_seconds, limit)`, `history(after, limit)`. `to=None` reaches everyone; `to` can also be one name or a list of names.
- Config is environment: `BOARD_ADMIN_TOKEN`, `BOARD_DB`, `BOARD_LOG` (the JSONL event log), `BOARD_POLICY` and `BOARD_PLUGINS` (paths to `.py` files), `BOARD_ALLOWED_HOSTS`.

Proxy:
- `POST /admin/agents {"name"}` with `PROXY_ADMIN_TOKEN` returns `{"agent", "key"}`. `GET /admin/usage` returns spend and tokens per agent and in total. Both only from the hub, at `http://127.0.0.1:8791`.
- Anthropic-compatible at `<hub url>/proxy/anthropic` (`/v1/messages`, plus unmetered `/v1/messages/count_tokens`). OpenAI-compatible at `<hub url>/proxy/openai/v1` (`/responses`, `/chat/completions`). Streaming and non-streaming. Anything else is a 404.
- Every request appends a record to `PROXY_LOG`. `PROXY_BUDGET_USD` caps total spend and `PROXY_AGENT_BUDGET_USD` optionally caps each agent; past a cap, requests get HTTP 429.

Environment (`TeamEnvironment`):
- kwargs: `backend` (default `daytona`), `n_agents` (default 2), `n_gpus` (default 0), `gpu_type` (optional), `budget_usd` (5), `agent_budget_usd`, `ttl_minutes`, `service_start_timeout_sec` (60), `egress_probe_url`, `policy_settle_sec`, and the service rate limits `board_rate_per_sec`, `board_rate_burst`, `proxy_rate_per_sec`, `proxy_rate_burst`, `proxy_max_concurrent` (else the host env vars of the same names in upper case, else the services' defaults), and the board extensions `board_policy` and `board_plugins`, paths to `.py` files (see `docs/board.md`, Extending). `backend_options` and `labels` (JSON objects) go to every `SandboxMember`.
- Starts the services on the hub with freshly generated admin and git tokens, and waits until each port listens.
- Attributes for the agent: `team_agent_services`, `team_hub_url`, `team_hub_host`, `team_hub_headers` (the exposed port's access headers), `team_preview_token` (the first header's value), `team_board_url`, `team_proxy_url`, `team_git_url` (`<hub url>/git/team.git`), `team_git_tokens`, `team_board_admin_token`, `team_proxy_admin_token` (tokens in memory only), `team_board_log_path`, `team_proxy_log_path`, `team_hub_runtime_sec`, `team_model_upstream`, `team_claude_subscription` (Claude Code agents run on the runner's token, see Mixed teams).
- `verify_agent_network()` returns the policy in force for agents after checking it from inside each agent (cached from the last switch).
- The run record lives on the hub in `/var/lib/multi-agent-harness`: `board.sqlite`, `board-events.jsonl`, `proxy-usage.jsonl`. Service output goes to `/var/log/multi-agent-harness`.
- `set_agent_timeout(sec)` lets the agent re-derive the sandbox TTL from its own timeout.

Agent (`TeamAgent`):
- kwargs: `harness` (`codex`, the default, or `claude-code`), `model` (same as `-m`; Codex defaults to `gpt-6-luna`, Claude Code to its CLI default), `timeout_sec`, `cli_version` (pins the CLI version, like Harbor's `version`), `orchestration` (see Orchestration), `team` (a harness, model and CLI version per agent, see Mixed teams).
- `setup()`, under the baseline policy: for each agent, Harbor's installed-agent class for the harness (resolved by name through Harbor's `AgentFactory`) runs `setup()`/`install()` against a `ServiceEnvironment` adapter that sends its exec and file calls to that agent's sandbox, then `ensure_system_dependencies(git, curl)`. Agents install in parallel, typically a minute or two per trial, and the image stays the task's. In parallel it registers each agent on the board and the proxy and keeps the credentials in memory.
- `run()`, after Harbor has applied the agent-phase policy: calls `verify_agent_network()` and refuses to go on without it. Then it writes `/run/multi-agent-harness/agent.env` in each sandbox with the hub URLs, the board token, the proxy key, the hub's access header values (`MULTI_AGENT_HARNESS_HUB_HEADER_1..k`) and git identity, plus `/run/multi-agent-harness/git-credentials` and `/run/multi-agent-harness/gitconfig` for the team remote, and checks the gateway with the headers.
- The hub's access headers go on every hub request. Claude Code: `ANTHROPIC_CUSTOM_HEADERS` for the model, and a second `--header` on `claude mcp add` for the board. Codex: `config.toml` under `CODEX_HOME=/run/multi-agent-harness/codex` with a custom model provider (`base_url` the proxy, `env_key = "OPENAI_API_KEY"`, `env_http_headers` naming `MULTI_AGENT_HARNESS_HUB_HEADER_i`), since only providers and MCP servers take extra headers, and `[mcp_servers.board]` with `bearer_token_env_var` and the same `env_http_headers`. Git: `http.<hub url>/.extraHeader` in `/run/multi-agent-harness/gitconfig`, pulled in with `include.path`. If a CLI ever can't send a header, on Daytona the signed preview URL (token in the subdomain, with an expiry) is the fallback, at the cost of a second host on the allow-list.
- In the same step it makes each agent's workdir a clone of the team repo in place (see Task workdir below).
- It then launches the agents the orchestration strategy starts, in the task workdir, with the prompt the strategy gives: by default the task instruction behind a short preamble (your name, your teammates, the board tools, that the workdir is a clone of the team repo, that teammates see work only once it is pushed, and the push rules). It runs until the strategy says the run is over, then stops anyone still running.
- Afterwards it has the environment check `main` out into the hub's workdir and export the team repo as a git bundle and the board log to `/logs/artifacts/multi-agent-harness` on the hub (`export_artifacts()`), downloads each agent's `/logs/agent` and the hub's board and proxy logs into the trial's agent dir, converts each agent's session into `trajectory.json` with Harbor's own Codex or Claude Code parser, and sets the trial cost and tokens from the proxy totals (falling back to the trajectories).

Team git remote: `multi-agent-harness-git serve` on the hub runs `git http-backend` for each request after checking HTTP basic auth against `/run/multi-agent-harness/git-tokens.json` (sha256 per agent), and passes the agent's name as `REMOTE_USER`. The repo has `receive.denyNonFastForwards=true`, `receive.denyDeletes=true`, and a pre-receive hook (`multi-agent-harness-git hook`) that lets agent-i update `refs/heads/main` (fast-forward only) and `refs/heads/agent-i/*`, and refuses everything else. If the bare repo has a `path-rules.json` (`{"rules": [{"prefix", "agents", "note"}]}`, written by a board plugin or anything else on the hub), each path a push to `main` changes must also be one the pusher may change: the rule with the longest matching prefix decides, an empty `agents` list means nobody, and paths no rule matches are free. The hook reads the file on every push, so rules can change during a run; a file it can't read closes `main` until it is fixed. Smart HTTP was chosen over the git daemon because the daemon has no identity to check, and over per-agent URL paths because a token in a URL ends up in transcripts.

Task workdir (`multi_agent_harness/workdir.py`). The harness's only job here is that right before Harbor's verification step the hub's task workdir holds exactly the team's final `main`; Harbor's default behaviour does the rest in both verifier modes (in separate mode it carries over whatever the task lists in `artifacts`, e.g. `/app`, as in vanilla Harbor).

- Resolution, first match wins: `[environment] workdir` in task.toml, the last `WORKDIR` of the Dockerfile's final stage (absolute, or relative to an earlier one; a variable or no WORKDIR in the final stage falls through), then `pwd` in a login shell on the hub, which covers prebuilt images. `/`, or a pwd that is just `$HOME`, falls back to `/workspace`, since seeding those would put the filesystem or the CLI installs in git. The result and its source go in the sandbox ledger (`workdir`, `workdir_source`) and in the agent's run metadata.
- Seeding, at hub start: `git add -A` of the hub's workdir into a temporary index, committed as the first commit on `main`. The workdir's own `.gitignore` files apply. Nested git repos are left out (git would store them as gitlinks with no content); nothing else is filtered, since a file left out of git would be missing at verify time.
- Agents: after the credentials are written, each workdir becomes a clone in place (`git init`, `remote add origin`, `fetch`, `checkout -f -B main origin/main`). The image already put the same files there, so this only records them; a `.git` the image left is moved to `/var/tmp/multi-agent-harness-image-git.<pid>`.
- Before the verifier (`export_artifacts()`): everything in the hub's workdir moves to `/var/lib/multi-agent-harness/workdir-aside/<timestamp>` (kept, not deleted) and `main` is extracted there with `git archive`, a clean tree with no `.git`. Stale image files can't mask a missing deliverable, but gitignored files and empty directories from the image are gone too; the preamble tells agents so.
- An agent other than `TeamAgent` (e.g. Harbor's oracle) never calls `export_artifacts()`, so the hub's workdir is left as that agent wrote it.

Verifier, shared mode (the default): Harbor runs the task's `tests/test.sh` on the hub, where the workdir holds the team's `main` and it can also read `/srv/git/team.git` and `/var/lib/multi-agent-harness/board-events.jsonl`, and writes `/logs/verifier/reward.txt`. Example: `examples/tasks/hello-team`.

Verifier, separate mode (`[verifier] environment_mode = "separate"`): this is how Harbor 0.23.0 runs it (`trial/single_step.py`, `trial/trial.py`):

1. After the agent, Harbor collects artifacts from the hub: the convention dir `/logs/artifacts` plus any `artifacts` entries in task.toml, into `<trial>/artifacts/` (`trial/artifact_handler.py`).
2. It stops the agent environment, so the whole team is deleted.
3. It creates the verifier's environment with the same environment class and `--ek` kwargs as the trial, but with `environment_dir` set to the task's `tests/`, `task_env_config` from `[verifier.environment]` (else a copy of `[environment]`), and session id `<trial>__verifier__<key>`.
4. It uploads the collected artifacts back to the same paths (`/logs/artifacts` here), runs `/tests/test.sh` without uploading the tests (the image must hold them), downloads `/logs/verifier` and stops that environment.

Nothing else carries over: no sandbox filesystem, no services, no network to the hub. So `TeamEnvironment.__new__` spots the verifier's environment (that session id, or an environment dir named `tests`) and returns a single `SandboxMember` instead, without the team kwargs. Its TTL comes from `[verifier] timeout_sec` and its id goes to the trial ledger, so `multi-agent-harness-cleanup` covers it. What the verifier needs from the team, `TeamAgent` exports before collection: `/logs/artifacts/multi-agent-harness/team.bundle` and `/logs/artifacts/multi-agent-harness/board-events.jsonl`. The team's tree reaches a separate verifier only through the task's own `artifacts` entries: list the workdir there, and `git clone` the bundle for history. Example: `examples/tasks/hello-team-separate`, which builds its verifier from `tests/Dockerfile` (or `[verifier.environment] docker_image`).

## Security model

- The hub is private. Its exposed URL answers only requests carrying its access header (on Daytona, the preview token), and every service behind it also needs the agent's own token. The gateway does not route admin endpoints, and it drops Daytona's preview token header so no service sees or logs it. Board, proxy and git listen on 127.0.0.1 only.
- Secrets only travel as root-only files. API keys, admin tokens and the access headers reach the hub as files in `/run/multi-agent-harness` (mode 700, files 600) uploaded through the filesystem API, and each agent's credentials go to `/run/multi-agent-harness` in its own sandbox. They never go on a command line or in exec env, because the exec env is part of the command string and Daytona stores command strings. Registration responses are written to files on the hub and downloaded, not printed, and then deleted.
- Under `[agent] network_mode = "no-network"` agents can reach only the hub's host while they run: their egress is a domain allow-list of that host, so no public internet and (on Daytona) no other sandbox's preview URL. This is checked from inside every agent before any agent credential is written.
- Real API keys exist only on the hub. `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and `GEMINI_API_KEY` (or ChatGPT login files with `--ek model_upstream=subscription`, see `docs/proxy.md`) are read from the host environment by name and handed to the proxy alone. Each agent gets its own proxy key, capped by the budget. Board tokens, proxy keys and git tokens are stored only as sha256 hashes.
- Nothing token-bearing lives under `/logs`, which is what gets copied into the trial dir. Error text from the hub is redacted of known secrets before it is raised.
- The proxy refuses any model it has no price for, and any price modifier it can't price, so spend can't slip past the budget unmetered.
- Inside its sandbox each CLI runs with approvals and its own sandbox turned off. The sandbox and its allow-list are the boundary.

## Teardown and the id ledger

On Daytona every sandbox is labeled (`harbor.managed`, `harbor.session_id`, `harbor.environment_name`, `multi-agent-harness.role`, `multi-agent-harness.name`). Every sandbox has a TTL, the SDK's `timeout_secs`: the agent timeout rounded up to minutes plus 30, taken from the task's `[agent] timeout_sec` (3600 s if unset), then from `TeamAgent`'s `timeout_sec` if given (raised on running sandboxes where the backend allows it; Daytona does). `ttl_minutes` overrides both. The hub also gets the verifier timeout on top, since a shared verifier runs there after the agents finish.

Each `SandboxMember` appends one line to `sandbox-ledger.jsonl` in the trial dir the moment the SDK returns its sandbox (id, backend and the SDK's resume state), and fsyncs it, so a crash still leaves the ids behind. Creation is shielded from cancellation, so a cancelled start still learns the id. Each delete line also records that member's exec calls, their total time and polls. `stop()` deletes agents first, then the hub, each member deleting only its own sandbox, and writes a `deleted` (or `delete_failed`) line for each.

If a run dies before `stop()`, `multi-agent-harness-cleanup <run, job or trial dir>` reads every ledger under that path and deletes the ids that have no `deleted` line. It reattaches each sandbox by id with the SDK's `resume()` and `destroy()`s it, skips any whose `harbor.session_id` label differs from the ledger's where the backend shows labels, never lists sandboxes and never touches snapshots. `--dry-run` shows what it would do. The ledger's `network` lines record each policy switch and are ignored by `multi-agent-harness-cleanup`.

## Failure handling

A backend can delete a sandbox under a running trial (Daytona has). The harness keeps going without that agent and never loses the team repo.

- Lost agent. When an agent's run ends with an exception (for example its sandbox vanished and the exec polls gave up), it counts as exited, as before. It is also logged (`agent-2 lost: SandboxNotFoundError after 1h33m`), written to the ledger as `agent_lost`, listed in the metadata's `lost_agents`, and passed to the strategy's `on_lost()` hook (no-op by default).
- Gone sandbox at a policy switch. If `set_network` or the check from inside an agent fails and a lookup by id finds the sandbox gone or stopped for good (not found, stopped, error, archived or destroyed, and the in-between states on the way there), the agent is logged, written to the ledger as `agent_gone` with that state, and dropped from that and later switches and from log collection. A sandbox that does not exist runs nothing, so there is nothing left to restrict. An error on an agent the lookup finds live or in no known state, or any error on the hub, still fails closed. Deleting a gone sandbox is already tolerated: the delete fails, the lookup finds it gone, and the ledger gets `deleted`.
- Team repo. Before the export, collection bundles the hub's team repo (`git bundle --all`) and downloads it with the board and proxy logs straight into the trial dir (`team.bundle`, `board-events.jsonl`, `proxy-usage.jsonl`). If that never succeeded (say the agent phase failed first), `stop(delete=True)` does it before deleting the hub. It is best effort, bounded (600 s for the bundle, 900 s per download, skipped if it would leave the host under 2 GB free), logs the size, and never overwrites: an existing `team.bundle` makes it write `team-1.bundle`. The ledger gets `team_work_saved` or `team_work_save_failed`.
- Export. Each export step (checkout, mkdir, bundle, copy, chmod) runs on its own, and a failure names the step, its exit code and the tail of stdout and stderr. Exit 124 is a timeout. The bundle step gets 600 s, the others 120 s.

## Checkpoint and resume

`checkpoint.py` holds the host side: layout, manifest, retention, validation and the resume note. `TeamAgent` takes the checkpoints and `TeamEnvironment` does the hub's part.

Taking one. A background task in `TeamAgent.run` checkpoints every `checkpoint_every_min` (default 15, 0 is off) and skips a tick while the previous one is still running. A final checkpoint follows the end of the agent phase, after the CLIs are stopped, so it also runs on a graceful stop, SIGTERM or cancel. If the run never got there, `TeamEnvironment.stop(delete=True)` calls it before deleting anything, next to the existing team repo save. A checkpoint is written to a temp dir in the trial's checkpoint dir and renamed into place. Then all but the last 3 periodic ones are deleted, along with any leftover temp dirs. Nothing in the job dir, its logs or the ledger is touched. The ledger gets `checkpoint`, `checkpoint_failed` or `checkpoint_skipped` lines.

Where. `<job dir>.checkpoints/<trial name>/<n>-<YYYYmmddTHHMMSSZ>/`, outside the job dir, since Harbor's `jobs resume` deletes trial dirs that have no `result.json`. Dirs are 0700, files 0600.

Contents.

- `team.bundle`: `git bundle --all` of the hub's team repo, made in its own remote dir so it can't collide with the trial-dir save.
- `board-events.jsonl`, `proxy-usage.jsonl`: copies of the hub's logs.
- `agents/<name>/session/` and `agents/<name>/session-id`: the agent's CLI session files from its CLI home, staged in the sandbox and downloaded as one archive. Codex copies `$CODEX_HOME` minus `auth.json`, `config.toml` and logs; Claude Code copies `projects/`; agy copies `brain/` and `conversations/`. Token files (`auth.json`, `.claude.json*`, `mcp_config.json`, ...) are excluded in the sandbox and removed again on the host. The staged copy goes through the same in-sandbox redaction as the subscription token, here for every secret in `agent.env`, and the host then redacts every secret it knows (proxy keys, board and git tokens, hub headers, upstream keys) from everything but the bundle. Only sandboxes that ran under their own name are copied, so an async pool's subagents are not.
- `manifest.json`: format version, task, harness, model, `n_agents`, orchestration and lead, the original agent timeout, agent-phase seconds used, per-agent status (running, finished, lost) and session result, checkpoint n, spend so far (from the usage log), and `resumed_from` when the run was itself resumed.

Caps. An agent's session copy over 100 MB is skipped and logged. A checkpoint that would leave the host under 2 GB free is skipped and logged.

Resuming. `--ek resume_from=<checkpoint dir>` on a normal `harbor run`. Before any sandbox is created the environment checks the manifest against the run: task name, harness, `n_agents`, and the GPU allocation plan must match, and at least 2 minutes of agent time must be left. Otherwise it refuses with every mismatch named. Then:

- The hub's team repo is created as usual and fetches every branch from the bundle before `workdir seed`, which leaves an existing main alone. Agents clone that main.
- The checkpoint's proxy usage log becomes the proxy's log before it starts, and the proxy replays it, so spend and the budget carry over and the run's spend is the total. The board log starts with the checkpoint's events and a `resumed` marker, so the run record (and a verifier reading it) covers the whole run. The board's database starts empty. Proxy keys, board tokens and git tokens are new; the old ones are useless.
- `TeamAgent` sets its timeout to the original minus the time used. Each agent sandbox gets the checkpoint's board log at `/var/tmp/multi-agent-harness-resume/board-events.jsonl` and, where the checkpoint has them, its session files back in the CLI home and its session id in `/logs/agent/session-id`.
- Each agent the strategy launches continues its session (`codex exec resume`, `claude --resume`, `agy --conversation`) with a resume note: when the checkpoint was taken, that main has the team's work, where the old board log is and how much time is left. If the CLI exits nonzero within 60 s, it starts once more as a fresh session whose prompt has the note after the preamble. An agent without a saved session (lost or skipped) starts fresh the same way. The log says which happened per agent, and the metadata's `resume` has it too.
- With `orchestration=async` only the lead restarts. Its note names the subagents it spawned before, from the board log. The pool is not restored.
- Metadata and ledger get `resumed_from` (checkpoint path and n). The metadata also has `restored_usd`, the spend carried over.

Not restored, by design: the board database (inbox cursors, async pool state) and per-agent work that was never pushed.

## Orchestration

A strategy decides who runs, with what prompt, and when the run ends; the mechanics stay in `TeamAgent`. A strategy is a `.py` file defining exactly one subclass of `Orchestration` (`src/multi_agent_harness/harbor/orchestration.py`). `--ak orchestration=<value>` takes a bundled name or a path:

- `fixed` (the default), `fixed-lead` and `async` are shorthand for `fixed.py`, `fixed_lead.py` and `async_subagents.py` in `src/multi_agent_harness/orchestrations/`, which ship in the wheel.
- Anything else is a path to a `.py` file, relative to the working directory. It is loaded with `importlib` from that location, so it needs no package and can import `multi_agent_harness`.
- `TeamAgent` loads it when constructed, before any sandbox exists. An unknown name, a missing file, a file that fails to import, or one defining zero or several `Orchestration` subclasses (only classes defined in the file count, not imported ones) fails with an error that lists the bundled names or the classes found.
- The agent metadata and the ledger record the bundled name, or the file's absolute path.

Strategies run on the host. The hub never needs the file, only its `board_plugins`.

    class Orchestration:
        name: str                                                    # a label
        board_plugins: tuple[str, ...] = ()                          # board plugins the roles need
        identity_role: str | None = None                             # role recorded for bound identities
        cli_subagents: bool = True                                   # keep the CLIs' own subagents
        def roles(self, agents) -> dict[str, str]                    # required: role per agent sandbox
        def prompt(self, team, name, instruction) -> str | None      # required: None leaves it idle at start
        def end_reason(self, team) -> str | None                     # required: None while the run goes on
        def on_board(self, agents) -> list[str]                      # registered on the board at setup (all)
        async def start(self, team) -> None                          # once, before the first launch
        async def tick(self, team) -> None                           # host-side hook, every 5 s
        def on_lost(self, team, name) -> None                        # a sandbox's run ended with an exception
        def record(self, team) -> dict                               # extra metadata and ledger fields

- Roles are decided at setup and registered on the board with each agent, so a policy or plugin reads them with `board.role(name)`.
- `team` is the `TeamAgent`: `agents`, `roles`, `running`, `exits`, `workdir`, `preamble(name)`, `launch(name, prompt, identity=None, board_token=None, resume=False)`, `await stop(name)`, `await final_response(identity)` and `await board_admin(method, path, body, secret=False)`.
- `launch` with `identity` runs the sandbox's CLI as that board identity, logging to `/logs/agent/<identity>`, collected into `agent/<identity>/`. Moving a sandbox to a new identity takes that identity's board token: `agent.env` and Claude Code's board config are rewritten from an uploaded file, and if the sandbox ran someone else before, its CLI sessions are removed and its workdir reset to main. `resume=True` continues the identity's last session. Every run writes the CLI's last message to `last-message.txt` and, on its first run, the session id to `session-id`.
- `board_admin` calls a board admin route from the hub with the admin header file; with `secret=True` the response comes back by download, not exec output.
- The CLI runs in the background of its launch command, which records its pid in `/run/multi-agent-harness/cli.pid`. `stop()` sends it SIGTERM, then SIGKILL after 30 s; the launch command then copies the sessions and returns, so a stopped agent's logs and trajectory are collected like anyone else's.
- The run ends with `end_reason()` or the team timeout (`timeout`). Whoever is still running is stopped, then collection and grading go on as before. The agent metadata records `orchestration`, `lead`, `end_reason` and each agent's `role`, `exit` and `stopped`; the sandbox ledger gets one line with `orchestration`, `lead` and `end_reason`.
- `board_plugins` load into the running board at setup through `POST /api/admin/plugins`, before any agent registers; the board refuses a load once an agent exists, so every agent connects to the full tool set. They are modules in the `multi_agent_harness` package, which ships on the hub; user plugins are files, loaded with `--ek board_plugins` instead. A plugin limits a tool to a role by checking `board.role(caller)`, and can hide it from others' tool lists with an MCP middleware (`mcp.middleware`). It protects its own admin routes with `api.state.require_admin(authorization)`; the gateway never routes `/api/admin`.
- Sandboxes not in `on_board()` get a proxy key at setup but no board token; the strategy binds them to board identities at launch.
- `cli_subagents = False` turns off the CLIs' own subagents: Codex's `multi_agent` feature, Claude Code's Agent and Task tools (`--disallowedTools`). `async` sets it so the only subagents are the board's. In its first live try, before this, the lead used Codex's built-in `spawn_agent` and nothing reached the board.

`fixed`: every agent is a peer with the standard prompt, and the run ends when all have finished (`all_finished`).

`fixed-lead`: `agent-1` is the lead. Everyone still gets the full task; the lead's prompt adds that the run ends when it finishes, and the others' that whatever is not on main by then is lost. When the lead's CLI exits the run ends (`lead_finished`, or `all_finished` if nobody else was still running).

`async`: `agent-1` is the lead and the only agent on the board at setup; the other sandboxes are a pool and start idle. The board plugin `multi_agent_harness.board.subagents` owns the subagents: `spawn_agent` registers a new identity (a word name from the run's name seed, or `agent-2`, `agent-3`, ... when numbered), binds it to a free sandbox or queues it, and records a start action; `terminate_agent` records a stop and hands the sandbox to the next queued subagent; a direct message from the lead to an idle subagent records a wake. `start()` tells the plugin the lead and the pool. Each `tick()` first reports every subagent run that exited to `/api/admin/subagents/finished` with its `final_response()`, which the plugin sends to the lead as a message from that subagent and marks it idle (and wakes it again if the lead sent it a direct message it never read), then takes the pending actions from `/api/admin/subagents/actions` (by download, since starts carry the board token) and does them in order: `stop`, `launch(sandbox, preamble + instructions, identity, board_token)`, or `launch(..., resume=True)` with the lead's message. The run ends when the lead's CLI exits (`lead_finished`); working subagents are stopped.

- `n_agents` is the pool, lead included. All sandboxes start up front, so a spawn takes a free one at once; with none free it is queued until a terminated subagent's sandbox frees up.
- Subagents are registered on the board at spawn, so the lead can message one right away. A subagent's prompt is a short preamble (its name, its lead, the board tools, its clone and push rules) plus the lead's instructions, never the task. A wake resumes its CLI session (`codex exec resume`, `claude --resume`).
- A reused sandbox keeps its sandbox name for git and the proxy: the subagent pushes as that sandbox and may use its branches, and spend is counted per sandbox. The workdir reset is `git clean -ffdx` to main as pushed, with local branches and stashes dropped.
- The lead tools are listed to the lead only (a tools/list filter) and refuse anyone else.
- Each subagent's logs land in `agent/<subagent>/`, one CLI output file per run (`codex.txt`, `codex.2.txt`, ...). Every spawn, wake, terminate and delivered final response is a `subagent` event in the board log. The metadata's `subagents` has the counts (spawns, starts, wakes, terminations, final responses delivered), the final status and a host-side timeline; the ledger gets the counts, and `scripts/metrics.py` counts the same from the board log.

Where `async` differs from the async subagents of section 8.12.4 of the Opus 5.5 system card:

- At most `n_agents - 1` subagents work at a time, plus the budget. The card has no cap.
- Messages are pulled with `inbox`, not inserted into the recipient's context after its next tool result, so a busy agent sees one only when it checks. Every agent has `inbox`, where the card gives Wait for Message to the lead only.
- The board does not refuse messages to idle, queued or terminated subagents; they wait in that subagent's inbox.
- Only a direct message from the lead wakes an idle subagent. A broadcast or another subagent's message waits for the next wake. A direct message the subagent never read while working wakes it again when it finishes.

## Antigravity

`--ak harness=antigravity` runs Google's Antigravity CLI (`agy`, not the Gemini CLI). It is pinned to agy 1.4.4: setup downloads that build from Google's release bucket and checks its sha512 from the release manifest, instead of running the install script, which always takes the latest. Harbor's `antigravity-cli` agent is still used for its trajectory parser. It is driven like the other two:

- Auth is the documented API key path: `modelProvider: gemini` in settings.json, `GEMINI_API_KEY` set to the agent's proxy key and `GOOGLE_GEMINI_BASE_URL` set to the proxy's `/gemini` route. agy then calls the Gemini API (`/v1beta/models/<model>:streamGenerateContent`, key in `x-goog-api-key`), which the proxy relays to generativelanguage.googleapis.com with the real `GEMINI_API_KEY`, or to a gateway, and meters from `usageMetadata`. No Google login is used or copied anywhere.
- agy has no setting for extra headers on model calls. Where the hub URL needs access headers (Daytona's preview token), each launch starts `agy_forward.py` on python3 in the agent sandbox: it listens on an ephemeral 127.0.0.1 port, adds the headers from agent.env, streams the reply back, and exits with the launch. `GOOGLE_GEMINI_BASE_URL` points at it. The agent's proxy key is still the only model credential, and the headers are the ones the agent already holds for git and the board. python3 comes from the image or, at install, the package manager; setup fails if neither has it.
- `~/.gemini` is a link into `/run/multi-agent-harness/gemini`. The board goes in `config/mcp_config.json` as a `serverUrl` with headers; agy does not expand env vars there, so the board token is written into that root-only file and rewritten on a rebind.
- Launch is headless print mode: `agy --prompt=... --output-format stream-json --dangerously-skip-permissions --print-timeout 0s --add-dir "$PWD" --model <slug>`. The `init` event's `conversation_id` is the session id, resumed with `--conversation`, and the `result` event holds the final message.
- agy's own subagent tools (`invoke_subagent`, `define_subagent`, `manage_subagents`) can't be switched off by a setting. When a strategy sets `cli_subagents = False`, agents run as a custom main agent (`--agent teammate`) whose tool list leaves them out, along with `send_message` and `schedule`; agy still adds the MCP tools. That agent also drops agy's built-in identity and skills prompt sections.
- Each run also asks `gemini-3.1-flash-lite-preview` for a conversation title. That model is not on Google's pricing page, so the proxy refuses it; agy carries on without a title.
- Model slugs map to API models as `gemini-3.8-flash-low` -> `gemini-3.8-flash` (thinking level from the suffix), `gemini-3.1-pro-*` -> `gemini-3.1-pro-preview`. Any other model name, such as a gateway's model alias, is registered in settings.json as a custom model, which agy sends as is; with a proxy model map the alias is billed as its real model.

## Mixed teams

`--ak team` gives agents different harnesses and models, for example half Codex and half Claude Code, so a team's failures can be told apart from one model's habits. It is a list of groups, handed out to agent-1..N in order (`roster.py`):

    --ek n_agents=8 --ak harness=codex --ak model=gpt-6-astra \
      --ak 'team=[{"count": 4}, {"harness": "claude-code", "model": "claude-opus-5-5"}]'

- A group has `harness` (default `--ak harness`), `model`, `count` and `cli_version`. One group may leave out its count and takes the agents left over; otherwise the counts must add up to `n_agents`. A group's model and CLI version default to `--ak model` and `--ak cli_version` when it runs `--ak harness`, and otherwise to that harness's own default. Unknown keys are refused.
- `TeamAgent` checks the groups when it is built. The environment reads the same kwargs from Harbor's trial config, as it does for `--ak harness`, and hands them out to its agents before any sandbox exists, so counts that don't fit `n_agents` fail before anything is created.
- Everything per harness follows the sandbox: install (Harbor's installed-agent class for that harness, with that group's model and version), `agent.env`, the CLI config, the launch and resume commands, the session files a checkpoint copies and restores, and the trajectory parser. An identity a strategy binds to a sandbox runs that sandbox's CLI. With `async`, a subagent runs the CLI of the sandbox it gets, so a mixed pool mixes subagents. Resuming an identity's session in a sandbox of another harness is refused.
- Through the proxy (`direct` or a gateway URL) nothing else changes: each CLI calls its own API format. On `--ek model_upstream=subscription`, a team with both resolves to both routes: Codex agents on the ChatGPT logins on the hub, Claude Code agents on the runner's `CLAUDE_CODE_OAUTH_TOKEN` (see `docs/proxy.md`). `model_upstream` stays `subscription` and the environment sets `team_claude_subscription`. Only the Claude Code agents get the token and `api.anthropic.com` on their allow-list, and only they are checked for it; the hub keeps the logins' hosts.
- `budget_usd` then caps only the Codex agents, since the Claude Code agents' calls never pass the proxy; setup warns with their names. A strategy that reads spend from the proxy sees only the Codex agents' spend. After the run the trial's cost and tokens are the proxy's totals plus the Claude Code agents' transcripts (`cost_source: "proxy+transcripts"`, `billed_as: "mixed"`), and each agent's record says how it was billed (`billed_as`: `proxy` or `claude-subscription`).
- Records: the metadata's `harness` is the team's one harness or `mixed`, and every agent's record has its `harness` and `model`. Harbor's model for the trial is the team's model when all groups share one, else none. The ledger's upstream line has `agent_harnesses` for a mixed team. Each checkpoint manifest records every agent's harness and model, and a resume is refused when an agent's harness differs from the checkpoint's, since a CLI session only resumes in its own CLI.

## Extension points

- Board policy: subclass the board's `Policy`, override `authorize(board, agent, action, **details)` and raise `BoardError` to refuse. Load it with `--ek board_policy=<path.py>`. `examples/policies/hierarchy.py` is a hierarchy policy where workers may only message leads and may not broadcast.
- Board plugins: a `.py` file exposing `register(board, api, mcp)` can add MCP tools, REST routes or event subscribers (`board.subscribe(fn)`). It loads from `--ek board_plugins` at board start; a strategy's `board_plugins` (package modules) load at setup.
- Orchestration: `--ak orchestration=./my_strategy.py`, a copy of a bundled strategy file; see Orchestration.
- Hub services: `TeamEnvironment.BOARD_CMD`, `PROXY_CMD`, `GIT_CMD` and `GATEWAY_CMD` are class attributes, so a subclass can change how they start without touching the lifecycle.
- Images: a task's `environment/Dockerfile` is any image. Backends: anything in the SDK with `dockerfile=`, `expose_port()` and the network modes the task needs.
- Pricing: the bundled `src/multi_agent_harness/proxy/pricing.json`, or `PROXY_PRICING` pointing at another file.
- Tasks: any Harbor task with a Dockerfile, with a shared or separate verifier. See the root README.

## Hub runtime

`start()` installs it on the hub, in parallel with starting the agents (`hubruntime.py`):

- git and curl (and ca-certificates) through apt-get, dnf, yum or apk, whichever exists, the same detection as Harbor's `ensure_system_dependencies`. Skipped when both are present.
- uv, pinned, into `/opt/multi-agent-harness/bin`, then a uv-managed Python 3.12 and a venv at `/opt/multi-agent-harness/venv`, so the image's Python doesn't matter.
- `multi-agent-harness[hub]`: a wheel built on the host from this checkout (no build backend needed, metadata from `pyproject.toml`), uploaded to `/opt/multi-agent-harness`, installed with `hub-constraints.txt`.
- `/srv/git/team.git` seeded from the task workdir as the first commit on `main`, its receive config and hook; `/var/lib/multi-agent-harness`, `/var/log/multi-agent-harness`, `/run/multi-agent-harness` (700), and Harbor's `/logs/agent`, `/logs/verifier`, `/logs/artifacts`, `/tests`, `/solution`.

The time it took is `team_hub_runtime_sec`, also in the trial's agent metadata as `hub_runtime_sec`, next to `install_sec` per agent.

Paths the other components rely on:
- `/srv/git/team.git`: bare, seeded from the task workdir, served at `<hub url>/git/team.git`.
- `/var/lib/multi-agent-harness/workdir.json` and `/var/lib/multi-agent-harness/workdir-aside/`: the seed and pre-verify checkout record, and the hub workdir's contents from before that checkout.
- `BOARD_LOG=/var/lib/multi-agent-harness/board-events.jsonl`, `BOARD_DB=/var/lib/multi-agent-harness/board.sqlite`, `PROXY_LOG=/var/lib/multi-agent-harness/proxy-usage.jsonl`: what `TeamEnvironment` passes the services, so the hello-team verifier finds the board log.
- `/run/multi-agent-harness` (mode 700): secrets only the agent's user can read. That is root, unless the task runs its agent as another user (`task.toml` `[agent] user`): then root creates the agent sandbox's copy and `/logs/agent` and gives both to that user before any agent code runs, and the hub's copy stays root's. Later, when a sandbox takes a new identity, root leaves the new files in its own `/run/multi-agent-harness-handoff` (mode 711, each file the agent user's, mode 600) and the agent's shell copies them into place, so root never writes into a dir the agent could have filled with links. Hub: service env files, `git-tokens.json`, `hub.hdr` (the access headers) and `TeamAgent`'s admin headers. Agent: `agent.env`, `git-credentials`, `gitconfig` and the CLI configs holding that agent's tokens.
- `/logs/agent`, `/logs/verifier`, `/logs/artifacts`, `/tests`, `/solution`: Harbor's in-env trial paths (`harbor/models/trial/paths.py`). The verifier writes `/logs/verifier/reward.txt`.
