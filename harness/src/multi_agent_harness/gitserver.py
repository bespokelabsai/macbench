"""The team's git remote over smart HTTP: `git http-backend` behind per-agent basic auth.

Runs on the hub. Each agent authenticates as its own name with a token whose sha256 is in the
tokens file, so the backend and the pre-receive hook know who is pushing (REMOTE_USER). The git
daemon can't tell agents apart, which is why this replaces it.

    multi-agent-harness-git serve --port 8792 --root /srv/git --tokens /run/multi-agent-harness/git-tokens.json
    multi-agent-harness-git hook        # the pre-receive hook: reads `<old> <new> <ref>` lines on stdin

Push rules, with receive.denyNonFastForwards and receive.denyDeletes set on the repo:
An agent may update main (fast-forward only) and refs/heads/<its name>/*, and nothing else.

Path rules, optional: a task can also give paths on main to particular agents, for example one
folder per subsystem owner. Something on the hub, such as a board plugin, writes them to
`path-rules.json` in the bare repo, and the hook reads that file on every push, so the rules can
change during a run:

    {"rules": [{"prefix": "design/launcher/", "agents": ["<agent name>"], "note": "the launcher's owner"}]}

Each path a push to main changes (old..new; a rename counts as both of its paths) follows the rule
with the longest prefix that matches it, and the pusher must be one of that rule's agents. An empty
list means nobody. Paths that no rule matches are free, and branches are never checked. Without the
file nothing changes. A file that can't be read closes main until it is fixed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import unquote

MAIN = "refs/heads/main"
PATH_RULES = "path-rules.json"  # in the bare repo; see the module docstring
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _zero(sha: str) -> bool:
    return set(sha) == {"0"}


def check_update(agent: str, old: str, new: str, ref: str,
                 is_ancestor: Callable[[str, str], bool]) -> str | None:
    """None if `agent` may move `ref` from `old` to `new`, else the reason it may not."""
    if not agent:
        return "unauthenticated push"
    if _zero(new):
        return f"deleting {ref} is not allowed"
    if ref == MAIN:
        if _zero(old) or is_ancestor(old, new):
            return None
        return "main only accepts fast-forwards; pull --rebase and push again"
    if ref.startswith(f"refs/heads/{agent}/"):
        return None
    return f"{agent} may push only to main and refs/heads/{agent}/*, not {ref}"


def _git_is_ancestor(old: str, new: str) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", old, new]).returncode == 0


def load_path_rules(git_dir: str | Path) -> list[dict] | None:
    """The repo's path rules, or None when it has no rules file. Raises ValueError when the file
    can't be read or isn't {"rules": [{"prefix": str, "agents": [str, ...], "note": str}, ...]}
    (note is optional)."""
    try:
        data = json.loads((Path(git_dir) / PATH_RULES).read_text())
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ValueError(f"{PATH_RULES}: {e}") from None
    rules = data.get("rules") if isinstance(data, dict) else None
    if not isinstance(rules, list) or not all(
            isinstance(r, dict) and isinstance(r.get("prefix"), str) and r["prefix"]
            and isinstance(r.get("agents"), list) and all(isinstance(a, str) for a in r["agents"])
            for r in rules):
        raise ValueError(f'{PATH_RULES} must be {{"rules": [{{"prefix": "...", "agents": ["..."]}}, ...]}}')
    return rules


def check_paths(agent: str, paths: Iterable[str], rules: list[dict]) -> str | None:
    """None if `agent` may change every one of `paths` on main, else the reason for the first it
    may not. A path follows the rule with the longest matching prefix; no matching rule, it is free."""
    for path in paths:
        rule = max((r for r in rules if path.startswith(r["prefix"])),
                   key=lambda r: len(r["prefix"]), default=None)
        if rule is None or agent in rule["agents"]:
            continue
        note = f" ({rule['note']})" if rule.get("note") else ""
        return (f"{agent} may not change {path} on main: {rule['prefix']} is writable by "
                f"{', '.join(rule['agents']) or 'nobody'}{note}")
    return None


def _git_changed(old: str, new: str) -> list[str]:
    """The paths that differ between old and new; a rename is its old and its new path."""
    out = subprocess.run(["git", "diff", "--name-only", "--no-renames", "-z",
                          EMPTY_TREE if _zero(old) else old, new], capture_output=True, check=True).stdout
    return [p for p in out.decode("utf-8", "surrogateescape").split("\0") if p]


def _check_main_paths(agent: str, old: str, new: str, git_dir: str,
                      changed: Callable[[str, str], list[str]]) -> str | None:
    try:
        rules = load_path_rules(git_dir)
    except ValueError as e:
        return f"main is closed until the repo's path rules are fixed: {e}"
    if not rules:
        return None
    try:
        paths = changed(old, new)
    except (OSError, subprocess.CalledProcessError) as e:
        return f"could not list the paths this push changes: {e}"
    return check_paths(agent, paths, rules)


def run_hook(lines: Iterable[str], agent: str,
             is_ancestor: Callable[[str, str], bool] = _git_is_ancestor,
             git_dir: str | None = None,
             changed: Callable[[str, str], list[str]] = _git_changed) -> int:
    """Check each `<old> <new> <ref>` line. With git_dir, updates of main also follow the path
    rules in that repo, if it has any."""
    ok = True
    for line in lines:
        parts = line.split()
        if len(parts) != 3:
            continue
        reason = check_update(agent, *parts, is_ancestor=is_ancestor)
        if reason is None and parts[2] == MAIN and git_dir is not None:
            reason = _check_main_paths(agent, parts[0], parts[1], git_dir, changed)
        if reason:
            print(f"multi-agent-harness: {reason}", file=sys.stderr)
            ok = False
    return 0 if ok else 1


def check_token(tokens_path: str, header: str | None) -> str | None:
    """The agent name for a valid `Authorization: Basic` header, else None."""
    if not header or not header.startswith("Basic "):
        return None
    try:
        user, _, password = base64.b64decode(header[6:], validate=True).decode().partition(":")
        tokens = json.loads(Path(tokens_path).read_text())
    except (ValueError, OSError, UnicodeDecodeError):
        return None
    want = tokens.get(user)
    got = hashlib.sha256(password.encode()).hexdigest()
    return user if isinstance(want, str) and hmac.compare_digest(want, got) else None


class Handler(BaseHTTPRequestHandler):
    server_version = "multi-agent-harness-git"
    root = "/srv/git"
    tokens = "/run/multi-agent-harness/git-tokens.json"

    def do_GET(self):
        self._git()

    def do_POST(self):
        self._git()

    def log_message(self, fmt, *args):  # no Authorization in here, just method, path, status
        sys.stderr.write("%s %s\n" % (self.client_address[0], fmt % args))

    def _body(self, sink) -> None:
        try:
            if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                while True:
                    size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                    if size == 0:
                        self.rfile.readline()
                        break
                    sink.write(self.rfile.read(size))
                    self.rfile.readline()
            else:
                left = int(self.headers.get("Content-Length") or 0)
                while left > 0:
                    chunk = self.rfile.read(min(left, 65536))
                    if not chunk:
                        break
                    sink.write(chunk)
                    left -= len(chunk)
        except (OSError, ValueError):
            pass
        finally:
            try:
                sink.close()
            except OSError:
                pass

    def _git(self) -> None:
        agent = check_token(self.tokens, self.headers.get("Authorization"))
        if agent is None:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="team"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        path, _, query = self.path.partition("?")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_PROJECT_ROOT": self.root,
               "GIT_HTTP_EXPORT_ALL": "1", "PATH_INFO": unquote(path), "QUERY_STRING": query,
               "REQUEST_METHOD": self.command, "REMOTE_USER": agent,
               "REMOTE_ADDR": self.client_address[0],
               "CONTENT_TYPE": self.headers.get("Content-Type", "")}
        for header, var in (("Content-Encoding", "HTTP_CONTENT_ENCODING"),
                            ("Git-Protocol", "HTTP_GIT_PROTOCOL")):
            if self.headers.get(header):
                env[var] = self.headers[header]
        if self.headers.get("Content-Length") and "chunked" not in self.headers.get("Transfer-Encoding", ""):
            env["CONTENT_LENGTH"] = self.headers["Content-Length"]
        proc = subprocess.Popen(["git", "http-backend"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, env=env)
        writer = threading.Thread(target=self._body, args=(proc.stdin,), daemon=True)
        writer.start()
        status, headers = 200, []
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("latin-1").rstrip("\r\n")
            if not line:
                break
            key, _, value = line.partition(":")
            if key.lower() == "status":
                status = int(value.split()[0])
            else:
                headers.append((key, value.strip()))
        self.send_response(status)
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        shutil.copyfileobj(proc.stdout, self.wfile)
        writer.join(timeout=5)
        proc.wait()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="multi-agent-harness-git")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8792)
    s.add_argument("--root", default="/srv/git")
    s.add_argument("--tokens", default="/run/multi-agent-harness/git-tokens.json")
    sub.add_parser("hook")
    a = p.parse_args(argv)
    if a.cmd == "hook":  # git runs it in the bare repo
        return run_hook(sys.stdin, os.environ.get("REMOTE_USER", ""), git_dir=os.environ.get("GIT_DIR", "."))
    Handler.root, Handler.tokens = a.root, a.tokens
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
