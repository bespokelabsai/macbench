#!/bin/bash
# CAVE RESCUE verifier (separate environment). Replays the HMAC-sealed op log written by the root-only
# service. The scenario is the one named inside the signed state.
set -uo pipefail
OUT=/logs/verifier; export OUT
mkdir -p "$OUT"
# only the root-only copy counts (Harbor restores the artifact at its source path); never agent-writable paths
SEALED=""; [ -f /var/lib/caverescue/export/state.json ] && SEALED=/var/lib/caverescue/export/state.json
echo "[verifier] sealed=${SEALED:-<none>}"
if [ -z "$SEALED" ]; then
  echo "[verifier] no sealed state: the agent never used the service"
  echo 0 > $OUT/reward.txt; exit 0
fi
cp "$SEALED" "$OUT/state.json"
cd /opt/caverescue
/opt/caverescue/venv/bin/python -m crsim.grade --sealed "$SEALED" --scenarios /opt/caverescue/scenarios --key /opt/caverescue/key > $OUT/grade.json 2>$OUT/grade.err
rc=$?
tail -5 $OUT/grade.err
if [ $rc -ne 0 ]; then
  if [ $rc -eq 2 ]; then echo "[verifier] invalid sealed state: $(tail -1 $OUT/grade.err)"; echo 0 > $OUT/reward.txt; exit 0; fi
  echo "[verifier] grader crashed rc=$rc"; exit 1
fi
/opt/caverescue/venv/bin/python - <<'P'
import json, os
r = json.loads(open(os.environ["OUT"] + "/grade.json").read().strip().splitlines()[-1])
s = max(0.0, min(1.0, float(r["score"])))
print(f"[verifier] score={s:.4f} found={r['found']} kits={r['kits']} reached={r['reached']} dead={r['dead']} "
      f"false_reports={r['false_reports']} drones_lost={r['drones_lost']} ops={r['ops']} sealed_at_h={r['sealed_at_h']}")
open(os.environ["OUT"] + "/reward.txt", "w").write(f"{s:.6f}\n")
P
