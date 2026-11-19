#!/bin/bash
# Build the single-agent Harbor task: build-single/caverescue (one container + a separate verifier).
#   ./build_single.sh
# Renders the photo library into build/s5 the first time.
# The agent runs as `agent`; the service runs as root and seals its op log to the root-only
# /var/lib/caverescue, which is collected as an artifact for the verifier. The verifier
# is the agent image without the launcher and without the photo library, and has no network.
set -euo pipefail
H=$(cd "$(dirname "$0")" && pwd); R=$H; cd "$R"   # this folder is the Harbor task and its sources
SCEN=("$R/build/s5")   # the task has one scenario, s5
# the photo library (285 MB) is rendered once, the first time (about an hour on 28 cores)
[ -f "$R/build/s5/views.json" ] || ${PYTHON:-python3} -m crsim.photos scenarios/s5.json build/s5 --procs "$(nproc)"
OUT=${CR_OUT:-$H/build-single/caverescue}
KEY=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
[ -f "$OUT/environment/caverescue/key" ] && KEY=$(cat "$OUT/environment/caverescue/key")   # stable across rebuilds
rm -rf "$OUT"; mkdir -p "$OUT"
E=$OUT/environment; mkdir -p "$E/caverescue/crsim" "$E/caverescue/scenarios"
cp "$H"/environment/{Dockerfile,start.c,entrypoint.sh,claude-managed-settings.json} "$E/"
cp "$H/environment/start.sh" "$E/caverescue/start.sh"
cp "$R"/crsim/{__init__,engine,world,server,grade}.py "$E/caverescue/crsim/"
printf '%s' "$KEY" > "$E/caverescue/key"
cp "$R/crsim/cli.py" "$E/cli.py"
# the agent owns its workdir here (the harness build keeps it root-owned and mounts its own)
sed -i 's|&& chown root:root /workdir|\&\& chown agent:agent /workdir|' "$E/Dockerfile"
grep -q 'chown agent:agent /workdir' "$E/Dockerfile"
for D in "${SCEN[@]}"; do
  N=$(basename "$D"); O=$E/caverescue/scenarios/$N; mkdir -p "$O"
  [ -f "$D/views.json" ] || { echo "no views.json in $D (render it first)"; exit 1; }
  python3 - "$R/scenarios/$N.json" "$O" <<'P'
import json, os, shutil, sys
sc = json.load(open(sys.argv[1])); out = sys.argv[2]
shutil.copy(sc["cave"], os.path.join(out, "cave.json")); sc["cave"] = "cave.json"
json.dump(sc, open(os.path.join(out, "scenario.json"), "w"))
P
  cp "$D/views.json" "$O/"; tar cf "$O/img.tar" -C "$D" img    # one file to upload; unpacked in the image
done
sed -i 's|^RUN python3 -m venv /opt/caverescue/venv|RUN for d in /opt/caverescue/scenarios/*/; do if [ -f "$d/img.tar" ]; then tar xf "$d/img.tar" -C "$d" \&\& rm "$d/img.tar"; fi; done \\\n \&\& python3 -m venv /opt/caverescue/venv|' "$E/Dockerfile"
grep -q 'img.tar' "$E/Dockerfile"
# task.toml plus what one container needs: a separate offline verifier that gets the root-only sealed state,
# and an unprivileged agent next to the service (the harness does this separation itself for team runs)
python3 - "$H/task.toml" "$OUT/task.toml" <<'P'
import re, sys
s = open(sys.argv[1]).read()
s = s.replace("[verifier]\n", '[verifier]\nenvironment_mode = "separate"\n', 1)
s = re.sub(r"(\[verifier\]\n(?:[^\[\n].*\n)*)", lambda m: m.group(1) + 'network_mode = "no-network"\n\n'
           '[[artifacts]]\nsource = "/var/lib/caverescue"\ndestination = "caverescue-state"\n', s, count=1)
s = re.sub(r"(\[agent\]\n(?:[^\[\n].*\n)*)", lambda m: m.group(1) + 'user = "agent"\n', s, count=1)
open(sys.argv[2], "w").write(s)
P
{ cat "$H/instruction.md"; printf '\n## Working as a team\n\nYou may split the work across subagents. Every `cr` talks to the same rescue: the drones, relays,\nchargers, kits, rescue teams and the clock are shared, so agree on the plan and split the work.\n'; } > "$OUT/instruction.md"
mkdir -p "$OUT/solution"; cp "$H/solution/solve.sh" "$OUT/solution/solve.sh"
# verifier: same image minus the launcher and the photos (it never needs them)
mkdir -p "$OUT/tests"
(cd "$E" && tar cf - --exclude='caverescue/scenarios/*/img.tar' .) | (cd "$OUT/tests" && tar xf -)
for N in "$OUT"/tests/caverescue/scenarios/*; do mkdir -p "$N/img"; done
cp "$H/tests/test_single.sh" "$OUT/tests/test.sh"
{ cat "$E/Dockerfile"; cat <<'D'

USER root
RUN rm -f /usr/local/bin/caverescue-start
ENTRYPOINT []
COPY test.sh /tests/test.sh
RUN chmod 700 /tests && chmod 0500 /tests/test.sh
WORKDIR /workdir
CMD ["bash"]
D
} > "$OUT/tests/Dockerfile"
find "$OUT" -name __pycache__ -prune -exec rm -rf {} +
echo "built $OUT ($(du -sh "$OUT" | cut -f1))"
