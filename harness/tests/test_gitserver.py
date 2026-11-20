"""The team git remote: push rules, token check, and a real clone/push over smart HTTP on
localhost."""

import base64
import hashlib
import json
import shutil
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from multi_agent_harness import gitserver
from multi_agent_harness.gitserver import (Handler, check_paths, check_token, check_update, load_path_rules,
                                          run_hook)

Z, A, B = "0" * 40, "a" * 40, "b" * 40


def test_push_rules():
    ff = lambda old, new: True  # noqa: E731
    nonff = lambda old, new: False  # noqa: E731
    assert check_update("agent-1", A, B, "refs/heads/main", ff) is None
    assert "fast-forward" in check_update("agent-1", A, B, "refs/heads/main", nonff)
    assert check_update("agent-1", Z, B, "refs/heads/agent-1/wip", ff) is None
    assert check_update("agent-1", A, B, "refs/heads/agent-1/wip", nonff) is None  # denyNonFastForwards covers it
    assert "not refs/heads/agent-2/x" in check_update("agent-1", Z, B, "refs/heads/agent-2/x", ff)
    assert "not refs/heads/agent-10/x" in check_update("agent-1", Z, B, "refs/heads/agent-10/x", ff)
    assert "not refs/tags/v1" in check_update("agent-1", Z, B, "refs/tags/v1", ff)
    assert "deleting" in check_update("agent-1", A, Z, "refs/heads/agent-1/wip", ff)
    assert check_update("", A, B, "refs/heads/main", ff) == "unauthenticated push"
    assert run_hook([f"{A} {B} refs/heads/main\n", f"{Z} {B} refs/heads/agent-1/x\n"], "agent-1", ff) == 0
    assert run_hook([f"{A} {B} refs/heads/main\n", f"{Z} {B} refs/heads/agent-2/x\n"], "agent-1", ff) == 1


def test_token_check(tmp_path):
    tokens = tmp_path / "t.json"
    tokens.write_text(json.dumps({"agent-1": hashlib.sha256(b"s3cret").hexdigest()}))
    basic = lambda u, p: "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()  # noqa: E731
    assert check_token(str(tokens), basic("agent-1", "s3cret")) == "agent-1"
    assert check_token(str(tokens), basic("agent-2", "s3cret")) is None
    assert check_token(str(tokens), basic("agent-1", "wrong")) is None
    assert check_token(str(tokens), None) is None and check_token(str(tokens), "Basic !!") is None


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_clone_and_push_over_http(tmp_path):
    root = tmp_path / "srv"
    repo = root / "team.git"
    git = lambda *a, cwd=None, check=True: subprocess.run(  # noqa: E731
        ["git", *a], cwd=cwd, capture_output=True, text=True, check=check)
    git("init", "-q", "--bare", str(repo))
    git("--git-dir", str(repo), "symbolic-ref", "HEAD", "refs/heads/main")
    for k in ("receive.denyNonFastForwards", "receive.denyDeletes", "http.receivepack"):
        git("--git-dir", str(repo), "config", k, "true")
    hook = repo / "hooks" / "pre-receive"
    hook.write_text(f"#!/bin/sh\nexec {sys.executable} -m multi_agent_harness.gitserver hook\n")
    hook.chmod(0o755)
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps({n: hashlib.sha256(f"tok-{n}".encode()).hexdigest()
                                  for n in ("agent-1", "agent-2")}))

    class H(Handler):
        pass
    H.root, H.tokens = str(root), str(tokens)
    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = lambda n, tok=None: f"http://{n}:{tok or 'tok-' + n}@127.0.0.1:{server.server_port}/team.git"  # noqa: E731
    ident = ["-c", "user.name=t", "-c", "user.email=t@t"]
    try:
        assert git("clone", "-q", url("agent-1", "bad"), str(tmp_path / "bad"), check=False).returncode != 0
        w1 = tmp_path / "w1"
        git("init", "-q", "-b", "main", str(w1))
        (w1 / "a.txt").write_text("1\n")
        git("add", "a.txt", cwd=w1)
        git(*ident, "commit", "-q", "-m", "one", cwd=w1)
        git("push", "-q", url("agent-1"), "main", cwd=w1)
        git("push", "-q", url("agent-1"), "main:refs/heads/agent-1/wip", cwd=w1)
        refused = git("push", url("agent-1"), "main:refs/heads/agent-2/x", cwd=w1, check=False)
        assert refused.returncode != 0 and "agent-1 may push only" in refused.stderr

        w2 = tmp_path / "w2"
        git("clone", "-q", url("agent-2"), str(w2))
        assert (w2 / "a.txt").read_text() == "1\n"
        git(*ident, "commit", "-q", "--amend", "-m", "rewritten", cwd=w2)
        forced = git("push", "-f", url("agent-2"), "main", cwd=w2, check=False)
        assert forced.returncode != 0
        deleted = git("push", url("agent-1"), ":refs/heads/agent-1/wip", cwd=w1, check=False)
        assert deleted.returncode != 0
        (w1 / "b.txt").write_text("2\n")
        git("add", "b.txt", cwd=w1)
        git(*ident, "commit", "-q", "-m", "two", cwd=w1)
        git("push", "-q", url("agent-1"), "main", cwd=w1)  # fast-forward is fine
        log = git("--git-dir", str(repo), "log", "--format=%s", "main").stdout.split()
        assert log == ["two", "one"]
    finally:
        server.shutdown()


def test_path_rules(tmp_path):
    rules = [{"prefix": "design/", "agents": ["agent-1", "agent-2"]},
             {"prefix": "design/launcher/", "agents": ["agent-3"], "note": "the launcher's owner"},
             {"prefix": "design/capsule/", "agents": []}]
    assert check_paths("agent-3", ["design/launcher/S1.json", "flight/guidance.py"], rules) is None
    why = check_paths("agent-1", ["README.md", "design/launcher/S1.json"], rules)
    assert why == ("agent-1 may not change design/launcher/S1.json on main: design/launcher/ is writable by "
                   "agent-3 (the launcher's owner)"), why
    assert check_paths("agent-2", ["design/mission.json"], rules) is None  # the shorter prefix
    assert "writable by nobody" in check_paths("agent-1", ["design/capsule/CAP.json"], rules)
    assert check_paths("agent-4", ["flight/x.py"], rules) is None  # no rule: free

    assert load_path_rules(tmp_path) is None
    (tmp_path / "path-rules.json").write_text(json.dumps({"rules": rules}))
    assert load_path_rules(tmp_path) == rules
    for bad in ("{", json.dumps({"rules": [{"prefix": "", "agents": []}]}),
                json.dumps({"rules": [{"prefix": "a/", "agents": "agent-1"}]})):
        (tmp_path / "path-rules.json").write_text(bad)
        with pytest.raises(ValueError):
            load_path_rules(tmp_path)

    ff = lambda old, new: True  # noqa: E731
    changed = lambda old, new: ["design/launcher/S1.json"]  # noqa: E731
    line = [f"{A} {B} refs/heads/main\n"]
    (tmp_path / "path-rules.json").write_text(json.dumps({"rules": rules}))
    assert run_hook(line, "agent-3", ff, git_dir=str(tmp_path), changed=changed) == 0
    assert run_hook(line, "agent-1", ff, git_dir=str(tmp_path), changed=changed) == 1
    assert run_hook(line, "agent-1", ff, changed=changed) == 0  # no git_dir: ref rules only
    branch = [f"{Z} {B} refs/heads/agent-1/wip\n"]
    assert run_hook(branch, "agent-1", ff, git_dir=str(tmp_path), changed=changed) == 0  # branches are free
    (tmp_path / "path-rules.json").write_text("{")
    assert run_hook(line, "agent-3", ff, git_dir=str(tmp_path), changed=changed) == 1  # unreadable: main closed
    assert run_hook(branch, "agent-1", ff, git_dir=str(tmp_path), changed=changed) == 0


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_path_rules_over_http(tmp_path):
    """A real push through http-backend and the hook, with rules a plugin changes during the run."""
    root = tmp_path / "srv"
    repo = root / "team.git"
    git = lambda *a, cwd=None, check=True: subprocess.run(  # noqa: E731
        ["git", *a], cwd=cwd, capture_output=True, text=True, check=check)
    git("init", "-q", "--bare", str(repo))
    git("--git-dir", str(repo), "symbolic-ref", "HEAD", "refs/heads/main")
    for k in ("receive.denyNonFastForwards", "receive.denyDeletes", "http.receivepack"):
        git("--git-dir", str(repo), "config", k, "true")
    # The backend gives the hook a bare environment, so point it at this very package.
    src = Path(gitserver.__file__).resolve().parents[1]
    hook = repo / "hooks" / "pre-receive"
    hook.write_text(f"#!/bin/sh\nPYTHONPATH={src} exec {sys.executable} -m multi_agent_harness.gitserver hook\n")
    hook.chmod(0o755)
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps({n: hashlib.sha256(f"tok-{n}".encode()).hexdigest()
                                  for n in ("agent-1", "agent-2")}))
    rules = repo / "path-rules.json"

    def own(**prefixes):
        rules.write_text(json.dumps({"rules": [{"prefix": p.replace("__", "/"), "agents": a}
                                               for p, a in prefixes.items()]}))

    class H(Handler):
        pass
    H.root, H.tokens = str(root), str(tokens)
    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = lambda n: f"http://{n}:tok-{n}@127.0.0.1:{server.server_port}/team.git"  # noqa: E731
    ident = ["-c", "user.name=t", "-c", "user.email=t@t"]

    def commit(w, path, text, msg):
        (w / path).parent.mkdir(parents=True, exist_ok=True)
        (w / path).write_text(text)
        git("add", "-A", cwd=w)
        git(*ident, "commit", "-q", "-m", msg, cwd=w)

    try:
        w1, w2 = tmp_path / "w1", tmp_path / "w2"
        git("init", "-q", "-b", "main", str(w1))
        commit(w1, "README.md", "team\n", "seed")
        git("push", "-q", url("agent-1"), "main", cwd=w1)
        own(design__a__=["agent-1"], design__b__=[])
        git("clone", "-q", url("agent-2"), str(w2))
        commit(w2, "design/a/x.json", "{}\n", "x")
        refused = git("push", url("agent-2"), "main", cwd=w2, check=False)
        assert refused.returncode != 0
        assert "agent-2 may not change design/a/x.json on main: design/a/ is writable by agent-1" in refused.stderr
        git("push", "-q", url("agent-2"), "main:refs/heads/agent-2/x", cwd=w2)  # a branch is fine
        git(*ident, "pull", "-q", "--no-rebase", url("agent-1"), "refs/heads/agent-2/x", cwd=w1)
        git("push", "-q", url("agent-1"), "main", cwd=w1)  # the owner lands it
        (w1 / "design" / "b").mkdir()
        git("mv", "design/a/x.json", "design/b/x.json", cwd=w1)
        git(*ident, "commit", "-q", "-m", "move", cwd=w1)
        moved = git("push", url("agent-1"), "main", cwd=w1, check=False)
        assert moved.returncode != 0 and "design/b/x.json on main: design/b/ is writable by nobody" in moved.stderr
        own(design__a__=["agent-1"], design__b__=["agent-1"])  # rules change mid-run
        git("push", "-q", url("agent-1"), "main", cwd=w1)
        git(*ident, "pull", "-q", "--rebase", url("agent-2"), "main", cwd=w2)
        commit(w2, "flight/guidance.py", "pass\n", "free path")
        git("push", "-q", url("agent-2"), "main", cwd=w2)
        rules.write_text("not json")
        commit(w2, "notes.md", "hi\n", "notes")
        closed = git("push", url("agent-2"), "main", cwd=w2, check=False)
        assert closed.returncode != 0 and "main is closed" in closed.stderr
        log = git("--git-dir", str(repo), "log", "--format=%s", "main").stdout.splitlines()
        assert log == ["free path", "move", "x", "seed"], log
    finally:
        server.shutdown()
