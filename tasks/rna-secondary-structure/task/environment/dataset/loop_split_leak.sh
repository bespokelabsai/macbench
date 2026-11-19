#!/usr/bin/env bash
# Steps 4 and 5 until the leakage check adds no more moves.
set -euo pipefail
cd "$(dirname "$0")"
for it in 1 2 3 4 5 6 7 8; do
  ./drun.sh python 04_split.py
  set +e; ./drun.sh python 05_leakage.py; rc=$?; set -e
  [ $rc -eq 0 ] && { echo "split final after $it leakage passes"; exit 0; }
  [ $rc -ne 3 ] && exit $rc
  echo "leakage moves added; iteration $it done, re-splitting"
done
echo "did not converge"; exit 1
