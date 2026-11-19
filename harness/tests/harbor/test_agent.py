import asyncio
import json
import logging
import re
import shlex
import tomllib
from pathlib import Path

import pytest
from harbor.environments.base import ExecResult
from harbor.models.agent.context import AgentContext
from harbor.models.task.config import NetworkMode, NetworkPolicy

import multi_agent_harness.harbor.agent as ta
from multi_agent_harness.harbor.agent import TeamAgent
from multi_agent_harness.harbor.orchestration import Orchestration

pytestmark = pytest.mark.asyncio

HUB = "https://8080-hub.proxy.test"
ADMIN = {"board": "BOARD-ADMIN-SECRET", "proxy": "PROXY-ADMIN-SECRET"}


class FakeInstaller:
    """Stands in for Harbor's ClaudeCode/Codex: records where install ran."""
    events: list = []

    def __init__(self, logs_dir, model_name=None, version=None, **kw):
        self.logs_dir = logs_dir

    async def setup(self, target):
        await target.exec("install-cli", user="root")
        self.events.append(("install", target.service))

    async def ensure_system_dependencies(self, target, deps):
        assert set(deps) == {"git", "curl"}

    def populate_context_post_run(self, ctx):
        pass


@pytest.fixture(autouse=True)
def fake_installer(monkeypatch):
    FakeInstaller.events = []
    monkeypatch.setattr(ta, "installer_class", lambda harness: FakeInstaller)


class FakeEnv:
    """Records execs and uploads; answers hub admin curls like the board and proxy would."""

    def __init__(self, n=3):
        self.team_agent_services = [f"agent-{i}" for i in range(1, n + 1)]
        self.team_board_url = HUB
        self.team_proxy_url = HUB + "/proxy"
        self.team_hub_headers = {"X-Daytona-Preview-Token": "PREVIEW-SECRET"}
        self.team_git_url = HUB + "/git/team.git"
        self.team_board_admin_token = ADMIN["board"]
        self.team_proxy_admin_token = ADMIN["proxy"]
        self.team_board_log_path = "/var/lib/multi-agent-harness/board-events.jsonl"
        self.team_proxy_log_path = "/var/lib/multi-agent-harness/proxy-usage.jsonl"
        self.team_git_tokens = {n: f"GTOK-{n}" for n in self.team_agent_services}
        self.team_workdir = "/app"
        self.default_user = None
        self.calls, self.files, self.downloads = [], {}, []
        self.trace = []  # agent-sandbox uploads and execs, in order
        self.in_flight = self.max_in_flight = 0
        self.verified = True
        FakeInstaller.events = self.events = FakeInstaller.events

    async def verify_agent_network(self):
        self.events.append(("verify",))
        if not self.verified:
            raise RuntimeError("agent-1 network no-network: example.com reachable; failing closed")
        return NetworkPolicy(network_mode=NetworkMode.NO_NETWORK)

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        return await self.service_exec(command, service=None, env=env, timeout_sec=timeout_sec, user=user)

    async def service_exec(self, command, *, service=None, cwd=None, env=None, timeout_sec=None,
                           user=None):
        self.calls.append({"service": service, "command": command, "env": env or {}, "user": user})
        if service is not None:
            self.trace.append(("exec", service, user, command))
        if service is None and "curl" in command:
            hdr = re.search(r"-H @(\S+)", command).group(1)
            assert self.files[(None, hdr)].startswith("Authorization: Bearer ")
            if "/admin/usage" in command:
                return ExecResult(stdout=json.dumps({"total": {"usd": 1.25, "requests": 3,
                                                               "tokens": {"input": 100, "output": 20}}}),
                                  return_code=0)
            if "/admin/plugins" in command:
                return ExecResult(stdout='{"plugins": []}', return_code=0)
            name = json.loads(re.search(r"-d '(.*?)'", command).group(1))["name"]
            out = re.search(r"-o (\S+)", command).group(1)
            body = {"agent": name, "token": f"BTOK-{name}"} if "8790/api/" in command \
                else {"agent": name, "key": f"PKEY-{name}"}
            self.files[(None, out)] = json.dumps(body)
            return ExecResult(stdout="", return_code=0)
        if "MULTI_AGENT_HARNESS_PROMPT" in (env or {}):
            self.events.append(("launch", service))
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            await asyncio.sleep(0.05)
            self.in_flight -= 1
        return ExecResult(stdout="", return_code=0)

    async def upload_file(self, source_path, target_path):
        self.files[(None, target_path)] = Path(source_path).read_text()

    async def service_upload_file(self, source_path, target_path, *, service=None):
        self.files[(service, target_path)] = Path(source_path).read_text()
        self.events.append(("upload", service, target_path))
        self.trace.append(("upload", service, target_path))

    async def download_file(self, source_path, target_path):
        self.downloads.append(source_path)
        Path(target_path).write_text(self.files.get((None, source_path), '{"event":"post"}\n'))

    async def service_download_dir(self, source_dir, target_dir, *, service=None):
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "claude-code.txt").write_text('{"type":"result"}\n')


def secrets(env):
    return [*ADMIN.values(), "PREVIEW-SECRET"] + [f"{p}-{n}" for n in env.team_agent_services for p in ("BTOK", "PKEY", "GTOK")]


def assert_no_secret_in_exec(env):
    for call in env.calls:
        blob = call["command"] + json.dumps(call["env"])
        assert not any(s in blob for s in secrets(env)), call


@pytest.mark.parametrize("harness", ["claude-code", "codex"])
async def test_setup_registers_and_writes_mcp_config(tmp_path, harness):
    env = FakeEnv(3)
    agent = TeamAgent(tmp_path, model_name="x/some-model", harness=harness)
    await agent.setup(env)
    assert not any(e[0] == "upload" for e in env.events)  # no agent secrets under the baseline
    assert sorted(e[1] for e in env.events if e[0] == "install") == env.team_agent_services
    await agent._lock_down(env)

    curls = [c["command"] for c in env.calls if c["service"] is None and "curl" in c["command"]]
    assert sorted(c.split()[-1] for c in curls) == sorted(
        ["http://127.0.0.1:8790/api/admin/agents"] * 3 + ["http://127.0.0.1:8791/admin/agents"] * 3)
    assert env.files[(None, "/run/multi-agent-harness/team-agent/board.hdr")] == f"Authorization: Bearer {ADMIN['board']}\n"

    sh = env.files[("agent-2", "/run/multi-agent-harness/agent.env")]
    assert "MULTI_AGENT_HARNESS_BOARD_TOKEN=BTOK-agent-2" in sh and f"MULTI_AGENT_HARNESS_BOARD_URL={HUB}\n" in sh
    assert "MULTI_AGENT_HARNESS_HUB_HEADER_1=PREVIEW-SECRET\n" in sh
    config = [c["command"] for c in env.calls if c["service"] == "agent-2"][-1]
    assert "chmod 600 /run/multi-agent-harness/agent.env /run/multi-agent-harness/git-credentials /run/multi-agent-harness/gitconfig" in config
    assert "credential.helper 'store --file=/run/multi-agent-harness/git-credentials'" in config
    assert "include.path /run/multi-agent-harness/gitconfig" in config
    assert 'curl -fsS -m 30 -o /dev/null -H "X-Daytona-Preview-Token: $MULTI_AGENT_HARNESS_HUB_HEADER_1" ' in config
    assert env.files[("agent-2", "/run/multi-agent-harness/git-credentials")] == "https://agent-2:GTOK-agent-2@8080-hub.proxy.test\n"
    assert env.files[("agent-2", "/run/multi-agent-harness/gitconfig")] == (
        f'[http "{HUB}/"]\n\textraHeader = X-Daytona-Preview-Token: PREVIEW-SECRET\n')
    if harness == "claude-code":
        managed = json.loads(env.files[("agent-2", MANAGED)])
        assert set(managed["permissions"]["deny"]) >= {"Read(//run/multi-agent-harness/agent.env)", "Read(//proc/*/environ)",
                                                       "Edit(//etc/claude-code/**)"}
        assert "sandbox" not in managed  # Bash runs unsandboxed off the subscription token
        trace = [t for t in env.trace if t[1] == "agent-2"]
        upload = trace.index(("upload", "agent-2", MANAGED))
        fix = next(i for i, t in enumerate(trace) if t[0] == "exec" and f"chown 0:0 {MANAGED}; chmod 644 {MANAGED}" in t[3])
        assert upload < fix  # root's and readable, set after the upload
        assert f"ANTHROPIC_BASE_URL={HUB}/proxy/anthropic" in sh
        assert "ANTHROPIC_API_KEY=PKEY-agent-2" in sh
        assert "ANTHROPIC_CUSTOM_HEADERS='X-Daytona-Preview-Token: PREVIEW-SECRET'" in sh
        assert ('claude mcp add --scope user --transport http board "$MULTI_AGENT_HARNESS_BOARD_URL/mcp" '
                '--header "Authorization: Bearer $MULTI_AGENT_HARNESS_BOARD_TOKEN" '
                '--header "X-Daytona-Preview-Token: $MULTI_AGENT_HARNESS_HUB_HEADER_1" ') in config
    else:
        assert json.loads(env.files[("agent-2", "/run/multi-agent-harness/codex/auth.json")]) == {"OPENAI_API_KEY": "PKEY-agent-2"}
        toml = tomllib.loads(env.files[("agent-2", "/run/multi-agent-harness/codex/config.toml")])
        header = {"X-Daytona-Preview-Token": "MULTI_AGENT_HARNESS_HUB_HEADER_1"}
        assert toml["model"] == "some-model" and toml["model_provider"] == "multi-agent-harness"
        assert toml["model_providers"]["multi-agent-harness"] == {
            "name": "multi-agent-harness proxy", "base_url": f"{HUB}/proxy/openai/v1", "env_key": "OPENAI_API_KEY",
            "wire_api": "responses", "env_http_headers": header}
        assert toml["mcp_servers"]["board"] == {
            "url": f"{HUB}/mcp", "bearer_token_env_var": "MULTI_AGENT_HARNESS_BOARD_TOKEN", "env_http_headers": header}
    assert_no_secret_in_exec(env)


@pytest.mark.parametrize("harness", ["claude-code", "codex"])
@pytest.mark.parametrize("user", [None, "agent"])
async def test_non_root_agent_user_owns_its_harness_files(tmp_path, harness, user):
    """task.toml [agent] user = "agent": the hub's admin calls and the dir setup run as root, the agent
    sandbox's secrets and logs go to that user, and its own config and CLI run as the default user. A root
    task names no user, so its commands run as before."""
    env = FakeEnv(2)
    env.default_user = user
    admin = "root" if user else None
    agent = TeamAgent(tmp_path, model_name="some-model", harness=harness)
    await agent.setup(env)
    await agent._lock_down(env)
    hub = [c for c in env.calls if c["service"] is None]
    assert hub and all(c["user"] == admin for c in hub)  # ADMIN dir, admin headers, registration curls
    chown = "chown -R agent /run/multi-agent-harness /logs/agent"
    for name in env.team_agent_services:
        trace = [t for t in env.trace if t[1] == name and t[-1] != "install-cli"]
        own = [i for i, t in enumerate(trace) if t[0] == "exec" and "mkdir -p /run/multi-agent-harness/codex" in t[3]]
        config = [i for i, t in enumerate(trace)
                  if t[0] == "exec" and "chmod 600 /run/multi-agent-harness/agent.env" in t[3]]
        uploads = [i for i, t in enumerate(trace) if t[0] == "upload"]
        assert own and uploads and len(config) == 1
        assert own[0] < min(uploads) and max(uploads) < own[-1] < config[0]  # every upload is handed over first
        assert all(trace[i][2] == admin and (chown in trace[i][3]) == bool(user) for i in own)
        assert trace[config[0]][2] is None  # the agent's own setup runs as the task's user
    n = len(env.trace)
    await agent._bind(env, "agent-1", "agent-2", "BTOK-x", fresh=False)
    new = env.trace[n:]
    assert not any(t[0] == "exec" and "chown -R" in t[3] for t in new)
    if user:
        # Root leaves the files in its own handoff dir and the agent's shell copies them into place.
        staged = "/run/multi-agent-harness-handoff/agent-1-agent.env"
        assert new[0] == ("upload", "agent-1", staged) and "BTOK-x" in env.files[("agent-1", staged)]
        assert new[1][2] == "root" and new[1][3].startswith(f"set -e; chown agent {staged}; chmod 600 {staged}")
        assert len(new) == 3 and new[2][2] is None
        assert f"cat {staged} > /run/multi-agent-harness/agent.env; chmod 600 " in new[2][3]
    else:
        assert [t[0] for t in new] == ["upload", "exec"] and new[0][2] == "/run/multi-agent-harness/agent.env"
        assert new[1][2] is None and "handoff" not in new[1][3]


@pytest.mark.parametrize("harness", ["claude-code", "codex"])
async def test_run_launches_all_concurrently_and_collects(tmp_path, harness, caplog):
    caplog.set_level(logging.DEBUG)
    env = FakeEnv(3)
    agent = TeamAgent(tmp_path, model_name="some-model", harness=harness, timeout_sec="600")
    await agent.setup(env)
    ctx = AgentContext()
    await agent.run("Write team.txt.", env, ctx)

    runs = [c for c in env.calls if "MULTI_AGENT_HARNESS_PROMPT" in c["env"]]
    assert sorted(c["service"] for c in runs) == ["agent-1", "agent-2", "agent-3"]
    assert env.max_in_flight == 3
    cli = "claude --verbose --output-format=stream-json" if harness == "claude-code" else \
        "codex exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check"
    for c in runs:
        prompt, me = c["env"]["MULTI_AGENT_HARNESS_PROMPT"], c["service"]
        assert prompt.startswith(f"You are {me}, one of 3 agents")
        assert "/app, is your own clone" in prompt and "inbox(wait_seconds=60)" in prompt
        assert "cd /app;" in c["command"]
        assert prompt.endswith("Task:\nWrite team.txt.")
        assert me not in prompt.split("Your teammates:")[1].split("\n")[0]
        assert c["command"].startswith(". /run/multi-agent-harness/agent.env;") and "timeout -k 30 600 " in c["command"]
        assert f"branches under {me}/" in prompt
        assert cli in c["command"]

    assert ctx.cost_usd == 1.25 and ctx.n_input_tokens == 100 and ctx.n_output_tokens == 20
    clones = [c["command"] for c in env.calls if "git remote add origin" in c["command"]]
    assert len(clones) == 3 and all(f"origin {HUB}/git/team.git" in c and "W=/app;" in c
                                    and "checkout -q -f -B main origin/main" in c for c in clones)
    assert ctx.metadata["workdir"] == "/app"
    assert (ctx.metadata["orchestration"], ctx.metadata["lead"], ctx.metadata["end_reason"]) == \
        ("fixed", None, "all_finished")
    assert not any("lead" in c["env"]["MULTI_AGENT_HARNESS_PROMPT"] for c in runs)
    assert not any("kill -TERM" in c["command"] for c in env.calls)  # nobody is stopped
    assert all(f"2>&1 & echo $! > /run/multi-agent-harness/cli.pid; wait $!; rc=$?" in c["command"] for c in runs)
    assert {"/var/lib/multi-agent-harness/board-events.jsonl", "/var/lib/multi-agent-harness/proxy-usage.jsonl"} <= set(env.downloads)
    assert all((tmp_path / a).is_dir() for a in env.team_agent_services)

    assert_no_secret_in_exec(env)
    logged = caplog.text + json.dumps(ctx.model_dump())
    logged += "".join(p.read_text() for p in tmp_path.rglob("*") if p.is_file())
    assert not any(s in logged for s in secrets(env))


async def test_run_timeout_still_collects(tmp_path, monkeypatch):
    env = FakeEnv(2)
    real = env.service_exec

    async def hang(command, *, service=None, env=None, **kw):
        if env and "MULTI_AGENT_HARNESS_PROMPT" in env:
            await asyncio.sleep(10)
        return await real(command, service=service, env=env, **kw)

    env.service_exec = hang
    monkeypatch.setattr(ta, "KILL_GRACE_SEC", 0)
    monkeypatch.setattr(ta, "STOP_WAIT_SEC", 0.1)
    agent = TeamAgent(tmp_path, harness="claude-code", timeout_sec=0.01)
    agent._env, agent.workdir = env, "/app"
    agent.agents = env.team_agent_services
    agent._creds = {n: (f"BTOK-{n}", f"PKEY-{n}") for n in agent.agents}
    env.files[(None, "/run/multi-agent-harness/team-agent/proxy.hdr")] = "Authorization: Bearer x\n"
    ctx = AgentContext()
    await agent.run("x", env, ctx)
    assert ctx.metadata["timed_out"] is True and ctx.cost_usd == 1.25
    assert ctx.metadata["end_reason"] == "timeout" and agent.stopped == {"agent-1", "agent-2"}


async def test_fixed_lead_ends_when_the_lead_exits_and_stops_the_rest(tmp_path):
    env = FakeEnv(3)
    real, released, prompts = env.service_exec, set(), {}

    async def lead_quick(command, *, service=None, env=None, **kw):
        if env and "MULTI_AGENT_HARNESS_PROMPT" in env and service != "agent-1":
            prompts[service] = env["MULTI_AGENT_HARNESS_PROMPT"]
            while service not in released:  # a worker runs until it is stopped
                await asyncio.sleep(0.01)
            return ExecResult(stdout="", return_code=143)
        if "kill -TERM" in command:
            released.add(service)
        return await real(command, service=service, env=env, **kw)

    env.service_exec = lead_quick
    agent = TeamAgent(tmp_path, harness="codex", orchestration="fixed-lead")
    await agent.setup(env)
    roles = {json.loads(re.search(r"-d '(.*?)'", c["command"]).group(1))["name"]:
             json.loads(re.search(r"-d '(.*?)'", c["command"]).group(1))["role"]
             for c in env.calls if "8790/api/admin/agents" in c["command"]}
    assert roles == {"agent-1": "lead", "agent-2": "peer", "agent-3": "peer"}
    ctx = AgentContext()
    await agent.run("x", env, ctx)
    prompts |= {c["service"]: c["env"]["MULTI_AGENT_HARNESS_PROMPT"] for c in env.calls if "MULTI_AGENT_HARNESS_PROMPT" in c["env"]}
    assert "You are the team lead" in prompts["agent-1"] and prompts["agent-1"].endswith("Task:\nx")
    assert "agent-1 is the team lead" in prompts["agent-2"]
    m = ctx.metadata
    assert (m["orchestration"], m["lead"], m["end_reason"], m["timed_out"]) == \
        ("fixed-lead", "agent-1", "lead_finished", False)
    assert released == {"agent-2", "agent-3"}
    assert {n: (a["exit"], a["stopped"]) for n, a in m["agents"].items()} == {
        "agent-1": (0, False), "agent-2": (143, True), "agent-3": (143, True)}
    assert all((tmp_path / a).is_dir() for a in env.team_agent_services)  # everyone's logs collected


STRATEGY = """
from multi_agent_harness.harbor.orchestration import PEER, Orchestration

class Mine(Orchestration):
    name = "mine"
    def roles(self, agents):
        return {a: PEER for a in agents}
"""


async def test_orchestration_resolves_bundled_names():
    from multi_agent_harness.harbor.orchestration import resolve
    for spec, cls in ((None, "Fixed"), ("fixed", "Fixed"), ("fixed-lead", "FixedLead"), ("async", "AsyncSubagents")):
        o = resolve(spec)
        assert isinstance(o, Orchestration) and type(o).__name__ == cls


async def test_orchestration_resolves_a_file_path(tmp_path, monkeypatch):
    from multi_agent_harness.harbor.orchestration import resolve
    (tmp_path / "mine.py").write_text(STRATEGY)
    monkeypatch.chdir(tmp_path)
    for spec in ("mine.py", "./mine.py", str(tmp_path / "mine.py")):
        assert type(resolve(spec)).__name__ == "Mine"
    agent = TeamAgent(tmp_path, harness="codex", orchestration="./mine.py")
    assert agent.orchestration_name == str(tmp_path / "mine.py")


async def test_orchestration_errors(tmp_path, monkeypatch):
    from multi_agent_harness.harbor.orchestration import resolve
    monkeypatch.chdir(tmp_path)
    (tmp_path / "none.py").write_text("X = 1\n")
    (tmp_path / "two.py").write_text(STRATEGY + "\nclass Other(Mine):\n    pass\n")
    cases = {"missing.py": "no such file.*fixed, fixed-lead, async",
             "none.py": "exactly one subclass.*found 0: none",
             "two.py": "exactly one subclass.*found 2: Mine, Other",
             "nope": "not a bundled name \\(fixed, fixed-lead, async\\)",
             "json:loads": "not a bundled name"}
    for spec, match in cases.items():
        with pytest.raises(ValueError, match=match):
            resolve(spec)
    with pytest.raises(ValueError, match="orchestration"):
        TeamAgent(tmp_path, harness="codex", orchestration="nope")


async def test_order_is_install_verify_tokens_launch(tmp_path):
    env = FakeEnv(2)
    agent = TeamAgent(tmp_path, harness="codex")
    await agent.setup(env)
    await agent.run("x", env, AgentContext())
    kinds = [e[0] for e in env.events]
    assert kinds.index("verify") > max(i for i, k in enumerate(kinds) if k == "install")
    assert kinds.index("verify") < min(i for i, k in enumerate(kinds) if k == "upload")
    assert max(i for i, k in enumerate(kinds) if k == "upload") < kinds.index("launch")
    install_calls = [c for c in env.calls if c["command"] == "install-cli"]
    assert sorted(c["service"] for c in install_calls) == ["agent-1", "agent-2"]  # in each agent's sandbox


async def test_unverified_network_fails_closed(tmp_path):
    env = FakeEnv(2)
    env.verified = False
    agent = TeamAgent(tmp_path, harness="codex")
    await agent.setup(env)
    with pytest.raises(RuntimeError, match="failing closed"):
        await agent.run("x", env, AgentContext())
    assert not any(e[0] in ("upload", "launch") for e in env.events)

    plain = FakeEnv(2)  # an environment that can't verify gets no tokens either
    plain.__class__ = type("NoVerify", (), {k: v for k, v in FakeEnv.__dict__.items()
                                            if k != "verify_agent_network"})
    seen = len(plain.events)
    with pytest.raises(RuntimeError, match="can't verify"):
        await agent.run("x", plain, AgentContext())
    assert not any(e[0] in ("upload", "launch") for e in plain.events[seen:])


async def test_harness_names_resolve_to_harbor_classes(monkeypatch):
    monkeypatch.undo()
    from harbor.agents.installed.claude_code import ClaudeCode
    from harbor.agents.installed.codex import Codex
    assert ta.installer_class("codex") is Codex and ta.installer_class("claude-code") is ClaudeCode
    assert ta.installer_class("gemini-cli").__name__ == "GeminiCli"
    with pytest.raises(ValueError, match="not one of Harbor's installed agents"):
        ta.installer_class("oracle")
    with pytest.raises(ValueError, match="only knows how to configure"):
        TeamAgent(Path("/tmp"), harness="gemini-cli")


class AsyncEnv(FakeEnv):
    """FakeEnv whose board admin calls reach a real Board and the subagents plugin's Pool, and
    whose agent-1 run plays a lead calling the lead tools."""

    def __init__(self, n, lead_script):
        from multi_agent_harness.board import Board
        from multi_agent_harness.board.subagents import Pool
        super().__init__(n)
        self.board = Board()
        self.board.limiter.rate = 0
        self.pool = Pool(self.board)
        self.board.subscribe(self.pool.on_event)
        self.lead_script, self.released, self.tokens, self.admin = lead_script, set(), [], []
        self.remote = {}

    async def service_exec(self, command, *, service=None, cwd=None, env=None, timeout_sec=None, user=None):
        if service is None and "curl" in command and "8790/api/admin/" in command:
            args = shlex.split(command.split("; ", 1)[1])
            url, body = args[-1], json.loads(args[args.index("-d") + 1]) if "-d" in args else None
            path = url.split("8790/api/admin/")[1]
            self.admin.append(path)
            self.calls.append({"service": None, "command": command, "env": {}})
            if path == "agents":
                out = {"agent": body["name"], "token": self.board.register_agent(body["name"], body["role"])}
                self.tokens.append(out["token"])
            elif path == "plugins":
                out = {"plugins": body["modules"]}
            elif path == "subagents/pool":
                self.pool.configure(body["lead"], body["slots"], body.get("resources"))
                out = body
            elif path == "subagents/lost":
                out = self.pool.lose(body["slot"])
            elif path == "subagents/actions":
                out = self.pool.take_actions()
                self.tokens += [a["token"] for a in out["actions"] if "token" in a]
            else:
                out = self.pool.finished(body["name"], body["text"], body["exit"])
            if "-o" in args:
                self.files[(None, args[args.index("-o") + 1])] = json.dumps(out)
                return ExecResult(stdout="", return_code=0)
            return ExecResult(stdout=json.dumps(out), return_code=0)
        if env and "MULTI_AGENT_HARNESS_PROMPT" in env:
            self.calls.append({"service": service, "command": command, "env": env})
            prompt = env["MULTI_AGENT_HARNESS_PROMPT"]
            if service == "agent-1":
                await self.lead_script(self)
                return ExecResult(stdout="", return_code=0)
            self.remote[(service, "last")] = "done: " + prompt.splitlines()[-1]
            if "STAY" in prompt:
                while service not in self.released:
                    await asyncio.sleep(0.01)
                return ExecResult(stdout="", return_code=143)
            return ExecResult(stdout="", return_code=0)
        if "kill -TERM" in command:
            self.released.add(service)
        return await super().service_exec(command, service=service, env=env, timeout_sec=timeout_sec)

    async def service_download_file(self, source_path, target_path, *, service=None):
        Path(target_path).write_text(self.remote.get((service, "last"), ""))


async def _until(cond, timeout=5):
    for _ in range(int(timeout / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never held")


async def test_async_subagents_spawn_deliver_wake_terminate_and_end_with_the_lead(tmp_path, monkeypatch):
    monkeypatch.setattr(ta, "TICK_SEC", 0.02)
    seen = {}

    async def lead(env):
        p = env.pool
        seen["a"] = p.spawn("agent-1", "write a line")            # into the one free sandbox
        seen["b"] = p.spawn("agent-1", "STAY and help")           # pool full: queued
        await _until(lambda: p.subs["agent-2"]["status"] == "idle")
        seen["inbox"] = await env.board.inbox("agent-1")
        env.board.send("agent-1", "one more line", to="agent-2")  # wakes it, resuming its session
        await _until(lambda: p.subs["agent-2"]["status"] == "working")
        await _until(lambda: p.subs["agent-2"]["status"] == "idle")
        p.terminate("agent-1", "agent-2")                          # its sandbox goes to agent-3
        await _until(lambda: "agent-2" in env.holder and env.holder["agent-2"] == "agent-3"
                     and "agent-2" in env.running)

    env = AsyncEnv(2, lead)
    agent = TeamAgent(tmp_path, harness="codex", orchestration="async")
    env.holder, env.running = agent.holder, set()
    real_launch = agent.launch

    def launch(name, prompt, **kw):
        real_launch(name, prompt, **kw)
        env.running.add(name)
    agent.launch = launch
    await agent.setup(env)
    assert env.admin[0] == "plugins" and env.admin[1:] == ["agents"]  # loaded before anyone registers
    assert set(env.board.agents()) == {"agent-1"}  # pool sandboxes have no board identity of their own
    ctx = AgentContext()
    await agent.run("SECRET TASK TEXT", env, ctx)

    assert seen["a"] == {"name": "agent-2", "status": "working"} and seen["b"] == {"name": "agent-3", "status": "queued"}
    [final] = seen["inbox"]
    assert final["from"] == "agent-2" and final["text"].endswith("\ndone: write a line")
    runs = [(c["service"], c["command"], c["env"]["MULTI_AGENT_HARNESS_PROMPT"]) for c in env.calls if "MULTI_AGENT_HARNESS_PROMPT" in c["env"]]
    assert "Task:\nSECRET TASK TEXT" in runs[0][2] and "spawn_agent(instructions)" in runs[0][2]
    subs = runs[1:]
    assert [s for s, _, _ in subs] == ["agent-2"] * 3
    first, wake, other = subs
    assert first[2].startswith("You are agent-2, a subagent. agent-1 leads") and "SECRET" not in first[2]
    assert first[2].endswith("Instructions from agent-1:\nwrite a line")
    assert "codex exec --dangerously" in first[1] and "/logs/agent/agent-2/codex.txt" in first[1]
    assert 'codex exec resume --dangerously' in wake[1] and '"$(cat /logs/agent/agent-2/session-id)"' in wake[1]
    assert "/logs/agent/agent-2/codex.2.txt" in wake[1] and wake[2].startswith("New message from agent-1:\none more line")
    assert other[2].startswith("You are agent-3, a subagent") and "SECRET" not in other[2]
    binds = [c["command"] for c in env.calls if c["service"] == "agent-2" and c["command"].startswith("set -e; umask 077; chmod 600 /run/multi-agent-harness/agent.env;")]
    assert len(binds) == 2 and "git clean -ffdxq" not in binds[0] and "git clean -ffdxq" in binds[1]
    assert 'rm -rf "$CODEX_HOME/sessions"' in binds[1]
    assert "MULTI_AGENT_HARNESS_AGENT_NAME=agent-3" in env.files[("agent-2", "/run/multi-agent-harness/agent.env")]

    m = ctx.metadata
    assert (m["orchestration"], m["lead"], m["end_reason"]) == ("async", "agent-1", "lead_finished")
    s = m["subagents"]
    assert (s["spawns"], s["starts"], s["wakes"], s["terminations"], s["final_responses"]) == (2, 2, 1, 1, 2)
    assert s["status"] == {"agent-2": "terminated", "agent-3": "working"}
    assert m["agents"]["agent-3"] == {**m["agents"]["agent-3"], "role": "subagent", "sandbox": "agent-2",
                                      "stopped": True, "exit": 143, "sessions": 1}
    assert m["agents"]["agent-2"]["sessions"] == 2 and m["agents"]["agent-1"]["role"] == "lead"
    assert all((tmp_path / a).is_dir() for a in ("agent-1", "agent-2", "agent-3"))
    for c in env.calls:  # subagent board tokens never in a command or exec env
        blob = c["command"] + json.dumps(c["env"])
        assert not any(t in blob for t in env.tokens)


async def test_async_turns_off_the_clis_own_subagents(tmp_path):
    for harness, orch, off in (("codex", "async", True), ("codex", "fixed", False), ("claude-code", "async", True)):
        env = FakeEnv(3)
        agent = TeamAgent(tmp_path / harness / orch, harness=harness, orchestration=orch)
        await agent.setup(env)
        await agent._lock_down(env)
        if harness == "codex":
            toml = tomllib.loads(env.files[("agent-1", "/run/multi-agent-harness/codex/config.toml")])
            assert (toml.get("features") == {"multi_agent": False}) is off
        else:
            assert "--disallowedTools Agent,Task " in agent._command()


def test_effort_goes_on_the_cli_command(tmp_path):
    claude = TeamAgent(tmp_path / "c", harness="claude-code", model="claude-opus-5-5", effort="max")
    assert "--model claude-opus-5-5 --effort max " in claude._command()
    codex = TeamAgent(tmp_path / "x", harness="codex", model="gpt-6-astra", effort="high")
    assert "-c model_reasoning_effort=high " in codex._command()
    codex = TeamAgent(tmp_path / "m", harness="codex", model="gpt-6-astra", effort="max")
    assert "-c model_reasoning_effort=max " in codex._command()
    assert "--effort" not in TeamAgent(tmp_path / "d", harness="claude-code")._command()
    with pytest.raises(ValueError, match="not an effort codex takes"):
        TeamAgent(tmp_path / "bad", harness="codex", effort="ultra")
    with pytest.raises(ValueError, match="antigravity"):
        TeamAgent(tmp_path / "agy", harness="antigravity", effort="high")


async def test_antigravity_config_and_launch(tmp_path):
    env = FakeEnv(2)  # hub headers, as on Daytona: model calls go through the forwarder
    agent = TeamAgent(tmp_path, harness="antigravity", orchestration="async")
    assert agent._model == "gemini-3.8-flash-low"
    await agent.setup(env)
    await agent._lock_down(env)
    installs = [c["command"] for c in env.calls if c["service"] == "agent-1" and "sha512sum -c" in c["command"]]
    assert len(installs) == 1 and "1.4.4-4783783166214144/linux-x64/cli_linux_x64.tar.gz" in installs[0]
    sh = env.files[("agent-1", "/run/multi-agent-harness/agent.env")]
    assert "GEMINI_API_KEY=PKEY-agent-1\n" in sh and f"MULTI_AGENT_HARNESS_GEMINI_UPSTREAM={HUB}/proxy/gemini\n" in sh
    assert """MULTI_AGENT_HARNESS_HUB_HEADER_NAMES='["X-Daytona-Preview-Token"]'""" in sh
    g = "/run/multi-agent-harness/gemini"
    settings = json.loads(env.files[("agent-1", f"{g}/antigravity-cli/settings.json")])
    assert settings["modelProvider"] == "gemini" and "customModelsConfig" not in settings
    assert json.loads(env.files[("agent-1", f"{g}/config/mcp_config.json")]) == {"mcpServers": {"board": {
        "serverUrl": f"{HUB}/mcp", "headers": {"Authorization": "Bearer BTOK-agent-1",
                                               "X-Daytona-Preview-Token": "PREVIEW-SECRET"}}}}
    assert "def do_POST" in env.files[("agent-1", "/run/multi-agent-harness/agy-forward.py")]
    agent_md = env.files[("agent-1", f"{g}/config/agents/teammate.md")]
    assert "subagent: false" in agent_md and "invoke_subagent" not in agent_md
    config = [c["command"] for c in env.calls if c["service"] == "agent-1"][-1]
    assert f'ln -s {g} "$HOME/.gemini"' in config and "command -v python3" in config
    cmd = agent._command()
    assert "python3 /run/multi-agent-harness/agy-forward.py /run/multi-agent-harness/agy-forward.port" in cmd
    assert 'export GOOGLE_GEMINI_BASE_URL="http://127.0.0.1:$(cat /run/multi-agent-harness/agy-forward.port)"' in cmd
    assert cmd.index("export GOOGLE_GEMINI_BASE_URL") < cmd.index("agy --prompt") < cmd.index("kill $fwd")
    assert ('agy --prompt="$MULTI_AGENT_HARNESS_PROMPT" --output-format stream-json --dangerously-skip-permissions '
            '--print-timeout 0s --add-dir "$PWD" --agent teammate --model gemini-3.8-flash-low </dev/null') in cmd
    assert "> /logs/agent/antigravity-stream.jsonl 2> /logs/agent/antigravity-cli.err" in cmd
    assert '--conversation "$(cat /logs/agent/session-id)" ' in agent._command(session=2, resume=True)
    assert_no_secret_in_exec(env)

    env = FakeEnv(1)  # no hub headers: straight to the proxy; a gateway alias is registered
    env.team_hub_headers = {}
    agent = TeamAgent(tmp_path / "h", harness="antigravity", model="gw-flash")
    await agent.setup(env)
    await agent._lock_down(env)
    settings = json.loads(env.files[("agent-1", f"{g}/antigravity-cli/settings.json")])
    assert settings["customModelsConfig"] == {"customModels": {"gw-flash": {"modelName": "gw-flash"}}}
    assert ("agent-1", "/run/multi-agent-harness/agy-forward.py") not in env.files
    assert "agy-forward" not in agent._command() and "--agent" not in agent._command()
    with pytest.raises(ValueError, match="pinned"):
        TeamAgent(tmp_path, harness="antigravity", cli_version="1.0.0")


async def test_antigravity_installs_with_harbors_agent(monkeypatch):
    monkeypatch.undo()
    assert ta.installer_class("antigravity").__name__ == "AntigravityCli"


CLAUDE_TOKEN = "sk-ant-oat01-FAKE-SUBSCRIPTION-TOKEN"
MANAGED = "/etc/claude-code/managed-settings.json"


def _transcript(name):
    """Two lines of one assistant message (one per content block) plus a second message."""
    usage = {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 5000,
             "cache_creation_input_tokens": 400}
    rows = [{"type": "assistant", "message": {"id": f"msg-{name}-1", "role": "assistant",
                                              "model": "claude-haiku-4-5", "usage": usage}}] * 2
    rows.append({"type": "assistant", "message": {"id": f"msg-{name}-2", "role": "assistant",
                                                  "model": "claude-haiku-4-5", "usage": usage}})
    return "".join(json.dumps(r) + "\n" for r in rows)


class SubscriptionEnv(FakeEnv):
    team_model_upstream = "claude-subscription"
    budget_usd = 5.0
    agent_budget_usd = None

    async def service_download_dir(self, source_dir, target_dir, *, service=None):
        await super().service_download_dir(source_dir, target_dir, service=service)
        d = target_dir / "sessions" / "projects" / "-app"
        d.mkdir(parents=True)
        (d / "s1.jsonl").write_text(_transcript(service))
        # what an agent that ran `env` would leave in its output, if the sandbox scrub missed it
        (target_dir / "claude-code.txt").write_text(f'{{"type":"result","leak":"{CLAUDE_TOKEN}"}}\n')


async def test_claude_subscription_token_goes_only_to_agents_and_usage_is_priced(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", CLAUDE_TOKEN)
    caplog.set_level(logging.DEBUG)
    env = SubscriptionEnv(2)
    agent = TeamAgent(tmp_path, model_name="claude-haiku-4-5", harness="claude-code")
    await agent.setup(env)
    ctx = AgentContext()
    await agent.run("Write team.txt.", env, ctx)

    sh = env.files[("agent-2", "/run/multi-agent-harness/agent.env")]
    assert f"CLAUDE_CODE_OAUTH_TOKEN={CLAUDE_TOKEN}\n" in sh and "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1" in sh
    assert "ANTHROPIC_BASE_URL" not in sh and "ANTHROPIC_API_KEY" not in sh and "ANTHROPIC_CUSTOM_HEADERS" not in sh
    assert all(CLAUDE_TOKEN not in v for (svc, _), v in env.files.items() if svc is None)  # nothing on the hub
    assert all(CLAUDE_TOKEN not in c["command"] + json.dumps(c["env"]) for c in env.calls)
    scrubs = [c for c in env.calls if "grep -rlF" in c["command"]]
    assert sorted(c["service"] for c in scrubs) == ["agent-1", "agent-2"]
    assert "budget cap" in caplog.text.lower() and CLAUDE_TOKEN not in caplog.text
    managed = json.loads(env.files[("agent-1", MANAGED)])  # the agent's own tools can't read the token
    deny = set(managed["permissions"]["deny"])
    assert {f"{t}(/{p})" for t in ("Read", "Edit") for p in (
        "/run/multi-agent-harness/agent.env", "/run/multi-agent-harness/git-credentials", "/proc/*/environ")} <= deny
    assert managed["sandbox"] == {"allowUnsandboxedCommands": False,
                                  "filesystem": {"denyRead": ["/run/multi-agent-harness/agent.env"]}}

    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert sum(CLAUDE_TOKEN in p.read_text() for p in files) == 0  # the host backstop redacted it

    # (1000*1 + 200*5 + 5000*0.1 + 400*1.25) / 1e6 per message, two messages per agent, two agents
    assert ctx.cost_usd == pytest.approx(0.012) and ctx.n_input_tokens == 4000 and ctx.n_output_tokens == 800
    assert ctx.metadata["billed_as"] == "claude-subscription" and ctx.metadata["budget_enforced"] is False
    assert ctx.metadata["cost_source"] == "transcripts"
    assert ctx.metadata["agents"]["agent-1"]["cost_usd"] == pytest.approx(0.006)
    usage = json.loads((tmp_path / "subscription-usage.json").read_text())
    assert usage["total"]["requests"] == 4 and usage["total"]["unpriced"] == {}


async def test_claude_env_scrub_can_be_turned_off_where_bubblewrap_cannot_run(tmp_path, monkeypatch, caplog):
    """MULTI_AGENT_HARNESS_CLAUDE_ENV_SCRUB=0: Claude Code without subprocess scrubbing (Docker hosts without
    unprivileged user namespaces, where Claude Code refuses to start with it), loudly; artifacts are still redacted."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", CLAUDE_TOKEN)
    monkeypatch.setenv("MULTI_AGENT_HARNESS_CLAUDE_ENV_SCRUB", "0")
    caplog.set_level(logging.DEBUG)
    env = SubscriptionEnv(2)
    agent = TeamAgent(tmp_path, model_name="claude-haiku-4-5", harness="claude-code")
    await agent.setup(env)
    await agent.run("Write team.txt.", env, AgentContext())
    sh = env.files[("agent-1", "/run/multi-agent-harness/agent.env")]
    assert "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=0" in sh and "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1" not in sh
    assert "can read the runner's subscription token" in caplog.text and CLAUDE_TOKEN not in caplog.text
    managed = json.loads(env.files[("agent-1", MANAGED)])
    assert "Read(//proc/*/environ)" in managed["permissions"]["deny"] and "sandbox" not in managed
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert sum(CLAUDE_TOKEN in p.read_text() for p in files) == 0  # the host backstop still redacts it


async def test_subscription_mismatches_fail_setup(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    with pytest.raises(ValueError, match="CLAUDE_CODE_OAUTH_TOKEN"):
        await TeamAgent(tmp_path, harness="claude-code").setup(SubscriptionEnv(1))
    with pytest.raises(ValueError, match="only Claude Code"):
        await TeamAgent(tmp_path, harness="codex").setup(SubscriptionEnv(1))
    codex_env = FakeEnv(1)
    codex_env.team_model_upstream = "subscription"
    with pytest.raises(ValueError, match="Codex ChatGPT logins"):
        await TeamAgent(tmp_path, harness="claude-code").setup(codex_env)
    await TeamAgent(tmp_path / "c", harness="codex").setup(codex_env)  # Codex on ChatGPT logins as before


async def test_time_reminders_schedule_resume_and_retry(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    team = TeamAgent(tmp_path, orchestration="fixed-lead", timeout_sec=200, time_reminders=19)
    team._original_timeout = 200
    team.roles = {"agent-1": "lead", "agent-2": "peer"}
    team.names = {"agent-1": "oak", "agent-2": "elm"}
    monkeypatch.setattr(TeamAgent, "running", property(lambda self: set(self.roles)))
    now = 0
    monkeypatch.setattr(team, "_elapsed", lambda: now)
    team.board_admin = AsyncMock()
    await team._time_reminders()
    team.board_admin.assert_not_called()
    for now in range(10, 200, 10):
        await team._time_reminders()
        await team._time_reminders()
    assert team.board_admin.await_count == 19
    assert all(c.args[2]["to"] == "oak" for c in team.board_admin.call_args_list)
    assert "0m10s of the run remain" in team.board_admin.call_args.args[2]["text"]
    now = 200
    await team._time_reminders()
    assert team.board_admin.await_count == 19

    team._reminder_slots.clear()
    team._elapsed_base = 100
    team.board_admin.reset_mock()
    now = 105
    await team._time_reminders()
    team.board_admin.assert_not_called()
    now = 145
    team.board_admin.side_effect = RuntimeError("hub unavailable")
    await team._time_reminders()
    assert not team._reminder_slots
    team.board_admin.side_effect = None
    await team._time_reminders()
    assert team._reminder_slots == {"agent-1": 14}
    assert "0m55s of the run remain" in team.board_admin.call_args.args[2]["text"]
    team.time_reminders = 0
    now = 190
    await team._time_reminders()
    assert team.board_admin.await_count == 2


@pytest.mark.parametrize("value", [-1, "1.5", "nan", True])
async def test_invalid_time_reminders(tmp_path, value):
    with pytest.raises(ValueError, match="time_reminders"):
        TeamAgent(tmp_path, time_reminders=value)


async def test_time_reminders_are_off_by_default(tmp_path):
    team = TeamAgent(tmp_path, timeout_sec=200)
    team._original_timeout = 200
    team.roles = {"agent-1": "lead", "agent-2": "peer"}
    assert team.time_reminders == 0 and not team._reminded("agent-1")


async def test_time_reminders_need_a_budget_and_reach_everyone_without_a_lead(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    team = TeamAgent(tmp_path, time_reminders=19)
    team.roles = {"agent-1": "peer", "agent-2": "peer"}
    team.names = {"agent-1": "oak", "agent-2": "elm"}
    monkeypatch.setattr(TeamAgent, "running", property(lambda self: set(self.roles)))
    monkeypatch.setattr(team, "_elapsed", lambda: 50)
    team.board_admin = AsyncMock()
    await team._time_reminders()
    team.board_admin.assert_not_called()  # no time budget, nothing to count down
    team._original_timeout = 100
    await team._time_reminders()
    assert sorted(c.args[2]["to"] for c in team.board_admin.call_args_list) == ["elm", "oak"]


def test_reminder_note_goes_after_the_identity_line():
    from multi_agent_harness.harbor.agent import REMINDER_NOTE, _after_identity

    out = _after_identity("You are oak, one of 4 agents.\nTask:\ndo it", REMINDER_NOTE)
    assert out.splitlines()[0] == "You are oak, one of 4 agents." and out.splitlines()[1] == REMINDER_NOTE
    assert out.endswith("Task:\ndo it")
