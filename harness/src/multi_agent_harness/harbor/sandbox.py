"""One SDK sandbox as a Harbor environment.

SandboxMember implements Harbor's BaseEnvironment over a single sandbox from the
Universal Sandbox Interface (bespokelabs-sandbox): create from the task's Dockerfile (or prebuilt image), exec, file and
dir transfer, network policy and delete. TeamEnvironment composes a hub and N agents of it, and
a separate verifier is one on its own.

The SDK's execute_command takes a command string and nothing else, so cwd, env, user and
timeout are shell-wrapped here, the way Harbor's own environments do it (env, timeout, cd, su).
stdout and stderr go to temp files in the sandbox and come back in one response, split on a
random marker. A command that may run longer than FOREGROUND_MAX_SEC runs detached (setsid
nohup) with its pid, output and exit code in files, and is polled with backoff, so no single
request blocks for minutes. Never pass secrets in env here: they land in the command string.

Backend notes live in provider_handle() and the two tables below it.
"""

from __future__ import annotations

import asyncio
import csv
import json
import math
import os
import re
import shlex
import sys
import tempfile
import threading
import time
import tomllib
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from bespokelabs.sandbox import AsyncSandbox, AsyncSandboxClient, SandboxNotFoundError, SandboxSessionState
from bespokelabs.sandbox import NetworkPolicy as SdkNetworkPolicy
from bespokelabs.sandbox.backends import BACKENDS
from bespokelabs.sandbox.exceptions import (ErrorOutcome, SandboxAuthenticationError, SandboxConfigurationError,
                                            SandboxError)
from tenacity import (AsyncRetrying, retry, retry_if_exception, stop_after_attempt, stop_after_delay,
                      wait_exponential, wait_random_exponential)

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import EnvironmentCapabilities
from harbor.environments.definition import should_use_prebuilt_docker_image
from harbor.environments.tar_transfer import (
    extract_dir_from_file,
    pack_dir_to_file,
    remote_pack_command,
    remote_unpack_command,
)
from harbor.models.task.config import NetworkMode, NetworkPolicy

DEFAULT_BACKEND = "daytona"
LEDGER_NAME = "sandbox-ledger.jsonl"  # in the trial dir: every sandbox id created and deleted
TTL_MARGIN_MIN = 30
FALLBACK_AGENT_TIMEOUT_SEC = 3600  # when the task sets no timeout_sec
FOREGROUND_MAX_SEC = 120  # longer or unbounded commands run detached and are polled
POLL_MIN_SEC, POLL_MAX_SEC = 0.5, 5.0
POLL_MAX_FAILURES = 10  # consecutive failed polls before giving up on a detached command
EXEC_DIR = "/tmp/multi-agent-harness-exec"

# Backends whose allow-lists take leading-wildcard hostnames (*.example.com).
WILDCARD_HOSTS = {"daytona"}


def create_options(backend: str, labels: dict[str, str], build_timeout_sec: float) -> dict[str, Any]:
    """Backend notes: what goes to the SDK's backend_options at create. On Daytona, harness sandboxes
    carry labels (multi-agent-harness-cleanup checks harbor.session_id), are ephemeral with no idle
    stop, and get the task's build timeout for the create. Ephemeral is required here: this region
    refuses anything else ("Only ephemeral sandboxes are permitted in this region", a 400), so a
    sandbox Daytona stops is deleted at once rather than kept for inspection. The lifetime bound is
    the TTL: the SDK maps timeout_secs to Daytona's ttl_minutes, which destroys the sandbox in any
    state, stopped and archived included (bespokelabs/sandbox/backends/daytona.py, the ttl_minutes note)."""
    if backend == "daytona":
        return {"labels": labels, "ephemeral": True, "auto_stop_interval": 0, "auto_delete_interval": 0,
                "create_timeout": round(build_timeout_sec)}
    return {}


def ttl_for(timeout_sec: float | None) -> int:
    """Sandbox TTL in minutes: the timeout rounded up, plus a margin for start and verify."""
    sec = timeout_sec if timeout_sec else FALLBACK_AGENT_TIMEOUT_SEC
    return math.ceil(sec / 60) + TTL_MARGIN_MIN


def task_timeout(environment_dir: Path, section: str = "agent") -> float | None:
    """[<section>] timeout_sec from the task.toml next to the given dir (environment/ or tests/)."""
    try:
        cfg = tomllib.loads((Path(environment_dir).parent / "task.toml").read_text())
        t = (cfg.get(section) or {}).get("timeout_sec")
        return float(t) if t else None
    except (OSError, ValueError, TypeError):
        return None


def append_ledger(path: Path, logger: Any, **entry: Any) -> None:
    """Append one line to the id ledger and fsync it, so a crash still leaves the ids."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), **entry}) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        logger.error("multi-agent-harness: ledger write failed (%s): %s", e, entry)


def as_dict(value: Any, what: str) -> dict:
    """An --ek value that should be a dict; Harbor passes JSON through, a string is parsed."""
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError(f"{what} must be a JSON object, got {value!r}")
    return value


# ---- backends

_clients: dict[tuple[str, int, bool], AsyncSandboxClient] = {}


def sdk_client(backend: str, *, gpu: bool = False) -> AsyncSandboxClient:
    """Cache CPU and GPU clients separately per backend/event loop, sharing auth within each."""
    key = (backend, id(asyncio.get_running_loop()), gpu)
    if key not in _clients:
        if gpu and backend == "daytona":
            from .daytona_gpu import DaytonaGpuAsyncClient
            _clients[key] = DaytonaGpuAsyncClient(backend)
        else:
            _clients[key] = AsyncSandboxClient(backend)
    return _clients[key]


def backend_network_modes(backend: str) -> frozenset[str]:
    if backend not in BACKENDS:
        raise ValueError(f"unknown sandbox backend {backend!r}; have {', '.join(BACKENDS)}")
    return frozenset(getattr(BACKENDS[backend], "network_modes", ())) | {"public"}


def backend_switches_network(backend: str) -> bool:
    """Whether the backend's sessions implement set_network (a policy change after start)."""
    module = sys.modules.get(BACKENDS[backend].__module__)
    return any(isinstance(c, type) and c.__module__ == module.__name__ and hasattr(c, "set_network")
               for c in vars(module).values()) if module else False


def capabilities_for(backend: str) -> EnvironmentCapabilities:
    """Harbor capabilities from what the backend can enforce. Allow-lists take hostnames only:
    agents' lists always hold the hub's host, and an allow-list can't mix names with IPs
    (Daytona's can't, and a CIDR to a preview host would open every preview host)."""
    modes = backend_network_modes(backend)
    allow = "allowlist" in modes
    return EnvironmentCapabilities(
        disable_internet="none" in modes or allow, network_allowlist=allow,
        network_allowlist_hostnames=allow,
        network_allowlist_wildcard_hostnames=allow and backend in WILDCARD_HOSTS,
        dynamic_network_policy=backend_switches_network(backend))


def to_sdk_policy(policy: NetworkPolicy) -> SdkNetworkPolicy:
    """Harbor's NetworkPolicy as the SDK's. An allow-list with no hosts blocks everything."""
    if policy.network_mode == NetworkMode.PUBLIC:
        return SdkNetworkPolicy("public")
    if policy.network_mode == NetworkMode.NO_NETWORK or not policy.allowed_hosts:
        return SdkNetworkPolicy("none")
    return SdkNetworkPolicy("allowlist", domains=tuple(policy.allowed_hosts))


def provider_handle(sb: Any) -> Any | None:
    """The provider's own sandbox object behind an SDK sandbox (AsyncSandbox._sandbox._session
    ._sandbox), for what the SDK has no public call for: reading labels and state, and changing
    the TTL of a running sandbox (Daytona). Every caller handles None."""
    session = getattr(getattr(sb, "_sandbox", None), "_session", None)
    return getattr(session, "_sandbox", None)


def docker_container(sb: Any) -> Any | None:
    """The docker-py Container behind an SDK sandbox on the docker backend (AsyncSandbox._sandbox
    ._session._container), for resizing a running sandbox, which the SDK has no call for."""
    session = getattr(getattr(sb, "_sandbox", None), "_session", None)
    return getattr(session, "_container", None)


# Backends whose running sandboxes can be given more CPU and memory (SandboxMember.resize).
RESIZABLE_BACKENDS = frozenset({"docker", "daytona"})


async def resume(backend: str, state: dict) -> AsyncSandbox:
    return await sdk_client(backend).resume(SandboxSessionState(backend=backend, data=state))


NOT_FOUND = "not_found"
# Provider states in which an agent sandbox runs nothing and will not again (Daytona's names).
AGENT_GONE_STATES = frozenset({NOT_FOUND, "stopping", "stopped", "error", "archiving", "archived",
                               "destroying", "destroyed"})


async def sandbox_status(backend: str, state: dict) -> str | None:
    """The backend's current state for the sandbox, lowercased: NOT_FOUND when it no longer
    exists, None when the backend shows no state."""
    try:
        sb = await resume(backend, state)
    except SandboxNotFoundError:
        return NOT_FOUND
    s = getattr(provider_handle(sb), "state", None)
    s = getattr(s, "value", s)
    return str(s).lower() if s else None


async def is_gone(backend: str, state: dict) -> bool:
    """True when the sandbox no longer exists or is being destroyed."""
    s = await sandbox_status(backend, state) or ""
    return s == NOT_FOUND or any(x in s for x in ("destroy", "delet"))


# ---- exec wrapping

def shell_command(command: str, cwd: str | None = None, env: dict[str, str] | None = None,
                  timeout_sec: int | None = None, user: str | int | None = None) -> str:
    """POSIX sh for `command` with cwd, env, timeout and user applied, in the order Harbor's own
    environments use (env, then timeout, then cd, then su). The command itself runs under bash
    when the image has it, else sh, like Harbor's `bash -c`."""
    cmd = f'"$(command -v bash || echo sh)" -c {shlex.quote(command)}'
    if env:
        cmd = "env " + " ".join(f"{k}={shlex.quote(str(v))}" for k, v in env.items()) + " " + cmd
    if timeout_sec:
        cmd = f"timeout {int(math.ceil(timeout_sec))} {cmd}"
    if cwd:
        cmd = f"cd {shlex.quote(cwd)} && {cmd}"
    if user is not None:
        who = f"$(getent passwd {int(user)} | cut -d: -f1)" if isinstance(user, int) else shlex.quote(user)
        cmd = f"su {who} -s /bin/sh -c {shlex.quote(cmd)}"
    return cmd


def foreground_script(inner: str, mark: str) -> str:
    """Run `inner`, print stdout, the marker, then stderr, and exit with its code."""
    return (f'd=$(mktemp -d) || exit 125; ({inner}) >"$d/o" 2>"$d/e" </dev/null; rc=$?; '
            f'cat "$d/o"; printf %s {mark}; cat "$d/e"; rm -rf "$d"; exit $rc')


def background_script(inner: str, job_dir: str) -> str:
    """Start `inner` detached; it leaves pid, o, e and finally rc in job_dir."""
    run = (f"echo $$ >{job_dir}/pid; ({inner}) >{job_dir}/o 2>{job_dir}/e </dev/null; "
           f"echo $? >{job_dir}/rc.tmp; mv {job_dir}/rc.tmp {job_dir}/rc")
    return (f"mkdir -p {job_dir} && if command -v setsid >/dev/null 2>&1; then s=setsid; else s=; fi; "
            f"$s nohup sh -c {shlex.quote(run)} >/dev/null 2>&1 </dev/null & echo started")


def split_output(text: str, mark: str) -> tuple[str, str]:
    out, sep, err = (text or "").partition(mark)
    return (out, err) if sep else (text or "", "")


_ENV_SWAP_LOCK = threading.Lock()


def serialize_daytona_env_swap() -> None:
    """Daytona's isolated_env clears os.environ and restores a copy taken on entry. The SDK
    runs creates in worker threads, and an image with a build context (any COPY) goes through
    it once per create, so two concurrent creates can copy an already-cleared environ and
    restore that: the harness then loses every variable, model API keys included, and the
    hub proxy starts with none. Holding one lock across each swap keeps the restore whole."""
    try:
        from daytona._sync import object_storage as sync_os
        from daytona._async import object_storage as async_os
    except ImportError:
        return
    original = sync_os.isolated_env
    if getattr(original, "_multi_agent_harness_locked", False):
        return

    @contextmanager
    def locked(temp_env=None):
        with _ENV_SWAP_LOCK, original(temp_env):
            yield

    locked._multi_agent_harness_locked = True
    sync_os.isolated_env = async_os.isolated_env = locked


serialize_daytona_env_swap()


_retry = retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10), reraise=True)

# Control-plane calls (create, network policy, delete) get a patient retry instead: under load the
# backend's API fails for longer than _retry's ten seconds (on Daytona a 502 while a sandbox starts,
# set_network failing at the switch to the verifier phase), and one such failure there costs the
# whole trial. Waits are jittered so the sandboxes of many trials don't retry in step.
PATIENT_SEC = 300
_patient_wait = wait_random_exponential(multiplier=2, max=60)


RETRYABLE_4XX = (408, 429)  # request timeout, rate limit


def _status(exc: BaseException) -> int | None:
    status = (getattr(exc, "context", None) or {}).get("provider_status")
    return status if isinstance(status, int) else None


def _refusal(exc: BaseException) -> bool:
    """An error no retry fixes: credentials, configuration, a missing sandbox, or a 4xx (a quota)
    other than 408 and 429."""
    if isinstance(exc, (SandboxAuthenticationError, SandboxConfigurationError, SandboxNotFoundError)):
        return True
    status = _status(exc)
    return status is not None and 400 <= status < 500 and status not in RETRYABLE_4XX


def _retry_after(exc: BaseException) -> float | None:
    """Seconds from a Retry-After on a 408 or 429: the SDK error's context, else the provider's
    error it was raised from (Daytona keeps response headers there)."""
    if _status(exc) not in RETRYABLE_4XX:
        return None
    ctx = getattr(exc, "context", None) or {}
    headers = {str(k).lower(): v for k, v in (getattr(exc.__cause__, "headers", None) or {}).items()}
    value = next((v for v in (ctx.get("retry_after"), ctx.get("retry-after"), headers.get("retry-after"))
                  if v is not None), None)
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        return max(0.0, (parsedate_to_datetime(str(value)) - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


def _patient_sleep(state) -> float:
    """The server's Retry-After when it sent one, else the jittered backoff; never past the
    patient window."""
    after = _retry_after(state.outcome.exception())
    wait = after if after is not None else _patient_wait(state)
    return max(0.0, min(wait, PATIENT_SEC - state.seconds_since_start))


def _retry_idempotent(exc: BaseException) -> bool:
    """set_network and destroy can be repeated as they are, so any failure but a refusal."""
    return isinstance(exc, Exception) and not _refusal(exc)


def _retry_create(exc: BaseException) -> bool:
    """A create is repeated only when the SDK proved the failed one left nothing behind (outcome
    failed, its own cleanup done); with the outcome unknown a retry could leave a second sandbox."""
    return isinstance(exc, SandboxError) and exc.outcome is ErrorOutcome.FAILED and not _refusal(exc)


def _patient(should_retry, what: str, logger) -> AsyncRetrying:
    def log(state):
        logger.warning("multi-agent-harness: %s failed (attempt %d), retrying in %.0fs: %s", what,
                       state.attempt_number, state.next_action.sleep, state.outcome.exception())
    return AsyncRetrying(stop=stop_after_delay(PATIENT_SEC), wait=_patient_sleep,
                         retry=retry_if_exception(should_retry), before_sleep=log, reraise=True)


class SandboxMember(BaseEnvironment):
    """One SDK sandbox (the hub, agent-i or a separate verifier) as a Harbor environment, plus
    harness labels, a TTL, and the trial's id ledger."""

    def __init__(self, *args, role: str = "verifier", name: str = "verifier",
                 backend: str = DEFAULT_BACKEND, ttl_minutes: int | None = None,
                 backend_options: dict | str | None = None, labels: dict | str | None = None,
                 gpu: bool = False, gpu_type: str | None = None, **kwargs):
        self.role, self.name, self.backend = role, name, str(backend).lower()
        self.gpu, self.gpu_type = gpu, gpu_type
        self.observed_gpus: list[dict[str, Any]] = []
        if gpu:
            if self.backend != "daytona" or role != "agent":
                raise ValueError("GPU allocation is supported only for Daytona agent sandboxes")
            from .daytona_gpu import validate_gpu_type
            validate_gpu_type(gpu_type)
        backend_network_modes(self.backend)  # unknown backend fails here
        self.backend_options = as_dict(backend_options, "backend_options")
        conflicts = {"resources", "gpu", "gpu_type", "gpuType", "device_requests"} & self.backend_options.keys()
        if gpu and conflicts:
            raise ValueError(f"backend_options cannot override GPU/resources allocation: {sorted(conflicts)}; use n_gpus/gpu_type")
        self.labels = as_dict(labels, "labels")
        super().__init__(*args, **kwargs)
        if gpu and self.task_env_config.gpu_types:
            raise ValueError("task gpu_types is unsupported; use --ek gpu_type with --ek n_gpus")
        self.ledger_path = Path(self.trial_paths.trial_dir) / LEDGER_NAME
        self.ttl_minutes = ttl_minutes or ttl_for(task_timeout(self.environment_dir, "verifier"))
        self.sb: AsyncSandbox | None = None
        self.sandbox_id: str | None = None
        self._state: dict | None = None
        self.calls, self.call_sec, self.polls = 0, 0.0, 0

    @staticmethod
    def type() -> str:
        return "multi-agent-harness-sandbox"

    @property
    def capabilities(self) -> EnvironmentCapabilities:
        return capabilities_for(self.backend)

    @property
    def sandbox(self) -> AsyncSandbox:
        if self.sb is None:
            raise RuntimeError(f"{self.name}: sandbox not started")
        return self.sb

    def _validate_definition(self):
        if not (Path(self.environment_dir) / "Dockerfile").is_file() and not self.task_env_config.docker_image:
            raise FileNotFoundError(f"{self.environment_dir}/Dockerfile not found and the task names no "
                                    "docker_image")

    def _validate_gpu_support(self):
        if self._effective_gpus:
            raise ValueError("task gpus/--override-gpus describes GPUs per environment; use --ek n_gpus for agent allocation. "
                             "The hub and verifier are CPU-only.")

    @property
    def resources(self) -> dict[str, Any]:
        return {"cpus": self._effective_cpus, "memory_mb": self._effective_memory_mb,
                "storage_mb": self._effective_storage_mb, "gpus": int(self.gpu),
                "gpu_type": self.gpu_type, "observed_gpus": list(self.observed_gpus)}

    # ---- lifecycle

    def create_spec(self, force_build: bool = False) -> dict[str, Any]:
        """Keyword arguments for the SDK's create()."""
        env_dir = Path(self.environment_dir)
        labels = {**self.labels, "harbor.managed": "true", "harbor.environment_name": self.environment_name,
                  "harbor.session_id": self.session_id, "multi-agent-harness.role": self.role, "multi-agent-harness.name": self.name}
        options = {**create_options(self.backend, labels, self.task_env_config.build_timeout_sec),
                   **self.backend_options}
        spec: dict[str, Any] = {"timeout_secs": self.ttl_minutes * 60, "env_vars": self._startup_env() or None,
                                "network": to_sdk_policy(self.network_policy), "backend_options": options}
        image = self.task_env_config.docker_image
        if image and should_use_prebuilt_docker_image(env_dir, docker_image=image, force_build=force_build):
            spec["image"] = image
        else:
            spec["dockerfile"], spec["context_dir"] = str(env_dir / "Dockerfile"), str(env_dir)
        if self._effective_cpus:
            spec["cpu"] = float(self._effective_cpus)
        if self._effective_memory_mb:
            spec["memory_mb"] = int(self._effective_memory_mb)
        if self._effective_storage_mb:
            spec["disk_mb"] = int(self._effective_storage_mb)
        if self.gpu:
            from .daytona_gpu import DEFAULT_GPU
            spec["gpu"] = self.gpu_type or DEFAULT_GPU
        return spec

    async def start(self, force_build: bool) -> None:
        async for attempt in _patient(_retry_create, f"create {self.name}", self.logger):
            with attempt:
                await self._create(force_build)
        if self.task_env_config.workdir:
            await self.check(f"mkdir -p {shlex.quote(self.task_env_config.workdir)}", "create workdir")
        await self.ensure_dirs(self._mount_targets(writable_only=True))
        await self._upload_environment_dir_after_start()
        if self.gpu:
            await self._verify_gpu()

    async def _verify_gpu(self) -> None:
        raw_count = getattr(provider_handle(self.sandbox), "gpu", None)
        if raw_count is not None and raw_count != 1:
            raise RuntimeError(f"{self.name}: provider reports {raw_count!r} GPUs; requested exactly one")
        result = await self.sh("nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits", 30)
        try:
            rows = list(csv.reader((result.stdout or "").strip().splitlines()))
            if result.return_code != 0 or len(rows) != 1 or len(rows[0]) != 2:
                raise ValueError("expected exactly one visible NVIDIA GPU")
            name, memory = (v.strip() for v in rows[0])
            memory_mb = int(memory)
            if not name or memory_mb <= 0:
                raise ValueError("invalid device name or memory")
            normalize = lambda value: re.sub(r"[^A-Z0-9]", "", value.upper())
            if self.gpu_type and normalize(self.gpu_type) not in normalize(name):
                raise ValueError(f"requested {self.gpu_type}, observed {name}")
        except (ValueError, csv.Error) as exc:
            raise RuntimeError(f"{self.name}: GPU verification failed: {exc}; "
                               f"{(result.stderr or result.stdout or '')[-300:]}") from exc
        self.observed_gpus = [{"name": name, "memory_mb": memory_mb}]
        append_ledger(self.ledger_path, self.logger, event="gpu_verified", name=self.name,
                      resources=self.resources)

    async def _create(self, force_build: bool) -> None:
        # Shielded: a cancel mid-create must still leave us the id to delete.
        task = asyncio.ensure_future(sdk_client(self.backend, gpu=self.gpu).create(**self.create_spec(force_build)))
        try:
            self._started(await asyncio.shield(task))
        except asyncio.CancelledError:
            try:
                self._started(await asyncio.wait_for(task, 120))
            except BaseException:  # noqa: BLE001
                pass
            raise

    def _started(self, sb: AsyncSandbox) -> None:
        self.sb = sb
        self._state = sb.session_state().data
        self.sandbox_id = str(self._state.get("sandbox_id") or self._state.get("id") or self._state)
        append_ledger(self.ledger_path, self.logger, role=self.role, name=self.name, id=self.sandbox_id,
                      session_id=self.session_id, backend=self.backend, state=self._state, resources=self.resources)
        self.logger.info("multi-agent-harness: created %s %s on %s", self.name, self.sandbox_id, self.backend)

    async def stop(self, delete: bool):
        if self.sb is None:
            return
        if not delete:
            self.logger.warning("multi-agent-harness: delete=False, leaving %s %s running", self.name, self.sandbox_id)
            return
        sb, self.sb = self.sb, None
        error = None
        try:
            async for attempt in _patient(_retry_idempotent, f"delete {self.name}", self.logger):
                with attempt:
                    await sb.destroy()
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {str(e)[:200]}"
        stats = {"exec_calls": self.calls, "exec_sec": round(self.call_sec, 1), "polls": self.polls}
        if error is None or await self._gone():
            append_ledger(self.ledger_path, self.logger, deleted=self.sandbox_id, name=self.name, **stats)
        else:
            append_ledger(self.ledger_path, self.logger, delete_failed=self.sandbox_id, name=self.name,
                          error=error, **stats)
            self.logger.error("multi-agent-harness: deleting %s %s failed: %s", self.name, self.sandbox_id, error)

    async def _gone(self) -> bool:
        try:
            return await is_gone(self.backend, self._state or {})
        except Exception:  # noqa: BLE001
            return False

    async def status(self) -> str | None:
        """sandbox_status for this sandbox; None when the lookup itself fails."""
        try:
            return await sandbox_status(self.backend, self._state or {})
        except Exception:  # noqa: BLE001
            return None

    async def set_ttl(self, minutes: int) -> bool:
        """Change the TTL of the running sandbox where the backend allows it (Daytona)."""
        raw = provider_handle(self.sb)
        if raw is None or not hasattr(raw, "set_ttl"):
            return False
        await asyncio.to_thread(raw.set_ttl, minutes)
        self.ttl_minutes = minutes
        return True

    async def resize(self, cpus: int | None, memory_mb: int | None) -> None:
        """Give the running sandbox this many CPUs and this much memory (None keeps the current
        value). Docker changes the container's cgroup limits in place (`docker update`), up or
        down; memory+swap is set to twice the memory, Docker's own default at create. Daytona's
        hot resize takes only increases, in whole GiB. Raises when the backend can't do it."""
        if self.sb is None:
            raise RuntimeError(f"{self.name}: sandbox not started")
        if self.backend == "docker":
            container = docker_container(self.sb)
            if container is None:
                raise RuntimeError(f"{self.name}: no Docker container behind the sandbox")
            update: dict[str, Any] = {}
            if cpus:
                update |= {"cpu_period": 100_000, "cpu_quota": int(cpus * 100_000)}
            if memory_mb:
                update |= {"mem_limit": f"{int(memory_mb)}m", "memswap_limit": f"{2 * int(memory_mb)}m"}
            await asyncio.to_thread(container.update, **update)
        elif self.backend == "daytona":
            raw = provider_handle(self.sb)
            if raw is None or not hasattr(raw, "resize"):
                raise RuntimeError(f"{self.name}: this Daytona SDK has no sandbox resize")
            from daytona import Resources
            await asyncio.to_thread(raw.resize, Resources(cpu=int(cpus) if cpus else None,
                                                          memory=math.ceil(memory_mb / 1024) if memory_mb else None))
        else:
            raise RuntimeError(f"{self.name}: backend {self.backend} can't resize a running sandbox")
        if cpus:
            self.task_env_config.cpus = int(cpus)
        if memory_mb:
            self.task_env_config.memory_mb = int(memory_mb)

    # ---- network

    async def _apply_network_policy(self, network_policy: NetworkPolicy) -> None:
        async for attempt in _patient(_retry_idempotent, f"set_network on {self.name}", self.logger):
            with attempt:
                await self.sandbox.set_network(to_sdk_policy(network_policy))

    # ---- exec

    async def _call(self, command: str):
        self.calls += 1
        t0 = time.monotonic()
        try:
            return await self.sandbox.execute_command(command)
        finally:
            self.call_sec += time.monotonic() - t0

    async def run(self, inner: str, timeout_sec: int | None = None) -> ExecResult:
        """Run a shell command (already wrapped) and split its output."""
        if timeout_sec and timeout_sec <= FOREGROUND_MAX_SEC:
            mark = f"@@multi-agent-harness-{uuid4().hex}@@"
            r = await self._call(f"sh -c {shlex.quote(foreground_script(inner, mark))}")
            out, err = split_output(r.stdout, mark)
            return ExecResult(stdout=out, stderr=err, return_code=int(r.exit_code))
        return await self._run_detached(inner, timeout_sec)

    async def _run_detached(self, inner: str, timeout_sec: int | None) -> ExecResult:
        job = f"{EXEC_DIR}/{uuid4().hex}"
        r = await self._call(f"sh -c {shlex.quote(background_script(inner, job))}")
        if r.exit_code != 0 or "started" not in (r.stdout or ""):
            raise RuntimeError(f"{self.name}: could not start a detached command: {(r.stdout or '')[-300:]}")
        deadline = time.monotonic() + timeout_sec + 60 if timeout_sec else None
        delay, failures, finished = POLL_MIN_SEC, 0, False
        try:
            while True:
                await asyncio.sleep(delay)
                delay = min(POLL_MAX_SEC, delay * 1.5)
                try:
                    self.polls += 1
                    rc = (await self._call(f"cat {job}/rc 2>/dev/null || true")).stdout.strip()
                    failures = 0
                except Exception:  # noqa: BLE001 - the API can drop a call, e.g. during a policy switch
                    failures += 1
                    if failures >= POLL_MAX_FAILURES:
                        raise
                    continue
                if rc:
                    break
                if deadline and time.monotonic() > deadline:
                    return ExecResult(stdout="", stderr=f"timed out after {timeout_sec}s", return_code=124)
            mark = f"@@multi-agent-harness-{uuid4().hex}@@"
            r = await _retry(self._call)(f"sh -c {shlex.quote(f'cat {job}/o; printf %s {mark}; cat {job}/e')}")
            out, err = split_output(r.stdout, mark)
            finished = True
            return ExecResult(stdout=out, stderr=err, return_code=int(rc) if rc.lstrip("-").isdigit() else 1)
        finally:
            kill = "" if finished else (f"p=$(cat {job}/pid 2>/dev/null) && "
                                         f"{{ kill -TERM -- -$p 2>/dev/null || kill -TERM $p; }}; ")
            await asyncio.shield(self._quiet(f"sh -c {shlex.quote(kill + f'rm -rf {job}')}"))

    async def _quiet(self, command: str) -> None:
        try:
            await self._call(command)
        except Exception:  # noqa: BLE001
            pass

    async def exec(self, command: str, cwd: str | None = None, env: dict[str, str] | None = None,
                   timeout_sec: int | None = None, user: str | int | None = None) -> ExecResult:
        inner = shell_command(command, cwd=cwd or self.task_env_config.workdir, env=self._merge_env(env),
                              timeout_sec=timeout_sec, user=self._resolve_user(user))
        return await self.run(inner, timeout_sec)

    # ---- helpers for the harness's own commands (as the sandbox user, no task env)

    async def sh(self, command: str, timeout_sec: int = 60) -> ExecResult:
        return await self.run(shell_command(command, timeout_sec=timeout_sec), timeout_sec)

    async def check(self, command: str, what: str, timeout_sec: int = 120) -> str:
        r = await self.sh(command, timeout_sec)
        if r.return_code != 0:
            raise RuntimeError(f"{self.name}: {what} failed (exit {r.return_code}): "
                               f"{(r.stderr or r.stdout or '')[-500:]}")
        return r.stdout or ""

    async def probe(self, url: str, header_file: str | None = None, timeout_sec: int = 8) -> int:
        """curl's exit code for `url`: 0 when any HTTP response came back."""
        h = f"-H @{header_file} " if header_file else ""
        r = await self.sh(f"curl -s -o /dev/null -m {timeout_sec} {h}{shlex.quote(url)}", timeout_sec + 30)
        return r.return_code

    async def spawn(self, command: str) -> None:
        """Start a long-running service, detached; its command redirects its own output."""
        r = await self._call("sh -c " + shlex.quote(
            "if command -v setsid >/dev/null 2>&1; then s=setsid; else s=; fi; "
            f"$s nohup sh -c {shlex.quote(command)} >/dev/null 2>&1 </dev/null & echo started"))
        if r.exit_code != 0:
            raise RuntimeError(f"{self.name}: spawn failed: {(r.stdout or '')[-300:]}")

    # ---- files

    async def upload_bytes(self, data: bytes, target_path: str) -> None:
        await _retry(self.sandbox.write_file)(target_path, data)

    async def upload_file(self, source_path: Path | str, target_path: str):
        await _retry(self.sandbox.upload_file)(str(source_path), target_path)

    async def download_file(self, source_path: str, target_path: Path | str):
        Path(target_path).parent.mkdir(parents=True, exist_ok=True)
        await _retry(self.sandbox.download_file)(source_path, str(target_path))

    async def upload_dir(self, source_dir: Path | str, target_dir: str):
        """One tarball, unpacked with --no-same-owner: as root, tar would chown to the host uid,
        which Daytona's user namespace rejects (this is how artifacts arrive)."""
        if not Path(source_dir).is_dir():
            raise FileNotFoundError(f"Source directory {source_dir} does not exist")
        self.sandbox  # noqa: B018 - raises before any upload when not started
        remote = f"/tmp/multi_agent_harness_upload_{uuid4().hex}.tar.gz"
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "upload.tar.gz"
            pack_dir_to_file(Path(source_dir), archive)
            await self.upload_file(archive, remote)
        try:
            r = await self.sh(remote_unpack_command(remote, target_dir) + " --no-same-owner", 600)
            if r.return_code != 0:
                raise RuntimeError(f"Failed to extract uploaded archive into {target_dir}: {r.stderr or r.stdout}")
        finally:
            await self._quiet(f"rm -f {shlex.quote(remote)}")

    async def download_dir(self, source_dir: str, target_dir: Path | str):
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        remote = f"/tmp/multi_agent_harness_download_{uuid4().hex}.tar.gz"
        try:
            r = await self.sh(remote_pack_command(source_dir, remote), 600)
            if r.return_code != 0:
                raise RuntimeError(f"Failed to archive {source_dir} (needs tar and gzip): {r.stderr or r.stdout}")
            with tempfile.TemporaryDirectory() as tmp:
                archive = Path(tmp) / "download.tar.gz"
                await self.download_file(remote, archive)
                extract_dir_from_file(archive, target)
        finally:
            await self._quiet(f"rm -f {shlex.quote(remote)}")

