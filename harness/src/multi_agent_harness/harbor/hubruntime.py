"""The hub's runtime, installed at start into /opt/multi-agent-harness on whatever image the task uses.

git and curl come from the image's package manager (apt-get, dnf, yum or apk, the same
detection Harbor's installed agents use). Python comes from uv's managed builds, so the hub does
not depend on the image having one. The harness package goes in as a wheel built here from this
checkout and uploaded, with the hub extra pinned by hub-constraints.txt.
"""

from __future__ import annotations

import base64
import hashlib
import shlex
import tomllib
import zipfile
from pathlib import Path

RUNTIME = "/opt/multi-agent-harness"
VENV_BIN = f"{RUNTIME}/venv/bin"
UV_VERSION = "0.11.28"
PYTHON = "3.12"
CONSTRAINTS = Path(__file__).with_name("hub-constraints.txt")
TEAM_GIT = "/srv/git/team.git"
_ZIP_DATE = (1980, 1, 1, 0, 0, 0)


def repo_root() -> Path:
    root = Path(__file__).resolve().parents[3]
    if not (root / "pyproject.toml").is_file() or not (root / "src" / "multi_agent_harness").is_dir():
        raise FileNotFoundError(
            f"no multi-agent-harness checkout at {root}; the hub runtime wheel is built from the repo "
            "(pip install -e .)")
    return root


def _record_hash(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def build_wheel(out_dir: Path, root: Path | None = None) -> Path:
    """A pure-python wheel of src/multi_agent_harness with pyproject's metadata, extras and
    scripts. Built directly (no build backend needed on the host) and byte-stable for unchanged sources."""
    root = root or repo_root()
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    name, ver = project["name"], project["version"]
    pkg = name.replace("-", "_")  # the import package, and the wheel's normalized name
    dist = f"{pkg}-{ver}.dist-info"
    meta = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {ver}"]
    if project.get("requires-python"):
        meta.append(f"Requires-Python: {project['requires-python']}")
    meta += [f"Requires-Dist: {r}" for r in project.get("dependencies", [])]
    for extra, reqs in project.get("optional-dependencies", {}).items():
        meta.append(f"Provides-Extra: {extra}")
        meta += [f'Requires-Dist: {r} ; extra == "{extra}"' for r in reqs]
    scripts = project.get("scripts", {})
    entry = "[console_scripts]\n" + "".join(f"{k} = {v}\n" for k, v in scripts.items())
    files: list[tuple[str, bytes]] = []
    src = root / "src" / pkg
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src.parent)
        if p.is_file() and "__pycache__" not in rel.parts and p.suffix != ".pyc":
            files.append((rel.as_posix(), p.read_bytes()))
    files += [(f"{dist}/METADATA", ("\n".join(meta) + "\n").encode()),
              (f"{dist}/entry_points.txt", entry.encode()),
              (f"{dist}/WHEEL", b"Wheel-Version: 1.0\nGenerator: multi-agent-harness\nRoot-Is-Purelib: true\n"
                                b"Tag: py3-none-any\n")]
    record = "".join(f"{n},{_record_hash(d)},{len(d)}\n" for n, d in files) + f"{dist}/RECORD,,\n"
    files.append((f"{dist}/RECORD", record.encode()))
    out = Path(out_dir) / f"{pkg}-{ver}-py3-none-any.whl"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in files:
            info = zipfile.ZipInfo(n, _ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, d)
    return out


# Package-manager detection as in harbor/agents/installed/base.py (_get_system_package_manager).
ENSURE_PACKAGES = """\
need=""
for c in {commands}; do command -v "$c" >/dev/null 2>&1 || need=1; done
if [ -n "$need" ]; then
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq && apt-get install -y -qq --no-install-recommends {packages}
  elif command -v dnf >/dev/null 2>&1; then dnf install -y -q {packages}
  elif command -v yum >/dev/null 2>&1; then yum install -y -q {packages}
  elif command -v apk >/dev/null 2>&1; then apk add --no-cache -q {packages}
  else echo "no supported package manager to install {packages}" >&2; exit 1
  fi
fi
"""


def ensure_packages(commands: tuple[str, ...], packages: tuple[str, ...]) -> str:
    return ENSURE_PACKAGES.format(commands=" ".join(commands), packages=" ".join(packages))


def install_script(wheel_name: str) -> str:
    """Shell that installs git, curl, uv, a managed python and the multi-agent-harness[hub] wheel (already
    uploaded to /opt/multi-agent-harness) into /opt/multi-agent-harness. Idempotent."""
    r = RUNTIME
    req = shlex.quote(f"multi-agent-harness[hub] @ file://{r}/{wheel_name}")
    return "set -eu\n" + ensure_packages(("git", "curl"), ("git", "curl", "ca-certificates")) + f"""\
mkdir -p {r}/bin
if [ ! -x {r}/bin/uv ]; then
  curl -LsSf https://astral.sh/uv/{UV_VERSION}/install.sh | env UV_INSTALL_DIR={r}/bin UV_NO_MODIFY_PATH=1 sh
fi
export UV_PYTHON_INSTALL_DIR={r}/python UV_CACHE_DIR={r}/cache
[ -x {VENV_BIN}/python ] || {r}/bin/uv venv -q --python {PYTHON} --python-preference only-managed {r}/venv
{r}/bin/uv pip install -q --python {VENV_BIN}/python -c {r}/hub-constraints.txt {req}
rm -rf {r}/cache
{VENV_BIN}/python -c "import multi_agent_harness.board.server, multi_agent_harness.proxy.server, multi_agent_harness.gitserver, multi_agent_harness.gateway"
"""


def git_script() -> str:
    """Shell that creates the team remote: bare, HEAD on main, no force pushes or deletions,
    and the pre-receive hook that enforces per-agent ref ownership. workdir_cmd("seed") then
    makes the first commit on main from the task workdir."""
    g = TEAM_GIT
    return f"""\
set -eu
if [ ! -d {g} ]; then
  git init -q --bare {g}
  git --git-dir={g} symbolic-ref HEAD refs/heads/main
fi
git --git-dir={g} config receive.denyNonFastForwards true
git --git-dir={g} config receive.denyDeletes true
git --git-dir={g} config http.receivepack true
printf '#!/bin/sh\\nexec {VENV_BIN}/python -m multi_agent_harness.gitserver hook\\n' > {g}/hooks/pre-receive
chmod 755 {g}/hooks/pre-receive
"""


def workdir_cmd(action: str, workdir: str, state_dir: str) -> str:
    """multi_agent_harness.workdir seed or checkout on the hub, with the hub's python."""
    return (f"{VENV_BIN}/python -m multi_agent_harness.workdir {action} --git-dir {TEAM_GIT} "
            f"--workdir {shlex.quote(workdir)} --state {shlex.quote(state_dir)}")
