#!/bin/bash
# Reference solution: installs the greedy-stem baseline (reference/rnaref.py, standard library only) as ./predict in
# the repo (APP, default /app). An honest baseline, not an oracle: it scores 0.329 (there is no 1.0 oracle).
set -euo pipefail
APP=${APP:-/app}
SRC=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$APP/ref"
cp "$SRC/reference/rnaref.py" "$APP/ref/rnaref.py"
cat > "$APP/predict" <<'EOS'
#!/bin/sh
exec python3 -I "$(dirname "$0")/ref/rnaref.py" "$@"
EOS
chmod +x "$APP/predict"
echo "installed ./predict (greedy stems)"
