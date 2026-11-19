#!/usr/bin/env bash
# End-to-end rebuild of the dataset (1,000 hidden + 300 dev molecules) from the raw MassSpecGym download, then a
# check of the five task files against the sha256 in SOURCE (the files the published runs used).
#
#   ./download.sh && ./run_all.sh
#
# Needs Docker and ./download.sh's files. Every step runs in the capped runner container (drun.sh: 20 GB memory,
# 10 CPUs, at most 8 workers); about 15 minutes on a 32-core machine. Writes, under build/ next to the task folder
# (or $MS2_BUILD_DIR):
# agent/ and grader/ (the dataset, ~230 MB), dev/ (a dev split, not used by the task), work/ (intermediates,
# ~0.8 GB) and logs/.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
BUILD=${MS2_BUILD_DIR:-$HERE/../../../build}   # build/ next to the task folder
cd "$HERE"
[ -f "$BUILD/massspecgym/data/MassSpecGym1.5.tsv" ] || { echo "no MassSpecGym files in $BUILD/massspecgym: run ./download.sh first" >&2; exit 1; }
mkdir -p "$BUILD/logs"
L=$BUILD/logs
step() { local log=$1; shift; echo "$(date -u +%T) $1"; ./drun.sh "$@" > "$L/$log" 2>&1 || { echo "failed: $1 (log: $L/$log)" >&2; exit 1; }; }
step s01.log s01_standardize.py msg 1m 4m
step s02.log s02_graph.py
step s03.log s03_precursor.py
step s04.log s04_fragments.py
step s05.log s05_split.py                    # 1,000 hidden + 300 dev
step s06.log s06_collection.py               # minimal-charge representatives
step s07.log s07_write.py                    # agent/, grader/, dev/
step s08.log s08_release_checks.py
echo "$(date -u +%T) built the dataset in $BUILD"

# The task files against SOURCE (lines 3+: "<sha256>  <file name>").
fail=0
while read -r sum name; do
  f=$(find "$BUILD/agent" "$BUILD/grader" -name "$name" | head -1)
  got=$( [ -n "$f" ] && sha256sum "$f" | cut -d' ' -f1 || echo missing)
  if [ "$got" = "$sum" ]; then echo "match     $name"; else echo "MISMATCH  $name ($got)"; fail=1; fi
done < <(tail -n +3 "$HERE/SOURCE")
[ "$fail" = 0 ] && echo "all five files match SOURCE" || { echo "rebuild differs from SOURCE" >&2; exit 1; }
