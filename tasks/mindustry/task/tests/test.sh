#!/bin/bash
# Runs on the hub (shared verifier), at each graded mark and at the end. See grade.py.
set -u
OUT="${MULTI_AGENT_HARNESS_GRADE_OUT:-/logs/verifier}"
mkdir -p "$OUT"
# No reward is written before grading: a grader that fails leaves none, so the harness records a failed mark and Harbor a
# failed verifier, and never a graded 0.
rm -f "$OUT/reward.txt"
exec python3 -I -S /tests/run_verifier.py
