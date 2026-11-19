"""The task workdir and the team repo: which directory it is, how it seeds team.git, and how the
team's final main comes back into it before the verifier runs.

Resolution, first match wins (resolve_static, then accept_sandbox):
  1. task.toml [environment] workdir
  2. the last WORKDIR of the Dockerfile's final stage (absolute, no variables)
  3. `pwd` in a login shell on the hub, which is the image's WORKDIR for prebuilt images
`/`, and a pwd that is just $HOME, are never used: seeding them would put the whole filesystem or
the CLI installs in git. Those fall back to /workspace, the harness default.

Runs on the hub (no harbor import) as `python -m multi_agent_harness.workdir seed|checkout ...`:

seed: commit the workdir's contents as the first commit on main. `git add -A` with the workdir's
own .gitignore files; nothing else is filtered (no size or binary rule: the image's files are the
task's starting state). Nested git repos are left out, since git would store them as gitlinks
with no content.

checkout: right before the verifier, move everything in the workdir aside to
<state>/workdir-aside/<ts> and extract main there as a clean tree (git archive, no .git). The
workdir then holds exactly the team's main: a file the team deleted or never pushed can't satisfy
the verifier. Harbor does the rest, including copying listed `artifacts` to a separate verifier.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

DEFAULT = "/workspace"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
META = "workdir.json"
_GIT = ["git", "-c", "safe.directory=*", "-c", "core.excludesFile=/dev/null"]


# ---- resolution (host side)

def dockerfile_workdir(text: str) -> str | None:
    """The final stage's last WORKDIR, joined onto earlier ones when relative. None when the
    stage sets none (it inherits from its base) or uses a variable."""
    lines = re.sub(r"\\\r?\n", " ", text).splitlines()
    wd: str | None = None
    for line in lines:
        parts = line.strip().split(None, 1)
        if not parts or parts[0].startswith("#"):
            continue
        op = parts[0].upper()
        if op == "FROM":
            wd = None
        elif op == "WORKDIR" and len(parts) == 2:
            value = parts[1].strip().strip("\"'")
            if "$" in value:
                wd = None
                continue
            if not value.startswith("/"):
                if wd is None:
                    continue
                value = f"{wd.rstrip('/')}/{value}"
            wd = os.path.normpath(value)
    return wd


def usable(path: str | None) -> bool:
    return bool(path) and path.startswith("/") and os.path.normpath(path) != "/"


def resolve_static(toml_workdir: str | None, environment_dir: str | Path) -> tuple[str, str] | None:
    """(workdir, source) from task.toml or the Dockerfile, else None (ask the sandbox)."""
    if toml_workdir:
        return (os.path.normpath(toml_workdir), "task.toml") if usable(toml_workdir) else (DEFAULT, "default")
    try:
        wd = dockerfile_workdir((Path(environment_dir) / "Dockerfile").read_text())
    except OSError:
        wd = None
    if wd is not None:
        return (wd, "dockerfile") if usable(wd) else (DEFAULT, "default")
    return None


def accept_sandbox(pwd: str, home: str) -> tuple[str, str]:
    pwd = pwd.strip()
    if usable(pwd) and os.path.normpath(pwd) != os.path.normpath(home.strip() or "/"):
        return os.path.normpath(pwd), "sandbox"
    return DEFAULT, "default"


# ---- seed and checkout (hub side)

def _git(git_dir: str, *args: str, work_tree: str | None = None, index: str | None = None,
         stdin: bytes | None = None) -> bytes:
    env = {**os.environ, "GIT_DIR": git_dir}
    if work_tree:
        env["GIT_WORK_TREE"] = work_tree
    if index:
        env["GIT_INDEX_FILE"] = index
    r = subprocess.run([*_GIT, *args], env=env, input=stdin, capture_output=True, cwd=work_tree)
    if r.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {r.stderr.decode(errors='replace')[-500:]}")
    return r.stdout


def _glob_escape(path: str) -> str:
    return re.sub(r"([\\*?\[\]!#])", r"\\\1", path)


def _z(out: bytes) -> list[str]:
    return [p for p in out.decode(errors="surrogateescape").split("\0") if p]


def seed(git_dir: str, workdir: str, state_dir: str) -> dict:
    """First commit on main from the workdir's files. A no-op if main already exists."""
    w = Path(workdir)
    w.mkdir(parents=True, exist_ok=True)
    meta_path = Path(state_dir) / META
    has_main = subprocess.run([*_GIT, "--git-dir", git_dir, "rev-parse", "-q", "--verify", "refs/heads/main"],
                              capture_output=True).returncode == 0
    if has_main and meta_path.is_file():
        return json.loads(meta_path.read_text())
    with tempfile.TemporaryDirectory(prefix="multi-agent-harness-seed-") as tmp:
        # Nested repos: git would store them as gitlinks with no content, so they stay out.
        nested = []
        for d, subdirs, files in os.walk(w):
            if d != str(w) and (".git" in subdirs or ".git" in files):
                nested.append(os.path.relpath(d, w))
                subdirs[:] = []
            else:
                subdirs[:] = [s for s in subdirs if s != ".git"]
        excludes = f"{tmp}/exclude"
        Path(excludes).write_text("".join(f"/{_glob_escape(n)}/\n" for n in nested))
        index = f"{tmp}/index"
        ex = ("-c", f"core.excludesFile={excludes}")
        _git(git_dir, *ex, "add", "-A", ".", work_tree=workdir, index=index)
        tracked = _z(_git(git_dir, "ls-files", "-z", work_tree=workdir, index=index))
        tree = _git(git_dir, "write-tree", work_tree=workdir, index=index).decode().strip() or EMPTY_TREE
    if not has_main:
        env = {**os.environ, "GIT_AUTHOR_NAME": "hub", "GIT_AUTHOR_EMAIL": "hub@team.local",
               "GIT_COMMITTER_NAME": "hub", "GIT_COMMITTER_EMAIL": "hub@team.local", "GIT_DIR": git_dir}
        r = subprocess.run([*_GIT, "commit-tree", tree, "-m", f"task starting files from {workdir}"],
                           env=env, capture_output=True, check=True)
        _git(git_dir, "update-ref", "refs/heads/main", r.stdout.decode().strip())
    meta = {"workdir": workdir, "seed": _git(git_dir, "rev-parse", "refs/heads/main").decode().strip(),
            "files": len(tracked), "nested_repos_skipped": nested}
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, indent=1))
    return meta


def checkout(git_dir: str, workdir: str, state_dir: str) -> dict:
    """Replace the workdir with a clean tree of main; the old contents go aside, not away."""
    w = Path(workdir)
    w.mkdir(parents=True, exist_ok=True)
    aside = Path(state_dir) / "workdir-aside" / time.strftime("%Y%m%dT%H%M%S")
    aside.mkdir(parents=True, exist_ok=True)
    for child in list(w.iterdir()):
        shutil.move(str(child), str(aside / child.name))
    sha = _git(git_dir, "rev-parse", "refs/heads/main^{commit}").decode().strip()
    data = _git(git_dir, "archive", "--format=tar", sha)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(w, filter="tar")
    result = {"workdir": workdir, "commit": sha, "aside": str(aside)}
    meta_path = Path(state_dir) / META
    meta = json.loads(meta_path.read_text()) if meta_path.is_file() else {}
    meta_path.write_text(json.dumps({**meta, "checkout": result}, indent=1))
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m multi_agent_harness.workdir")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("seed", "checkout"):
        s = sub.add_parser(name)
        s.add_argument("--git-dir", required=True)
        s.add_argument("--workdir", required=True)
        s.add_argument("--state", required=True)
    a = p.parse_args(argv)
    r = seed(a.git_dir, a.workdir, a.state) if a.cmd == "seed" else checkout(a.git_dir, a.workdir, a.state)
    print(json.dumps(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
