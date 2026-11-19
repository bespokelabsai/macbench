#!/bin/bash
# Build the single-agent task for plain Harbor: build-single/chicago-skyline-survey.
#   ./build_single.sh
# The task as it is, with two single-agent changes:
# - instruction.md leaves out the paragraph on the graded marks: a plain Harbor run is graded once, at the end;
# - the agent may reach the model APIs and nothing else (in a team run the harness routes model calls through its
#   hub, so task.toml keeps the agents offline).
set -euo pipefail
H=$(cd "$(dirname "$0")" && pwd); cd "$H"
OUT=$H/build-single/chicago-skyline-survey
rm -rf "$OUT"; mkdir -p "$OUT"
cp -R environment "$OUT/environment"
cp -R tests "$OUT/tests"
mkdir -p "$OUT/solution"; cp -R solution/solve.sh solution/plans "$OUT/solution/"
python3 - "$H" "$OUT" <<'P'
import re, sys
src, out = sys.argv[1], sys.argv[2]
s = open(f"{src}/instruction.md").read()
s, n = re.subn(r"(You have 4 hours\.)\n\nThe team's `main` [^\n]*\n", r"\1\n", s, count=1)
assert n == 1, "instruction.md: no graded-marks paragraph after 'You have 4 hours.'"
open(f"{out}/instruction.md", "w").write(s)
s = open(f"{src}/task.toml").read()
hosts = '["api.anthropic.com", "api.openai.com", "generativelanguage.googleapis.com"]'
s, n = re.subn(r'(\[agent\]\n(?:[^\[\n].*\n|\n)*?)network_mode = "no-network"\n',
               lambda m: m.group(1) + f'network_mode = "allowlist"\nallowed_hosts = {hosts}\n', s, count=1)
assert n == 1, "task.toml: no [agent] network_mode to open for the model APIs"
open(f"{out}/task.toml", "w").write(s)
P
find "$OUT" -name __pycache__ -prune -exec rm -rf {} +
echo "built build-single/chicago-skyline-survey ($(du -sh "$OUT" | cut -f1))"
