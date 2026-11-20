"""Step 5: leakage check of the current split, and moves for leaky test units.

Three independent homology signals between every test sequence and the training set:
  A. BLASTN (-task blastn, word size 7, plus strand, E <= 0.01) of test sequences against the training sequences.
     Per test sequence: best hit by bitscore; qid = identical residues / test length ("identity over the
     whole test sequence"), plus pident and query coverage.
  B. cmscan of test sequences against all Rfam 15.1 covariance models (CMs), Rfam's own gathering
     thresholds (--cut_ga). A hit to a TRAINING family's CM means Rfam's model of a training family
     recognises the test sequence: homology even where BLAST is blind.
  C. cmsearch of each TEST family's CM against all training sequences (--cut_ga): the reverse direction,
     catching test families that sit inside longer training RNAs.
Decision (per test family F of unit U):
  leaky_seq = (A: a hit with qid >= 0.7 and qcov >= 0.8, or a hit with E <= 1e-3 and qcov >= 0.5)
              or B-hit to a train family
  F is homologous if any of its test sequences is leaky, or if C finds >= C_MIN_HITS training
  sequences (from any one train unit) above GA.
  - U not forced (no 15.1-new family): U moves to train.
  - U forced (contains a 15.1-new family): the matching train units move to test if eligible, else the
    leaky family is dropped.
  Remaining isolated leaky test sequences (family not homologous overall) are removed from test.
Writes work/leak_moves.json (cumulative), leak_seq.parquet, leak_family.parquet, leak_stats.json.
Exit code 0 = no new moves (split is final), 3 = moves were added (re-run step 4 then 5).
"""
import gzip
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

from common import R151, WORK, WORKERS, peak_mem_note, write_json

QID, QCOV = 0.7, 0.8      # high identity over most of the test sequence (hit E <= 0.01)
E_SIG, QCOV_SIG = 1e-3, 0.5  # or: a significant hit spanning at least half of the test sequence
FAM_FRAC = 1e-9            # any leaky test sequence marks its family homologous (zero tolerance)
C_MIN_HITS = 2
CMDIR = WORK / "cm"


def sh(cmd, **kw):
    subprocess.run(cmd, check=True, **kw)


def prep_cms():
    CMDIR.mkdir(exist_ok=True)
    cm = CMDIR / "Rfam.cm"
    if not (CMDIR / "Rfam.cm.i1m").exists():
        with gzip.open(R151 / "Rfam.cm.gz", "rb") as fi, open(cm, "wb") as fo:
            shutil.copyfileobj(fi, fo)
        sh(["cmpress", "-F", str(cm)], stdout=subprocess.DEVNULL)
    return cm


def write_fa(path, rows):
    with open(path, "w") as f:
        for u, s in rows:
            f.write(f">{u}\n{s}\n")


def parse_tbl(path):
    rows = []
    for line in open(path):
        if line.startswith("#"):
            continue
        p = line.split()
        # target name, target acc, query name, query acc, mdl, mdl from, mdl to, seq from, seq to, strand, trunc,
        # pass, gc, bias, score, E-value, inc
        rows.append((p[0], p[1], p[2], p[3], p[9], float(p[14]), float(p[15])))
    return pd.DataFrame(rows, columns=["t", "tacc", "q", "qacc", "strand", "score", "evalue"])


def cmscan_cached(cm, test):
    """cmscan each test uid once against all CMs; results cached across iterations."""
    cache = WORK / "cmscan_cache.parquet"
    done = pd.read_parquet(cache) if cache.exists() else pd.DataFrame(columns=["uid", "hit_family", "score", "evalue"])
    scanned = set(pd.read_parquet(WORK / "cmscan_scanned.parquet").uid) if (WORK / "cmscan_scanned.parquet").exists() else set()
    todo = test[~test.uid.isin(scanned)]
    if len(todo):
        tmp = WORK / "cmscan_tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        arr = todo.sort_values("length", ascending=False)[["uid", "sequence"]].values
        jobs = []
        for k in range(WORKERS):
            if len(arr[k::WORKERS]):
                write_fa(tmp / f"c{k}.fa", [tuple(x) for x in arr[k::WORKERS]])
                jobs.append(k)

        def run(k):
            sh(["cmscan", "--cut_ga", "--rfam", "--nohmmonly", "--toponly", "--cpu", "1", "--noali",
                "--tblout", str(tmp / f"c{k}.tbl"), "-o", "/dev/null", str(cm), str(tmp / f"c{k}.fa")])

        with ThreadPoolExecutor(WORKERS) as ex:
            list(ex.map(run, jobs))
        new = pd.concat([parse_tbl(tmp / f"c{k}.tbl") for k in jobs])
        new = new.rename(columns={"q": "uid", "tacc": "hit_family"})[["uid", "hit_family", "score", "evalue"]]
        done = pd.concat([done, new], ignore_index=True)
        done.to_parquet(cache, index=False)
        pd.DataFrame({"uid": sorted(scanned | set(todo.uid))}).to_parquet(WORK / "cmscan_scanned.parquet", index=False)
        shutil.rmtree(tmp)
    return done[done.uid.isin(test.uid)]


def cmsearch_cached(cm, families, allseq):
    """cmsearch each test family's CM against ALL sequences once; cached per family."""
    cache = WORK / "cmsearch_cache.parquet"
    done = pd.read_parquet(cache) if cache.exists() else pd.DataFrame(columns=["family", "hit_uid", "score", "evalue"])
    searched_p = WORK / "cmsearch_searched.parquet"
    searched = set(pd.read_parquet(searched_p).family) if searched_p.exists() else set()
    todo = sorted(set(families) - searched)
    if todo:
        tmp = WORK / "cmsearch_tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        db = WORK / "all_for_cmsearch.fa"
        if not db.exists():
            write_fa(db, list(zip(allseq.uid, allseq.sequence)))
        groups = [todo[k::WORKERS] for k in range(WORKERS)]

        def run(k):
            if not groups[k]:
                return
            (tmp / f"g{k}.txt").write_text("\n".join(groups[k]) + "\n")
            sh(["cmfetch", "-o", str(tmp / f"g{k}.cm"), "-f", str(cm), str(tmp / f"g{k}.txt")], stdout=subprocess.DEVNULL)
            sh(["cmsearch", "--cut_ga", "--rfam", "--nohmmonly", "--toponly", "--cpu", "1", "--noali",
                "--tblout", str(tmp / f"g{k}.tbl"), "-o", "/dev/null", str(tmp / f"g{k}.cm"), str(db)])

        with ThreadPoolExecutor(WORKERS) as ex:
            list(ex.map(run, range(WORKERS)))
        new = pd.concat([parse_tbl(tmp / f"g{k}.tbl") for k in range(WORKERS) if groups[k]])
        new = new.rename(columns={"t": "hit_uid", "qacc": "family"})[["family", "hit_uid", "score", "evalue"]]
        done = pd.concat([done, new], ignore_index=True)
        done.to_parquet(cache, index=False)
        pd.DataFrame({"family": sorted(searched | set(todo))}).to_parquet(searched_p, index=False)
        shutil.rmtree(tmp)
    return done[done.family.isin(families)]


def blast_test_vs_train(test, train):
    tmp = WORK / "leak_blast"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    write_fa(tmp / "train.fa", list(zip(train.uid, train.sequence)))
    write_fa(tmp / "test.fa", list(zip(test.uid, test.sequence)))
    sh(["makeblastdb", "-in", str(tmp / "train.fa"), "-dbtype", "nucl", "-out", str(tmp / "train")], stdout=subprocess.DEVNULL)
    sh(["blastn", "-task", "blastn", "-word_size", "7", "-query", str(tmp / "test.fa"), "-db", str(tmp / "train"),
        "-strand", "plus", "-evalue", "1e-2", "-max_target_seqs", "50", "-num_threads", str(WORKERS), "-mt_mode", "1",
        "-outfmt", "6 qseqid sseqid pident length nident qlen slen qstart qend evalue bitscore",
        "-out", str(tmp / "hits.tsv")])
    h = pd.read_csv(tmp / "hits.tsv", sep="\t", names=["uid", "hit_uid", "pident", "alen", "nident", "qlen", "slen",
                                                         "qstart", "qend", "evalue", "bits"])
    h["qid"] = h.nident / h.qlen
    h["qcov"] = (h.qend - h.qstart + 1) / h.qlen
    h["leak"] = ((h.qid >= QID) & (h.qcov >= QCOV)) | ((h.evalue <= E_SIG) & (h.qcov >= QCOV_SIG))
    best_bits = h.sort_values(["uid", "bits"], ascending=[True, False]).drop_duplicates("uid")
    best_qid = h.sort_values(["uid", "qid"], ascending=[True, False]).drop_duplicates("uid")
    leak_hits = h[h.leak].sort_values(["uid", "bits"], ascending=[True, False]).drop_duplicates("uid")
    shutil.rmtree(tmp)
    return best_bits, best_qid, leak_hits


def main():
    sp = pd.read_parquet(WORK / "split.parquet")
    units = pd.read_parquet(WORK / "split_units.parquet")
    test, train = sp[sp.split == "test"], sp[sp.split == "train"]
    fam_unit = dict(zip(sp.family, sp.unit))
    train_fams = set(train.family)
    cm = prep_cms()

    best_bits, best_qid, leak_hits = blast_test_vs_train(test, train)
    scan = cmscan_cached(cm, test)
    srch = cmsearch_cached(cm, sorted(test.family.unique()), sp[["uid", "sequence"]])

    seq = test[["uid", "family", "unit", "length", "new_in_15_1"]].copy()
    seq = seq.merge(best_bits[["uid", "hit_uid", "pident", "qid", "qcov", "evalue", "bits"]], on="uid", how="left")
    seq = seq.merge(best_qid[["uid", "qid", "qcov"]].rename(columns={"qid": "max_qid", "qcov": "max_qid_qcov"}),
                    on="uid", how="left")
    seq["hit_family"] = seq.hit_uid.map(dict(zip(sp.uid, sp.family)))
    seq[["qid", "qcov", "max_qid", "max_qid_qcov", "pident"]] = seq[["qid", "qcov", "max_qid", "max_qid_qcov", "pident"]].fillna(0)
    seq["blast_leak"] = seq.uid.isin(leak_hits.uid)
    seq["blast_leak_family"] = seq.uid.map(dict(zip(leak_hits.uid, leak_hits.hit_uid.map(dict(zip(sp.uid, sp.family))))))
    scan_tr = scan[scan.hit_family.isin(train_fams)]
    seq["cm_train_hits"] = seq.uid.map(scan_tr.groupby("uid").hit_family.apply(lambda s: ";".join(sorted(set(s))))).fillna("")
    seq["cm_leak"] = seq.cm_train_hits != ""
    own = scan.merge(test[["uid", "family"]], on="uid")
    seq["cm_own_family_hit"] = seq.uid.isin(own[own.hit_family == own.family].uid)
    seq["leaky"] = seq.blast_leak | seq.cm_leak

    srch = srch.copy()
    srch["hit_split"] = srch.hit_uid.map(dict(zip(sp.uid, sp.split)))
    srch["hit_family"] = srch.hit_uid.map(dict(zip(sp.uid, sp.family)))
    srch_tr = srch[srch.hit_split == "train"]
    rev = srch_tr.groupby(["family", "hit_family"]).hit_uid.nunique().reset_index(name="n")

    fam = seq.groupby("family").agg(unit=("unit", "first"), n_test=("uid", "size"), new=("new_in_15_1", "any"),
                                    frac_leaky=("leaky", "mean"), frac_blast=("blast_leak", "mean"),
                                    frac_cm=("cm_leak", "mean"), median_qid=("max_qid", "median"),
                                    frac_own_cm=("cm_own_family_hit", "mean")).reset_index()
    fam["rev_hits"] = fam.family.map(rev.groupby("family").n.sum()).fillna(0).astype(int)
    fam["rev_max_one_family"] = fam.family.map(rev.groupby("family").n.max()).fillna(0).astype(int)
    partners = {}
    for r in seq[seq.leaky].itertuples():
        fs = set(r.cm_train_hits.split(";")) - {""}
        if r.blast_leak and isinstance(r.blast_leak_family, str):
            fs.add(r.blast_leak_family)
        partners.setdefault(r.family, set()).update(fs)
    for r in rev[rev.n >= C_MIN_HITS].itertuples():
        partners.setdefault(r.family, set()).add(r.hit_family)
    fam["train_partners"] = fam.family.map(lambda f: ";".join(sorted(partners.get(f, ()))))
    fam["homologous"] = (fam.frac_leaky >= FAM_FRAC) | (fam.rev_max_one_family >= C_MIN_HITS)

    mp = WORK / "leak_moves.json"
    moves = json.loads(mp.read_text()) if mp.exists() else {"to_train": [], "drop": [], "to_test": [],
                                                           "drop_seqs": [], "log": []}
    for k in ("to_test", "drop_seqs", "log"):
        moves.setdefault(k, [])
    n_before = (len(moves["to_train"]), len(moves["drop"]), len(moves["to_test"]), len(moves["drop_seqs"]))
    for r in fam[fam.homologous].itertuples():
        u = r.unit
        tr_units = sorted({fam_unit[f] for f in r.train_partners.split(";") if f in fam_unit})
        forced = bool(units.loc[u, "has_new"])
        if not forced:
            if u not in moves["to_train"]:
                moves["to_train"].append(u)
                moves["log"].append({"family": r.family, "unit": u, "action": "unit_to_train", "partners": r.train_partners,
                                     "frac_leaky": round(r.frac_leaky, 3), "rev_hits": r.rev_hits})
        else:
            ok = [t for t in tr_units if units.loc[t, "eligible"]]
            bad = [t for t in tr_units if not units.loc[t, "eligible"]]
            for t in ok:
                if t not in moves["to_test"]:
                    moves["to_test"].append(t)
                    moves["log"].append({"family": r.family, "unit": u, "action": "train_unit_to_test", "moved": t,
                                         "partners": r.train_partners})
            if bad and r.family not in moves["drop"]:
                moves["drop"].append(r.family)
                moves["log"].append({"family": r.family, "unit": u, "action": "drop_family_partner_ineligible",
                                     "partners": r.train_partners})
    homfam = set(fam.family[fam.homologous])
    iso = seq[seq.leaky & ~seq.family.isin(homfam)]
    for u in iso.uid:
        if u not in moves["drop_seqs"]:
            moves["drop_seqs"].append(u)
    n_after = (len(moves["to_train"]), len(moves["drop"]), len(moves["to_test"]), len(moves["drop_seqs"]))
    write_json(mp, moves)
    seq.to_parquet(WORK / "leak_seq.parquet", index=False)
    fam.to_parquet(WORK / "leak_family.parquet", index=False)
    st = {"test_seqs": len(seq), "test_families": int(seq.family.nunique()),
          "seqs_blast_leak": int(seq.blast_leak.sum()), "seqs_cm_leak": int(seq.cm_leak.sum()),
          "seqs_leaky": int(seq.leaky.sum()), "families_homologous": int(fam.homologous.sum()),
          "families_with_any_rev_hit": int((fam.rev_hits > 0).sum()),
          "own_family_cm_hit_rate": float(seq.cm_own_family_hit.mean()),
          "qid_quantiles": {str(q): float(seq.max_qid.quantile(q)) for q in (0.5, 0.75, 0.9, 0.95, 0.99, 1.0)},
          "moves_before": n_before, "moves_after": n_after, "peak_mem_gb": peak_mem_note()}
    write_json(WORK / "leak_stats.json", st)
    hist_p = WORK / "leak_history.json"
    hist = json.loads(hist_p.read_text()) if hist_p.exists() else []
    hist.append(st)
    write_json(hist_p, hist)
    it = len(hist)
    seq.to_parquet(WORK / f"leak_seq_iter{it}.parquet", index=False)
    fam.to_parquet(WORK / f"leak_family_iter{it}.parquet", index=False)
    print(st)
    sys.exit(3 if n_after != n_before else 0)


if __name__ == "__main__":
    main()
