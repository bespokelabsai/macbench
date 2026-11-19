"""Step 6: the candidate collection ("union") and a provenance audit.
Collection = std(1M) U std(4M) U all training and hidden true structures, deduplicated by std ik14.
Representative SMILES per ik14 = minimal |net charge|, then lexicographically smallest standardised SMILES across all sources.
Shipped order = sorted by ik14; shipped columns = smiles, inchikey14 only.
Audit: how many truths are in neither pool ("added"), whether added molecules carry a SMILES/tautomer quirk,
and whether added-ness is predictable among MassSpecGym molecules (oracle detector, 5-fold CV AUC).
Writes WORK/coll_union.parquet (internal, with flags), WORK/rep_smiles.parquet, WORK/collection_audit.json.
Usage: python -I s06_collection.py   (in Docker)"""
import json
import sys
from multiprocessing import Pool
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from rdkit import Chem, RDLogger  # noqa: E402
from rdkit.Chem.MolStandardize import rdMolStandardize  # noqa: E402

from common import SEED, WORK  # noqa: E402

RDLogger.DisableLog("rdApp.*")
_te = None


def taut_same(smi):
    """Is this SMILES already RDKit's canonical tautomer (after stereo removal)?"""
    global _te
    if _te is None:
        _te = rdMolStandardize.TautomerEnumerator()
        _te.SetMaxTautomers(200)
        _te.SetMaxTransforms(200)
    try:
        m = Chem.MolFromSmiles(smi)
        c = _te.Canonicalize(m)
        Chem.RemoveStereochemistry(c)
        return Chem.MolToSmiles(c) == smi
    except Exception:
        return None


def string_feats(s):
    return [len(s), s.count("[nH]"), s.count("[H]"), s.count("+"), s.count("-]"), s.count("="), s.count("#"),
            sum(ch.isdigit() for ch in s), s.count("("), sum(ch.islower() for ch in s) / max(len(s), 1), s.count("O=c"),
            s.count("Oc"), s.count("N=c"), s.count("Nc")]


def main():
    cols = ["smiles", "ik14", "mass", "n_heavy", "n_chiral"]
    src = {k: pd.read_parquet(f"{WORK}/std_{k}.parquet").dropna(subset=["smiles"])[cols] for k in ["msg", "1m", "4m"]}
    split = pd.read_parquet(f"{WORK}/split.parquet")
    side = split.set_index("ik14").side
    allrows = pd.concat([src["msg"], src["1m"], src["4m"]], ignore_index=True)
    # representative per ik14: minimal |net formal charge|, ties broken by the lexicographically smallest SMILES
    # (source-independent). Charges are only computed where an ik14 has more than one distinct SMILES.
    allrows = allrows.drop_duplicates(["ik14", "smiles"]).reset_index(drop=True)
    multi_ik = allrows.ik14[allrows.ik14.duplicated(keep=False)]
    allrows["abs_charge"] = 0
    mrows = allrows.ik14.isin(set(multi_ik))
    allrows.loc[mrows, "abs_charge"] = [abs(Chem.GetFormalCharge(Chem.MolFromSmiles(x))) for x in allrows.smiles[mrows]]
    rep = allrows.sort_values(["abs_charge", "smiles"]).drop_duplicates("ik14").set_index("ik14")
    lexmin = allrows[mrows].sort_values("smiles").drop_duplicates("ik14").set_index("ik14").smiles
    changed = [k for k in lexmin.index if lexmin[k] != rep.smiles[k]]
    au_rep = {"ik14_with_multiple_std_smiles": int(multi_ik.nunique()), "rep_changed_vs_lexmin_rule": len(changed),
              "rep_changed_msg_molecules": int(sum(k in set(src["msg"].ik14) for k in changed))}
    rep[["smiles", "mass", "n_heavy", "n_chiral"]].reset_index().to_parquet(f"{WORK}/rep_smiles.parquet", index=False)
    ik_1m, ik_4m = set(src["1m"].ik14), set(src["4m"].ik14)
    tr = set(split.ik14[split.side == "train"])
    te = set(split.ik14[split.side == "test"])
    dv = set(split.ik14[split.side == "dev"])
    iks = ik_1m | ik_4m | tr | te | dv
    c = rep.loc[sorted(iks)].reset_index()  # sorted by ik14
    c["in_1m"] = c.ik14.isin(ik_1m)
    c["in_4m"] = c.ik14.isin(ik_4m)
    c["is_train"] = c.ik14.isin(tr)
    c["is_test"] = c.ik14.isin(te)
    c["added"] = ~(c.in_1m | c.in_4m)
    c.to_parquet(f"{WORK}/coll_union.parquet", index=False)
    au = {**au_rep, "collection_size": len(c), "added_total": int(c.added.sum()),
          "added_not_a_truth (should be 0)": int((c.added & ~c.is_train & ~c.is_test).sum())}
    c_dev = c.ik14.isin(dv)
    for name, m in [("hidden", c.is_test), ("dev", c_dev), ("train", c.is_train)]:
        au[f"{name}_truths"] = int(m.sum())
        au[f"{name}_in_neither_pool"] = int((m & c.added).sum())
        au[f"{name}_in_1m_only"] = int((m & c.in_1m & ~c.in_4m).sum())
        au[f"{name}_in_4m_only"] = int((m & ~c.in_1m & c.in_4m).sum())
        au[f"{name}_in_both"] = int((m & c.in_1m & c.in_4m).sum())
    # how many ik14 have >1 std SMILES, and among added vs base, whose representative came from which source
    multi = allrows.drop_duplicates(["ik14", "smiles"]).groupby("ik14").size()
    au["ik14_with_multiple_std_smiles"] = int((multi > 1).sum())
    msg_smiles = set(src["msg"].smiles)
    au["train_truth_rep_equals_msg_std_smiles"] = float(c[c.is_train].smiles.isin(msg_smiles).mean())
    au["hidden_truth_rep_equals_msg_std_smiles"] = float(c[c.is_test].smiles.isin(msg_smiles).mean())

    # ---- quirk check 1: canonical-tautomer agreement by group (samples) ----
    rng = np.random.default_rng(SEED)
    groups = {
        "train_truth_added": c[c.is_train & c.added],
        "train_truth_in_pool": c[c.is_train & ~c.added],
        "hidden_truth_added": c[c.is_test & c.added],
        "hidden_truth_in_pool": c[c.is_test & ~c.added],
        "pool_non_truth": c[~c.is_train & ~c.is_test],
    }
    smp = {k: g.smiles.iloc[rng.choice(len(g), min(4000, len(g)), replace=False)].tolist() for k, g in groups.items() if len(g)}
    with Pool(8) as pool:
        tq = {k: pool.map(taut_same, v, chunksize=50) for k, v in smp.items()}
    au["canonical_tautomer_frac_by_group"] = {k: round(float(np.mean([x for x in v if x is not None])), 4) for k, v in tq.items()}
    au["string_feature_means_by_group"] = {k: np.round(np.mean([string_feats(s) for s in v], 0), 3).tolist() for k, v in smp.items()}
    au["string_feature_names"] = ["len", "[nH]", "[H]", "+", "-]", "=", "#", "digits", "(", "aromatic_frac", "O=c", "Oc", "N=c", "Nc"]

    # ---- quirk check 2: is added-ness predictable among MSG training truths? (oracle labels agents do not have) ----
    import lightgbm as lgb
    from sklearn.model_selection import StratifiedKFold
    from sklearn.metrics import roc_auc_score
    from rdkit.Chem import rdFingerprintGenerator
    gen = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=1024)
    t = c[c.is_train].reset_index(drop=True)

    def feats(s, mass, nh, nc):
        m = Chem.MolFromSmiles(s)
        return np.r_[gen.GetCountFingerprintAsNumPy(m), mass, nh, nc, string_feats(s)].astype(np.float32)
    X = np.stack([feats(s, a, b, d) for s, a, b, d in zip(t.smiles, t.mass, t.n_heavy, t.n_chiral)])
    y = t.added.to_numpy().astype(int)
    aucs = {}
    for name, cols_ in [("structure+string", slice(None)), ("string_only", slice(1024 + 3, None)), ("mass_heavy_chiral", slice(1024, 1027))]:
        p = np.zeros(len(y))
        for trn, tst in StratifiedKFold(5, shuffle=True, random_state=SEED).split(X, y):
            mdl = lgb.LGBMClassifier(n_estimators=300, learning_rate=0.05, num_leaves=31, n_jobs=8, verbose=-1, random_state=SEED)
            mdl.fit(X[trn][:, cols_], y[trn])
            p[tst] = mdl.predict_proba(X[tst][:, cols_])[:, 1]
        aucs[name] = round(float(roc_auc_score(y, p)), 4)
    au["oracle_detector_added_vs_in_pool_among_train_truths_cv_auc"] = aucs
    au["train_truths_added_frac"] = float(y.mean())
    Path(f"{WORK}/collection_audit.json").write_text(json.dumps(au, indent=1))
    print(json.dumps(au, indent=1))


if __name__ == "__main__":
    main()
