"""Step 2: MCES<10 graph over standardised-ik14 molecules (from the shipped pairwise matrix, TSV SMILES only).
Writes WORK/mces_graph.npz (nodes, lab, a, b) and WORK/eligible.parquet (ik14 outside the giant component).
Usage: python -I s02_graph.py   (in Docker)"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import h5py  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy.sparse import coo_matrix  # noqa: E402
from scipy.sparse.csgraph import connected_components  # noqa: E402

from common import RAW, WORK  # noqa: E402


def main():
    msg = pd.read_csv(f"{RAW}/MassSpecGym1.5.tsv", sep="\t", usecols=["identifier", "smiles"])
    orig = pd.read_csv(f"{RAW}/MassSpecGym.tsv", sep="\t", usecols=["identifier", "smiles"]).rename(columns={"smiles": "smiles_orig"})  # the original release (the MCES matrix uses its SMILES)
    df = msg.merge(orig, on="identifier", validate="1:1")
    std = pd.read_parquet(f"{WORK}/std_msg.parquet").set_index("src_smiles")
    df["ik14"] = df.smiles.map(std.ik14)
    with h5py.File(f"{RAW}/auxiliary/all_smiles_mces.hdf5", "r") as f:
        order = [s.decode() for s in f["mces_smiles_order"][:]]
        D = f["mces"][:]
    n = len(order)
    k = np.nonzero(D < 10)[0]
    del D
    starts = np.cumsum(np.r_[0, np.arange(n - 1, 0, -1)])
    i = np.searchsorted(starts, k, side="right") - 1
    j = k - starts[i] + i + 1
    order_ik = pd.Series(order).map(df.drop_duplicates("smiles_orig").set_index("smiles_orig").ik14)
    nodes = np.array(sorted(df.ik14.unique()))
    node_id = pd.Series(np.arange(len(nodes)), index=nodes)
    oid = order_ik.map(node_id).to_numpy()
    a, b = oid[i], oid[j]
    keep = ~np.isnan(a) & ~np.isnan(b)
    a, b = a[keep].astype(int), b[keep].astype(int)
    keep = a != b
    a, b = a[keep], b[keep]
    N = len(nodes)
    g = coo_matrix((np.ones(len(a), dtype=np.int8), (a, b)), shape=(N, N)).tocsr()
    nc, lab = connected_components(g, directed=False)
    sizes = np.bincount(lab)
    giant = int(np.argmax(sizes))
    np.savez(f"{WORK}/mces_graph.npz", nodes=nodes, lab=lab, a=a, b=b, giant=giant)
    pd.DataFrame({"ik14": nodes[lab != giant]}).to_parquet(f"{WORK}/eligible.parquet", index=False)
    st = {"molecules": int(N), "edges": int(len(a)), "components": int(nc), "giant": int(sizes[giant]),
          "eligible_outside_giant": int((lab != giant).sum())}
    Path(f"{WORK}/graph_stats.json").write_text(json.dumps(st, indent=1))
    print(json.dumps(st, indent=1))


if __name__ == "__main__":
    main()
