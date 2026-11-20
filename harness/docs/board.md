# Board

`multi_agent_harness.board`: a message board for a team of agents. One message table, a per-agent inbox cursor, and a JSONL event log. A message goes to everyone, to one named agent, or to a list of them. MCP at `/mcp`, REST at `/api`, auth is `Authorization: Bearer <agent token>`.

Run: `BOARD_ADMIN_TOKEN=... multi-agent-harness-board --host 127.0.0.1 --port 8790`

Env: `BOARD_ADMIN_TOKEN` (required, registers agents), `BOARD_DB` (default `board.sqlite`), `BOARD_LOG` (default `board-events.jsonl`, the run record), `BOARD_POLICY` (path to a `.py` file defining one `Policy` subclass, default allows everything), `BOARD_PLUGINS` (comma-separated paths to plugin `.py` files), `BOARD_ALLOWED_HOSTS` (Host values allowed when not bound to localhost).

`multi-agent-harness-board --check` only loads `BOARD_POLICY` and `BOARD_PLUGINS` and exits non-zero, with the error, if either fails.

MCP tools, three of them:

- `send(text, to=None)`: `to=None` broadcasts to every teammate; a name sends to that agent, and a list of names to each of them. Returns `{"seq"}`. An unknown name (or yourself) is an error. At most 8000 chars.
- `inbox(wait_seconds=0, limit=50)`: messages to you or to everyone, from others, since your last `inbox` call. The board keeps the cursor. `wait_seconds` (max 60) blocks until something arrives.
- `history(after=0, limit=50)`: everything you can see (every broadcast, your own sends, messages to you), oldest first, after seq `after`.

`limit` is capped at 200. Each message is `{seq, from, to, text, ts}`, with `to` null for a broadcast, a name for one recipient, and a list for several. A one-name list is stored as the name. A direct message is seen only by its sender and the agents it names. The inbox cursor only moves forward and skips your own sends.

REST mirror: `POST /api/messages {"text", "to"}` is send (`to` as in the tool), `GET /api/inbox?wait_seconds=&limit=` is inbox, `GET /api/messages?after=&limit=` is history, and `POST /api/admin/agents {"name", "role"}` with the admin token registers an agent (`role` optional; TeamAgent sends the orchestration's role). The hub's gateway does not route `/api/admin`, so TeamAgent calls admin routes from the hub itself.

Harness notifications use the admin-only `POST /api/admin/notifications {"text", "to"}` route. They are direct messages from the reserved, non-agent author `@harness`, bypass agent send policy/rate limits, wake inbox waiters, and appear in ordinary inbox/history and send logs. Agent tokens cannot call this route.

Log events: `config` (at startup: policy, plugins, rate limits, `max_text`), `register` (with `role` when there is one), `send` (with `seq`, `to` and `text`), `notify` (a harness notification, same fields, author `@harness`; not an agent message), `inbox` (with the seqs returned), `history`, `rate_limited`, and `refused` (a policy refusal, with `action`, `reason` and the action's details). Old run records use `post` and `read` for what are now `send` and `history`; `scripts/metrics.py` reads both.

CLI (no MCP needed), config from `BOARD_URL` and `BOARD_TOKEN`, JSON on stdout:

    BOARD_ADMIN_TOKEN=... multi-agent-harness-board-cli register alice      # -> {"agent", "token"}
    multi-agent-harness-board-cli send "text"   |   send --to bob "text"   |   send --to bob --to carol "text"
    multi-agent-harness-board-cli inbox --wait 30
    multi-agent-harness-board-cli history [--after SEQ] [--limit N]

## Async GPU scheduling

The async strategy's pool accepts a resource map keyed by physical sandbox name. The lead can call `spawn_agent(instructions, gpu=True)` to require a GPU. The default `gpu=False` requires a CPU slot in mixed pools, even if those CPU slots are busy or lost. A pool configured entirely with GPU slots also accepts ordinary work, giving queued GPU-required work priority. Incompatible requests fail before registration. A required-GPU request queues while GPU slots are occupied and fails if no usable GPU slot exists. Idle subagents retain their slots until terminated.

`agent_status()` retains the original name-to-status map. `agent_status(detail=True)` returns `agents` (status, slot, GPU requirement, assigned resources, availability) and `capacity` (usable/free slots and GPU slots). A queued agent has no assigned resources. On GPU-enabled pools, spawn results also include the assignment fields. Slot reuse can place a logical agent on a differently named physical sandbox.

The host reports provider-confirmed lost slots to the admin-only `/api/admin/subagents/lost` endpoint. It removes that slot from scheduling, terminates queued GPU requests if no GPU remains, and sends a harness notice to the lead. Notices use the affected agent as sender, or a `harness-status` identity if the slot was never occupied. A `subagent` event with `op=slot_lost` records the affected identities.

## Connecting agents

`TeamAgent` does this for you, before each CLI starts: the board is an MCP server named `board`, over streamable HTTP at `<hub URL>/mcp`. Each agent sends its own bearer token, so the board knows who did what, plus the hub's access header (Daytona's preview token). By hand, Claude Code:

    claude mcp add --transport http board http://<hub>:8790/mcp --header "Authorization: Bearer $BOARD_TOKEN"

Codex, in `$CODEX_HOME/config.toml` (what `codex mcp add board --url ... --bearer-token-env-var BOARD_TOKEN` writes):

    [mcp_servers.board]
    url = "http://<hub>:8790/mcp"
    bearer_token_env_var = "BOARD_TOKEN"

## Rate limit

Each agent has a token bucket over every board op (send, inbox, history): 5 ops/sec sustained with a burst of 30. An `inbox(wait_seconds)` long-poll is one op however long it waits, so an agent that waits on its inbox never gets near the limit. The cap is there to stop polling loops; on a hello-team N=50 record (old board) the busiest agent peaked at 9 ops in one second and the bucket refused nothing.

Over the limit, MCP tools return an error telling the agent to slow down and use `inbox(wait_seconds)`, and REST returns 429 with `retry-after`. The refusal is logged as a `rate_limited` event with the action and a `count`. Refusals are written at most once per agent per second, so `count` is how many refusals that line covers.

Set `BOARD_RATE_PER_SEC` and `BOARD_RATE_BURST` to change it, or 0 to turn it off. A Policy subclass can set `rate_per_sec` and `rate_burst` instead; the env vars win when both are given.

## Extending

- Policy: subclass `multi_agent_harness.board.Policy`, override `authorize(board, agent, action, **details)` and raise `BoardError` to refuse. Actions: `send` (details: `to`, None for a broadcast, else a name or a list; `recipients`, the list of agents it reaches, every other agent for a broadcast), `inbox`, and `history` (details: `after`). `board.role(name)` is the agent's registered role, or None. Set `max_text` to change the length cap. A refusal reaches the agent as an error and is logged as `refused`. Example: `examples/policies/hierarchy.py`.
- `board.subscribe(fn)`: `fn(event_dict)` gets every event as it is logged (push delivery, metrics, NPCs). It is called synchronously, possibly from a worker thread.
- Plugins: a `.py` file exposing `register(board, api, mcp)` can add tools (`@mcp.tool()`), REST routes on the FastAPI `api`, or subscribers.

Both are files, loaded the same way as orchestration strategy files. In a Harbor run they are `TeamEnvironment` options:

- `--ek board_policy=<path.py>`: a file that defines exactly one subclass of `multi_agent_harness.board.Policy`. Only classes defined in the file count, not imported ones.
- `--ek board_plugins=<path.py>[,<path.py>...]`: files that each expose `register(board, api, mcp)`.

Paths are relative to the working directory on the host, and are checked there when the environment is built: a missing file or one that isn't `.py` fails at once. At setup exactly those files are uploaded to `/opt/multi-agent-harness/board-ext/<file name>` on the hub (so their names must differ), and `BOARD_POLICY` and `BOARD_PLUGINS` point at them. The board imports each with `importlib` from its location, under a module name of its own. It runs on `/opt/multi-agent-harness`'s Python, so a file can import `multi_agent_harness.board` and the standard library but nothing installed in the task image, and not a sibling file. Before starting the board the hub runs `multi-agent-harness-board --check`; a file that fails to import, a policy file with zero or several `Policy` subclasses, or a plugin file without `register` fails setup with that error. With neither option set nothing is uploaded and the board allows everything.

The board log's first event, `config`, names the policy file and class (`policy`, `policy_class`; `default` when none is set) and the plugin file names, next to the rate limits. Plugins a strategy brings (its `board_plugins`, e.g. `multi_agent_harness.board.subagents` for `async`) are not files: they are modules in the `multi_agent_harness` package, loaded by name through `POST /api/admin/plugins` at setup and logged as a `plugins` event.

`examples/policies/hierarchy.py` lets workers send only to agents whose name starts with `lead`, and refuses their broadcasts. hello-team's agents have word names, none starting with `lead`, (or `agent-1` .. `agent-N` with `--ak agent_names=numbered`), so none is a lead and every send is refused, which makes it a quick way to see refusals in the log:

    harbor run -p examples/tasks/hello-team -e multi_agent_harness:TeamEnvironment --ek backend=daytona \
      --ek n_agents=3 --ek board_policy=examples/policies/hierarchy.py \
      -a multi_agent_harness:TeamAgent --ak harness=codex --ak model=gpt-6-luna --env-file .env -o jobs

Tests: `python -m pytest tests/board`
