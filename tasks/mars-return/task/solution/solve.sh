#!/bin/bash
# Reference solution. There is no true oracle for this open-ended task: this submits the reference vehicle
# (solution/reference: sysml/vehicle.sysml, and the flight software guidance.py + gnc/) honestly, as an agent would,
# and checks the pipeline end to end. With the `program` command (where a program office is reachable) it also goes through the
# program office: a design review, the cost report, a static fire of S1 and a drop test of the Mars parachute (both
# run at once, about 15 real minutes), with their results saved to program/test_results.json exactly as the office
# gave them. The reference is an honest submission, not a ceiling: it scores what its design earns.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
cd /app
rm -f design/*.sysml
cp "$HERE/reference/sysml/vehicle.sysml" design/vehicle.sysml
rm -rf flight && mkdir -p flight
cp "$HERE/reference/guidance.py" flight/guidance.py
cp -r "$HERE/reference/gnc" flight/gnc
design-check
command -v program >/dev/null || { echo "no program office CLI here: design and flight software submitted"; exit 0; }
program design_review | python3 -c "import json,sys; r=json.load(sys.stdin); print('design_review ok:', r['ok']); sys.exit(0 if r['ok'] else 1)"
program cost_report | python3 -c "import json,sys; r=json.load(sys.stdin); print('hardware', r['hardware_usd'], 'over budget', r['over_budget'])"

order() {  # kind target params -> test id
  program order_test "$1" "$2" --params "$3" --note "reference test plan" | python3 -c "import json,sys; print(json.load(sys.stdin)['test'])"
}
ids="$(order stage_static_fire vehicle.S1 '{"burn_s": 30}') $(order parachute_drop vehicle.CHUTE '{}')"
echo "ordered: $ids"
# Wait for both (the stage stand takes 12 minutes, a disk-gap-band drop 15), at most 40 minutes.
for i in $(seq 1 240); do
  pending=0
  for t in $ids; do
    s=$(program test_result "$t" | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])")
    [ "$s" = done ] || [ "$s" = cancelled ] || pending=1
  done
  [ $pending = 0 ] && break
  sleep 10
done
python3 - $ids <<'PY'
import json, subprocess, sys
results = []
for t in sys.argv[1:]:
    r = json.loads(subprocess.run(["program", "test_result", t], capture_output=True, text=True, check=True).stdout)
    print(t, r["status"], (r.get("result") or {}).get("summary") if isinstance(r.get("result"), dict) else r.get("result"))
    if r["status"] == "done" and isinstance(r.get("result"), dict):
        results.append(r["result"])
with open("/app/program/test_results.json", "w") as f:
    json.dump(results, f, indent=1)
PY
program program_status | python3 -c "import json,sys; r=json.load(sys.stdin); print('R&D committed', r['rnd_committed_usd'], 'left', r['rnd_left_usd'])"
