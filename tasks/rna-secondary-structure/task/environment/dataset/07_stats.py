"""Step 7: statistics for the report (work/report_stats.json)."""
import json

import numpy as np
import pandas as pd

from common import OUT, TEST_MAX_LEN, WORK, write_json

Q = [0, 0.05, 0.25, 0.5, 0.75, 0.95, 1.0]


def qs(s):
    return {str(q): float(np.round(s.quantile(q), 3)) for q in Q}


def coarse_type(t):
    parts = [p.strip() for p in t.split(";") if p.strip()]
    if not parts:
        return "unknown"
    if parts[0] == "Gene" and len(parts) > 1:
        if parts[1] == "snRNA" and len(parts) > 2:
            return "snoRNA" if parts[2] == "snoRNA" else "snRNA/" + parts[2]
        return parts[1]
    return parts[0]


def main():
    sp = pd.read_parquet(WORK / "split.parquet")
    sp["ctype"] = sp.rna_type.map(coarse_type)
    sp["has_pk"] = sp.n_pk_pairs > 0
    out = {}
    for name in ["train", "test"]:
        d = sp[sp.split == name]
        fams = d.groupby("family").first()
        out[name] = {
            "sequences": len(d), "families": int(d.family.nunique()), "clans": int(d.clan.dropna().nunique()),
            "clanless_families": int(d[d.clan.isna()].family.nunique()),
            "families_in_clan": int(d[d.clan.notna()].family.nunique()),
            "length": qs(d.length), "length_gt_500": int((d.length > TEST_MAX_LEN).sum()),
            "length_gt_1000": int((d.length > 1000).sum()),
            "len_500_1000_seqs": int(((d.length > 500) & (d.length <= 1000)).sum()),
            "len_500_1000_families": int(d[(d.length > 500) & (d.length <= 1000)].family.nunique()),
            "short_span_pairs_dropped": int(d.n_short_span_dropped.sum()) if "n_short_span_dropped" in d else None,
            "seqs_with_short_span_dropped": int((d.n_short_span_dropped > 0).sum()) if "n_short_span_dropped" in d else None,
            "length_hist": d.length.pipe(pd.cut, [0, 50, 100, 150, 200, 300, 500, 750, 1000, 2000, 5000]).value_counts(sort=False).astype(int).rename(str).to_dict(),
            "n_pairs": qs(d.n_pairs), "pairs_per_nt": qs(d.n_pairs / d.length),
            "pk_seq_share": float(d.has_pk.mean()), "pk_pair_share": float(d.n_pk_pairs.sum() / d.n_pairs.sum()),
            "pk_family_share": float(d.groupby("family").has_pk.any().mean()),
            "pages_used": d.n_pages.value_counts().sort_index().astype(int).rename(str).to_dict(),
            "seqs_with_nonacgu": int((d.n_nonacgu > 0).sum()),
            "per_family_seqs": qs(d.groupby("family").size()),
            "type_by_seq": (d.ctype.value_counts(normalize=True).round(4)).to_dict(),
            "type_by_family": (fams.ctype.value_counts(normalize=True).round(4)).to_dict(),
            "type_family_counts": fams.ctype.value_counts().astype(int).to_dict(),
        }
    te = sp[sp.split == "test"]
    out["test_new_15_1"] = {"sequences": int(te.new_in_15_1.sum()), "families": int(te[te.new_in_15_1].family.nunique()),
                            "plastid_families": int(te[te.new_in_15_1 & te.family_name.str.startswith("Plastid")].family.nunique()),
                            "plastid_sequences": int((te.new_in_15_1 & te.family_name.str.startswith("Plastid")).sum())}
    out["test_largest_clans_by_seqs"] = te.groupby("clan").size().sort_values(ascending=False).head(10).astype(int).to_dict()
    out["split_counts"] = sp.split.value_counts().astype(int).to_dict()
    rr = sp[sp.rna_type.str.contains("rRNA")]
    out["rrna_families"] = rr.groupby(["family", "family_name"]).agg(n=("uid", "size"), med_len=("length", "median"), split=("split", "first")).reset_index().to_dict("records")
    hist = json.loads((WORK / "leak_history.json").read_text())
    out["leak_history"] = hist
    lf = WORK / "leak_seq.parquet"
    if lf.exists():
        ls = pd.read_parquet(lf)
        out["final_leak"] = {
            "no_blast_hit_share": float((ls.max_qid == 0).mean()),
            "max_qid": qs(ls.max_qid),
            "max_qid_hist": ls.max_qid.pipe(pd.cut, [-0.01, 0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 1.0]).value_counts(sort=False).astype(int).rename(str).to_dict(),
            "best_hit_pident_given_hit": qs(ls[ls.max_qid > 0].pident),
            "best_hit_qcov_given_hit": qs(ls[ls.max_qid > 0].qcov),
            "own_family_cm_hit_rate": float(ls.cm_own_family_hit.mean()),
            "cm_train_hits": int(ls.cm_leak.sum()),
        }
    for f in ["project_stats", "dedup_stats", "homology_stats", "split_stats"]:
        out[f] = json.loads((WORK / f"{f}.json").read_text())
    write_json(WORK / "report_stats.json", out)
    print(json.dumps({k: out[k] for k in ["train", "test"]}, indent=1)[:6000])


if __name__ == "__main__":
    main()
