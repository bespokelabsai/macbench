"""Mixed teams (--ak team, roster.py): each agent sandbox runs its own harness and model."""

import json
import logging

import pytest
from harbor.models.agent.context import AgentContext

import multi_agent_harness.harbor.agent as ta
from multi_agent_harness.harbor import checkpoint as ck
from multi_agent_harness.harbor import roster, upstream
from multi_agent_harness.harbor.agent import TeamAgent
from test_agent import CLAUDE_TOKEN, HUB, FakeEnv, FakeInstaller, SubscriptionEnv, fake_installer  # noqa: F401
from test_checkpoint import write_checkpoint
from test_environment import NO_NET, SdkPolicy, _codex_login, client, make_env  # noqa: F401 - fixture

# With --ak harness=codex --ak model=gpt-6-astra: two Codex agents on gpt-6-astra, the rest Claude Code.
TEAM = [{"harness": "codex", "count": 2}, {"harness": "claude-code", "model": "claude-opus-5-5"}]
AGENTS = ["agent-1", "agent-2", "agent-3", "agent-4"]


def test_groups_hand_out_harness_model_and_cli_version_in_order():
    [one] = roster.groups(None, "codex", None)
    assert one == roster.Group("codex", "gpt-6-luna") and roster.assign([one], AGENTS[:2]) == {
        "agent-1": one, "agent-2": one}
    groups = roster.groups(json.dumps(TEAM), "codex", "openai/gpt-6-astra", "0.99.0")  # a JSON string works too
    assert groups == [roster.Group("codex", "openai/gpt-6-astra", "0.99.0", 2),
                      roster.Group("claude-code", "claude-opus-5-5", None, None)]
    seats = roster.assign(groups, AGENTS)
    assert [seats[a].harness for a in AGENTS] == ["codex", "codex", "claude-code", "claude-code"]
    assert seats["agent-1"].short_model == "gpt-6-astra"
    assert roster.describe(seats) == ("agent-1..agent-2 codex (gpt-6-astra), "
                                      "agent-3..agent-4 claude-code (claude-opus-5-5)")
    # a group on another harness gets that harness's default model, never --ak model
    other = roster.groups([{"harness": "claude-code", "count": 1}, {"harness": "antigravity"}, {"count": 1}],
                          "codex", "gpt-6-astra")
    assert [g.model for g in other] == [None, "gemini-3.8-flash-low", "gpt-6-astra"]
    assert roster.label(["codex", "codex"]) == "codex" and roster.label(["codex", "claude-code"]) == "mixed"
    assert roster.harnesses({"harness": "codex", "team": TEAM}, AGENTS) == {
        "agent-1": "codex", "agent-2": "codex", "agent-3": "claude-code", "agent-4": "claude-code"}
    assert roster.harnesses(None, AGENTS[:1]) == {"agent-1": "codex"}


@pytest.mark.parametrize("team, error", [
    ("codex x2", "JSON list of groups"),
    ({"harness": "codex"}, "nonempty list"),
    ([{"harness": "codex", "n": 2}], r"unknown keys \['n'\]"),
    ([{"count": 0}], "count must be an integer >= 1"),
    ([{"count": True}], "count must be an integer >= 1"),
    ([{"model": 5}], "model must be a string"),
    ([{}, {"harness": "claude-code"}], "only one group may leave out its count"),
])
def test_bad_groups_are_refused(team, error):
    with pytest.raises(ValueError, match=error):
        roster.groups(team)


def test_counts_must_fit_the_team():
    with pytest.raises(ValueError, match="add up to 5, but the team has 4 agents"):
        roster.assign(roster.groups([{"count": 2}, {"count": 3}]), AGENTS)
    with pytest.raises(ValueError, match="leaves none of the 4 agents"):
        roster.assign(roster.groups([{"count": 4}, {"harness": "claude-code"}]), AGENTS)


async def test_each_agent_installs_configures_and_launches_its_own_cli(tmp_path, monkeypatch, caplog):
    installs = []

    def per_harness(harness):
        class Installer(FakeInstaller):
            def __init__(self, logs_dir, model_name=None, version=None, **kw):
                super().__init__(logs_dir, model_name, version)
                installs.append((logs_dir.name, harness, model_name, version))
        return Installer

    monkeypatch.setattr(ta, "installer_class", per_harness)
    caplog.set_level(logging.INFO)
    env = FakeEnv(4)
    agent = TeamAgent(tmp_path, harness="codex", model="gpt-6-astra", cli_version="0.99.0", timeout_sec="600",
                      team=TEAM)
    assert agent.harness == "mixed" and agent.model_name is None  # no one model for Harbor's record
    await agent.setup(env)
    assert sorted(installs) == [("agent-1", "codex", "gpt-6-astra", "0.99.0"), ("agent-2", "codex", "gpt-6-astra", "0.99.0"),
                                ("agent-3", "claude-code", "claude-opus-5-5", None),
                                ("agent-4", "claude-code", "claude-opus-5-5", None)]
    assert ("installed and registered 4 agents: agent-1..agent-2 codex (gpt-6-astra), "
            "agent-3..agent-4 claude-code (claude-opus-5-5)") in caplog.text
    ctx = AgentContext()
    await agent.run("Write team.txt.", env, ctx)

    codex_env = env.files[("agent-2", "/run/multi-agent-harness/agent.env")]
    claude_env = env.files[("agent-3", "/run/multi-agent-harness/agent.env")]
    assert f"OPENAI_BASE_URL={HUB}/proxy/openai/v1" in codex_env and "ANTHROPIC" not in codex_env
    assert f"ANTHROPIC_BASE_URL={HUB}/proxy/anthropic" in claude_env and "ANTHROPIC_API_KEY=PKEY-agent-3" in claude_env
    assert "ANTHROPIC_MODEL=claude-opus-5-5" in claude_env and "CODEX_HOME" not in claude_env
    assert 'model = "gpt-6-astra"' in env.files[("agent-2", "/run/multi-agent-harness/codex/config.toml")]
    assert ("agent-3", "/run/multi-agent-harness/codex/config.toml") not in env.files
    config = [c["command"] for c in env.calls if c["service"] == "agent-4" and "git remote add origin" in c["command"]]
    assert len(config) == 1 and "claude mcp add --scope user --transport http board" in config[0]

    runs = {c["service"]: c["command"] for c in env.calls if "MULTI_AGENT_HARNESS_PROMPT" in c["env"]}
    assert sorted(runs) == AGENTS
    for a in ("agent-1", "agent-2"):
        assert "codex exec --dangerously-bypass-approvals-and-sandbox" in runs[a] and "--model gpt-6-astra " in runs[a]
    for a in ("agent-3", "agent-4"):
        assert "| timeout -k 30 600 claude --verbose" in runs[a] and "--model claude-opus-5-5 " in runs[a]
    # a wake resumes each agent's session in its own CLI
    assert "codex exec resume" in agent._command("/app", session=2, resume=True, slot="agent-1")
    assert 'claude --resume "$(cat /logs/agent/session-id)"' in agent._command("/app", session=2, resume=True,
                                                                               slot="agent-4")
    with pytest.raises(ValueError, match="claude-code, which can't resume it"):
        agent.launch("agent-3", "Wake up.", identity="agent-1", board_token="BTOK-x", resume=True)

    m = ctx.metadata
    assert m["harness"] == "mixed"
    assert {a: (m["agents"][a]["harness"], m["agents"][a]["model"]) for a in AGENTS} == {
        "agent-1": ("codex", "gpt-6-astra"), "agent-2": ("codex", "gpt-6-astra"),
        "agent-3": ("claude-code", "claude-opus-5-5"), "agent-4": ("claude-code", "claude-opus-5-5")}


async def test_word_names_follow_each_sandbox_and_its_own_cli(tmp_path):
    """Agent-facing word names (#5, #6) with a mixed team: a sandbox's group is looked up by its slot,
    and its agent.env carries its word name."""
    from multi_agent_harness import names as naming
    env = FakeEnv(4)
    env.team_agent_names = naming.assign(env.team_agent_services, "mixed__abc__env")
    env.team_agent_names_mode, env.team_name_seed = naming.WORDS, "mixed__abc__env"
    env.team_git_tokens = {v: f"GTOK-{v}" for v in env.team_agent_names.values()}
    agent = TeamAgent(tmp_path, harness="codex", model="gpt-6-astra", team=TEAM)
    await agent.setup(env)
    ctx = AgentContext()
    await agent.run("Write team.txt.", env, ctx)
    for slot in AGENTS:
        body = env.files[(slot, "/run/multi-agent-harness/agent.env")]
        assert f"MULTI_AGENT_HARNESS_AGENT_NAME={env.team_agent_names[slot]}\n" in body
        assert ("ANTHROPIC_MODEL=claude-opus-5-5" in body) == (slot in ("agent-3", "agent-4"))
        assert ("CODEX_HOME" in body) == (slot in ("agent-1", "agent-2"))
    m = ctx.metadata
    assert m["agent_names"] == env.team_agent_names
    assert {a: m["agents"][a]["harness"] for a in AGENTS} == {
        "agent-1": "codex", "agent-2": "codex", "agent-3": "claude-code", "agent-4": "claude-code"}


class MixedSubscriptionEnv(SubscriptionEnv):
    """What the environment resolves for a mixed team on model_upstream=subscription."""
    team_model_upstream = "subscription"
    team_claude_subscription = True


async def test_on_subscriptions_claude_code_runs_on_the_token_and_codex_through_the_proxy(tmp_path, monkeypatch,
                                                                                          caplog):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", CLAUDE_TOKEN)
    caplog.set_level(logging.DEBUG)
    env = MixedSubscriptionEnv(4)
    agent = TeamAgent(tmp_path, harness="codex", model="gpt-6-astra", team=TEAM)
    await agent.setup(env)
    ctx = AgentContext()
    await agent.run("Write team.txt.", env, ctx)

    codex_env = env.files[("agent-1", "/run/multi-agent-harness/agent.env")]
    claude_env = env.files[("agent-4", "/run/multi-agent-harness/agent.env")]
    assert CLAUDE_TOKEN not in codex_env and "OPENAI_API_KEY=PKEY-agent-1" in codex_env
    assert f"CLAUDE_CODE_OAUTH_TOKEN={CLAUDE_TOKEN}\n" in claude_env and "ANTHROPIC_BASE_URL" not in claude_env
    assert all(CLAUDE_TOKEN not in c["command"] + json.dumps(c["env"]) for c in env.calls)
    assert sorted(c["service"] for c in env.calls if "grep -rlF" in c["command"]) == ["agent-3", "agent-4"]
    assert "Claude Code agents (agent-3, agent-4) bill" in caplog.text and "only for the other agents" in caplog.text
    assert sum(CLAUDE_TOKEN in p.read_text() for p in tmp_path.rglob("*") if p.is_file()) == 0

    # the proxy's $1.25 (the Codex agents) plus the two Claude Code agents' transcripts at $0.006 each;
    # the fake leaves transcripts in every agent's logs, and only the Claude Code agents' count
    assert ctx.cost_usd == pytest.approx(1.262)
    assert ctx.n_input_tokens == 100 + 4000 and ctx.n_output_tokens == 20 + 800
    m = ctx.metadata
    assert (m["billed_as"], m["budget_enforced"], m["cost_source"]) == ("mixed", False, "proxy+transcripts")
    assert m["agents"]["agent-3"]["billed_as"] == "claude-subscription"
    assert m["agents"]["agent-3"]["cost_usd"] == pytest.approx(0.006)
    assert m["agents"]["agent-1"]["billed_as"] == "proxy"
    usage = json.loads((tmp_path / "subscription-usage.json").read_text())
    assert usage["total"]["requests"] == 4 and sorted(usage["agents"]) == ["agent-3", "agent-4"]


async def test_upstream_mismatches_fail_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", CLAUDE_TOKEN)
    with pytest.raises(ValueError, match="only Claude Code"):  # claude-subscription has no route for Codex
        await TeamAgent(tmp_path, team=TEAM).setup(SubscriptionEnv(4))
    logins_only = FakeEnv(4)
    logins_only.team_model_upstream = "subscription"
    with pytest.raises(ValueError, match="could not read --ak harness or --ak team"):
        await TeamAgent(tmp_path, team=TEAM).setup(logins_only)
    with pytest.raises(ValueError, match="add up to 3, but the team has 4 agents"):
        await TeamAgent(tmp_path, team=[{"count": 2}, {"harness": "claude-code", "count": 1}]).setup(FakeEnv(4))
    with pytest.raises(ValueError, match="only knows how to configure"):
        TeamAgent(tmp_path, team=[{"count": 1}, {"harness": "gemini-cli"}])
    with pytest.raises(ValueError, match="pinned"):
        TeamAgent(tmp_path, team=[{"count": 1}, {"harness": "antigravity", "cli_version": "1.0.0"}])


def test_a_mixed_team_on_subscriptions_gets_both_upstreams(tmp_path):
    _codex_login(tmp_path / ".codex" / "auth.json", 1)
    environ = {"HOME": str(tmp_path), "CLAUDE_CODE_OAUTH_TOKEN": CLAUDE_TOKEN}
    u = upstream.resolve("subscription", environ, "t1", harness={"codex", "claude-code"})
    assert (u.mode, u.claude_subscription, u.agent_hosts) == ("subscription", True, ["api.anthropic.com"])
    assert u.hosts == ["chatgpt.com", "auth.openai.com"] and u.proxy_env() == {
        "PROXY_OPENAI_UPSTREAM": upstream.CHATGPT_CODEX_URL}
    assert {CLAUDE_TOKEN, "AT-SECRET-1"} <= set(u.secrets) and CLAUDE_TOKEN not in repr(u)
    assert not upstream.resolve("subscription", environ, "t1", harness="codex").claude_subscription
    with pytest.raises(ValueError, match="claude setup-token"):
        upstream.resolve("subscription", {"HOME": str(tmp_path)}, "t1", harness=["codex", "claude-code"])


def test_a_mixed_team_on_subscriptions_keeps_the_other_formats_on_their_api_keys(tmp_path):
    _codex_login(tmp_path / ".codex" / "auth.json", 1)
    base = {"HOME": str(tmp_path), "ANTHROPIC_API_KEY": "AKEY", "GEMINI_API_KEY": "GKEY"}
    three = {"codex", "claude-code", "antigravity"}
    # Claude Code through the proxy on ANTHROPIC_API_KEY (MULTI_AGENT_HARNESS_CLAUDE_ROUTE=direct), Antigravity on its key
    u = upstream.resolve("subscription", {**base, upstream.CLAUDE_ROUTE_VAR: "direct"}, "t1", harness=three)
    assert not u.claude_subscription and not u.agent_hosts
    assert (u.urls["anthropic"], u.keys["anthropic"]) == (upstream.DIRECT["anthropic"], "AKEY")
    assert (u.urls["gemini"], u.keys["gemini"]) == (upstream.DIRECT["gemini"], "GKEY")
    # with no route, Claude Code runs on the token and Antigravity still on its key
    u = upstream.resolve("subscription", {**base, "CLAUDE_CODE_OAUTH_TOKEN": CLAUDE_TOKEN}, "t1", harness=three)
    assert u.claude_subscription and "anthropic" not in u.urls and u.keys["gemini"] == "GKEY"


def test_each_group_may_carry_its_own_effort(tmp_path):
    team = [{"harness": "claude-code", "model": "claude-opus-5-5", "count": 1, "effort": "max"},
            {"harness": "codex", "model": "gpt-6-astra", "count": 1, "effort": "high"},
            {"harness": "antigravity", "count": 1}]
    agent = TeamAgent(tmp_path / "a", harness="codex", team=team)
    agent._seats = roster.assign(agent._groups, AGENTS[:3])
    assert "--model claude-opus-5-5 --effort max " in agent._command(slot="agent-1")
    assert "-c model_reasoning_effort=high " in agent._command(slot="agent-2")
    assert "--effort" not in agent._command(slot="agent-3")
    # a team-wide effort goes only to the groups whose harness takes it
    two = [{"harness": "codex", "count": 1}, {"harness": "antigravity", "count": 1}]
    agent = TeamAgent(tmp_path / "b", harness="codex", effort="xhigh", team=two)
    agent._seats = roster.assign(agent._groups, AGENTS[:2])
    assert "-c model_reasoning_effort=xhigh " in agent._command(slot="agent-1")
    assert "model_reasoning_effort" not in agent._command(slot="agent-2")
    with pytest.raises(ValueError, match="not an effort any harness in the team takes"):
        TeamAgent(tmp_path / "c", harness="codex", effort="ultra", team=two)
    with pytest.raises(ValueError, match="not an effort codex takes"):
        TeamAgent(tmp_path / "d", harness="codex", team=[{"harness": "codex", "count": 1, "effort": "ultra"}])


def _trial_config(tmp_path, kwargs):
    (tmp_path / "trial").mkdir(exist_ok=True)
    (tmp_path / "trial" / "config.json").write_text(json.dumps({"agent": {"kwargs": kwargs}}))


async def test_only_the_claude_code_agents_reach_anthropic(tmp_path, monkeypatch, client, caplog):
    _codex_login(tmp_path / "pool" / "one.json", 1)
    monkeypatch.setenv(upstream.CODEX_LOGINS_VAR, str(tmp_path / "pool"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", CLAUDE_TOKEN)
    _trial_config(tmp_path, {"harness": "codex", "team": [{"count": 2}, {"harness": "claude-code"}]})
    env = make_env(tmp_path, n=3, model_upstream="subscription")
    with caplog.at_level(logging.DEBUG):
        await env.start(force_build=False)
        await env.set_network_policy(NO_NET)
    hub, *kids = client.created
    assert (env.team_model_upstream, env.team_claude_subscription) == ("subscription", True)
    assert hub.net == SdkPolicy("allowlist", domains=("chatgpt.com", "auth.openai.com"))
    name = {k: k.spec["backend_options"]["labels"]["multi-agent-harness.name"] for k in kids}
    assert {name[k]: set(k.net.domains) for k in kids} == {
        "agent-1": {env.team_hub_host}, "agent-2": {env.team_hub_host},
        "agent-3": {env.team_hub_host, "api.anthropic.com"}}
    assert {name[k]: any("https://api.anthropic.com" in c for c in k.commands) for k in kids} == {
        "agent-1": False, "agent-2": False, "agent-3": True}  # checked from inside where it is allowed
    for sb in client.created:  # the environment never ships the token anywhere
        assert all(CLAUDE_TOKEN not in c for c in sb.commands) and CLAUDE_TOKEN not in str(sb.spec)
        assert all(CLAUDE_TOKEN.encode() not in v for v in sb.uploads.values())
    assert CLAUDE_TOKEN not in caplog.text and CLAUDE_TOKEN not in env.ledger_path.read_text()
    ledger = [json.loads(line) for line in env.ledger_path.read_text().splitlines()]
    assert any(e.get("agent_harnesses") == {"agent-1": "codex", "agent-2": "codex", "agent-3": "claude-code"}
               for e in ledger)


async def test_a_team_that_does_not_fit_n_agents_fails_before_any_sandbox(tmp_path, client):
    _trial_config(tmp_path, {"team": [{"count": 3}, {"harness": "claude-code"}]})
    with pytest.raises(ValueError, match="leaves none of the 3 agents"):
        await make_env(tmp_path, n=3).start(force_build=False)
    assert client.created == []


def test_resume_refuses_an_agent_whose_harness_changed(tmp_path):
    ckdir = write_checkpoint(tmp_path / "ck", n_agents=2, harness="mixed")
    m = json.loads((ckdir / ck.MANIFEST).read_text())
    m["agents"] = {"agent-1": {"status": "finished", "session": "saved", "harness": "codex"},
                   "agent-2": {"status": "finished", "session": "saved", "harness": "claude-code"}}
    (ckdir / ck.MANIFEST).write_text(json.dumps(m))
    same = {"agent-1": "codex", "agent-2": "claude-code"}
    assert ck.load(ckdir, task="hello-team", harness="mixed", n_agents=2, harnesses=same)["path"] == ckdir
    with pytest.raises(ValueError, match="agent-1 runs 'codex' in the checkpoint but 'claude-code' in this run; "
                                         "agent-2 runs 'claude-code' in the checkpoint but 'codex'"):
        ck.load(ckdir, task="hello-team", harness="mixed", n_agents=2,
                harnesses={"agent-1": "claude-code", "agent-2": "codex"})
