#!/usr/bin/env bash
# Put the built dataset into the build contexts. Run once before building or running the task, and again after
# rebuilding the dataset (then rebuild the images too).
#
#   task/environment/dataset/prepare.sh                    # default: build/dataset next to task/ (run_all.sh's
#                                                          # output; $RNA_DATASET_BUILD/dataset if that is set)
#   task/environment/dataset/prepare.sh /path/to/dataset   # or a dataset dir elsewhere
#
# The dataset dir must have this layout:
#   agent/train.parquet  agent/sequences.fa.gz
#   grader/hidden_sequences.jsonl  grader/hidden_labels.jsonl  grader/manifest.json
#
# environment/app/data/ gets agent/* (the agents see it at /app/data; its README.md is part of the task).
# tests/hidden/         gets agent/*, the data README and grader/* (the verifier image only; /tests is root-only
#                       there; its Dockerfile copies agent/* and README.md to /opt/rna/data, which verify.py copies
#                       fresh to /app/data for every batch). The hidden set lives nowhere else in task/.
# Files are copied (not linked). Both are generated: do not edit them. SOURCE (next to this script) has the
# sha256 of the five dataset files.
set -euo pipefail

TASK=$(cd "$(dirname "$0")/../.." && pwd)
DEFAULT_DATA="${RNA_DATASET_BUILD:-$(cd "$TASK/.." && pwd)/build}/dataset"
DATA="${1:-${RNA_DATA:-$DEFAULT_DATA}}"
if [ ! -d "$DATA" ]; then
  echo "no dataset at $DATA: build it first with task/environment/dataset/run_all.sh (see the README)," \
       "or pass its path" >&2
  exit 1
fi
DATA=$(cd "$DATA" && pwd)

need=(agent/train.parquet agent/sequences.fa.gz grader/hidden_sequences.jsonl grader/hidden_labels.jsonl
      grader/manifest.json)
for f in "${need[@]}"; do
  [ -f "$DATA/$f" ] || { echo "missing $DATA/$f: the dataset build is incomplete (rerun run_all.sh)" >&2; exit 1; }
done

place() {  # place <src> <dst>
  rm -f "$2"
  cp "$1" "$2"
  chmod 644 "$2"
}

APPDATA="$TASK/environment/app/data"; HIDDEN="$TASK/tests/hidden"
rm -f "$APPDATA/train.parquet" "$APPDATA/sequences.fa.gz"
rm -rf "$HIDDEN"
mkdir -p "$APPDATA" "$HIDDEN"
for f in train.parquet sequences.fa.gz; do
  place "$DATA/agent/$f" "$APPDATA/$f"
  place "$DATA/agent/$f" "$HIDDEN/$f"
done
for f in hidden_sequences.jsonl hidden_labels.jsonl manifest.json; do
  place "$DATA/grader/$f" "$HIDDEN/$f"
done
cp "$APPDATA/README.md" "$HIDDEN/README.md"
# The verifier installs exactly the agents' package set.
cp "$TASK/environment/requirements.txt" "$TASK/tests/requirements.txt"

{
  echo "source: $DATA"
  echo "prepared: $(date -u +%FT%TZ)"
  (cd "$HIDDEN" && sha256sum train.parquet sequences.fa.gz hidden_sequences.jsonl hidden_labels.jsonl manifest.json)
} > "$HIDDEN/SOURCE"
cat "$HIDDEN/SOURCE"
