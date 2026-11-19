#!/usr/bin/env bash
# Keep every checkpoint of a team run. The harness writes a checkpoint every --ak checkpoint_every_min into
# <job dir>.checkpoints/<trial>/ but keeps only the last few, so start this next to `harbor run` (same job dir)
# and it copies each one into <job dir>.ckpt-archive/<trial>/ as soon as it appears. grade_checkpoints.sh reads them.
#   task/tests/checkpoints/archive_checkpoints.sh jobs/<job name> &
# It exits by itself once the run has been graded (a trial's verifier/reward.txt exists), or after
# IDLE_EXIT_SEC (default 3 h) without a new checkpoint.
set -euo pipefail
[ $# -eq 1 ] || { sed -n 2,7p "$0"; exit 2; }
JOB=${1%/}
SRC="$JOB.checkpoints"; DST="$JOB.ckpt-archive"
IDLE_EXIT_SEC=${IDLE_EXIT_SEC:-10800}
last=$(date +%s)
while :; do
  for d in "$SRC"/*/*/; do
    [ -f "$d/manifest.json" ] || continue
    n=$(basename "$d"); t=$(basename "$(dirname "$d")")
    if [ ! -e "$DST/$t/$n" ]; then
      mkdir -p "$DST/$t"
      cp -a "$d" "$DST/$t/$n.tmp" && mv "$DST/$t/$n.tmp" "$DST/$t/$n" && echo "archived $t/$n"
      last=$(date +%s)
    fi
  done
  if compgen -G "$JOB/*__*/verifier/reward.txt" >/dev/null; then exit 0; fi
  if [ $(( $(date +%s) - last )) -gt "$IDLE_EXIT_SEC" ]; then exit 0; fi
  sleep 20
done
