"""Step 4: are fragment m/z values measured or computed?
For each spectrum: enumerate all subformulas of the precursor ion formula, ion m/z = subformula mass - electron.
Per peak: signed error (Da) to the nearest subformula ion. A peak is "exact" if |err| < 0.1 mDa (the same
threshold that flags computed precursors). Null: the same peaks shifted by +3.7 mDa, which keeps the local
subformula density but removes any real match, giving the chance rate of an "exact" hit.
Runs on all spectra of molecules outside the giant MCES component (the hidden-eligible pool) and a
20,000-spectrum random sample of giant-component (always-training) spectra.
Writes WORK/fragment_exactness.parquet (identifier, side, n_peaks, n_scored, frac_exact, frac_exact_null,
median_abs_err_mda, peak_decimals_median, skipped) and WORK/fragment_stats.json.
Usage: python -I s04_fragments.py   (in Docker)"""
import json
import re
import sys
from multiprocessing import Pool
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from rdkit import Chem  # noqa: E402

from common import RAW, SEED, WORK  # noqa: E402

E = 0.000548579909
PT = Chem.GetPeriodicTable()
MAX_COMBOS = 40_000_000
_cache = {}


def sub_masses(formula):
    if formula in _cache:
        return _cache[formula]
    parts = [(e, int(n) if n else 1) for e, n in re.findall(r"([A-Z][a-z]?)(\d*)", formula)]
    if np.prod([n + 1 for _, n in parts], dtype=float) > MAX_COMBOS:
        _cache[formula] = None
        return None
    m = np.zeros(1)
    for e, n in parts:
        m = (m[:, None] + np.arange(n + 1) * PT.GetMostCommonIsotopeMass(e)).ravel()
    m = np.unique(m) - E
    if len(_cache) > 200:
        _cache.clear()
    _cache[formula] = m
    return m


def nearest_err(sm, x):
    i = np.clip(np.searchsorted(sm, x), 1, len(sm) - 1)
    lo, hi = sm[i - 1], sm[i]
    return np.where(np.abs(x - lo) < np.abs(hi - x), x - lo, x - hi)


def one(args):
    ident, formula, mzs_s, prec = args
    mzs_str = mzs_s.split(",")
    mz = np.array([float(v) for v in mzs_str])
    dec = np.median([len(v.split(".")[1]) if "." in v else 0 for v in mzs_str])
    sm = sub_masses(formula)
    if sm is None:
        return ident, len(mz), 0, np.nan, np.nan, np.nan, dec, True
    k = mz < prec + 0.5  # fragments must not exceed the precursor (drops impossible peaks)
    x = mz[k]
    if len(x) == 0:
        return ident, len(mz), 0, np.nan, np.nan, np.nan, dec, False
    err = nearest_err(sm, x)
    errn = nearest_err(sm, x + 0.0037)
    return (ident, len(mz), len(x), float((np.abs(err) < 1e-4).mean()), float((np.abs(errn) < 1e-4).mean()),
            float(np.median(np.abs(err)) * 1e3), dec, False)


def main():
    df = pd.read_csv(f"{RAW}/MassSpecGym1.5.tsv", sep="\t", usecols=["identifier", "smiles", "mzs", "precursor_formula", "precursor_mz"])
    std = pd.read_parquet(f"{WORK}/std_msg.parquet").set_index("src_smiles")
    elig = set(pd.read_parquet(f"{WORK}/eligible.parquet").ik14)
    df["ik14"] = df.smiles.map(std.ik14)
    df["side"] = np.where(df.ik14.isin(elig), "eligible", "giant")
    hid = df[df.side == "eligible"]
    tr = df[df.side == "giant"].sample(20000, random_state=SEED)
    sub = pd.concat([hid, tr]).sort_values("precursor_formula")  # group by formula so the per-worker cache helps
    with Pool(8) as pool:
        res = pool.map(one, list(zip(sub.identifier, sub.precursor_formula, sub.mzs, sub.precursor_mz)), chunksize=64)
    out = pd.DataFrame(res, columns=["identifier", "n_peaks", "n_scored", "frac_exact", "frac_exact_null",
                                     "median_abs_err_mda", "peak_decimals_median", "skipped"])
    out["side"] = out.identifier.map(sub.set_index("identifier").side)
    out["ik14"] = out.identifier.map(sub.set_index("identifier").ik14)
    pe = pd.read_parquet(f"{WORK}/precursor_errors.parquet").set_index("identifier")
    out["prec_computed"] = out.identifier.map(pe.computed)
    out["instrument"] = out.identifier.map(pe.instrument)
    out.to_parquet(f"{WORK}/fragment_exactness.parquet", index=False)
    st = {"skipped_too_many_subformulas": int(out.skipped.sum())}
    o = out[~out.skipped & (out.n_scored >= 3)]
    st["n_scored_spectra_ge3_peaks"] = int(len(o))
    o = o.assign(frag_exact=o.frac_exact >= 0.8)
    for s, g in o.groupby("side"):
        st[s] = {
            "n": int(len(g)),
            "frac_exact_mean": round(float(g.frac_exact.mean()), 3),
            "null_frac_exact_mean": round(float(g.frac_exact_null.mean()), 3),
            "spectra_with_>=80%_exact_peaks": round(float(g.frag_exact.mean()), 3),
            "by_precursor_group": {("computed" if c else "measured"): {
                "n": int(len(h)), "frac_exact_mean": round(float(h.frac_exact.mean()), 3),
                "null_mean": round(float(h.frac_exact_null.mean()), 3),
                "spectra_>=80%_exact": round(float(h.frag_exact.mean()), 3),
                "median_abs_err_mDa_q50": round(float(h.median_abs_err_mda.median()), 3)} for c, h in g.groupby("prec_computed")},
            "by_instrument_spectra_>=80%_exact": g.groupby("instrument").frag_exact.mean().round(3).to_dict(),
            "by_peak_decimals_spectra_>=80%_exact": g.groupby(g.peak_decimals_median.clip(upper=6)).frag_exact.agg(["mean", "size"]).round(3).to_dict(),
            "frac_exact_hist_0.1": np.histogram(g.frac_exact, bins=np.linspace(0, 1, 11))[0].tolist(),
        }
    Path(f"{WORK}/fragment_stats.json").write_text(json.dumps(st, indent=1))
    print(json.dumps(st, indent=1))


if __name__ == "__main__":
    main()
