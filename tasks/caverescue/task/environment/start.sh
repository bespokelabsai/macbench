#!/bin/bash
# Start the CAVE RESCUE service as root (root-only scenario, photos and ground truth). Idempotent.
STATE=/var/lib/caverescue
mkdir -p /run/caverescue && chmod 700 /run/caverescue; exec 9>/run/caverescue/lock; flock 9
ping() { python3 -c "import urllib.request as u;u.urlopen(u.Request('http://127.0.0.1:7788/api/ping',data=b'{}'),timeout=2)" 2>/dev/null; }
if ! ping; then
  [ -f /opt/caverescue/key ] && [ -d /opt/caverescue/scenarios ] || exit 1   # data not unpacked (harness agent box)
  mkdir -p "$STATE/export" && chmod 700 "$STATE"
  if ! grep -qxE 's[0-9]+' "$STATE/scenario" 2>/dev/null; then
    ls /opt/caverescue/scenarios | grep -xE 's[0-9]+' | shuf --random-source=/dev/urandom -n1 > "$STATE/scenario"; chmod 600 "$STATE/scenario"
  fi
  grep -qxE 's[0-9]+' "$STATE/scenario" || exit 1
  cd /opt/caverescue; umask 077
  setsid nohup /opt/caverescue/venv/bin/python -m crsim.server --data "/opt/caverescue/scenarios/$(cat $STATE/scenario)" \
      --port 7788 --speed "${CAVERESCUE_SPEED:-9.47}" --export "$STATE/export/state.json" --key /opt/caverescue/key \
      >> /var/log/caverescue.log 2>&1 < /dev/null 9>&- &
  for i in $(seq 1 120); do ping && break; sleep 1; done
fi
