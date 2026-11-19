#!/bin/bash
# Reference: there is no oracle for an open-ended search (the people's places are only in the photos).
# This exercises the pipeline: start the service, read the map and send drones into the first leads.
set -euo pipefail
cd /workdir
cr briefing >/dev/null
cr map -o map.json
python3 - <<'P'
import json, subprocess
m = json.load(open("map.json"))
for i, lead in enumerate(m["leads"][:6]):
    subprocess.run(["cr", "explore", f"D{i + 1}", lead["at"], lead["toward"]], stdout=subprocess.DEVNULL)
P
cr status >/dev/null
