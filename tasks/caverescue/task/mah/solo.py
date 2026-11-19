"""Orchestration for k = 1 team runs (`--ak orchestration=mah/solo.py`): like `fixed`, but the CLI's own subagents are off (Codex multi_agent, Claude Code's Agent tool), so a
k=1 run is one agent for every harness."""

from __future__ import annotations

from typing import TYPE_CHECKING

from multi_agent_harness.harbor.orchestration import PEER, Orchestration

if TYPE_CHECKING:
    from multi_agent_harness.harbor.agent import TeamAgent


class Solo(Orchestration):
    name = "solo"
    cli_subagents = False

    def roles(self, agents: list[str]) -> dict[str, str]:
        return {a: PEER for a in agents}

    def prompt(self, team: TeamAgent, name: str, instruction: str) -> str | None:
        return team.preamble(name) + instruction

    def end_reason(self, team: TeamAgent) -> str | None:
        return None if team.running else "all_finished"
