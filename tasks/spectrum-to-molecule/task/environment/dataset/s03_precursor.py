"""Step 3: precursor m/z accuracy model.
For every MassSpecGym spectrum: theoretical ion m/z from precursor_formula (monoisotopic, minus one electron),
error in Da and ppm, number of decimals written. A precursor is "computed" if |err| < 0.1 mDa or within
0.05 mDa of the +/- electron-mass convention offset. The calibration set is "measured" precursors written
with >= 4 decimals (fewer decimals = rounding error dominates). Per instrument, the error model is the
empirical signed ppm distribution of that set, truncated at its 99th percentile of |ppm|. NA instrument
uses the pooled Orbitrap+QTOF model.
Writes WORK/precursor_errors.parquet (identifier, err_da, err_ppm, decimals, computed) and WORK/eps_model.json.
Usage: python -I s03_precursor.py"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from rdkit import Chem  # noqa: E402

from common import RAW, WORK  # noqa: E402

E = 0.000548579909
PT = Chem.GetPeriodicTable()


def fmass(f):
    return sum(PT.GetMostCommonIsotopeMass(e) * (int(n) if n else 1) for e, n in re.findall(r"([A-Z][a-z]?)(\d*)", f))


def q(x, qs):
    return [round(float(np.quantile(x, k)), 3) for k in qs]


def main():
    df = pd.read_csv(f"{RAW}/MassSpecGym1.5.tsv", sep="\t", dtype={"precursor_mz": str},
                     usecols=["identifier", "precursor_formula", "precursor_mz", "adduct", "instrument_type"])
    df["decimals"] = df.precursor_mz.str.split(".").str[1].str.len().fillna(0).astype(int)
    df["precursor_mz"] = df.precursor_mz.astype(float)
    df["theo"] = df.precursor_formula.map(fmass) - E
    df["err_da"] = df.precursor_mz - df.theo
    df["err_ppm"] = df.err_da / df.theo * 1e6
    df["computed"] = (df.err_da.abs() < 1e-4) | ((df.err_da - E).abs() < 5e-5) | ((df.err_da + E).abs() < 5e-5)
    df["instrument"] = df.instrument_type.fillna("NA")
    df[["identifier", "err_da", "err_ppm", "decimals", "computed", "instrument"]].to_parquet(f"{WORK}/precursor_errors.parquet", index=False)
    st = {"n": len(df), "computed_frac": float(df.computed.mean()),
          "computed_frac_by_instrument": df.groupby("instrument").computed.mean().round(3).to_dict(),
          "decimals_counts": df.decimals.clip(upper=6).value_counts().sort_index().to_dict(),
          "computed_frac_by_decimals": df.groupby(df.decimals.clip(upper=6)).computed.mean().round(3).to_dict(),
          "near_electron_offset_frac": float((((df.err_da - E).abs() < 5e-5) | ((df.err_da + E).abs() < 5e-5)).mean())}
    st["measured_abs_ppm_by_decimals_q50_90_95_99"] = {
        int(d): q(g[~g.computed].err_ppm.abs(), (.5, .9, .95, .99)) for d, g in df.groupby(df.decimals.clip(upper=6))}
    cal = df[(~df.computed) & (df.decimals >= 4)]
    model = {}
    for inst in ["Orbitrap", "QTOF"]:
        x = cal[cal.instrument == inst].err_ppm.to_numpy()
        cap = float(np.quantile(np.abs(x), 0.99))
        xs = x[np.abs(x) <= cap]
        model[inst] = {"n_calibration": int(len(x)), "clip_abs_ppm": round(cap, 2), "samples_ppm": xs.round(4).tolist(),
                       "abs_q50_90_95_99_after_clip": q(np.abs(xs), (.5, .9, .95, .99)),
                       "signed_q01_05_50_95_99_after_clip": q(xs, (.01, .05, .5, .95, .99)),
                       "mean": round(float(xs.mean()), 3), "sd": round(float(xs.std()), 3)}
    pooled = np.array(model["Orbitrap"]["samples_ppm"] + model["QTOF"]["samples_ppm"])
    model["NA"] = {"n_calibration": int(len(pooled)), "clip_abs_ppm": max(model["Orbitrap"]["clip_abs_ppm"], model["QTOF"]["clip_abs_ppm"]),
                   "samples_ppm": pooled.round(4).tolist(), "abs_q50_90_95_99_after_clip": q(np.abs(pooled), (.5, .9, .95, .99)),
                   "note": "pooled Orbitrap+QTOF (NA instrument type)"}
    st["calibration_rule"] = "measured (not computed) precursors written with >= 4 decimals; signed ppm truncated at per-instrument P99 of |ppm|"
    st["model_summary"] = {k: {kk: vv for kk, vv in v.items() if kk != "samples_ppm"} for k, v in model.items()}
    # also show the 3-decimal measured group per instrument (rounding-dominated) for reference
    st["measured_3dec_abs_ppm_by_instrument_q50_95"] = {
        i: q(g.err_ppm.abs(), (.5, .95)) for i, g in df[(~df.computed) & (df.decimals == 3)].groupby("instrument")}
    Path(f"{WORK}/eps_model.json").write_text(json.dumps(model))
    Path(f"{WORK}/precursor_stats.json").write_text(json.dumps(st, indent=1))
    print(json.dumps(st, indent=1))


if __name__ == "__main__":
    main()
