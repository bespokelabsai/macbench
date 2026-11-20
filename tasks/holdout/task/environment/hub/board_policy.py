"""Board policy for Holdout: survivors talk only through the game (say, shout, radio, the stronghold noticeboard).

The harness's message board stays up, because the harness uses it, but an agent's `send` is refused with a short
pointer to the in-game channels. Reading the inbox and history still works (they stay empty).
Load it with --ek board_policy=<this file>.
"""
from multi_agent_harness.board.core import BoardError, Policy


class HoldoutPolicy(Policy):
    def authorize(self, board, agent, action, **details):
        if action == "send":
            raise BoardError("The board is closed in Holdout: your survivor talks through the game. Use the "
                             "holdout_act ops say, shout or radio, or post_note at the stronghold noticeboard.")
        return None
