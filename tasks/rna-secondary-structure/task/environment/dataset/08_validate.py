"""Step 8: independent checks of the release files (re-derives everything from the files themselves)."""
import json
from collections import Counter

import pandas as pd

from common import (DROP_LABEL_KINDS, MIN_TRAIN_UNITS_PER_CLASS_FROM, CLASS_MAX_TEST_FAMILY_FRAC, DROP_SINGLE_SEQ_LABELS, MIN_HAIRPIN_SPAN, MIRNA_MAX_TEST_FAMILY_FRAC, OUT, R151,
                    read_table, structure_source_kind, TEST_MAX_LEN, TEST_PER_FAMILY, sha256, write_json, WORK)

CANON = {"AU", "UA", "GC", "CG", "GU", "UG"}
BR = {"(": ")", "[": "]", "{": "}", "<": ">"}


def parse_db(s):
    st = {o: [] for o in BR}
    close = {c: o for o, c in BR.items()}
    pairs, pages = [], {}
    for i, ch in enumerate(s, 1):
        if ch in BR:
            st[ch].append(i)
        elif ch in close:
            j = st[close[ch]].pop()
            pairs.append((j, i))
            pages[(j, i)] = close[ch]
        else:
            assert ch == ".", ch
    assert all(not v for v in st.values())
    return sorted(pairs), pages


def main():
    checks = {}
    tr = pd.read_parquet(OUT / "agent/train.parquet")
    hs = [json.loads(l) for l in open(OUT / "grader/hidden_sequences.jsonl")]
    hl = [json.loads(l) for l in open(OUT / "grader/hidden_labels.jsonl")]
    man = json.loads((OUT / "grader/manifest.json").read_text())
    checks["train_columns"] = list(tr.columns) == ["id", "sequence", "structure", "family", "family_name", "clan", "rna_type"]
    checks["hidden_seq_keys"] = all(set(d) == {"id", "sequence"} for d in hs)
    checks["ids_unique"] = tr.id.is_unique and len({d["id"] for d in hs}) == len(hs)
    checks["hidden_ids_match_labels"] = [d["id"] for d in hs] == [d["id"] for d in hl]
    seqs = dict((d["id"], d["sequence"]) for d in hs)
    bad = Counter()
    for d in hl:
        s = seqs[d["id"]]
        pairs, pages = parse_db(d["structure"])
        if [list(p) for p in pairs] != d["pairs"]:
            bad["pairs_vs_structure"] += 1
        if len(s) != len(d["structure"]) or len(s) != d["length"]:
            bad["length"] += 1
        if len(s) > TEST_MAX_LEN:
            bad["too_long"] += 1
        if any(s[i - 1] + s[j - 1] not in CANON for i, j in pairs):
            bad["noncanonical"] += 1
        if len(pairs) < 3:
            bad["lt3"] += 1
        if MIN_HAIRPIN_SPAN and any(j - i < MIN_HAIRPIN_SPAN for i, j in pairs):
            bad["short_span"] += 1
        pos = [x for p in pairs for x in p]
        if len(pos) != len(set(pos)):
            bad["multi_pair"] += 1
        # pairs on the same page must not cross
        by = {}
        for p, o in pages.items():
            by.setdefault(o, []).append(p)
        for ps in by.values():
            ps.sort()
            for a in range(len(ps)):
                for b in range(a + 1, len(ps)):
                    (i, j), (k, l) = ps[a], ps[b]
                    if i < k < j < l:
                        bad["crossing_within_page"] += 1
        if set(s) - set("ACGU") and sum(c not in "ACGU" for c in s) / len(s) > 0.02:
            bad["nonacgu"] += 1
    for r in tr.itertuples():
        pairs, _ = parse_db(r.structure)
        if len(r.sequence) != len(r.structure):
            bad["train_length"] += 1
        if any(r.sequence[i - 1] + r.sequence[j - 1] not in CANON for i, j in pairs):
            bad["train_noncanonical"] += 1
        if len(pairs) < 3:
            bad["train_lt3"] += 1
        if MIN_HAIRPIN_SPAN and any(j - i < MIN_HAIRPIN_SPAN for i, j in pairs):
            bad["train_short_span"] += 1
    checks["label_problems"] = dict(bad)
    te_fam = Counter(d["family"] for d in hl)
    if MIRNA_MAX_TEST_FAMILY_FRAC:
        fam_type = {d["family"]: d["rna_type"] for d in hl}
        share = sum("miRNA" in t for t in fam_type.values()) / len(fam_type)
        checks["mirna_family_share"] = round(share, 4)
        checks["mirna_cap_ok"] = share <= MIRNA_MAX_TEST_FAMILY_FRAC
    if CLASS_MAX_TEST_FAMILY_FRAC:
        fam_type = {d["family"]: d["rna_type"] for d in hl}
        cc = Counter(fam_type.values())
        checks["max_class_share"] = round(max(cc.values()) / len(fam_type), 4)
        checks["class_cap_ok"] = max(cc.values()) <= CLASS_MAX_TEST_FAMILY_FRAC * len(fam_type)
    if DROP_SINGLE_SEQ_LABELS:
        ft = read_table(R151 / "database_files/family.txt.gz", R151 / "database_files/family.sql")
        kind = {a: structure_source_kind(x) for a, x in zip(ft.rfam_acc, ft.structure_source)}
        checks["no_single_seq_label_family_in_test"] = not any(kind.get(f) in DROP_LABEL_KINDS for f in te_fam)
    if MIN_TRAIN_UNITS_PER_CLASS_FROM:
        un = pd.read_parquet(WORK / "units.parquet")
        n_units = un.groupby("rna_type").unit.nunique()
        tr_cls = set(tr.rna_type)
        missing = sorted(c for c, n in n_units.items() if n >= MIN_TRAIN_UNITS_PER_CLASS_FROM and c not in tr_cls)
        checks["classes_with_3plus_units_all_in_train"] = not missing
        if missing:
            checks["classes_missing_from_train"] = missing
    checks["max_per_family"] = max(te_fam.values()) <= TEST_PER_FAMILY
    checks["families_disjoint"] = not (set(te_fam) & set(tr.family))
    checks["clans_disjoint"] = not ({d["clan"] for d in hl if d["clan"]} & set(tr.clan.dropna()))
    checks["sequences_disjoint"] = not (set(seqs.values()) & set(tr.sequence))
    checks["no_dup_train_seq_across_families"] = int(tr.groupby("sequence").family.nunique().max()) == 1
    checks["sha256_match"] = all(sha256(OUT / p) == h for p, h in man["files_sha256"].items())
    checks["counts_match"] = man["counts"]["test"]["sequences"] == len(hl) and man["counts"]["train"]["sequences"] == len(tr)
    ok = all(v for k, v in checks.items() if k not in ("label_problems", "mirna_family_share", "max_class_share")) and not bad
    checks["ALL_OK"] = ok
    write_json(WORK / "validate.json", checks)
    print(json.dumps(checks, indent=1))
    if not ok:  # validate.json is written either way; a failed check stops run_all.sh
        raise SystemExit("validation failed (ALL_OK false)")


if __name__ == "__main__":
    main()
