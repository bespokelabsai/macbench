#!/usr/bin/env bash
# Run a build step inside the resource-capped dataset container (20 GB RAM, no swap, 10 CPUs, no network).
# Mounts only this folder (read-only, at /dataset, the working directory) and the build dir (read-write, at
# /build; RNA_DATASET_BUILD, default build/ at the top of the delivery folder). run_all.sh builds the image
# from docker/.
#   ./drun.sh python 01_parse_project.py
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
BUILD="${RNA_DATASET_BUILD:-$HERE/../../../build}"; mkdir -p "$BUILD"; BUILD="$(cd "$BUILD" && pwd)"
IMAGE="${RNA_DATASET_IMAGE:-rna-ss-dataset}"
docker run --rm -i --network none --memory=20g --memory-swap=20g --cpus=10 --pids-limit=4096 \
  -u "$(id -u):$(id -g)" -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 -e RNA_DATASET_BUILD=/build \
  -v "$HERE:/dataset:ro" -v "$BUILD:/build" -w /dataset \
  "$IMAGE" "$@"
