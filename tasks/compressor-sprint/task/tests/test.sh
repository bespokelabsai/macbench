#!/bin/bash
# Graded marks set MULTI_AGENT_HARNESS_GRADE_APP (the pinned checkout) and _GRADE_OUT; the final
# verifier sets neither and grades /app into /logs/verifier.
OUT=${MULTI_AGENT_HARNESS_GRADE_OUT:-/logs/verifier}
mkdir -p "$OUT"
rm -f "$OUT/reward.json"  # Harbor reads reward.json before reward.txt: only the grader may leave one
python3 "$(dirname "$0")/grade.py" > "$OUT/grade-stdout.txt" 2>&1 || { rm -f "$OUT/reward.json"; echo 0 > "$OUT/reward.txt"; }
cat "$OUT/grade-stdout.txt"
