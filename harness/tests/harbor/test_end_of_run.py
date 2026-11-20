"""Harbor's agent timeout landing in the harness's end-of-run steps: main must still be checked out into the
workdir before the verifier, and a verifier that ran without it is flagged.

The run seen in the wild (legion, 4 Codex agents): the agents ended at their own timeout, the final checkpoint
took the last seconds of Harbor's 14,400 s, and Harbor's cancel landed in the download of the team repo bundle
in _collect. export_artifacts, which came after it, never ran: the verifier graded the starter workdir."""

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from harbor.models.agent.context import AgentContext

from multi_agent_harness import workdir as wd
from multi_agent_harness.harbor.agent import TeamAgent
from test_agent import fake_installer  # noqa: F401 - autouse fixture
from test_checkpoint import CkptEnv
from test_environment import client, started  # noqa: F401 - fixtures

pytestmark = pytest.mark.asyncio

STARTER = "// starter\n"
MAIN = "// the team's main\nexport function ai() {}\n"
SLOW = 0.6  # how long the slow end-of-run step takes


def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def team_repo(tmp: Path) -> tuple[Path, Path, Path]:
    """A hub in miniature: a team repo whose main is two commits past the starter, and a workdir still holding
    the starter files (as the agents leave it: they work in their own clones)."""
    git_dir, work, state, scratch = tmp / "team.git", tmp / "app", tmp / "state", tmp / "clone"
    work.mkdir(parents=True)
    (work / "main.js").write_text(STARTER)
    git("init", "-q", "--bare", "-b", "main", str(git_dir))
    wd.seed(str(git_dir), str(work), str(state))
    git("clone", "-q", str(git_dir), str(scratch))
    for name, text in (("main.js", MAIN), ("AI.md", "# Strategy\n"), ("modules/eco.js", "// eco\n")):
        (scratch / name).parent.mkdir(parents=True, exist_ok=True)
        (scratch / name).write_text(text)
    git("-c", "user.name=a", "-c", "user.email=a@x", "commit", "-qam", "work", cwd=scratch)
    git("add", "-A", cwd=scratch)
    git("-c", "user.name=a", "-c", "user.email=a@x", "commit", "-qm", "more", cwd=scratch)
    git("push", "-q", "origin", "main", cwd=scratch)
    return git_dir, work, state


class HubEnv(CkptEnv):
    """CkptEnv with the hub's end-of-run steps: export_artifacts runs the real workdir checkout on a local team
    repo, and one of the steps (`slow`) takes SLOW s and sets `entered[slow]` when it starts, so a test can land
    Harbor's cancel in it."""

    def __init__(self, tmp: Path, slow: str, n=2):
        super().__init__(n)
        self.git_dir, self.work, self.state = team_repo(tmp)
        self.slow = slow
        self.entered = {k: asyncio.Event() for k in ("export", "checkpoint", "save_team_work")}
        self.order: list[str] = []
        self.team_verify_commit = None
        self.team_final_mark_t0 = None

    async def _step(self, name: str) -> None:
        self.order.append(name)
        self.entered[name].set()
        if name == self.slow:
            await asyncio.sleep(SLOW)

    async def export_artifacts(self):
        await self._step("export")
        checkout = wd.checkout(str(self.git_dir), str(self.work), str(self.state))
        self._ledger(verify_checkout=checkout)
        self.team_verify_commit = checkout["commit"]
        self.order.append("export done")

    async def checkpoint_hub(self, dest):
        await self._step("checkpoint")
        return await super().checkpoint_hub(dest)

    async def save_team_work(self):
        await self._step("save_team_work")
        self.order.append("save_team_work done")


def main_commit(env: HubEnv) -> str:
    return subprocess.run(["git", "--git-dir", str(env.git_dir), "rev-parse", "main"], capture_output=True,
                          text=True, check=True).stdout.strip()


def assert_workdir_holds_main(env: HubEnv) -> None:
    assert (env.work / "main.js").read_text() == MAIN
    assert (env.work / "AI.md").is_file() and (env.work / "modules" / "eco.js").is_file()
    checkouts = [e["verify_checkout"] for e in env.ledger if "verify_checkout" in e]
    assert [c["commit"] for c in checkouts] == [main_commit(env)]


@pytest.mark.parametrize("where", ["export", "checkpoint", "save_team_work"])
async def test_harbors_cancel_in_an_end_of_run_step_still_leaves_main_in_the_workdir(tmp_path, where):
    env = HubEnv(tmp_path / "hub", slow=where)
    agent = TeamAgent(tmp_path / "job" / "t" / "agent", harness="codex", timeout_sec=600, checkpoint_every_min=15)
    await agent.setup(env)
    ctx = AgentContext()
    run = asyncio.create_task(agent.run("Write team.txt.", env, ctx))
    await env.entered[where].wait()
    run.cancel()  # what Harbor's asyncio.wait_for does at its agent timeout
    with pytest.raises(asyncio.CancelledError):
        await run
    assert_workdir_holds_main(env)
    # main went into the workdir right after the agents stopped, before the final checkpoint and collection
    assert env.order.index("export done") < env.order.index("checkpoint") < env.order.index("save_team_work")
    assert "save_team_work done" in env.order and ctx.metadata["end_reason"]  # collection ran to the end too


async def test_harbors_agent_timeout_during_the_bundle_download_as_seen(tmp_path):
    # Harbor's own mechanism: asyncio.wait_for(agent.run(...), timeout), which on Python 3.12 is asyncio.timeout:
    # at the deadline it cancels the run, waits for it to return, and raises TimeoutError (AgentTimeoutError).
    # The deadline falls in save_team_work, as in the run that scored 0.
    env = HubEnv(tmp_path / "hub", slow="save_team_work")
    agent = TeamAgent(tmp_path / "job" / "t" / "agent", harness="codex", timeout_sec=600, checkpoint_every_min=0)
    await agent.setup(env)
    loop = asyncio.get_running_loop()
    deadline: list = []

    async def harbor():
        async with asyncio.timeout(None) as cm:
            deadline.append(cm)
            await agent.run("Write team.txt.", env, AgentContext())

    task = asyncio.create_task(harbor())
    await env.entered["save_team_work"].wait()
    t0 = loop.time()
    deadline[0].reschedule(t0)
    with pytest.raises(TimeoutError):
        await task
    assert loop.time() - t0 >= SLOW * 0.9  # Harbor waited for the end-of-run steps
    assert "save_team_work done" in env.order
    assert_workdir_holds_main(env)


async def test_a_second_cancel_still_stops_the_export(tmp_path):
    env = HubEnv(tmp_path / "hub", slow="export")
    agent = TeamAgent(tmp_path / "job" / "t" / "agent", harness="codex", timeout_sec=600, checkpoint_every_min=0)
    await agent.setup(env)
    run = asyncio.create_task(agent.run("Write team.txt.", env, AgentContext()))
    await env.entered["export"].wait()
    run.cancel()
    await asyncio.sleep(0.05)
    run.cancel()  # an impatient Ctrl-C
    with pytest.raises(asyncio.CancelledError):
        await run
    assert "export done" not in env.order and (env.work / "main.js").read_text() == STARTER


# ---- the environment's backstop and flag (TeamEnvironment against the fake sandbox SDK)

def ledger(env) -> list[dict]:
    return [json.loads(line) for line in env.ledger_path.read_text().splitlines()]


async def test_the_verifier_uploading_its_tests_checks_main_out_if_nothing_did(started, tmp_path):
    env, client = started
    hub = client.created[0]
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test.sh").write_text("true\n")
    before = len(hub.commands)
    await env.upload_dir(tmp_path / "tests", "/tests")
    assert "multi_agent_harness.workdir checkout --git-dir /srv/git/team.git" in "\n".join(hub.commands[before:])
    assert any(e.get("event") == "verify_checkout_late" for e in ledger(env))
    assert env.team_verify_checkout is not None and env.team_verify_checkout_late
    # once main is in the workdir, neither the export nor another upload checks it out again
    before = len(hub.commands)
    await env.export_artifacts()
    await env.upload_dir(tmp_path / "tests", "/tests")
    assert "workdir checkout" not in "\n".join(hub.commands[before:])


async def test_no_backstop_when_the_export_ran(started, tmp_path):
    env, client = started
    hub = client.created[0]
    await env.export_artifacts()
    (tmp_path / "tests").mkdir()
    before = len(hub.commands)
    await env.upload_dir(tmp_path / "tests", "/tests")
    assert "workdir checkout" not in "\n".join(hub.commands[before:])
    assert not any(e.get("event") in ("verify_checkout_late", "verifier_without_checkout") for e in ledger(env))


async def test_a_verifier_run_without_main_checked_out_fails_the_final_mark(started, tmp_path, caplog):
    env, client = started

    async def broken():
        raise RuntimeError("hub unreachable")

    env.checkout_main = broken
    (tmp_path / "tests").mkdir()
    await env.upload_dir(tmp_path / "tests", "/tests")  # the backstop fails too; the upload still happens
    assert any(e.get("event") == "verify_checkout_failed" for e in ledger(env))
    vdir = Path(env.trial_paths.verifier_dir)
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / "reward.txt").write_text("0.0\n")
    rec = {"mark": "end", "status": "awaiting_final_verifier", "final": True, "commit": None,
           "dir": str(tmp_path / "marks" / "end")}
    env.team_marks = [rec]
    env.finish_final_mark()
    assert rec["status"] == "failed" and rec["reward"] is None and rec["verifier_reward"] == 0.0
    assert "no verify_checkout" in rec["error"]
    events = [e.get("event") for e in ledger(env)]
    assert "verifier_without_checkout" in events and "mark_graded" in events
    assert "ran without main checked out" in caplog.text
