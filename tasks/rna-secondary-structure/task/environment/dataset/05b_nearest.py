"""Step 5b (report only): nearest training sequence for every test sequence with a permissive BLASTN
(word size 7, E <= 10), so that even weak, non-significant similarity is measured.
Per test sequence: max over hits of identical residues / test length (qid), with that hit's pident, coverage, E.
Also a k-mer proxy independent of alignment: max Jaccard of 6-mer sets against any training sequence of the
same length bin (within 0.5x-2x length).
Output: work/nearest.parquet, nearest_stats.json
"""
import shutil
import subprocess
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

from common import WORK, WORKERS, peak_mem_note, write_json

K = 6


def kmers(s):
    return {s[i:i + K] for i in range(len(s) - K + 1)}


_TR = None


def _init(tr):
    global _TR
    _TR = tr


def best_jacc(args):
    seq, n = args
    a = kmers(seq)
    best, bf = 0.0, None
    for L, ks, fam in _TR:
        if L < n / 2 or L > 2 * n:
            continue
        inter = len(a & ks)
        if inter:
            j = inter / (len(a) + len(ks) - inter)
            if j > best:
                best, bf = j, fam
    return best, bf


def main():
    sp = pd.read_parquet(WORK / "split.parquet")
    te, tr = sp[sp.split == "test"], sp[sp.split == "train"]
    tmp = WORK / "nearest_tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    for name, d in (("train", tr), ("test", te)):
        with open(tmp / f"{name}.fa", "w") as f:
            for r in d.itertuples():
                f.write(f">{r.uid}\n{r.sequence}\n")
    subprocess.run(["makeblastdb", "-in", str(tmp / "train.fa"), "-dbtype", "nucl", "-out", str(tmp / "train")],
                   check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["blastn", "-task", "blastn", "-word_size", "7", "-evalue", "10", "-strand", "plus",
                    "-query", str(tmp / "test.fa"), "-db", str(tmp / "train"), "-max_target_seqs", "20",
                    "-num_threads", str(WORKERS), "-mt_mode", "1",
                    "-outfmt", "6 qseqid sseqid pident length nident qlen slen qstart qend evalue bitscore",
                    "-out", str(tmp / "hits.tsv")], check=True)
    h = pd.read_csv(tmp / "hits.tsv", sep="\t", names=["uid", "hit_uid", "pident", "alen", "nident", "qlen", "slen",
                                                         "qstart", "qend", "evalue", "bits"])
    h["qid"] = h.nident / h.qlen
    h["qcov"] = (h.qend - h.qstart + 1) / h.qlen
    b = h.sort_values(["uid", "qid"], ascending=[True, False]).drop_duplicates("uid")
    fam = dict(zip(sp.uid, sp.family))
    out = te[["uid", "family", "length"]].merge(b[["uid", "hit_uid", "qid", "pident", "qcov", "evalue"]], on="uid", how="left")
    out["hit_family"] = out.hit_uid.map(fam)
    out[["qid", "qcov"]] = out[["qid", "qcov"]].fillna(0)
    trk = [(r.length, kmers(r.sequence), r.family) for r in tr.itertuples()]
    with ProcessPoolExecutor(WORKERS, initializer=_init, initargs=(trk,)) as ex:
        res = list(ex.map(best_jacc, list(zip(te.sequence, te.length)), chunksize=16))
    out["kmer_jaccard"] = [r[0] for r in res]
    out["kmer_family"] = [r[1] for r in res]
    out.to_parquet(WORK / "nearest.parquet", index=False)
    q = [0.5, 0.75, 0.9, 0.95, 0.99, 1.0]
    fam_med = out.groupby("family").qid.median()
    st = {
        "qid_quantiles": {str(x): round(float(out.qid.quantile(x)), 3) for x in q},
        "qid_ge_0.5": int((out.qid >= 0.5).sum()), "qid_ge_0.6": int((out.qid >= 0.6).sum()),
        "qid_ge_0.7": int((out.qid >= 0.7).sum()),
        "qid_hist": out.qid.pipe(pd.cut, [-0.01, 0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 1.0]).value_counts(sort=False).astype(int).rename(str).to_dict(),
        "family_median_qid_ge_0.5": int((fam_med >= 0.5).sum()),
        "top_test_seqs": out.sort_values("qid", ascending=False).head(15)[["family", "length", "qid", "pident", "qcov", "evalue", "hit_family"]].to_dict("records"),
        "kmer_jaccard_quantiles": {str(x): round(float(out.kmer_jaccard.quantile(x)), 3) for x in q},
        "kmer_top": out.sort_values("kmer_jaccard", ascending=False).head(10)[["family", "length", "kmer_jaccard", "kmer_family"]].to_dict("records"),
        "peak_mem_gb": peak_mem_note(),
    }
    shutil.rmtree(tmp)
    write_json(WORK / "nearest_stats.json", st)
    print(st)


if __name__ == "__main__":
    main()
