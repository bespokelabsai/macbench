"""Step 10b: the sequence-only homolog database shipped as agent/sequences.fa.gz.

Source: Rfam 15.1 fasta_files/Rfam.fa.gz (all seed + full-region sequences of every family), exact-deduplicated
by step 10a (BUILD/homolog/uniq.fa + uniq_meta.tsv, T->U, upper case; read-only here).
Rules, in order:
  1. Near-duplicates of hidden test sequences are removed: BLASTN (plus strand, word 11, plus blastn-short for test
     sequences <= 60 nt, -perc_identity 90) of every hidden test sequence against the deduplicated pool; a pool
     sequence goes if identical residues / min(test length, pool length) >= 0.95. Exact copies are caught too.
  2. Depth cap: at most DB_PER_FAMILY sequences per Rfam family, drawn at random with a per-family RNG seeded from
     SEED and the family accession. A sequence in several families is kept if any family draws it.
  3. Output: sequences only, in one random order (seed stream [SEED, 6]), renumbered s0000001.., wrapped never.
     The gzip header carries a fixed time (GZ_MTIME, the published file's), so a rebuild is byte-identical.
     The id -> families map is written to work/db/ for analysis only (never shipped).
Inputs: BUILD/dataset/grader/hidden_sequences.jsonl, step 10a's uniq.fa / uniq_meta.tsv / BLAST index.
"""
import gzip
import hashlib
import io
import json
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd

from common import HOMOLOG, OUT, SEED, WORK, write_json

DB_PER_FAMILY = 200
GZ_MTIME = 1793909313  # 2026-11-05T20:08:33Z, the time in the published sequences.fa.gz's gzip header
POOL = HOMOLOG  # step 10a's output
D = WORK / "db"


def fam_rng(family):
    return np.random.default_rng([SEED, 5, int(hashlib.sha256(family.encode()).hexdigest()[:8], 16)])


def main():
    D.mkdir(parents=True, exist_ok=True)
    hs = [json.loads(l) for l in open(OUT / "grader/hidden_sequences.jsonl")]
    with open(D / "test.fa", "w") as o, open(D / "test_short.fa", "w") as s:
        for r in hs:
            o.write(f">{r['id']}\n{r['sequence']}\n")
            if len(r["sequence"]) <= 60:
                s.write(f">{r['id']}\n{r['sequence']}\n")
    fmt = "6 qseqid sseqid pident length nident qlen slen evalue"
    common = ["-db", str(POOL / "blast/uniq"), "-strand", "plus", "-dust", "no", "-perc_identity", "90",
              "-max_target_seqs", "1000000", "-max_hsps", "1", "-num_threads", "8", "-outfmt", fmt]
    subprocess.run(["blastn", "-task", "blastn", "-word_size", "11", "-query", str(D / "test.fa"), "-evalue", "1e-2",
                    "-out", str(D / "neardup_w11.tsv")] + common, check=True)
    subprocess.run(["blastn", "-task", "blastn-short", "-query", str(D / "test_short.fa"), "-evalue", "10",
                    "-out", str(D / "neardup_short.tsv")] + common, check=True)
    cols = fmt.split()[1:]
    h = pd.concat([pd.read_csv(D / f, sep="\t", names=cols) for f in ("neardup_w11.tsv", "neardup_short.tsv")])
    h["sseqid"] = h.sseqid.astype(str).str.replace("^lcl\\|", "", regex=True)
    rmin = h.nident / h[["qlen", "slen"]].min(axis=1)
    near = set(h.sseqid[rmin >= 0.95])
    st = {"source": "Rfam 15.1 Rfam.fa.gz, exact-deduplicated (step 10a)",
          "neardup_rule": "nident/min(qlen,slen)>=0.95 vs every hidden test sequence",
          "test_sequences": len(hs), "test_seqs_with_a_neardup": int(h.qseqid[rmin >= 0.95].nunique()),
          "neardup_removed": len(near), "per_family_cap": DB_PER_FAMILY, "seed": SEED}

    meta = pd.read_csv(POOL / "uniq_meta.tsv", sep="\t")
    st["pool_unique"] = len(meta)
    meta = meta[~meta.uid.isin(near)]
    fam = meta.assign(family=meta.families.str.split(",")).explode("family")[["uid", "family", "length"]]
    keep = set()
    for f, g in fam.groupby("family", sort=True):
        u = g.uid.sort_values().to_numpy()
        if len(u) > DB_PER_FAMILY:
            u = u[np.sort(fam_rng(f).choice(len(u), DB_PER_FAMILY, replace=False))]
        keep.update(u)
    seqs = {}
    with open(POOL / "uniq.fa") as fh:
        name = None
        for line in fh:
            if line.startswith(">"):
                name = line[1:].strip()
            elif name in keep:
                seqs[name] = line.strip()
    order = sorted(seqs)
    order = [order[k] for k in np.random.default_rng([SEED, 6]).permutation(len(order))]
    newid = {u: f"s{k + 1:07d}" for k, u in enumerate(order)}
    (OUT / "agent").mkdir(parents=True, exist_ok=True)
    with io.TextIOWrapper(gzip.GzipFile(OUT / "agent/sequences.fa.gz", "wb", compresslevel=9, mtime=GZ_MTIME)) as o:
        for u in order:
            o.write(f">{newid[u]}\n{seqs[u]}\n")
    m = fam[fam.uid.isin(keep)].assign(id=lambda x: x.uid.map(newid))
    m[["id", "uid", "family", "length"]].to_parquet(D / "db_map.parquet", index=False)

    hl = pd.read_json(OUT / "grader/hidden_labels.jsonl", lines=True)
    tf = sorted(hl.family.unique())
    depth = m[m.family.isin(tf)].groupby("family").size().reindex(tf).fillna(0).astype(int)
    depth.rename("db_members").to_frame().to_parquet(D / "test_family_depth.parquet")
    lens = pd.Series([len(seqs[u]) for u in order])
    st.update({
        "sequences": len(order), "nt": int(lens.sum()), "families_represented": int(m.family.nunique()),
        "length_quantiles": {str(q): float(lens.quantile(q)) for q in (0.05, 0.5, 0.95, 1.0)},
        "gz_bytes": (OUT / "agent/sequences.fa.gz").stat().st_size,
        "test_families": len(tf), "test_family_depth_quantiles": {str(q): float(depth.quantile(q)) for q in (0, 0.1, 0.25, 0.5, 0.75, 0.9, 1)},
        "test_families_depth_0": int((depth == 0).sum()), "test_families_depth_le_5": int((depth <= 5).sum()),
        "test_families_at_cap": int((depth >= DB_PER_FAMILY).sum()),
        "db_sequences_from_test_families": int(m[m.family.isin(tf)].uid.nunique()),
    })
    write_json(D / "db_stats.json", st)
    print(json.dumps(st, indent=1))


if __name__ == "__main__":
    main()
