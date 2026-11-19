"""TeamEnvironment against a fake sandbox SDK: no network, no sandboxes. The shell wrapping
itself is run for real with the local sh."""

import asyncio
import json
import logging
import re
import subprocess
from types import SimpleNamespace

import pytest
from bespokelabs.sandbox import ExposedPort, SandboxNotFoundError, SandboxSessionState
from bespokelabs.sandbox import NetworkPolicy as SdkPolicy
from bespokelabs.sandbox.exceptions import ErrorOutcome, SandboxCreationError, SandboxExecutionError
from tenacity import wait_none

from harbor.environments.base import ServiceOperationsUnsupportedError
from harbor.models.task.config import EnvironmentConfig, NetworkMode, NetworkPolicy
from harbor.models.trial.paths import TrialPaths
from multi_agent_harness.harbor import sandbox as sbmod
from multi_agent_harness.harbor import upstream
from multi_agent_harness.harbor.environment import HUB_LIMITS_CMD as HUB_LIMITS
from multi_agent_harness.harbor.environment import DaytonaTeamEnvironment, SandboxMember, TeamEnvironment

NO_NET = NetworkPolicy(network_mode=NetworkMode.NO_NETWORK)
PUBLIC = NetworkPolicy()
DOCKERFILE = "FROM python:3.12-slim-bookworm\nRUN echo task-step\n"
JOB = re.compile(r"/tmp/multi-agent-harness-exec/[0-9a-f]+")
MARK = re.compile(r"printf %s (@@multi-agent-harness-[0-9a-f]+@@)")


class FakeSandbox:
    """An SDK AsyncSandbox that answers curl probes from its current network policy."""

    leaky = False  # set on a sandbox whose restriction never takes effect
    no_curl = False
    hub_down = False  # set on a sandbox that can't reach the hub

    def __init__(self, client, spec):
        self.client, self.spec = client, spec
        self.id = f"sb-{len(client.created)}"
        self.net: SdkPolicy = spec.get("network") or SdkPolicy()
        self.commands: list[str] = []
        self.uploads: dict[str, bytes] = {}
        self.jobs: dict[str, tuple[str, int]] = {}
        self.raw = SimpleNamespace(state="started", labels=spec["backend_options"].get("labels"),
                                   ttl=spec["timeout_secs"] // 60)
        self.raw.set_ttl = lambda m: setattr(self.raw, "ttl", m)
        self._sandbox = SimpleNamespace(_session=SimpleNamespace(_sandbox=self.raw))

    def host(self):
        return f"8080-{self.id}.proxy.test"

    def _reachable(self, url):
        host = re.match(r"https?://([^:/'\"]+)", url).group(1)
        if self.hub_down and url.endswith("/healthz"):
            return False
        if self.leaky or self.net.mode == "public":
            return True
        return self.net.mode == "allowlist" and host in self.net.domains

    def _answer(self, cmd):
        """(stdout, exit code) for the user command inside a wrapped one."""
        if "multi-agent-harness-board --check" in cmd and self.client.check_error:
            return self.client.check_error, 1
        if "command -v curl" in cmd and self.no_curl:
            return "", 1
        if "curl -s -o /dev/null" in cmd:
            return "", 0 if self._reachable(re.findall(r"https?://[^\s'\"]+", cmd)[-1]) else 6
        return self.id, 0

    async def execute_command(self, cmd, args=None):
        self.commands.append(cmd)
        job = JOB.search(cmd)
        if job and "rc.tmp" in cmd:  # detached start
            self.jobs[job.group()] = self._answer(cmd)
            return SimpleNamespace(stdout="started\n", exit_code=0)
        if job and "/rc 2>/dev/null" in cmd:  # poll
            return SimpleNamespace(stdout=f"{self.jobs[job.group()][1]}\n", exit_code=0)
        if job and MARK.search(cmd):  # fetch
            return SimpleNamespace(stdout=self.jobs[job.group()][0] + MARK.search(cmd).group(1), exit_code=0)
        if "nohup" in cmd or job:  # spawn, cleanup
            return SimpleNamespace(stdout="started\n", exit_code=0)
        out, rc = self._answer(cmd)
        return SimpleNamespace(stdout=out + MARK.search(cmd).group(1), exit_code=rc)

    async def write_file(self, path, data):
        self.uploads[path] = data

    async def upload_file(self, local, remote):
        self.uploads[remote] = open(local, "rb").read()

    async def download_file(self, remote, local):
        open(local, "wb").write(b"")

    async def expose_port(self, port):
        return ExposedPort(port=port, url=f"https://{port}-{self.id}.proxy.test",
                           headers={"X-Daytona-Preview-Token": f"PREVIEW-{self.id}"})

    async def set_network(self, policy):
        self.net = policy

    def session_state(self):
        return SandboxSessionState(backend="daytona", data={"sandbox_id": self.id})

    async def destroy(self):
        self.client.deleted.append(self.id)
        self.raw.state = "destroying"


class FakeClient:
    def __init__(self):
        self.created: list[FakeSandbox] = []
        self.deleted: list[str] = []
        self.check_error: str | None = None  # what the hub's `multi-agent-harness-board --check` fails with

    async def create(self, **spec):
        sb = FakeSandbox(self, spec)
        self.created.append(sb)
        return sb

    async def resume(self, state):
        sb = next((s for s in self.created if s.id == state.data["sandbox_id"]), None)
        if sb is None:
            raise SandboxNotFoundError("gone")
        return sb


@pytest.fixture
def client(monkeypatch):
    c = FakeClient()
    monkeypatch.setattr(sbmod, "sdk_client", lambda backend, **kw: c)
    return c


def make_env(tmp_path, n=3, name="environment", **kw):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "Dockerfile").write_text(DOCKERFILE)
    return TeamEnvironment(
        environment_dir=d, environment_name="hello-team", session_id="hello-team__abc__env",
        trial_paths=TrialPaths(tmp_path / "trial"), task_env_config=EnvironmentConfig(),
        n_agents=n, policy_settle_sec=0, **kw)


@pytest.fixture
async def started(tmp_path, monkeypatch, client):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SECRET-VALUE")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    env = make_env(tmp_path)
    await env.start(force_build=False)
    return env, client


async def test_start_composes_sdk_sandboxes(started, tmp_path):
    env, client = started
    hub, *kids = client.created
    assert len(kids) == 3 and all(isinstance(m, SandboxMember) for m in env.members)
    for sb, name in zip(client.created, ["hub", "agent-1", "agent-2", "agent-3"]):
        s = sb.spec  # built from the task's Dockerfile as-is, no snapshot
        assert s["dockerfile"] == str(tmp_path / "environment" / "Dockerfile")
        assert s["context_dir"] == str(tmp_path / "environment") and "image" not in s
        assert s["timeout_secs"] == 90 * 60 and s["network"] == SdkPolicy("public")
        opts = s["backend_options"]
        assert opts["ephemeral"] and opts["labels"]["harbor.session_id"] == "hello-team__abc__env"
        assert opts["labels"]["multi-agent-harness.name"] == name
    assert env.team_hub_url == f"https://8080-{hub.id}.proxy.test"
    assert env.team_hub_headers == {"X-Daytona-Preview-Token": f"PREVIEW-{hub.id}"}
    assert env.team_proxy_url == env.team_hub_url + "/proxy"
    assert env.team_git_url == env.team_hub_url + "/git/team.git"
    hub_cmds = "\n".join(hub.commands)
    for needle in ("astral.sh/uv/", "multi-agent-harness[hub] @ file:///opt/multi-agent-harness/multi_agent_harness-0.1.0-py3-none-any.whl",
                   "receive.denyNonFastForwards true", "-m multi_agent_harness.gitserver hook",
                   "multi-agent-harness-board --host 127.0.0.1", "multi-agent-harness-git serve --host 127.0.0.1",
                   "multi-agent-harness-gateway --host 0.0.0.0 --port 8080", "-H @/run/multi-agent-harness/hub.hdr"):
        assert needle in hub_cmds, needle
    assert hub.uploads["/run/multi-agent-harness/hub.hdr"] == f"X-Daytona-Preview-Token: PREVIEW-{hub.id}\n".encode()
    assert "multi-agent-harness-board --check" not in hub_cmds and b"BOARD_POLICY" not in hub.uploads["/run/multi-agent-harness/board.env"]
    assert DaytonaTeamEnvironment is TeamEnvironment


async def test_board_extensions_upload_check_and_env(tmp_path, monkeypatch, client):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "ext").mkdir()
    (tmp_path / "ext" / "pol.py").write_text("from multi_agent_harness.board import Policy\nclass P(Policy): pass\n")
    (tmp_path / "plug_a.py").write_text("def register(board, api, mcp): pass\n")
    (tmp_path / "plug_b.py").write_text("def register(board, api, mcp): pass\n")
    env = make_env(tmp_path, n=1, board_policy="ext/pol.py", board_plugins="plug_a.py, plug_b.py")
    assert env.board_policy == tmp_path / "ext" / "pol.py"
    await env.start(force_build=False)
    hub = client.created[0]
    uploaded = {p: d for p, d in hub.uploads.items() if p.startswith("/opt/multi-agent-harness/board-ext/")}
    assert uploaded == {f"/opt/multi-agent-harness/board-ext/{f.name}": f.read_bytes()  # exactly those files
                        for f in (tmp_path / "ext" / "pol.py", tmp_path / "plug_a.py", tmp_path / "plug_b.py")}
    env_vars = ("BOARD_POLICY=/opt/multi-agent-harness/board-ext/pol.py "
                "BOARD_PLUGINS=/opt/multi-agent-harness/board-ext/plug_a.py,/opt/multi-agent-harness/board-ext/plug_b.py")
    assert f"{env_vars} /opt/multi-agent-harness/venv/bin/multi-agent-harness-board --check" in "\n".join(hub.commands)
    board_env = hub.uploads["/run/multi-agent-harness/board.env"]
    for line in env_vars.encode().split(b" "):
        assert line in board_env
    assert b"PYTHONPATH" not in board_env


def test_board_extension_paths_are_checked_on_the_host(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.py").write_text("")
    (tmp_path / "x.py").write_text("")
    cases = {("hierarchy:HierarchyPolicy", None): "board_policy .*not a path to a .py file",
             ("missing.py", None): "board_policy .*no such file",
             (None, "x.py,missing.py"): "board_plugins .*no such file",
             ("x.py", "a/x.py"): "distinct names; repeated: x.py"}
    for (pol, plugs), match in cases.items():
        with pytest.raises(ValueError, match=match):
            make_env(tmp_path, n=1, board_policy=pol, board_plugins=plugs)


async def test_bad_board_policy_fails_setup(tmp_path, monkeypatch, client):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / "pol.py").write_text("X = 1\n")
    client.check_error = "board policy /opt/multi-agent-harness/board-ext/pol.py must define exactly one subclass"
    env = make_env(tmp_path, n=1, board_policy=str(tmp_path / "pol.py"))
    with pytest.raises(RuntimeError, match="board extensions failed to load.*exactly one subclass"):
        await env.start(force_build=False)
    assert sorted(client.deleted) == sorted(sb.id for sb in client.created)
    assert not any("multi-agent-harness-board --host" in c for c in client.created[0].commands)  # board never started


async def test_service_routing(started):
    env, client = started
    hub, a1, a2, a3 = client.created
    assert (await env.service_exec("hostname", service="agent-2", timeout_sec=10)).stdout == a2.id
    assert (await env.exec("hostname", timeout_sec=10)).stdout == hub.id
    assert (await env.service_exec("hostname", service="main")).stdout == hub.id  # detached + polled
    with pytest.raises(ServiceOperationsUnsupportedError):
        await env.service_exec("hostname", service="agent-9")


async def test_stop_deletes_agents_then_hub_and_ledgers_it(started):
    env, client = started
    hub, *kids = client.created
    await env.stop(delete=True)
    assert sorted(client.deleted[:3]) == sorted(k.id for k in kids) and client.deleted[3:] == [hub.id]
    await env.stop(delete=True)
    assert len(client.deleted) == 4
    lines = [json.loads(l) for l in env.ledger_path.read_text().splitlines()]
    created = [l for l in lines if "id" in l]
    assert [l["id"] for l in created] == [sb.id for sb in client.created]
    assert created[0]["role"] == "hub" and created[1]["name"] == "agent-1"
    assert all(l["session_id"] == "hello-team__abc__env" and l["backend"] == "daytona" for l in created)
    assert created[0]["state"] == {"sandbox_id": hub.id}
    deleted = [l for l in lines if "deleted" in l]
    assert sorted(l["deleted"] for l in deleted) == sorted(sb.id for sb in client.created)
    assert all(l["exec_calls"] > 0 for l in deleted)


async def test_secrets_stay_out_of_logs_and_commands(tmp_path, monkeypatch, client, caplog):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SECRET-VALUE")
    monkeypatch.setenv("PROXY_MAX_CONCURRENT", "3")
    env = make_env(tmp_path, n=2, board_rate_per_sec=2)
    with caplog.at_level(logging.DEBUG):
        await env.start(force_build=False)
        await env.stop(delete=True)
    secrets = ["sk-ant-SECRET-VALUE", env.team_board_admin_token, env.team_proxy_admin_token,
               env.team_preview_token, *env.team_git_tokens.values()]
    hub, *kids = client.created
    commands = "\n".join(c for sb in client.created for c in sb.commands)
    for s in secrets:
        assert s not in caplog.text and s not in commands
        assert all(s not in str(k.spec) and not k.uploads for k in kids)
        assert s.encode() not in hub.uploads["/run/multi-agent-harness/git-tokens.json"]  # hashes only
    assert b"sk-ant-SECRET-VALUE" in hub.uploads["/run/multi-agent-harness/proxy.env"]
    assert b"sk-ant-SECRET-VALUE" not in hub.uploads["/run/multi-agent-harness/board.env"]
    assert b"PROXY_MAX_CONCURRENT=3.0" in hub.uploads["/run/multi-agent-harness/proxy.env"]
    assert b"BOARD_RATE_PER_SEC=2.0" in hub.uploads["/run/multi-agent-harness/board.env"]


def test_ttl_from_task_toml_and_override(tmp_path, client):
    from multi_agent_harness.harbor.environment import ttl_for
    (tmp_path / "task.toml").write_text("[agent]\ntimeout_sec = 1200.0\n")
    env = make_env(tmp_path)
    assert env.ttl_minutes == 50 and all(m.ttl_minutes == 50 for m in env.members)
    assert ttl_for(1201) == 51 and ttl_for(None) == 90
    assert make_env(tmp_path / "b", ttl_minutes=7).ttl_minutes == 7


def test_hub_ttl_also_covers_the_verifier(tmp_path, client):
    (tmp_path / "task.toml").write_text("[agent]\ntimeout_sec = 1200.0\n[verifier]\ntimeout_sec = 4800.0\n")
    env = make_env(tmp_path)
    ttls = {m.role: m.ttl_minutes for m in env.members}
    assert ttls["hub"] == 50 + 80 and ttls["agent"] == 50


async def test_agent_timeout_raises_ttl_through_the_provider(started):
    env, client = started
    await env.set_agent_timeout(3600)
    assert all(sb.raw.ttl == 90 for sb in client.created)
    await env.set_agent_timeout(7200)
    assert all(sb.raw.ttl == 150 for sb in client.created) and env.ttl_minutes == 150


# ---- hub size (--ek hub_cpus / hub_memory_mb)

def test_hub_size_defaults_to_the_task_size(tmp_path, client):
    env = make_env(tmp_path, n=2, override_cpus=4, override_memory_mb=8192)
    assert {m.resources["cpus"] for m in env.members} == {4}
    assert {m.resources["memory_mb"] for m in env.members} == {8192}
    assert env.hub_size == {"cpus": None, "memory_mb": None} and env.hub_size_at == "verify"


async def test_hub_size_at_start_sizes_only_the_hub(tmp_path, monkeypatch, client):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    env = make_env(tmp_path, n=2, override_cpus=4, override_memory_mb=8192, hub_cpus="48",
                   hub_memory_mb=65536, hub_size_at="start")
    await env.start(force_build=False)
    hub, *kids = client.created
    assert hub.spec["cpu"] == 48.0 and hub.spec["memory_mb"] == 65536
    assert all(k.spec["cpu"] == 4.0 and k.spec["memory_mb"] == 8192 for k in kids)
    assert env.task_env_config.cpus == 4  # the agents' (and Harbor's) config is untouched
    assert await env.size_hub_for_verifier() is None  # nothing left to do before the verifier


class FakeContainer:
    def __init__(self):
        self.updates = []

    def update(self, **kw):
        self.updates.append(kw)


async def test_hub_grows_right_before_the_verifier_on_docker(tmp_path, monkeypatch, client):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    env = make_env(tmp_path, n=2, override_cpus=4, override_memory_mb=8192, hub_cpus=48, hub_memory_mb=65536,
                   backend="docker")
    await env.start(force_build=False)
    hub, *kids = client.created
    assert all(sb.spec["cpu"] == 4.0 and sb.spec["memory_mb"] == 8192 for sb in client.created)  # small while agents work
    container = FakeContainer()
    hub._sandbox._session._container = container
    done = await env.size_hub_for_verifier()
    assert container.updates == [{"cpu_period": 100_000, "cpu_quota": 4_800_000, "mem_limit": "65536m",
                                  "memswap_limit": "131072m"}]
    assert done["ok"] and done["from"] == {"cpus": 4, "memory_mb": 8192} and done["to"] == {"cpus": 48, "memory_mb": 65536}
    assert env.hub.resources["cpus"] == 48 and all(a.resources["cpus"] == 4 for a in env.agents.values())
    assert any(HUB_LIMITS in c for c in hub.commands)  # what the hub's cgroup then says, for the record
    assert await env.size_hub_for_verifier() is None and len(container.updates) == 1  # once
    lines = [json.loads(l) for l in env.ledger_path.read_text().splitlines()]
    assert [l for l in lines if l.get("event") == "hub_resized"][0]["to"] == {"cpus": 48, "memory_mb": 65536}


async def test_hub_resize_on_daytona_and_failure_is_not_fatal(tmp_path, monkeypatch, client):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    env = make_env(tmp_path, n=1, hub_memory_mb=65536)
    await env.start(force_build=False)
    hub = client.created[0]
    asked = []
    hub.raw.resize = lambda r: asked.append((r.cpu, r.memory))
    assert (await env.size_hub_for_verifier())["ok"] and asked == [(None, 64)]

    env2 = make_env(tmp_path / "b", n=1, hub_cpus=48)
    await env2.start(force_build=False)

    def refuse(r):
        raise RuntimeError("Failed to resize sandbox: cpu quota exceeded")
    client.created[-2].raw.resize = refuse
    done = await env2.size_hub_for_verifier()
    assert done["ok"] is False and "quota exceeded" in done["error"] and env2.hub.resources["cpus"] != 48


def test_hub_size_arguments_are_checked(tmp_path, client):
    for kw, match in (({"hub_cpus": 0}, "hub_cpus must be >= 1"), ({"hub_memory_mb": "lots"}, "hub_memory_mb must be"),
                      ({"hub_size_at": "later"}, "hub_size_at must be"),
                      ({"hub_cpus": 8, "backend": "modal"}, "can't resize the running hub")):
        with pytest.raises(ValueError, match=match):
            make_env(tmp_path, n=1, **kw)
    make_env(tmp_path, n=1, hub_cpus=8, backend="modal", hub_size_at="start")  # creating it big always works


def test_missing_dockerfile_fails_before_any_sandbox(tmp_path, client):
    (tmp_path / "environment").mkdir()
    with pytest.raises(FileNotFoundError):
        TeamEnvironment(environment_dir=tmp_path / "environment", environment_name="t",
                        session_id="t__abc__env", trial_paths=TrialPaths(tmp_path / "trial"),
                        task_env_config=EnvironmentConfig())
    assert not client.created


# ---- network policy (Harbor's per-phase contract)

def test_policy_mapping_and_capabilities(tmp_path, client):
    to = sbmod.to_sdk_policy
    assert to(PUBLIC) == SdkPolicy("public") and to(NO_NET) == SdkPolicy("none")
    allow = NetworkPolicy(network_mode=NetworkMode.ALLOWLIST, allowed_hosts=["pypi.org", "*.github.com"])
    assert to(allow) == SdkPolicy("allowlist", domains=("pypi.org", "*.github.com"))
    assert to(NetworkPolicy(network_mode=NetworkMode.ALLOWLIST)) == SdkPolicy("none")
    caps = sbmod.capabilities_for("daytona")
    assert caps.disable_internet and caps.network_allowlist_wildcard_hostnames and caps.dynamic_network_policy
    assert not caps.network_allowlist_ipv4_cidrs
    with pytest.raises(ValueError, match="unknown sandbox backend"):
        make_env(tmp_path / "x", backend="nope")


async def test_policy_merge_for_each_mode(tmp_path, monkeypatch, client):
    env = make_env(tmp_path)
    env.team_hub_host, env._model_hosts = "8080-hub.proxy.test", ["api.openai.com"]
    allow = NetworkPolicy(network_mode=NetworkMode.ALLOWLIST, allowed_hosts=["pypi.org", "*.github.com"])
    assert env.agent_policy(PUBLIC) == PUBLIC and env.hub_policy(PUBLIC) == PUBLIC
    assert env.agent_policy(NO_NET).allowed_hosts == ["8080-hub.proxy.test"]
    assert env.agent_policy(NO_NET).network_mode == NetworkMode.ALLOWLIST
    assert env.hub_policy(NO_NET).allowed_hosts == ["api.openai.com"]
    assert env.agent_policy(allow).allowed_hosts == ["pypi.org", "*.github.com", "8080-hub.proxy.test"]
    ips = NetworkPolicy(network_mode=NetworkMode.ALLOWLIST, allowed_hosts=["10.1.2.3"])
    with pytest.raises(ValueError, match="IPv4"):  # can't mix with the hub's hostname
        env.validate_network_policy_support(ips)


async def test_phase_switch_applies_and_verifies(started):
    env, client = started
    hub, *kids = client.created
    await env.set_network_policy(NO_NET)  # what Harbor calls before agent.run()
    assert env.network_policy == NO_NET
    assert all(k.net == SdkPolicy("allowlist", domains=(hub.host(),)) for k in kids)
    assert hub.net == SdkPolicy("allowlist", domains=("api.anthropic.com",))
    probes = [c for c in kids[0].commands if "curl -s -o /dev/null" in c]
    assert any("example.com" in c for c in probes) and any(f"{hub.host()}/healthz" in c for c in probes)
    assert await env.verify_agent_network() == NO_NET
    await env.set_network_policy(PUBLIC)  # and after it
    assert all(k.net == SdkPolicy("public") for k in kids)


async def test_restricted_baseline_is_applied_at_create(tmp_path, monkeypatch, client):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    env = make_env(tmp_path, network_policy=NO_NET)
    await env.start(force_build=False)
    hub, *kids = client.created
    assert hub.spec["network"] == SdkPolicy("public")  # open for the runtime install
    assert all(k.spec["network"] == SdkPolicy("allowlist", domains=(hub.host(),)) for k in kids)
    assert hub.net == SdkPolicy("allowlist", domains=("api.openai.com",))  # baseline at the end
    assert await env.verify_agent_network() == NO_NET


async def test_policy_that_does_not_take_fails_closed(started):
    env, client = started
    client.created[2].leaky = True  # agent-2 still reaches example.com
    with pytest.raises(RuntimeError, match="agent-2 network no-network.*failing closed"):
        await env.set_network_policy(NO_NET)
    assert env.network_policy == PUBLIC  # Harbor never records the switch
    env._network_policy = NO_NET
    with pytest.raises(RuntimeError, match="failing closed"):
        await env.verify_agent_network()


async def test_unreachable_hub_fails_restricted_switch_but_not_public(started, monkeypatch):
    env, client = started
    warnings = []
    monkeypatch.setattr(env.logger, "warning", lambda msg, *a: warnings.append(msg % a))
    await env.set_network_policy(NO_NET)
    client.created[2].hub_down = True
    await env.set_network_policy(PUBLIC)  # loosening: logged, not raised
    assert any("agent-2 network public" in w and "not failing" in w for w in warnings)
    with pytest.raises(RuntimeError, match="agent-2 network no-network.*failing closed"):
        await env.set_network_policy(NO_NET)


async def test_missing_curl_fails_closed(started):
    env, client = started
    client.created[1].no_curl = True
    with pytest.raises(RuntimeError, match="no curl"):
        await env.set_network_policy(NO_NET)


def test_hub_wheel_has_hub_extra_and_scripts(tmp_path):
    import zipfile
    from multi_agent_harness.harbor.hubruntime import build_wheel
    whl = build_wheel(tmp_path)
    with zipfile.ZipFile(whl) as z:
        meta = z.read("multi_agent_harness-0.1.0.dist-info/METADATA").decode()
        entry = z.read("multi_agent_harness-0.1.0.dist-info/entry_points.txt").decode()
        assert "multi_agent_harness/gateway.py" in z.namelist() and "multi_agent_harness/board/server.py" in z.namelist()
    assert 'Requires-Dist: fastapi ; extra == "hub"' in meta
    assert "multi-agent-harness-gateway = multi_agent_harness.gateway:main" in entry


async def test_export_artifacts_bundles_repo_and_board_log(started):
    env, client = started
    hub = client.created[0]
    # No WORKDIR in the Dockerfile and the fake's pwd is not a path: the default, seeded at start.
    assert (env.team_workdir, env.team_workdir_source) == ("/workspace", "default")
    seed = "/opt/multi-agent-harness/venv/bin/python -m multi_agent_harness.workdir seed --git-dir /srv/git/team.git --workdir /workspace"
    assert seed in "\n".join(hub.commands)
    assert '"workdir": "/workspace"' in env.ledger_path.read_text()
    before = len(hub.commands)
    await env.export_artifacts()
    checkout = cmd = "\n".join(hub.commands[before:])  # each export step is its own command
    assert "multi_agent_harness.workdir checkout --git-dir /srv/git/team.git --workdir /workspace" in checkout
    assert "bundle create /logs/artifacts/multi-agent-harness/team.bundle --all" in cmd
    assert "/var/lib/multi-agent-harness/board-events.jsonl /logs/artifacts/multi-agent-harness/board-events.jsonl" in cmd


async def test_workdir_from_dockerfile_is_seeded_and_checked_out(tmp_path, client):
    env = make_env(tmp_path)
    (tmp_path / "environment/Dockerfile").write_text("FROM python\nWORKDIR /app\n")
    await env.start(force_build=False)
    assert (env.team_workdir, env.team_workdir_source) == ("/app", "dockerfile")
    await env.export_artifacts()
    cmds = "\n".join(client.created[0].commands)
    assert "multi_agent_harness.workdir seed --git-dir /srv/git/team.git --workdir /app" in cmds
    assert "multi_agent_harness.workdir checkout --git-dir /srv/git/team.git --workdir /app" in cmds


# ---- exec: shell wrapping (run for real) and the detached path

def sh(script):
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=30)


def test_shell_wrapping_applies_cwd_env_timeout_and_splits_output(tmp_path):
    mark = "@@multi-agent-harness-test@@"
    inner = sbmod.shell_command('echo "$PWD $A"; echo oops >&2; exit 3', cwd=str(tmp_path),
                                env={"A": "it's set"}, timeout_sec=10)
    r = sh(sbmod.foreground_script(inner, mark))
    assert r.returncode == 3
    assert sbmod.split_output(r.stdout, mark) == (f"{tmp_path} it's set\n", "oops\n")
    r = sh(sbmod.foreground_script(sbmod.shell_command("sleep 5", timeout_sec=1), mark))
    assert r.returncode == 124
    su = sbmod.shell_command("id", user="agent")
    assert su.startswith("su agent -s /bin/sh -c ")


def test_background_script_leaves_pid_output_and_rc(tmp_path):
    job = str(tmp_path / "job")
    r = sh(sbmod.background_script(sbmod.shell_command("echo out; echo err >&2; exit 7"), job))
    assert r.returncode == 0 and "started" in r.stdout
    for _ in range(100):
        if (tmp_path / "job" / "rc").exists():
            break
        subprocess.run(["sleep", "0.05"])
    assert (tmp_path / "job" / "rc").read_text().strip() == "7"
    assert (tmp_path / "job" / "o").read_text() == "out\n" and (tmp_path / "job" / "e").read_text() == "err\n"
    assert (tmp_path / "job" / "pid").read_text().strip().isdigit()


async def test_detached_exec_polls_then_cleans_up_and_kills_on_cancel(started, monkeypatch):
    env, client = started
    hub = client.created[0]
    monkeypatch.setattr(sbmod, "POLL_MIN_SEC", 0.01)
    r = await env.exec("hostname")  # no timeout: detached
    assert r.stdout == hub.id and r.return_code == 0
    started_job = JOB.search(next(c for c in reversed(hub.commands) if "rc.tmp" in c)).group()
    assert any(f"rm -rf {started_job}" in c and "kill" not in c for c in hub.commands[-2:])

    hub.jobs_never_finish = True
    real = hub.execute_command

    async def slow(cmd, args=None):
        if "/rc 2>/dev/null" in cmd:
            return SimpleNamespace(stdout="", exit_code=0)
        return await real(cmd, args)

    hub.execute_command = slow
    task = asyncio.ensure_future(env.exec("sleep 999"))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "kill -TERM -- -$p" in hub.commands[-1] and "rm -rf /tmp/multi-agent-harness-exec/" in hub.commands[-1]


# ---- separate verifier mode

async def test_separate_verifier_is_one_plain_sandbox(tmp_path, client):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "Dockerfile").write_text("FROM python:3.12-slim\n")
    (tmp_path / "task.toml").write_text("[verifier]\ntimeout_sec = 120.0\n")
    env = TeamEnvironment(
        environment_dir=tests, environment_name="t", session_id="t__abc__verifier__trial",
        trial_paths=TrialPaths(tmp_path / "trial"), task_env_config=EnvironmentConfig(),
        n_agents=3, budget_usd=5, auto_snapshot=True, network_policy=NO_NET)
    assert type(env) is SandboxMember and env.ttl_minutes == 32
    await env.start(force_build=False)
    [sb] = client.created
    assert sb.spec["timeout_secs"] == 32 * 60 and sb.spec["dockerfile"] == str(tests / "Dockerfile")
    assert sb.spec["backend_options"]["labels"]["multi-agent-harness.role"] == "verifier"
    assert sb.spec["network"] == SdkPolicy("none")
    await env.upload_dir(tests, "/logs/artifacts")
    assert any(c for c in sb.commands if "/logs/artifacts" in c and "--no-same-owner" in c)
    await env.stop(delete=True)
    assert client.deleted == [sb.id]


def test_concurrent_daytona_context_uploads_keep_environ(monkeypatch):
    """Creates from an image with a COPY context each swap os.environ in a worker thread;
    unserialized, 7 at once (an N=6 hub plus agents) wiped OPENAI_API_KEY."""
    import threading

    pytest.importorskip("daytona")
    from daytona._sync.object_storage import ObjectStorage

    monkeypatch.setenv("MULTI_AGENT_HARNESS_ENV_RACE_PROBE", "1")

    def make():
        ObjectStorage("https://s3.invalid", "a", "b", "c", "bucket", region="us-east-1")

    for _ in range(10):
        threads = [threading.Thread(target=make) for _ in range(7)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert "MULTI_AGENT_HARNESS_ENV_RACE_PROBE" in sbmod.os.environ


def test_upstream_modes():
    env = {"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o"}
    d = upstream.resolve("direct", env, "t1")
    assert d.hosts == ["api.anthropic.com", "api.openai.com"]
    assert d.proxy_env() == env and d.model_map is None  # what the proxy got before upstreams existed
    c = upstream.resolve("https://gw.example:8443/", {"OPENAI_API_KEY": "o"}, "t1")
    assert c.mode == "custom" and c.urls == {"openai": "https://gw.example:8443"} and c.hosts == ["gw.example"]
    for bad in ("drect", "ftp://x", "https://u:p@gw.example"):
        with pytest.raises(ValueError):
            upstream.check_mode(bad)


def _codex_login(path, n):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tokens": {"access_token": f"AT-SECRET-{n}", "refresh_token": f"RT-SECRET-{n}",
                                           "id_token": f"ID-SECRET-{n}", "account_id": f"acct-{n}"}}))


def test_subscription_finds_logins(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="codex login"):
        upstream.resolve("subscription", {"HOME": str(tmp_path)}, "t1")
    _codex_login(tmp_path / ".codex" / "auth.json", 1)
    s = upstream.resolve("subscription", {"HOME": str(tmp_path), "OPENAI_API_KEY": "o"}, "t1")
    assert s.mode == "subscription" and s.keys == {} and len(s.logins) == 1
    assert s.hosts == ["chatgpt.com", "auth.openai.com"] and "AT-SECRET-1" in s.secrets
    assert "SECRET" not in repr(s) and s.proxy_env() == {"PROXY_OPENAI_UPSTREAM": upstream.CHATGPT_CODEX_URL}
    _codex_login(tmp_path / "pool" / "b" / "auth.json", 2)
    _codex_login(tmp_path / "pool" / "a.json", 3)
    assert len(upstream.resolve("subscription", {upstream.CODEX_LOGINS_VAR: str(tmp_path / "pool")}, "t1").logins) == 2
    (tmp_path / "pool" / "a.json").write_text(json.dumps({"OPENAI_API_KEY": "sk-x"}))
    with pytest.raises(ValueError, match="no ChatGPT tokens"):
        upstream.resolve("subscription", {upstream.CODEX_LOGINS_VAR: str(tmp_path / "pool")}, "t1")


def test_a_refreshed_login_goes_back_to_its_host_file(tmp_path):
    path = tmp_path / "auth.json"
    _codex_login(path, 1)
    uploaded = path.read_bytes()
    _codex_login(tmp_path / "new.json", 2)
    refreshed = (tmp_path / "new.json").read_bytes()
    save = upstream.save_refreshed_login
    assert save(path, uploaded, uploaded) == "unchanged" and path.read_bytes() == uploaded
    assert save(path, uploaded, b"") == "invalid" and save(path, uploaded, b'{"tokens": {}}') == "invalid"
    assert save(path, uploaded, refreshed) == "saved" and path.read_bytes() == refreshed
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    _codex_login(path, 3)   # a new codex login on the host since the upload wins
    assert save(path, uploaded, refreshed) == "host-changed" and b"AT-SECRET-3" in path.read_bytes()


async def test_stop_saves_the_hubs_refreshed_login(tmp_path, monkeypatch, client):
    path = tmp_path / "pool" / "auth.json"
    _codex_login(path, 1)
    monkeypatch.setenv(upstream.CODEX_LOGINS_VAR, str(tmp_path / "pool"))
    env = make_env(tmp_path / "task", model_upstream="subscription")
    await env.start(force_build=False)
    _codex_login(tmp_path / "hub.json", 9)   # what the hub's proxy wrote after refreshing

    async def download(remote, local):
        data = (tmp_path / "hub.json").read_bytes() if remote.endswith("codex-login-1.json") else b""
        open(local, "wb").write(data)

    monkeypatch.setattr(env.hub, "download_file", download)
    await env.save_logins()
    assert b"RT-SECRET-9" in path.read_bytes()


async def test_subscription_logins_reach_only_the_hub(tmp_path, monkeypatch, client, caplog):
    pool = tmp_path / "pool"
    _codex_login(pool / "one.json", 1)
    _codex_login(pool / "two.json", 2)
    monkeypatch.setenv(upstream.CODEX_LOGINS_VAR, str(pool))
    env = make_env(tmp_path / "task", model_upstream="subscription")
    with caplog.at_level(logging.DEBUG):
        await env.start(force_build=False)
        await env.set_network_policy(NO_NET)
    hub, *kids = client.created
    assert hub.net == SdkPolicy("allowlist", domains=("chatgpt.com", "auth.openai.com"))
    penv = hub.uploads["/run/multi-agent-harness/proxy.env"].decode()
    assert "PROXY_CODEX_LOGINS=/run/multi-agent-harness/codex-login-1.json:/run/multi-agent-harness/codex-login-2.json" in penv
    assert f"PROXY_OPENAI_UPSTREAM={upstream.CHATGPT_CODEX_URL}" in penv and "SECRET" not in penv
    assert b"AT-SECRET-2" in hub.uploads["/run/multi-agent-harness/codex-login-2.json"]
    commands = "\n".join(c for sb in client.created for c in sb.commands)
    for s in ("AT-SECRET-1", "RT-SECRET-2", "ID-SECRET-1"):
        assert s not in commands and s not in caplog.text
        assert all(s not in str(k.spec) and all(s.encode() not in v for v in k.uploads.values()) for k in kids)
    assert any("chmod 600 /run/multi-agent-harness/*" in c for c in hub.commands)
    ledger = [json.loads(l) for l in env.ledger_path.read_text().splitlines()]
    assert any(l.get("model_upstream") == "subscription" and l.get("subscription_logins") == 2 for l in ledger)


CLAUDE_TOKEN = "sk-ant-oat01-FAKE-SUBSCRIPTION-TOKEN"


def test_claude_subscription_upstream(tmp_path):
    env = {"CLAUDE_CODE_OAUTH_TOKEN": CLAUDE_TOKEN, "HOME": str(tmp_path)}
    c = upstream.resolve("subscription", env, "t1", harness="claude-code")
    assert (c.mode, c.urls, c.keys, c.logins, c.hosts) == ("claude-subscription", {}, {}, [], [])
    assert c.agent_hosts == ["api.anthropic.com"] and c.proxy_env() == {} and CLAUDE_TOKEN not in repr(c)
    with pytest.raises(ValueError, match="claude setup-token"):
        upstream.resolve("subscription", {"HOME": str(tmp_path)}, "t1", harness="claude-code")
    with pytest.raises(ValueError, match="codex login"):  # codex keeps meaning ChatGPT logins
        upstream.resolve("subscription", env, "t1", harness="codex")


async def test_claude_subscription_allows_anthropic_for_agents_only(tmp_path, monkeypatch, client, caplog):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", CLAUDE_TOKEN)
    (tmp_path / "trial").mkdir()
    (tmp_path / "trial" / "config.json").write_text(json.dumps({"agent": {"kwargs": {"harness": "claude-code"}}}))
    env = make_env(tmp_path, model_upstream="subscription")
    with caplog.at_level(logging.DEBUG):
        await env.start(force_build=False)
        await env.set_network_policy(NO_NET)
    hub, *kids = client.created
    assert env.team_model_upstream == "claude-subscription"
    assert hub.net == SdkPolicy("none")  # no model upstream on the hub
    for k in kids:
        assert set(k.net.domains) == {env.team_hub_host, "api.anthropic.com"}
        assert any("https://api.anthropic.com" in c for c in k.commands)  # checked from inside, fail closed
    penv = hub.uploads["/run/multi-agent-harness/proxy.env"].decode()
    assert "ANTHROPIC" not in penv
    for sb in client.created:  # the environment never ships the token anywhere
        assert all(CLAUDE_TOKEN not in c for c in sb.commands) and CLAUDE_TOKEN not in str(sb.spec)
        assert all(CLAUDE_TOKEN.encode() not in v for v in sb.uploads.values())
    assert CLAUDE_TOKEN not in caplog.text and CLAUDE_TOKEN not in env.ledger_path.read_text()


# ---- control-plane retries


@pytest.fixture
def instant_retries(monkeypatch):
    monkeypatch.setattr(sbmod, "_patient_wait", wait_none())


def fail_creates(client, errors):
    """The next creates raise these errors, in order; later ones succeed."""
    real = client.create

    async def create(**spec):
        if errors:
            raise errors.pop(0)
        return await real(**spec)
    client.create = create


def create_error(status, outcome=ErrorOutcome.FAILED):
    return SandboxCreationError(f"Failed to create Daytona sandbox: {status}", backend="daytona", op="create",
                                context={"provider_status": status}, outcome=outcome)


async def test_create_is_retried_after_a_failure_the_sdk_cleaned_up(tmp_path, monkeypatch, client, instant_retries):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SECRET-VALUE")
    fail_creates(client, [create_error(502), create_error(503)])
    env = make_env(tmp_path)
    await env.start(force_build=False)
    assert len(client.created) == 4  # hub and three agents, nothing extra
    ledger = [json.loads(line) for line in env.ledger_path.read_text().splitlines()]
    assert sorted(e["name"] for e in ledger if "id" in e) == ["agent-1", "agent-2", "agent-3", "hub"]


@pytest.mark.parametrize("error", [create_error(400), create_error(502, ErrorOutcome.UNKNOWN)],
                         ids=["quota refusal", "outcome unknown"])
async def test_create_is_not_retried_on_a_refusal_or_an_unknown_outcome(tmp_path, monkeypatch, client,
                                                                         instant_retries, error):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SECRET-VALUE")
    fail_creates(client, [error, create_error(502)])
    with pytest.raises(SandboxCreationError) as raised:
        await make_env(tmp_path).start(force_build=False)
    assert raised.value is error
    assert client.created == []


async def test_set_network_and_delete_ride_out_a_backend_outage(started, instant_retries):
    env, client = started
    hub = client.created[0]
    real_set, real_destroy = hub.set_network, hub.destroy
    outage = [SandboxExecutionError("Daytona set_network failed.", backend="daytona", op="set_network")] * 2

    async def set_network(policy):
        if outage:
            raise outage.pop()
        await real_set(policy)

    hub.set_network = set_network
    await env.set_network_policy(NO_NET)
    assert hub.net.mode == "allowlist" and not outage

    gone = [SandboxExecutionError("Daytona destroy failed.", backend="daytona", op="destroy")]

    async def destroy():
        if gone:
            raise gone.pop()
        await real_destroy()

    hub.destroy = destroy
    await env.stop(delete=True)
    assert hub.id in client.deleted and not gone


async def test_docker_defaults_publish_the_hub_on_the_bridge_and_run_no_network_public(tmp_path, client, caplog):
    import multi_agent_harness.harbor.environment as envmod
    env = make_env(tmp_path, n=2, backend="docker", network_policy=NO_NET, phase_network_policies=[NO_NET])
    with caplog.at_level(logging.WARNING):
        await env.start(force_build=False)
        await env.set_network_policy(NO_NET)
        assert await env.verify_agent_network() == PUBLIC
    hub, *kids = client.created
    assert hub.spec["backend_options"]["ports"] == {"8080/tcp": {"HostIp": "172.18.0.1", "HostPort": None}}
    assert all("ports" not in k.spec["backend_options"] for k in kids)
    assert all(sb.spec["network"] == SdkPolicy("public") == sb.net for sb in client.created)
    warned = [r for r in caplog.records if "isn't enforced" in r.getMessage()]
    assert len(warned) == 1 and "backend=daytona" in warned[0].getMessage()
    ledger = [json.loads(l) for l in env.ledger_path.read_text().splitlines()]
    assert [l["requested"] for l in ledger if l.get("event") == "network_not_enforced"] == ["no-network"]
    assert any(l.get("event") == "docker_hub_port" and l["host_ip"] == "172.18.0.1" for l in ledger)
    assert envmod.DOCKER_BRIDGE_GATEWAY == "172.17.0.1"


async def test_docker_explicit_ports_and_public_task_are_used_as_given(tmp_path, client, caplog):
    ports = {"8080/tcp": {"HostIp": "172.17.0.1", "HostPort": None}}
    env = make_env(tmp_path, n=2, backend="docker", backend_options={"ports": ports})
    with caplog.at_level(logging.WARNING):
        await env.start(force_build=False)
    assert all(sb.spec["backend_options"]["ports"] == ports for sb in client.created)
    assert not any("isn't enforced" in r.getMessage() for r in caplog.records)
    ledger = env.ledger_path.read_text()
    assert "network_not_enforced" not in ledger and "docker_hub_port" not in ledger


def test_docker_bridge_gateway_never_unspecified(monkeypatch):
    import sys
    import multi_agent_harness.harbor.environment as envmod
    monkeypatch.undo()  # the real lookup, against a fake docker module

    def fake(gateways):
        net = SimpleNamespace(attrs={"IPAM": {"Config": [{"Gateway": g} for g in gateways]}})
        client = SimpleNamespace(networks=SimpleNamespace(get=lambda name: net), close=lambda: None)
        return SimpleNamespace(from_env=lambda: client)

    for gateways, want in ((["fd00::1", "172.20.0.1"], "172.20.0.1"), (["0.0.0.0"], "172.17.0.1"), ([], "172.17.0.1")):
        monkeypatch.setitem(sys.modules, "docker", fake(gateways))
        assert envmod.docker_bridge_gateway() == want
