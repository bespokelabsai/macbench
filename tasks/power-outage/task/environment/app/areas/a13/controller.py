"""Controller for control area a13.

The simulator runs this file in a process of its own, as its own user, once per scenario. It sees only what the
a13 control room sees. make_controller() gets the public grid map, this area's private data and its name; step()
is called every 30 simulated seconds with this area's observation and the messages sent to it, and returns this
area's commands and its outgoing messages. MANUAL.md has the observation, the commands and the rules.

Shared code can live in lib/ (import lib.something); the repository root is on sys.path.
"""


def make_controller(area, grid, private):
    return Controller(area, grid, private)


class Controller:
    def __init__(self, area, grid, private):
        self.area = area

    def step(self, obs, inbox):
        actions = []    # e.g. {"op": "start", "unit": "G001"}
        outbox = []     # e.g. {"to": "a02", "text": "..."}
        return actions, outbox
