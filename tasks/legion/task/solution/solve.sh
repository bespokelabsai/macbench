#!/bin/bash
# Reference solution. It installs a small original rush AI, graded like any
# submission, so the whole pipeline (static checks, copy detectors, every hidden
# match and its reproducibility re-run, the Elo fit) runs on an honest AI. It
# scores close to 0; there is no true oracle for this task (see the README).
set -euo pipefail
rm -rf /app/legion
mkdir -p /app/legion
cp "$(dirname "$0")/reference/main.js" /app/legion/main.js
cat > /app/AI.md <<'MD'
# Reference AI

A deliberately small rush AI in one module, `main.js`, exporting `LegionBot`.

Every simulated second it trains civilians from the civic centre until it has
eighteen, then trains citizen infantry in groups of three from the civic centre
and barracks. When the population is within eight of the limit it places a
house on a ring around the civic centre, letting the simulation reject bad
spots, and sends up to three civilians to finish any house foundation before
placing another. Idle workers and idle reserve soldiers gather food and wood
from the nearest supplies. Once fourteen soldiers are waiting and at least
sixty simulated seconds have passed since the last wave, all waiting soldiers
attack-move to the nearest enemy structure; raiders that go idle are sent to
the next nearest target.

It was tested with `legion.py match` and `legion.py gauntlet` against Petra at
every difficulty, with the reproducibility check enabled.
MD
