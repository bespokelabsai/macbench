"""`fixed-lead`: the first slot (agent-1) leads; the run ends when it finishes and the others are stopped."""

from __future__ import annotations

from typing import TYPE_CHECKING

from multi_agent_harness.harbor.orchestration import LEAD, PEER, Orchestration

if TYPE_CHECKING:
    from multi_agent_harness.harbor.agent import TeamAgent


class FixedLead(Orchestration):
    name = "fixed-lead"

    def roles(self, agents: list[str]) -> dict[str, str]:
        return {a: LEAD if i == 0 else PEER for i, a in enumerate(agents)}

    def prompt(self, team: TeamAgent, name: str, instruction: str) -> str | None:
        lead = next(a for a, r in team.roles.items() if r == LEAD)
        if name == lead:
            line = ("You are the team lead. The run ends when you finish: your teammates are stopped "
                    "then and main is graded as it stands, so finish only once the work is on main.")
        else:
            line = (f"{team.facing(lead)} is the team lead. The run ends when {team.facing(lead)} finishes, and anything not "
                    "pushed to main by then is lost.")
        return team.preamble(name).replace("\nTask:\n", f"{line}\n\nTask:\n") + instruction

    def end_reason(self, team: TeamAgent) -> str | None:
        lead = next(a for a, r in team.roles.items() if r == LEAD)
        if lead in team.running:
            return None
        return "lead_finished" if team.running else "all_finished"
