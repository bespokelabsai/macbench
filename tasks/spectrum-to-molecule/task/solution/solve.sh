#!/bin/bash
# Reference solution: installs reference/ms2ref.py, a simple library-matching ranker, as ./predict
# in the repo and builds its one file (masses.npy, 36 MB). It scores 0.133 hit@1; there is no 1.0 oracle.
#   solve.sh            library match within the stated precursor tolerance (default)
#   solve.sh random     random order within the same tolerance (calibration floor)
set -euo pipefail
APP=${APP:-/app}
METHOD=${1:-library}
SRC=$(cd "$(dirname "$0")" && pwd)

mkdir -p "$APP/ref"
cp "$SRC/reference/ms2ref.py" "$APP/ref/ms2ref.py"
cat > "$APP/predict" <<PRED
#!/bin/sh
exec python3 -I "\$(dirname "\$0")/ref/ms2ref.py" --method $METHOD "\$@"
PRED
chmod +x "$APP/predict"
python3 -I "$APP/ref/ms2ref.py" --build
echo "installed ./predict ($METHOD)"
