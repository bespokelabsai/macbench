"""Step 6: write the release files under BUILD/dataset/ and the manifest."""
import json
import subprocess

import numpy as np
import pandas as pd

from common import (DROP_LABEL_KINDS, CLAN_TEST_FRAC, CLANLESS_TEST_FRAC, CLASS_MAX_TEST_FAMILY_FRAC, DROP_SINGLE_SEQ_LABELS,
                    MAX_NONACGU_FRAC, MIN_TRAIN_UNITS_PER_CLASS_FROM, MIN_HAIRPIN_SPAN, MIN_PAIRS,
                    MIRNA_MAX_TEST_FAMILY_FRAC, NEAR_DUP_ID, OUT, RAW, SEED,
                    TEST_MAX_LEN, TEST_PER_FAMILY, WORK, sha256, write_json)


def tool_version(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True).stdout.splitlines()[0].strip()
    except Exception:
        return None


def main():
    sp = pd.read_parquet(WORK / "split.parquet")
    units = pd.read_parquet(WORK / "split_units.parquet")
    moves = json.loads((WORK / "leak_moves.json").read_text()) if (WORK / "leak_moves.json").exists() else {}
    (OUT / "agent").mkdir(parents=True, exist_ok=True)
    (OUT / "grader").mkdir(parents=True, exist_ok=True)

    tr = sp[sp.split == "train"].copy()
    assert tr.seed_name.is_unique
    tr["id"] = tr.seed_name
    tr = tr.sort_values(["family", "id"])
    tr[["id", "sequence", "structure", "family", "family_name", "clan", "rna_type"]].to_parquet(
        OUT / "agent/train.parquet", index=False)

    te = sp[sp.split == "test"].copy()
    rng = np.random.default_rng(SEED)
    te = te.iloc[rng.permutation(len(te))].reset_index(drop=True)
    te["id"] = [f"q{k:05d}" for k in range(len(te))]
    with open(OUT / "grader/hidden_sequences.jsonl", "w") as f:
        for r in te.itertuples():
            f.write(json.dumps({"id": r.id, "sequence": r.sequence}) + "\n")
    with open(OUT / "grader/hidden_labels.jsonl", "w") as f:
        for r in te.itertuples():
            f.write(json.dumps({
                "id": r.id, "family": r.family, "family_name": r.family_name,
                "clan": r.clan if isinstance(r.clan, str) else None, "rna_type": r.rna_type, "length": int(r.length),
                "pairs": [[int(a), int(b)] for a, b in r.pairs], "structure": r.structure,
                "new_in_15_1": bool(r.new_in_15_1)}) + "\n")
    te[["id", "uid", "seed_name", "family", "unit"]].to_parquet(WORK / "hidden_id_map.parquet", index=False)

    test_units = units[units.test]
    clans_all = set(sp.clan.dropna())
    test_clans = sorted(set(te.clan.dropna()))
    train_clans = sorted(set(tr.clan.dropna()))
    assert not set(test_clans) & set(train_clans), "a clan is in both splits"
    assert not set(te.family) & set(tr.family), "a family is in both splits"
    assert not set(te.sequence) & set(tr.sequence), "a sequence is in both splits"
    raw_files = sorted(p for p in RAW.rglob("*") if p.is_file() and p.name != "SHA256SUMS")
    man = {
        "dataset": "RNA secondary structure, single sequence, held-out clans and families",
        "source": {
            "name": "Rfam", "release": "15.1", "license": "CC0",
            "urls": ["https://ftp.ebi.ac.uk/pub/databases/Rfam/15.1/",
                     "https://ftp.ebi.ac.uk/pub/databases/Rfam/15.0/database_files/family.txt.gz (to find 15.1-new families)"],
            "files_sha256": {str(p.relative_to(RAW)): sha256(p) for p in raw_files},
        },
        "tools": {"blastn": tool_version(["blastn", "-version"]), "infernal": tool_version(["cmscan", "-h"])[2:] if tool_version(["cmscan", "-h"]) else None},
        "seed": SEED,
        "params": {"max_nonacgu_frac": MAX_NONACGU_FRAC, "min_pairs": MIN_PAIRS, "near_dup_identity": NEAR_DUP_ID,
                   "test_max_len": TEST_MAX_LEN, "test_per_family": TEST_PER_FAMILY,
                   "min_hairpin_span_j_minus_i": MIN_HAIRPIN_SPAN, "mirna_max_test_family_frac": MIRNA_MAX_TEST_FAMILY_FRAC,
                   "drop_single_sequence_labels_from_test": DROP_SINGLE_SEQ_LABELS, "label_kinds_dropped_from_test": list(DROP_LABEL_KINDS),
                   "class_max_test_family_frac": CLASS_MAX_TEST_FAMILY_FRAC,
                   "min_train_units_per_class_from": MIN_TRAIN_UNITS_PER_CLASS_FROM,
                   "clan_test_frac": CLAN_TEST_FRAC, "clanless_test_frac": CLANLESS_TEST_FRAC,
                   "canonical_pairs": ["AU", "UA", "GC", "CG", "GU", "UG"],
                   "dot_bracket_pages": ["()", "[]", "{}", "<>"]},
        "counts": {
            "train": {"sequences": len(tr), "families": int(tr.family.nunique()), "clans": len(train_clans),
                      "clanless_families": int(tr[tr.clan.isna()].family.nunique())},
            "test": {"sequences": len(te), "families": int(te.family.nunique()), "clans": len(test_clans),
                     "clanless_families": int(te[te.clan.isna()].family.nunique()),
                     "new_in_15_1_sequences": int(te.new_in_15_1.sum()),
                     "new_in_15_1_families": int(te[te.new_in_15_1].family.nunique())},
            "clans_in_rfam_with_data": len(clans_all),
        },
        "held_out_clans": test_clans,
        "test_families": sorted(te.family.unique()),
        "test_families_new_in_15_1": sorted(te[te.new_in_15_1].family.unique()),
        "test_units": sorted(test_units.index),
        "leak_moves": {k: v for k, v in moves.items() if k != "log"},
        "files_sha256": {},
    }
    for p in ["agent/train.parquet", "grader/hidden_sequences.jsonl", "grader/hidden_labels.jsonl"]:
        man["files_sha256"][p] = sha256(OUT / p)
    write_json(OUT / "grader/manifest.json", man)
    print(json.dumps(man["counts"], indent=1))


if __name__ == "__main__":
    main()
