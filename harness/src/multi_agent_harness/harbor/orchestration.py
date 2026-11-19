"""Orchestration: who runs, with what prompt, and when the run ends.

TeamAgent owns the mechanics (sandboxes, credentials, launching and stopping CLIs, collecting
logs); a strategy only makes the decisions. A strategy is a .py file defining one subclass of
Orchestration. Pick one with --ak orchestration=<name or path>: a bundled name (`fixed`, the
default; `fixed-lead`; `async`) is shorthand for a file in src/multi_agent_harness/orchestrations/, anything else
is a path, relative to the working directory.

A strategy sees the team through TeamAgent:
  team.agents          every agent sandbox, from the environment's n_agents
  team.facing(name)    the name agents know a sandbox by (board, git, prompts); team.names maps them
  team.ordered(names, viewer)   names in the order sandbox `viewer` should see them
  team.roles           {name: role}, what roles() returned
  team.agent_resources per physical sandbox: requested resources and observed GPUs
  team.running         names whose CLI is still running
  team.exits           {name: exit code, or an exception's type name}
  team.preamble(name)  the standard team preamble for that agent, ending in "Task:\\n"
  team.launch(name, prompt, identity=None, board_token=None, resume=False)
                       start a CLI in a sandbox (from tick(), for sandboxes not running one); with
                       identity, as that board identity, and resume continues its last session
  await team.stop(name)       kill a sandbox's CLI; its logs are still collected
  await team.final_response(identity)   the last assistant message of its latest run
  await team.board_admin(method, path, body=None, secret=False)   a board admin route, from the hub
  team.workdir         the agents' working directory
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import pyfile

if TYPE_CHECKING:
    from .agent import TeamAgent

LEAD = "lead"
PEER = "peer"
SUBAGENT = "subagent"

BUNDLED_DIR = Path(__file__).resolve().parents[1] / "orchestrations"
BUNDLED = {"fixed": "fixed.py", "fixed-lead": "fixed_lead.py", "async": "async_subagents.py"}


class Orchestration:
    """Base strategy. Subclass it in a .py file and override roles, prompt and end_reason."""

    name = "base"
    # Board plugin modules the strategy's roles need (extra MCP tools, e.g. spawn/terminate/
    # status for a lead). Modules inside the harness package, which ships on the hub; TeamAgent loads
    # them into the running board at setup, before any agent registers. A plugin limits a tool to
    # a role with board.role(caller).
    board_plugins: tuple[str, ...] = ()
    # Role recorded for identities a strategy binds to sandboxes at launch.
    identity_role: str | None = None
    # Whether the CLI's own built-in subagents stay on (Codex's multi_agent feature).
    cli_subagents = True

    def roles(self, agents: list[str]) -> dict[str, str]:
        """Role of every agent sandbox. Registered on the board with the agent."""
        raise NotImplementedError

    def on_board(self, agents: list[str]) -> list[str]:
        """Sandboxes registered on the board under their own name at setup. The others are a pool
        the strategy binds to board identities at launch."""
        return agents

    def prompt(self, team: TeamAgent, name: str, instruction: str) -> str | None:
        """The agent's starting prompt, or None to leave it idle at start (tick() may launch it)."""
        raise NotImplementedError

    def end_reason(self, team: TeamAgent) -> str | None:
        """Why the run is over, or None while it isn't. TeamAgent stops whoever is still running."""
        raise NotImplementedError

    async def start(self, team: TeamAgent) -> None:
        """Called once, after the network check and before the first launch."""

    async def tick(self, team: TeamAgent) -> None:
        """Called every few seconds while agents run, and whenever one exits."""

    def on_lost(self, team: TeamAgent, name: str) -> None:
        """Called once when sandbox `name`'s run ended with an exception (e.g. the backend deleted
        the sandbox). It already counts as exited; a strategy may react, e.g. reassign its work."""

    def record(self, team: TeamAgent) -> dict[str, Any]:
        """Extra {"metadata": {...}, "ledger": {...}} for the agent metadata and sandbox ledger."""
        return {}


def _bundled() -> str:
    return ", ".join(BUNDLED)


def locate(spec: str | None) -> Path:
    """The strategy file for a bundled name or a path (relative to the working directory)."""
    spec = (spec or "fixed").strip()
    if spec in BUNDLED:
        return BUNDLED_DIR / BUNDLED[spec]
    if not spec.endswith(".py"):
        raise ValueError(f"orchestration {spec!r}: not a bundled name ({_bundled()}) or a path to a .py file")
    try:
        return pyfile.locate(spec, "orchestration")
    except ValueError as e:
        raise ValueError(f"{e}; bundled names are {_bundled()}") from None


def load(path: Path) -> type[Orchestration]:
    """The one Orchestration subclass the file defines."""
    module = pyfile.import_file(path, "orchestration")
    return pyfile.one_subclass(module, Orchestration, "multi_agent_harness.harbor.orchestration.Orchestration",
                               path, "orchestration")


def resolve(spec: str | None) -> Orchestration:
    """A bundled name or a path to a strategy file, as an instance of its strategy class."""
    return load(locate(spec))()
