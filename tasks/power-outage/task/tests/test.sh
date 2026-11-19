#!/bin/bash
set -uo pipefail
exec 2>&1  # keep errors next to output in every runner
# The shared verifier runs on the hub, where the board, the model proxy and the team's git remote also run, so
# nothing here may stop other processes. A graded mark (multi-agent-harness --ak grade_at) grades a checkout of
# main pinned at the mark and writes to a folder of its own: the harness passes both. The final grading has
# neither, and grades /app into /logs/verifier.
OUT=${MULTI_AGENT_HARNESS_GRADE_OUT:-/logs/verifier}
mkdir -p "$OUT"
chmod 755 "$OUT"
printf '0\n' > "$OUT/reward.txt"
chmod 700 /tests
# The simulator is in the image, root-only; the hidden scenarios' salt is only here, in tests/hidden/salt.
[ -f /opt/blackstart/gridsim/sim.py ] || { echo "no simulator at /opt/blackstart; reward stays 0"; exit 0; }
chmod 700 /opt/blackstart
/usr/local/bin/python3 -I /tests/verify.py || echo "verifier failed; reward stays 0"

# After the final grading, on the hub: grade every earlier mark again in full (all 48 scenarios). During the run the
# marks are quick gradings, and each saved what it graded in /logs/marks/<label>/solution.tar.gz on the hub. The
# results go to /logs/verifier/marks/<label>/ (the trial's verifier/marks/), with a summary in
# /logs/verifier/marks/rewards.txt; the trial's reward stays the final grading's. BLACKSTART_FULL_MARKS_S (task.toml) caps
# the time for all of them, so the verifier ends before its own timeout (which would lose the final reward): a mark
# is skipped when less than 10 minutes are left, and one still grading at the cap is stopped and keeps reward 0.
if [ -z "${MULTI_AGENT_HARNESS_GRADE_MARK:-}" ] && [ -d /logs/marks ] && [ "${BLACKSTART_FULL_MARKS_S:-0}" -gt 0 ]; then
  start=$(date +%s)
  mkdir -p /logs/verifier/marks && chmod 755 /logs/verifier/marks
  : > /logs/verifier/marks/rewards.txt
  # in time order: h01 is 3600 s, m30 1800 s, s090 90 s
  for label in $(for d in /logs/marks/*/; do l=$(basename "$d"); n=${l:1}; n=$((10#${n%%.*}));
                 case $l in h*) n=$((n*3600));; m*) n=$((n*60));; esac; echo "$n $l"; done | sort -n | cut -d' ' -f2); do
    sol=/logs/marks/$label/solution.tar.gz
    [ -f "$sol" ] && grep -q '"kind": "quick"' "/logs/marks/$label/solution.json" 2>/dev/null || continue
    left=$(( BLACKSTART_FULL_MARKS_S - ($(date +%s) - start) ))
    if [ "$left" -lt 600 ]; then echo "full marks: $label skipped, ${left} s left" | tee -a /logs/verifier/marks/rewards.txt; continue; fi
    commit=$(sed -n 's/.*"commit": "\([0-9a-f]*\)".*/\1/p' "/logs/marks/$label/solution.json")
    work=$(mktemp -d)
    tar -xzf "$sol" -C "$work"
    out=/logs/verifier/marks/$label
    mkdir -p "$out" && chmod 755 "$out"
    printf '0\n' > "$out/reward.txt"
    env -u BLACKSTART_MARK_SCENARIOS MULTI_AGENT_HARNESS_GRADE_APP="$work/app" MULTI_AGENT_HARNESS_GRADE_OUT="$out" \
      MULTI_AGENT_HARNESS_GRADE_MARK="$label" MULTI_AGENT_HARNESS_GRADE_COMMIT="$commit" timeout -k 30 "$left" /usr/local/bin/python3 -I /tests/verify.py > "$out/test-stdout.txt" 2>&1 \
      || echo "verifier failed or ran out of time; reward stays 0" >> "$out/test-stdout.txt"
    rm -rf "$work"
    echo "full marks: $label $(cat "$out/reward.txt")" | tee -a /logs/verifier/marks/rewards.txt
  done
fi
