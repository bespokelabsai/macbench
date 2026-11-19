#!/bin/bash
# Runs in the verifier's own sandbox, not on the hub. Harbor copies the hub's /logs/artifacts
# here, where the team left a git bundle of its repo and the board log under multi-agent-harness/.
A=${MULTI_AGENT_HARNESS_ARTIFACTS:-/logs/artifacts/multi-agent-harness}
OUT=${MULTI_AGENT_HARNESS_VERIFIER_DIR:-/logs/verifier}
mkdir -p "$OUT"
repo=$(mktemp -d)/team.git
if ! git clone -q --bare "$A/team.bundle" "$repo"; then
  echo "no team bundle at $A/team.bundle" >&2
  echo 0 > "$OUT/reward.txt"
  exit 0
fi
MULTI_AGENT_HARNESS_TEAM_GIT="$repo" BOARD_LOG="$A/board-events.jsonl" MULTI_AGENT_HARNESS_VERIFIER_DIR="$OUT" \
  python3 "$(dirname "$0")/verify.py" || echo 0 > "$OUT/reward.txt"
