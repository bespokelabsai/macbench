#!/usr/bin/env bash
# Fetch the dataset image's tool binaries and Python wheels on the host into <dir> (run_all.sh passes the build
# dir's image/vendor; the image build itself runs offline). Every file is checked against vendor.sha256 (the files
# the published dataset was built with); a mismatch stops the build.
# Downloads go to a .part file first, so an interrupted fetch is retried on the next run instead of being kept.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
[ $# -eq 1 ] || { echo "usage: fetch_vendor.sh <dir>" >&2; exit 2; }
V="$1"; mkdir -p "$V/wheels"; V=$(cd "$V" && pwd)
get() {  # get <url> <dest>
  [ -s "$2" ] && return 0
  curl -sSfL --retry 3 -o "$2.part" "$1" && mv "$2.part" "$2"
}
get https://ftp.ncbi.nlm.nih.gov/blast/executables/blast+/2.16.0/ncbi-blast-2.16.0+-x64-linux.tar.gz "$V/blast.tgz"
get https://eddylab.org/infernal/infernal-1.1.5-linux-intel-gcc.tar.gz                             "$V/infernal.tgz"
get https://deb.debian.org/debian/pool/main/g/gcc-12/libgomp1_12.2.0-14+deb12u1_amd64.deb            "$V/libgomp1.deb"
# Wheels: the exact files listed in vendor.sha256, located through PyPI's JSON API (HTTPS).
python3 -I - "$V/wheels" "$HERE/vendor.sha256" <<'PY2'
import json, os, sys, urllib.request
out, sums = sys.argv[1], sys.argv[2]
for line in open(sums):
    name = line.split()[1]
    if not name.startswith("wheels/"):
        continue
    fn = name.split("/", 1)[1]
    dst = os.path.join(out, fn)
    if os.path.exists(dst) and os.path.getsize(dst):
        continue
    proj, ver = fn.split("-")[:2]
    meta = json.load(urllib.request.urlopen(f"https://pypi.org/pypi/{proj}/{ver}/json"))
    url = next(u["url"] for u in meta["urls"] if u["filename"] == fn)
    urllib.request.urlretrieve(url, dst + ".part")
    os.replace(dst + ".part", dst)
    print(fn)
PY2
( cd "$V" && sha256sum -c --quiet "$HERE/vendor.sha256" ) || { echo "vendor checksum mismatch (see $HERE/vendor.sha256)" >&2; exit 1; }
