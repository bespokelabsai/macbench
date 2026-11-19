#!/usr/bin/env bash
# Put the built dataset into the two image build contexts. Run once before building or running the task,
# and again after rebuilding the dataset.
#
#   environment/dataset/prepare.sh                    # the dataset run_all.sh built: build/ next to task/
#   environment/dataset/prepare.sh /path/to/dataset   # another copy, e.g. a build elsewhere
#   MS2_DATA=/path/to/dataset environment/dataset/prepare.sh
#
# The default follows $MS2_BUILD_DIR, as the dataset scripts do.
# The dataset dir must have the layout run_all.sh writes:
#   agent/train_spectra.parquet  agent/candidates.parquet
#   grader/hidden_spectra.jsonl  grader/hidden_labels.jsonl  grader/manifest.json
#
# environment/app/data/ gets agent/* next to its README.md (what agents see at /app/data).
# tests/hidden/         gets grader/* (the held-out spectra and labels: the only place they live), a pristine copy of
#                       agent/* for the verifier, the data README and requirements.txt (the verifier image; /tests is
#                       root-only there).
# Files are hard-linked when possible, else copied. Both are generated (git-ignored): don't edit them.
set -euo pipefail

HERE=$(cd "$(dirname "$0")/../.." && pwd)   # the task folder
DEFAULT_DATA=${MS2_BUILD_DIR:-$HERE/../build}
DATA=${1:-${MS2_DATA:-$DEFAULT_DATA}}
if [ ! -d "$DATA" ]; then
  echo "no dataset at $DATA" >&2
  if [ -z "${1:-}${MS2_DATA:-}" ]; then
    echo "build it first: environment/dataset/download.sh && environment/dataset/run_all.sh (see the README), or pass its path" >&2
  fi
  exit 1
fi
DATA=$(cd "$DATA" && pwd)

need=(agent/train_spectra.parquet agent/candidates.parquet
      grader/hidden_spectra.jsonl grader/hidden_labels.jsonl grader/manifest.json)
for f in "${need[@]}"; do
  [ -f "$DATA/$f" ] || { echo "missing $DATA/$f" >&2; exit 1; }
done

place() {  # place <src> <dst>
  rm -f "$2"
  ln "$1" "$2" 2>/dev/null || cp "$1" "$2"
  chmod 644 "$2"
}

APPDATA=$HERE/environment/app/data   # tracked: README.md; generated: the two parquet files
HIDDEN=$HERE/tests/hidden
rm -rf "$HIDDEN"
mkdir -p "$APPDATA" "$HIDDEN"
for f in train_spectra.parquet candidates.parquet; do
  place "$DATA/agent/$f" "$APPDATA/$f"
  place "$DATA/agent/$f" "$HIDDEN/$f"
done
for f in hidden_spectra.jsonl hidden_labels.jsonl manifest.json; do
  place "$DATA/grader/$f" "$HIDDEN/$f"
done
cp "$APPDATA/README.md" "$HIDDEN/README.md"
# The verifier installs exactly the agents' package set.
cp "$HERE/environment/requirements.txt" "$HIDDEN/requirements.txt"

{
  echo "source: ${DATA#"$HERE"/}"
  echo "prepared: $(date -u +%FT%TZ)"
  (cd "$HIDDEN" && sha256sum train_spectra.parquet candidates.parquet hidden_spectra.jsonl \
     hidden_labels.jsonl manifest.json)
} > "$HIDDEN/SOURCE"
cat "$HIDDEN/SOURCE"
