"""A sandbox the backend deletes mid-run: the trial carries on without that agent, and the team's
repo reaches the trial dir before the hub is deleted."""

import json
import logging
from types import SimpleNamespace

import pytest
from bespokelabs.sandbox import SandboxNotFoundError
from daytona.common.errors import DaytonaError
from bespokelabs.sandbox.exceptions import ErrorOutcome, SandboxCreationError, SandboxExecutionError
from harbor.models.agent.context import AgentContext

from multi_agent_harness.harbor import sandbox as sbmod
from multi_agent_harness.harbor.agent import TeamAgent
from test_agent import FakeEnv
from test_environment import (NO_NET, PUBLIC, client, create_error, fail_creates, instant_retries,  # noqa: F401
                              make_env, started)

pytestmark = pytest.mark.asyncio


async def _missing(*a, **k):
    raise SandboxNotFoundError("404 sandbox not found")


def _ledger(env):
    return [json.loads(l) for l in env.ledger_path.read_text().splitlines()]


async def test_gone_agent_is_dropped_but_a_gone_hub_still_fails(started):
    env, client = started
    hub, _, kid, _ = client.created
    client.created.remove(kid)  # the backend no longer knows agent-2
    kid.execute_command = _missing
    env.agents["agent-2"]._apply_network_policy = _missing
    await env.set_network_policy(NO_NET)
    await env.set_network_policy(PUBLIC)
    gone = [l for l in _ledger(env) if l.get("event") == "agent_gone"]
    assert [(l["name"], l["sandbox_id"]) for l in gone] == [("agent-2", kid.id)]

    env.hub._apply_network_policy = _missing
    with pytest.raises(SandboxNotFoundError):
        await env.set_network_policy(NO_NET)
    del env.hub._apply_network_policy
    a3 = env.agents["agent-3"]
    a3._network_policy = PUBLIC  # it took NO_NET while the hub failed

    async def broken(*a, **k):
        raise RuntimeError("500")
    a3._apply_network_policy = broken  # any other error on a live agent
    with pytest.raises(RuntimeError, match="500"):
        await env.set_network_policy(NO_NET)
    a3._apply_network_policy = _missing  # a 404 the lookup does not confirm
    with pytest.raises(SandboxNotFoundError):
        await env.set_network_policy(NO_NET)


async def test_failed_export_names_its_step_and_stop_saves_the_repo_before_deleting_the_hub(started):
    env, client = started
    hub = client.created[0]
    answer = hub._answer
    hub._answer = lambda cmd: ("", 124) if "bundle create /logs/artifacts" in cmd else answer(cmd)
    with pytest.raises(RuntimeError, match=r"bundle failed with exit 124 \(timed out after 600s\)"):
        await env.export_artifacts()
    trial = env.ledger_path.parent
    seen = {}
    destroy = hub.destroy

    async def spy():
        seen["bundle"] = (trial / "team.bundle").is_file()
        await destroy()
    hub.destroy = spy
    await env.stop(delete=True)
    assert seen == {"bundle": True} and (trial / "board-events.jsonl").is_file()
    assert any(l.get("event") == "team_work_saved" for l in _ledger(env))
    env.team_work_saved = None
    (trial / "team.bundle").write_text("first")
    hub.destroy = destroy
    env.hub.sb = hub
    assert (await env.save_team_work()).name == "team-1.bundle"
    assert (trial / "team.bundle").read_text() == "first"


async def test_lost_agent_is_logged_ledgered_and_listed(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    env = FakeEnv(3)
    entries, lost = [], []
    env._ledger = lambda **e: entries.append(e)
    real = env.service_exec

    async def exec_(command, *, service=None, env=None, **kw):
        if service == "agent-2" and env and "MULTI_AGENT_HARNESS_PROMPT" in env:
            raise SandboxNotFoundError("404")
        return await real(command, service=service, env=env, **kw)
    env.service_exec = exec_
    agent = TeamAgent(tmp_path, model_name="m", harness="codex", timeout_sec="600")
    agent.orchestration.on_lost = lambda team, name: lost.append(name)
    await agent.setup(env)
    ctx = AgentContext()
    await agent.run("x", env, ctx)
    assert "agent-2 lost: SandboxNotFoundError after 0m00s" in caplog.text
    assert ctx.metadata["lost_agents"] == ["agent-2"] and ctx.metadata["end_reason"] == "all_finished"
    assert lost == ["agent-2"]
    assert {"event": "agent_lost", "name": "agent-2", "error": "SandboxNotFoundError", "after": "0m00s"} in entries
    assert any(e.get("lost_agents") == ["agent-2"] for e in entries)


# ---- rate limits, and a sandbox the backend stops

def _err(status, cause_headers=None, **ctx):
    e = SandboxExecutionError("Daytona set_network failed.", backend="daytona", op="set_network",
                              context={"provider_status": status, **ctx})
    if cause_headers is not None:
        e.__cause__ = DaytonaError("rate limited", status_code=status, headers=cause_headers)
    return e


@pytest.mark.parametrize("error", [_err(429), _err(408), _err(429, retry_after=0), _err(429, {"Retry-After": "0"})],
                         ids=["429", "408", "429 retry_after", "429 Retry-After header"])
async def test_rate_limit_and_request_timeout_are_retried(instant_retries, error):
    errors = [error]

    async def call():
        if errors:
            raise errors.pop()
        return "ok"
    async for attempt in sbmod._patient(sbmod._retry_idempotent, "x", logging.getLogger("t")):
        with attempt:
            result = await call()
    assert result == "ok" and not errors


@pytest.mark.parametrize("status", [400, 403, 409, 422])
async def test_other_4xx_are_refusals(status):
    assert sbmod._refusal(_err(status)) and not sbmod._retry_idempotent(_err(status))


async def test_retry_after_is_honored_and_capped_by_the_patient_window():
    def state(e, elapsed=0.0):
        return SimpleNamespace(outcome=SimpleNamespace(exception=lambda: e), seconds_since_start=elapsed)
    assert sbmod._patient_sleep(state(_err(429, retry_after=7))) == 7
    assert sbmod._patient_sleep(state(_err(408, {"retry-after": "12"}))) == 12
    assert sbmod._patient_sleep(state(_err(429, retry_after=900), elapsed=250)) == sbmod.PATIENT_SEC - 250
    assert sbmod._retry_after(_err(503, retry_after=7)) is None  # only on 408 and 429


@pytest.mark.parametrize("outcome, retried", [(ErrorOutcome.FAILED, True), (ErrorOutcome.UNKNOWN, False)])
async def test_create_is_retried_on_a_failed_429_only(tmp_path, monkeypatch, client, instant_retries, outcome,
                                                      retried):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SECRET-VALUE")
    fail_creates(client, [create_error(429, outcome)])
    env = make_env(tmp_path)
    if retried:
        await env.start(force_build=False)
        assert len(client.created) == 4
    else:
        with pytest.raises(SandboxCreationError):
            await env.start(force_build=False)
        assert client.created == []


async def test_daytona_create_options():
    opts = sbmod.create_options("daytona", {"a": "b"}, 600.4)
    assert opts == {"labels": {"a": "b"}, "ephemeral": True, "auto_stop_interval": 0, "auto_delete_interval": 0,
                    "create_timeout": 600}
    assert sbmod.create_options("docker", {}, 600) == {}


async def _stopped(*a, **k):
    raise SandboxExecutionError("Daytona execute failed.", backend="daytona", op="execute",
                                context={"provider_status": 400})


async def test_stopped_agent_is_dropped_but_a_stopped_hub_or_unknown_state_still_fails(started):
    env, client = started
    hub, _, kid, a3 = client.created
    kid.raw.state = "stopped"
    kid.execute_command = _stopped
    env.agents["agent-2"]._apply_network_policy = _stopped
    await env.set_network_policy(NO_NET)
    gone = [l for l in _ledger(env) if l.get("event") == "agent_gone"]
    assert [(l["name"], l["sandbox_id"], l["state"]) for l in gone] == [("agent-2", kid.id, "stopped")]

    hub.raw.state = "stopped"
    env.hub._apply_network_policy = _stopped
    with pytest.raises(SandboxExecutionError):
        await env.set_network_policy(PUBLIC)
    del env.hub._apply_network_policy
    hub.raw.state = "started"

    a3.raw.state = None  # the backend shows no state
    env.agents["agent-3"]._network_policy = NO_NET  # it took PUBLIC while the hub failed
    env.agents["agent-3"]._apply_network_policy = _stopped
    with pytest.raises(SandboxExecutionError):
        await env.set_network_policy(PUBLIC)
    assert [l["name"] for l in _ledger(env) if l.get("event") == "agent_gone"] == ["agent-2"]
