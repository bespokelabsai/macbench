"""Checkpoints and resume, against fakes."""

import io
import json
import stat
import tarfile
from pathlib import Path

import pytest
from harbor.environments.base import ExecResult
from harbor.models.agent.context import AgentContext

from multi_agent_harness.harbor import checkpoint as ck
from multi_agent_harness.harbor.agent import TeamAgent
from test_agent import FakeEnv, fake_installer  # noqa: F401 - autouse fixture
from test_environment import client, make_env  # noqa: F401 - fixture

pytestmark = pytest.mark.asyncio


def _tgz(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class CkptEnv(FakeEnv):
    """FakeEnv plus the hub's checkpoint half and an agent sandbox that hands back session files."""

    environment_name = "hello-team"
    team_task_agent_timeout_sec = 1200.0

    def __init__(self, n=3, fail_resume=False):
        super().__init__(n)
        self.fail_resume = fail_resume
        self.ledger, self.blobs = [], {}

    def _ledger(self, **entry):
        def append_ledger(path, logger, **e):  # TeamEnvironment's signature: no `path` key in entries
            self.ledger.append(e)
        append_ledger(None, None, **entry)

    async def checkpoint_hub(self, dest):
        (dest / "team.bundle").write_bytes(b"BUNDLE")
        (dest / "board-events.jsonl").write_text('{"event": "send", "agent": "agent-1"}\n')
        (dest / "proxy-usage.jsonl").write_text('{"agent": "agent-1", "usd": 0.25}\n{"agent": "agent-2", "usd": 0.5}\n')
        return 6

    async def service_exec(self, command, *, service=None, cwd=None, env=None, timeout_sec=None, user=None):
        if "codex exec resume" in command and self.fail_resume:
            self.calls.append({"service": service, "command": command, "env": env or {}})
            self.events.append(("resume-failed", service))
            return ExecResult(stdout="", stderr="no session", return_code=1)
        if "du -sk" in command:
            self.calls.append({"service": service, "command": command, "env": env or {}})
            return ExecResult(stdout="12\n", return_code=0)
        return await super().service_exec(command, service=service, cwd=cwd, env=env, timeout_sec=timeout_sec,
                                          user=user)

    async def service_upload_file(self, source_path, target_path, *, service=None):
        self.blobs[(service, target_path)] = Path(source_path).read_bytes()
        self.events.append(("upload", service, target_path))
        self.trace.append(("upload", service, target_path))

    async def service_download_file(self, source_path, target_path, *, service=None):
        Path(target_path).write_bytes(_tgz({
            "session/sessions/2026/rollout-1.jsonl": f'{{"key": "PKEY-{service}"}}\n'.encode(),
            "session/auth.json": b'{"OPENAI_API_KEY": "x"}',
            "session-id": f"thread-{service}\n".encode()}))


def write_checkpoint(path: Path, n_agents=3, task="hello-team", harness="codex", elapsed=300.0,
                     timeout=1200.0) -> Path:
    path.mkdir(parents=True)
    (path / "team.bundle").write_bytes(b"BUNDLE")
    (path / "proxy-usage.jsonl").write_text('{"agent": "agent-1", "usd": 0.4}\n')
    (path / "board-events.jsonl").write_text('{"event": "send", "agent": "agent-1", "text": "hi"}\n')
    s = path / "agents" / "agent-1"
    (s / "session" / "sessions").mkdir(parents=True)
    (s / "session" / "sessions" / "rollout.jsonl").write_text("{}\n")
    (s / "session-id").write_text("thread-1\n")
    (path / ck.MANIFEST).write_text(json.dumps({
        "format": 1, "n": 2, "task": task, "harness": harness, "n_agents": n_agents,  # legacy CPU checkpoint
        "agent_timeout_sec": timeout, "elapsed_agent_sec": elapsed, "spend_usd": 0.4, "final": False}))
    return path


async def test_checkpoint_layout_and_manifest_without_token_files(tmp_path):
    trial = tmp_path / "job" / "hello-team__abc"
    env = CkptEnv(3)
    agent = TeamAgent(trial / "agent", harness="codex", checkpoint_every_min=15)
    await agent.setup(env)
    await agent.run("Write team.txt.", env, AgentContext())

    root = tmp_path / "job.checkpoints" / "hello-team__abc"
    [path] = [p for _, p in ck.listing(root)]
    assert path.name.startswith("1-") and not (tmp_path / "job" / "hello-team__abc" / "agent" / "1").exists()
    for f in ("team.bundle", "board-events.jsonl", "proxy-usage.jsonl", "manifest.json",
              "agents/agent-2/session-id", "agents/agent-2/session/sessions/2026/rollout-1.jsonl"):
        assert (path / f).is_file(), f
    assert not list(path.rglob("auth.json"))
    assert "PKEY-agent-2" not in (path / "agents/agent-2/session/sessions/2026/rollout-1.jsonl").read_text()
    assert all(stat.S_IMODE(p.stat().st_mode) == (0o700 if p.is_dir() else 0o600) for p in [path, *path.rglob("*")])

    m = json.loads((path / "manifest.json").read_text())
    assert m["format"] == ck.FORMAT and m["n"] == 1 and m["final"] is True and m["reason"] == "all_finished"
    assert (m["task"], m["harness"], m["n_agents"], m["orchestration"]) == ("hello-team", "codex", 3, "fixed")
    assert m["agent_timeout_sec"] == 1200.0 and m["spend_usd"] == 0.75 and m["elapsed_agent_sec"] >= 0
    assert m["agents"]["agent-1"] == {"status": "finished", "session": "saved", "harness": "codex",
                                      "model": "gpt-6-luna"}
    scrub = [c for c in env.calls if "grep -rlF" in c["command"] and "/tmp/multi-agent-harness-checkpoint" in c["command"]]
    assert len(scrub) == 3 and all('"$OPENAI_API_KEY"' in c["command"] for c in scrub)
    assert any(e.get("event") == "checkpoint" for e in env.ledger)


async def test_retention_keeps_last_three_and_the_final(tmp_path):
    root = tmp_path / "job.checkpoints" / "t"
    for n in range(1, 7):
        path = ck.commit(ck.begin(root, n), root, n, {"n": n, "final": n == 2})
        path.rename(root / f"{n}-2026010{n}T000000Z")  # commit() names by the clock, which can repeat here
    ck.begin(root, 7)  # a leftover temp dir from an interrupted checkpoint
    pruned = ck.prune(root)
    assert [n for n, _ in ck.listing(root)] == [2, 4, 5, 6]
    assert len(pruned) == 3 and not list(root.glob(".tmp-*"))
    assert ck.next_n(root) == 7


async def test_resume_seeds_from_bundle_preloads_usage_and_shortens_timeout(tmp_path, client):  # noqa: F811
    ckdir = write_checkpoint(tmp_path / "job.checkpoints" / "t" / "2-20260101T000000Z")
    env = make_env(tmp_path, resume_from=str(ckdir))
    await env.start(force_build=False)
    hub = client.created[0]
    cmds = "\n".join(hub.commands)
    assert "fetch -q /tmp/multi-agent-harness-resume.bundle" in cmds and "+refs/heads/*:refs/heads/*" in cmds
    assert cmds.index("fetch -q /tmp/multi-agent-harness-resume.bundle") < cmds.index("multi_agent_harness.workdir seed")
    assert hub.uploads["/tmp/multi-agent-harness-resume.bundle"] == b"BUNDLE"
    assert hub.uploads[env.team_proxy_log_path] == (ckdir / "proxy-usage.jsonl").read_bytes()
    board = hub.uploads[env.team_board_log_path].decode().splitlines()
    assert json.loads(board[0])["text"] == "hi" and json.loads(board[-1])["event"] == "resumed"
    ledger = [json.loads(l) for l in env.ledger_path.read_text().splitlines()]
    assert ledger[0]["resumed_from"] == {"path": str(ckdir), "n": 2}

    fake = CkptEnv(3)
    fake.team_resume = env.team_resume
    agent = TeamAgent(tmp_path / "job2" / "t2" / "agent", harness="codex", timeout_sec=3600)
    await agent.setup(fake)
    assert agent.timeout_sec == 900.0 and agent._original_timeout == 1200.0


async def test_manifest_mismatch_is_refused(tmp_path, client):  # noqa: F811
    ckdir = write_checkpoint(tmp_path / "ck", n_agents=2)
    with pytest.raises(ValueError, match="n_agents is 2 in the checkpoint but 3 in this run"):
        ck.load(ckdir, task="hello-team", harness="codex", n_agents=3)
    with pytest.raises(ValueError, match="harness is 'codex' in the checkpoint but 'claude-code'"):
        ck.load(ckdir, task="hello-team", harness="claude-code", n_agents=2)
    env = make_env(tmp_path, resume_from=str(ckdir))
    with pytest.raises(ValueError, match="does not match this run"):
        await env.start(force_build=False)
    assert client.created == []  # refused before anything was created
    late = write_checkpoint(tmp_path / "late", elapsed=1150)
    with pytest.raises(ValueError, match="only 50s"):
        ck.load(late, task="hello-team", harness="codex", n_agents=3)


async def test_failed_session_resume_falls_back_to_a_fresh_session(tmp_path):
    ckdir = write_checkpoint(tmp_path / "ck")
    env = CkptEnv(3, fail_resume=True)
    env.team_resume = ck.load(ckdir, task="hello-team", harness="codex", n_agents=3)
    agent = TeamAgent(tmp_path / "job" / "t" / "agent", harness="codex", checkpoint_every_min=0)
    await agent.setup(env)
    ctx = AgentContext()
    await agent.run("Write team.txt.", env, ctx)

    assert ("agent-1", "/var/tmp/multi-agent-harness-resume/board-events.jsonl") in env.blobs
    assert env.blobs[("agent-1", "/logs/agent/session-id")] == b"thread-1\n"
    launches = [c for c in env.calls if c["service"] == "agent-1" and "MULTI_AGENT_HARNESS_PROMPT" in c["env"]]
    assert "codex exec resume" in launches[0]["command"] and "codex exec resume" not in launches[1]["command"]
    assert "rm -f /logs/agent/last-message.txt /logs/agent/session-id;" in launches[1]["command"]
    fresh = launches[1]["env"]["MULTI_AGENT_HARNESS_PROMPT"]
    assert "resumed from a checkpoint taken 5m00s into it" in fresh and "15m00s of agent time remains" in fresh
    assert fresh.index("resumed from a checkpoint") < fresh.index("Task:\n") < fresh.index("Write team.txt.")
    assert agent.resume_outcome["agent-1"].startswith("fresh: resume exited 1")
    assert agent.resume_outcome["agent-2"] == "fresh: no saved session"
    assert ctx.metadata["resumed_from"]["n"] == 2 and ctx.metadata["restored_usd"] == 0.4


async def test_checkpoint_without_a_proxy_log(tmp_path, client):  # noqa: F811
    env = make_env(tmp_path)  # under claude-subscription the proxy never writes its log
    await env.start(force_build=False)
    hub = client.created[0]
    real = hub.download_file

    async def download(remote, local):
        if remote == env.team_proxy_log_path:
            raise FileNotFoundError(remote)
        await real(remote, local)
    hub.download_file = download
    dest = tmp_path / "ck"
    dest.mkdir()
    await env.checkpoint_hub(dest)
    assert (dest / "team.bundle").is_file() and (dest / "board-events.jsonl").is_file()


async def test_a_failed_restore_starts_that_agent_fresh(tmp_path):
    env = CkptEnv(3)
    env.team_resume = ck.load(write_checkpoint(tmp_path / "ck"), task="hello-team", harness="codex", n_agents=3)
    upload = env.service_upload_file

    async def failing(src, target, *, service=None):
        if service == "agent-1" and target.endswith("-restore.tar"):
            raise RuntimeError("upload failed")
        await upload(src, target, service=service)
    env.service_upload_file = failing
    agent = TeamAgent(tmp_path / "job" / "t" / "agent", harness="codex", checkpoint_every_min=0)
    await agent.setup(env)
    await agent.run("Write team.txt.", env, AgentContext())
    assert agent.resume_outcome["agent-1"] == "fresh: no saved session"
    assert ("agent-1", "/logs/agent/session-id") not in env.blobs


async def test_resume_hands_restored_files_to_a_non_root_agent_user(tmp_path):
    """Restore runs before any agent code in the new sandbox, so root gives the restored session id and
    board log to the task's agent user, and the resumed CLI can rewrite its session id."""
    env = CkptEnv(3)
    env.default_user = "agent"
    env.team_resume = ck.load(write_checkpoint(tmp_path / "ck"), task="hello-team", harness="codex", n_agents=3)
    agent = TeamAgent(tmp_path / "job" / "t" / "agent", harness="codex", checkpoint_every_min=0)
    await agent.setup(env)
    await agent.run("Write team.txt.", env, AgentContext())
    trace = [t for t in env.trace if t[1] == "agent-1"]
    sid = trace.index(("upload", "agent-1", "/logs/agent/session-id"))
    handover = next(t for t in trace[sid:] if t[0] == "exec" and t[2] == "root")
    assert "chown -R agent /run/multi-agent-harness /logs/agent" in handover[3]
    assert "chmod 644 /var/tmp/multi-agent-harness-resume/board-events.jsonl" in handover[3]
    restore = next(i for i, t in enumerate(trace) if t[0] == "exec" and "tar -xf" in t[3])
    assert trace[restore][2] is None and "rm -f" not in trace[restore][3]  # the agent user unpacks its session
    assert trace[restore + 1][2:] == ("root", "rm -f /tmp/multi-agent-harness-checkpoint-restore.tar")


async def test_a_failed_final_checkpoint_is_retried_before_delete(tmp_path):
    env = CkptEnv(3)
    real, fails = env.checkpoint_hub, [RuntimeError("hub busy")]

    async def flaky(dest):
        if fails:
            raise fails.pop()
        return await real(dest)
    env.checkpoint_hub = flaky
    agent = TeamAgent(tmp_path / "job" / "t" / "agent", harness="codex", checkpoint_every_min=15)
    await agent.setup(env)
    await agent.run("Write team.txt.", env, AgentContext())
    assert agent.checkpoints == []
    await env.team_checkpoint_hook()
    assert len(agent.checkpoints) == 1
