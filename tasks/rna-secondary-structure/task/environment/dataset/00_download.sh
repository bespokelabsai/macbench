#!/usr/bin/env bash
# Download the Rfam source files into the build dir (RNA_DATASET_BUILD, default build/ at the top of the delivery
# folder) and check them against rfam.sha256 (the files the published dataset was built from). Idempotent: a file already present is
# kept; a download goes to a .part file first, so an interrupted transfer is never mistaken for a finished one.
#   raw/            the split's inputs (Rfam 15.1 seed alignments, CMs, family/clan tables; 15.0 family table).
#                   06_write.py hashes every file here into the manifest, so nothing else may go in raw/.
#   homolog/raw/    Rfam 15.1 Rfam.fa.gz and Rfam.full_region.gz, the source of the homolog database (10a, 10b).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
BUILD="${RNA_DATASET_BUILD:-$HERE/../../../build}"
RAW="$BUILD/raw"; HRAW="$BUILD/homolog/raw"
B151=https://ftp.ebi.ac.uk/pub/databases/Rfam/15.1
B150=https://ftp.ebi.ac.uk/pub/databases/Rfam/15.0
get() {  # get <url> <dest>
  local url=$1 out=$2
  mkdir -p "$(dirname "$out")"
  [ -s "$out" ] && return 0
  rm -f "$out.part"
  curl -sSfL --retry 3 -o "$out.part" "$url"
  mv "$out.part" "$out"
}
get $B151/Rfam.seed.gz                        "$RAW/rfam-15.1/Rfam.seed.gz"
get $B151/Rfam.cm.gz                          "$RAW/rfam-15.1/Rfam.cm.gz"
get $B151/README                              "$RAW/rfam-15.1/README"
get $B151/COPYING                             "$RAW/rfam-15.1/COPYING"
for f in family clan clan_membership; do
  get $B151/database_files/$f.txt.gz "$RAW/rfam-15.1/database_files/$f.txt.gz"
  get $B151/database_files/$f.sql    "$RAW/rfam-15.1/database_files/$f.sql"
done
get $B150/database_files/family.txt.gz        "$RAW/rfam-15.0/family.txt.gz"
get $B150/database_files/family.sql           "$RAW/rfam-15.0/family.sql"
get $B151/fasta_files/Rfam.fa.gz              "$HRAW/Rfam.fa.gz"
get $B151/Rfam.full_region.gz                 "$HRAW/Rfam.full_region.gz"
( cd "$RAW" && find . -type f ! -name SHA256SUMS ! -name '*.part' | sort | xargs sha256sum > SHA256SUMS )
# Rfam's FTP files for a release do not change; a mismatch means a corrupt download or a changed upstream file.
( cd "$BUILD" && sha256sum -c --quiet "$HERE/rfam.sha256" ) \
  || { echo "Rfam downloads differ from rfam.sha256: delete the bad file under $BUILD and rerun" >&2; exit 1; }
echo "Rfam downloads match rfam.sha256 ($BUILD)"
