"""Agent-facing names: words by default, per-agent list order, numbered on request, kept on resume."""

import json
import re

import pytest
from harbor.models.agent.context import AgentContext

from multi_agent_harness import names as naming
from multi_agent_harness.board import Board
from multi_agent_harness.board.subagents import Pool
from multi_agent_harness.harbor import agent as ta
from multi_agent_harness.harbor.agent import TeamAgent
from test_agent import fake_installer  # noqa: F401 - autouse fixture
from test_checkpoint import CkptEnv, write_checkpoint
from test_environment import client, make_env  # noqa: F401 - fixture

SEED = "hello-team__abc__env"
NUMBERED = re.compile(r"agent-\d")
WORD_NAME = r"[a-z]+-[a-z]+-[a-z]+ing"


def test_word_names_are_unique_and_deterministic_per_seed():
    slots = [f"agent-{i}" for i in range(1, 9)]
    a = naming.assign(slots, SEED)
    assert list(a) == slots and len(set(a.values())) == 8
    assert all(re.fullmatch(WORD_NAME, n) for n in a.values())
    assert naming.assign(slots, SEED) == a and naming.assign(slots, "other__env") != a
    assert naming.assign(slots, SEED, "numbered") == {s: s for s in slots}
    assert naming.check_mode("hashed") == naming.check_mode(None) == "words"
    with pytest.raises(ValueError, match="agent_names"):
        naming.check_mode("letters")


def test_word_lists_carry_no_rank_or_lead_cue():
    assert len(set(naming.NOUNS)) == 64 and len(set(naming.VERBS)) == 32
    words = naming.NOUNS + naming.VERBS
    assert all(re.fullmatch(r"[a-z]+", w) and not w.startswith("lead") for w in words)
    rank = {"alpha", "first", "chief", "prime", "captain", "boss", "head", "top", "king", "leading", "winning"}
    assert not rank & set(words)


def named_env(n, mode):
    env = CkptEnv(n)
    env.team_agent_names = naming.assign(env.team_agent_services, SEED, mode)
    env.team_agent_names_mode, env.team_name_seed = mode, SEED
    env.team_git_tokens = {v: f"GTOK-{v}" for v in env.team_agent_names.values()}
    return env


@pytest.mark.asyncio
async def run_team(tmp_path, env):
    agent = TeamAgent(tmp_path / "job" / "hello-team__abc" / "agent", harness="codex", orchestration="fixed-lead")
    await agent.setup(env)
    await agent.run("Write team.txt.", env, AgentContext())
    prompts = {c["service"]: c["env"]["MULTI_AGENT_HARNESS_PROMPT"] for c in env.calls
               if "MULTI_AGENT_HARNESS_PROMPT" in c["env"]}
    teammates = {s: re.search(r"Your teammates: (.*)\.\n", p).group(1).split(", ") for s, p in prompts.items()}
    return agent, prompts, teammates


@pytest.mark.asyncio
async def test_default_names_hide_slots_and_each_agent_has_its_own_order(tmp_path):
    env = named_env(6, "words")
    agent, prompts, teammates = await run_team(tmp_path, env)
    faced = [re.search(r"-d '(.*?)'", c["command"]).group(1) for c in env.calls if "/admin/agents" in c["command"]]
    faced += [v for (svc, path), v in env.files.items() if svc and path in (ta.AGENT_ENV, ta.GIT_CREDENTIALS)]
    assert len(prompts) == 6 and faced
    assert not [t for t in [*prompts.values(), *faced] if NUMBERED.search(t)]
    lead = env.team_agent_names["agent-1"]
    assert all(f"{lead} is the team lead" in p for s, p in prompts.items() if s != "agent-1")
    slot_of = {v: k for k, v in env.team_agent_names.items()}
    orders = {tuple(slot_of[n] for n in t) for t in teammates.values()}
    assert any(list(o) != sorted(o) for o in orders) and len({o[0] for o in orders}) > 1
    _, _, again = await run_team(tmp_path / "rerun", named_env(6, "words"))
    assert again == teammates  # seeded: the same trial id gets the same order
    m = json.loads(next((tmp_path / "job.checkpoints").rglob("manifest.json")).read_text())
    assert m["agent_names"] == env.team_agent_names and m["name_seed"] == SEED


@pytest.mark.asyncio
async def test_numbered_restores_old_names_and_ascending_order(tmp_path):
    env = named_env(4, "numbered")
    _, prompts, teammates = await run_team(tmp_path, env)
    assert prompts["agent-1"].startswith("You are agent-1, one of 4 agents")
    assert teammates == {s: [o for o in env.team_agent_services if o != s] for s in env.team_agent_services}
    assert any('"name": "agent-2"' in c["command"] for c in env.calls if "/admin/agents" in c["command"])


@pytest.mark.asyncio
async def test_resume_keeps_the_checkpoints_names(tmp_path, client):  # noqa: F811
    fresh = make_env(tmp_path / "a")
    fresh._assign_names()
    assert all(re.fullmatch(WORD_NAME, n) for n in fresh.team_agent_names.values())

    saved = {"agent-1": "agent-beef", "agent-2": "agent-c0de", "agent-3": "agent-d00d"}
    ckdir = write_checkpoint(tmp_path / "ck")
    m = json.loads((ckdir / "manifest.json").read_text())
    (ckdir / "manifest.json").write_text(json.dumps({**m, "agent_names": saved, "name_seed": "old__env"}))
    env = make_env(tmp_path / "b", resume_from=str(ckdir))
    await env.start(force_build=False)
    assert env.team_agent_names == saved and set(env.team_git_tokens) == set(saved.values())
    assert env.team_name_seed == "old__env" and env.team_agent_names_mode == "words"

    legacy = make_env(tmp_path / "c", resume_from=str(write_checkpoint(tmp_path / "ck2")))
    await legacy.start(force_build=False)
    assert legacy.team_agent_names == {s: s for s in legacy.team_agent_services}


def test_subagent_names_are_words_when_seeded(tmp_path):
    board = Board(tmp_path / "b.sqlite")
    board.policy.rate_per_sec = board.limiter.rate = 0
    board.register_agent("agent-beef", "lead")
    pool = Pool(board)
    pool.configure("agent-beef", ["agent-c0de", "agent-d00d"], name_seed="t:subagents")
    spawned = [pool.spawn("agent-beef", "x")["name"] for _ in range(3)]
    assert len(set(spawned)) == 3 and not [n for n in spawned if NUMBERED.search(n)]
    again = Pool(Board(tmp_path / "c.sqlite"))
    again.board.register_agent("agent-beef", "lead")
    again.board.policy.rate_per_sec = again.board.limiter.rate = 0
    again.configure("agent-beef", ["agent-c0de", "agent-d00d"], name_seed="t:subagents")
    assert [again.spawn("agent-beef", "x")["name"] for _ in range(3)] == spawned
