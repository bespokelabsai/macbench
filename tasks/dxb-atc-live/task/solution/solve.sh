#!/bin/bash
# Harbor oracle on a backend without the harness (on the harness the hub serves the live day, behind
# environment/hub/plugins/atc_plugin.py). The image's root-only entrypoint serves the hidden day on 127.0.0.1:8091 and,
# because Harbor copied this folder in as /solution, adds its rule-based controller (environment/hub/server/standin.py) for
# every sector no seat has open. This script runs as the unprivileged agent user and plays the reference controller's
# FLOW seat through the API (bot.py) for ORACLE_SEC seconds (default 1800: 90 minutes of the day, past the grader's
# 30-minute lag). The task is open-ended: the reference is a slow, cautious controller, not a perfect day.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
sleep 5   # the entrypoint checks for this folder every second; the day must not start before it has
up=""
for _ in $(seq 300); do
  (echo > /dev/tcp/127.0.0.1/8091) 2>/dev/null && { up=1; break; }
  sleep 2
done
[ -n "$up" ] || { echo "the live day's server is not listening"; exit 1; }
timeout "${ORACLE_SEC:-1800}" python3 "$HERE/bot.py" --index 0 || true
sleep 70   # the server saves the record every airport minute
