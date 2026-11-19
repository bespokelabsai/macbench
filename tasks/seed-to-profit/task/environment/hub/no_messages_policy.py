"""Board policy for a comms-off run (--ek board_policy=environment/hub/no_messages_policy.py): the board refuses every message.

The prompt is unchanged, so agents still see the board. Every send is refused and logged as a `refused` event in
board-events.jsonl, so the run shows how often the team tried to talk. Inboxes and history stay empty. The git
repository and the shared company stay open: the graded software is what the team pushes to main.
"""

from typing import Any

from multi_agent_harness.board import Board, BoardError, Policy


class NoMessagesPolicy(Policy):
    def authorize(self, board: Board, agent: str, action: str, **details: Any) -> None:
        if action == "send":
            raise BoardError("messaging is off in this run: the board refuses every message")
