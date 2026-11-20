#!/bin/bash
set -uo pipefail
exec 2>&1
# A graded mark (multi-agent-harness --ak grade_at) grades a checkout of main pinned at the mark, and writes to a
# folder of its own: the harness passes both. The final grading has neither, and grades /app into /logs/verifier.
OUT=${MULTI_AGENT_HARNESS_GRADE_OUT:-/logs/verifier}
export SEEDPROFIT_VERIFIER_OUT=$OUT
if [ -n "${MULTI_AGENT_HARNESS_GRADE_APP:-}" ]; then export SEEDPROFIT_APP=$MULTI_AGENT_HARNESS_GRADE_APP; fi
mkdir -p "$OUT"
# Team software runs on the hub before grading (the office's plan_review and test_market), and the harness leaves
# /logs/verifier writable, so anything already in the output folder is removed: a planted reward.json would win.
find "$OUT" -mindepth 1 ! -name test-stdout.txt -delete   # keep the log Harbor writes this script's output to
chmod 755 "$OUT"
printf '0\n' > "$OUT/reward.txt"
printf '{"reward": 0}\n' > "$OUT/reward.json"
# The team's software runs as unprivileged users; it must not read the tests or write the reward.
chmod 700 /tests
# The hidden simulator: the hub plugin (build/seedprofit_hub_plugin.py) unpacked it; if it did not run (plain
# Harbor), copy it from tests/hidden.
[ -f /opt/marketsim/marketsim/sim.py ] || { mkdir -p /opt/marketsim && cp -R /tests/hidden/marketsim/marketsim /opt/marketsim/; }
chmod 700 /opt/marketsim   # root only, as the hub plugin leaves it: team software must not import the simulator
PYTHONPATH=/opt/marketsim /usr/local/bin/python3 /tests/verify.py || echo "verifier failed; reward stays 0"
