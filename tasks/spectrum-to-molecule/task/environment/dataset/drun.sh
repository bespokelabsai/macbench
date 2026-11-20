#!/usr/bin/env bash
# Run one build script inside a memory- and CPU-capped container built from Dockerfile.runner (pinned Python and
# packages). The container sees only this folder (read-only, at /dataset) and the build dir (read-write, at
# /build); output lands in the build dir.
#
#   ./drun.sh <script.py> [args...]
#
# Build dir: build/ next to the task folder, or $MS2_BUILD_DIR. Caps: $MS2_DOCKER_MEMORY (default 20g, also the swap cap) and
# $MS2_DOCKER_CPUS (default 10); the scripts use at most 8 worker processes.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
BUILD=${MS2_BUILD_DIR:-$HERE/../../../build}   # build/ next to the task folder
mkdir -p "$BUILD"
BUILD=$(cd "$BUILD" && pwd)
IMAGE=spectrum-dataset-runner
MEM=${MS2_DOCKER_MEMORY:-20g}
# Build context: Dockerfile.runner and requirements-runner.txt only (.dockerignore keeps build/ out). Cached.
docker build -q -t "$IMAGE" -f "$HERE/Dockerfile.runner" "$HERE" >/dev/null
exec docker run --rm --memory="$MEM" --memory-swap="$MEM" --cpus="${MS2_DOCKER_CPUS:-10}" \
  --user "$(id -u):$(id -g)" -e HOME=/tmp -e OMP_NUM_THREADS=8 -e MKL_NUM_THREADS=8 -e OPENBLAS_NUM_THREADS=8 \
  -e PYTHONHASHSEED=0 -e PYTHONDONTWRITEBYTECODE=1 -e MS2_BUILD_DIR=/build \
  -v "$HERE":/dataset:ro -v "$BUILD":/build -w /dataset \
  "$IMAGE" python -I "$@"
