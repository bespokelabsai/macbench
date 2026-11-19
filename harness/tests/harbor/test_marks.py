"""Graded marks (--ak grade_at), against fakes: when marks fire, the grading queue, the final verifier as the
last mark, failures, the record in result.json metadata and checkpoints, and resume."""

import asyncio
import json
import math
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from harbor.environments.base import ExecResult
from harbor.models.agent.context import AgentContext

from multi_agent_harness.harbor import checkpoint as ck
from multi_agent_harness.harbor import marks
from multi_agent_harness.harbor.agent import TeamAgent
from multi_agent_harness.harbor.environment import TeamEnvironment
from test_agent import fake_installer  # noqa: F401 - autouse fixture
from test_checkpoint import CkptEnv, write_checkpoint
from test_environment import FakeContainer, client, make_env  # noqa: F401 - fixture

pytestmark = pytest.mark.asyncio


class MarkEnv(CkptEnv):
    """CkptEnv whose agents work for `agent_s`, with a team repo whose main moves on every 0.05 s and a
    verifier that takes `grade_s` and gives reward = the mark's time."""

    def __init__(self, n=2, agent_s=0.6, grade_s=0.3, fail=(), **kw):
        super().__init__(n, **kw)
        self.agent_s, self.grade_s, self.fail = agent_s, grade_s, set(fail)
        self.t0 = time.monotonic()
        self.gradings, self.grading_now, self.max_grading = [], 0, 0

    async def service_exec(self, command, *, service=None, cwd=None, env=None, timeout_sec=None, user=None):
        if "MULTI_AGENT_HARNESS_PROMPT" in (env or {}):
            self.calls.append({"service": service, "command": command, "env": env or {}})
            await asyncio.sleep(self.agent_s)
            return ExecResult(stdout="", return_code=0)
        return await super().service_exec(command, service=service, cwd=cwd, env=env, timeout_sec=timeout_sec)

    async def pin_main(self):
        return f"c{int((time.monotonic() - self.t0) / 0.05):04d}"

    async def grade_mark(self, label, commit, dest):
        self.grading_now += 1
        self.max_grading = max(self.max_grading, self.grading_now)
        self.gradings.append((label, commit, round(time.monotonic() - self.t0, 2)))
        try:
            await asyncio.sleep(self.grade_s)
            if label in self.fail:
                raise RuntimeError("verifier crashed")
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "reward.txt").write_text(f"{float(label[1:]):.3f}\n")
            return {"status": "graded", "reward": marks.read_reward(dest), "verifier_s": self.grade_s, "exit": 0}
        finally:
            self.grading_now -= 1


def final_verifier(env, agent, trial: Path, reward="0.9"):
    """What Harbor's verifier and TeamEnvironment.stop() do after the agent: the reward lands in
    <trial>/verifier, then finish_final_mark fills in the last record."""
    (trial / "verifier").mkdir(parents=True, exist_ok=True)
    (trial / "verifier" / "reward.txt").write_text(reward + "\n")
    (trial / "verifier" / "details.json").write_text("{}")
    fake = SimpleNamespace(team_marks=agent.marks, trial_paths=SimpleNamespace(verifier_dir=trial / "verifier",
                                                                               trial_dir=trial),
                           team_final_mark_t0=time.time() - 5, _ledger=env._ledger,
                           team_verify_commit=None, team_verify_checkout_late=False,
                           _verifier_ran_without_checkout=lambda: False)
    TeamEnvironment.finish_final_mark(fake)


async def test_parse_label_and_schedule():
    assert marks.parse("1h,2h,4h,8h,16h") == [3600, 7200, 14400, 28800, 57600]
    assert marks.parse("8m, 2m,4m") == [120, 240, 480] and marks.parse("90s,300") == [90, 300]
    assert marks.parse("") == [] and marks.parse(None) == []
    for bad, match in (("2x", "not a time"), ("0h", "after the start"), ("1h,60m", "twice")):
        with pytest.raises(ValueError, match=match):
            marks.parse(bad)
    assert [marks.label(s) for s in (3600, 57600, 120, 90, 0.2)] == ["h01", "h16", "m02", "s090", "s0.2"]
    assert marks.words(14400) == "hour 4" and marks.words(480) == "minute 8"
    # run_sweep's 8-minute run is 479.88 s: the 8-minute mark is its end, so it is the final verifier's
    plan = marks.schedule(marks.parse("2m,4m,8m"), 0.1333 * 3600)
    assert [(m["mark"], m["final"]) for m in plan] == [("m02", False), ("m04", False), ("m08", True)]
    plan = marks.schedule(marks.parse("1h,2h,4h"), 3 * 3600)  # 4h is past a 3 h run: dropped
    assert [(m["mark"], m["final"]) for m in plan] == [("h01", False), ("h02", False)]


async def test_read_reward(tmp_path):
    assert marks.read_reward(tmp_path) is None
    (tmp_path / "reward.txt").write_text("0.25\n")
    assert marks.read_reward(tmp_path) == 0.25
    (tmp_path / "reward.json").write_text('{"reward": 0.5, "other": 1}')
    assert marks.read_reward(tmp_path) == 0.5
    (tmp_path / "reward.json").write_text('"nan"')
    assert marks.read_reward(tmp_path) is None


async def test_marks_fire_on_time_queue_and_the_last_is_the_final_verifier(tmp_path):
    trial = tmp_path / "job" / "hello-team__abc"
    env = MarkEnv(2, agent_s=0.6, grade_s=0.3)
    agent = TeamAgent(trial / "agent", harness="codex", timeout_sec=1.0, grade_at="0.1s,0.2s,0.4s,1s",
                      checkpoint_every_min=15)
    await agent.setup(env)
    ctx = AgentContext()
    t0 = time.monotonic()
    await agent.run("Write team.txt.", env, ctx)
    took = time.monotonic() - t0

    # The marks fired at their times while the agents worked; the gradings ran one at a time, in order,
    # the last ones after the agents ended and before the run returned (so before the final verifier).
    assert env.max_grading == 1
    assert [g[0] for g in env.gradings] == ["s0.1", "s0.2", "s0.4"]
    starts = [g[2] for g in env.gradings]
    assert starts[1] >= starts[0] + 0.29 and starts[2] >= starts[1] + 0.29  # each waited for the one before
    assert took >= 0.1 + 3 * 0.3 - 0.05
    recs = {r["mark"]: r for r in agent.marks}
    assert list(recs) == ["s0.1", "s0.2", "s0.4", "s001"]
    for label, at in (("s0.1", 0.1), ("s0.2", 0.2), ("s0.4", 0.4)):
        r = recs[label]
        assert r["status"] == "graded" and r["reward"] == float(label[1:]) and r["final"] is False
        assert at - 0.01 <= r["elapsed_s"] <= at + 0.1
        # the commit pinned when the mark fired, not when its grading started
        assert r["commit"] == [g[1] for g in env.gradings if g[0] == label][0]
        assert int(r["commit"][1:]) <= int((at + 0.1) / 0.05)
        assert Path(r["dir"]) == (trial / "marks" / label).absolute() and (Path(r["dir"]) / "reward.txt").is_file()
        assert Path(r["dir"]).is_absolute()
        assert r["checkpoint"] and json.loads((Path(r["checkpoint"]) / "manifest.json").read_text())["mark"] == label
    assert recs["s0.2"]["commit"] != recs["s0.1"]["commit"]
    # the last mark is the run's end: not graded here, the final verifier's record waits for its reward
    final = recs["s001"]
    assert final["final"] and final["status"] == "awaiting_final_verifier" and "s001" not in [g[0] for g in env.gradings]
    assert ctx.metadata["grade_at"] == ["s0.1", "s0.2", "s0.4", "s001"]
    final_verifier(env, agent, trial, "0.9")
    assert ctx.metadata["marks"][-1]["reward"] == 0.9 and ctx.metadata["marks"][-1]["status"] == "graded"
    assert (trial / "marks" / "s001" / "reward.txt").read_text() == "0.9\n"
    assert [m["mark"] for m in ctx.metadata["marks"]] == ["s0.1", "s0.2", "s0.4", "s001"]
    events = [e for e in env.ledger if e.get("event") == "mark_graded"]
    assert [e["mark"] for e in events] == ["s0.1", "s0.2", "s0.4", "s001"]
    # mark checkpoints are kept by retention, and later manifests carry the graded marks
    root = ck.root_for(trial)
    # each mark's checkpoint has a plain name, and its manifest got the grade once grading finished
    for label in ("s0.1", "s0.2", "s0.4"):
        link = root / f"mark-{label}"
        assert link.is_symlink() and link.resolve() == Path(recs[label]["checkpoint"]).resolve()
        m = json.loads((link / "manifest.json").read_text())
        assert m["mark"] == label and [x for x in m["marks"] if x["mark"] == label][0]["status"] == "graded"
    ck.prune(root, keep=0)
    kept = [json.loads((p / "manifest.json").read_text()) for _, p in ck.listing(root)]
    assert sum(1 for m in kept if m.get("mark")) == 3 and len(kept) == 4
    last = [m for m in kept if m.get("final")][0]
    assert [m["status"] for m in last["marks"]][:3] == ["graded"] * 3


async def test_a_failed_grading_is_recorded_and_the_run_goes_on(tmp_path):
    trial = tmp_path / "job" / "t"
    env = MarkEnv(2, agent_s=0.5, grade_s=0.05, fail={"s0.1"})
    agent = TeamAgent(trial / "agent", harness="codex", timeout_sec=1.0, grade_at="0.1s,0.3s", checkpoint_every_min=0)
    await agent.setup(env)
    ctx = AgentContext()
    await agent.run("Write team.txt.", env, ctx)
    recs = {r["mark"]: r for r in ctx.metadata["marks"]}
    assert recs["s0.1"]["status"] == "failed" and "verifier crashed" in recs["s0.1"]["error"]
    assert recs["s0.1"]["reward"] is None and recs["s0.3"]["status"] == "graded"
    # the last mark is before the end: the final verifier gets an "end" record of its own
    assert recs["end"]["final"] and recs["end"]["status"] == "awaiting_final_verifier"
    final_verifier(env, agent, trial, "not a number")
    assert recs["end"]["status"] == "failed"
    assert all(c["service"] for c in env.calls if "MULTI_AGENT_HARNESS_PROMPT" in c["env"])  # agents ran untouched


async def test_a_cancelled_run_finishes_its_gradings_and_a_second_cancel_stops_them(tmp_path):
    # Harbor's agent timeout cancels run(): the gradings still finish, so none is left running on the hub
    env = MarkEnv(2, agent_s=5.0, grade_s=0.2)
    agent = TeamAgent(tmp_path / "job" / "t" / "agent", harness="codex", timeout_sec=10, grade_at="0.05s,0.1s",
                      checkpoint_every_min=0)
    await agent.setup(env)
    task = asyncio.create_task(agent.run("Write team.txt.", env, AgentContext()))
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [r["status"] for r in agent.marks if not r.get("final")] == ["graded", "graded"]
    # a second cancel while they finish (an impatient Ctrl-C) cancels them
    env = MarkEnv(2, agent_s=5.0, grade_s=5.0)
    agent = TeamAgent(tmp_path / "job" / "t2" / "agent", harness="codex", timeout_sec=10, grade_at="0.05s,0.1s",
                      checkpoint_every_min=0)
    await agent.setup(env)
    task = asyncio.create_task(agent.run("Write team.txt.", env, AgentContext()))
    await asyncio.sleep(0.3)
    task.cancel()
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert {r["status"] for r in agent.marks if not r.get("final")} == {"cancelled"}


async def test_resume_keeps_graded_marks_and_regrades_an_unfinished_one(tmp_path):
    old = tmp_path / "old" / "t1" / "marks" / "s0.1"
    old.mkdir(parents=True)
    (old / "reward.txt").write_text("0.1\n")
    ckdir = write_checkpoint(tmp_path / "job.checkpoints" / "t" / "3-20260101T000000Z", n_agents=2, elapsed=0.2,
                             timeout=1200)
    m = json.loads((ckdir / ck.MANIFEST).read_text())
    m["marks"] = [{"mark": "s0.1", "mark_s": 0.1, "elapsed_s": 0.1, "commit": "aaa", "reward": 0.1, "status": "graded",
                   "dir": str(old), "final": False},
                  {"mark": "s0.2", "mark_s": 0.2, "elapsed_s": 0.2, "commit": "bbb", "reward": None, "status": "grading",
                   "final": False}]
    (ckdir / ck.MANIFEST).write_text(json.dumps(m))
    env = MarkEnv(2, agent_s=0.3, grade_s=0.05)
    env.team_resume = ck.load(ckdir, task="hello-team", harness="codex", n_agents=2)
    trial = tmp_path / "job2" / "t2"
    agent = TeamAgent(trial / "agent", harness="codex", grade_at="0.1s,0.2s,0.35s", checkpoint_every_min=0)
    await agent.setup(env)
    ctx = AgentContext()
    await agent.run("Write team.txt.", env, ctx)
    recs = {r["mark"]: r for r in ctx.metadata["marks"]}
    assert recs["s0.1"]["status"] == "graded" and recs["s0.1"]["resumed"] and recs["s0.1"]["commit"] == "aaa"
    assert (trial / "marks" / "s0.1" / "reward.txt").read_text() == "0.1\n"
    # the mark the checkpoint had not finished grading is graded again, at its pinned commit, first
    assert env.gradings[0][:2] == ("s0.2", "bbb") and recs["s0.2"]["regraded_after_resume"]
    assert recs["s0.2"]["status"] == "graded"
    # 0.35 s of agent time is 0.15 s into this run (0.2 s were used before)
    assert [g[0] for g in env.gradings] == ["s0.2", "s0.35"] and recs["s0.35"]["elapsed_s"] >= 0.35
    # the agents' resume note gives the time left and the next mark
    prompts = [c["env"]["MULTI_AGENT_HARNESS_PROMPT"] for c in env.calls if "MULTI_AGENT_HARNESS_PROMPT" in c["env"]]
    assert all("The next grading of main is at second 0.35 of the run" in p for p in prompts), prompts


async def test_grade_at_needs_an_environment_that_grades(tmp_path):
    from test_agent import FakeEnv
    agent = TeamAgent(tmp_path / "agent", harness="codex", grade_at="1h")
    with pytest.raises(ValueError, match="grade_at needs an environment"):
        await agent.setup(FakeEnv(2))


async def test_next_mark_for_strategies(tmp_path):
    env = MarkEnv(2)
    agent = TeamAgent(tmp_path / "agent", harness="codex", timeout_sec=8 * 3600, grade_at="1h,2h,4h,8h")
    await agent.setup(env)
    agent._phase_t0 = time.monotonic() - 5400  # 1.5 h in
    nxt = agent.next_mark()
    assert nxt["mark"] == "h02" and nxt["words"] == "hour 2" and 1799 <= nxt["in_s"] <= 1801 and not nxt["final"]
    agent._phase_t0 = time.monotonic() - 5 * 3600
    assert agent.next_mark()["final"]


# ---- TeamEnvironment.grade_mark on the hub

def task_dir(tmp_path):
    (tmp_path / "task.toml").write_text(
        '[verifier]\ntimeout_sec = 600.0\nenv = { TASK_SEEDS = "4" }\n[agent]\ntimeout_sec = 3600.0\n')
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "test.sh").write_text("#!/bin/bash\necho graded\n")


async def test_grade_mark_runs_the_task_verifier_on_a_checkout_of_the_pinned_commit(tmp_path, monkeypatch, client):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    task_dir(tmp_path)
    env = make_env(tmp_path, n=1, override_cpus=2, override_memory_mb=4096, hub_cpus=8, hub_memory_mb=16384,
                   backend="docker")
    await env.start(force_build=False)
    hub = client.created[0]
    container = FakeContainer()
    hub._sandbox._session._container = container
    got = []

    async def download_dir(src, dest):
        got.append(src)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "reward.txt").write_text("0.42\n")
        (dest / "details.json").write_text("{}")
    env.hub.download_dir = download_dir
    dest = tmp_path / "trial" / "marks" / "h01"
    out = await env.grade_mark("h01", "abc123", dest)
    assert out["status"] == "graded" and out["reward"] == 0.42 and out["exit"] == 0
    assert got == ["/logs/marks/h01"]
    cmds = "\n".join(hub.commands)
    assert "archive --format=tar abc123 | tar -x -C /var/lib/multi-agent-harness/marks/h01" in cmds
    assert "chmod 700 /logs/marks /var/lib/multi-agent-harness/marks" in cmds
    run = [c for c in hub.commands if "/tests/test.sh > /logs/marks/h01/test-stdout.txt" in c]
    assert len(run) == 1
    for needle in ("MULTI_AGENT_HARNESS_GRADE_APP=/var/lib/multi-agent-harness/marks/h01",
                   "MULTI_AGENT_HARNESS_GRADE_OUT=/logs/marks/h01", "MULTI_AGENT_HARNESS_GRADE_MARK=h01",
                   "MULTI_AGENT_HARNESS_GRADE_COMMIT=abc123", "TASK_SEEDS=4"):
        assert needle in run[0], needle
    assert "rm -rf /var/lib/multi-agent-harness/marks/h01" in hub.commands[-1] or any(
        "rm -rf /var/lib/multi-agent-harness/marks/h01" in c for c in hub.commands[-3:])
    # the tests went up root-only, once
    assert any("chmod 700 /tests" in c for c in hub.commands) and env._mark_tests_uploaded
    # the hub grew for the grading and went back to the agents' size; the final verifier grows it again
    assert [u.get("cpu_quota") for u in container.updates] == [800_000, 200_000]
    assert env.team_hub_resized is None
    uploads = sum("mkdir -p /tests" in c for c in hub.commands)
    await env.grade_mark("h02", "def456", tmp_path / "trial" / "marks" / "h02")
    assert any("/tests/test.sh > /logs/marks/h02/" in c for c in hub.commands)
    assert sum("mkdir -p /tests" in c for c in hub.commands) == uploads  # once per run


async def test_grade_mark_failures(tmp_path, monkeypatch, client):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    task_dir(tmp_path)
    env = make_env(tmp_path, n=1)
    await env.start(force_build=False)

    async def nothing(src, dest):
        dest.mkdir(parents=True, exist_ok=True)
    env.hub.download_dir = nothing
    out = await env.grade_mark("h01", "abc", tmp_path / "m")
    assert out["status"] == "failed" and "no reward" in out["error"]

    async def exits_124(*a, **k):  # the script's own exit code, well before its timeout: not a timeout
        return ExecResult(stdout="", stderr="", return_code=124)
    env.hub.exec = exits_124
    out = await env.grade_mark("h02", "abc", tmp_path / "m2")
    assert out["status"] == "failed" and out["exit"] == 124

    async def hangs(*a, **k):
        await asyncio.sleep(30)
    env.hub.exec = hangs
    env.trial_paths.config_path.write_text(json.dumps({"verifier": {"override_timeout_sec": 0.1}}))
    monkeypatch.setattr("multi_agent_harness.harbor.environment.MARK_SLACK_SEC", 0)
    monkeypatch.setattr("multi_agent_harness.harbor.environment.MARK_KILL_AFTER_SEC", 0)
    hub = client.created[0]
    n = len(hub.commands)
    out = await env.grade_mark("h03", "abc", tmp_path / "m3")
    assert out["status"] == "timeout" and out["reward"] is None
    # what is left of the mark's verifier on the hub is killed, by its output dir, before its checkout goes
    kill = [c for c in hub.commands[n:] if "/environ" in c]
    assert len(kill) == 1 and "MULTI_AGENT_HARNESS_GRADE_OUT=/logs/marks/h03" in kill[0] and "kill -KILL" in kill[0]


async def test_the_hub_lives_longer_with_marks(tmp_path, monkeypatch, client):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    task_dir(tmp_path)
    env = make_env(tmp_path, n=1)
    env.trial_paths.trial_dir.mkdir(parents=True, exist_ok=True)
    env.trial_paths.config_path.write_text(json.dumps({"agent": {"kwargs": {"grade_at": "1h"}}}))
    before = env.hub.ttl_minutes
    await env.start(force_build=False)
    assert env.hub.ttl_minutes == before + 10 and env._marks_extra_minutes == 10


async def test_hub_state_dirs_go_into_checkpoints_and_come_back_on_resume(tmp_path, monkeypatch, client):  # noqa: F811
    from multi_agent_harness.harbor.environment import RESUME_STATE, STATE_REMOTE, parse_state_dirs
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert parse_state_dirs("/var/lib/a, /srv/b/") == ["/var/lib/a", "/srv/b"] and parse_state_dirs(None) == []
    for bad in ("relative/x", "/a/../b", "/a b"):
        with pytest.raises(ValueError, match="hub_state_dirs"):
            parse_state_dirs(bad)
    env = make_env(tmp_path, n=1, hub_state_dirs="/var/lib/marsflight-hub")
    await env.start(force_build=False)
    hub = client.created[0]
    dest = tmp_path / "ck"
    dest.mkdir()
    await env.checkpoint_hub(dest)
    pack = [c for c in hub.commands if f"-czpf {STATE_REMOTE} -C /" in c]
    assert pack and "/var/lib/marsflight-hub" in pack[0] and (dest / ck.HUB_STATE).is_file()
    assert env.team_hub_state_saved  # recorded in the manifest as hub_state

    ckdir = write_checkpoint(tmp_path / "job.checkpoints" / "t" / "2-20260101T000000Z")
    (ckdir / ck.HUB_STATE).write_bytes(b"STATE")
    env2 = make_env(tmp_path / "r", n=3, resume_from=str(ckdir), hub_state_dirs="/var/lib/marsflight-hub")
    await env2.start(force_build=False)
    hub2 = client.created[-4]
    cmds = "\n".join(hub2.commands)
    assert hub2.uploads[RESUME_STATE] == b"STATE"
    # restored after the team repo and before the workdir seed and any board plugin loads
    assert cmds.index("fetch -q /tmp/multi-agent-harness-resume.bundle") < cmds.index(f"tar -xzpf {RESUME_STATE} -C /") \
        < cmds.index("multi_agent_harness.workdir seed")
    ledger = [json.loads(l) for l in env2.ledger_path.read_text().splitlines()]
    assert any(e.get("event") == "hub_state_restored" for e in ledger)


async def test_a_separate_verifier_task_is_refused(tmp_path, client):  # noqa: F811
    (tmp_path / "task.toml").write_text('[verifier]\nenvironment_mode = "separate"\n')
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test.sh").write_text("#!/bin/bash\n")
    env = make_env(tmp_path, n=1)
    with pytest.raises(ValueError, match="shared verifier mode"):
        env.mark_verifier()


async def test_grade_mark_runs_as_the_final_verifier_would(tmp_path, monkeypatch, client):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    task_dir(tmp_path)
    env = make_env(tmp_path, n=1)
    await env.start(force_build=False)
    hub = client.created[0]

    async def reward(src, dest):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "reward.txt").write_text("1\n")
    env.hub.download_dir = reward
    # the agent phase: Harbor's default user is the agent's, and its scoped env is on; the mark gets neither
    env.default_user = "agent"
    with env.scoped_exec_env({"AGENT_ONLY": "x"}):
        assert (await env.grade_mark("h01", "abc", tmp_path / "m1"))["status"] == "graded"
    run = [c for c in hub.commands if "/tests/test.sh > /logs/marks/h01/" in c][0]
    assert "su " not in run and "AGENT_ONLY" not in run and "timeout -k 30 600 /tests/test.sh" in run
    # a task with a verifier user: the mark runs as it, and it owns the checkout, the output and the tests
    (tmp_path / "task.toml").write_text('[verifier]\ntimeout_sec = 600.0\nuser = "tester"\n[agent]\ntimeout_sec = 3600.0\n')
    assert (await env.grade_mark("h02", "abc", tmp_path / "m2"))["status"] == "graded"
    run = [c for c in hub.commands if "/tests/test.sh > /logs/marks/h02/" in c][0]
    assert "su tester" in run
    assert any("chown -R tester /var/lib/multi-agent-harness/marks/h02 /logs/marks/h02 /tests" in c for c in hub.commands)
    # the mark's timeout falls back to the trial's timeout_multiplier, as Harbor's does
    env.trial_paths.config_path.write_text(json.dumps({"timeout_multiplier": 2.0, "verifier_timeout_multiplier": None}))
    assert env.mark_verifier()["timeout_sec"] == 1200
    env.trial_paths.config_path.write_text(json.dumps({"timeout_multiplier": 2.0, "verifier_timeout_multiplier": 0.5}))
    assert env.mark_verifier()["timeout_sec"] == 300
    # before the drain, the hub's TTL (from now) covers the pending gradings and the final verifier
    await env.hold_hub_for_marks(2)
    assert hub.raw.ttl == math.ceil(2 * (900 + 300 + 30 + 120 + 600) / 60) + env.hub_extra_minutes + 30


async def test_a_second_cancel_still_checkpoints_and_collects(tmp_path):
    env = MarkEnv(2, agent_s=5.0, grade_s=5.0)
    trial = tmp_path / "job" / "t"
    agent = TeamAgent(trial / "agent", harness="codex", timeout_sec=10, grade_at="0.05s", checkpoint_every_min=15)
    await agent.setup(env)
    ctx = AgentContext()
    task = asyncio.create_task(agent.run("Write team.txt.", env, ctx))
    await asyncio.sleep(0.3)
    task.cancel()
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [r["status"] for r in ctx.metadata["marks"] if not r.get("final")] == ["cancelled"]
    final = [json.loads((Path(p) / ck.MANIFEST).read_text()) for p in agent.checkpoints]
    assert final[-1]["final"] and final[-1]["marks"][0]["status"] == "cancelled"


async def test_a_grading_updates_every_checkpoint_of_this_run_and_never_the_source_of_a_resume(tmp_path):
    # s0.05 grades until ~0.35 s, after the checkpoint at s0.1: that checkpoint gets its grade too
    env = MarkEnv(2, agent_s=0.6, grade_s=0.3)
    trial = tmp_path / "job" / "t"
    agent = TeamAgent(trial / "agent", harness="codex", timeout_sec=600, grade_at="0.05s,0.1s", checkpoint_every_min=15)
    await agent.setup(env)
    await agent.run("Write team.txt.", env, AgentContext())
    m = json.loads((ck.root_for(trial) / "mark-s0.1" / ck.MANIFEST).read_text())
    assert {x["mark"]: x["status"] for x in m["marks"]}["s0.05"] == "graded"
    # resume from the checkpoint at s0.1 as if s0.1 had not finished: it is regraded, and its record names the
    # source checkpoint, which stays as it was
    src = ck.root_for(trial) / "mark-s0.1"
    m["marks"] = [dict(x, status="grading") if x["mark"] == "s0.1" else x for x in m["marks"]]
    (src / ck.MANIFEST).write_text(json.dumps(m))
    before = (src / ck.MANIFEST).read_bytes()
    env = MarkEnv(2, agent_s=0.3, grade_s=0.05)
    env.team_resume = ck.load(src, task="hello-team", harness="codex", n_agents=2)
    trial2 = tmp_path / "job2" / "t"
    agent = TeamAgent(trial2 / "agent", harness="codex", timeout_sec=600, grade_at="0.05s,0.1s", checkpoint_every_min=15)
    await agent.setup(env)
    await agent.run("Write team.txt.", env, AgentContext())
    assert [g[0] for g in env.gradings] == ["s0.1"]
    assert (src / ck.MANIFEST).read_bytes() == before
    last = json.loads((Path(agent.checkpoints[-1]) / ck.MANIFEST).read_text())
    assert {x["mark"]: x["status"] for x in last["marks"]} == {"s0.05": "graded", "s0.1": "graded"}
