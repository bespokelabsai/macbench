#!/usr/bin/env bash
# Build the dataset end to end from the public Rfam 15.1 files, then check it against SOURCE.
#   ./run_all.sh
# Heavy steps run in a resource-capped container (drun.sh). Everything is written under the build dir:
# RNA_DATASET_BUILD if set, else build/ at the top of the delivery folder (outside task/, gitignored):
#   raw/, homolog/raw/   the Rfam downloads (00_download.sh; kept, so a rerun does not download again)
#   homolog/             the deduplicated Rfam sequence pool and its BLAST index (10a; kept and reused)
#   work/                intermediates (rebuilt from scratch on every run)
#   image/               the build image's context (its Dockerfile and the fetched tools)
#   dataset/             the dataset that prepare.sh reads
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; cd "$HERE"
export RNA_DATASET_BUILD="${RNA_DATASET_BUILD:-$HERE/../../../build}"; mkdir -p "$RNA_DATASET_BUILD"
RNA_DATASET_BUILD="$(cd "$RNA_DATASET_BUILD" && pwd)"
IMAGE="${RNA_DATASET_IMAGE:-rna-ss-dataset}"; export RNA_DATASET_IMAGE="$IMAGE"
rm -rf "$RNA_DATASET_BUILD/work"
./00_download.sh
CTX="$RNA_DATASET_BUILD/image"; mkdir -p "$CTX"; cp docker/Dockerfile "$CTX/Dockerfile"
docker/fetch_vendor.sh "$CTX/vendor" >/dev/null
DOCKER_BUILDKIT=0 docker build -q -t "$IMAGE" "$CTX"
./drun.sh python 01_parse_project.py
./drun.sh python 02_dedup.py
./drun.sh python 03_homology.py
./loop_split_leak.sh
./drun.sh python 05b_nearest.py
./drun.sh python 06_write.py
./drun.sh python 07_stats.py
./drun.sh python 08_validate.py
# The homolog database agent/sequences.fa.gz: the pool once, then the database.
./drun.sh python 10a_homolog_pool.py
./drun.sh python 10b_homolog_db.py
OUT="$RNA_DATASET_BUILD/dataset"
# SOURCE lists the sha256 of the five dataset files the published runs used.
bad=0
while read -r h f; do
  p=""; for d in agent grader; do if [ -f "$OUT/$d/$f" ]; then p="$OUT/$d/$f"; fi; done
  if [ -n "$p" ] && [ "$(sha256sum "$p" | cut -d' ' -f1)" = "$h" ]; then echo "match     $f"; else echo "MISMATCH  $f"; bad=1; fi
done < <(grep -E '^[0-9a-f]{64}  ' SOURCE)
[ $bad = 0 ] || { echo "the rebuilt dataset differs from SOURCE" >&2; exit 1; }
echo "dataset: $OUT  (next: prepare.sh)"
