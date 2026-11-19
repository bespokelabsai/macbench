#!/bin/bash
# Build the team task for multi-agent-harness: build-mah/chicago-skyline-survey (any team size).
#   ./build.sh
# The run copy holds the task as it is, minus the reference plans (solution/) and the build and orchestration
# files, as in the published runs. On the Docker backend the agents reach the hub (board, model proxy, team
# repository) over the network, so the copy opens the agent and verifier network ("public"), as the published
# Docker runs did.
set -euo pipefail
H=$(cd "$(dirname "$0")" && pwd); cd "$H"
OUT=$H/build-mah/chicago-skyline-survey
rm -rf "$OUT"; mkdir -p "$OUT"
cp instruction.md "$OUT/instruction.md"
cp -R environment "$OUT/environment"
cp -R tests "$OUT/tests"
sed 's/^network_mode = "no-network"$/network_mode = "public"/' task.toml > "$OUT/task.toml"
if grep -q 'no-network' "$OUT/task.toml"; then echo "task.toml: a network_mode was not opened"; exit 1; fi
find "$OUT" -name __pycache__ -prune -exec rm -rf {} +
echo "built build-mah/chicago-skyline-survey ($(du -sh "$OUT" | cut -f1))"
