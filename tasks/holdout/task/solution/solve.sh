#!/bin/bash
# Reference solution, for plain Harbor, which has no hub (in a team run the game runs in a board plugin on the hub).
# It unpacks the hidden game from hub_bundle.tar.gz (written by environment/hub/make_hub_plugin.py from tests/hidden/),
# serves the game API on loopback for one survivor, and plays the scripted reference bot (reference/bot.py) for
# ORACLE_SEC real seconds (default 900, one game day), after which the game freezes the world. tests/test.sh then
# reads the grading state the game leaves in /var/lib/holdout. The bot is an honest, simple player and scores close to
# 0; there is no true oracle for this task (see the README).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
[ -f "$HERE/hub_bundle.tar.gz" ] || { echo "solution/hub_bundle.tar.gz is missing: run python3 task/environment/hub/make_hub_plugin.py"; exit 1; }
G=/tmp/holdout-oracle
SEC=${ORACLE_SEC:-900}
mkdir -p "$G" && tar -xzf "$HERE/hub_bundle.tar.gz" -C "$G"
python3 "$G/opt/holdout/standalone.py" --port 8088 --token oracle --state /var/lib/holdout --stop-after "$SEC" \
  > /tmp/holdout-host.log 2>&1 &
host=$!
export MULTI_AGENT_HARNESS_BOARD_URL=http://127.0.0.1:8088 MULTI_AGENT_HARNESS_BOARD_TOKEN=oracle
for _ in $(seq 60); do python3 /app/tools/holdout.py status > /dev/null 2>&1 && break; sleep 1; done
# The bot stops when the world freezes; the timeout is only a guard.
timeout $((SEC + 120)) python3 "$HERE/reference/bot.py" --index 0 --n 1 || true
sleep 5   # the game rewrites its grading state every 2 s: let it record the frozen world
kill "$host" 2> /dev/null || true
