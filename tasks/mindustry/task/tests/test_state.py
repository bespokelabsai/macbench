"""Diagnostic checks on the hub's grading state, in the unittest layout. The reward comes
from grade.py alone: a continuous grade has no pass or fail, so these go to checks.json and never change it."""
import json
import os
import unittest

import grade as G


def load():
    with open(G.STATE) as f:
        return json.load(f)


class StateChecks(unittest.TestCase):
    def setUp(self):
        if not os.path.exists(G.STATE):
            self.skipTest("no game was played")
        self.state = load()

    def test_grade_comes_from_native_counters(self):
        """Scoring: the grade is a function of per-world counters, each bounded by source and core_in."""
        self.assertAlmostEqual(G.score(self.state), sum(G.multiplier(w["losses_before"]) * G.world_points(w) for w in self.state["worlds"]))
        self.assertTrue(0.0 <= G.grade(G.score(self.state)) <= 1.0)

    def test_every_lost_core_is_banked_and_penalised(self):
        """Losing the core: each world records the losses before it, and the count of lost worlds matches."""
        lost = 0
        for w in self.state["worlds"]:
            self.assertEqual(w["losses_before"], lost)
            lost += w["ended"] in ("lost", "crash")
        self.assertEqual(self.state["losses"], lost)
