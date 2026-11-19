# Proxy

`multi_agent_harness.proxy`: the model API proxy on the hub. Each agent gets its own key, the proxy forwards with the real key for the configured upstream (by default `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`, see [Upstreams](#upstreams)), meters USD per agent and stops at the budget.

    PROXY_ADMIN_TOKEN=... PROXY_BUDGET_USD=20 multi-agent-harness-proxy --host 0.0.0.0 --port 8791

Routes (anything else is a 404):
- `POST /anthropic/v1/messages` - set `ANTHROPIC_BASE_URL=http://<hub>:8791/anthropic`, `ANTHROPIC_API_KEY=<agent key>`
- `POST /openai/v1/responses`, `POST /openai/v1/chat/completions` - `OPENAI_BASE_URL=http://<hub>:8791/openai/v1`
- `POST /gemini/v1beta/models/<model>:generateContent`, `:streamGenerateContent`, `:countTokens` (free) - `GOOGLE_GEMINI_BASE_URL=http://<hub>:8791/gemini`, metered from `usageMetadata` (thinking tokens bill as output). A `key=` query parameter is dropped, never forwarded.
- `POST /admin/agents {"name"}` -> `{"agent","key"}`, `GET /admin/usage`; both need `Authorization: Bearer $PROXY_ADMIN_TOKEN`

Agent keys go in `x-api-key`, `x-goog-api-key` or `Authorization: Bearer`. Only their sha256 is stored (`PROXY_KEYS`). Streaming responses are relayed chunk by chunk and metered from the SSE usage events as they pass. Streamed chat completions get `stream_options.include_usage` turned on so they can be metered.

Every request, refusals included, appends a JSONL record to `PROXY_LOG`: ts, agent, provider, model, path, status, tokens, usd, latency_ms. The log is replayed on start, so a restart keeps spend.

Budget: `PROXY_BUDGET_USD` (total, required) and `PROXY_AGENT_BUDGET_USD` (optional). The check runs before forwarding, so requests already in flight when a cap is crossed finish and are billed. Spend can overshoot a cap by the cost of those requests. Refusals are 429 with `x-should-retry: false`.

Pricing is `src/multi_agent_harness/proxy/pricing.json` (USD per million tokens), or `PROXY_PRICING` pointing at another file. A model not in it is refused with 400, as are price modifiers it can't price (unknown `speed`, `inference_geo`, `service_tier`). Not metered: server-tool fees (e.g. web search), OpenAI cache-write tokens, and a stream cut off before its final usage event (counted as far as it got).

Rate limit: each agent gets a token bucket of 4 requests/sec with a burst of 20, and at most 12 requests in flight. Over either, the proxy answers 429 in the provider's error shape with `retry-after` (whole seconds), `retry-after-ms` and `x-should-retry: true`, and a message ending "Please try again in Ns". The usage record has status 429 and `reason: "rate_limit"`. A budget refusal is also a 429 but has `x-should-retry: false`, no `retry-after`, and `reason: "budget"`, so the CLI stops instead of retrying. Both CLIs retry a rate-limit 429 on their own: Claude Code up to 10 times (`CLAUDE_CODE_MAX_RETRIES`), waiting `retry-after-ms`/`retry-after`; Codex up to `request_max_retries` (default 4) with backoff, taking the delay from the "try again in" text. The defaults sit well above normal use: Codex on the N=50 record never had more than one request in flight or started more than one per second, and Claude Code runs at most 10 tools at once (`CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY`), so 12 in flight leaves room for subagents plus side calls. Change them with `PROXY_RATE_PER_SEC`, `PROXY_RATE_BURST` and `PROXY_MAX_CONCURRENT`; 0 turns either limit off.

The status in the log is the upstream HTTP status. An error event inside a 200 stream (Anthropic `error`, OpenAI `response.failed` or `error`, or a chat completions error chunk) is still logged as 200, with `reason: "stream_error"` and the provider's message in `error`. A client that hangs up after the final event (`message_stop`, `response.completed`, `[DONE]`) is not counted as "stream ended early".

## Upstreams

Each provider format (anthropic, openai, gemini) goes to one upstream: a base URL and a key. By default those are `https://api.anthropic.com`, `https://api.openai.com` and `https://generativelanguage.googleapis.com` with `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY`. The proxy also takes:

- `PROXY_ANTHROPIC_UPSTREAM`, `PROXY_OPENAI_UPSTREAM`, `PROXY_GEMINI_UPSTREAM`: other base URLs; the route's path (`v1/messages`, `v1/responses`, ...) is appended
- `PROXY_UPSTREAM_HEADERS`: a JSON object of headers added to every upstream call, with `{agent}` replaced by the agent's name. They can't replace auth, content or Anthropic version headers.
- `PROXY_MODEL_MAP`: a JSON file mapping the model name an agent sends to the model it is billed as. A mapped name is priced as its target and the record gets `billed_as`. A name not in the map is priced as itself, so anything without a price is still refused.

`TeamEnvironment` sets the upstream URLs and keys from `--ek model_upstream` (`src/multi_agent_harness/harbor/upstream.py`), resolved on the host before any sandbox starts:

- `direct` (default): exactly what the proxy did before upstreams were configurable
- `subscription`: the openai format goes to the ChatGPT backend on one or more ChatGPT logins, see [below](#chatgpt-subscription). No anthropic upstream.
- an `http(s)://` URL: any gateway serving the formats under that root, with the provider keys as in direct and real model names

The hub's allow-list in restricted phases is the upstream URLs' hostnames, and the fail-closed hub check probes those URLs. The sandbox ledger records `model_upstream` and `model_hosts` (and `models_mapped` when a model map is in use); the trial metadata has `model_upstream`.

Pricing stays by provider format: a mapped name is priced from the table of the route it came in on. A model whose name (or mapped target) isn't in `pricing.json` is refused until it is added. A gateway's own budget refusal comes back as the upstream's own error.

## ChatGPT subscription

`--ek model_upstream=subscription` runs Codex agents on ChatGPT plan logins instead of `OPENAI_API_KEY`. Agents are set up exactly as in direct mode: Codex in the sandbox talks to the proxy with its own agent key and never sees a login. The proxy sends `/v1/responses` to `https://chatgpt.com/backend-api/codex/responses`, the base URL Codex itself uses in ChatGPT mode, with `Authorization: Bearer <access token>` and `ChatGPT-Account-ID`, and passes on Codex's own non-auth headers (`originator`, `session_id`, `x-codex-turn-state`, ...). Chat completions are refused with 400, the backend only serves Responses.

Log in once on the host. Codex must store the login in a file, not the OS keyring, so set `cli_auth_credentials_store = "file"` in `~/.codex/config.toml` if you changed it.

    codex login --device-auth    # prints a URL and a code; works over ssh
    codex login                  # or the browser flow, on a machine with a browser

That writes `~/.codex/auth.json` (or `$CODEX_HOME/auth.json`), which the preset picks up. For several logins, give each its own `CODEX_HOME` and point `MULTI_AGENT_HARNESS_CODEX_LOGINS` at the parent dir; every `*.json` and `*/auth.json` in it is one login:

    CODEX_HOME=~/codex-logins/a codex login --device-auth
    CODEX_HOME=~/codex-logins/b codex login --device-auth
    MULTI_AGENT_HARNESS_CODEX_LOGINS=~/codex-logins

The files are read on the host, checked for ChatGPT tokens (an API-key `auth.json` is refused before anything starts) and uploaded to the hub as `/run/multi-agent-harness/codex-login-N.json`, root-only, next to `proxy.env` (`PROXY_CODEX_LOGINS`). Nothing about them goes to agents, commands or logs. The hub's allow-list in restricted phases is `chatgpt.com` and `auth.openai.com`.

Refresh happens on the hub, like Codex does it: when the access token is within 5 minutes of expiry or the last refresh is 8 days old, and once more if the backend answers 401. It posts the refresh token to `https://auth.openai.com/oauth/token` with Codex's client id and writes the new tokens back to the hub's copy, so a proxy restart keeps them. The host's `auth.json` is not updated, and refresh tokens are single use, so the hub's copy and the host's drift apart: after a run, Codex on the host may need `codex login` again.

Each agent sticks to one login; a new agent goes to the login with the fewest agents. Usage records carry `billed_as: "subscription"` and `login: N`. Spend is still priced from `pricing.json` at API rates, so `budget_usd` caps the API-equivalent cost and a model not in the table is refused. When a login hits its plan limit the backend answers 429 (`usage_limit_reached`, with `resets_at`); the agent gets the proxy's usual retryable rate-limit 429 with `retry-after` set to the reset time and `reason: "subscription_rate_limit"` in the log. Agents are not moved to another login on a 429.

Claude Code runs on a Claude plan differently, see [below](#claude-subscription).

Policy, as the providers state it. OpenAI's Codex auth docs say API keys "are still the recommended default for automation", describe copying `~/.codex/auth.json` to a headless machine, and say access tokens "are intended for trusted scripts, schedulers, and private CI runners". OpenAI's terms of use bar automatically or programmatically extracting data or output and circumventing rate limits. Check these against your use before running on a subscription.

## Claude subscription

`--ek model_upstream=subscription` with `--ak harness=claude-code` runs Claude Code agents on the runner's own Pro or Max plan. The proxy is not involved. Anthropic's Claude Code docs (Legal and compliance, "Authentication and credential use") say developers "may not collect, store, or intermediate Claude.ai credentials or session tokens - sign-in to a Claude account must complete through Anthropic's own flow", and that this does not "prevent an end user from signing in to the unmodified Claude Code binary with their own Claude subscription, including where a platform hosts Claude Code". So the token is made by Anthropic's own flow and only ever handed to unmodified Claude Code:

    claude setup-token     # Anthropic's browser flow; prints a one-year token, saves nothing
    # then in .env:  CLAUDE_CODE_OAUTH_TOKEN=<token>

The Authentication docs describe this token as the one to use "for CI pipelines and scripts where browser login isn't available": set it as `CLAUDE_CODE_OAUTH_TOKEN` "wherever you want to authenticate". It needs a Pro, Max, Team or Enterprise plan and "can only make model requests".

How it runs:
- The environment reads `--ak harness` from Harbor's trial config before anything starts, and resolves the preset to `claude-subscription` (that is what the ledger and the trial metadata record as `model_upstream`). Codex keeps the ChatGPT logins above.
- The token is read from the host env by name and written only into each agent sandbox's root-only `/run/multi-agent-harness/agent.env`. Claude Code gets no `ANTHROPIC_BASE_URL`, proxy key or hub headers, and `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` keeps the token out of the environment of its Bash and MCP subprocesses. Nothing goes to the hub: the proxy has no anthropic upstream and no hub file holds the token.
- In restricted phases the agents' allow-list is the hub's host plus `api.anthropic.com`, the only host Claude Code needs on an env token (code.claude.com/docs/en/network-config; OAuth sign-in and refresh don't happen). Each switch is checked from inside every agent: the hub and `api.anthropic.com` must answer, the probe URL must not. The hub gets no model hosts.
- The agent's own tools are kept off it by Claude Code's managed settings, `/etc/claude-code/managed-settings.json` (highest precedence, so no settings file an agent writes overrides them). Read and Edit deny rules (Grep, Glob and Write follow them) cover `agent.env`, `git-credentials`, `gitconfig`, `/proc/*/environ` (the CLI's own environment; the rest of /proc stays readable) and the managed settings dir; in Claude Code 2.1.312 deny rules are checked before the permission mode, so they hold under `bypassPermissions`. Bash runs in the scrub sandbox (its own PID namespace, so no other process's environ), with `sandbox.filesystem.denyRead` hiding `agent.env` and `sandbox.allowUnsandboxedCommands: false` so it can't run outside. Other Claude Code agents get the deny rules only. Read from Claude Code's code, not yet checked on a live run.
- Before the agents' `/logs` are downloaded, the token is replaced with `[REDACTED]` in every file there, inside the sandbox. The host then checks everything under the trial's agent dir again, redacts what it finds and logs only the count. `.credentials.json` is never written on an env token and lives under `CLAUDE_CONFIG_DIR` in the root-only dir anyway, which is never downloaded.

No budget cap applies. The calls never pass the proxy, so `budget_usd` and `agent_budget_usd` are not enforced for these agents; setup logs a warning saying so. Plan limits are the only ceiling. Anthropic notes that "advertised usage limits for Pro and Max plans assume ordinary, individual usage of Claude Code and the Agent SDK".

Usage stays comparable with API runs: after the run each assistant message in the agents' Claude Code transcripts (`sessions/projects/**/*.jsonl`, each message id once) is priced from `pricing.json` like a proxy request. The result is `subscription-usage.json` in the trial's agent dir, in the proxy's `/admin/usage` shape plus `unpriced` models, and it fills the trial's cost and tokens. The metadata has `billed_as: "claude-subscription"`, `budget_enforced: false` and `cost_source: "transcripts"`.

A mixed team (`--ak team` with Codex and Claude Code groups, see `docs/architecture.md`, Mixed teams) on `--ek model_upstream=subscription` gets both routes. The hub holds the ChatGPT logins for the Codex agents, as above, and only the Claude Code agents get the token in their `agent.env` and `api.anthropic.com` on their allow-list. `model_upstream` stays `subscription`. The budget caps only the Codex agents. `subscription-usage.json` covers the Claude Code agents, the trial's cost and tokens are the proxy's totals plus theirs, and the metadata has `billed_as: "mixed"`, `budget_enforced: false`, `cost_source: "proxy+transcripts"` and each agent's own `billed_as`.

Tests: `python -m pytest tests/proxy` (fake upstream, no network).
