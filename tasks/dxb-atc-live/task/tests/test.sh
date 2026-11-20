#!/bin/bash
# Runs as root on the hub at each graded mark, or in a verifier container of its own. See grade.py.
set -u
OUT="${MULTI_AGENT_HARNESS_GRADE_OUT:-/logs/verifier}"
mkdir -p "$OUT"
# No reward is written before grading: a grader that fails leaves none, so the harness records a failed mark and Harbor a
# failed verifier, and never a graded 0.
rm -f "$OUT/reward.txt"
exec python3 -I -S /tests/run_verifier.py
