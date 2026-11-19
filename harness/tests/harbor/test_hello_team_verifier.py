import importlib.util
import json
import subprocess
from pathlib import Path

VERIFY = Path(__file__).resolve().parents[2] / "examples/tasks/hello-team/tests/verify.py"
spec = importlib.util.spec_from_file_location("hello_team_verify", VERIFY)
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


def make_repo(tmp_path, files):
    """A bare repo whose main has `files`, pushed from a scratch clone."""
    bare, work = tmp_path / "team.git", tmp_path / "work"
    git = lambda *a, cwd=None: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)
    git("init", "-q", "--bare", "--initial-branch=main", str(bare))
    git("init", "-q", "-b", "main", str(work))
    for name, text in files.items():
        (work / name).write_text(text)
    git("add", ".", cwd=work)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x", cwd=work)
    git("push", "-q", str(bare), "main", cwd=work)
    return str(bare)


def make_log(tmp_path, agents, posters):
    events = [{"ts": 0, "event": "register", "agent": a} for a in agents]
    events += [{"ts": 1, "event": "send", "agent": a, "to": None, "text": "hi"} for a in posters]
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return str(path)


AGENTS = ["agent-1", "agent-2", "agent-3"]


def test_full_marks(tmp_path):
    repo = make_repo(tmp_path, {"team.txt": "agent-1: 4\nagent-2: 7\nagent-3: 9\n", "LEADER": "agent-2\n"})
    r = verify.score(repo, make_log(tmp_path, AGENTS, AGENTS))
    assert r["reward"] == 1.0


def test_partial_credit(tmp_path):
    # agent-3 missing, duplicate number, a stray line, leader unknown, agent-3 never sent
    repo = make_repo(tmp_path, {"team.txt": "agent-1: 4\nagent-2: 4\nhello\n", "LEADER": "bob\n"})
    r = verify.score(repo, make_log(tmp_path, AGENTS, ["agent-1", "agent-2"]))
    assert r["checks"] == {"lines": 2 / 4, "unique": 0.0, "leader": 0.0, "sent": 2 / 3}
    assert abs(r["reward"] - (0.5 + 2 / 3) / 4) < 1e-9


def test_empty_repo_and_main_writes_reward(tmp_path, monkeypatch):
    bare = tmp_path / "empty.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    monkeypatch.setenv("MULTI_AGENT_HARNESS_TEAM_GIT", str(bare))
    monkeypatch.setenv("BOARD_LOG", make_log(tmp_path, AGENTS, []))
    monkeypatch.setenv("MULTI_AGENT_HARNESS_VERIFIER_DIR", str(tmp_path / "v"))
    verify.main()
    assert (tmp_path / "v/reward.txt").read_text() == "0.0000\n"


def test_separate_test_sh_scores_the_exported_bundle(tmp_path):
    """hello-team-separate's test.sh, run on what export_artifacts leaves in /logs/artifacts/multi-agent-harness."""
    repo = make_repo(tmp_path, {"team.txt": "agent-1: 4\nagent-2: 7\nagent-3: 9\n", "LEADER": "agent-2\n"})
    art, out = tmp_path / "artifacts", tmp_path / "verifier"
    art.mkdir()
    subprocess.run(["git", "--git-dir", repo, "bundle", "create", str(art / "team.bundle"), "--all"],
                   check=True, capture_output=True)
    Path(make_log(tmp_path, AGENTS, AGENTS)).rename(art / "board-events.jsonl")
    script = VERIFY.parents[2] / "hello-team-separate/tests/test.sh"
    env = {"PATH": "/usr/bin:/bin", "MULTI_AGENT_HARNESS_ARTIFACTS": str(art), "MULTI_AGENT_HARNESS_VERIFIER_DIR": str(out)}
    subprocess.run(["bash", str(script)], env=env, check=True, capture_output=True)
    assert (out / "reward.txt").read_text().strip() == "1.0000"
