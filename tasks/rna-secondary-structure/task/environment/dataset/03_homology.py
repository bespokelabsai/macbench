"""Step 3: find families that are homologous across Rfam clan boundaries, before splitting.

All-vs-all BLASTN over the deduplicated sequences. Two families are linked when at least one pair of their
sequences has a hit with E <= 1e-5 whose aligned identical residues cover >= 50% of the shorter sequence,
or at least 5 sequence pairs (10 directed hits) with E <= 1e-3 at >= 30% cover. A looser 2-pair rule chained
unrelated clans through a few AT-rich plastid hits (RNase P - L31 riboswitch - plastid ndhA-ndhB), so it was raised.
Split units = clans and clan-less families, merged by these links (union-find).
Output: work/blast_all.tsv, work/family_links.parquet, work/units.parquet, work/homology_stats.json
"""
import subprocess
from collections import Counter

import pandas as pd

from common import WORK, WORKERS, peak_mem_note, write_json

E_STRONG, COV_STRONG = 1e-5, 0.5
E_WEAK, COV_WEAK, MIN_WEAK_PAIRS = 1e-3, 0.3, 5


class UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def main():
    df = pd.read_parquet(WORK / "dedup.parquet")
    fa = WORK / "all.fa"
    with open(fa, "w") as f:
        for r in df.itertuples():
            f.write(f">{r.uid}\n{r.sequence}\n")
    db = WORK / "blastdb/all"
    db.parent.mkdir(exist_ok=True)
    tsv = WORK / "blast_all.tsv"
    if not tsv.exists():
        subprocess.run(["makeblastdb", "-in", str(fa), "-dbtype", "nucl", "-out", str(db)], check=True,
                       stdout=subprocess.DEVNULL)
        subprocess.run(["blastn", "-task", "blastn", "-query", str(fa), "-db", str(db), "-strand", "plus",
                        "-evalue", "1e-3", "-max_target_seqs", "1000", "-num_threads", str(WORKERS), "-mt_mode", "1",
                        "-outfmt", "6 qseqid sseqid pident length nident qlen slen evalue bitscore",
                        "-out", str(tsv) + ".part"], check=True)
        (WORK / "blast_all.tsv.part").rename(tsv)
    cols = ["q", "s", "pident", "alen", "nident", "qlen", "slen", "evalue", "bits"]
    h = pd.read_csv(tsv, sep="\t", names=cols)
    fam = dict(zip(df.uid, df.family))
    h["qf"], h["sf"] = h.q.map(fam), h.s.map(fam)
    h = h[h.qf != h.sf]
    h["cov_short"] = h.nident / h[["qlen", "slen"]].min(axis=1)
    h = h.sort_values("evalue").drop_duplicates(["q", "s"])
    h["fa"] = h[["qf", "sf"]].min(axis=1)
    h["fb"] = h[["qf", "sf"]].max(axis=1)
    strong = h[(h.evalue <= E_STRONG) & (h.cov_short >= COV_STRONG)]
    weak = h[(h.evalue <= E_WEAK) & (h.cov_short >= COV_WEAK)]
    agg = weak.groupby(["fa", "fb"]).agg(n_pairs=("q", "size"), best_e=("evalue", "min"),
                                         best_cov=("cov_short", "max"), best_pident=("pident", "max")).reset_index()
    sp = set(zip(strong.fa, strong.fb))
    agg["strong"] = [(a, b) in sp for a, b in zip(agg.fa, agg.fb)]
    agg["link"] = agg.strong | (agg.n_pairs >= MIN_WEAK_PAIRS * 2)  # weak pairs are counted in both directions
    fams = pd.read_parquet(WORK / "families.parquet")
    fams = fams[fams.family.isin(df.family.unique())]
    clan = dict(zip(fams.family, fams.clan))
    agg["clan_a"], agg["clan_b"] = agg.fa.map(clan), agg.fb.map(clan)
    agg["same_clan"] = agg.clan_a.notna() & (agg.clan_a == agg.clan_b)
    agg.to_parquet(WORK / "family_links.parquet", index=False)

    uf = UF()
    unit0 = {f: (c if isinstance(c, str) else f) for f, c in clan.items()}
    for f in fams.family:
        uf.find(unit0[f])
    for r in agg[agg.link & ~agg.same_clan].itertuples():
        uf.union(unit0[r.fa], unit0[r.fb])
    fams["unit0"] = fams.family.map(unit0)
    fams["unit"] = fams.unit0.map(uf.find)
    fams[["family", "family_name", "clan", "rna_type", "new_in_15_1", "unit0", "unit"]].to_parquet(
        WORK / "units.parquet", index=False)
    usize = fams.groupby("unit").family.size().sort_values(ascending=False)
    merged = fams.groupby("unit").unit0.nunique()
    st = {
        "blast_hits_cross_family": int(len(h)),
        "family_pairs_with_weak_hits": int(len(agg)),
        "family_pairs_linked": int(agg.link.sum()),
        "family_pairs_linked_cross_clan_or_clanless": int((agg.link & ~agg.same_clan).sum()),
        "units_before_merge": int(fams.unit0.nunique()),
        "units_after_merge": int(fams.unit.nunique()),
        "merged_units": int((merged > 1).sum()),
        "families_in_merged_units": int(fams.unit.isin(merged[merged > 1].index).sum()),
        "largest_units": {u: int(n) for u, n in usize.head(20).items()},
        "peak_mem_gb": peak_mem_note(),
    }
    write_json(WORK / "homology_stats.json", st)
    print({k: v for k, v in st.items() if not isinstance(v, dict)})
    print(st["largest_units"])


if __name__ == "__main__":
    main()
