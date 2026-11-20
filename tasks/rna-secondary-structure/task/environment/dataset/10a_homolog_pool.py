"""Step 10a: the deduplicated Rfam sequence pool that 10b_homolog_db.py draws the homolog database from.

Rfam 15.1 fasta_files/Rfam.fa.gz (every seed and full-region sequence of every family, 10,164,372 records) ->
exact-sequence dedup after T->U and upper-casing (6,246,385 sequences), each with the set of Rfam families it
belongs to. Family membership: Rfam.full_region.gz (accession/start-end -> family) plus the seed names in
Rfam.seed.gz (seed rows are not all in full_region). Then a BLAST index of the pool (makeblastdb -parse_seqids).

The pool does not depend on the split, so it is built once and reused (delete BUILD/homolog/pool.done to rebuild).

Inputs: BUILD/homolog/raw/Rfam.fa.gz, BUILD/homolog/raw/Rfam.full_region.gz, BUILD/raw/rfam-15.1/Rfam.seed.gz
Outputs (BUILD/homolog/): uniq.fa (ids u########), uniq_meta.tsv (uid, length, families), step1a_stats.json,
blast/uniq.* (BLAST index).
"""
import gzip
import hashlib
import json
import os
import subprocess
import time

from common import HOMOLOG, R151

HRAW = HOMOLOG / "raw"
SEED_ALN = R151 / "Rfam.seed.gz"
DONE = HOMOLOG / "pool.done"


def build_pool():
    t0 = time.time()
    region_fam = {}
    with gzip.open(HRAW / "Rfam.full_region.gz", "rt") as f:
        for line in f:
            p = line.split("\t")
            key = f"{p[1]}/{p[2]}-{p[3]}"
            region_fam.setdefault(key, set()).add(p[0])
    print("regions", len(region_fam), round(time.time() - t0), "s", flush=True)

    # seed names -> family (seed rows are not all in full_region)
    fam = None
    with gzip.open(SEED_ALN, "rt", encoding="latin-1") as f:
        for line in f:
            if line.startswith("#=GF AC"):
                fam = line.split()[2]
            elif line and not line.startswith("#") and not line.startswith("//") and line.strip():
                region_fam.setdefault(line.split()[0], set()).add(fam)
    print("regions+seeds", len(region_fam), flush=True)

    seen = {}  # digest -> uid index
    fams = []  # per uid: set of families
    lens = []
    n = {"rec": 0, "unk": 0, "bases": 0}
    out = open(HOMOLOG / "uniq.fa.part", "w")

    def flush(name, chunks):
        if name is None:
            return
        s = "".join(chunks).upper().replace("T", "U")
        n["rec"] += 1
        n["bases"] += len(s)
        fs = region_fam.get(name)
        if fs is None:
            n["unk"] += 1
            fs = {"UNK"}
        d = hashlib.blake2b(s.encode(), digest_size=12).digest()
        i = seen.get(d)
        if i is None:
            i = len(fams)
            seen[d] = i
            fams.append(set(fs))
            lens.append(len(s))
            out.write(f">u{i:08d}\n{s}\n")
        else:
            fams[i] |= fs

    with gzip.open(HRAW / "Rfam.fa.gz", "rt") as f:
        name, chunks = None, []
        for line in f:
            if line.startswith(">"):
                flush(name, chunks)
                name, chunks = line[1:].split()[0], []
            else:
                chunks.append(line.strip())
        flush(name, chunks)
    out.close()
    with open(HOMOLOG / "uniq_meta.tsv.part", "w") as m:
        m.write("uid\tlength\tfamilies\n")
        for i, (L, fs) in enumerate(zip(lens, fams)):
            m.write(f"u{i:08d}\t{L}\t{','.join(sorted(fs))}\n")
    os.replace(HOMOLOG / "uniq.fa.part", HOMOLOG / "uniq.fa")
    os.replace(HOMOLOG / "uniq_meta.tsv.part", HOMOLOG / "uniq_meta.tsv")
    st = dict(records=n["rec"], bases=n["bases"], unknown_family_records=n["unk"], unique=len(fams),
              unique_bases=sum(lens), multi_family_unique=sum(len(x) > 1 for x in fams), secs=round(time.time() - t0))
    json.dump(st, open(HOMOLOG / "step1a_stats.json", "w"), indent=1)
    print(st, flush=True)


def main():
    if DONE.exists():
        print(f"pool already built ({HOMOLOG}); delete {DONE.name} to rebuild")
        return
    HOMOLOG.mkdir(parents=True, exist_ok=True)
    build_pool()
    (HOMOLOG / "blast").mkdir(exist_ok=True)
    with open(HOMOLOG / "makeblastdb_uniq.log", "w") as lg:
        subprocess.run(["makeblastdb", "-in", str(HOMOLOG / "uniq.fa"), "-dbtype", "nucl",
                        "-out", str(HOMOLOG / "blast/uniq"), "-parse_seqids"], stdout=lg, check=True)
    DONE.write_text("ok\n")


if __name__ == "__main__":
    main()
