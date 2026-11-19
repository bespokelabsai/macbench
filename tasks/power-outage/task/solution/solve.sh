#!/bin/bash
# Reference solution, not an optimum (the task is open-ended): it installs the task's reference controller
# (bottom-up restoration with a tie-line protocol, untuned) for all 16 areas, so the verifier grades a real
# submission end to end.
set -euo pipefail
SOL="$(cd "$(dirname "$0")" && pwd)"
mkdir -p /app/lib
cp "$SOL/reference/oracle.py" /app/lib/oracle.py
for i in $(seq -w 1 16); do
  printf '"""Area a%s: the reference controller in lib/oracle.py."""\nfrom lib.oracle import make_controller  # noqa: F401\n' "$i" \
    > "/app/areas/a$i/controller.py"
done
echo "reference controller installed in all 16 areas"
