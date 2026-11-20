#!/bin/bash
# Root-only container entrypoint (installed as /usr/local/sbin/dxb-atc-entry, mode 0700). It runs as root when the
# container starts, before any agent command, and serves the live day from root-only /opt/dxb-atc on 127.0.0.1:8091.
# The day starts at the first call, or 10 minutes after the container starts. In a run of the reference solution
# (Harbor's oracle copies in /solution), the server's rule-based controller also works every sector no seat has
# open. The agent runs as an unprivileged user and can create nothing under /, so only the oracle can switch that on.
(
  umask 077
  [ -f /opt/dxb-atc/standalone.py ] || exit 0   # the harness's copy: the hub serves the day
  S=/var/lib/dxb-atc
  mkdir -p "$S" && chmod 700 "$S" && rm -f "$S/reference"
  (
    while :; do
      # Only Harbor can create /solution: the agent cannot create anything under /. Harbor's copy keeps the host
      # user's owner on Docker, so the owner is not checked.
      if [ -d /solution ] && [ ! -L /solution ] && [ -f /solution/solve.sh ] && [ ! -L /solution/solve.sh ]; then
        touch "$S/reference"; exit 0
      fi
      sleep 1
    done
  ) &
  cd /opt/dxb-atc || exit 0
  exec env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/root LANG=C.UTF-8 \
    python3 -E -s -u standalone.py --port 8091 --seats controller --speed 3 --auto-start 10 \
    --reference-if "$S/reference" >> /var/log/dxb-atc.log 2>&1 < /dev/null
) &
exec "$@"
