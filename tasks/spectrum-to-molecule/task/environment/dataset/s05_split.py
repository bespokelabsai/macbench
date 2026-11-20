"""Step 5: hidden (1,000 molecules) / dev (~300) / train split.
Rules for hidden and dev molecules:
  - outside the giant MCES<10 component;
  - in the 1M or 4M pool (no "added" molecule);
  - no charge clash: net charge 0 in both the MassSpecGym standardised SMILES and the collection representative
    (representative = minimal |net charge|, then lexicographic -- the rule used by s06), and no spectrum whose
    precursor_formula disagrees with representative mass + adduct by > 5 ppm;
  - >= 1 spectrum left after dropping spectra with >= 50% exact fragment peaks.
Selection:
  1. a first draw of 200 molecules, made as for the dataset's earlier 200-molecule hidden set (default_rng(SEED)
     draw, invalid ones replaced, with the lexicographic representative for the charge check); those that pass the
     rules above are kept;
  2. hidden is filled to 1,000 from default_rng(SEED+100).permutation of the remaining valid eligible molecules;
  3. dev (300) from default_rng(SEED+200).permutation of valid eligible molecules that are not hidden and have no
     MCES<10 edge to any hidden molecule.
Train = all molecules minus hidden, dev, and anything with MCES<10 to a hidden or dev molecule.
Writes WORK/split.parquet (side: test/dev/train/dropped), WORK/hidden_spectrum_ids.parquet,
WORK/dev_spectrum_ids.parquet, WORK/test_max_tani.parquet (hidden and dev), WORK/split_stats.json.
Usage: python -I s05_split.py   (in Docker)"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from rdkit import Chem, DataStructs  # noqa: E402
from scipy.sparse import coo_matrix  # noqa: E402

from common import RAW, SEED, WORK, morgan_bits  # noqa: E402

N_FIRST, N_TEST, N_DEV = 200, 1000, 300
E = 0.000548579909
PT = Chem.GetPeriodicTable()
ADDUCT_ION = {"[M+H]+": 1.007276467, "[M+Na]+": 22.989218}


def fmass(f):
    return sum(PT.GetMostCommonIsotopeMass(e) * (int(n) if n else 1) for e, n in re.findall(r"([A-Z][a-z]?)(\d*)", f))


def q(x, qs=(0, .05, .25, .5, .75, .95, 1)):
    return {str(k): round(float(np.quantile(x, k)), 4) for k in qs}


def charge_of(s):
    return Chem.GetFormalCharge(Chem.MolFromSmiles(s))


def main():
    st = {}
    df = pd.read_csv(f"{RAW}/MassSpecGym1.5.tsv", sep="\t",
                     usecols=["identifier", "smiles", "fold", "adduct", "instrument_type", "precursor_formula"])
    std = pd.read_parquet(f"{WORK}/std_msg.parquet").set_index("src_smiles")
    df["ik14"] = df.smiles.map(std.ik14)
    df["std_smiles"] = df.smiles.map(std.smiles)
    df["mass"] = df.smiles.map(std.mass)
    z = np.load(f"{WORK}/mces_graph.npz")
    nodes, lab, a, b, giant = z["nodes"], z["lab"], z["a"], z["b"], int(z["giant"])
    N = len(nodes)
    g = coo_matrix((np.ones(len(a), dtype=np.int8), (a, b)), shape=(N, N)).tocsr()
    g = ((g + g.T) > 0).astype(np.int8).tocsr()
    eligible = np.nonzero(lab != giant)[0]

    # ---- rule inputs ----
    p1 = pd.read_parquet(f"{WORK}/std_1m.parquet", columns=["smiles", "ik14", "mass"]).dropna()
    p4 = pd.read_parquet(f"{WORK}/std_4m.parquet", columns=["smiles", "ik14", "mass"]).dropna()
    in_pool = np.isin(nodes, np.array(list(set(p1.ik14) | set(p4.ik14))))
    smi_of = df.drop_duplicates("ik14").set_index("ik14").std_smiles
    msg_ik = set(nodes)
    allr = pd.concat([pd.read_parquet(f"{WORK}/std_msg.parquet", columns=["smiles", "ik14", "mass"]),
                      p1[p1.ik14.isin(msg_ik)], p4[p4.ik14.isin(msg_ik)]]).drop_duplicates(["ik14", "smiles"])
    allr["abs_charge"] = [abs(charge_of(s)) for s in allr.smiles]
    rep_lex = allr.sort_values("smiles").drop_duplicates("ik14").set_index("ik14")            # first-draw rule
    rep = allr.sort_values(["abs_charge", "smiles"]).drop_duplicates("ik14").set_index("ik14")  # the rule (= s06)
    charge_msg = np.array([charge_of(smi_of[x]) for x in nodes])
    df["theo"] = df.precursor_formula.map(fmass) - E

    def clash(repr_):
        ch = np.array([charge_of(repr_.smiles[x]) for x in nodes])
        mm = ((df.ik14.map(repr_.mass) + df.adduct.map(ADDUCT_ION)) - df.theo) / df.theo * 1e6
        bad = set(df.ik14[mm.abs() > 5])
        return (ch != 0) | (charge_msg != 0) | np.isin(nodes, np.array(list(bad))), ch
    clash_first, _ = clash(rep_lex)
    clash_rep, ch_rep = clash(rep)
    fr = pd.read_parquet(f"{WORK}/fragment_exactness.parquet")
    fr = fr[fr.side == "eligible"]
    df["exact_frag"] = df.identifier.isin(set(fr.identifier[fr.frac_exact.fillna(0) >= 0.5]))
    n_ok = df[~df.exact_frag].groupby("ik14").size()
    has_ok = np.array([n_ok.get(x, 0) > 0 for x in nodes])
    valid_first = in_pool & ~clash_first & has_ok
    valid = in_pool & ~clash_rep & has_ok
    st["eligible_molecules"] = int(len(eligible))
    st["eligible_valid_first_draw_rules"] = int(valid_first[eligible].sum())
    st["eligible_valid"] = int(valid[eligible].sum())
    st["msg_molecules_charge_clash_first_draw_rep"] = int(clash_first.sum())
    st["msg_molecules_charge_clash"] = int(clash_rep.sum())

    # ---- 1. the first 200 ----
    rng = np.random.default_rng(SEED)
    first = np.sort(rng.choice(eligible, size=N_FIRST, replace=False))
    kept = first[valid_first[first]]
    rest = np.setdiff1d(eligible[valid_first[eligible]], first)
    first200 = np.sort(np.r_[kept, rng.permutation(rest)[: N_FIRST - len(kept)]])
    first_ok = first200[valid[first200]]
    st["first_draw_valid"] = int(len(first_ok))
    # ---- 2. fill hidden to 1,000 ----
    pool_h = np.setdiff1d(eligible[valid[eligible]], first_ok)
    add = np.random.default_rng(SEED + 100).permutation(pool_h)[: N_TEST - len(first_ok)]
    test = np.sort(np.r_[first_ok, add])
    assert len(test) == N_TEST
    # ---- 3. dev ----
    nb_test = np.unique(g[test].indices)
    pool_d = np.setdiff1d(eligible[valid[eligible]], np.r_[test, nb_test])
    st["dev_pool_size"] = int(len(pool_d))
    dev = np.sort(np.random.default_rng(SEED + 200).permutation(pool_d)[:N_DEV])
    assert not np.intersect1d(dev, test).size and not g[dev][:, test].nnz
    nb = np.unique(np.r_[nb_test, g[dev].indices])
    side = np.full(N, "train", dtype=object)
    side[np.setdiff1d(nb, np.r_[test, dev])] = "dropped"
    side[test] = "test"
    side[dev] = "dev"
    tr_idx = np.nonzero(side == "train")[0]
    assert not g[test][:, tr_idx].nnz and not g[dev][:, tr_idx].nnz
    assert valid[test].all() and valid[dev].all()
    fold_of = df.groupby("ik14").fold.agg(lambda x: "/".join(sorted(set(x))))
    sizes = np.bincount(lab)
    sp = pd.DataFrame({"ik14": nodes, "side": side, "comp": lab, "comp_size": sizes[lab], "in_pool": in_pool,
                       "in_first_draw": np.isin(np.arange(N), first200)})
    sp["orig_fold"] = sp.ik14.map(fold_of)
    sp.to_parquet(f"{WORK}/split.parquet", index=False)

    # ---- spectra ----
    df["side"] = df.ik14.map(sp.set_index("ik14").side)
    for s, fname in [("test", "hidden"), ("dev", "dev")]:
        d0 = df[df.side == s]
        st[f"{fname}_spectra_before_fragment_drop"] = len(d0)
        st[f"{fname}_spectra_dropped_exact_fragments"] = int(d0.exact_frag.sum())
        pd.DataFrame({"identifier": d0.identifier[~d0.exact_frag]}).to_parquet(f"{WORK}/{fname}_spectrum_ids.parquet", index=False)
    keep = set(pd.read_parquet(f"{WORK}/hidden_spectrum_ids.parquet").identifier) | set(pd.read_parquet(f"{WORK}/dev_spectrum_ids.parquet").identifier)
    for s in ["test", "dev", "train", "dropped"]:
        dd = df[(df.side == s) & (df.identifier.isin(keep) | ~df.side.isin(["test", "dev"]))]
        st[f"{s}_spectra"] = len(dd)
        st[f"{s}_molecules"] = int(dd.ik14.nunique())
        st[f"{s}_adducts"] = dd.adduct.value_counts().to_dict()
        st[f"{s}_instrument"] = dd.instrument_type.fillna("NA").value_counts().to_dict()
        st[f"{s}_mol_mass_q"] = q(dd.drop_duplicates("ik14").mass)
        st[f"{s}_spectra_per_mol_q"] = q(dd.groupby("ik14").size())
    for s in ["test", "dev"]:
        st[f"{s}_orig_fold"] = sp[sp.side == s].orig_fold.value_counts().to_dict()
        st[f"{s}_singletons"] = int((sp[sp.side == s].comp_size == 1).sum())
    st["first_draw_in_hidden"] = int(sp[(sp.side == "test") & sp.in_first_draw].shape[0])
    # training loss relative to the full MassSpecGym set
    st["train_loss_vs_all_msg"] = {"spectra": int(len(df) - st["train_spectra"]), "molecules": int(N - st["train_molecules"])}

    # ---- Tanimoto to training ----
    trf = [morgan_bits(smi_of[x]) for x in sp[sp.side == "train"].ik14]
    rows = []
    for s in ["test", "dev"]:
        ik = sp[sp.side == s].ik14.tolist()
        mx = np.array([max(DataStructs.BulkTanimotoSimilarity(morgan_bits(smi_of[x]), trf)) for x in ik])
        rows += list(zip(ik, mx, [s] * len(ik)))
        st[f"{s}_max_tanimoto_to_train_q"] = q(mx)
        st[f"{s}_max_tanimoto_ge_0.7"] = int((mx >= 0.7).sum())
        st[f"{s}_max_tanimoto_hist_0.1"] = np.histogram(mx, bins=np.linspace(0, 1, 11))[0].tolist()
    pd.DataFrame(rows, columns=["ik14", "max_tani_train", "side"]).to_parquet(f"{WORK}/test_max_tani.parquet", index=False)
    Path(f"{WORK}/split_stats.json").write_text(json.dumps(st, indent=1))
    print(json.dumps(st, indent=1))


if __name__ == "__main__":
    main()
