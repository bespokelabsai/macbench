#!/bin/bash
set -uo pipefail
exec 2>&1
# A graded mark (multi-agent-harness --ak grade_at) grades a checkout of main pinned at the mark, and writes to a
# folder of its own: the harness passes both. The final grading has neither, and grades /app into /logs/verifier.
OUT=${MULTI_AGENT_HARNESS_GRADE_OUT:-/logs/verifier}
export MARSFLIGHT_VERIFIER_OUT=$OUT
if [ -n "${MULTI_AGENT_HARNESS_GRADE_APP:-}" ]; then export MARSFLIGHT_APP=$MULTI_AGENT_HARNESS_GRADE_APP; fi
mkdir -p "$OUT"
chmod 755 "$OUT"
printf '0\n' > "$OUT/reward.txt"
# The flight software runs as the unprivileged user "flight"; it must not read the tests or write the reward.
chmod 700 /tests
# The hidden simulator and compiler come with the verifier (tests/hidden/); on the hub the plugin may have put them in /opt
# already. Readable by all, writable by root: the flight user imports the runner.
[ -f /opt/marsflight/marsflight/sim.py ] || { mkdir -p /opt/marsflight && cp -r /tests/hidden/marsflight /opt/marsflight/marsflight; }
[ -d /opt/designlang/designlang/hidden ] || cp -r /tests/hidden/designlang /opt/designlang/designlang/hidden
chmod -R a+rX,go-w /opt/marsflight /opt/designlang/designlang/hidden
# How much of the starting repo the team changed (tests/start_manifest.json, from tests/activity.py --write): a check that the
# agents acted, printed before grading. It does not change the score.
/opt/conda/envs/marsflight/bin/python3 -I /tests/activity.py "${MARSFLIGHT_APP:-/app}" /tests/start_manifest.json || true
# The 48 fixed hidden worlds of the published scores. MARSFLIGHT_VERIFY_SEEDS (default 48) flies only the first N of
# them, for quick local checks.
export MARSFLIGHT_VERIFY_SEED_FILE=${MARSFLIGHT_VERIFY_SEED_FILE:-/tests/seeds.json}
PYTHONPATH=/opt/marsflight:/opt/designlang MARSFLIGHT_KERNELS=/opt/tudat/.tudat/resource/spice_kernels \
  /opt/conda/envs/marsflight/bin/python3 /tests/verify.py || echo "verifier failed; reward stays 0"
echo "[verifier] reward=$(cat "$OUT/reward.txt")"
