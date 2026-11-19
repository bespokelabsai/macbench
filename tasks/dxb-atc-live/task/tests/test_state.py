"""Diagnostic checks on the hub's record. The reward comes from grade.py alone: a continuous grade has no pass or fail,
so these go to checks.json and never change it."""
import json
import os
import unittest

import grade as G


class RecordChecks(unittest.TestCase):
    def setUp(self):
        p = os.path.join(G.STATE, "score.json")
        if not os.path.exists(p):
            self.skipTest("no day was played")
        with open(p) as f:
            self.rec = json.load(f)

    def test_grade_in_range(self):
        """R01-R03: the grade is a number in [0, 1] computed from flight outcomes and safety events."""
        g = G.grade(self.rec)["grade"]
        self.assertTrue(0.0 <= g <= 1.0)

    def test_outcomes_are_consistent(self):
        """Every completed flight has a completion time; no flight both landed and diverted."""
        for f in self.rec["flights"]:
            if f["outcome"] in ("landed", "departed"):
                self.assertIsNotNone(f["t_done"])
            self.assertIn(f["outcome"], (None, "landed", "departed", "diverted", "cancelled", "crashed"))
