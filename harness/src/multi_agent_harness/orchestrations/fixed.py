"""`fixed`: N peers, identical tools, all see the full task; the run ends when all finish.

Copy this file to start your own strategy and pass its path: --ak orchestration=./my_strategy.py.
The interface is multi_agent_harness.harbor.orchestration.Orchestration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from multi_agent_harness.harbor.orchestration import PEER, Orchestration

if TYPE_CHECKING:
    from multi_agent_harness.harbor.agent import TeamAgent


class Fixed(Orchestration):
    name = "fixed"

    def roles(self, agents: list[str]) -> dict[str, str]:
        return {a: PEER for a in agents}

    def prompt(self, team: TeamAgent, name: str, instruction: str) -> str | None:
        return team.preamble(name) + instruction

    def end_reason(self, team: TeamAgent) -> str | None:
        return None if team.running else "all_finished"
