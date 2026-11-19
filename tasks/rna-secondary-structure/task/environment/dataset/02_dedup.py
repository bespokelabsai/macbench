"""Step 2: exact-duplicate removal and within-family near-duplicate reduction.

- Exact duplicates inside one family: keep one copy (first seed name in sort order).
- A sequence that appears in two or more families: dropped from all of them (its label is ambiguous,
  and it would tie two families together across the split).
- Near duplicates inside a family: megablast all-vs-all (plus strand). Two members are near duplicates when
  identical aligned residues / length of the LONGER sequence >= NEAR_DUP_ID. Greedy, longest first (as
  cd-hit does): a member is dropped if an already kept member is its near duplicate.
Output: work/dedup.parquet, work/dedup_stats.json
"""
import os
import subprocess
from collections import Counter

import pandas as pd

from common import NEAR_DUP_ID, WORK, WORKERS, peak_mem_note, write_json


def main():
    df = pd.read_parquet(WORK / "projected.parquet").sort_values(["family", "seed_name"]).reset_index(drop=True)
    st = {"in": len(df)}
    df = df.drop_duplicates(["family", "sequence"], keep="first")
    st["drop_exact_dup_within_family"] = st["in"] - len(df)
    nfam = df.groupby("sequence").family.nunique()
    multi = set(nfam[nfam > 1].index)
    cross = df[df.sequence.isin(multi)]
    st["cross_family_identical_sequences"] = len(multi)
    st["drop_cross_family_identical_rows"] = len(cross)
    st["cross_family_identical_family_pairs"] = Counter(
        " / ".join(sorted(g.family.unique())) for _, g in cross.groupby("sequence")).most_common(40)
    df = df[~df.sequence.isin(multi)].reset_index(drop=True)
    df["uid"] = [f"s{i:06d}" for i in range(len(df))]

    fa = WORK / "dedup_in.fa"
    with open(fa, "w") as f:
        for r in df.itertuples():
            f.write(f">{r.uid}\n{r.sequence}\n")
    tsv = WORK / "megablast_neardup.tsv"
    if not tsv.exists():
        db = WORK / "blastdb/dedup_in"
        db.parent.mkdir(exist_ok=True)
        subprocess.run(["makeblastdb", "-in", str(fa), "-dbtype", "nucl", "-out", str(db)], check=True,
                       stdout=subprocess.DEVNULL)
        subprocess.run(["blastn", "-task", "megablast", "-query", str(fa), "-db", str(db), "-strand", "plus",
                        "-perc_identity", str(round(100 * NEAR_DUP_ID)), "-evalue", "1e-5", "-dust", "no",
                        "-max_target_seqs", "5000", "-num_threads", str(WORKERS), "-mt_mode", "1",
                        "-outfmt", "6 qseqid sseqid nident qlen slen", "-out", str(tsv) + ".part"], check=True)
        os.rename(str(tsv) + ".part", tsv)
    h = pd.read_csv(tsv, sep="\t", names=["q", "s", "nident", "qlen", "slen"])
    fam = dict(zip(df.uid, df.family))
    h = h[(h.q != h.s) & (h.q.map(fam) == h.s.map(fam))]
    # global-identity proxy: identical aligned residues over the LONGER sequence
    h = h[h.nident / h[["qlen", "slen"]].max(axis=1) >= NEAR_DUP_ID]
    nbr = {}
    for q, s_ in zip(h.q, h.s):
        nbr.setdefault(q, set()).add(s_)
        nbr.setdefault(s_, set()).add(q)
    # greedy, cd-hit style: longest first; a sequence is dropped if a kept one is a near duplicate of it
    df = df.sort_values(["family", "length", "seed_name"], ascending=[True, False, True])
    kept_ids, rep_of = set(), {}
    for u in df.uid:
        hit = [k for k in nbr.get(u, ()) if k in kept_ids]
        if hit:
            rep_of[u] = min(hit)
        else:
            kept_ids.add(u)
    before = len(df)
    kept = df[df.uid.isin(kept_ids)].copy()
    st["near_dup_identity"] = NEAR_DUP_ID
    st["drop_near_dup_within_family"] = before - len(kept)
    per_fam_before = df.groupby("family").size()
    per_fam_after = kept.groupby("family").size()
    big = per_fam_before.sort_values(ascending=False).head(15)
    st["largest_families_before_after"] = {f: [int(big[f]), int(per_fam_after.get(f, 0))] for f in big.index}
    st["out"] = len(kept)
    st["families_out"] = int(kept.family.nunique())
    st["peak_mem_gb"] = peak_mem_note()
    kept = kept.sort_values(["family", "seed_name"]).reset_index(drop=True)
    kept.to_parquet(WORK / "dedup.parquet", index=False)
    write_json(WORK / "dedup_stats.json", st)
    print({k: v for k, v in st.items() if not isinstance(v, (list, dict))})


if __name__ == "__main__":
    main()
