#!/bin/bash
# Verifier entry point, in the verifier's own sandbox (no network). Harbor has restored the
# team's tree at /app from the hub. Writes reward.txt, details.json, per_spectrum.jsonl and
# predict-log.txt. Follows the graded-marks contract when it is set:
#   MULTI_AGENT_HARNESS_GRADE_APP  tree to grade (default /app)
#   MULTI_AGENT_HARNESS_GRADE_OUT  where results go (default /logs/verifier)
set -uo pipefail
exec 2>&1
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
APP=${MULTI_AGENT_HARNESS_GRADE_APP:-/app}
OUT=${MULTI_AGENT_HARNESS_GRADE_OUT:-/logs/verifier}
mkdir -p "$OUT"
chown root:root "$OUT" && chmod 755 "$OUT"
printf '0\n' > "$OUT/reward.txt"
chmod 700 /tests
python3 -I /tests/verify.py --app "$APP" --out "$OUT" || true
echo "reward: $(cat "$OUT/reward.txt")"
