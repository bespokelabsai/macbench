"""Make `cr` talk to the team's shared rescue simulation on the hub when running under multi-agent-harness."""
import sys
p = sys.argv[1]; s = open(p).read()
s = s.replace('''URL = os.environ.get("CAVERESCUE_URL", "http://127.0.0.1:7788/api/")
HEADERS = {"Content-Type": "application/json"}''', '''AGENT_ENV = "/run/multi-agent-harness/agent.env"


def _harness_env():
    """The hub URL, board token and hub access header, from the environment or the agent.env file
    (Claude Code's subprocess env scrubbing drops the token from tool shells)."""
    env = {k: v for k, v in os.environ.items() if k.startswith("MULTI_AGENT_HARNESS_")}
    if os.access(AGENT_ENV, os.R_OK):
        import shlex
        for line in open(AGENT_ENV):
            parts = shlex.split(line.removeprefix("export ").strip())
            if parts and "=" in parts[0] and parts[0].startswith("MULTI_AGENT_HARNESS_"):
                k, v = parts[0].split("=", 1)
                env.setdefault(k, v)
    return env


_H = _harness_env()
HUB = _H.get("MULTI_AGENT_HARNESS_BOARD_URL")
URL = os.environ.get("CAVERESCUE_URL") or (HUB.rstrip("/") + "/api/caverescue/" if HUB else "http://127.0.0.1:7788/api/")
HEADERS = {"Content-Type": "application/json"}
if HUB:
    HEADERS["Authorization"] = "Bearer " + _H.get("MULTI_AGENT_HARNESS_BOARD_TOKEN", "")
    if _H.get("MULTI_AGENT_HARNESS_HUB_HEADER_1"):
        HEADERS["X-Daytona-Preview-Token"] = _H["MULTI_AGENT_HARNESS_HUB_HEADER_1"]''')
s = s.replace("if _retry and os.path.exists(LAUNCHER):", "if _retry and not HUB and os.path.exists(LAUNCHER):")
assert "HUB" in s and "not HUB" in s, "patch did not apply"
open(p, "w").write(s)
