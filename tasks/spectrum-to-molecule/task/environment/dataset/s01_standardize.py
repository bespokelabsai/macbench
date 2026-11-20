"""Step 1: standardise every molecule source with one RDKit pipeline.
Writes WORK/std_{msg,1m,4m}.parquet with columns: src_smiles, smiles, ik14, mass, n_heavy, n_chiral.
Usage: python -I s01_standardize.py [msg 1m 4m]   (run in capped Docker: ./drun.sh s01_standardize.py)"""
import sys
import time
from multiprocessing import Pool
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pandas as pd  # noqa: E402

from common import RAW, WORK, standardize  # noqa: E402

SOURCES = {
    "msg": (f"{RAW}/MassSpecGym1.5.tsv", "smiles"),
    "1m": (f"{RAW}/molecules/candidate_pools/MassSpecGym_retrieval_molecules_1M.tsv", "smiles"),
    "4m": (f"{RAW}/molecules/candidate_pools/MassSpecGym_retrieval_molecules_4M.tsv", "smiles"),
}


def main():
    Path(WORK).mkdir(parents=True, exist_ok=True)
    names = sys.argv[1:] or list(SOURCES)
    for name in names:
        path, col = SOURCES[name]
        t0 = time.time()
        smi = pd.read_csv(path, sep="\t", usecols=[col])[col].drop_duplicates().tolist()
        with Pool(8) as pool:
            res = pool.map(standardize, smi, chunksize=2000)
        rows = [(s, *r) if r else (s, None, None, None, None, None) for s, r in zip(smi, res)]
        df = pd.DataFrame(rows, columns=["src_smiles", "smiles", "ik14", "mass", "n_heavy", "n_chiral"])
        df.to_parquet(f"{WORK}/std_{name}.parquet", index=False)
        print(f"{name}: {len(df)} input SMILES, failed {df.smiles.isna().sum()}, "
              f"unique ik14 {df.ik14.nunique()}, {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
