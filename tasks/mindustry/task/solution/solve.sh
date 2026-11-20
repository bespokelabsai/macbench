#!/bin/bash
# Harbor oracle, for a backend without the harness (the game normally runs in a board plugin on the hub). It unpacks the
# hidden game from hub_bundle.tar.gz (written by environment/hub/make_hub_plugin.py from tests/hidden/), serves the API on loopback and plays the
# scripted reference bot as a team of ORACLE_N seats (default 8, one unit each, since a bot drives one unit) for ORACLE_SEC seconds. It leaves the grading state for tests/test.sh.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
[ -f "$HERE/hub_bundle.tar.gz" ] || { echo "solution/hub_bundle.tar.gz is missing: run python3 task/environment/hub/make_hub_plugin.py"; exit 1; }
# Harbor runs the oracle as the task's agent user, so the game unpacks and runs under /tmp/mmf; the grading state goes to
# /var/lib/mindustry-many-fronts (made for this user in environment/Dockerfile), where tests/test.sh reads it.
G=/tmp/mmf
mkdir -p "$G" && tar -xzf "$HERE/hub_bundle.tar.gz" -C "$G"
n=${ORACLE_N:-8}
python3 "$G/opt/mindustry-many-fronts/standalone.py" --work "$G/work" --logs "$G/logs" --port 8088 --seats "$(seq -f 'oracle%g' -s , 0 $((n - 1)))" > /tmp/mmf-host.log 2>&1 &
host=$!
export MULTI_AGENT_HARNESS_BOARD_URL=http://127.0.0.1:8088
bots=()
for i in $(seq 0 $((n - 1))); do
  MULTI_AGENT_HARNESS_BOARD_TOKEN="oracle$i" timeout "${ORACLE_SEC:-600}" python3 "$HERE/reference/bot.py" --index "$i" --n "$n" &
  bots+=($!)
done
wait "${bots[@]}" || true
kill "$host" 2>/dev/null || true
pkill -f /opt/mindustry/server.jar || true
