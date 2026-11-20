#!/bin/bash
set -uo pipefail
exec 2>&1
# The shared verifier runs on the hub (graded marks need it). It reads the game's own grading state, which the
# game host rewrites every 2 seconds; it never stops anything. A mark writes to the folder the harness gives it.
OUT=${MULTI_AGENT_HARNESS_GRADE_OUT:-/logs/verifier}
mkdir -p "$OUT"
chmod 755 "$OUT"
/usr/local/bin/python3 -I /tests/verify.py || echo "verifier failed; no reward written, so this grading counts as failed"
