"""Step 7: write the dataset (agent/, grader/) plus dev/ (dev_spectra.jsonl, dev_labels.jsonl; same formats as grader/).
agent/  candidates.parquet      smiles, inchikey14 (sorted by inchikey14; no provenance columns)
        train_spectra.parquet   spectrum_id, mzs, intensities, precursor_mz (library value, unchanged), adduct,
                                instrument_type, collision_energy, smiles, inchikey14
grader/ hidden_spectra.jsonl    spectrum_id, mzs, intensities, precursor_mz (realistic, see below), adduct,
                                instrument_type, collision_energy   -- no candidate lists
        hidden_labels.jsonl     spectrum_id, molecule_id, inchikey14, smiles + analysis metadata
        manifest.json
Hidden precursor_mz = theoretical ion m/z (precursor_formula, monoisotopic, minus electron) * (1 + eps*1e-6),
eps drawn from the per-instrument empirical error model (eps_model.json) plus U(-0.05, 0.05) ppm jitter,
rounded to 4 decimals. Fragment peaks are NOT modified.
Usage: python -I s07_write.py"""
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from rdkit import Chem  # noqa: E402

from common import OUT, RAW, SEED, WORK  # noqa: E402

E = 0.000548579909
PT = Chem.GetPeriodicTable()
# Spectrum ids are sha256("<salt>-<SEED>-<i>")[:12].
ID_SALT_HIDDEN = "spectrum-to-molecule-hidden-ids"
ID_SALT_DEV = "spectrum-to-molecule-dev-ids"
ADDUCT_ION = {"[M+H]+": 1.007276467, "[M+Na]+": 22.989218}


def fmass(f):
    return sum(PT.GetMostCommonIsotopeMass(e) * (int(n) if n else 1) for e, n in re.findall(r"([A-Z][a-z]?)(\d*)", f))


def peaks(s):
    return [float(x) for x in s.split(",")]


def cnull(x):
    return None if pd.isna(x) else x


def main():
    out = Path(OUT)
    (out / "agent").mkdir(parents=True, exist_ok=True)
    (out / "grader").mkdir(parents=True, exist_ok=True)
    coll = pd.read_parquet(f"{WORK}/coll_union.parquet")
    assert coll.ik14.is_monotonic_increasing
    coll[["smiles", "ik14"]].rename(columns={"ik14": "inchikey14"}).to_parquet(out / "agent/candidates.parquet", index=False, compression="zstd")
    added = coll.set_index("ik14").added
    split = pd.read_parquet(f"{WORK}/split.parquet")
    side = split.set_index("ik14").side
    rep = pd.read_parquet(f"{WORK}/rep_smiles.parquet").set_index("ik14")
    std = pd.read_parquet(f"{WORK}/std_msg.parquet").set_index("src_smiles")
    tani = pd.read_parquet(f"{WORK}/test_max_tani.parquet").set_index("ik14").max_tani_train
    pe = pd.read_parquet(f"{WORK}/precursor_errors.parquet").set_index("identifier")
    fr = pd.read_parquet(f"{WORK}/fragment_exactness.parquet").set_index("identifier")
    model = json.loads(Path(f"{WORK}/eps_model.json").read_text())

    df = pd.read_csv(f"{RAW}/MassSpecGym1.5.tsv", sep="\t")
    df["inchikey14"] = df.smiles.map(std.ik14)
    df["side"] = df.inchikey14.map(side)
    df["smiles_std"] = df.inchikey14.map(rep.smiles)
    keep = ["precursor_mz", "adduct", "instrument_type", "collision_energy"]

    tr = df[df.side == "train"].sample(frac=1.0, random_state=SEED).reset_index(drop=True)
    tro = pd.DataFrame({"spectrum_id": [f"T{i:06d}" for i in range(len(tr))]})
    tro["mzs"] = tr.mzs.map(peaks)
    tro["intensities"] = tr.intensities.map(peaks)
    for c in keep:
        tro[c] = tr[c]
    tro["smiles"] = tr.smiles_std
    tro["inchikey14"] = tr.inchikey14
    tro.to_parquet(out / "agent/train_spectra.parquet", index=False, compression="zstd")

    def write_queries(name, side_name, spec_path, lab_path, id_tag, mol_prefix, shuffle_seed, eps_seed):
        keep_ids = set(pd.read_parquet(f"{WORK}/{name}_spectrum_ids.parquet").identifier)
        te = df[(df.side == side_name) & df.identifier.isin(keep_ids)].sample(frac=1.0, random_state=shuffle_seed).reset_index(drop=True)
        rng = np.random.default_rng(eps_seed)
        ids = [hashlib.sha256(f"{id_tag}-{SEED}-{i}".encode()).hexdigest()[:12] for i in range(len(te))]
        mol_id = {ik: f"{mol_prefix}{k:03d}" for k, ik in enumerate(sorted(te.inchikey14.unique()))}
        n_struct_mismatch = 0
        with open(spec_path, "w") as fs, open(lab_path, "w") as fl:
            for i, r in te.iterrows():
                inst = r.instrument_type if isinstance(r.instrument_type, str) else "NA"
                samples = model[inst]["samples_ppm"]
                eps = float(samples[rng.integers(len(samples))] + rng.uniform(-0.05, 0.05))
                theo = fmass(r.precursor_formula) - E
                # cross-check against the standardised structure's neutral mass + adduct
                theo_struct = rep.mass[r.inchikey14] + ADDUCT_ION[r.adduct]
                if abs(theo_struct - theo) / theo * 1e6 > 1:
                    n_struct_mismatch += 1
                pmz = round(theo * (1 + eps * 1e-6), 4)
                fs.write(json.dumps({
                    "spectrum_id": ids[i], "mzs": peaks(r.mzs), "intensities": peaks(r.intensities),
                    "precursor_mz": pmz, "adduct": r.adduct, "instrument_type": cnull(r.instrument_type),
                    "collision_energy": cnull(r.collision_energy)}) + "\n")
                f = fr.loc[r.identifier]
                fl.write(json.dumps({
                    "spectrum_id": ids[i], "molecule_id": mol_id[r.inchikey14], "inchikey14": r.inchikey14,
                    "smiles": r.smiles_std, "max_tanimoto_to_train": round(float(tani[r.inchikey14]), 4),
                    "tanimoto_ge_0.7": bool(tani[r.inchikey14] >= 0.7), "truth_in_neither_pool": bool(added[r.inchikey14]),
                    "precursor_mz_theoretical": round(theo, 6), "precursor_mz_library": float(r.precursor_mz),
                    "library_precursor_computed": bool(pe.computed[r.identifier]), "eps_ppm_applied": round(eps, 4),
                    "precursor_mz_vs_structure_mass_ppm": round((theo_struct - theo) / theo * 1e6, 3),
                    "fragment_frac_exact": None if pd.isna(f.frac_exact) else round(float(f.frac_exact), 3),
                    "msg_identifier": r.identifier, "msg_fold": r.fold}) + "\n")
        return te, n_struct_mismatch

    (out / "dev").mkdir(parents=True, exist_ok=True)
    te, n_struct_mismatch = write_queries("hidden", "test", out / "grader/hidden_spectra.jsonl", out / "grader/hidden_labels.jsonl",
                                          ID_SALT_HIDDEN, "M", SEED + 7, SEED + 9)
    dv, n_dev_mismatch = write_queries("dev", "dev", out / "dev/dev_spectra.jsonl", out / "dev/dev_labels.jsonl",
                                       ID_SALT_DEV, "D", SEED + 17, SEED + 19)
    man = {"collection": "union (1M U 4M U truths), sorted by inchikey14", "collection_size": len(coll),
           "train_spectra": len(tro), "train_molecules": int(tro.inchikey14.nunique()),
           "hidden_spectra": len(te), "hidden_molecules": int(te.inchikey14.nunique()), "seed": SEED,
           "hidden_precursor_model": {k: {kk: vv for kk, vv in v.items() if kk != "samples_ppm"} for k, v in model.items()},
           "hidden_spectra_theo_vs_structure_mass_gt_1ppm": n_struct_mismatch,
           "dev_spectra": len(dv), "dev_molecules": int(dv.inchikey14.nunique()),
           "dev_spectra_theo_vs_structure_mass_gt_1ppm": n_dev_mismatch,
           "source": "MassSpecGym v1.5, HF revision 018a9542378725cf5620423f930608730c36dd8c"}
    (out / "grader/manifest.json").write_text(json.dumps(man, indent=1))
    print(json.dumps(man, indent=1))
    for p in sorted(x for d in ("agent", "grader", "dev") for x in (out / d).rglob("*")):
        if p.is_file():
            print(f"{p.relative_to(out)}  {p.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
