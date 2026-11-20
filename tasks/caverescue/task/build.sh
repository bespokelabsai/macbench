#!/bin/bash
# Build the CAVE RESCUE harness tasks: build-mah/caverescue-m{1,2,4,8}.
#   ./build.sh
# Renders the photo library into build/s5 the first time. Every agent box gets the size in task.toml,
# whatever k is; the k=1 build cannot spawn subagents.
set -euo pipefail
H=$(cd "$(dirname "$0")" && pwd); R=$H; cd "$R"   # this folder is the Harbor task and its sources
SCEN=("$R/build/s5")   # the task has one scenario, s5
# the photo library (285 MB) is rendered once, the first time (about an hour on 28 cores)
[ -f "$R/build/s5/views.json" ] || ${PYTHON:-python3} -m crsim.photos scenarios/s5.json build/s5 --procs "$(nproc)"
STAGE=$(mktemp -d); trap 'rm -rf "$STAGE"' EXIT
E=$STAGE/environment; mkdir -p "$E/caverescue/crsim" "$E/caverescue/scenarios"
cp "$H"/environment/{Dockerfile,start.c,entrypoint.sh,claude-managed-settings.json} "$E/"
cp "$H/environment/start.sh" "$E/caverescue/start.sh"
cp "$R"/crsim/{__init__,engine,world,server,grade}.py "$E/caverescue/crsim/"
head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$E/caverescue/key"
cp "$R/crsim/cli.py" "$E/cli.py"; python3 "$H/mah/patch_cli.py" "$E/cli.py"
for D in "${SCEN[@]}"; do
  N=$(basename "$D"); O=$E/caverescue/scenarios/$N; mkdir -p "$O"
  [ -f "$D/views.json" ] || { echo "no views.json in $D (render it first)"; exit 1; }
  python3 - "$R/scenarios/$N.json" "$O" <<'P'
import json, os, shutil, sys
sc = json.load(open(sys.argv[1])); out = sys.argv[2]
shutil.copy(sc["cave"], os.path.join(out, "cave.json")); sc["cave"] = "cave.json"
json.dump(sc, open(os.path.join(out, "scenario.json"), "w"))
P
  cp "$D/views.json" "$O/"; cp -r "$D/img" "$O/img"
done
# agents run as root in boxes built from this image: ship the scenario, photos and seal key encrypted;
# only the hub's board plugin (build-mah/cr_board.py, never in the image) carries the passphrase
PASS=$(head -c 32 /dev/urandom | base64 | tr -d '/+=\n')
tar czf - -C "$E/caverescue" scenarios key | CR_DATA_PASS=$PASS openssl enc -aes-256-cbc -pbkdf2 -salt -pass env:CR_DATA_PASS -out "$E/caverescue/data.enc"
rm -rf "$E/caverescue/scenarios" "$E/caverescue/key"
mkdir -p "$H/build-mah"; sed "s|__CR_DATA_PASS__|$PASS|" "$H/mah/cr_board.py" > "$H/build-mah/cr_board.py"
grep -q "$PASS" "$H/build-mah/cr_board.py"
for K in 1 2 4 8; do
  OUT=$H/build-mah/caverescue-m$K
  rm -rf "$OUT"; mkdir -p "$OUT/tests"; cp -a "$E" "$OUT/environment"
  cp "$H/tests/test.sh" "$OUT/tests/test.sh"; cp "$H/task.toml" "$OUT/task.toml"; cp "$H/instruction.md" "$OUT/instruction.md"
  cp -r "$H/solution" "$OUT/solution"
  python3 - "$OUT" "$K" <<'P'
import json, sys
out, k = sys.argv[1], int(sys.argv[2])
p = f"{out}/task.toml"; s = open(p).read()
s = s.replace('name = "bespokelabsai/caverescue"', f'name = "bespokelabsai/caverescue-m{k}"')
open(p, "w").write(s)
p = f"{out}/instruction.md"; s = open(p).read().rstrip() + "\n\n"
if k == 1:
    s += "## Working alone\n\nYou are the only controller on duty.\n"
    q = f"{out}/environment/claude-managed-settings.json"
    d = json.load(open(q)); d["permissions"]["deny"] = ["Agent", "Task"]; json.dump(d, open(q, "w"))
else:
    s += ("## Working as a team\n\n"
          f"Rescue control has {k} controllers: you (the lead) and {k - 1} subagent slots. Use all {k}: keep "
          f"{k - 1} subagents working for the whole episode rather than doing the work with fewer. Everyone's "
          "`cr` talks to the same rescue: the drones, relays, chargers, kits, rescue teams and the clock are "
          "shared, so agree on the plan and split the work.\n")
open(p, "w").write(s)
P
  echo "built $OUT ($(du -sh "$OUT" | cut -f1))"
done
