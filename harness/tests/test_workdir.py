"""Workdir resolution, team repo seeding, the agents' clone and the pre-verify checkout, run for
real with the local git and sh."""

import json
import shutil
import subprocess

from multi_agent_harness import workdir as wd
from multi_agent_harness.harbor import agent as ta


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def test_resolution_order(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM a AS b\nWORKDIR /build\nFROM python\nWORKDIR /app\n"
                                         "WORKDIR src\nCOPY . .\n")
    assert wd.resolve_static("/srv/x", tmp_path) == ("/srv/x", "task.toml")
    assert wd.resolve_static(None, tmp_path) == ("/app/src", "dockerfile")
    # final stage sets none (inherits): ask the sandbox
    (tmp_path / "Dockerfile").write_text("FROM a\nWORKDIR /build\nFROM b\n")
    assert wd.resolve_static(None, tmp_path) is None
    assert wd.dockerfile_workdir("FROM a\nWORKDIR $HOME/x\n") is None
    assert wd.accept_sandbox("/testbed\n", "/root") == ("/testbed", "sandbox")
    for pwd in ("/", "/root", "sb-0", ""):  # never the filesystem root or $HOME
        assert wd.accept_sandbox(pwd, "/root") == ("/workspace", "default")


def make_workdir(tmp_path):
    w = tmp_path / "app"
    for d in ("bin", "harness", "node_modules/x"):
        (w / d).mkdir(parents=True)
    (w / "MATCH.md").write_text("rules\n")
    (w / "harness/match.py").write_text("print(1)\n")
    (w / "node_modules/x/a.js").write_text("nm\n")
    (w / ".gitignore").write_text("node_modules/\n")
    git("init", "-q", str(w / "vendor"))  # nested repo, no commits
    (w / "vendor/f").write_text("v\n")
    bare = tmp_path / "team.git"
    git("init", "-q", "--bare", str(bare))
    git("--git-dir", str(bare), "symbolic-ref", "HEAD", "refs/heads/main")
    return w, str(bare)


def test_seed_commits_the_starting_files(tmp_path):
    w, bare = make_workdir(tmp_path)
    meta = wd.seed(bare, str(w), str(tmp_path / "state"))
    assert git("--git-dir", bare, "ls-tree", "-r", "--name-only", "main").split() == \
        [".gitignore", "MATCH.md", "harness/match.py"]
    assert meta["nested_repos_skipped"] == ["vendor"]
    assert wd.seed(bare, str(w), str(tmp_path / "state"))["seed"] == meta["seed"]  # idempotent


def test_agent_clone_in_place_and_checkout(tmp_path, monkeypatch):
    w, bare = make_workdir(tmp_path)
    state = tmp_path / "state"
    wd.seed(bare, str(w), str(state))
    # An agent's workdir: the same image files, plus a .git the image left there.
    a = tmp_path / "agent"
    shutil.copytree(w, a)
    git("init", "-q", str(a))
    monkeypatch.setattr(ta, "IMAGE_GIT_ASIDE", str(tmp_path / "image-git"))
    subprocess.run(["sh", "-ec", ta.clone_script(str(a), f"file://{bare}")], check=True, capture_output=True)
    assert git("status", "--porcelain", cwd=a) == "?? vendor/\n"  # nested repo stays untracked
    assert list(tmp_path.glob("image-git.*"))
    # The team deletes MATCH.md and adds the engine; a stale file is left in the hub's workdir.
    git("rm", "-q", "MATCH.md", cwd=a)
    (a / "build.sh").write_text("make\n")
    git("add", "build.sh", cwd=a)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "engine", cwd=a)
    git("push", "-q", "origin", "main", cwd=a)
    (w / "STALE").write_text("x")

    r = wd.checkout(bare, str(w), str(state))
    have = sorted(str(p.relative_to(w)) for p in w.rglob("*"))
    assert have == [".gitignore", "build.sh", "harness", "harness/match.py"]  # exactly main
    assert (tmp_path / r["aside"] / "STALE").exists() and (tmp_path / r["aside"] / "MATCH.md").exists()
