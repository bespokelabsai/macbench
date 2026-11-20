"""GPU allocation through the provider boundary; no cloud sandboxes."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bespokelabs.sandbox.types import SandboxConfig
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths

from multi_agent_harness.harbor import checkpoint as ck
from multi_agent_harness.harbor import sandbox as sbmod
from multi_agent_harness.harbor.agent import TeamAgent
from multi_agent_harness.harbor.daytona_gpu import DaytonaGpuClient, sdk
from multi_agent_harness.harbor.environment import TeamEnvironment
from multi_agent_harness.harbor.resources import gpu_plan
from test_agent import FakeEnv, fake_installer  # noqa: F401
from test_checkpoint import write_checkpoint
from test_environment import NO_NET, FakeSandbox, client, make_env  # noqa: F401


@pytest.mark.parametrize("n,k,selected", [(3, 0, []), (3, 1, ["agent-3"]),
    (3, 3, ["agent-1", "agent-2", "agent-3"]), (1, 1, ["agent-1"]), (12, 2, ["agent-11", "agent-12"])])
def test_allocation_only_changes_selected_member_gpu_spec(tmp_path, n, k, selected):
    env = make_env(tmp_path, n=n, n_gpus=k)
    assert [name for name, member in env.agents.items() if "gpu" in member.create_spec()] == selected
    assert "gpu" not in env.hub.create_spec()
    assert all(member.resources["gpus"] == int(name in selected) for name, member in env.agents.items())
    assert env.task_env_config.gpus is None


@pytest.mark.parametrize("kw", [dict(n_gpus=-1), dict(n_gpus=4), dict(n_gpus=1.2), dict(n_gpus=True),
    dict(n_gpus="1.0"), dict(n_gpus="bad"), dict(n_gpus=None), dict(gpu_type="H100"),
    dict(n_gpus=1, gpu_type="MI355X"), dict(n_gpus=1, gpu_type="unknown_default_open_api"),
    dict(n_gpus=1, backend="modal"), dict(n_gpus=1, backend_options={"resources": {"gpu": 9}})])
def test_invalid_gpu_config_before_create(tmp_path, client, kw):
    with pytest.raises(ValueError):
        make_env(tmp_path, **kw)
    assert client.created == []


@pytest.mark.parametrize("prebuilt", [False, True])
@pytest.mark.parametrize("gpu_type", [None, "H100"])
def test_real_daytona_parameters_preserve_resources_network_ttl_and_labels(tmp_path, prebuilt, gpu_type):
    env = make_env(tmp_path, n_gpus=1, gpu_type=gpu_type, override_cpus=4,
                   override_memory_mb=8192, override_storage_mb=20480, network_policy=NO_NET)
    member = env.agents["agent-3"]
    if prebuilt:
        member.task_env_config.docker_image = "example/gpu:latest"
    spec = member.create_spec()
    config = SandboxConfig(backend="daytona", timeout_secs_explicit=True, **spec)
    backend = DaytonaGpuClient()
    requests = []
    backend._client = SimpleNamespace(create=lambda params, **kw: requests.append((params, kw)) or SimpleNamespace(id="gpu-1"))
    result = backend.create(config)
    assert result.provider_resource_id == "gpu-1"
    params, kw = requests[0]
    assert (params.resources.cpu, params.resources.memory, params.resources.disk, params.resources.gpu) == (4, 8, 20, 1)
    assert params.resources.gpu_type == gpu_type
    assert params.ttl_minutes == spec["timeout_secs"] // 60
    assert params.ephemeral and params.auto_stop_interval == 0
    assert params.network_block_all is True
    assert params.labels["multi-agent-harness.name"] == "agent-3"
    assert params.labels[sdk._CREATE_TOKEN_LABEL]
    assert kw["timeout"] == member.task_env_config.build_timeout_sec
    # The same client uses the upstream CPU path without adding a GPU.
    cpu_spec = env.hub.create_spec()
    backend.create(SandboxConfig(backend="daytona", **cpu_spec))
    assert requests[1][0].resources.gpu is None


def gpu_answer(monkeypatch, text="NVIDIA H100 80GB HBM3, 81920\n", rc=0):
    original = FakeSandbox._answer
    def answer(self, command):
        if "nvidia-smi --query-gpu" in command:
            return text, rc
        return original(self, command)
    monkeypatch.setattr(FakeSandbox, "_answer", answer)


async def test_gpu_probe_and_ledger_then_cleanup(tmp_path, client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    gpu_answer(monkeypatch)
    env = make_env(tmp_path, n_gpus=1, gpu_type="H100")
    await env.start(False)
    assert env.team_agent_resources["agent-3"]["observed_gpus"] == [{"name": "NVIDIA H100 80GB HBM3", "memory_mb": 81920}]
    assert all(not any("nvidia-smi" in c for c in s.commands) for s in client.created[:3])
    records = [json.loads(line) for line in env.ledger_path.read_text().splitlines()]
    created = next(i for i, r in enumerate(records) if r.get("id") == client.created[-1].id)
    verified = next(i for i, r in enumerate(records) if r.get("event") == "gpu_verified")
    assert created < verified
    await env.stop(True)
    assert set(client.deleted) == {s.id for s in client.created}


@pytest.mark.parametrize("text,rc", [("", 127), ("", 0), ("H100, 80000\nH100, 80000", 0),
    ("H200, 140000", 0), ("H100, nonsense", 0)])
async def test_failed_gpu_probe_cleans_whole_team(tmp_path, client, monkeypatch, text, rc):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    gpu_answer(monkeypatch, text, rc)
    env = make_env(tmp_path, n_gpus=1, gpu_type="H100")
    with pytest.raises(RuntimeError, match="GPU verification failed"):
        await env.start(False)
    assert len(client.created) == 4 and set(client.deleted) == {s.id for s in client.created}


def test_separate_verifier_discards_team_gpu_settings(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "Dockerfile").write_text("FROM ubuntu:24.04\n")
    env = TeamEnvironment(environment_dir=tests, environment_name="t", session_id="t__verifier__id",
                          trial_paths=TrialPaths(tmp_path / "trial"), task_env_config=EnvironmentConfig(),
                          n_agents=3, n_gpus=2, gpu_type="H100")
    assert type(env) is sbmod.SandboxMember and "gpu" not in env.create_spec()


def test_harbor_per_environment_gpus_are_not_team_count(tmp_path):
    with pytest.raises(ValueError, match="--override-gpus"):
        make_env(tmp_path, n_gpus=1, override_gpus=2)


def configure_resources(env):
    env.team_gpu_plan = gpu_plan(len(env.team_agent_services), 1, "H100")
    env.team_agent_resources = {n: {"gpus": k, "gpu_type": "H100" if k else None,
                                  "observed_gpus": [{"name": "NVIDIA H100", "memory_mb": 81920}] if k else []}
                                for n, k in env.team_gpu_plan["slots"].items()}


async def test_checkpoint_resource_plan_and_legacy_validation(tmp_path, client):
    ckdir = write_checkpoint(tmp_path / "ck")
    env = make_env(tmp_path, n_gpus=1, resume_from=str(ckdir))
    with pytest.raises(ValueError, match="GPU allocation"):
        await env.start(False)
    assert not client.created
    ck.load(ckdir, task="hello-team", harness="codex", n_agents=3)  # legacy CPU accepted
    path = ckdir / "manifest.json"
    manifest = json.loads(path.read_text()) | {"format": ck.FORMAT, "gpu_allocation": env.team_gpu_plan}
    path.write_text(json.dumps(manifest))
    ck.load(ckdir, task="hello-team", harness="codex", n_agents=3, gpu_allocation=env.team_gpu_plan)
    for plan in (gpu_plan(3), gpu_plan(3, 2), gpu_plan(3, 1, "H100"),
                 env.team_gpu_plan | {"slots": {"agent-1": 1, "agent-2": 0, "agent-3": 0}}):
        with pytest.raises(ValueError, match="GPU allocation"):
            ck.load(ckdir, task="hello-team", harness="codex", n_agents=3, gpu_allocation=plan)
    del manifest["gpu_allocation"]
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="requires a GPU allocation plan"):
        ck.load(ckdir, task="hello-team", harness="codex", n_agents=3)


@pytest.mark.parametrize("strategy", ["fixed", "fixed-lead", "async"])
async def test_cpu_only_resource_map_does_not_change_prompts(tmp_path, strategy):
    env = FakeEnv(3)
    env.team_agent_resources = {n: {"gpus": 0} for n in env.team_agent_services}
    agent = TeamAgent(tmp_path, orchestration=strategy, checkpoint_every_min=0)
    await agent.setup(env)
    for slot in env.team_agent_services:
        prompt = agent.orchestration.prompt(agent, slot, "TASK")
        if prompt is not None:
            assert "inspect your sandbox" not in prompt and "gpu=True" not in prompt
            assert agent._resource_prompt(slot, prompt) == prompt
        assert agent._resource_prompt(slot, "custom prompt") == "custom prompt"


def test_cpu_only_backend_options_pass_through(tmp_path):
    options = {"resources": {"cpu": 2}, "device_requests": ["custom"]}
    env = make_env(tmp_path, backend_options=options)
    assert all(all(m.create_spec()["backend_options"][k] == v for k, v in options.items()) for m in env.members)
    with pytest.raises(ValueError, match="cannot override"):
        make_env(tmp_path, n_gpus=1, backend_options=options)


def test_concurrent_gpu_types_and_cpu_create_do_not_share_overrides():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(3)
    backend = DaytonaGpuClient()
    def create(params, **kw):
        barrier.wait(timeout=5)
        resources = params.resources
        return SimpleNamespace(id=f"{resources.gpu}:{resources.gpu_type.value}" if resources else "cpu")
    provider = SimpleNamespace(create=create)
    backend._client = provider
    configs = [SandboxConfig(backend="daytona", image="test:latest", gpu=g) for g in ("H100", "H200", None)]
    with ThreadPoolExecutor(3) as workers:
        results = list(workers.map(backend.create, configs))
    assert [r.provider_resource_id for r in results] == ["1:H100", "1:H200", "cpu"]
    assert backend._client is provider


async def test_cpu_members_use_original_sdk_client():
    from bespokelabs.sandbox import AsyncSandboxClient
    from multi_agent_harness.harbor.daytona_gpu import DaytonaGpuAsyncClient

    assert type(sbmod.sdk_client("daytona")) is AsyncSandboxClient
    assert type(sbmod.sdk_client("daytona", gpu=True)) is DaytonaGpuAsyncClient


async def test_resource_note_once_per_session_and_new_identity(tmp_path):
    env = FakeEnv(3)
    configure_resources(env)
    agent = TeamAgent(tmp_path, checkpoint_every_min=0)
    await agent.setup(env)
    for who, resume in [("worker", False), ("worker", True), ("replacement", False), ("restored", True)]:
        agent.runs.setdefault(who, {"logs": "/logs/" + who, "sessions": 1})
        await agent._run_one(env, "agent-3", "Task", who, resume=resume)
    prompts = [c["env"]["MULTI_AGENT_HARNESS_PROMPT"] for c in env.calls if "MULTI_AGENT_HARNESS_PROMPT" in c["env"]]
    assert ["your sandbox has one GPU" in p for p in prompts] == [True, False, True, True]


@pytest.mark.parametrize("state", ["not_found", "started", None])
async def test_run_error_only_removes_provider_confirmed_slot(tmp_path, state):
    team = TeamAgent(tmp_path, orchestration="async")
    team._env = SimpleNamespace(agents={"gpu": SimpleNamespace(status=AsyncMock(return_value=state))},
                                team_gone_agents=set(), agent_gone=lambda *a, **kw: None)
    team.lost = {"gpu": "run error"}
    team.runs = {"worker": {"exit": "RuntimeError"}}
    team.final_response = AsyncMock(return_value="run failed")
    team.board_admin = AsyncMock(return_value={"actions": []})
    team.orchestration.active = {"gpu": "worker"}
    await team.orchestration.tick(team)
    posts = [c.args[1] for c in team.board_admin.call_args_list if c.args[0] == "POST"]
    assert posts == ["/api/admin/subagents/" + ("lost" if state == "not_found" else "finished")]
