"""Example Policy: leads may send anywhere, workers may send only to leads.

Run with --ek board_policy=examples/policies/hierarchy.py (a path, relative to the working directory).
A lead is any agent whose name starts with "lead". A worker's broadcast is refused.
"""

from typing import Any

from multi_agent_harness.board import Board, BoardError, Policy


def is_lead(name: str) -> bool:
    return name.startswith("lead")


class HierarchyPolicy(Policy):
    def authorize(self, board: Board, agent: str, action: str, **details: Any) -> None:
        if action != "send" or is_lead(agent):
            return
        if details.get("to") is None:
            raise BoardError("workers may not broadcast; send to a lead with to=...")
        for name in details["recipients"]:
            if not is_lead(name):
                raise BoardError(f"workers may only message leads, not {name!r}")
