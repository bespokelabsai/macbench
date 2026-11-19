"""Harbor environment: a hub sandbox plus N agent sandboxes from the Universal Sandbox Interface (bespokelabs-sandbox).

    harbor run ... -e multi_agent_harness:TeamEnvironment --ek backend=daytona

Each sandbox is a SandboxMember (sandbox.py), built from the task's environment/Dockerfile. The
hub is Harbor's main service (a shared verifier runs there); agents are services
``agent-1..N``. Daytona is the tested backend.

The hub installs its runtime into /opt/multi-agent-harness at start (hubruntime.py) and serves the board, the
model proxy and the team git remote behind one gateway port (multi_agent_harness.gateway), which the SDK's
expose_port() turns into a URL plus the headers every request must carry. On Daytona that is
the preview URL https://<port>-<hub id>.<proxy domain> and X-Daytona-Preview-Token; the hub stays
private, so agents send that header on top of their own bearer token.

Network follows Harbor's per-phase contract: sandboxes start under the [environment] baseline
and Harbor calls set_network_policy() around the agent and verifier phases. Agents get the phase
policy plus the hub's host as an extra allowed host (Harbor's merge_extra_allowlists), so
no-network becomes a domain allow-list of that one host. The hub gets the phase policy plus the
hosts of the model upstream the proxy calls (upstream.py: direct, subscription or a gateway
URL). Claude Code on a subscription token is the exception: its agents call Anthropic themselves,
so they get Anthropic's API host too and the hub gets no model hosts. In a mixed team (--ak team,
roster.py) only the Claude Code agents get that host, and the hub keeps the Codex logins' hosts.
Policies map onto the SDK's NetworkPolicy, and every switch is then checked from inside the
sandboxes and fails closed.

Secrets (API keys, subscription logins, admin tokens, git tokens, the hub's access headers) reach
the hub as root-only files uploaded through the filesystem API, never in a command string, sandbox
env or log line.

The task workdir (multi_agent_harness/workdir.py) seeds the team repo at start: the hub's copy of the image's
starting files becomes the first commit on main, and each agent's workdir is a clone of it. After
the agents, export_artifacts() checks main out into the hub's workdir as a clean tree, so a shared
verifier and Harbor's artifact collection see the team's result and nothing else.

Every sandbox gets the task's [environment] size by default. `--ek hub_cpus=N --ek hub_memory_mb=M`
give the hub a size of its own, for a shared verifier that needs more than an agent does. By default
(hub_size_at=verify) the hub is created at the task's size and grown to N CPUs and M MB right
before the verifier (size_hub_for_verifier: `docker update` on Docker, a hot resize on Daytona, which
only grows); hub_size_at=start creates it at that size instead.

In Harbor's separate verifier mode, Harbor builds the verifier's environment with this same
class. That one is a single plain sandbox, so construction returns a SandboxMember instead.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import math
import json
import os
import secrets
import shlex
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from harbor.environments.base import BaseEnvironment, ExecResult, ServiceOperationsUnsupportedError
from harbor.environments.capabilities import EnvironmentCapabilities
from harbor.models.task.config import NetworkMode, NetworkPolicy
from harbor.models.trial.paths import EnvironmentPaths
from harbor.trial.network_policy import merge_extra_allowlists

from .. import names as naming
from .. import pyfile
from .. import workdir as wd
from . import checkpoint, hubruntime, roster, upstream
from .resources import gpu_plan, integer
from .hubruntime import RUNTIME, TEAM_GIT, VENV_BIN
from .marks import GRADE_APP_VAR, GRADE_COMMIT_VAR, GRADE_MARK_VAR, GRADE_OUT_VAR
from .sandbox import (
    AGENT_GONE_STATES,
    DEFAULT_BACKEND,
    LEDGER_NAME,
    RESIZABLE_BACKENDS,
    SandboxMember,
    append_ledger,
    backend_network_modes,
    capabilities_for,
    TTL_MARGIN_MIN,
    task_timeout,
    ttl_for,
)

GATEWAY_PORT = 8080  # the one hub port exposed outside the sandbox
DOCKER_BRIDGE_GATEWAY = "172.17.0.1"  # Docker's default bridge gateway, when the daemon doesn't say
BOARD_PORT, PROXY_PORT, GIT_PORT = 8790, 8791, 8792  # hub localhost only, behind the gateway
STATE_DIR = "/var/lib/multi-agent-harness"  # board db + event log, proxy usage log: the run record
LOG_DIR = "/var/log/multi-agent-harness"  # service stdout/stderr
SECRET_DIR = "/run/multi-agent-harness"  # root-only env files
HUB_HEADERS_FILE = f"{SECRET_DIR}/hub.hdr"  # the exposed port's access headers, for curl -H @
BOARD_EXT_DIR = f"{RUNTIME}/board-ext"  # the board_policy and board_plugins files land here
MODEL_MAP_PATH = f"{STATE_DIR}/model-map.json"  # gateway model aliases -> billed model, part of the record
EXPORT_DIR = f"{EnvironmentPaths.artifacts_dir}/multi-agent-harness"  # Harbor collects /logs/artifacts after the agent
SERVICE_LIMIT_VARS = ("BOARD_RATE_PER_SEC", "BOARD_RATE_BURST", "PROXY_RATE_PER_SEC",
                      "PROXY_RATE_BURST", "PROXY_MAX_CONCURRENT")
EGRESS_PROBE_URL = "https://example.com"  # must be unreachable under no-network
POLICY_SETTLE_SEC = 60  # how long a policy switch may take to show up inside a sandbox
EXPORT_STEP_SEC = 120  # per export step; the bundle gets BUNDLE_SEC
BUNDLE_SEC = 600  # git bundle of the team repo; past 120s it runs detached and is polled
SAVE_DOWNLOAD_SEC = 900  # downloading the saved bundle and logs to the trial dir
SAVE_MIN_FREE_BYTES = 2 << 30  # host disk left free after the saved bundle lands
SAVE_REMOTE_DIR = "/tmp/multi-agent-harness-save"
CHECKPOINT_REMOTE_DIR = "/tmp/multi-agent-harness-checkpoint"  # apart from SAVE_REMOTE_DIR: both may run at once
RESUME_BUNDLE = "/tmp/multi-agent-harness-resume.bundle"
RESUME_STATE = "/tmp/multi-agent-harness-resume-state.tgz"
STATE_REMOTE = "/tmp/multi-agent-harness-hub-state.tgz"  # a checkpoint's pack of --ek hub_state_dirs
MARKS_OUT_DIR = "/logs/marks"  # a mark's verifier output, /logs/marks/<label>; root-only, never an agent's
MARKS_APP_DIR = f"{STATE_DIR}/marks"  # a mark's checkout of its pinned commit, removed after grading
MARK_CHECKOUT_SEC = 900  # checking out a mark's commit
MARK_KILL_AFTER_SEC = 30  # a mark's verifier still running this long after its timeout's SIGTERM is killed
MARK_SLACK_SEC = 120  # past the verifier's timeout and kill, the harness stops waiting for a mark's exec
MARK_DOWNLOAD_SEC = 600  # packing and downloading a mark's output
# --ek hub_cpus / hub_memory_mb size the hub apart from the agents (a shared verifier runs there).
# hub_size_at=verify (the default) creates the hub at the task's size and grows it right before the
# verifier (size_hub_for_verifier); start creates it at that size.
HUB_SIZE_AT_START, HUB_SIZE_AT_VERIFY = "start", "verify"
HUB_LIMITS_CMD = ("cat /sys/fs/cgroup/cpu.max /sys/fs/cgroup/memory.max 2>/dev/null || "
                  "cat /sys/fs/cgroup/cpu/cpu.cfs_quota_us /sys/fs/cgroup/memory/memory.limit_in_bytes 2>/dev/null; "
                  "nproc")

# Team kwargs that members must not see. auto_snapshot is accepted and dropped for old commands.
TEAM_KWARGS = ("n_agents", "n_gpus", "gpu_type", "budget_usd", "agent_budget_usd", "ttl_minutes", "service_start_timeout_sec",
               "egress_probe_url", "policy_settle_sec", "auto_snapshot", "hub_cpus", "hub_memory_mb", "hub_size_at",
               "board_policy", "board_plugins", "model_upstream", "resume_from", "hub_state_dirs",
               *(v.lower() for v in SERVICE_LIMIT_VARS))


def with_hosts(policy: NetworkPolicy, hosts: list[str]) -> NetworkPolicy:
    """`policy` plus extra allowed hostnames, Harbor's way (merge_extra_allowlists): public stays
    public, no-network becomes an allow-list of just these hosts."""
    if policy.network_mode == NetworkMode.PUBLIC or not hosts:
        return policy
    return merge_extra_allowlists(policy, list(hosts))


def _env_file(values: dict[str, str]) -> bytes:
    return "".join(f"{k}={shlex.quote(v)}\n" for k, v in values.items()).encode()


def _last_json(text: str | None) -> Any:
    try:
        return json.loads((text or "").strip().splitlines()[-1])
    except (IndexError, ValueError):
        return (text or "").strip()[-300:]


def parse_state_dirs(value: str | list[str] | None) -> list[str]:
    """--ek hub_state_dirs=/a,/b: absolute hub folders, no '..'."""
    items = value.split(",") if isinstance(value, str) else list(value or [])
    dirs = [str(d).strip().rstrip("/") for d in items if str(d).strip()]
    for d in dirs:
        if not d.startswith("/") or ".." in d.split("/") or d == "" or any(c in d for c in "'\"$`\\ \n"):
            raise ValueError(f"hub_state_dirs: {d!r} must be an absolute path with no '..', quotes or spaces")
    return dirs


def is_separate_verifier(environment_dir: Any, session_id: Any) -> bool:
    """Harbor builds a separate verifier's environment from the task's tests dir, with a session
    id of `<trial>__verifier__<key>` (harbor/trial/trial.py, _separate_verifier_env)."""
    return "__verifier__" in str(session_id or "") or Path(str(environment_dir or "")).name == "tests"



def docker_bridge_gateway() -> str:
    """The default bridge network's IPv4 gateway (docker network inspect bridge), else 172.17.0.1.
    Never an unspecified address such as 0.0.0.0."""
    configs: list = []
    try:
        import docker

        client = docker.from_env()
        try:
            configs = (client.networks.get("bridge").attrs.get("IPAM") or {}).get("Config") or []
        finally:
            client.close()
    except Exception:  # noqa: BLE001 - no docker-py, no daemon: the usual default
        pass
    for c in configs:
        try:
            ip = ipaddress.ip_address(str((c or {}).get("Gateway") or "").split("/")[0])
        except ValueError:
            continue
        if ip.version == 4 and not ip.is_unspecified:
            return str(ip)
    return DOCKER_BRIDGE_GATEWAY

class TeamEnvironment(BaseEnvironment):
    """Hub (main service) plus ``agent-1..N``, each an SDK sandbox from the task's image."""

    # Overridable so the hub's services can change without touching the lifecycle.
    BOARD_CMD = f"exec {VENV_BIN}/multi-agent-harness-board --host 127.0.0.1 --port {BOARD_PORT}"
    PROXY_CMD = f"exec {VENV_BIN}/multi-agent-harness-proxy --host 127.0.0.1 --port {PROXY_PORT}"
    GIT_CMD = (f"exec {VENV_BIN}/multi-agent-harness-git serve --host 127.0.0.1 --port {GIT_PORT} --root /srv/git "
               f"--tokens {SECRET_DIR}/git-tokens.json")
    GATEWAY_CMD = f"exec {VENV_BIN}/multi-agent-harness-gateway --host 0.0.0.0 --port {GATEWAY_PORT}"

    def __new__(cls, *args, **kwargs):
        if is_separate_verifier(kwargs.get("environment_dir"), kwargs.get("session_id")):
            return SandboxMember(*args, **{k: v for k, v in kwargs.items() if k not in TEAM_KWARGS})
        return super().__new__(cls)

    def __init__(self, *args, n_agents: int = 2, n_gpus: int = 0, gpu_type: str | None = None, budget_usd: float = 5.0,
                 agent_budget_usd: float | None = None, ttl_minutes: int | None = None,
                 service_start_timeout_sec: int = 60, egress_probe_url: str = EGRESS_PROBE_URL,
                 policy_settle_sec: float = POLICY_SETTLE_SEC, board_policy: str | None = None,
                 board_plugins: str | list[str] | None = None, model_upstream: str = "direct",
                 backend: str = DEFAULT_BACKEND, resume_from: str | None = None,
                 hub_cpus: int | str | None = None, hub_memory_mb: int | str | None = None,
                 hub_size_at: str = HUB_SIZE_AT_VERIFY, hub_state_dirs: str | list[str] | None = None, **kwargs):
        # Rate limits for the hub services: --ek board_rate_per_sec=... or the host env var;
        # unset means the service's own default.
        self.service_limits: dict[str, str] = {}
        for var in SERVICE_LIMIT_VARS:
            value = kwargs.pop(var.lower(), None)
            value = value if value is not None else os.environ.get(var)
            if value not in (None, ""):
                self.service_limits[var] = str(float(value))
        kwargs.pop("auto_snapshot", None)
        self.n_agents = integer(n_agents, "n_agents", 1)
        self.team_gpu_plan = gpu_plan(self.n_agents, n_gpus, gpu_type)
        self.n_gpus = self.team_gpu_plan["n_gpus"]
        self.gpu_type = self.team_gpu_plan["gpu_type"]
        self.backend = str(backend).lower()
        backend_network_modes(self.backend)  # unknown backend fails before anything starts
        if self.n_gpus and self.backend != "daytona":
            raise ValueError("n_gpus > 0 currently requires backend=daytona")
        if self.n_gpus:
            from .daytona_gpu import validate_gpu_type
            validate_gpu_type(self.gpu_type)
        self.hub_size = {"cpus": integer(hub_cpus, "hub_cpus", 1) if hub_cpus not in (None, "") else None,
                         "memory_mb": (integer(hub_memory_mb, "hub_memory_mb", 512)
                                       if hub_memory_mb not in (None, "") else None)}
        self.hub_size_at = str(hub_size_at).lower()
        if self.hub_size_at not in (HUB_SIZE_AT_START, HUB_SIZE_AT_VERIFY):
            raise ValueError(f"hub_size_at must be {HUB_SIZE_AT_START} or {HUB_SIZE_AT_VERIFY}, got {hub_size_at!r}")
        if (any(self.hub_size.values()) and self.hub_size_at == HUB_SIZE_AT_VERIFY
                and self.backend not in RESIZABLE_BACKENDS):
            raise ValueError(f"backend {self.backend} can't resize the running hub; pass --ek hub_size_at=start "
                             f"(resizing works on {', '.join(sorted(RESIZABLE_BACKENDS))})")
        self.team_hub_resized: dict[str, Any] | None = None  # what size_hub_for_verifier() did
        self.budget_usd = float(budget_usd)
        self.agent_budget_usd = agent_budget_usd
        self._ttl_override = ttl_minutes is not None
        self.service_start_timeout_sec = int(service_start_timeout_sec)
        self.egress_probe_url = egress_probe_url
        self.policy_settle_sec = float(policy_settle_sec)
        self.model_upstream = upstream.check_mode(model_upstream)
        self._upstream: upstream.Upstream | None = None
        self.team_model_upstream: str | None = None
        self._secrets: list[str] = []
        self._model_hosts: list[str] = []
        self._model_urls: list[str] = []
        self._agent_model_hosts: list[str] = []  # model hosts the agents call directly (claude-subscription)
        self._model_host_agents: set[str] = set()  # the agents that do: the Claude Code ones
        self.team_claude_subscription = False  # Claude Code agents run on the runner's own token
        self._verified_policy: NetworkPolicy | None = None  # last policy checked from inside the agents
        self._docker_open_warned: set[str] | None = None  # restricted modes run public on Docker (_enforced)
        self.team_gone_agents: set[str] = set()  # agent sandboxes the backend no longer has
        self.team_work_saved: Path | None = None  # the team repo bundle saved into the trial dir
        self._resume_from = str(resume_from) if resume_from not in (None, "") else None
        self.team_resume: dict[str, Any] | None = None  # {"path", "manifest"}, checked at start
        self.team_checkpoint_hook = None  # TeamAgent's final checkpoint, run before a delete
        self.team_agent_services = [f"agent-{i}" for i in range(1, self.n_agents + 1)]
        # Slot -> the name agents see (board, git, prompts); set at start, see multi_agent_harness/names.py.
        self.team_agent_names = {n: n for n in self.team_agent_services}
        self.team_agent_names_mode = naming.NUMBERED
        self.team_name_seed = ""
        self.team_hub_url: str | None = None
        self.team_hub_host: str | None = None
        self.team_hub_headers: dict[str, str] = {}
        self.team_board_url: str | None = None
        self.team_proxy_url: str | None = None
        self.team_git_url: str | None = None
        self.team_git_tokens: dict[str, str] = {}
        self.team_board_admin_token: str | None = None
        self.team_proxy_admin_token: str | None = None
        self.team_hub_runtime_sec: float | None = None
        self.team_workdir: str | None = None  # resolved at start (multi_agent_harness.workdir)
        self.team_workdir_source: str | None = None
        self.team_board_log_path = f"{STATE_DIR}/board-events.jsonl"
        self.team_proxy_log_path = f"{STATE_DIR}/proxy-usage.jsonl"
        super().__init__(*args, **kwargs)
        if (Path(self.environment_dir) / "docker-compose.yaml").exists():
            raise ValueError("docker-compose tasks are not supported; give the task an environment/Dockerfile")
        self.board_policy, self.board_plugins = self._board_extensions(board_policy, board_plugins)
        self.team_task_agent_timeout_sec = task_timeout(self.environment_dir)
        self.ttl_minutes = int(ttl_minutes) if ttl_minutes is not None else ttl_for(
            self.team_task_agent_timeout_sec)
        # The hub outlives the agents: a shared verifier runs on it after they finish.
        self.hub_extra_minutes = math.ceil((task_timeout(self.environment_dir, "verifier") or 0) / 60)
        self.hub_state_dirs = parse_state_dirs(hub_state_dirs)  # the task's own hub state, in every checkpoint
        self.team_hub_state_saved: list[str] | None = None  # what the last checkpoint saved of it
        self._marks_extra_minutes = 0  # --ak grade_at: room on the hub's TTL for a mark grading at the end
        self.team_marks: list[dict[str, Any]] | None = None  # TeamAgent's mark records (marks.py)
        self.team_verify_commit: str | None = None  # the commit export_artifacts checked out for the verifier
        self.team_verify_checkout: dict | None = None  # checkout_main's result; None: the workdir is not main
        self.team_verify_checkout_late = False  # checked out by the backstop in upload_dir, not by the agent
        self.team_final_mark_t0: float | None = None  # wall clock when the final verifier could start
        self._mark_tests_uploaded = False
        self.ledger_path = Path(self.trial_paths.trial_dir) / LEDGER_NAME
        # Members get everything Harbor and --ek passed. The hub starts open so it can install its
        # runtime; the baseline goes on at the end of start(). Agents start under the baseline
        # plus the hub's host, which is known by the time they are created.
        member = {**kwargs, "backend": self.backend, "phase_network_policies": []}
        # The hub gets its own copy of the task's [environment] config, so its size (hub_cpus,
        # hub_memory_mb) never reaches the agents, which share the original.
        hub_member = {**member, "task_env_config": self.task_env_config.model_copy(deep=True)}
        if self.hub_size_at == HUB_SIZE_AT_START:
            if self.hub_size["cpus"]:
                hub_member["override_cpus"] = self.hub_size["cpus"]
            if self.hub_size["memory_mb"]:
                hub_member["override_memory_mb"] = self.hub_size["memory_mb"]
        self.hub = self._member("hub", "hub", hub_member, NetworkPolicy())
        self.agents = {n: self._member("agent", n, member, self._enforced(self.network_policy))
                       for n in self.team_agent_services}

    @property
    def team_preview_token(self) -> str | None:
        """The first access header's value (Daytona's preview token), for older callers."""
        return next(iter(self.team_hub_headers.values()), None)

    @staticmethod
    def _board_extensions(policy: str | None, plugins: str | list[str] | None) -> tuple[Path | None, list[Path]]:
        """--ek board_policy=<path.py> and board_plugins=<path.py>[,<path.py>...], resolved against
        the working directory. Files are uploaded under their own names, so names must differ."""
        policy_file = pyfile.locate(str(policy), "board_policy") if policy and str(policy).strip() else None
        if isinstance(plugins, str):
            plugins = plugins.split(",")
        plugin_files = [pyfile.locate(str(p), "board_plugins") for p in plugins or [] if str(p).strip()]
        names = [f.name for f in ([policy_file] if policy_file else []) + plugin_files]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"board_policy and board_plugins files need distinct names; repeated: {', '.join(dupes)}")
        return policy_file, plugin_files

    def _board_ext_env(self) -> dict[str, str]:
        """BOARD_POLICY and BOARD_PLUGINS as the uploaded files' paths on the hub."""
        env: dict[str, str] = {}
        if self.board_policy:
            env["BOARD_POLICY"] = f"{BOARD_EXT_DIR}/{self.board_policy.name}"
        if self.board_plugins:
            env["BOARD_PLUGINS"] = ",".join(f"{BOARD_EXT_DIR}/{f.name}" for f in self.board_plugins)
        return env

    async def _check_board_extensions(self) -> None:
        """Upload the policy and plugin files and load them with the hub's Python, so a file that
        won't load fails setup instead of leaving the board down."""
        env = self._board_ext_env()
        if not env:
            return
        await self.hub.check(f"mkdir -p {BOARD_EXT_DIR}", "create board extensions dir")
        for f in ([self.board_policy] if self.board_policy else []) + self.board_plugins:
            await self.hub.upload_file(f, f"{BOARD_EXT_DIR}/{f.name}")
        assigns = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
        r = await self.hub.sh(f"env {assigns} {VENV_BIN}/multi-agent-harness-board --check", 120)
        if r.return_code != 0:
            raise RuntimeError(f"board extensions failed to load on the hub (board_policy={self.board_policy}, "
                               f"board_plugins={[str(f) for f in self.board_plugins]}): "
                               f"{(r.stderr or r.stdout or '').strip()[-500:]}")
        self.logger.info("multi-agent-harness-team: board policy %s, plugins %s",
                         self.board_policy.name if self.board_policy else "default",
                         [f.name for f in self.board_plugins])

    def _member(self, role: str, name: str, kwargs: dict[str, Any], policy: NetworkPolicy) -> SandboxMember:
        gpu = bool(self.team_gpu_plan["slots"].get(name, 0)) if role == "agent" else False
        return SandboxMember(role=role, name=name, ttl_minutes=self._ttl_for_role(role, self.ttl_minutes),
                             **{**kwargs, "network_policy": policy, "gpu": gpu,
                                "gpu_type": self.gpu_type if gpu else None})

    @property
    def team_agent_resources(self) -> dict[str, dict[str, Any]]:
        return {n: a.resources for n, a in self.agents.items()}

    _validate_gpu_support = SandboxMember._validate_gpu_support

    def _ttl_for_role(self, role: str, ttl: int) -> int:
        return ttl + self.hub_extra_minutes + self._marks_extra_minutes if role == "hub" else ttl

    @property
    def members(self) -> list[SandboxMember]:
        return [self.hub, *self.agents.values()]

    @staticmethod
    def type() -> str:
        return "multi-agent-harness-team"

    @property
    def capabilities(self) -> EnvironmentCapabilities:
        # Agents always keep the hub's host, so even no-network needs an allow-list backend.
        caps = capabilities_for(self.backend)
        if self.backend == "docker":
            # Docker can't allow-list the hub's host, so a restricted phase runs public (see _enforced). Harbor
            # is told the team takes those policies so the task isn't refused.
            return caps.model_copy(update={"disable_internet": True, "network_allowlist": True,
                                           "network_allowlist_hostnames": True,
                                           "network_allowlist_wildcard_hostnames": True,
                                           "dynamic_network_policy": True})
        return caps.model_copy(update={"disable_internet": caps.network_allowlist})

    def _validate_definition(self):
        """Members validate the task's environment dir when they are built."""

    # ---- lifecycle

    def _ledger(self, **entry: Any) -> None:
        append_ledger(self.ledger_path, self.logger, **entry)

    async def start(self, force_build: bool) -> None:
        try:
            await self._start(force_build)
        except BaseException:
            await asyncio.shield(self._delete_all())
            raise

    async def _start(self, force_build: bool) -> None:
        harnesses = self._agent_harnesses()  # a team that doesn't fit n_agents fails here, before any create
        if self._resume_from:  # checked before anything is created
            self.team_resume = checkpoint.load(self._resume_from, task=self.environment_name,
                                               harness=roster.label(harnesses.values()), n_agents=self.n_agents,
                                               gpu_allocation=self.team_gpu_plan, harnesses=harnesses)
            m = self.team_resume["manifest"]
            self._ledger(resumed_from={"path": str(self.team_resume["path"]), "n": m.get("n")})
            self.logger.info("multi-agent-harness-team: resuming from checkpoint %d (%s in, $%.4f spent): %s",
                             m.get("n"), m.get("elapsed_agent_sec"), m.get("spend_usd") or 0, self.team_resume["path"])
        self._assign_names()
        if self._agent_kwarg("grade_at") and not self._marks_extra_minutes:
            # A mark still grading when the agents end runs before the final verifier: the hub lives that much longer.
            self._marks_extra_minutes = self.hub_extra_minutes
            self.hub.ttl_minutes += self._marks_extra_minutes
        # Resolved once, before any create: the proxy's upstreams and the hub's allow-list must agree.
        self._upstream = upstream.resolve(self.model_upstream, os.environ, str(self.session_id),
                                          harness=set(harnesses.values()))
        self._secrets += self._upstream.secrets
        self._model_hosts = self._upstream.hosts
        self._model_urls = self._upstream.probe_urls
        self._agent_model_hosts = list(self._upstream.agent_hosts)
        self.team_claude_subscription = self._upstream.claude_subscription
        self.team_upstream_providers = sorted(self._upstream.urls)
        if self._agent_model_hosts:  # only agents that call their model provider themselves reach it
            self._model_host_agents = {n for n, h in harnesses.items() if h == "claude-code"}
        self.team_model_upstream = self._upstream.mode
        t0 = time.monotonic()
        self._ledger(event="gpu_allocation", gpu_allocation=self.team_gpu_plan)
        if self.backend == "docker" and "ports" not in self.hub.backend_options:
            # Publish the gateway on the bridge's gateway IP: agents on the bridge reach the hub there, and it
            # isn't on every host interface. backend_options with its own ports is used as given.
            gw = await asyncio.to_thread(docker_bridge_gateway)
            self.hub.backend_options = {**self.hub.backend_options,
                                        "ports": {f"{GATEWAY_PORT}/tcp": {"HostIp": gw, "HostPort": None}}}
            self._ledger(event="docker_hub_port", host_ip=gw, port=GATEWAY_PORT)
            self.logger.info("multi-agent-harness-team: publishing the hub's port %d on the Docker bridge gateway %s",
                             GATEWAY_PORT, gw)
        await self.hub.start(force_build)  # builds the task image; agents reuse the backend's build
        port = await self.hub.sandbox.expose_port(GATEWAY_PORT)
        self.team_hub_headers = dict(port.headers)
        self._secrets += [v for v in self.team_hub_headers.values() if v]
        self.team_hub_url = port.url.rstrip("/")
        self.team_hub_host = urlparse(self.team_hub_url).hostname
        self.team_workdir, self.team_workdir_source = await self._resolve_workdir()
        mapped = self._upstream.model_map
        mixed = roster.label(harnesses.values()) == roster.MIXED
        self._ledger(workdir=self.team_workdir, workdir_source=self.team_workdir_source,
                     model_upstream=self._upstream.mode, model_hosts=self._model_hosts,
                     **({"agent_harnesses": harnesses} if mixed else {}),
                     **({"agent_model_hosts": self._agent_model_hosts} if self._agent_model_hosts else {}),
                     **({"models_mapped": len(mapped)} if mapped is not None else {}),
                     **({"subscription_logins": len(self._upstream.logins)} if self._upstream.logins else {}))
        for name, a in self.agents.items():
            a._network_policy = self.agent_policy(self._enforced(self.network_policy), name)
        results = await asyncio.gather(self._install_runtime(),
                                       *(a.start(force_build) for a in self.agents.values()),
                                       return_exceptions=True)
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            raise errors[0]
        await self._start_services()
        await self._apply(self.network_policy)
        self.logger.info("multi-agent-harness-team: hub %s at %s, %d agents on %s, network %s, started in %.0fs",
                         self.hub.sandbox_id, self.team_hub_host, len(self.agents), self.backend,
                         self.network_policy.network_mode.value, time.monotonic() - t0)

    async def _resolve_workdir(self) -> tuple[str, str]:
        """task.toml workdir, else the Dockerfile's WORKDIR, else pwd in a login shell on the hub."""
        static = wd.resolve_static(self.task_env_config.workdir, self.environment_dir)
        if static:
            return static
        r = await self.hub.sh('"$(command -v bash || echo sh)" -lc \'pwd; printf "%s\\n" "$HOME"\'', 30)
        lines = (r.stdout or "").splitlines() if r.return_code == 0 else []
        return wd.accept_sandbox(lines[0] if lines else "", lines[1] if len(lines) > 1 else "")

    async def _install_runtime(self) -> None:
        """git, curl, a managed python and the multi-agent-harness[hub] wheel into /opt/multi-agent-harness; the team repo with
        its push rules; the run-record, secrets and Harbor verifier dirs."""
        hub = self.hub
        t0 = time.monotonic()
        await hub.check(f"mkdir -p {RUNTIME}", "create runtime dir")
        with tempfile.TemporaryDirectory(prefix="multi-agent-harness-wheel-") as tmp:
            wheel = hubruntime.build_wheel(Path(tmp))
            await hub.upload_file(wheel, f"{RUNTIME}/{wheel.name}")
        await hub.upload_file(hubruntime.CONSTRAINTS, f"{RUNTIME}/hub-constraints.txt")
        await hub.check(hubruntime.install_script(wheel.name), "install hub runtime", 900)
        dirs = [str(EnvironmentPaths.logs_dir), str(EnvironmentPaths.agent_dir),
                str(EnvironmentPaths.verifier_dir), str(EnvironmentPaths.artifacts_dir)]
        extra = [d for d in ("/tests", "/solution", self.task_env_config.workdir, self.team_workdir) if d]
        await hub.check(f"mkdir -p {' '.join(map(shlex.quote, dirs + extra))} {STATE_DIR} {LOG_DIR} "
                        f"{SECRET_DIR} && chmod 777 {' '.join(map(shlex.quote, dirs))} "
                        f"&& chmod 700 {SECRET_DIR}", "create dirs")
        await hub.check(hubruntime.git_script(), "create team repo")
        if self.team_resume:  # main and every branch from the checkpoint; seed then leaves main alone
            await hub.upload_file(self.team_resume["path"] / "team.bundle", RESUME_BUNDLE)
            await hub.check(f"git -c safe.directory='*' --git-dir={TEAM_GIT} fetch -q {RESUME_BUNDLE} "
                            f"'+refs/heads/*:refs/heads/*' && rm -f {RESUME_BUNDLE}",
                            "seed team repo from the checkpoint bundle", 900)
            state = self.team_resume["path"] / checkpoint.HUB_STATE
            if state.is_file():  # the task's own hub state, back before any board plugin loads (_check_board_extensions)
                await hub.upload_file(state, RESUME_STATE)
                await hub.check(f"tar -xzpf {RESUME_STATE} -C / && rm -f {RESUME_STATE}",
                                "restore the task's hub state from the checkpoint", 900)
                saved = self.team_resume["manifest"].get("hub_state")
                self._ledger(event="hub_state_restored", dirs=saved, bytes=state.stat().st_size)
                self.logger.info("multi-agent-harness-team: restored the task's hub state %s from the checkpoint", saved)
        seeded = await hub.check(hubruntime.workdir_cmd("seed", self.team_workdir, STATE_DIR),
                                 "seed team repo from the workdir", 900)
        self._ledger(team_repo_seed=_last_json(seeded))
        await self._check_board_extensions()
        self.team_hub_runtime_sec = round(time.monotonic() - t0, 1)
        self.logger.info("multi-agent-harness-team: hub runtime installed in %.1fs", self.team_hub_runtime_sec)

    async def _start_services(self) -> None:
        hub = self.hub
        self.team_board_admin_token = secrets.token_urlsafe(32)
        self.team_proxy_admin_token = secrets.token_urlsafe(32)
        self.team_git_tokens = {self.team_agent_names[n]: secrets.token_urlsafe(32) for n in self.team_agent_services}
        self._secrets += [self.team_board_admin_token, self.team_proxy_admin_token,
                          *self.team_git_tokens.values()]
        board_env = {"BOARD_ADMIN_TOKEN": self.team_board_admin_token, "BOARD_DB": f"{STATE_DIR}/board.sqlite",
                     "BOARD_LOG": self.team_board_log_path, **self._board_ext_env()}
        proxy_env = {"PROXY_ADMIN_TOKEN": self.team_proxy_admin_token, "PROXY_LOG": self.team_proxy_log_path,
                     "PROXY_BUDGET_USD": str(self.budget_usd), **self._upstream.proxy_env()}
        if self._upstream.model_map is not None:
            await hub.upload_bytes(json.dumps(self._upstream.model_map, indent=1).encode(), MODEL_MAP_PATH)
            proxy_env["PROXY_MODEL_MAP"] = MODEL_MAP_PATH
        if self._upstream.logins:  # subscription credentials: root-only files on the hub, like the keys
            paths = [f"{SECRET_DIR}/codex-login-{i}.json" for i in range(1, len(self._upstream.logins) + 1)]
            for raw, path in zip(self._upstream.logins, paths):
                await hub.upload_bytes(raw, path)
            proxy_env["PROXY_CODEX_LOGINS"] = ":".join(paths)
        if self.agent_budget_usd is not None:
            proxy_env["PROXY_AGENT_BUDGET_USD"] = str(self.agent_budget_usd)
        for var, value in self.service_limits.items():
            (board_env if var.startswith("BOARD_") else proxy_env)[var] = value
        git_tokens = {n: hashlib.sha256(t.encode()).hexdigest() for n, t in self.team_git_tokens.items()}
        await hub.upload_bytes(_env_file(board_env), f"{SECRET_DIR}/board.env")
        await hub.upload_bytes(_env_file(proxy_env), f"{SECRET_DIR}/proxy.env")
        await hub.upload_bytes(json.dumps(git_tokens).encode(), f"{SECRET_DIR}/git-tokens.json")
        await hub.upload_bytes("".join(f"{k}: {v}\n" for k, v in self.team_hub_headers.items()).encode(),
                               HUB_HEADERS_FILE)
        await hub.check(f"chmod 600 {SECRET_DIR}/*", "lock secrets")
        if self.team_resume:
            # The proxy replays its usage log at start, so spend and budget carry over. The board
            # log is the run's record: the earlier events, a marker, then this run's.
            ck = self.team_resume["path"]
            usage = ck / "proxy-usage.jsonl"
            await hub.upload_bytes(usage.read_bytes() if usage.is_file() else b"", self.team_proxy_log_path)
            board = ck / "board-events.jsonl"
            marker = json.dumps({"ts": time.time(), "event": "resumed", "agent": None, "checkpoint": str(ck),
                                 "n": self.team_resume["manifest"].get("n")}) + "\n"
            await hub.upload_bytes((board.read_bytes() if board.is_file() else b"") + marker.encode(),
                                   self.team_board_log_path)

        services = {"board": (BOARD_PORT, f"{SECRET_DIR}/board.env", self.BOARD_CMD),
                    "proxy": (PROXY_PORT, f"{SECRET_DIR}/proxy.env", self.PROXY_CMD),
                    "git": (GIT_PORT, None, self.GIT_CMD),
                    "gateway": (GATEWAY_PORT, None, self.GATEWAY_CMD)}
        for name, (_, env_file, cmd) in services.items():
            source = f"set -a; . {env_file}; set +a; " if env_file else ""
            await hub.spawn(f"{source}{cmd} >>{LOG_DIR}/{name}.log 2>&1")
        ports = " ".join(str(p) for p, _, _ in services.values())
        wait = (f"for p in {ports}; do ok=; for i in $(seq {self.service_start_timeout_sec}); do "
                f"if (echo > /dev/tcp/127.0.0.1/$p) 2>/dev/null; then ok=1; break; fi; sleep 1; done; "
                f'[ -n "$ok" ] || {{ echo "nothing listening on :$p" >&2; tail -n 20 {LOG_DIR}/*.log >&2; exit 1; }}; done')
        r = await hub.sh(wait, self.service_start_timeout_sec * len(services) + 30)
        if r.return_code != 0:
            raise RuntimeError(f"hub services did not start: {self._redact(r.stderr or r.stdout or '')}")

        self.team_board_url = self.team_hub_url
        self.team_proxy_url = f"{self.team_hub_url}/proxy"
        self.team_git_url = f"{self.team_hub_url}/git/team.git"
        # The whole path an agent will use: the exposed URL, its headers, the gateway.
        rc = await hub.check(f"curl -fsS -m 20 -o /dev/null -w '%{{http_code}}' "
                             f"-H @{HUB_HEADERS_FILE} {self.team_hub_url}/healthz",
                             "reach the gateway through the exposed URL", 60)
        self.logger.info("multi-agent-harness-team: gateway answers %s through the exposed URL", rc.strip())

    # ---- network policy (Harbor's per-phase contract)

    async def _apply_network_policy(self, network_policy: NetworkPolicy) -> None:
        """Called by Harbor's set_network_policy() at phase boundaries."""
        await self._apply(network_policy)

    def _agent_harnesses(self) -> dict[str, str]:
        """Agent sandbox -> its harness, from --ak harness and --ak team (codex for all when there is
        none). The environment needs it before any sandbox exists: model_upstream=subscription means
        Codex logins on the hub for codex, but a token in the agent sandboxes for claude-code."""
        return roster.harnesses(self._agent_kwargs(), self.team_agent_services)

    def _agent_kwargs(self) -> dict | None:
        """The --ak values, from the trial config Harbor writes before the environment starts."""
        try:
            config = json.loads(Path(self.trial_paths.config_path).read_text())
        except (OSError, ValueError, AttributeError):
            config = None
        kwargs = (config.get("agent") or {}).get("kwargs") if isinstance(config, dict) else None
        return kwargs if isinstance(kwargs, dict) else None

    def _agent_kwarg(self, key: str) -> Any:
        """One --ak value, or None."""
        kwargs = self._agent_kwargs()
        return kwargs.get(key) if kwargs else None

    def _assign_names(self) -> None:
        """Agent-facing names, before the git tokens are made for them. --ak agent_names picks
        words (default) or numbered; a resumed run keeps its checkpoint's names (numbered for a
        checkpoint from before names were recorded)."""
        self.team_name_seed = str(self.session_id)
        mode = naming.check_mode(self._agent_kwarg("agent_names"))
        names = naming.assign(self.team_agent_services, self.team_name_seed, mode)
        if self.team_resume:
            m = self.team_resume["manifest"]
            names = m.get("agent_names") or {n: n for n in self.team_agent_services}
            if sorted(names) != sorted(self.team_agent_services) or len(set(names.values())) != len(names):
                raise ValueError(f"resume_from: the checkpoint's agent_names {names!r} do not fit "
                                 f"{self.n_agents} agents")
            mode = naming.NUMBERED if all(k == v for k, v in names.items()) else naming.WORDS
            self.team_name_seed = m.get("name_seed") or self.team_name_seed
        self.team_agent_names = {n: names[n] for n in self.team_agent_services}
        self.team_agent_names_mode = mode
        self._ledger(agent_names=self.team_agent_names, agent_names_mode=mode)

    def agent_policy(self, policy: NetworkPolicy, name: str | None = None) -> NetworkPolicy:
        """What agent `name` gets under `policy`: it plus the hub's host, and for a Claude Code agent
        on the subscription token Anthropic's API host. With no name, what the widest agent gets."""
        hosts = [self.team_hub_host] if self.team_hub_host else []
        if name is None or name in self._model_host_agents:
            hosts += self._agent_model_hosts
        return with_hosts(policy, hosts)

    def hub_policy(self, policy: NetworkPolicy) -> NetworkPolicy:
        """What the hub gets under `policy`: it plus the model upstream's hosts."""
        return with_hosts(policy, self._model_hosts)

    def _enforced(self, policy: NetworkPolicy) -> NetworkPolicy:
        """The policy the team runs under for `policy`. Docker can't keep the hub's host open under a
        restricted policy (it has no allow-lists), so there the team runs public: warned once, and
        ledgered per requested mode. Everything else, and public on Docker, is used as given."""
        if self.backend != "docker" or policy.network_mode == NetworkMode.PUBLIC:
            return policy
        mode = policy.network_mode.value
        if not self._docker_open_warned:
            self._docker_open_warned = set()
            self.logger.warning("multi-agent-harness-team: on --ek backend=docker the agents have internet access: "
                                "the task's network_mode=%s isn't enforced (Docker has no allow-lists to keep the "
                                "hub reachable). Use --ek backend=daytona for network isolation.", mode)
        if mode not in self._docker_open_warned:
            self._docker_open_warned.add(mode)
            self._ledger(event="network_not_enforced", backend=self.backend, requested=mode,
                         allowed_hosts=list(policy.allowed_hosts), applied="public")
        return NetworkPolicy()

    async def _apply(self, policy: NetworkPolicy) -> None:
        """Put `policy` (as _enforced has it) on every sandbox, then check it from inside each one. Fails
        closed: any error leaves the caller with an exception."""
        policy = self._enforced(policy)
        self._verified_policy = None
        hub_p, agent_p = self.hub_policy(policy), self.agent_policy(policy)
        await asyncio.gather(self.hub.set_network_policy(hub_p),
                             *(self._on_agent(a, "set_network", a.set_network_policy(self.agent_policy(policy, a.name)))
                               for a in self._live_agents()))
        await asyncio.gather(self._verify_hub(policy),
                             *(self._on_agent(a, "verify_network", self._verify_agent(a, policy))
                               for a in self._live_agents()))
        self._verified_policy = policy
        self._ledger(network=policy.network_mode.value, allowed_hosts=list(policy.allowed_hosts),
                     agents=agent_p.network_mode.value, agent_hosts=list(agent_p.allowed_hosts),
                     hub=hub_p.network_mode.value, hub_hosts=list(hub_p.allowed_hosts))

    async def verify_agent_network(self) -> NetworkPolicy:
        """The policy in force for agents, checked from inside each agent sandbox (skipped when
        the last switch already checked it). TeamAgent calls this before writing tokens."""
        policy = self._enforced(self.network_policy)
        if self._verified_policy != policy:
            await asyncio.gather(*(self._on_agent(a, "verify_network", self._verify_agent(a, policy))
                                   for a in self._live_agents()))
            self._verified_policy = policy
        return policy

    def _live_agents(self) -> list[SandboxMember]:
        return [a for n, a in self.agents.items() if n not in self.team_gone_agents]

    async def _on_agent(self, agent: SandboxMember, step: str, call) -> None:
        """Await `call` on an agent sandbox. If it fails and a lookup confirms the sandbox no
        longer exists or has stopped for good (AGENT_GONE_STATES: the backend stopped, archived or
        destroyed it), the agent is recorded as gone and dropped from later steps: such a sandbox
        runs nothing, so there is nothing left to restrict. Any other error, or one the lookup does
        not confirm (a live or unknown state), still raises (fails closed)."""
        try:
            await call
        except Exception as e:
            state = await agent.status()
            if state not in AGENT_GONE_STATES:
                raise
            self.agent_gone(agent.name, step, e, state=state)

    def agent_gone(self, name: str, step: str, error: BaseException | str, state: str | None = None) -> None:
        if name in self.team_gone_agents:
            return
        self.team_gone_agents.add(name)
        err = error if isinstance(error, str) else f"{type(error).__name__}: {str(error)[:200]}"
        self.logger.warning("multi-agent-harness-team: %s sandbox %s is gone (%s, %s at %s); continuing without it",
                            name, self.agents[name].sandbox_id, state or "unknown state", err, step)
        self._ledger(event="agent_gone", name=name, sandbox_id=self.agents[name].sandbox_id, step=step, error=err,
                     state=state)

    async def _settle(self, what: str, check) -> None:
        deadline = time.monotonic() + self.policy_settle_sec
        while True:
            try:
                ok, detail = await check()
            except Exception as e:  # noqa: BLE001 - the API can drop a call while a switch lands
                ok, detail = False, f"{type(e).__name__}: {str(e)[:200]}"
            if ok:
                return
            if time.monotonic() > deadline:
                raise RuntimeError(f"{what}: {detail} after {self.policy_settle_sec:.0f}s; failing closed")
            await asyncio.sleep(3)

    async def _verify_agent(self, agent: SandboxMember, policy: NetworkPolicy) -> None:
        """The hub's host must answer (without the access headers Daytona's proxy answers 401,
        which still proves the route); under a restricted policy the probe URL must not. Without
        curl a restricted policy fails closed; a public one has nothing to prove (at start,
        before setup has installed curl)."""
        restricted = policy.network_mode != NetworkMode.PUBLIC
        if (await agent.sh("command -v curl", 30)).return_code != 0:
            if not restricted:
                return
            raise RuntimeError(f"{agent.name}: no curl to verify network policy {policy.network_mode.value}; "
                               "failing closed")

        own_hosts = self._agent_model_hosts if agent.name in self._model_host_agents else []
        probe_host = urlparse(self.egress_probe_url).hostname
        check_blocked = restricted and probe_host not in policy.allowed_hosts and probe_host not in own_hosts

        async def check():
            hub = await agent.probe(f"{self.team_hub_url}/healthz")
            ext = await agent.probe(self.egress_probe_url) if check_blocked else None
            models = [await agent.probe(f"https://{h}") for h in own_hosts] if restricted else []
            ok = hub == 0 and (ext is None or ext != 0) and all(rc == 0 for rc in models)
            return ok, f"hub curl rc={hub}, {self.egress_probe_url} curl rc={ext}, model hosts curl rc={models}"

        what = f"{agent.name} network {policy.network_mode.value}"
        if restricted:
            await self._settle(what, check)
            return
        # Loosening (e.g. back to a public baseline after the agents finish) can't leak anything,
        # so a slow hub probe here is a warning, not a failed trial. Restricted switches fail closed.
        try:
            await self._settle(what, check)
        except RuntimeError as e:
            self.logger.warning("multi-agent-harness-team: %s; not failing on a public policy", e)

    async def _verify_hub(self, policy: NetworkPolicy) -> None:
        """Under a restricted policy the model upstreams must answer and the probe URL must
        not, unless the policy names its host."""
        if policy.network_mode == NetworkMode.PUBLIC:
            return
        probe_host = urlparse(self.egress_probe_url).hostname
        check_blocked = probe_host not in policy.allowed_hosts and probe_host not in self._model_hosts

        async def check():
            models = [await self.hub.probe(u) for u in self._model_urls]
            ext = await self.hub.probe(self.egress_probe_url) if check_blocked else None
            ok = all(rc == 0 for rc in models) and (ext is None or ext != 0)
            return ok, f"model hosts curl rc={models}, {self.egress_probe_url} curl rc={ext}"

        await self._settle(f"hub network {policy.network_mode.value}", check)

    async def size_hub_for_verifier(self) -> dict[str, Any] | None:
        """With hub_size_at=verify, grow the hub to hub_cpus / hub_memory_mb once the agents are
        done, right before the shared verifier runs there: the hub stays small while the agents
        work. Best effort, never raises: a failed resize is logged and ledgered, and the verifier
        runs at the hub's old size. Returns what was done, or None when there was nothing to do."""
        size = {k: v for k, v in self.hub_size.items() if v}
        if not size or self.hub_size_at != HUB_SIZE_AT_VERIFY or self.hub.sb is None or self.team_hub_resized:
            return None
        before = {"cpus": self.hub.resources["cpus"], "memory_mb": self.hub.resources["memory_mb"]}
        t0 = time.monotonic()
        try:
            await self.hub.resize(size.get("cpus"), size.get("memory_mb"))
        except Exception as e:  # noqa: BLE001 - the verifier still runs, at the old size
            err = self._redact(f"{type(e).__name__}: {e}")[:300]
            self.logger.warning("multi-agent-harness-team: resizing the hub to %s for the verifier failed; "
                                "it stays %s: %s", size, before, err)
            self.team_hub_resized = {"ok": False, "from": before, "to": size, "error": err}
            self._ledger(event="hub_resize_failed", **self.team_hub_resized)
            return self.team_hub_resized
        try:
            seen = (await self.hub.sh(HUB_LIMITS_CMD, 30)).stdout or ""
        except Exception as e:  # noqa: BLE001 - only a diagnostic; the resize itself succeeded
            seen = f"unavailable: {type(e).__name__}"
        self.team_hub_resized = {"ok": True, "from": before, "to": size, "sec": round(time.monotonic() - t0, 1),
                                 "limits_seen": " ".join(seen.split())[:200]}
        self.logger.info("multi-agent-harness-team: hub resized for the verifier from %s to %s (cgroup: %s)",
                         before, size, self.team_hub_resized["limits_seen"])
        self._ledger(event="hub_resized", **self.team_hub_resized)
        return self.team_hub_resized

    async def checkout_main(self) -> dict:
        """Check main out into the hub's workdir as a clean tree for the verifier (the old contents move
        to /var/lib/multi-agent-harness/workdir-aside, see multi_agent_harness/workdir.py) and ledger it as
        verify_checkout. Raises if the checkout fails."""
        r = await self.hub.sh(hubruntime.workdir_cmd("checkout", self.team_workdir or wd.DEFAULT, STATE_DIR),
                              900)
        self._check_step(r, f"export: checkout of main into {self.team_workdir}", 900)
        checkout = _last_json(r.stdout)
        self._ledger(verify_checkout=checkout)
        self.team_verify_checkout = checkout if isinstance(checkout, dict) else {"output": checkout}
        if isinstance(checkout, dict) and checkout.get("commit"):
            self.team_verify_commit = checkout["commit"]
        return self.team_verify_checkout

    async def export_artifacts(self) -> None:
        """Before the verifier: check main out into the hub's workdir (checkout_main), and put a git
        bundle of the team repo (all refs) and the board log under /logs/artifacts/multi-agent-harness.
        Harbor collects /logs/artifacts and the task's `artifacts` after the agent runs and, in
        separate verifier mode, re-creates them in the verifier's sandbox. Nothing there is secret.
        TeamAgent runs it as soon as the agents are stopped, through Harbor's agent timeout; the
        backstop in upload_dir checks main out if it never ran."""
        if self.team_verify_checkout is None:
            await self.checkout_main()
        steps = [("mkdir", f"mkdir -p {EXPORT_DIR}", EXPORT_STEP_SEC),
                 ("bundle", f"git -c safe.directory='*' --git-dir={TEAM_GIT} bundle create "
                            f"{EXPORT_DIR}/team.bundle --all", BUNDLE_SEC),
                 ("copy board log", f"cp {self.team_board_log_path} {EXPORT_DIR}/board-events.jsonl",
                  EXPORT_STEP_SEC),
                 ("chmod", f"chmod -R a+rX {EXPORT_DIR}", EXPORT_STEP_SEC)]
        for step, cmd, timeout in steps:
            self._check_step(await self.hub.sh(cmd, timeout), f"export to {EXPORT_DIR}: {step}", timeout)
        self.team_final_mark_t0 = time.time()

    # ---- graded marks (marks.py): the task's verifier on a pinned main while the agents work

    def _trial_config(self) -> dict:
        try:
            config = json.loads(Path(self.trial_paths.config_path).read_text())
        except (OSError, ValueError, AttributeError):
            return {}
        return config if isinstance(config, dict) else {}

    def mark_verifier(self) -> dict[str, Any]:
        """How the final verifier runs, for a mark to run the same way: the task's tests dir and test
        script, its [verifier] env (plus the trial's --ve overrides), user and timeout. Raises for a
        task whose verifier runs in a separate environment, which a mark can't reproduce on the hub."""
        from harbor.models.task.config import TaskConfig
        from harbor.models.task.verifier_mode import VerifierEnvironmentMode, resolve_task_verifier_mode
        from harbor.utils.scripts import discover_script

        task_dir = Path(self.environment_dir).parent
        cfg = TaskConfig.model_validate_toml((task_dir / "task.toml").read_text())
        if resolve_task_verifier_mode(cfg) != VerifierEnvironmentMode.SHARED:
            raise ValueError("grade_at needs a task whose verifier runs on the hub (shared verifier mode)")
        tests = task_dir / "tests"
        script = discover_script(tests, "test")
        if script is None:
            raise ValueError(f"grade_at: no test script in {tests}")
        trial = self._trial_config().get("verifier") or {}
        timeout = float(trial.get("override_timeout_sec") or cfg.verifier.timeout_sec)
        if trial.get("max_timeout_sec"):
            timeout = min(timeout, float(trial["max_timeout_sec"]))
        mult = self._trial_config().get("verifier_timeout_multiplier")  # else timeout_multiplier, as Harbor does
        timeout *= float(mult if mult is not None else self._trial_config().get("timeout_multiplier", 1.0))
        return {"tests": tests, "script": f"{EnvironmentPaths.tests_dir}/{script.relative_to(tests).as_posix()}",
                "env": {**cfg.verifier.env, **(trial.get("env") or {})}, "user": cfg.verifier.user,
                "timeout_sec": timeout}

    async def pin_main(self) -> str:
        """The team repo's main commit right now."""
        r = await self.hub.sh(f"git -c safe.directory='*' --git-dir={TEAM_GIT} rev-parse 'refs/heads/main^{{commit}}'", 60)
        self._check_step(r, "pin main", 60)
        return (r.stdout or "").strip().splitlines()[-1]

    async def _grow_hub_for_mark(self) -> dict[str, Any] | None:
        """Grow the hub to hub_cpus / hub_memory_mb for a mark's grading, as before the final verifier.
        Returns the size to go back to, or None when there is nothing to undo."""
        size = {k: v for k, v in self.hub_size.items() if v}
        if not size or self.hub_size_at != HUB_SIZE_AT_VERIFY or self.team_hub_resized or self.hub.sb is None:
            return None
        before = {"cpus": self.hub.resources["cpus"], "memory_mb": self.hub.resources["memory_mb"]}
        try:
            await self.hub.resize(size.get("cpus"), size.get("memory_mb"))
        except Exception as e:  # noqa: BLE001 - the mark grades at the hub's current size
            err = self._redact(f"{type(e).__name__}: {e}")[:300]
            self.logger.warning("multi-agent-harness-team: growing the hub for a mark failed; it stays %s: %s", before, err)
            self._ledger(event="hub_resize_failed", reason="mark", ok=False, **{"from": before, "to": size}, error=err)
            return None
        self._ledger(event="hub_resized", reason="mark", ok=True, **{"from": before, "to": size})
        return before

    async def _shrink_hub_after_mark(self, before: dict[str, Any] | None) -> None:
        """Back to the agents' size after a mark. Daytona can only grow, and a shrink that fails leaves the
        hub at grading size: then it stays there, and size_hub_for_verifier has nothing left to do."""
        if before is None:
            return
        size = {k: v for k, v in self.hub_size.items() if v}
        err = "daytona resizes only up"
        if self.backend != "daytona":
            try:
                await self.hub.resize(before["cpus"] if size.get("cpus") else None,
                                      before["memory_mb"] if size.get("memory_mb") else None)
                self._ledger(event="hub_resized", reason="after mark", ok=True, **{"from": size, "to": before})
                return
            except Exception as e:  # noqa: BLE001
                err = self._redact(f"{type(e).__name__}: {e}")[:300]
        self.logger.info("multi-agent-harness-team: the hub stays at grading size %s (%s)", size, err)
        self.team_hub_resized = {"ok": True, "from": before, "to": size, "kept_after_mark": True, "note": err}
        self._ledger(event="hub_resized", **self.team_hub_resized)

    async def grade_mark(self, label: str, commit: str, dest: Path) -> dict[str, Any]:
        """Run the task's verifier on the hub for `commit`, while the agents keep working: a fresh
        checkout of the commit in a root-only folder, output in /logs/marks/<label> (see marks.py for the
        environment variables), then that output downloaded into dest. Returns {status, reward,
        verifier_s, exit, ...}; raises only when the grading could not run at all."""
        from harbor.utils.env import resolve_env_vars

        from .marks import read_reward

        spec = self.mark_verifier()
        app, out = f"{MARKS_APP_DIR}/{label}", f"{MARKS_OUT_DIR}/{label}"
        q = shlex.quote
        user = spec["user"]
        # A verifier user other than the sandbox's own gets its checkout, output and the tests; the parents
        # only let it through (711).
        own = (f"chown -R {q(str(user))} {q(app)} {q(out)} /tests; chmod 711 {MARKS_OUT_DIR} {MARKS_APP_DIR}; "
               if user is not None else "")
        before = await self._grow_hub_for_mark()
        t0 = time.monotonic()
        try:
            if not self._mark_tests_uploaded:  # what Harbor uploads before the final verifier, root-only
                tests = str(EnvironmentPaths.tests_dir)
                await self.hub.check(f"mkdir -p {q(tests)} && chmod 700 {q(tests)}", "create the tests dir")
                await self.hub.upload_dir(spec["tests"], tests)
                await self.hub.check(f"chmod 700 {q(tests)}", "lock the tests dir")
                self._mark_tests_uploaded = True
            await self.hub.check(
                f"set -e; umask 022; mkdir -p {MARKS_OUT_DIR} {MARKS_APP_DIR}; chmod 700 {MARKS_OUT_DIR} {MARKS_APP_DIR}; "
                f"rm -rf {q(app)} {q(out)}; mkdir -p {q(app)} {q(out)}; chmod 755 {q(out)}; "
                f"git -c safe.directory='*' --git-dir={TEAM_GIT} archive --format=tar {q(commit)} | tar -x -C {q(app)}; "
                f"{own}", f"check out {commit[:12]} for mark {label}", MARK_CHECKOUT_SEC)
            env = {**resolve_env_vars(spec["env"]), GRADE_APP_VAR: app, GRADE_OUT_VAR: out,
                   GRADE_MARK_VAR: label, GRADE_COMMIT_VAR: commit}
            if spec["script"].endswith(".sh"):
                await self.exec(f"chmod +x {q(spec['script'])}", user="root", timeout_sec=60)
            timeout = int(spec["timeout_sec"])
            # Run as Harbor runs the final verifier: as the task's verifier user (not the agent phase's default
            # user) with the environment's own env (not the agent phase's scoped env). The timeout is ours, with
            # a kill after it; a script that itself exits 124 before it is not a timeout.
            cmd = f"timeout -k {MARK_KILL_AFTER_SEC} {timeout} {q(spec['script'])} > {q(out)}/test-stdout.txt 2>&1"
            t1 = time.monotonic()
            try:
                r = await asyncio.wait_for(self.hub.exec(cmd, env={**self._persistent_env, **env}, user=user),
                                           timeout + MARK_KILL_AFTER_SEC + MARK_SLACK_SEC)
                rc = r.return_code
                timed_out = rc in (124, 137) and time.monotonic() - t1 >= timeout
            except asyncio.TimeoutError:
                rc, timed_out = None, True
            verifier_s = round(time.monotonic() - t0, 1)
            try:
                await self.hub.download_dir(out, dest)
            except Exception as e:  # noqa: BLE001
                return {"status": "failed", "reward": None, "verifier_s": verifier_s, "exit": rc,
                        "error": self._redact(f"downloading {out}: {type(e).__name__}: {e}")[:300]}
            if timed_out:
                return {"status": "timeout", "reward": None, "verifier_s": verifier_s, "exit": rc,
                        "error": f"the verifier ran past its {timeout}s timeout"}
            reward = read_reward(dest)
            if reward is None:
                return {"status": "failed", "reward": None, "verifier_s": verifier_s, "exit": rc,
                        "error": "no reward.txt or reward.json in the mark's output"}
            return {"status": "graded", "reward": reward, "verifier_s": verifier_s, "exit": rc}
        finally:
            # Whatever of the mark's verifier is still running (it timed out, the grading was cancelled, or it
            # left something behind) is killed, by the output dir in its environment, before the next grading.
            kill = (f"for p in /proc/[0-9]*; do tr '\\0' '\\n' 2>/dev/null < $p/environ | "
                    f"grep -qxF {q(f'{GRADE_OUT_VAR}={out}')} && kill -KILL ${{p#/proc/}} 2>/dev/null; done; "
                    f"rm -rf {q(app)}")
            await asyncio.shield(self.hub._quiet(f"sh -c {q(kill)}"))
            await self._shrink_hub_after_mark(before)

    async def hold_hub_for_marks(self, pending: int) -> None:
        """Before the agent phase finishes the `pending` gradings still queued or running: set the hub's TTL
        (Daytona counts it from now) to cover each at its longest, then the final verifier, plus the margin."""
        if not pending or self.hub.sb is None:
            return
        per = (MARK_CHECKOUT_SEC + float(self.mark_verifier()["timeout_sec"]) + MARK_KILL_AFTER_SEC + MARK_SLACK_SEC
               + MARK_DOWNLOAD_SEC)
        minutes = math.ceil(pending * per / 60) + self.hub_extra_minutes + TTL_MARGIN_MIN
        try:
            ok = await self.hub.set_ttl(minutes)
        except Exception as e:  # noqa: BLE001 - the gradings run anyway
            self.logger.warning("multi-agent-harness-team: setting the hub's TTL for %d gradings failed: %s",
                                pending, self._redact(f"{type(e).__name__}: {e}")[:300])
            return
        if ok:
            self._ledger(event="hub_ttl", reason="marks", pending=pending, minutes=minutes)

    def finish_final_mark(self) -> None:
        """After the final verifier: fill in the record of the mark it graded (TeamAgent adds it with status
        awaiting_final_verifier) from <trial>/verifier, and copy those files into <trial>/marks/<label>/.
        result.json is written after this, so the record lands there too. Never raises."""
        from .marks import read_reward

        unchecked = self._verifier_ran_without_checkout()
        rec = next((m for m in self.team_marks or [] if m.get("status") == "awaiting_final_verifier"), None)
        if rec is None:
            return
        try:
            vdir = Path(self.trial_paths.verifier_dir)
            reward = read_reward(vdir)
            done = [p for p in (vdir / "reward.json", vdir / "reward.txt") if p.is_file()]
            if reward is not None and self.team_final_mark_t0:
                rec["verifier_s"] = round(max(p.stat().st_mtime for p in done) - self.team_final_mark_t0, 1)
            rec.update(status="graded" if reward is not None else "failed", reward=reward)
            if reward is None:
                rec["error"] = "the final verifier left no reward"
            if rec.get("commit") is None and self.team_verify_commit:
                rec["commit"] = self.team_verify_commit
            if self.team_verify_checkout_late:
                rec["checkout"] = "late"
            if unchecked:  # its reward is for the workdir as the agents left it, not for main
                rec.update(status="failed", reward=None, verifier_reward=reward,
                           error="the final verifier ran without main checked out into the workdir "
                                 "(no verify_checkout); its reward is not main's")
            dest = Path(rec.get("dir") or Path(self.trial_paths.trial_dir) / "marks" / rec["mark"])
            if vdir.is_dir():
                shutil.copytree(vdir, dest, dirs_exist_ok=True)
            rec["dir"] = str(dest)
        except Exception as e:  # noqa: BLE001
            rec.update(status="failed", error=f"{type(e).__name__}: {e}"[:300])
        self._ledger(event="mark_graded", **rec)

    def _verifier_ran_without_checkout(self) -> bool:
        """After the verifier: True, logged and ledgered (verifier_without_checkout), when it ran (left output in
        <trial>/verifier) without main checked out into the workdir. Never raises."""
        if self.team_verify_checkout is not None:
            return False
        try:
            vdir = Path(self.trial_paths.verifier_dir)
            ran = vdir.is_dir() and any(vdir.iterdir())
        except Exception:  # noqa: BLE001
            return False
        if ran:
            self.logger.error("multi-agent-harness-team: the final verifier ran without main checked out into %s "
                              "(no verify_checkout in the ledger); its reward is not the team's main",
                              self.team_workdir)
            self._ledger(event="verifier_without_checkout", workdir=self.team_workdir)
        return ran

    def _check_step(self, r: ExecResult, what: str, timeout: int) -> None:
        """Raise naming the step, its exit code and the tail of its output, redacted."""
        if r.return_code == 0:
            return
        hint = f" (timed out after {timeout}s)" if r.return_code == 124 else ""
        out = self._redact((r.stdout or "").strip())[-300:]
        err = self._redact((r.stderr or "").strip())[-300:]
        raise RuntimeError(f"{what} failed with exit {r.return_code}{hint}; stdout: {out or '(empty)'}; "
                           f"stderr: {err or '(empty)'}")

    def _fresh_path(self, name: str) -> Path:
        """`name` in the trial dir, or name-1, name-2, ... when it exists: never overwrite."""
        d = Path(self.trial_paths.trial_dir)
        stem, dot, ext = name.partition(".")
        path, i = d / name, 0
        while path.exists():
            i += 1
            path = d / f"{stem}-{i}{dot}{ext}"
        return path

    async def save_team_work(self) -> Path | None:
        """Best effort, never raises: a bundle of the team repo (all refs) plus the board and proxy
        logs, downloaded from the hub straight into the trial dir. Independent of export_artifacts,
        so the hub's copy of the team's work survives the hub being deleted. Returns the bundle path."""
        if self.hub.sb is None:
            return None
        path = self._fresh_path("team.bundle")
        try:
            await self._pull_team_work(SAVE_REMOTE_DIR, path, self._fresh_path, SAVE_MIN_FREE_BYTES, "save team repo")
        except Exception as e:  # noqa: BLE001 - best effort; the caller carries on either way
            self.logger.error("multi-agent-harness-team: saving the team repo to the trial dir failed: %s",
                              self._redact(f"{type(e).__name__}: {e}")[:600])
            self._ledger(event="team_work_save_failed", error=self._redact(f"{type(e).__name__}: {e}")[:300])
            return None
        nbytes = path.stat().st_size if path.exists() else 0
        self.logger.info("multi-agent-harness-team: saved the team repo to %s (%d bytes)", path, nbytes)
        self._ledger(event="team_work_saved", file=path.name, bytes=nbytes)
        self.team_work_saved = path
        return path

    async def checkpoint_hub(self, dest: Path) -> int:
        """The hub's part of a checkpoint into dest: team.bundle (all refs), board-events.jsonl and
        proxy-usage.jsonl. Raises on failure, or if the bundle would leave the host under
        checkpoint.MIN_FREE_BYTES. Returns the bundle size. Also saves refreshed logins back."""
        await self.save_logins()
        size = await self._pull_team_work(CHECKPOINT_REMOTE_DIR, dest / "team.bundle", lambda n: dest / n,
                                          checkpoint.MIN_FREE_BYTES, "checkpoint")
        await self._pull_hub_state(dest)
        return size

    async def _pull_hub_state(self, dest: Path) -> None:
        """--ek hub_state_dirs: those hub folders, packed as root with their modes, into dest/hub-state.tgz.
        A folder that does not exist yet is left out; none at all is a warning. Raises if packing or the
        download fails, so a checkpoint never silently lacks the task's state."""
        self.team_hub_state_saved = None
        if not self.hub_state_dirs:
            return
        dirs = " ".join(shlex.quote(d) for d in self.hub_state_dirs)
        cmd = (f'umask 077; set --; for d in {dirs}; do [ -e "$d" ] && set -- "$@" "${{d#/}}"; done; '
               # GNU tar exits 1 when a file changed while it was read (a task's office adding a job or appending to
               # its ledger mid-pack): accept it, the archive is complete. A file rewritten in place during the pack
               # can still come out cut, so tasks should write their state files whole (temp file, then rename).
               f'[ $# -gt 0 ] || {{ echo none; exit 0; }}; tar --warning=no-file-changed -czpf {STATE_REMOTE} -C / "$@"; '
               f'rc=$?; [ $rc -le 1 ] || exit $rc; printf "%s\\n" "$@"')
        try:
            r = await self.hub.sh(cmd, 600)
            self._check_step(r, "checkpoint: pack hub_state_dirs", 600)
            found = [l for l in (r.stdout or "").strip().splitlines() if l]
            if found == ["none"]:
                self.logger.warning("multi-agent-harness-team: checkpoint: none of hub_state_dirs %s exists yet",
                                    self.hub_state_dirs)
                return
            await asyncio.wait_for(self.hub.download_file(STATE_REMOTE, dest / checkpoint.HUB_STATE), SAVE_DOWNLOAD_SEC)
            self.team_hub_state_saved = ["/" + d for d in found]
        finally:
            await self.hub._quiet(f"rm -f {STATE_REMOTE}")

    async def _pull_team_work(self, remote_dir: str, bundle: Path, log_path, min_free: int, what: str) -> int:
        """Bundle the team repo on the hub and download it to `bundle`, then the board and proxy logs
        to log_path(name). Raises if the bundle fails or would leave the host under min_free bytes.
        A log that won't download is only a warning: the proxy writes none until a model call goes
        through it, which under claude-subscription is never. Returns the bundle size."""
        remote = f"{remote_dir}/team.bundle"
        try:
            r = await self.hub.sh(f"mkdir -p {remote_dir} && git -c safe.directory='*' --git-dir={TEAM_GIT} "
                                  f"bundle create {remote} --all && stat -c %s {remote}", BUNDLE_SEC)
            self._check_step(r, f"{what}: bundle", BUNDLE_SEC)
            size = (r.stdout or "").strip().splitlines()[-1:]
            size = int(size[0]) if size and size[0].isdigit() else 0
            free = shutil.disk_usage(bundle.parent).free
            if free - size < min_free:
                raise RuntimeError(f"bundle is {size} bytes and the host has {free} free; not downloading")
            await asyncio.wait_for(self.hub.download_file(remote, bundle), SAVE_DOWNLOAD_SEC)
        finally:
            await self.hub._quiet(f"rm -rf {remote_dir}")
        for src, name in ((self.team_board_log_path, "board-events.jsonl"),
                          (self.team_proxy_log_path, "proxy-usage.jsonl")):
            try:
                await asyncio.wait_for(self.hub.download_file(src, log_path(name)), SAVE_DOWNLOAD_SEC)
            except Exception as e:  # noqa: BLE001
                self.logger.warning("multi-agent-harness-team: %s: downloading %s failed: %s", what, name,
                                    type(e).__name__)
        return size

    def _redact(self, text: str) -> str:
        for s in self._secrets:
            text = text.replace(s, "***")
        return text

    async def save_logins(self) -> None:
        """Best effort: copy each subscription login the hub's proxy refreshed back to its host file
        (upstream.save_refreshed_login), so the next run and the host's own codex keep working."""
        up = getattr(self, "_upstream", None)
        if up is None or not up.logins or self.hub.sb is None:
            return
        with tempfile.TemporaryDirectory(prefix="mah-logins-") as tmp:
            for i, (raw, path) in enumerate(zip(up.logins, up.login_paths), start=1):
                local = Path(tmp) / f"login-{i}.json"
                try:
                    await asyncio.wait_for(self.hub.download_file(f"{SECRET_DIR}/codex-login-{i}.json", local),
                                           SAVE_DOWNLOAD_SEC)
                    outcome = upstream.save_refreshed_login(Path(path), raw, local.read_bytes())
                except Exception as e:  # noqa: BLE001
                    outcome = f"failed ({type(e).__name__})"
                if outcome == "saved":
                    up.logins[i - 1] = local.read_bytes()   # a later save compares against what the host now holds
                self.logger.info("multi-agent-harness-team: login %d: %s", i, outcome)

    async def stop(self, delete: bool):
        self.finish_final_mark()  # after the verifier, before Harbor writes result.json
        if delete and self.hub.sb is not None:
            await self.save_logins()
        if delete and self.team_checkpoint_hook is not None and self.hub.sb is not None:
            try:  # best effort; a no-op when the agent already wrote its final checkpoint
                await self.team_checkpoint_hook()
            except Exception as e:  # noqa: BLE001
                self.logger.warning("multi-agent-harness-team: checkpoint before delete failed: %s", type(e).__name__)
        if delete and self.team_work_saved is None:  # e.g. the agent phase failed before collecting
            await self.save_team_work()
        if not delete:
            ids = [m.sandbox_id for m in self.members if m.sb is not None]
            self.logger.warning("multi-agent-harness-team: delete=False, leaving sandboxes running: %s", ids)
            return
        await self._delete_all()

    async def _delete_all(self) -> None:
        """Agents first, then the hub; each member deletes only the sandbox it created."""
        live = [a for a in self.agents.values() if a.sb is not None]
        results = await asyncio.gather(*(a.stop(delete=True) for a in live), return_exceptions=True)
        for a, r in zip(live, results):
            if isinstance(r, BaseException):
                self.logger.error("multi-agent-harness-team: deleting %s failed: %s", a.name, r)
        if self.hub.sb is not None:
            await self.hub.stop(delete=True)

    async def set_agent_timeout(self, timeout_sec: float | None) -> None:
        """Re-derive the TTL from the agent's own timeout (TeamAgent's timeout_sec) and apply it
        to every sandbox already created. A ttl_minutes kwarg still wins."""
        if self._ttl_override or not timeout_sec:
            return
        ttl = ttl_for(timeout_sec)
        live = [m for m in self.members if m.sb is not None]
        targets = [self._ttl_for_role(m.role, ttl) for m in live]
        results = await asyncio.gather(*(m.set_ttl(t) for m, t in zip(live, targets)), return_exceptions=True)
        for m, t, r in zip(live, targets, results):
            if r is not True and t > m.ttl_minutes:
                self.logger.warning("multi-agent-harness-team: can't raise the TTL of %s to %d min (%s); it stays %d",
                                    m.name, t, r if isinstance(r, BaseException) else "backend has no call",
                                    m.ttl_minutes)
        self.ttl_minutes = ttl
        for m in self.members:
            if m.sb is None:
                m.ttl_minutes = self._ttl_for_role(m.role, ttl)

    # ---- main service (the hub)

    async def exec(self, command: str, cwd: str | None = None, env: dict[str, str] | None = None,
                   timeout_sec: int | None = None, user: str | int | None = None) -> ExecResult:
        return await self.hub.exec(command, cwd=cwd, env=self._merge_env(env), timeout_sec=timeout_sec,
                                   user=self._resolve_user(user))

    async def upload_file(self, source_path: Path | str, target_path: str):
        await self.hub.upload_file(source_path, target_path)

    async def upload_dir(self, source_dir: Path | str, target_dir: str):
        if str(target_dir).rstrip("/") == str(EnvironmentPaths.tests_dir):
            await self._checkout_before_verifier()
        await self.hub.upload_dir(source_dir, target_dir)

    async def _checkout_before_verifier(self) -> None:
        """Backstop, never raises: Harbor's shared verifier uploads the tests (through upload_dir) right before
        it runs. If main was never checked out into the workdir (the agent's end-of-run export did not run or
        failed), the verifier would grade whatever the workdir holds, the starter files at worst: check main
        out now, loudly. If that fails too, ledger it; finish_final_mark then fails the final mark."""
        if self.team_verify_checkout is not None or self.hub.sb is None:
            return
        self.logger.error("multi-agent-harness-team: the verifier is about to run and main was never checked "
                          "out into %s; checking it out now", self.team_workdir)
        try:
            await self.checkout_main()
        except Exception as e:  # noqa: BLE001 - the verifier runs either way; the final mark records it
            err = self._redact(f"{type(e).__name__}: {e}")[:300]
            self.logger.error("multi-agent-harness-team: checking main out before the verifier failed: %s", err)
            self._ledger(event="verify_checkout_failed", when="before_verifier", error=err)
            return
        self.team_verify_checkout_late = True
        self._ledger(event="verify_checkout_late", commit=self.team_verify_commit)

    async def download_file(self, source_path: str, target_path: Path | str):
        await self.hub.download_file(source_path, target_path)

    async def download_dir(self, source_dir: str, target_dir: Path | str):
        await self.hub.download_dir(source_dir, target_dir)

    async def is_dir(self, path: str, user: str | int | None = None) -> bool:
        return await self.hub.is_dir(path, user=self._resolve_user(user))

    async def is_file(self, path: str, user: str | int | None = None) -> bool:
        return await self.hub.is_file(path, user=self._resolve_user(user))

    # ---- per-service ops: agent-i goes to that agent's sandbox

    def _agent(self, service: str) -> SandboxMember:
        member = self.agents.get(service)
        if member is None:
            raise ServiceOperationsUnsupportedError(
                f"unknown service {service!r}; have main and {', '.join(self.team_agent_services)}")
        return member

    async def service_exec(self, command: str, *, service: str | None = None, cwd: str | None = None,
                           env: dict[str, str] | None = None, timeout_sec: int | None = None,
                           user: str | int | None = None) -> ExecResult:
        if self.is_main_service(service):
            return await self.exec(command, cwd=cwd, env=env, timeout_sec=timeout_sec, user=user)
        return await self._agent(service).exec(command, cwd=cwd, env=self._merge_env(env),
                                               timeout_sec=timeout_sec, user=self._resolve_user(user))

    async def service_upload_file(self, source_path: Path | str, target_path: str, *,
                                  service: str | None = None) -> None:
        if self.is_main_service(service):
            return await self.upload_file(source_path, target_path)
        await self._agent(service).upload_file(source_path, target_path)

    async def service_upload_dir(self, source_dir: Path | str, target_dir: str, *,
                                 service: str | None = None) -> None:
        if self.is_main_service(service):
            return await self.upload_dir(source_dir, target_dir)
        await self._agent(service).upload_dir(source_dir, target_dir)

    async def service_download_file(self, source_path: str, target_path: Path | str, *,
                                    service: str | None = None) -> None:
        if self.is_main_service(service):
            return await self.download_file(source_path, target_path)
        await self._agent(service).download_file(source_path, target_path)

    async def service_download_dir(self, source_dir: str, target_dir: Path | str, *,
                                   service: str | None = None) -> None:
        if self.is_main_service(service):
            return await self.download_dir(source_dir, target_dir)
        await self._agent(service).download_dir(source_dir, target_dir)


DaytonaTeamEnvironment = TeamEnvironment  # the pre-SDK name; backend defaults to daytona
