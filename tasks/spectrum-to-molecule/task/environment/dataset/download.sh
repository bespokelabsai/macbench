#!/usr/bin/env bash
# Download the MassSpecGym files the dataset build reads (about 3.5 GB) from Hugging Face at a pinned revision, into
# <build dir>/massspecgym/<repo path>, and check each against its sha256. Files already present with the right
# checksum are skipped, so it is safe to rerun.
#
#   ./download.sh             # into build/massspecgym next to the task folder (or $MS2_BUILD_DIR/massspecgym)
#   ./download.sh --dry-run   # list what would be downloaded and from where; download nothing
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
BUILD=${MS2_BUILD_DIR:-$HERE/../../../build}   # build/ next to the task folder
D=$BUILD/massspecgym
REV=018a9542378725cf5620423f930608730c36dd8c
BASE=https://huggingface.co/datasets/roman-bushuiev/MassSpecGym/resolve/$REV
DRY=0
case "${1:-}" in
  "") ;;
  --dry-run|-n) DRY=1 ;;
  *) echo "usage: $0 [--dry-run]" >&2; exit 2 ;;
esac

# sha256 and repo path of every input of s01-s08 (261 MB, 262 MB, 2.4 GB, 291 MB, 309 MB)
FILES="
50cfdd1d22f79543c59555f9ce43c6893bd788a19a42b21fb4e2e3a54673c06a data/MassSpecGym1.5.tsv
0c9cc50450def3f0d4fe2dc09dea1105fc15e635db8c6656bc3e3be37a3bcd95 data/MassSpecGym.tsv
0239113850176c1e3887452d7d3fc347783eaefb0a06ec62e853dcb52f87f6b2 data/auxiliary/all_smiles_mces.hdf5
b25ed5035ad58d0dbde31d64421b7a1661b785beffd3d6360014ce0fedc92abf data/molecules/candidate_pools/MassSpecGym_retrieval_molecules_1M.tsv
1f8ca23530d594836a29f7b07daff3698b558135604d957d3c5d82b58ff95e5f data/molecules/candidate_pools/MassSpecGym_retrieval_molecules_4M.tsv
"

ok() { [ -f "$1" ] && [ "$(sha256sum "$1" | cut -d' ' -f1)" = "$2" ]; }

echo "MassSpecGym @ $REV -> $D"
n=0
while read -r sum p; do
  [ -n "$p" ] || continue
  if ok "$D/$p" "$sum"; then echo "ok   $p"; continue; fi
  n=$((n + 1))
  if [ "$DRY" = 1 ]; then echo "get  $p  <- $BASE/$p"; continue; fi
  echo "get  $p"
  mkdir -p "$D/$(dirname "$p")"
  curl -sSL --fail --retry 3 -o "$D/$p.part" "$BASE/$p"
  mv "$D/$p.part" "$D/$p"
  ok "$D/$p" "$sum" || { echo "sha256 mismatch: $D/$p (expected $sum)" >&2; exit 1; }
done <<< "$FILES"
if [ "$DRY" = 1 ]; then echo "dry run: $n of 5 files to download"; else echo "done: $n downloaded; all 5 present with the expected sha256"; fi
