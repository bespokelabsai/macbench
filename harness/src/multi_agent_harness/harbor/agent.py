"""TeamAgent: a Harbor agent that runs N CLI coding agents as a team, one sandbox each.

Setup, per agent sandbox and under the environment's baseline network policy: Harbor's own
installed-agent class for the harness (ClaudeCode, Codex, ...) installs the CLI, exactly as in
a vanilla Harbor trial, through a thin adapter that points its exec and uploads at that
sandbox; git and curl come from the same package-manager detection. Agents are registered on
the board and the proxy from the hub.

Run: Harbor has applied the agent-phase network policy by now. The agent asks the environment
to confirm it from inside every agent sandbox and fails closed otherwise; only then are the
agent tokens written and the CLIs launched. Who launches with what prompt, and when the run
ends, is the orchestration strategy's call (a strategy file, --ak orchestration=...).

The hub is reached through the URL the sandbox backend exposes for its gateway port, so every
request also carries that URL's access headers (on Daytona, the preview token in
X-Daytona-Preview-Token): ANTHROPIC_CUSTOM_HEADERS for Claude Code, a custom model provider with
env_http_headers for Codex, extra MCP headers for the board, and an http.extraHeader include for
git. The header values live in agent.env as MULTI_AGENT_HARNESS_HUB_HEADER_1..k. Antigravity (agy)
can't add headers to its model calls, so where there are any it calls a small forwarder on
127.0.0.1 in its own sandbox (agy_forward.py) that adds them.

Secrets never go in a command line or exec env (the exec env is part of the command string). They move only as
uploaded files: admin tokens to a root-only dir on the hub, agent credentials to /run/multi-agent-harness
(mode 700) in the agent's sandbox, which the launch command sources. Registration responses are
written to files on the hub and downloaded, not printed. Nothing token-bearing lives under
/logs, which gets downloaded into the trial dir.

Claude Code on a subscription (model_upstream=subscription with harness=claude-code, which the
environment resolves to claude-subscription): the runner's own CLAUDE_CODE_OAUTH_TOKEN, from
claude setup-token, is read from the host env by name and written only into each agent's
agent.env, where unmodified Claude Code reads it and calls Anthropic directly. The hub, its proxy
and its files never see it. Claude Code's managed settings keep the agent's own tools off it (see
_claude_settings). No budget applies to those calls; usage is priced afterwards from the
session transcripts. Before anything is downloaded the token is scrubbed from each sandbox's
/logs, and the trial dir is checked again on the host.

CLI invocations follow Harbor's own claude-code, codex and antigravity-cli agents
(harbor/agents/installed/claude_code.py, codex.py, antigravity_cli.py), whose parsers also build
trajectories.

Mixed teams (--ak team, roster.py): each agent sandbox runs its own group's harness, model and CLI
version, and everything per harness above (install, config, launch, sessions, trajectories) follows the
sandbox. An identity a strategy binds to a sandbox runs that sandbox's harness.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shlex
import shutil
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from harbor.agents.base import BaseAgent
from harbor.agents.factory import AgentFactory
from harbor.agents.installed.base import BaseInstalledAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.agent.name import AgentName

from .. import names as naming
from ..proxy.metering import transcript_usage
from . import checkpoint as ck
from . import marks
from . import roster
from .orchestration import BUNDLED, LEAD, locate, resolve
from .roster import DEFAULT_MODEL, RUNNERS  # noqa: F401 - DEFAULT_MODEL kept importable from here
from .upstream import CLAUDE_SCRUB_VAR, CLAUDE_TOKEN_VAR

HARBOR_NAMES = {"antigravity": "antigravity-cli"}  # --ak harness name -> Harbor's agent name
SECRETS = "/run/multi-agent-harness"              # the agent user's only, token-bearing; never downloaded
AGENT_ENV = f"{SECRETS}/agent.env"
GIT_CREDENTIALS = f"{SECRETS}/git-credentials"
GIT_INCLUDE = f"{SECRETS}/gitconfig"  # the hub's access headers for the team remote
# Root's (mode 711): where root leaves files for a non-root agent once agent code has run there; the agent copies them.
HANDOFF = "/run/multi-agent-harness-handoff"
# Claude Code's managed settings: the highest precedence, so no settings file an agent writes overrides them.
CLAUDE_MANAGED_DIR = "/etc/claude-code"
CLAUDE_MANAGED = f"{CLAUDE_MANAGED_DIR}/managed-settings.json"
PREVIEW_HEADER = "X-Daytona-Preview-Token"  # for environments that only expose team_preview_token
BOARD_LOCAL, PROXY_LOCAL = "http://127.0.0.1:8790", "http://127.0.0.1:8791"  # admin calls, on the hub
ADMIN = f"{SECRETS}/team-agent"   # on the hub: admin headers and registration responses
LOGS = "/logs/agent"              # per-agent output, downloaded into <trial agent dir>/agent-i/
WORKDIR = "/workspace"            # fallback when the environment resolved no team_workdir
IMAGE_GIT_ASIDE = "/var/tmp/multi-agent-harness-image-git"  # where a .git the image left in the workdir goes
BOARD_LOG = "/var/lib/multi-agent-harness/board-events.jsonl"   # fallbacks if the env lacks team_*_log_path
PROXY_LOG = "/var/lib/multi-agent-harness/proxy-usage.jsonl"
CLAUDE_SUB = "claude-subscription"  # the environment's resolved model_upstream for Claude Code on a subscription token
REDACTED = "[REDACTED]"
KILL_GRACE_SEC = 30
TICK_SEC = 5        # how often the strategy's tick() runs while agents work
STOP_WAIT_SEC = 60  # after a stop, how long to wait for the agent's launch command to return
CLI_PID = f"{SECRETS}/cli.pid"  # the running CLI's pid (timeout's, when there is a limit)
# agy keeps settings, MCP config and sessions under ~/.gemini and its MCP headers take no env vars,
# so ~/.gemini is a link into the root-only dir and the board token is written there.
GEMINI_HOME = f"{SECRETS}/gemini"
AGY_STATE = f"{GEMINI_HOME}/antigravity-cli"
AGY_MCP = f"{GEMINI_HOME}/config/mcp_config.json"
AGY_FORWARD = f"{SECRETS}/agy-forward.py"
AGY_FORWARD_PORT = f"{SECRETS}/agy-forward.port"
# Checkpoints: per harness, the CLI home and the session paths in it that get copied (token files
# never, see checkpoint.TOKEN_FILES), and the agent.env variables whose values are scrubbed.
SESSION_HOMES = {"codex": (f"{SECRETS}/codex", (".",)), "claude-code": (f"{SECRETS}/claude", ("projects",)),
                 "antigravity": (AGY_STATE, ("brain", "conversations"))}
SESSION_EXCLUDES = ("config.toml", "log", "tmp", ".tmp", ".claude.json*", "logs_*.sqlite*")
# Where a running CLI's session id is, before the launch command writes session-id at exit.
SESSION_ID_FROM = {"codex": ("codex*.txt", "thread_id"), "claude-code": ("claude-code*.txt", "session_id"),
                   "antigravity": ("antigravity-stream*.jsonl", "conversation_id")}
SECRET_VARS = (CLAUDE_TOKEN_VAR, "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
               "MULTI_AGENT_HARNESS_BOARD_TOKEN")
CKPT_STAGE = "/tmp/multi-agent-harness-checkpoint"
RESUME_BOARD_DIR = "/var/tmp/multi-agent-harness-resume"
RESUME_BOARD = f"{RESUME_BOARD_DIR}/board-events.jsonl"  # the checkpoint's board log, readable by agents
# agy is pinned to one build from Google's release manifest (manifests/linux_<arch>.json of the
# Antigravity CLI auto-updater).
AGY_VERSION = "1.4.4"
_AGY_BASE = "https://storage.googleapis.com/antigravity-public/antigravity-cli/1.4.4-4783783166214144"
AGY_BUILDS = {
    "x86_64|amd64": (f"{_AGY_BASE}/linux-x64/cli_linux_x64.tar.gz",
                     "7ce149d350344c1046c7b59b6659bcc5d659f351bf3a07c6c085887d93427ceb"
                     "89d927752e4ccd0844b53ffb91d33d6a49f72835ceb276c7cffdd13cfb8023b8"),
    "aarch64|arm64": (f"{_AGY_BASE}/linux-arm/cli_linux_arm64.tar.gz",
                      "7bfc4f7e5e2dda4cd0c536c72cf9725c2918a038028ad11fa4f7f5e57db77e9f"
                      "f7d6b9beca05cd85618b77dd3e77de64b63629d33e5779f29111390dc53c7aaf"),
}
# python3 for the forwarder: the image's, else the package manager's (run as root, at install).
PYTHON_CHECK = ("command -v python3 >/dev/null 2>&1 || { if command -v apt-get >/dev/null 2>&1; then "
                "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-minimal; "
                "elif command -v apk >/dev/null 2>&1; then apk add --no-cache python3; "
                "elif command -v dnf >/dev/null 2>&1; then dnf install -y python3; "
                "elif command -v yum >/dev/null 2>&1; then yum install -y python3; fi; } >/dev/null 2>&1; "
                "command -v python3 >/dev/null 2>&1")
AGY_AGENT = "teammate"  # a main agent without agy's subagent tools, for strategies that turn them off
AGY_AGENT_FILE = f"""---
name: {AGY_AGENT}
description: Coding agent working on a team task.
tools:
  - read_file
  - run_command
  - manage_task
  - write_file
  - edit_file
  - search_web
  - ask_question
subagent: false
mainAgent: true
commandExecutionPolicy: eager
---
You are an agentic AI coding assistant. Work autonomously on the user's task in the workspace until it is done.
"""
# Where Harbor's installs put the CLIs: claude-code in ~/.local/bin, codex under nvm (Harbor's
# Codex agent sources nvm.sh the same way before running it).
PATH_HEAD = ('export PATH="$HOME/.local/bin:$PATH"; '
             'if [ -s "$HOME/.nvm/nvm.sh" ]; then . "$HOME/.nvm/nvm.sh" || true; fi; ')

# Reasoning efforts each CLI accepts for --ak effort. Codex's ultra is left out: it is max plus automatic task
# delegation (subagents), so a team of k would no longer be k agents.
EFFORTS = {"claude-code": ("low", "medium", "high", "xhigh", "max"),
           "codex": ("minimal", "low", "medium", "high", "xhigh", "max")}

PREAMBLE = """\
You are {name}, one of {n} agents working as a team on the task below. Your teammates: {others}.
Each of you works in your own sandbox. You share two things:
- A message board, the MCP server "board". Message teammates with its tools: send(text, to=None) \
(to=None means everyone; to can also be one name or a list of names), inbox(wait_seconds=60) to wait for replies, history() to re-read.
- A git repository. Your working directory, {workdir}, is your own clone of it (remote origin, \
branch main, credentials already set up), and it starts with the task's files. Teammates see \
your work only after you commit and push it; pull to get theirs. You can push to main and to \
your own branches under {name}/. Pushes to main must be fast-forwards: if one is rejected, pull \
--rebase and push again. Force pushes, deleting branches and pushing to a teammate's branches \
are refused. When the team is done, {workdir} is replaced by main exactly as pushed, so anything \
not committed to main is gone, including empty directories and gitignored files.

Task:
"""


def agy_install_script() -> str:
    """Install the pinned agy build to ~/.local/bin/agy, checking its sha512."""
    cases = "".join(f'{arch}) u={shlex.quote(url)}; s={sha};; ' for arch, (url, sha) in AGY_BUILDS.items())
    v = AGY_VERSION
    return (f'set -e; case "$(uname -m)" in {cases}*) echo "agy {v}: no build for $(uname -m)" >&2; exit 1;; esac; '
            f'if ldd /bin/ls 2>&1 | grep -q musl; then echo "agy {v}: no build for musl images" >&2; exit 1; fi; '
            'd=$(mktemp -d); curl -fsSL -o "$d/agy.tgz" "$u"; '
            f'echo "$s  $d/agy.tgz" | sha512sum -c - >/dev/null || {{ echo "agy {v}: sha512 mismatch" >&2; exit 1; }}; '
            'tar -xzf "$d/agy.tgz" -C "$d" antigravity; mkdir -p "$HOME/.local/bin"; '
            'mv "$d/antigravity" "$HOME/.local/bin/agy"; chmod 755 "$HOME/.local/bin/agy"; rm -rf "$d"; '
            '"$HOME/.local/bin/agy" --version')


def installer_class(harness: str) -> type[BaseInstalledAgent]:
    """Harbor's installed-agent class for a harness name (any name `harbor run -a` accepts)."""
    try:
        cls = AgentFactory.get_agent_class(AgentName(HARBOR_NAMES.get(harness, harness)))
    except ValueError as e:
        raise ValueError(f"unknown harness {harness!r}: not a Harbor agent name") from e
    if not issubclass(cls, BaseInstalledAgent):
        raise ValueError(f"harness {harness!r} is not one of Harbor's installed agents")
    return cls


def reset_script(workdir: str) -> str:
    """Put an existing clone back to main exactly as pushed: no local changes, untracked or
    ignored files, other local branches or stashes."""
    return (f"cd {shlex.quote(workdir)} && git fetch -q origin && git checkout -q -f -B main origin/main && "
            "git clean -ffdxq && git stash clear && "
            "git for-each-ref --format='%(refname:short)' refs/heads | grep -vx main | xargs -r git branch -q -D")


def clone_script(workdir: str, url: str) -> str:
    """Make the workdir a clone of the team repo in place. The image already put the same
    starting files there, so checkout -f just records them; untracked files stay as they are,
    and a .git the image left behind is moved aside, not deleted. Credentials come from the
    global credential helper, never the URL."""
    return (f"W={shlex.quote(workdir)}; mkdir -p \"$W\" && cd \"$W\"; "
            f'if [ -e .git ]; then mv .git {IMAGE_GIT_ASIDE}.$$; fi; '
            f"git init -q && git symbolic-ref HEAD refs/heads/main && "
            f"git remote add origin {shlex.quote(url)} && git fetch -q origin && "
            f"git checkout -q -f -B main origin/main")


class ServiceEnvironment:
    """One agent sandbox seen as a plain environment, so a Harbor installed agent's setup() and
    install() run there: exec, uploads and downloads go to that service; anything else is the
    team environment's."""

    def __init__(self, env: BaseEnvironment, service: str):
        self._env = env
        self.service = service

    @property
    def default_user(self) -> str | int | None:
        return getattr(self._env, "default_user", None)

    async def exec(self, command: str, cwd: str | None = None, env: dict[str, str] | None = None,
                   timeout_sec: int | None = None, user: str | int | None = None):
        return await self._env.service_exec(command, service=self.service, cwd=cwd, env=env,
                                            timeout_sec=timeout_sec, user=user)

    async def upload_file(self, source_path, target_path):
        await self._env.service_upload_file(source_path, target_path, service=self.service)

    async def upload_dir(self, source_dir, target_dir):
        await self._env.service_upload_dir(source_dir, target_dir, service=self.service)

    async def download_file(self, source_path, target_path):
        await self._env.service_download_file(source_path, target_path, service=self.service)

    async def download_dir(self, source_dir, target_dir):
        await self._env.service_download_dir(source_dir, target_dir, service=self.service)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._env, name)


REMINDER_NOTE = ("The harness sends periodic time-remaining reminders to your board inbox. "
                 "Check inbox() regularly while working to receive them.")


def _after_identity(prompt: str, note: str) -> str:
    """`note` as the prompt's second line: the identity line stays first and the task last."""
    first, nl, rest = prompt.partition("\n")
    return f"{first}\n{note}\n{rest}" if nl else f"{first}\n{note}"


def _duration(sec: float) -> str:
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def _task_user(env: BaseEnvironment) -> str | None:
    """The user the task runs its agent as (task.toml [agent] user), or None when that is root."""
    user = getattr(env, "default_user", None)
    return None if user in (None, 0, "0", "root") else str(user)


def _admin(env: BaseEnvironment) -> str | None:
    """The user for the harness's own commands: root, named only when the task's agent user is someone
    else, so a root task's commands run as before (no su)."""
    return "root" if _task_user(env) else None


def _root(url: str) -> str:
    """Service root URL, tolerating a trailing /mcp or /api."""
    url = url.rstrip("/")
    for suffix in ("/mcp", "/api"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    return url


def hub_headers(env: Any) -> dict[str, str]:
    """The access headers for the hub's exposed URL, each sent on every hub request."""
    headers = getattr(env, "team_hub_headers", None)
    if headers is None and getattr(env, "team_preview_token", None):
        headers = {PREVIEW_HEADER: env.team_preview_token}
    return dict(headers or {})


def header_vars(env: Any) -> dict[str, str]:
    """Header name -> the agent.env variable holding its value."""
    return {k: f"MULTI_AGENT_HARNESS_HUB_HEADER_{i}" for i, k in enumerate(hub_headers(env), 1)}


def _exports(values: dict[str, str]) -> str:
    return "".join(f"export {k}={shlex.quote(v)}\n" for k, v in values.items())


def _proxy_totals(usage: Any) -> dict[str, float]:
    """Overall cost/tokens from the proxy's /admin/usage: {"total": {"usd", "tokens": {...}}}."""
    total = usage.get("total") if isinstance(usage, dict) else None
    if not isinstance(total, dict):
        return {}
    tokens = total.get("tokens") or {}
    return {"cost_usd": total.get("usd", 0.0),
            "input_tokens": tokens.get("input", 0),
            "output_tokens": tokens.get("output", 0),
            "cache_tokens": sum(tokens.get(k, 0) for k in ("cache_read", "cache_write", "cache_write_1h"))}


def _usage_sum(*usages: Any) -> dict[str, Any]:
    """The totals of several /admin/usage-shaped records added up: for a mixed team, the proxy's
    (the agents behind it) and the transcripts' (Claude Code agents on the subscription token)."""
    usd, tokens = 0.0, {}
    for usage in usages:
        total = usage.get("total") if isinstance(usage, dict) else None
        if isinstance(total, dict):
            usd += total.get("usd") or 0.0
            for k, v in (total.get("tokens") or {}).items():
                tokens[k] = tokens.get(k, 0) + (v or 0)
    return {"total": {"usd": usd, "tokens": tokens}}


def _first_int(out: str | None) -> int:
    line = (out or "").strip().splitlines()[:1]
    return int(line[0]) if line and line[0].isdigit() else 0


def _extract(tgz: Path, dest: Path) -> None:
    with tarfile.open(tgz) as t:
        t.extractall(dest, filter="data")


async def _put(env: BaseEnvironment, service: str | None, path: str, content: str) -> None:
    """Upload content as a file, so it never appears in a command string."""
    fd, tmp = tempfile.mkstemp(prefix="multi-agent-harness-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        if service is None:
            await env.upload_file(tmp, path)
        else:
            await env.service_upload_file(tmp, path, service=service)
    finally:
        os.unlink(tmp)


async def _get(env: BaseEnvironment, path: str) -> str:
    """Read a hub file through download, so its content never appears in exec output."""
    fd, tmp = tempfile.mkstemp(prefix="multi-agent-harness-")
    os.close(fd)
    try:
        await env.download_file(path, tmp)
        return Path(tmp).read_text()
    finally:
        os.unlink(tmp)


class TeamAgent(BaseAgent):
    """N Claude Code, Codex or Antigravity agents, or a mix of them, on one board, one proxy and one
    git remote."""

    def __init__(self, logs_dir: Path, model_name: str | None = None, harness: str = "codex",
                 model: str | None = None, timeout_sec: float | str | None = None,
                 cli_version: str | None = None, orchestration: str | None = None,
                 checkpoint_every_min: float | str | None = 15, team: Any = None,
                 agent_names: str | None = None, grade_at: str | None = None, effort: str | None = None,
                 time_reminders: int | str = 0,
                 **kwargs):
        # --ak model=... is a convenience for -m; harbor would otherwise drop it silently.
        self._groups = roster.groups(team, harness, model_name or model, cli_version)
        self._installers: dict[str, type[BaseInstalledAgent]] = {}
        for g in self._groups:
            self._installers[g.harness] = installer_class(g.harness)
            if g.harness not in RUNNERS:
                raise ValueError(f"harness {g.harness!r} installs through Harbor, but TeamAgent only knows "
                                 f"how to configure and launch {RUNNERS}")
            if g.harness == "antigravity" and g.cli_version not in (None, "", AGY_VERSION):
                raise ValueError(f"antigravity is pinned to agy {AGY_VERSION}; cli_version={g.cli_version!r} "
                                 "is not known")
        self.harnesses = {g.harness for g in self._groups}
        # --ak effort: the CLIs' reasoning effort (claude --effort, codex model_reasoning_effort); unset leaves
        # each CLI's default.
        self.effort = (effort or "").strip() or None
        # A group's own effort (--ak team [{..., "effort": "max"}]) wins over --ak effort for that group; a team-wide
        # --ak effort applies only to the groups whose harness takes it (Antigravity takes none: its thinking level
        # is in the model slug).
        for g in self._groups:
            e = g.effort or (self.effort if self.effort in EFFORTS.get(g.harness, ()) else None)
            if g.effort and g.effort not in EFFORTS.get(g.harness, ()):
                raise ValueError(f"effort={g.effort!r} is not an effort {g.harness} takes "
                                 f"({', '.join(EFFORTS.get(g.harness, ())) or 'it has no effort setting'})")
            object.__setattr__(g, "effort", e)
        if self.effort and not any(self.effort in EFFORTS.get(h, ()) for h in self.harnesses):
            if len(self.harnesses) == 1:
                [h] = self.harnesses
                raise ValueError(f"effort={self.effort!r} is not an effort {h} takes "
                                 f"({', '.join(EFFORTS.get(h, ())) or 'it has no effort setting'})")
            raise ValueError(f"effort={self.effort!r} is not an effort any harness in the team takes")
        self.harness = roster.label(self.harnesses)  # the one harness, or "mixed"
        self._seats: dict[str, roster.Group] = {}  # agent sandbox -> its group, from _plan()
        self.orchestration = resolve(orchestration)
        spec = (orchestration or "fixed").strip()
        self.orchestration_name = spec if spec in BUNDLED else str(locate(spec))
        if not re.fullmatch(r"[0-9]+", str(time_reminders)):
            raise ValueError("time_reminders must be a non-negative integer")
        self.time_reminders = int(time_reminders)
        self._reminder_slots: dict[str, int] = {}
        self.roles: dict[str, str] = {}
        self.agent_resources: dict[str, dict[str, Any]] = {}
        self.on_board: set[str] = set()
        self.exits: dict[str, Any] = {}
        self.stopped: set[str] = set()
        self.lost: dict[str, str] = {}  # sandbox -> why its CLI's task ended with an exception
        self._launched: dict[str, float] = {}  # sandbox -> monotonic time of its last launch
        # Per identity that ran a CLI: {"slot", "logs", "sessions", "exit", "stopped"}. An identity
        # is a sandbox's own name, or a board identity a strategy bound to a sandbox at launch.
        self.runs: dict[str, dict[str, Any]] = {}
        self.holder: dict[str, str] = {}  # sandbox -> identity whose CLI it last ran
        self._tasks: dict[str, asyncio.Task] = {}
        self._env: Any = None
        self.workdir = WORKDIR
        self._forwarding = False  # antigravity behind hub access headers: see agy_forward.py
        self._claude_sub = False  # Claude Code on the runner's subscription token, straight to Anthropic
        self._claude_scrub = True  # Claude Code keeps that token out of its subprocesses (CLAUDE_SCRUB_VAR)
        self.end_reason: str | None = None
        self.timeout_sec = float(timeout_sec) if timeout_sec not in (None, "") else None
        self.agents: list[str] = []
        # Slot -> agent-facing name (board, git, prompts), from the environment; see names.py.
        # The environment reads --ak agent_names itself; it is checked here so a typo fails early.
        naming.check_mode(agent_names)
        self.names: dict[str, str] = {}
        self.name_mode = naming.NUMBERED
        self.name_seed = ""
        self.install_sec: dict[str, float] = {}
        self._creds: dict[str, tuple[str, str]] = {}
        self._proxy_register_lock = asyncio.Lock()
        # Checkpoints (checkpoint.py): every checkpoint_every_min while agents run, and a final one.
        self.checkpoint_every_sec = float(checkpoint_every_min or 0) * 60
        self._ckpt_lock = asyncio.Lock()
        self._ckpt_task: asyncio.Task | None = None
        self._ckpt_final_done = False
        self.checkpoints: list[str] = []
        self._original_timeout: float | None = None  # the first run's agent timeout, kept across resumes
        self._elapsed_base = 0.0  # agent-phase seconds before this run, when resumed
        self._phase_t0: float | None = None
        self._resume: dict[str, Any] | None = None  # the environment's team_resume
        self._restored: dict[str, str] = {}  # sandbox -> CLI session id restored into it
        self.resume_outcome: dict[str, str] = {}
        # Graded marks (marks.py): --ak grade_at, agent time from when the agents start.
        self.grade_at = marks.parse(grade_at)
        self.mark_plan: list[dict[str, Any]] = []  # the marks this run reaches, set in setup()
        self.marks: list[dict[str, Any]] = []      # one record per mark fired (and the final verifier's)
        self._marks_task: asyncio.Task | None = None
        self._grade_task: asyncio.Task | None = None
        self._grade_queue: asyncio.Queue | None = None
        self._regrade: list[dict[str, Any]] = []   # marks a resumed checkpoint had not finished grading
        # Harbor's record of the agent's model: the team's, when every group runs the same one.
        models = {g.model for g in self._groups}
        super().__init__(logs_dir, model_name=models.pop() if len(models) == 1 else None, **kwargs)

    @staticmethod
    def name() -> str:
        return "multi-agent-harness-team"

    def version(self) -> str | None:
        return "0.1.0"

    def _seat(self, slot: str | None = None) -> roster.Group:
        """The group of agent sandbox `slot`: its harness, model and CLI version. Before _plan(), or
        with no slot, the first group."""
        if slot is None or not self._seats:
            return self._groups[0]
        return self._seats[slot]

    @property
    def _model(self) -> str | None:
        return self._seat().short_model

    @property
    def cli_version(self) -> str | None:
        return self._seat().cli_version

    # -- setup ------------------------------------------------------------

    async def _hub_admin(self, env: BaseEnvironment, which: str, method: str, url: str,
                         body: dict | None = None, out: str | None = None) -> Any:
        """Call a hub admin endpoint from the hub. The header comes from an uploaded file; with
        `out`, the response goes to a file and comes back by download instead of stdout."""
        cmd = f"umask 077; curl -fsS -X {method} -H @{ADMIN}/{which}.hdr "
        if body is not None:
            cmd += f"-H 'Content-Type: application/json' -d {shlex.quote(json.dumps(body))} "
        if out:
            cmd += f"-o {out} "
        result = await env.exec(cmd + shlex.quote(url), timeout_sec=30, user=_admin(env))
        if result.return_code != 0:
            raise RuntimeError(f"{method} {url} failed ({result.return_code}): {(result.stderr or '')[:300]}")
        return json.loads(await _get(env, out) if out else result.stdout or "null")

    async def _register(self, env: BaseEnvironment, name: str) -> tuple[str | None, str]:
        """Proxy key for every sandbox; a board token only for those the strategy puts on the board
        under their own name (the rest get theirs when a strategy binds them, see launch())."""
        board = None
        if name in self.on_board:
            board = await self._hub_admin(env, "board", "POST", f"{BOARD_LOCAL}/api/admin/agents",
                                          {"name": self.facing(name), "role": self.roles.get(name)},
                                          out=f"{ADMIN}/{name}.board.json")
        async with self._proxy_register_lock:
            proxy = await self._hub_admin(env, "proxy", "POST", f"{PROXY_LOCAL}/admin/agents",
                                          {"name": self.facing(name)}, out=f"{ADMIN}/{name}.proxy.json")
        return board and board["token"], proxy["key"]

    async def _install(self, env: BaseEnvironment, name: str) -> None:
        """Harbor's installed agent sets up its CLI in this agent's sandbox, then git and curl
        (for the team remote and the network check) the same way."""
        t0 = time.monotonic()
        target = ServiceEnvironment(env, name)
        seat = self._seat(name)
        inst = self._installers[seat.harness](logs_dir=self.logs_dir / name, model_name=seat.model,
                                              version=seat.cli_version)
        if seat.harness == "antigravity":  # Harbor's install takes the latest; this one is pinned
            await inst.ensure_system_dependencies(target, ("git", "curl"))
            for cmd, user, what in ((agy_install_script(), None, f"installing agy {AGY_VERSION}"),
                                    (PYTHON_CHECK if hub_headers(env) else None, "root",
                                     "python3 for the hub header forwarder (none in the image, none installable)")):
                if cmd and (r := await target.exec(cmd, user=user, timeout_sec=600)).return_code != 0:
                    raise RuntimeError(f"{name}: {what} failed ({r.return_code}): {(r.stderr or '')[-300:]}")
        else:
            await inst.setup(target)
            await inst.ensure_system_dependencies(target, ("git", "curl"))
        self.install_sec[name] = round(time.monotonic() - t0, 1)
        self.logger.info("%s: %s installed in %.1fs", name, seat.harness, self.install_sec[name])

    def _plan(self, environment: BaseEnvironment) -> None:
        """The agent sandboxes, the group each one runs, and the strategy's roles for them."""
        self.agents = list(environment.team_agent_services)
        self._seats = roster.assign(self._groups, self.agents)
        names = getattr(environment, "team_agent_names", None)
        self.names = dict(names) if names else {a: a for a in self.agents}
        self.name_mode = getattr(environment, "team_agent_names_mode", naming.NUMBERED) if names else naming.NUMBERED
        self.name_seed = getattr(environment, "team_name_seed", "") or ""
        self.agent_resources = getattr(environment, "team_agent_resources", {})
        self.roles = self.orchestration.roles(self.agents)
        self.on_board = set(self.orchestration.on_board(self.agents))

    def _check_upstream(self, env: BaseEnvironment) -> None:
        """Whether Claude Code agents run on the runner's subscription token: the environment resolved
        claude-subscription, or (a mixed team on model_upstream=subscription) Codex on the hub's
        ChatGPT logins and Claude Code on the token."""
        mode = getattr(env, "team_model_upstream", None)
        direct = mode == CLAUDE_SUB or bool(getattr(env, "team_claude_subscription", False))
        self._claude_sub = direct and "claude-code" in self.harnesses
        if mode == CLAUDE_SUB and self.harnesses != {"claude-code"}:
            raise ValueError(f"model_upstream resolved to {CLAUDE_SUB}, which only Claude Code can run on")
        if mode == "subscription" and "claude-code" in self.harnesses and not direct \
                and "anthropic" not in getattr(env, "team_upstream_providers", ()):
            raise ValueError("model_upstream=subscription resolved to Codex ChatGPT logins, but harness is "
                             "claude-code; the environment could not read --ak harness or --ak team from the "
                             "trial config")
        if not self._claude_sub:
            return
        if not os.environ.get(CLAUDE_TOKEN_VAR):
            raise ValueError(f"{CLAUDE_SUB} needs {CLAUDE_TOKEN_VAR} in the host environment")
        self._claude_scrub = os.environ.get(CLAUDE_SCRUB_VAR, "1").strip() != "0"
        if not self._claude_scrub:
            self.logger.warning(
                "%s=0: Claude Code agents do not scrub %s from their Bash and MCP subprocesses, so commands the "
                "agents run can read the runner's subscription token. Use it only where bubblewrap cannot run "
                "(Docker without unprivileged user namespaces). Saved artifacts are still redacted on the host.",
                CLAUDE_SCRUB_VAR, CLAUDE_TOKEN_VAR)
        budget = getattr(env, "budget_usd", None)
        per_agent = getattr(env, "agent_budget_usd", None)
        claude = [n for n in self.agents if self._seat(n).harness == "claude-code"]
        mixed = len(claude) < len(self.agents)
        self.logger.warning(
            "%s: Claude Code agents%s bill the runner's Claude subscription directly. The proxy never sees "
            "their model calls, so NO budget cap applies: budget_usd=%s%s is not enforced for them%s. Usage is "
            "priced at API rates from their transcripts after the run.", CLAUDE_SUB,
            f" ({', '.join(claude)})" if mixed else "", budget,
            f", agent_budget_usd={per_agent}" if per_agent is not None else "",
            ", only for the other agents" if mixed else "")

    async def setup(self, environment: BaseEnvironment) -> None:
        self._forwarding = "antigravity" in self.harnesses and bool(hub_headers(environment))
        self._plan(environment)
        self._check_upstream(environment)
        self._resume = getattr(environment, "team_resume", None)
        if self._resume:
            m = self._resume["manifest"]
            left = ck.time_left(m)
            if left is not None and left < ck.MIN_LEFT_SEC:
                raise ValueError(f"only {int(left)}s of agent time left in the checkpoint; not resuming")
            if left is not None:
                self.timeout_sec = left
            self._original_timeout = m.get("agent_timeout_sec")
            self._elapsed_base = float(m.get("elapsed_agent_sec") or 0)
            self.logger.info("resuming checkpoint %s: %.0fs of agent time used, timeout now %ss",
                             m.get("n"), self._elapsed_base, self.timeout_sec)
        else:
            self._original_timeout = self.timeout_sec or getattr(environment, "team_task_agent_timeout_sec", None)
        self._plan_marks(environment)
        if self.timeout_sec and hasattr(environment, "set_agent_timeout"):
            await environment.set_agent_timeout(self.timeout_sec)
        await environment.exec(f"mkdir -p {ADMIN}", timeout_sec=30, user=_admin(environment))
        await _put(environment, None, f"{ADMIN}/board.hdr",
                   f"Authorization: Bearer {environment.team_board_admin_token}\n")
        await _put(environment, None, f"{ADMIN}/proxy.hdr",
                   f"Authorization: Bearer {environment.team_proxy_admin_token}\n")
        await environment.exec(f"chmod 700 {SECRETS} {ADMIN}; chmod 600 {ADMIN}/*", timeout_sec=30,
                               user=_admin(environment))
        if self.orchestration.board_plugins:
            # The strategy's tools go into the running board before anyone registers or connects.
            loaded = await self._hub_admin(environment, "board", "POST", f"{BOARD_LOCAL}/api/admin/plugins",
                                           {"modules": list(self.orchestration.board_plugins)})
            self.logger.info("board plugins loaded: %s", loaded.get("plugins"))

        async def register_all():
            try:
                creds = await asyncio.gather(*(self._register(environment, n) for n in self.agents))
            finally:
                await environment.exec(f"rm -f {ADMIN}/*.json", timeout_sec=30, user=_admin(environment))
            self._creds = dict(zip(self.agents, creds))

        # Credentials stay in memory until run(): nothing secret reaches an agent sandbox while
        # it is still under the baseline policy.
        results = await asyncio.gather(register_all(), *(self._install(environment, n) for n in self.agents),
                                       return_exceptions=True)
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            raise errors[0]
        self.logger.info("installed and registered %d agents: %s", len(self.agents), roster.describe(self._seats))

    def _agent_env(self, env: BaseEnvironment, name: str, board_token: str | None, key: str,
                   slot: str | None = None) -> str:
        """Body of /run/multi-agent-harness/agent.env: everything an agent's CLI needs, secrets included.
        `name` is who it runs as, `slot` (default `name`) the sandbox, whose harness it is."""
        harness, model = self._seat(slot or name).harness, self._seat(slot or name).short_model
        proxy = _root(env.team_proxy_url)
        headers = hub_headers(env)
        values = {
            "MULTI_AGENT_HARNESS_AGENT_NAME": name,
            "MULTI_AGENT_HARNESS_BOARD_URL": _root(env.team_board_url),
            "MULTI_AGENT_HARNESS_BOARD_TOKEN": board_token or "",
            **{var: headers[k] for k, var in header_vars(env).items()},
            "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": f"{name}@team.local",
            "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": f"{name}@team.local",
        }
        if harness == "claude-code" and self._claude_sub:
            # Straight to Anthropic on the runner's token: no proxy URL or key, and no hub headers
            # (they would go to Anthropic). Bash and MCP subprocesses don't inherit the token.
            values |= {CLAUDE_TOKEN_VAR: os.environ[CLAUDE_TOKEN_VAR],
                       "CLAUDE_CONFIG_DIR": f"{SECRETS}/claude", "IS_SANDBOX": "1",
                       "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                       "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1" if self._claude_scrub else "0"}
        elif harness == "claude-code":
            values |= {"ANTHROPIC_BASE_URL": f"{proxy}/anthropic", "ANTHROPIC_API_KEY": key,
                       "ANTHROPIC_CUSTOM_HEADERS": "\n".join(f"{k}: {v}" for k, v in headers.items()),
                       "CLAUDE_CONFIG_DIR": f"{SECRETS}/claude", "IS_SANDBOX": "1",
                       "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
        if harness == "claude-code":
            if model:
                values |= {k: model for k in (
                    "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL")}
        elif harness == "antigravity":
            # With hub headers the launch command points GOOGLE_GEMINI_BASE_URL at the forwarder.
            values |= {"GEMINI_API_KEY": key, "GOOGLE_GEMINI_BASE_URL": f"{proxy}/gemini",
                       "MULTI_AGENT_HARNESS_GEMINI_UPSTREAM": f"{proxy}/gemini"}
            if headers:
                values["MULTI_AGENT_HARNESS_HUB_HEADER_NAMES"] = json.dumps(list(headers))
        else:
            values |= {"OPENAI_BASE_URL": f"{proxy}/openai/v1", "OPENAI_API_KEY": key,
                       "CODEX_HOME": f"{SECRETS}/codex"}
        return _exports(values)

    def _agy_settings(self, slot: str | None = None) -> str:
        """agy's settings.json: Gemini API key auth (the key is the agent's proxy key). A model that
        isn't one of agy's own gemini-* slugs (a gateway's model alias) is registered as a custom model,
        which agy then sends as is."""
        config = {"modelProvider": "gemini", "allowNonWorkspaceAccess": True, "enableTelemetry": False}
        model = self._seat(slot).short_model
        if model and not model.startswith("gemini-"):
            config["customModelsConfig"] = {"customModels": {model: {"modelName": model}}}
        return json.dumps(config, indent=1)

    @staticmethod
    def _agy_mcp(env: BaseEnvironment, board_token: str | None) -> str:
        """agy's mcp_config.json, with the board token and hub headers written in."""
        servers = {}
        if board_token:
            servers["board"] = {"serverUrl": _root(env.team_board_url) + "/mcp",
                                "headers": {"Authorization": f"Bearer {board_token}", **hub_headers(env)}}
        return json.dumps({"mcpServers": servers}, indent=1)

    def _codex_config(self, env: BaseEnvironment, slot: str | None = None) -> str:
        """Codex reaches the proxy as a custom provider, since only providers and MCP servers
        take extra headers; env_http_headers reads the hub's access headers from agent.env."""
        pairs = ", ".join(f"{json.dumps(k)} = {json.dumps(v)}" for k, v in header_vars(env).items())
        header = f"env_http_headers = {{ {pairs} }}" if pairs else ""
        model = self._seat(slot).short_model
        lines = [f"model = {json.dumps(model)}"] if model else []
        lines += ['model_provider = "multi-agent-harness"', "",
                  "[model_providers.multi-agent-harness]", 'name = "multi-agent-harness proxy"',
                  f"base_url = {json.dumps(_root(env.team_proxy_url) + '/openai/v1')}",
                  'env_key = "OPENAI_API_KEY"', 'wire_api = "responses"', header, "",
                  "[mcp_servers.board]", f"url = {json.dumps(_root(env.team_board_url) + '/mcp')}",
                  'bearer_token_env_var = "MULTI_AGENT_HARNESS_BOARD_TOKEN"', header]
        if not self.orchestration.cli_subagents:
            # Codex's own multi-agent tools (spawn_agent, wait, ...) would run helpers inside this
            # sandbox, outside the board, and shadow a strategy's tools of the same name.
            lines += ["", "[features]", "multi_agent = false"]
        return "\n".join(lines) + "\n"

    @staticmethod
    def _git_credentials(env: BaseEnvironment, name: str) -> str:
        """A git-credential-store line for the team remote, as this agent."""
        u = urlparse(env.team_git_url)
        return f"{u.scheme}://{name}:{env.team_git_tokens[name]}@{u.netloc}\n"

    @staticmethod
    def _git_include(env: BaseEnvironment) -> str:
        """Git config adding the hub's access headers to requests for the team remote."""
        u = urlparse(env.team_git_url)
        return f'[http "{u.scheme}://{u.netloc}/"]\n' + "".join(
            f"\textraHeader = {k}: {v}\n" for k, v in hub_headers(env).items())

    async def _own(self, env: BaseEnvironment, name: str, root_cmd: str = "") -> None:
        """Create the sandbox's harness dirs as root. When the task runs its agent as a non-root user
        (task.toml [agent] user), the CLI runs as that user, so it gets SECRETS and LOGS, files
        uploaded there included. Claude Code's managed settings stay root's. `root_cmd` runs after,
        as root. Only before any agent code runs in the sandbox: once it has, the agent could have
        linked a root file into those dirs, and _bind hands files over through HANDOFF instead."""
        user = _task_user(env)
        managed = CLAUDE_MANAGED_DIR if self._seat(name).harness == "claude-code" else ""
        cmd = (f"set -e; mkdir -p {SECRETS}/codex {SECRETS}/claude {AGY_STATE} {GEMINI_HOME}/config/agents {LOGS} "
               f"{managed}")
        if user:
            cmd += (f"; chown -R {shlex.quote(user)} {SECRETS} {LOGS}; "
                    f"mkdir -p {HANDOFF}; chown 0:0 {HANDOFF}; chmod 711 {HANDOFF}")
        if root_cmd:
            cmd += f"; {root_cmd}"
        r = await env.service_exec(cmd, service=name, timeout_sec=60, user=_admin(env))
        if r.return_code != 0:
            raise RuntimeError(f"preparing {name}'s harness files failed ({r.return_code}): {(r.stderr or '')[-300:]}")

    async def _configure(self, env: BaseEnvironment, name: str) -> None:
        board_token, key = self._creds[name]
        harness = self._seat(name).harness
        await self._own(env, name)
        await _put(env, name, AGENT_ENV, self._agent_env(env, self.facing(name), board_token, key, name))
        await _put(env, name, GIT_CREDENTIALS, self._git_credentials(env, self.facing(name)))
        await _put(env, name, GIT_INCLUDE, self._git_include(env))
        clone = clone_script(self._workdir(env), env.team_git_url)
        # The token-bearing $VARS expand in the sandbox shell; the command string holds no secret.
        # The gateway check proves the whole path with the headers: exposed URL, hub, gateway.
        hdrs = "".join(f'-H "{k}: ${var}" ' for k, var in header_vars(env).items())
        cmd = (f"set -e; umask 077; chmod 700 {SECRETS}; chmod 600 {AGENT_ENV} {GIT_CREDENTIALS} "
               f"{GIT_INCLUDE}; git config --global credential.helper 'store --file={GIT_CREDENTIALS}'; "
               f"git config --global include.path {GIT_INCLUDE}; "
               f"git config --global --add safe.directory '*'; "
               f"mkdir -p {LOGS}; {PATH_HEAD}. {AGENT_ENV}; "
               f'curl -fsS -m 30 -o /dev/null {hdrs}'
               f'"$MULTI_AGENT_HARNESS_BOARD_URL/healthz"; ({clone}); ')
        if harness == "claude-code":
            await _put(env, name, CLAUDE_MANAGED, self._claude_settings())
            cmd += 'mkdir -p "$CLAUDE_CONFIG_DIR"; ' + (self._claude_mcp_add(env) if board_token else "true")
        elif harness == "antigravity":
            await _put(env, name, f"{AGY_STATE}/settings.json", self._agy_settings(name))
            await _put(env, name, AGY_MCP, self._agy_mcp(env, board_token))
            if not self.orchestration.cli_subagents:
                await _put(env, name, f"{GEMINI_HOME}/config/agents/{AGY_AGENT}.md", AGY_AGENT_FILE)
            if hub_headers(env):
                await _put(env, name, AGY_FORWARD, (Path(__file__).parent / "agy_forward.py").read_text())
                cmd += ('command -v python3 >/dev/null || { echo "antigravity needs python3 for the hub '
                        'header forwarder" >&2; exit 1; }; ')
            cmd += (f'chmod -R go-rwx {GEMINI_HOME}; rm -rf "$HOME/.gemini"; ln -s {GEMINI_HOME} "$HOME/.gemini"')
        else:
            await _put(env, name, f"{SECRETS}/codex/config.toml", self._codex_config(env, name))
            await _put(env, name, f"{SECRETS}/codex/auth.json", json.dumps({"OPENAI_API_KEY": key}))
            cmd += 'chmod 600 "$CODEX_HOME/config.toml" "$CODEX_HOME/auth.json"'
        await self._own(env, name, f"chown 0:0 {CLAUDE_MANAGED}; chmod 644 {CLAUDE_MANAGED}"
                        if harness == "claude-code" else "")
        result = await env.service_exec(cmd, service=name, timeout_sec=120)
        if result.return_code != 0:
            # The end of stderr holds the error: Claude Code prints its own source lines before the message.
            err = (result.stderr or "").strip()
            hint = (f" Bubblewrap cannot run in this sandbox; set {CLAUDE_SCRUB_VAR}=0 to run Claude Code without "
                    "subprocess token scrubbing." if "bubblewrap is required" in err else "")
            raise RuntimeError(f"configuring {name} failed ({result.return_code}): {err[-600:]}{hint}")

    def _claude_settings(self) -> str:
        """Claude Code's managed settings. Deny rules hold under bypassPermissions (checked before the
        mode): Read and Edit, which also bind Grep, Glob and Write, can't reach the token-bearing files,
        any process's /proc/<pid>/environ (the CLI's own env) or these settings. On the subscription token Bash runs in Claude
        Code's scrub sandbox (own PID namespace); there agent.env is hidden too and Bash can't leave it."""
        paths = [AGENT_ENV, GIT_CREDENTIALS, GIT_INCLUDE, "/proc/*/environ"]  # environ only: agents read /proc/cpuinfo etc.
        deny = [f"{tool}(/{p})" for p in paths for tool in ("Read", "Edit")] + [f"Edit(/{CLAUDE_MANAGED_DIR}/**)"]
        settings: dict[str, Any] = {"permissions": {"deny": deny}}
        if self._claude_sub and self._claude_scrub:  # without the scrub sandbox Bash isn't sandboxed at all
            settings["sandbox"] = {"allowUnsandboxedCommands": False, "filesystem": {"denyRead": [AGENT_ENV]}}
        return json.dumps(settings, indent=1)

    @staticmethod
    def _claude_mcp_add(env: BaseEnvironment) -> str:
        return ('claude mcp add --scope user --transport http board "$MULTI_AGENT_HARNESS_BOARD_URL/mcp" '
                '--header "Authorization: Bearer $MULTI_AGENT_HARNESS_BOARD_TOKEN" '
                + "".join(f'--header "{k}: ${var}" ' for k, var in header_vars(env).items())
                + ">/dev/null")

    async def _bind(self, env: BaseEnvironment, slot: str, identity: str, board_token: str, fresh: bool) -> None:
        """Make a sandbox run as another board identity: its agent.env (and Claude Code's board
        config) take the new name and token. When the sandbox ran someone else before (`fresh`),
        that one's CLI sessions go (already copied to its own logs) and the workdir is reset to
        main exactly as pushed, so nothing carries over. Git and the proxy still know the sandbox
        by its own name."""
        harness = self._seat(slot).harness
        files = {AGENT_ENV: self._agent_env(env, identity, board_token, self._creds[slot][1], slot)}
        if harness == "antigravity":
            files[AGY_MCP] = self._agy_mcp(env, board_token)
        cmd = "set -e; umask 077; "
        if user := _task_user(env):
            # The previous identity's CLI ran here as the agent user, so root writes nothing under its dirs: the
            # files go to root's HANDOFF dir, are given to the agent user there, and the agent's own shell copies them.
            names = {path: f"{HANDOFF}/{slot}-{Path(path).name}" for path in files}
            for path, body in files.items():
                await _put(env, slot, names[path], body)
            r = await env.service_exec(f"set -e; chown {shlex.quote(user)} {' '.join(names.values())}; "
                                       f"chmod 600 {' '.join(names.values())}", service=slot, timeout_sec=60,
                                       user="root")
            if r.return_code != 0:
                raise RuntimeError(f"handing {slot}'s new identity files over failed ({r.return_code}): "
                                   f"{(r.stderr or '')[-300:]}")
            cmd += "".join(f"cat {src} > {path}; " for path, src in names.items())
        else:
            for path, body in files.items():
                await _put(env, slot, path, body)
        cmd += f"chmod 600 {AGENT_ENV}; {PATH_HEAD}. {AGENT_ENV}; "
        if fresh:
            cmd += {"claude-code": 'rm -rf "$CLAUDE_CONFIG_DIR/projects"; ',
                    "antigravity": f"rm -rf {AGY_STATE}/conversations {AGY_STATE}/brain; "}.get(
                        harness, 'rm -rf "$CODEX_HOME/sessions"; ') + f"({reset_script(self.workdir)}); "
        if harness == "claude-code":
            cmd += "claude mcp remove --scope user board >/dev/null 2>&1 || true; " + self._claude_mcp_add(env)
        elif harness == "antigravity":
            cmd += f"chmod 600 {AGY_MCP}; "
        result = await env.service_exec(cmd + "true", service=slot, timeout_sec=120)
        if result.return_code != 0:
            raise RuntimeError(f"binding {slot} to {identity} failed ({result.return_code}): "
                               f"{(result.stderr or '')[:300]}")

    # -- run --------------------------------------------------------------

    @staticmethod
    def _workdir(env: BaseEnvironment) -> str:
        return getattr(env, "team_workdir", None) or WORKDIR

    def facing(self, slot: str) -> str:
        """The name agents know sandbox `slot` by."""
        return self.names.get(slot, slot)

    def ordered(self, names, viewer: str) -> list[str]:
        """Agent-facing names in the order sandbox `viewer` sees them (see names.py)."""
        return naming.ordered(names, self.name_seed, self.facing(viewer), self.name_mode)

    def preamble(self, name: str) -> str:
        others = ", ".join(self.ordered((self.facing(a) for a in self.agents if a != name), name)) or "none"
        return PREAMBLE.format(name=self.facing(name), n=len(self.agents), others=others, workdir=self.workdir)

    def _resource_prompt(self, slot: str, prompt: str) -> str:
        """Resource facts for a new identity/session in a GPU-enabled team."""
        if not any(r["gpus"] for r in self.agent_resources.values()):
            return prompt
        resources = self.agent_resources[slot]
        devices = resources.get("observed_gpus") or []
        own = ("one GPU" + (f" ({devices[0]['name']}, {devices[0]['memory_mb']} MiB)" if devices else "")
               if resources["gpus"] else "no GPU")
        text = f"Sandbox resources: your sandbox has {own}. "
        if "multi_agent_harness.board.subagents" in self.orchestration.board_plugins:
            pool = [n for n in self.agents if self.roles[n] != LEAD and n not in getattr(self._env, "team_gone_agents", ())]
            count = sum(self.agent_resources[n]["gpus"] for n in pool)
            text += (f"The subagent pool has {count} GPU slot(s). The lead can use spawn_agent(instructions, gpu=True) "
                     "to require a GPU and agent_status(detail=True) to see current assignments. "
                     "Mixed pools reserve GPU slots; gpu=False can use a GPU only when the pool has no CPU slots. "
                     "Idle subagents retain their slots until terminated; terminate idle workers to serve queued work. ")
        else:
            peers = self.ordered([self.facing(n) for n, r in self.agent_resources.items()
                                  if r["gpus"] and n not in getattr(self._env, "team_gone_agents", ())], slot)
            text += f"GPU-equipped agents: {', '.join(peers) or 'none'}. "
        text += ("For GPU work on another sandbox, push the code and send its agent the commit/branch, command, "
                 "and expected outputs on the board. Files and processes remain local to each sandbox. "
                 "The verifier runs on CPU. GPU access is for task execution, not the coding model.")
        text += (" Before starting work, inspect your sandbox CPU, RAM, disk and GPU availability "
                 "(including cgroup CPU/memory limits) and share a brief summary on the board.")
        # Keep identity first and the task/instructions last, even for resume/wake prompts.
        first, sep, rest = prompt.partition("\n")
        return first + "\n\n" + text + "\n" + (sep + rest if sep else "")

    def _command(self, workdir: str = WORKDIR, logs: str = LOGS, session: int = 1, resume: bool = False,
                 slot: str | None = None) -> str:
        """The launch command in sandbox `slot`, with its harness and model. Run k of an identity
        writes its CLI output to codex.txt (codex.k.txt after the first), the final message to
        last-message.txt and, on run 1, the CLI's session id to session-id, which resume=True
        continues (codex exec resume, claude --resume, agy --conversation)."""
        seat = self._seat(slot)
        limit = f"timeout -k {KILL_GRACE_SEC} {max(1, int(self.timeout_sec))} " if self.timeout_sec else ""
        model = f"--model {shlex.quote(seat.short_model)} " if seat.short_model else ""
        eff = getattr(seat, "effort", None)
        if eff and seat.harness == "claude-code":
            model += f"--effort {eff} "
        elif eff and seat.harness == "codex":
            model += f"-c model_reasoning_effort={eff} "
        w = shlex.quote(workdir)
        last, sid = f"{logs}/last-message.txt", f"{logs}/session-id"
        head = f". {AGENT_ENV}; {PATH_HEAD}mkdir -p {logs} {w}; cd {w}; rm -f {last}{'' if resume else ' ' + sid}; "
        # The CLI runs in the background so stop() can signal it by pid and this command still
        # copies its sessions afterwards.
        bg = f" & echo $! > {CLI_PID}; wait $!; "
        n = "" if session == 1 else f".{session}"
        if seat.harness == "claude-code":
            out = f"{logs}/claude-code{n}.txt"
            res = f'--resume "$(cat {sid})" ' if resume else ""
            if not self.orchestration.cli_subagents:  # Claude Code's own subagents, as for Codex
                res += "--disallowedTools Agent,Task "
            keep = "" if resume else f"""grep -m1 -o '"session_id":"[^"]*"' {out} | cut -d'"' -f4 > {sid}; """
            return head + (
                f'printf "%s" "$MULTI_AGENT_HARNESS_PROMPT" | {limit}claude {res}--verbose --output-format=stream-json '
                f"--permission-mode=bypassPermissions {model}--print > {out} 2>&1{bg}"
                f"""rc=$?; grep '"type":"result"' {out} | tail -n 1 > {last}; {keep}mkdir -p {logs}/sessions; """
                f'cp -R "$CLAUDE_CONFIG_DIR/projects" {logs}/sessions/ 2>/dev/null; exit $rc')
        if seat.harness == "antigravity":
            # Names as Harbor's antigravity-cli parser reads them. stderr is agy's log, kept apart
            # so it can't split a stream-json line.
            out, err = f"{logs}/antigravity-stream{n}.jsonl", f"{logs}/antigravity-cli{n}.err"
            res = f'--conversation "$(cat {sid})" ' if resume else ""
            if not self.orchestration.cli_subagents:  # agy's own subagents, as for Codex
                res += f"--agent {AGY_AGENT} "
            keep = "" if resume else f"""grep -m1 -o '"conversation_id":"[^"]*"' {out} | cut -d'"' -f4 > {sid}; """
            fwd = stop = ""
            if self._forwarding:  # agy's model calls go out through the hub header forwarder
                fp = AGY_FORWARD_PORT
                fwd = (f"rm -f {fp}; python3 {AGY_FORWARD} {fp} 2>> {err} & fwd=$!; i=0; "
                       f'while [ ! -s {fp} ] && [ $i -lt 100 ]; do sleep 0.1; i=$((i+1)); done; '
                       f'if [ ! -s {fp} ]; then echo "hub header forwarder did not start" >> {err}; exit 1; fi; '
                       f'export GOOGLE_GEMINI_BASE_URL="http://127.0.0.1:$(cat {fp})"; ')
                stop = 'kill $fwd 2>/dev/null; '
            return head + fwd + (
                f'{limit}agy --prompt="$MULTI_AGENT_HARNESS_PROMPT" --output-format stream-json '
                f'--dangerously-skip-permissions --print-timeout 0s --add-dir "$PWD" {res}{model}'
                f"</dev/null > {out} 2> {err}{bg}"
                f"""rc=$?; {stop}grep '"event":"result"' {out} | tail -n 1 > {last}; {keep}"""
                f'c="$(cat {sid} 2>/dev/null)"; if [ -n "$c" ]; then mkdir -p {logs}/sessions; '
                f'cp -R {AGY_STATE}/brain/"$c" {logs}/sessions/ 2>/dev/null; '
                f'cp {AGY_STATE}/conversations/"$c".db* {logs}/sessions/ 2>/dev/null; '
                f'cp {AGY_STATE}/brain/"$c"/.system_generated/logs/transcript_full.jsonl '
                f"{logs}/antigravity-cli.trajectory.jsonl 2>/dev/null; fi; exit $rc")
        out = f"{logs}/codex{n}.txt"
        cli = "codex exec resume" if resume else "codex exec"
        args = f'-- "$(cat {sid})" "$MULTI_AGENT_HARNESS_PROMPT"' if resume else '-- "$MULTI_AGENT_HARNESS_PROMPT"'
        keep = "" if resume else f"""grep -m1 -o '"thread_id":"[^"]*"' {out} | cut -d'"' -f4 > {sid}; """
        return head + (
            f"{limit}{cli} --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check "
            f"{model}--json -o {last} {args} </dev/null > {out} 2>&1{bg}"
            f"rc=$?; {keep}rm -rf {logs}/sessions; "
            f'if [ -d "$CODEX_HOME/sessions" ]; then cp -R "$CODEX_HOME/sessions" {logs}/sessions; fi; '
            "exit $rc")

    async def _run_one(self, env: BaseEnvironment, name: str, prompt: str, who: str | None = None,
                       resume: bool = False, bind: tuple[str, bool] | None = None) -> int:
        who = who or name
        run = self.runs[who]
        try:
            if bind:
                await self._bind(env, name, who, *bind)
            timeout = int(self.timeout_sec) + 2 * KILL_GRACE_SEC if self.timeout_sec else None
            # The prompt is not secret; exec env is fine for it.
            command = self._command(self.workdir, run["logs"], run["sessions"], resume, slot=name)
            if not resume or run.get("resource_context_slot") != name:
                prompt = self._resource_prompt(name, prompt)
            result = await env.service_exec(command, service=name, timeout_sec=timeout,
                                            env={"MULTI_AGENT_HARNESS_PROMPT": prompt})
            if result.return_code == 0:
                run["resource_context_slot"] = name
        except BaseException as e:
            run["exit"] = type(e).__name__
            raise
        run["exit"] = result.return_code
        self.logger.info("%s exited %d", name if who == name else f"{who} ({name})", result.return_code)
        return result.return_code

    @property
    def running(self) -> set[str]:
        return {n for n, t in self._tasks.items() if not t.done()}

    def launch(self, name: str, prompt: str, *, identity: str | None = None, board_token: str | None = None,
               resume: bool = False, fallback: str | None = None) -> None:
        """Start a CLI in sandbox `name`; one at a time per sandbox. With `identity`, it runs as that
        board identity, logging to /logs/agent/<identity> (collected into <logs>/<identity>); a
        sandbox moving to a new identity needs that identity's `board_token` (see _bind). With
        resume=True the identity's last CLI session continues with `prompt`; with `fallback`, a
        resume that fails quickly is retried once as a fresh session with that prompt."""
        if name in self.running:
            raise RuntimeError(f"{name} is already running")
        who = identity or name
        holder = self.holder.get(name, name if name in self.on_board else None)
        bind = None
        if who != holder:
            if not board_token:
                raise ValueError(f"{name} runs as {holder}; binding it to {who} needs a board token")
            bind = (board_token, name in self.holder)
        if resume and not (self.runs.get(who, {}).get("sessions") or who in self._restored):
            raise ValueError(f"{who} has no session to resume")
        last = self.runs.get(who, {}).get("slot")
        if resume and last and self._seat(last).harness != self._seat(name).harness:
            raise ValueError(f"{who}'s session is {self._seat(last).harness}'s (in {last}); {name} runs "
                             f"{self._seat(name).harness}, which can't resume it")
        run = self.runs.setdefault(who, {"slot": name, "logs": LOGS if identity is None else f"{LOGS}/{identity}",
                                         "sessions": 0, "exit": None, "stopped": False})
        run["slot"] = name
        run["sessions"] += 1
        self.holder[name] = who
        self.exits.pop(name, None)
        self.lost.pop(name, None)
        self._launched[name] = time.monotonic()
        coro = (self._resume_or_fresh(self._env, name, prompt, fallback, who, bind) if resume and fallback
                else self._run_one(self._env, name, prompt, who, resume, bind))
        self._tasks[name] = asyncio.create_task(coro)

    async def _resume_or_fresh(self, env: BaseEnvironment, name: str, prompt: str, fresh: str, who: str,
                               bind: tuple[str, bool] | None) -> int:
        """Resume the restored CLI session; if the CLI errors or exits nonzero within
        RESUME_QUICK_SEC, start once more as a fresh session with `fresh`."""
        t0 = time.monotonic()
        rc = await self._run_one(env, name, prompt, who, True, bind)
        took = time.monotonic() - t0
        if rc == 0 or took >= ck.RESUME_QUICK_SEC or name in self.stopped:
            self.resume_outcome[name] = "resumed"
            self.logger.info("%s: resumed its %s session %s (exit %d after %.0fs)", name, self._seat(name).harness,
                             self._restored.get(name), rc, took)
            return rc
        self.resume_outcome[name] = f"fresh: resume exited {rc} after {took:.0f}s"
        self.logger.warning("%s: resuming its session failed (exit %d after %.0fs); starting a fresh session "
                            "with the resume note", name, rc, took)
        self.runs[who]["sessions"] += 1
        return await self._run_one(env, name, fresh, who, False)

    async def final_response(self, identity: str) -> str | None:
        """The last assistant message of the identity's latest CLI run, or None."""
        run = self.runs.get(identity)
        if run is None:
            return None
        fd, tmp = tempfile.mkstemp(prefix="multi-agent-harness-")
        os.close(fd)
        try:
            await self._env.service_download_file(f"{run['logs']}/last-message.txt", tmp, service=run["slot"])
            text = Path(tmp).read_text().strip()
            harness = self._seat(run["slot"]).harness
            if harness == "claude-code":
                text = json.loads(text).get("result") if text else ""
            elif harness == "antigravity":
                text = (json.loads(text).get("result") or {}).get("response") if text else ""
            return text or None
        except Exception as e:
            self.logger.warning("final response of %s unavailable: %s", identity, type(e).__name__)
            return None
        finally:
            os.unlink(tmp)

    async def board_admin(self, method: str, path: str, body: dict | None = None, secret: bool = False) -> Any:
        """Call a board admin route (path from /api), from the hub. With secret=True the response
        (which may hold tokens) comes back by file download, never through exec output."""
        out = f"{ADMIN}/admin-{secrets.token_hex(6)}.json" if secret else None
        try:
            return await self._hub_admin(self._env, "board", method, f"{BOARD_LOCAL}{path}", body, out=out)
        finally:
            if out:
                await self._env.exec(f"rm -f {out}", timeout_sec=30, user=_admin(self._env))

    async def stop(self, name: str) -> None:
        """TERM the agent's CLI (KILL after KILL_GRACE_SEC); its launch command then copies the
        sessions and returns, so logs and trajectories are collected as usual."""
        task = self._tasks.get(name)
        if task is None or task.done():
            return
        self.stopped.add(name)
        if name in self.holder:
            self.runs[self.holder[name]]["stopped"] = True
        g = KILL_GRACE_SEC
        script = (f'P=$(cat {CLI_PID} 2>/dev/null); [ -n "$P" ] && kill -0 "$P" 2>/dev/null || exit 0; '
                  f'kill -TERM "$P"; pkill -TERM -P "$P" 2>/dev/null; '
                  f'for i in $(seq {g}); do kill -0 "$P" 2>/dev/null || exit 0; sleep 1; done; '
                  f'kill -KILL -- "-$P" 2>/dev/null; pkill -KILL -P "$P" 2>/dev/null; kill -KILL "$P" 2>/dev/null; '
                  f'exit 0')
        try:
            await self._env.service_exec(script, service=name, timeout_sec=g + 30)
        except Exception as e:
            self.logger.warning("stopping %s failed: %s", name, type(e).__name__)
        try:
            await asyncio.wait_for(asyncio.shield(task), STOP_WAIT_SEC)
        except asyncio.TimeoutError:
            self.logger.warning("%s did not return within %ss of stop; abandoning it", name, STOP_WAIT_SEC)
            task.cancel()
        except Exception:
            pass
        self.logger.info("%s stopped", name)

    def _reap(self) -> None:
        for n, t in self._tasks.items():
            if t.done() and n not in self.exits:
                e = None if t.cancelled() else t.exception()
                self.exits[n] = "CancelledError" if t.cancelled() else (
                    t.result() if e is None else type(e).__name__)
                if e is not None:
                    self._lose(n, e)

    def _lose(self, name: str, e: BaseException) -> None:
        """An agent's run task ended with an exception (e.g. its sandbox vanished and the polls
        gave up): it counts as ended, and is logged, ledgered and listed as lost."""
        after = _duration(time.monotonic() - self._launched.get(name, time.monotonic()))
        self.lost[name] = f"{type(e).__name__} after {after}"
        self.logger.warning("%s lost: %s after %s (%s)", name, type(e).__name__, after, str(e)[:200])
        if hasattr(self._env, "_ledger"):
            self._env._ledger(event="agent_lost", name=name, error=type(e).__name__, after=after)
        self.orchestration.on_lost(self, name)

    async def sandbox_gone(self, name: str) -> bool:
        """Only provider-confirmed terminal state removes a physical slot."""
        from .sandbox import AGENT_GONE_STATES

        if name in getattr(self._env, "team_gone_agents", ()):
            return True
        member = getattr(self._env, "agents", {}).get(name)
        state = await member.status() if member else None
        if state not in AGENT_GONE_STATES:
            return False
        self._env.agent_gone(name, "agent_run", self.lost.get(name, "sandbox unavailable"), state=state)
        return True

    def _reminded(self, name: str) -> bool:
        """Whether `name` gets time reminders: the lead, or every agent when the run has no lead."""
        if not self.time_reminders or not self._original_timeout:
            return False
        has_lead = LEAD in self.roles.values()
        return self.roles.get(name) == LEAD if has_lead else True

    async def _time_reminders(self) -> None:
        budget = self._original_timeout
        if not self.time_reminders or not budget or budget <= 0:
            return
        elapsed = self._elapsed()
        if elapsed >= budget:
            return
        # Skip pre-resume slots and coalesce delayed ticks into one current reminder.
        slot = min(self.time_reminders, int(elapsed / budget * (self.time_reminders + 1)))
        previous = int(self._elapsed_base / budget * (self.time_reminders + 1))
        left = budget - elapsed
        for name in self.running:
            if not self._reminded(name) or slot <= self._reminder_slots.get(name, previous):
                continue
            text = (f"Time reminder: {_duration(left)} of the run remain. "
                    "Keep going while useful work remains: coordinate your teammates, verify the result, "
                    "and commit and push your work to main before time runs out.")
            try:
                await self.board_admin("POST", "/api/admin/notifications",
                                       {"to": self.facing(name), "text": text})
            except Exception as exc:
                self.logger.warning("time reminder delivery failed: %s", type(exc).__name__)
            else:
                self._reminder_slots[name] = slot

    async def _supervise(self) -> str:
        """Run until the strategy says the run is over or the team's time is up."""
        deadline = time.monotonic() + self.timeout_sec + 3 * KILL_GRACE_SEC if self.timeout_sec else None
        while True:
            self._reap()
            reason = self.orchestration.end_reason(self)
            if reason:
                return reason
            if deadline is not None and time.monotonic() >= deadline:
                self.logger.warning("team timed out after %ss", self.timeout_sec)
                return "timeout"
            await self._time_reminders()
            await self.orchestration.tick(self)
            wait = TICK_SEC if deadline is None else max(0.0, min(TICK_SEC, deadline - time.monotonic()))
            pending = [self._tasks[n] for n in self.running]
            if pending:
                await asyncio.wait(pending, timeout=wait, return_when=asyncio.FIRST_COMPLETED)
            else:
                await asyncio.sleep(wait)

    async def _lock_down(self, env: BaseEnvironment) -> str:
        """Confirm the agent-phase network policy from inside every agent sandbox, then write the
        agents' credentials. Fails closed: no verification, no tokens, no launch."""
        verify = getattr(env, "verify_agent_network", None)
        if verify is None:
            raise RuntimeError(f"{type(env).__name__} can't verify agent network policy; refusing to "
                               "write agent tokens")
        policy = await verify()
        if not self._creds:
            raise RuntimeError("agents were never registered; run setup() first")
        await asyncio.gather(*(self._configure(env, n) for n in self.agents))
        mode = getattr(getattr(policy, "network_mode", None), "value", str(policy))
        self.logger.info("agent network %s verified; configured %d agents", mode, len(self.agents))
        return mode

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        if not self.agents:
            self._plan(environment)
        self.on_board = self.on_board or set(self.orchestration.on_board(self.agents))
        self._network_mode = await self._lock_down(environment)
        self._env, self.workdir = environment, self._workdir(environment)
        if self._resume:
            await self._restore(environment)
        self._phase_t0 = time.monotonic()
        if self._checkpointing():
            environment.team_checkpoint_hook = self._final_checkpoint
            self._ckpt_task = asyncio.create_task(self._checkpoint_loop())
        if self.mark_plan:
            environment.team_marks = self.marks
            self._grade_queue = asyncio.Queue()
            for rec in self._regrade:
                self._grade_queue.put_nowait(rec)
            self._grade_task = asyncio.create_task(self._grade_worker())
            self._marks_task = asyncio.create_task(self._mark_loop())
        try:
            await self.orchestration.start(self)
            for n in self.agents:
                prompt = self.orchestration.prompt(self, n, instruction)
                if prompt is not None:
                    if self._reminded(n):
                        prompt = _after_identity(prompt, REMINDER_NOTE)
                    self._launch_resumed(n, prompt) if self._resume else self.launch(n, prompt)
            self.end_reason = await self._supervise()
            if self.running:
                self.logger.info("run over (%s); stopping %s", self.end_reason, sorted(self.running))
        finally:
            self.end_reason = self.end_reason or "interrupted"
            # Harbor's agent timeout cancels the run once, wherever it is, and then waits for it to return. A
            # cancel in the stops or the final checkpoint stops what it lands in; the gradings still queued finish
            # (_finish_marks), and the export of main and the collection run to the end through it
            # (_through_timeout). A second cancel (an impatient Ctrl-C) stops the gradings and whichever of those
            # it lands in. Whatever cancel came is raised again at the end.
            cancel: asyncio.CancelledError | None = None
            try:
                if self._ckpt_task is not None:
                    self._ckpt_task.cancel()
                    await asyncio.gather(self._ckpt_task, return_exceptions=True)
                await asyncio.gather(*(self.stop(n) for n in self.running), return_exceptions=True)
                self._reap()
            except asyncio.CancelledError as e:
                cancel = e
            # The verifier grades the workdir: main goes into it as soon as the agents are stopped, before the
            # slow steps (the gradings still queued, the final checkpoint) that Harbor's timeout can land in.
            try:
                await self._through_timeout("the export of main", self._export(environment))
            except asyncio.CancelledError as e:
                cancel = cancel or e
            try:
                me = asyncio.current_task()
                if me is None or me.cancelling() <= 1:  # a second cancel by now (in the stops) skips the gradings
                    await self._finish_marks()
            except asyncio.CancelledError as e:
                cancel = cancel or e
            finally:
                self._abandon_marks()  # a second cancel: no grading task outlives the run
            try:
                await self._final_checkpoint()
            except asyncio.CancelledError as e:
                cancel = cancel or e
            except Exception as e:  # noqa: BLE001 - collection must still run
                self.logger.warning("final checkpoint failed: %s", type(e).__name__)
            try:
                await self._through_timeout("collection", self._collect(environment, context))
            except asyncio.CancelledError as e:
                cancel = cancel or e
            if cancel is not None:
                raise cancel

    async def _through_timeout(self, what: str, coro) -> Any:
        """Run coro to the end even when Harbor's agent timeout cancels the run while it is in it: that cancel
        is held and raised once coro is done. A cancel beyond Harbor's one (task.cancelling() > 1: a second
        timeout, an impatient Ctrl-C) cancels coro and is raised at once."""
        task = asyncio.ensure_future(coro)
        me = asyncio.current_task()
        held: asyncio.CancelledError | None = None
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError as e:
                if task.cancelled() or me is None or me.cancelling() > 1:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise
                held = held or e
                self.logger.warning("Harbor's agent timeout landed in %s; finishing it before the verifier", what)
        if held is not None:
            raise held
        return result

    async def _export(self, env: BaseEnvironment) -> None:
        """Check main out into the hub's workdir for the verifier, plus the team bundle and board log for a
        separate verifier (TeamEnvironment.export_artifacts). A failure is logged, and the environment's
        backstop checks main out before the verifier."""
        if not hasattr(env, "export_artifacts"):
            return
        try:
            await env.export_artifacts()
        except Exception as e:  # noqa: BLE001
            self.logger.warning("artifact export failed: %s", e)

    # -- graded marks (marks.py) --------------------------------------------

    def _plan_marks(self, env: BaseEnvironment) -> None:
        """The marks this run reaches, and on a resume the marks the checkpoint already graded (kept, their
        files copied into this trial) or had not finished grading (graded again in this run)."""
        if not self.grade_at:
            return
        if not hasattr(env, "grade_mark"):
            raise ValueError(f"grade_at needs an environment that can grade a mark; {type(env).__name__} can't")
        if hasattr(env, "mark_verifier"):
            env.mark_verifier()  # a task whose verifier can't run on the hub fails here, before anything runs
        self.mark_plan = marks.schedule(self.grade_at, self._original_timeout)
        dropped = [marks.label(s) for s in self.grade_at if marks.label(s) not in {m["mark"] for m in self.mark_plan}]
        if dropped:
            self.logger.warning("grade_at: marks %s are past the end of the run (%ss) and are not graded",
                                dropped, self._original_timeout)
        self.logger.info("graded marks at %s%s", [m["mark"] for m in self.mark_plan],
                         "; the last one is the final verifier" if self.mark_plan and self.mark_plan[-1]["final"] else "")
        for old in (self._resume or {}).get("manifest", {}).get("marks") or []:
            if not isinstance(old, dict) or old.get("final") or not old.get("mark"):
                continue
            rec = dict(old, resumed=True)
            if rec.get("status") not in ("graded", "failed", "timeout"):
                if not rec.get("commit"):
                    continue
                rec.update(status="queued", regraded_after_resume=True, reward=None)
                self._regrade.append(rec)
            elif rec.get("dir") and Path(rec["dir"]).is_dir():
                dest = self._mark_dir(rec["mark"])
                try:
                    shutil.copytree(rec["dir"], dest, dirs_exist_ok=True)
                    rec["dir"] = str(dest)
                except OSError as e:
                    self.logger.warning("mark %s: copying its results from %s failed: %s", rec["mark"], rec["dir"], e)
            self.marks.append(rec)

    def _mark_dir(self, label: str) -> Path:
        """<trial>/marks/<label>, absolute (Harbor's trial paths can be relative to where it was started)."""
        return (self.logs_dir.parent / marks.MARKS_DIR / label).absolute()

    def next_mark(self) -> dict[str, Any] | None:
        """The next mark not yet reached, for strategies' notes: {mark, mark_s, words, in_s, final}."""
        now = self._elapsed()
        for m in self.mark_plan:
            if m["mark_s"] > now:
                return {**m, "words": marks.words(m["mark_s"]), "in_s": m["mark_s"] - now}
        return None

    def _mark_ledger(self, event: str, rec: dict[str, Any]) -> None:
        if hasattr(self._env, "_ledger"):
            self._env._ledger(event=event, **rec)

    async def _mark_loop(self) -> None:
        """Fire each mark at its time. The final one is the final verifier's, so it is not fired here."""
        done = {r["mark"] for r in self.marks}
        for m in self.mark_plan:
            if m["final"]:
                return
            if m["mark"] in done:
                continue
            wait = m["mark_s"] - self._elapsed()
            if wait > 0:
                await asyncio.sleep(wait)
            await self._fire_mark(m)

    async def _fire_mark(self, m: dict[str, Any]) -> None:
        """Pin main, queue the grading, checkpoint. The agents keep working throughout."""
        rec: dict[str, Any] = {"mark": m["mark"], "mark_s": m["mark_s"], "elapsed_s": round(self._elapsed(), 1),
                               "commit": None, "reward": None, "verifier_s": None, "status": "pinning", "final": False}
        self.marks.append(rec)
        try:
            rec["commit"] = await self._env.pin_main()
        except Exception as e:  # noqa: BLE001 - the run goes on
            rec.update(status="failed", error=getattr(self._env, "_redact", str)(f"pin main: {type(e).__name__}: {e}")[:300])
            self.logger.warning("mark %s: %s", rec["mark"], rec["error"])
            self._mark_ledger("mark_graded", rec)
            return
        rec["status"] = "queued"
        waiting = self._grade_queue.qsize() + (1 if any(r.get("status") == "grading" for r in self.marks) else 0)
        self.logger.info("mark %s at %s: main is %s%s", rec["mark"], _duration(rec["elapsed_s"]), rec["commit"][:12],
                         f"; grading waits for {waiting} earlier mark(s)" if waiting else "")
        self._grade_queue.put_nowait(rec)
        self._mark_ledger("mark", rec)
        if hasattr(self._env, "checkpoint_hub"):
            path = await self._checkpoint(f"mark {rec['mark']}", mark=rec["mark"])
            rec["checkpoint"] = str(path) if path else None
            self._update_mark_checkpoint(rec)  # when the grading already finished during the checkpoint

    def _update_mark_checkpoint(self, rec: dict[str, Any]) -> None:
        """A finished grading goes into the manifest of every checkpoint this run took that carries the mark, so
        a resume from any of them keeps the grade. Never a checkpoint of another run: on a resume, the source
        checkpoint's records are history (rec["checkpoint"] can name one; it is not written through)."""
        if rec.get("status") not in ("graded", "failed", "timeout"):
            return
        for path in self.checkpoints:
            try:
                ck.update_mark(Path(path), rec)
            except OSError as e:
                self.logger.warning("mark %s: updating the manifest of %s failed: %s", rec["mark"], path, e)

    async def _grade_worker(self) -> None:
        """One grading at a time, in mark order."""
        while True:
            rec = await self._grade_queue.get()
            try:
                await self._grade(rec)
            finally:
                self._grade_queue.task_done()

    async def _grade(self, rec: dict[str, Any]) -> None:
        rec["status"] = "grading"
        dest = self._mark_dir(rec["mark"])
        rec["dir"] = str(dest)
        t0 = time.monotonic()
        try:
            rec.update(await self._env.grade_mark(rec["mark"], rec["commit"], dest))
        except asyncio.CancelledError:
            rec.update(status="cancelled")
            raise
        except Exception as e:  # noqa: BLE001 - a failed grading is recorded and the run goes on
            rec.update(status="failed", error=getattr(self._env, "_redact", str)(f"{type(e).__name__}: {e}")[:300])
        if rec.get("verifier_s") is None:
            rec["verifier_s"] = round(time.monotonic() - t0, 1)
        (self.logger.info if rec["status"] == "graded" else self.logger.warning)(
            "mark %s (%s): %s, reward %s, %.0fs of grading%s", rec["mark"], rec["commit"][:12], rec["status"],
            rec.get("reward"), rec["verifier_s"], f": {rec['error']}" if rec.get("error") else "")
        self._mark_ledger("mark_graded", rec)
        self._update_mark_checkpoint(rec)

    async def _finish_marks(self) -> None:
        """At the end of the agent phase: no more marks fire, and the gradings already queued run to the end
        (one at a time, each within the verifier's timeout) so none overlaps the final verifier. That holds
        when Harbor's agent timeout cancelled the run too: a cancelled grading would leave its verifier running
        on the hub. A second cancel while they finish cancels them."""
        if self._marks_task is not None:
            self._marks_task.cancel()
            await asyncio.gather(self._marks_task, return_exceptions=True)
        if self._grade_task is None:
            return
        if self._grade_queue is not None:
            pending = [r["mark"] for r in self.marks if r.get("status") in ("queued", "grading")]
            if pending:
                self.logger.info("run over; finishing the gradings of marks %s before the final verifier", pending)
                if hasattr(self._env, "hold_hub_for_marks"):
                    await self._env.hold_hub_for_marks(len(pending))
            try:
                await self._grade_queue.join()
            except asyncio.CancelledError:
                self._grade_task.cancel()
                raise
        self._grade_task.cancel()
        await asyncio.gather(self._grade_task, return_exceptions=True)

    def _abandon_marks(self) -> None:
        """Cancel whatever of the marks' tasks is still running, and record the marks they left as cancelled
        (a resume from a later checkpoint grades them again)."""
        for t in (self._marks_task, self._grade_task):
            if t is not None and not t.done():
                t.cancel()
        for r in self.marks:
            if r.get("status") in ("queued", "grading", "pinning"):
                r["status"] = "cancelled"

    def _final_mark(self, env: BaseEnvironment) -> None:
        """The record of the final verifier, which grades main as the run ends: the last mark when the run
        reaches it, else "end". The environment fills in its reward after the verifier (finish_final_mark)."""
        if not self.mark_plan:
            return
        last, now = self.mark_plan[-1], self._elapsed()
        reached = last["final"] and now >= last["mark_s"] - marks.END_SLACK_SEC
        label = last["mark"] if reached else "end"
        rec = {"mark": label, "mark_s": last["mark_s"] if reached else None, "elapsed_s": round(now, 1),
               "commit": getattr(env, "team_verify_commit", None), "reward": None, "verifier_s": None,
               "status": "awaiting_final_verifier", "final": True,
               "dir": str(self._mark_dir(label))}
        if last["final"] and not reached:
            rec["ended_early"] = True
        self.marks.append(rec)
        self._mark_ledger("mark", rec)

    # -- checkpoint and resume (checkpoint.py) ----------------------------

    def _checkpointing(self) -> bool:
        return self.checkpoint_every_sec > 0 and hasattr(self._env, "checkpoint_hub")

    async def _checkpoint_loop(self) -> None:
        """Every checkpoint_every_sec while agents run. Agents are never paused; a tick that finds
        the last checkpoint still running is skipped."""
        while True:
            await asyncio.sleep(self.checkpoint_every_sec)
            if self._ckpt_lock.locked():
                self.logger.info("checkpoint still running; skipping this tick")
                continue
            await self._checkpoint("periodic")

    async def _final_checkpoint(self) -> None:
        """At the end of the agent phase (also on a stop or cancel), or before a hub delete if the
        run never got there. Once per run; a failed one is tried again by the next call."""
        if self._ckpt_final_done or not self._checkpointing():
            return
        self._ckpt_final_done = await self._checkpoint(self.end_reason or "before_delete", final=True) is not None

    def _status(self, name: str) -> str:
        if name in self.lost or name in getattr(self._env, "team_gone_agents", set()):
            return "lost"
        if name in self.running:
            return "running"
        return "finished" if name in self.exits or name in self.runs else "idle"

    def _known_secrets(self, env: BaseEnvironment) -> list[str]:
        values = [v for pair in self._creds.values() for v in pair if v]
        values += list(getattr(env, "_secrets", []) or []) + list(hub_headers(env).values())
        values += list((getattr(env, "team_git_tokens", None) or {}).values())
        return values + [os.environ.get(CLAUDE_TOKEN_VAR, "")]

    def _elapsed(self) -> float:
        return self._elapsed_base + (time.monotonic() - self._phase_t0 if self._phase_t0 else 0.0)

    async def _checkpoint(self, reason: str, final: bool = False, mark: str | None = None) -> Path | None:
        """One checkpoint into <job dir>.checkpoints/<trial>/<n>-<stamp>/. Never raises (except
        on cancellation); a failure or a disk under the floor is logged and skipped."""
        env = self._env
        root = ck.root_for(self.logs_dir.parent)
        redact = getattr(env, "_redact", lambda t: t)
        async with self._ckpt_lock:
            probe = next(p for p in (root, *root.parents) if p.exists())
            free = shutil.disk_usage(probe).free
            if free < ck.MIN_FREE_BYTES:
                self.logger.warning("checkpoint skipped: %d MB free on the host, under %d MB",
                                    free >> 20, ck.MIN_FREE_BYTES >> 20)
                if hasattr(env, "_ledger"):
                    env._ledger(event="checkpoint_skipped", reason="disk", free_bytes=free)
                return None
            n = ck.next_n(root)
            tmp = ck.begin(root, n)
            try:
                await env.checkpoint_hub(tmp)
                gone = getattr(env, "team_gone_agents", set())
                live = [a for a in self.agents if a in self.runs and a not in gone and a not in self.lost]
                results = await asyncio.gather(*(self._checkpoint_agent(env, a, tmp / "agents" / a) for a in live),
                                               return_exceptions=True)
                for r in results:
                    if isinstance(r, asyncio.CancelledError):
                        raise r
                sessions = {a: r if isinstance(r, str) else f"failed: {type(r).__name__}"
                            for a, r in zip(live, results)}
                dropped = await asyncio.to_thread(ck.drop_token_files, tmp)
                hits = await asyncio.to_thread(ck.redact_tree, tmp, self._known_secrets(env))
                if dropped or hits:
                    self.logger.warning("checkpoint %d: removed %d token files and redacted %d files", n, dropped, hits)
                path = ck.commit(tmp, root, n, self._manifest(n, reason, final, sessions, tmp, mark))
            except BaseException as e:
                shutil.rmtree(tmp, ignore_errors=True)
                if not isinstance(e, Exception):
                    raise
                msg = redact(f"{type(e).__name__}: {e}")[:300]
                self.logger.warning("checkpoint %d failed: %s", n, msg)
                if hasattr(env, "_ledger"):
                    env._ledger(event="checkpoint_failed", n=n, error=msg)
                return None
            self.checkpoints.append(str(path))
            try:
                if mark:
                    ck.link_mark(root, path, mark)
                pruned = ck.prune(root)
                size = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
                self.logger.info("checkpoint %d (%s) at %s: %.1f MB, sessions %s%s", n, reason, path,
                                 size / 2**20, sessions, f", pruned {len(pruned)}" if pruned else "")
                if hasattr(env, "_ledger"):
                    env._ledger(event="checkpoint", n=n, reason=reason, dir=str(path), bytes=size)
            except Exception as e:  # noqa: BLE001 - the checkpoint itself is in place
                self.logger.warning("checkpoint %d: after-steps failed: %s", n, type(e).__name__)
        return path

    async def _checkpoint_agent(self, env: BaseEnvironment, name: str, dest: Path) -> str:
        """Copy the agent's CLI session files (no token files, scrubbed in the sandbox) and its
        session id into dest. Returns what happened, for the manifest."""
        home, paths = SESSION_HOMES[self._seat(name).harness]
        out, key = SESSION_ID_FROM[self._seat(name).harness]
        s = CKPT_STAGE
        excl = " ".join(f"--exclude={shlex.quote(x)}" for x in (*ck.TOKEN_FILES, *SESSION_EXCLUDES))
        cap_kb = ck.SESSION_CAP_BYTES >> 10
        stage = (f"umask 077; rm -rf {s} {s}.tgz; mkdir -p {s}/session; "
                 f"if cd {home} 2>/dev/null; then p=; for x in {' '.join(paths)}; do "
                 f'[ -e "$x" ] && p="$p $x"; done; '
                 f'if [ -n "$p" ]; then kb=$(du -sck {excl} $p 2>/dev/null | tail -n1 | cut -f1); echo "${{kb:-0}}"; '
                 f'if [ "${{kb:-0}}" -le {cap_kb} ]; then tar -cf - {excl} $p 2>/dev/null | tar -xf - -C {s}/session; '
                 f"fi; fi; fi; "
                 f"if [ -s {LOGS}/session-id ]; then cp {LOGS}/session-id {s}/session-id; else "
                 f"""grep -m1 -o '"{key}":"[^"]*"' "$(ls -t {LOGS}/{out} 2>/dev/null | head -n1)" 2>/dev/null | cut -d'"' -f4 > {s}/session-id; """
                 f"[ -s {s}/session-id ] || rm -f {s}/session-id; fi; true")
        pack = (f'kb=$(du -sk {s} | cut -f1); echo "$kb"; '
                f'if [ "$kb" -le {cap_kb} ]; then tar -czf {s}.tgz -C {s} .; fi')
        try:
            r = await env.service_exec(stage, service=name, timeout_sec=300)
            if r.return_code != 0:
                raise RuntimeError(f"staging exited {r.return_code}")
            kb = _first_int(r.stdout)
            if kb <= cap_kb:
                await self._scrub_sandbox(env, name, root=s, names=(*SECRET_VARS, *header_vars(env).values()))
                r = await env.service_exec(pack, service=name, timeout_sec=300)
                kb = _first_int(r.stdout)
            if kb > cap_kb:
                self.logger.warning("checkpoint: %s session files are %d MB, over the %d MB cap; not copied",
                                    name, kb >> 10, cap_kb >> 10)
                return f"skipped: {kb >> 10} MB over the cap"
            if r.return_code != 0:
                raise RuntimeError(f"packing exited {r.return_code}")
            dest.mkdir(parents=True, exist_ok=True)
            tgz = dest.parent / f".{name}.tgz"
            try:
                await env.service_download_file(f"{s}.tgz", tgz, service=name)
                await asyncio.to_thread(_extract, tgz, dest)
            finally:
                tgz.unlink(missing_ok=True)
        finally:
            try:
                await env.service_exec(f"rm -rf {s} {s}.tgz", service=name, timeout_sec=60)
            except Exception:  # noqa: BLE001
                pass
        return "saved" if (dest / "session-id").is_file() else "saved, no session id"

    def _manifest(self, n: int, reason: str, final: bool, sessions: dict[str, str], tmp: Path,
                  mark: str | None = None) -> dict[str, Any]:
        from .resources import gpu_plan
        env = self._env
        r = self._resume
        return {
            "format": ck.FORMAT, "n": n, "reason": reason, "final": final,
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "task": getattr(env, "environment_name", None), "harness": self.harness, "model": self.model_name,
            "n_agents": len(self.agents), "orchestration": self.orchestration_name,
            "gpu_allocation": getattr(env, "team_gpu_plan", gpu_plan(len(self.agents))),
            "agent_resources": self.agent_resources,
            "lead": next((a for a, role in self.roles.items() if role == LEAD), None),
            "agent_timeout_sec": self._original_timeout, "elapsed_agent_sec": round(self._elapsed(), 1),
            "spend_usd": ck.spend(tmp / "proxy-usage.jsonl"),
            "agents": {a: {"status": self._status(a), "session": sessions.get(a, "none"),
                           "harness": self._seat(a).harness, "model": self._seat(a).model} for a in self.agents},
            "agent_names": dict(self.names), "name_seed": self.name_seed,
            "resumed_from": {"path": str(r["path"]), "n": r["manifest"].get("n")} if r else None,
            **({"mark": mark} if mark else {}),
            **({"hub_state": env.team_hub_state_saved} if getattr(env, "team_hub_state_saved", None) else {}),
            **({"grade_at": [m["mark"] for m in self.mark_plan], "marks": [dict(x) for x in self.marks]}
               if self.mark_plan else {}),
        }

    async def _restore(self, env: BaseEnvironment) -> None:
        """Into every agent sandbox: the checkpoint's board log at RESUME_BOARD and, where the
        checkpoint has one, the agent's CLI session files and session id. An agent whose restore
        fails starts a fresh session with the resume note."""
        gone = getattr(env, "team_gone_agents", set())
        names = [a for a in self.agents if a not in gone]
        results = await asyncio.gather(*(self._restore_agent(env, a) for a in names), return_exceptions=True)
        for a, r in zip(names, results):
            if isinstance(r, asyncio.CancelledError):
                raise r
            if isinstance(r, BaseException):
                self._restored.pop(a, None)
                self.logger.warning("%s: restoring from the checkpoint failed (%s); it starts fresh",
                                    a, getattr(env, "_redact", str)(str(r))[:300])

    async def _restore_agent(self, env: BaseEnvironment, name: str) -> None:
        ckdir: Path = self._resume["path"]
        board = ckdir / "board-events.jsonl"
        r = await env.service_exec(f"mkdir -p {RESUME_BOARD_DIR} && chmod 755 {RESUME_BOARD_DIR}",
                                   service=name, timeout_sec=60)
        if r.return_code != 0:
            raise RuntimeError(f"{name}: creating {RESUME_BOARD_DIR} failed ({r.return_code})")
        if board.is_file():
            await env.service_upload_file(board, RESUME_BOARD, service=name)
        src = ckdir / "agents" / name
        sid = (src / "session-id").read_text().strip() if (src / "session-id").is_file() else ""
        if not sid or not (src / "session").is_dir():
            self.logger.info("%s: no saved CLI session in the checkpoint", name)
        else:
            home = SESSION_HOMES[self._seat(name).harness][0]
            fd, tmp = tempfile.mkstemp(prefix="multi-agent-harness-", suffix=".tar")
            os.close(fd)
            try:
                with tarfile.open(tmp, "w") as t:
                    t.add(src / "session", arcname=".")
                await env.service_upload_file(tmp, f"{CKPT_STAGE}-restore.tar", service=name)
            finally:
                os.unlink(tmp)
            cmd = (f"set -e; umask 077; mkdir -p {home}; tar -xf {CKPT_STAGE}-restore.tar -C {home} --no-same-owner; "
                   f"chmod -R go-rwx {SECRETS}")
            try:
                r = await env.service_exec(cmd, service=name, timeout_sec=300)
            finally:
                # The upload is root's, and /tmp lets only its owner delete it.
                await env.service_exec(f"rm -f {CKPT_STAGE}-restore.tar", service=name, timeout_sec=60,
                                       user=_admin(env))
            if r.return_code != 0:
                raise RuntimeError(f"{name}: restoring its session files failed ({r.return_code}): "
                                   f"{(r.stderr or '')[:300]}")
            await _put(env, name, f"{LOGS}/session-id", sid + "\n")
            self._restored[name] = sid
        # No agent code has run in this sandbox yet, so _own may hand the restored files to the agent user.
        await self._own(env, name, f"chmod 644 {RESUME_BOARD} 2>/dev/null || true")

    def _launch_resumed(self, name: str, prompt: str) -> None:
        """Launch an agent of a resumed run: its restored CLI session continues with the resume
        note, or (no session, or the resume fails) a fresh one gets the note after its preamble."""
        m = self._resume["manifest"]
        subs = (ck.spawned(self._resume["path"] / "board-events.jsonl")
                if self.roles.get(name) == LEAD and self.orchestration.identity_role else None)
        note = ck.note(m, RESUME_BOARD, self.timeout_sec, subs)
        if self._reminded(name):
            note += " Check inbox() regularly for the harness time-remaining reminders."
        if nxt := self.next_mark():
            what = "The final grading of main" if nxt["final"] else "The next grading of main"
            note += f" {what} is at {nxt['words']} of the run, in {_duration(nxt['in_s'])}."
        fresh = (prompt.replace("\nTask:\n", f"\n{note}\n\nTask:\n", 1) if "\nTask:\n" in prompt
                 else f"{note}\n\n{prompt}")
        if name not in self._restored:
            self.resume_outcome[name] = "fresh: no saved session"
            self.logger.info("%s: no session to resume; starting a fresh session with the resume note", name)
            self.launch(name, fresh)
            return
        self.logger.info("%s: resuming its %s session %s", name, self._seat(name).harness, self._restored[name])
        self.launch(name, f"{note}\n\nContinue the task from where you left off.", resume=True, fallback=fresh)

    # -- collect ----------------------------------------------------------

    async def _download_agent(self, env: BaseEnvironment, name: str, slot: str, source: str = LOGS) -> None:
        try:
            await env.service_download_dir(source, self.logs_dir / name, service=slot)
        except Exception as e:
            self.logger.warning("download from %s failed: %s", name, type(e).__name__)

    async def _download_hub(self, env: BaseEnvironment, source: str, target: str) -> None:
        try:
            await env.download_file(source, self.logs_dir / target)
        except Exception as e:
            self.logger.warning("download of %s failed: %s", target, type(e).__name__)

    async def _scrub_sandbox(self, env: BaseEnvironment, slot: str, root: str = LOGS,
                             names: tuple[str, ...] = (CLAUDE_TOKEN_VAR,)) -> None:
        """Replace the values of agent.env's `names` (by default the subscription token) in every
        file under `root` (by default the sandbox's /logs) before download. The values expand from
        agent.env inside the sandbox; the command holds none."""
        vals = " ".join(f'"${n}"' for n in names)
        cmd = (f'. {AGENT_ENV}; for t in {vals}; do [ -n "$t" ] || continue; '
               f'grep -rlF -e "$t" {root} 2>/dev/null | while IFS= read -r f; do '
               f'sed -i "s|$t|{REDACTED}|g" "$f"; done; done; exit 0')
        try:
            await env.service_exec(cmd, service=slot, timeout_sec=120)
        except Exception as e:
            self.logger.warning("token scrub in %s failed: %s", slot, type(e).__name__)

    def _scrub_run_dir(self) -> int:
        """Backstop on the host: redact the token from anything under the trial agent dir and
        return how many files had it. Never logs the token."""
        tok = os.environ.get(CLAUDE_TOKEN_VAR, "").encode()
        if not tok:
            return 0
        hits = 0
        for path in self.logs_dir.rglob("*"):
            if path.is_file() and not path.is_symlink():
                data = path.read_bytes()
                if tok in data:
                    path.write_bytes(data.replace(tok, REDACTED.encode()))
                    hits += 1
        return hits

    def _subscription_usage(self, names) -> dict[str, Any]:
        """API-equivalent usage from these agents' Claude Code transcripts (priced from pricing.json),
        shaped like the proxy's /admin/usage. The total counts each message once across agents."""
        total = transcript_usage([self.logs_dir / n for n in names])
        return {"billed_as": CLAUDE_SUB, "source": "claude-code transcripts", "budget_enforced": False,
                "total": total, "agents": {n: transcript_usage(self.logs_dir / n) for n in names}}

    def _parse_agent(self, name: str) -> AgentContext:
        """Convert one agent's native session into trajectory.json with Harbor's own parser for the
        harness of the sandbox it ran in."""
        seat = self._seat(self.runs.get(name, {}).get("slot", name))
        ctx = AgentContext()
        try:
            self._installers[seat.harness](logs_dir=self.logs_dir / name,
                                           model_name=seat.model).populate_context_post_run(ctx)
        except Exception as e:
            self.logger.warning("trajectory for %s failed: %s", name, type(e).__name__)
        return ctx

    async def _collect(self, env: BaseEnvironment, context: AgentContext) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        if hasattr(env, "save_team_work"):  # the team repo into the trial dir, whatever the export does
            await env.save_team_work()
        if hasattr(env, "size_hub_for_verifier"):  # --ek hub_cpus/hub_memory_mb: the verifier's size
            await env.size_hub_for_verifier()
        if hasattr(env, "team_final_mark_t0"):  # the final verifier can start once collection is done
            env.team_final_mark_t0 = time.time()
        self._final_mark(env)
        # Each sandbox's own logs, plus each identity a strategy bound to a sandbox, into its own dir.
        targets = {n: (n, LOGS) for n in self.agents if n in self.on_board}
        targets |= {who: (r["slot"], r["logs"]) for who, r in self.runs.items() if r["logs"] != LOGS}
        gone = getattr(env, "team_gone_agents", set())
        if gone:
            self.logger.warning("not collecting logs from gone sandboxes %s", sorted(gone))
        # Who ran Claude Code on the runner's subscription token: everyone, or in a mixed team some.
        claude = [who for who, (slot, _) in targets.items() if self._seat(slot).harness == "claude-code"]
        mixed = self.harnesses != {"claude-code"}
        if self._claude_sub:
            await asyncio.gather(*(self._scrub_sandbox(env, n) for n in self.agents
                                   if n not in gone and self._seat(n).harness == "claude-code"))
        await asyncio.gather(
            *(self._download_agent(env, who, slot, src) for who, (slot, src) in targets.items()
              if slot not in gone),
            self._download_hub(env, getattr(env, "team_board_log_path", BOARD_LOG), "board-events.jsonl"),
            self._download_hub(env, getattr(env, "team_proxy_log_path", PROXY_LOG), "proxy-usage.jsonl"))
        usage: Any = None
        try:
            usage = await self._hub_admin(env, "proxy", "GET", f"{PROXY_LOCAL}/admin/usage")
            (self.logs_dir / "proxy-usage.json").write_text(json.dumps(usage, indent=2))
        except Exception as e:
            self.logger.warning("proxy usage fetch failed: %s", type(e).__name__)

        sub: dict[str, Any] | None = None
        if self._claude_sub:
            hits = self._scrub_run_dir()
            (self.logger.warning if hits else self.logger.info)(
                "%s token found in %d downloaded files (redacted)", CLAUDE_TOKEN_VAR, hits)
            sub = self._subscription_usage(claude)
            (self.logs_dir / "subscription-usage.json").write_text(json.dumps(sub, indent=2))
            if not mixed:
                usage = sub  # the run's usage summary: the proxy saw none of these calls
            elif usage:  # the proxy saw the other agents' calls, and only those
                usage = _usage_sum(usage, sub)
            if sub["total"]["unpriced"]:
                self.logger.warning("models not in pricing.json, tokens counted but not priced: %s",
                                    sub["total"]["unpriced"])

        per_agent = {n: self._parse_agent(n) for n in targets}
        summed = {f: sum(getattr(c, f) or 0 for c in per_agent.values())
                  for f in ("cost_usd", "n_input_tokens", "n_output_tokens", "n_cache_tokens")}
        proxy = _proxy_totals(usage)
        context.cost_usd = proxy.get("cost_usd", summed["cost_usd"])
        context.n_input_tokens = int(proxy.get("input_tokens", summed["n_input_tokens"]))
        context.n_output_tokens = int(proxy.get("output_tokens", summed["n_output_tokens"]))
        context.n_cache_tokens = int(proxy.get("cache_tokens", summed["n_cache_tokens"]))

        lead = next((a for a, r in self.roles.items() if r == "lead"), None)
        record = self.orchestration.record(self)
        resumed = ({"path": str(self._resume["path"]), "n": self._resume["manifest"].get("n")}
                   if self._resume else None)
        if hasattr(env, "_ledger"):
            env._ledger(orchestration=self.orchestration_name, lead=lead, end_reason=self.end_reason,
                        lost_agents=sorted(self.lost), **({"resumed_from": resumed} if resumed else {}),
                        **({"checkpoints": len(self.checkpoints)} if self.checkpoints else {}),
                        **record.get("ledger", {}))
        context.metadata = {
            "harness": self.harness,
            "gpu_allocation": getattr(env, "team_gpu_plan", None),
            "agent_resources": self.agent_resources,
            "agent_names": dict(self.names),
            "agent_names_mode": self.name_mode,
            "verifier_gpus": 0,
            "orchestration": self.orchestration_name,
            "lead": lead,
            "end_reason": self.end_reason,
            "timed_out": self.end_reason == "timeout",
            "lost_agents": sorted(self.lost),
            "lost": dict(self.lost),
            "network_mode": getattr(self, "_network_mode", None),
            "model_upstream": getattr(env, "team_model_upstream", None),
            "workdir": self._workdir(env),
            "workdir_source": getattr(env, "team_workdir_source", None),
            "install_sec": dict(self.install_sec),
            "hub_runtime_sec": getattr(env, "team_hub_runtime_sec", None),
            **({"resumed_from": resumed, "resume": dict(self.resume_outcome),
                "restored_usd": self._resume["manifest"].get("spend_usd")} if resumed else {}),
            **({"checkpoints": list(self.checkpoints)} if self.checkpoints else {}),
            # The same list the environment completes after the final verifier, before result.json is written.
            **({"grade_at": [m["mark"] for m in self.mark_plan], "marks": self.marks} if self.mark_plan else {}),
            "cost_source": ("proxy" if not sub else "proxy+transcripts" if mixed else "transcripts")
                           if "cost_usd" in proxy else "trajectories",
            **({"billed_as": "mixed" if mixed else CLAUDE_SUB, "budget_enforced": False} if sub else {}),
            "agents": {n: self._agent_record(n) | ({
                           "billed_as": CLAUDE_SUB, "cost_usd": sub["agents"][n]["usd"],
                           "n_input_tokens": sub["agents"][n]["tokens"]["input"],
                           "n_output_tokens": sub["agents"][n]["tokens"]["output"]} if sub and n in sub["agents"] else {
                           **({"billed_as": "proxy"} if sub else {}), "cost_usd": c.cost_usd,
                           "n_input_tokens": c.n_input_tokens, "n_output_tokens": c.n_output_tokens})
                       for n, c in per_agent.items()},
            **record.get("metadata", {}),
        }

    def _agent_record(self, name: str) -> dict[str, Any]:
        run = self.runs.get(name, {})
        resources = self.agent_resources.get(run.get("slot", name), {})
        seat = self._seat(run.get("slot", name))
        cli = {"harness": seat.harness, "model": seat.model}
        if run.get("logs", LOGS) == LOGS:  # a sandbox under its own name
            return {"role": self.roles.get(name), **cli, "resources": resources, "exit": self.exits.get(name),
                    "stopped": name in self.stopped, "sessions": run.get("sessions", 0)}
        return {"role": self.orchestration.identity_role, **cli, "resources": resources, "sandbox": run.get("slot"),
                "exit": run.get("exit"), "stopped": run.get("stopped", False), "sessions": run.get("sessions", 0)}
