#!/usr/bin/env bash
# Intermediate scores of a finished team run.
#   tests/checkpoints/grade_checkpoints.sh <job_or_trial_dir> [--marks 0.5h,1h,2h,3h,4h] [--out DIR] [--dry-run]
# Reconstructs the team's main at each checkpoint (agent time 0.5, 1, 2, 3 and 4 h by default) from the run's
# archived checkpoints (tests/checkpoints/archive_checkpoints.sh, started next to the run) and its final
# team.bundle, grades each commit with the verifier image as the final verifier does (no network, 4 CPUs, 8 GB),
# and prints one row per checkpoint: mark, commit, how certain the commit is, reward. Results and per-mark
# verifier output go to <job dir>.graded-checkpoints/. The verifier image (rna-ss-verifier, IMAGE to change it) is
# built from tests/ if it is missing; run task/environment/dataset/prepare.sh first. Needs git, docker and python3.
set -euo pipefail
[ $# -ge 1 ] && [ "$1" != -h ] && [ "$1" != --help ] || { sed -n 2,9p "$0"; exit 2; }
HERE=$(cd "$(dirname "$0")" && pwd)
IMAGE=${IMAGE:-rna-ss-verifier}
if ! docker image inspect "$IMAGE" >/dev/null 2>&1 && [[ " $* " != *" --dry-run "* ]]; then
  [ -f "$HERE/../hidden/hidden_labels.jsonl" ] || { echo "no tests/hidden: run task/environment/dataset/prepare.sh first" >&2; exit 1; }
  docker build -q -t "$IMAGE" "$HERE/.." >/dev/null
fi
exec python3 -I "$HERE/grade_checkpoints.py" --image "$IMAGE" "$@"
