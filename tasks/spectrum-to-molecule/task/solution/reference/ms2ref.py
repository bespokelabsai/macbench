"""Reference ranker for spectrum-to-molecule: library matching (a simple baseline, not tuned).

  --build            neutral monoisotopic mass of every collection molecule -> masses.npy
  --method library   candidates = collection molecules within the precursor tolerance; score =
                     sum over the 10 most cosine-similar training spectra (0.01 Da bins, sqrt
                     intensities) of cosine x Morgan-2048 Tanimoto to that spectrum's structure
  --method random    same candidates, random order (seeded per spectrum)

Tolerance from the stated accuracy, upper ends: Orbitrap 6 ppm, QTOF / unknown 22 ppm.
Usage: predict <spectra.jsonl> <out.jsonl>  (see solve.sh)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import zlib
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd
from rdkit import Chem, DataStructs, RDLogger
from rdkit.Chem import rdFingerprintGenerator
from rdkit.Chem.rdMolDescriptors import CalcExactMolWt
from scipy import sparse

RDLogger.DisableLog("rdApp.*")
HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
ADDUCT = {"[M+H]+": 1.007276467, "[M+Na]+": 22.989218}
PPM = {"Orbitrap": 6.0, "QTOF": 22.0}
PPM_UNKNOWN = 22.0
K = 20


def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, file=sys.stderr, flush=True)


def _masses(chunk):
    RDLogger.DisableLog("rdApp.*")
    out = np.full(len(chunk), np.nan)
    for i, s in enumerate(chunk):
        m = Chem.MolFromSmiles(s)
        if m is not None:
            out[i] = CalcExactMolWt(m)
    return out


def build():
    smi = pd.read_parquet(DATA / "candidates.parquet", columns=["smiles"]).smiles.tolist()
    chunks = [smi[i:i + 20000] for i in range(0, len(smi), 20000)]
    with Pool(len(os.sched_getaffinity(0))) as p:
        m = np.concatenate(p.map(_masses, chunks))
    np.save(HERE / "masses.npy", m)
    log(f"masses for {len(m)} molecules, {int(np.isnan(m).sum())} unparsable")


def binned(mzs, ints, width=0.01, maxmz=3000.0):
    rows, cols, vals = [], [], []
    for i, (m, it) in enumerate(zip(mzs, ints)):
        m = np.asarray(m, float)
        it = np.sqrt(np.asarray(it, float))
        k = m < maxmz
        rows.append(np.full(k.sum(), i))
        cols.append(np.round(m[k] / width).astype(np.int64))
        vals.append(it[k])
    X = sparse.csr_matrix((np.concatenate(vals).astype(np.float32), (np.concatenate(rows), np.concatenate(cols))),
                          shape=(len(mzs), int(maxmz / width) + 2))
    X.sum_duplicates()
    nrm = np.sqrt(np.asarray(X.multiply(X).sum(1)).ravel()) + 1e-12
    return (sparse.diags((1 / nrm).astype(np.float32)) @ X).tocsr()


def predict(method: str, inp: str, outp: str):
    spectra = [json.loads(line) for line in open(inp) if line.strip()]
    log(f"{len(spectra)} spectra, method {method}")
    masses = np.load(HERE / "masses.npy")
    smiles = pd.read_parquet(DATA / "candidates.parquet", columns=["smiles"]).smiles.to_numpy()
    ok = ~np.isnan(masses)
    order = np.flatnonzero(ok)[np.argsort(masses[ok], kind="stable")]
    ms = masses[order]

    nn = None
    if method == "library":
        tr = pd.read_parquet(DATA / "train_spectra.parquet", columns=["mzs", "intensities", "smiles"])
        Xtr = binned(tr.mzs.tolist(), tr.intensities.tolist())
        Xq = binned([s["mzs"] for s in spectra], [s["intensities"] for s in spectra])
        XtrT = Xtr.T.tocsr()
        nn = []
        for a in range(0, Xq.shape[0], 256):
            blk = (Xq[a:a + 256] @ XtrT).toarray()
            top = np.argsort(-blk, axis=1)[:, :10]
            nn += [(top[r], blk[r, top[r]]) for r in range(len(top))]
        trsmi = tr.smiles.to_numpy()
        del Xtr, XtrT, tr
        log("library neighbours done")
        gen = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)
        fpc: dict = {}

        def fp(s):
            if s not in fpc:
                m = Chem.MolFromSmiles(s)
                fpc[s] = gen.GetFingerprint(m) if m is not None else None
            return fpc[s]

    with open(outp, "w") as out:
        for i, s in enumerate(spectra):
            M = s["precursor_mz"] - ADDUCT.get(s["adduct"], ADDUCT["[M+H]+"])
            tol = PPM.get(s.get("instrument_type"), PPM_UNKNOWN) * 1e-6 * M
            a, b = np.searchsorted(ms, M - tol, "left"), np.searchsorted(ms, M + tol, "right")
            idx = order[a:b]
            err = np.abs(ms[a:b] - M)
            if method == "random":
                rng = np.random.default_rng(zlib.crc32(s["spectrum_id"].encode()))
                pick = rng.permutation(len(idx))[:K]
            else:
                cfp = [fp(x) for x in smiles[idx]]
                valid = [j for j, f in enumerate(cfp) if f is not None]
                score = np.full(len(idx), -1.0)
                if valid:
                    vf = [cfp[j] for j in valid]
                    acc = np.zeros(len(valid))
                    for j, w in zip(*nn[i]):
                        nf = fp(trsmi[j])
                        if nf is not None:
                            acc += w * np.asarray(DataStructs.BulkTanimotoSimilarity(nf, vf))
                    score[valid] = acc
                pick = np.lexsort((err, -score))[:K]
            out.write(json.dumps({"spectrum_id": s["spectrum_id"], "smiles": smiles[idx[pick]].tolist()}) + "\n")
            out.flush()
    log("done")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--method", choices=["library", "random"], default="library")
    ap.add_argument("files", nargs="*")
    a = ap.parse_args()
    if a.build:
        build()
    else:
        predict(a.method, *a.files)


if __name__ == "__main__":
    main()
