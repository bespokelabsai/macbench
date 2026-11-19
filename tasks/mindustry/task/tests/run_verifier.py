"""The verifier: write the reward from the hub's game state, then record the diagnostic checks next to it. Takes no input
from the team's repository."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import grade  # noqa: E402

grade.main()
out = os.environ.get("MULTI_AGENT_HARNESS_GRADE_OUT") or "/logs/verifier"
result = unittest.TextTestRunner(stream=open(os.devnull, "w")).run(unittest.defaultTestLoader.loadTestsFromName("test_state"))
with open(os.path.join(out, "checks.json"), "w") as f:
    json.dump({"ran": result.testsRun, "skipped": len(result.skipped), "failures": [str(t) for t, _ in result.failures + result.errors]}, f)
