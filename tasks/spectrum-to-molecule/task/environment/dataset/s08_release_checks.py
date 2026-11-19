"""Step 8: integrity checks on the release in OUT (agent/, grader/, dev/; reads only those, plus labels).
Usage: python -I s08_release_checks.py"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from rdkit import Chem  # noqa: E402

from common import OUT, WORK, standardize  # noqa: E402


def main():
    coll = pd.read_parquet(f"{OUT}/agent/candidates.parquet")
    tr = pd.read_parquet(f"{OUT}/agent/train_spectra.parquet")
    hs = pd.read_json(f"{OUT}/grader/hidden_spectra.jsonl", lines=True)
    lab = pd.read_json(f"{OUT}/grader/hidden_labels.jsonl", lines=True)
    out = {
        "collection_columns": list(coll.columns),
        "collection_rows": len(coll),
        "collection_unique_ik14": int(coll.inchikey14.nunique()),
        "collection_sorted_by_ik14": bool(coll.inchikey14.is_monotonic_increasing),
        "hidden_spectra_columns": list(hs.columns),
        "hidden_rows": len(hs), "labels_rows": len(lab),
        "label_ids_match": bool(set(hs.spectrum_id) == set(lab.spectrum_id)),
        "hidden_truths_in_collection": float(lab.inchikey14.isin(set(coll.inchikey14)).mean()),
        "hidden_truth_smiles_in_collection": float(lab.smiles.isin(set(coll.smiles)).mean()),
        "hidden_ik14_in_train_labels": int(len(set(lab.inchikey14) & set(tr.inchikey14))),
        "train_smiles_in_collection": bool(tr.smiles.isin(set(coll.smiles)).all()),
    }
    ds = pd.read_json(f"{OUT}/dev/dev_spectra.jsonl", lines=True)
    dl = pd.read_json(f"{OUT}/dev/dev_labels.jsonl", lines=True)
    out.update({
        "dev_rows": len(ds), "dev_molecules": int(dl.inchikey14.nunique()),
        "dev_columns_equal_hidden": list(ds.columns) == list(hs.columns) and list(dl.columns) == list(lab.columns),
        "dev_hidden_ik14_overlap": int(len(set(dl.inchikey14) & set(lab.inchikey14))),
        "dev_ik14_in_train_labels": int(len(set(dl.inchikey14) & set(tr.inchikey14))),
        "dev_truths_in_collection": float(dl.inchikey14.isin(set(coll.inchikey14)).mean()),
        "dev_hidden_spectrum_id_overlap": int(len(set(ds.spectrum_id) & set(hs.spectrum_id))),
        "dev_label_ids_match": bool(set(ds.spectrum_id) == set(dl.spectrum_id)),
        "dev_or_hidden_charged_label_smiles": int(sum(Chem.GetFormalCharge(Chem.MolFromSmiles(x)) != 0 for x in pd.concat([dl.smiles, lab.smiles]).unique())),
        "dev_max_fragment_frac_exact": float(dl.fragment_frac_exact.max()),
        "hidden_max_fragment_frac_exact": float(lab.fragment_frac_exact.max()),
    })
    smp = coll.smiles.sample(20000, random_state=1).tolist() + lab.smiles.drop_duplicates().tolist() + dl.smiles.drop_duplicates().tolist()
    out["standardiser_fixed_point_frac"] = float(np.mean([standardize(s)[0] == s for s in smp]))
    key_tr = set(zip(tr.mzs.map(lambda x: tuple(np.round(x, 3))), tr.intensities.map(lambda x: tuple(np.round(x, 3)))))
    out["hidden_peaklists_identical_to_a_training_spectrum"] = int(sum(
        (tuple(np.round(m, 3)), tuple(np.round(i, 3))) in key_tr for m, i in zip(hs.mzs, hs.intensities)))
    m = hs.merge(lab, on="spectrum_id")
    err = (m.precursor_mz - m.precursor_mz_theoretical) / m.precursor_mz_theoretical * 1e6
    m["inst"] = m.instrument_type.fillna("NA")
    out["hidden_precursor_err_ppm_abs_q50_95_max_by_instrument"] = {
        k: [round(float(np.quantile(g.abs(), x)), 3) for x in (.5, .95, 1)] for k, g in err.groupby(m.inst)}
    out["hidden_precursor_err_lt_0.1ppm_frac"] = float((err.abs() < 0.1).mean())
    out["hidden_precursor_decimals"] = hs.precursor_mz.map(lambda v: len(repr(v).split(".")[1])).value_counts().to_dict()
    Path(f"{WORK}/release_checks.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
