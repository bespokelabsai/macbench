"""Step 4: split units (clans / clan-less families, merged by cross-family homology) into train and test.

Rules (seed SEED):
- Units containing an rRNA family are never test (they are the long, iconic training staples; reported).
- Units with no sequence <= TEST_MAX_LEN cannot give test sequences and stay in train.
- Units containing a family new in Rfam 15.1 are forced to test.
- Of the remaining eligible units, CLAN_TEST_FRAC of the clan units and CLANLESS_TEST_FRAC of the clan-less
  units are drawn at random for test.
- work/leak_moves.json (written by step 5) lists units the leakage check moved to train or to test, families
  it dropped, and single test sequences it removed.
- Test sequences: per test family, those <= TEST_MAX_LEN, at most TEST_PER_FAMILY sampled with a per-family RNG.
  Unsampled sequences of test units are discarded (never in train).
- Then, after the leakage moves and before the per-family sampling:
  (a) every test unit holding a test-eligible family whose Rfam structure_source is a single-sequence folding
      or alignment-folding prediction (common.structure_source_kind, DROP_LABEL_KINDS) goes to train whole;
  (c) [runs before (b)] training coverage: every RNA class held by >= MIN_TRAIN_UNITS_PER_CLASS_FROM units
      keeps at least one unit in train; if all its units are test, the first in a fixed permutation (seed stream
      [SEED, 4]) goes to train whole. Classes held by 1-2 units may be wholly held out (novel classes).
  (b) uniform class cap: no RNA class (the full Rfam type string) above CLASS_MAX_TEST_FAMILY_FRAC of test
      families. Excess units, in one fixed random permutation (seed stream [SEED, 3]), go to train whole.
Output: work/split.parquet (every deduplicated sequence with split in {train, test, discard}), split_stats.json
"""
import hashlib
import json

import numpy as np
import pandas as pd

from common import (DROP_LABEL_KINDS, MIN_TRAIN_UNITS_PER_CLASS_FROM, CLAN_TEST_FRAC, CLANLESS_TEST_FRAC, CLASS_MAX_TEST_FAMILY_FRAC, DROP_SINGLE_SEQ_LABELS,
                    EXCLUDE_FAMILIES, MIRNA_MAX_TEST_FAMILY_FRAC, R151, SEED, TEST_MAX_LEN, TEST_PER_FAMILY, WORK,
                    read_table, structure_source_kind, write_json)


def fam_rng(family):
    return np.random.default_rng([SEED, int(hashlib.sha256(family.encode()).hexdigest()[:8], 16)])


def main():
    df = pd.read_parquet(WORK / "dedup.parquet")
    units = pd.read_parquet(WORK / "units.parquet")
    units = units[~units.family.isin(EXCLUDE_FAMILIES)]
    df = df.merge(units[["family", "unit"]], on="family", how="left")
    moves = {"to_train": [], "drop": [], "to_test": [], "drop_seqs": []}
    mp = WORK / "leak_moves.json"
    if mp.exists():
        moves.update(json.loads(mp.read_text()))

    g = df.groupby("unit")
    u = pd.DataFrame({
        "n_families": units.groupby("unit").family.nunique(),
        "has_clan": units.groupby("unit").clan.apply(lambda s: s.notna().any()),
        "has_new": units.groupby("unit").new_in_15_1.any(),
        "has_rrna": units.groupby("unit").rna_type.apply(lambda s: s.str.contains("rRNA").any()),
        "n_seqs": g.size(),
        "n_short": g.length.apply(lambda s: int((s <= TEST_MAX_LEN).sum())),
    }).fillna({"n_seqs": 0, "n_short": 0})
    u["eligible"] = ~u.has_rrna & (u.n_short > 0)
    rng = np.random.default_rng(SEED)
    u["test"] = u.has_new & u.eligible
    pool_clan = sorted(u.index[u.eligible & ~u.has_new & u.has_clan])
    pool_free = sorted(u.index[u.eligible & ~u.has_new & ~u.has_clan])
    pick_clan = rng.choice(pool_clan, size=round(CLAN_TEST_FRAC * len(pool_clan)), replace=False)
    pick_free = rng.choice(pool_free, size=round(CLANLESS_TEST_FRAC * len(pool_free)), replace=False)
    u.loc[list(pick_clan) + list(pick_free), "test"] = True
    u["drawn_test"] = u.test.copy()
    u.loc[u.index.isin(moves["to_test"]) & u.eligible, "test"] = True
    u.loc[u.index.isin(moves["to_train"]), "test"] = False

    # Test-eligible families (some sequence <= TEST_MAX_LEN, not dropped by the leakage check).
    fam_ok = set(df[(df.length <= TEST_MAX_LEN) & ~df.uid.isin(moves["drop_seqs"]) & ~df.family.isin(moves["drop"])].family)

    # (a): labels predicted by a folding program are circular for a folding benchmark.
    ss_to_train, ss_fams, ss_collateral = [], [], []
    src_kind = {}
    if DROP_SINGLE_SEQ_LABELS:
        ft = read_table(R151 / "database_files/family.txt.gz", R151 / "database_files/family.sql")
        src_kind = {a: structure_source_kind(x) for a, x in zip(ft.rfam_acc, ft.structure_source)}
        uf = units[units.family.isin(fam_ok) & units.unit.isin(u.index[u.test])]
        bad = uf[uf.family.map(src_kind).isin(DROP_LABEL_KINDS)]
        ss_to_train = sorted(set(bad.unit))
        ss_fams = sorted(bad.family)
        ss_collateral = sorted(set(uf[uf.unit.isin(ss_to_train)].family) - set(ss_fams))
        u.loc[u.index.isin(ss_to_train), "test"] = False
    u["single_seq_label_to_train"] = u.index.isin(ss_to_train)

    # (c): training coverage per RNA class (runs before the class cap, which only moves units to train).
    cov_to_train = []
    if MIN_TRAIN_UNITS_PER_CLASS_FROM:
        cls_units = units.groupby("rna_type").unit.apply(lambda x: sorted(set(x)))
        rank4 = {x: k for k, x in enumerate(np.random.default_rng([SEED, 4]).permutation(sorted(set(units.unit))))}
        for c, us in cls_units.items():
            if len(us) >= MIN_TRAIN_UNITS_PER_CLASS_FROM and all(u.loc[x, "test"] for x in us):
                x = min(us, key=rank4.get)
                u.loc[x, "test"] = False
                cov_to_train.append(x)
    u["coverage_to_train"] = u.index.isin(cov_to_train)

    # Optional miRNA cap (off: MIRNA_MAX_TEST_FAMILY_FRAC is None; the class cap (b) covers miRNA): cap miRNA
    # families at MIRNA_MAX_TEST_FAMILY_FRAC of test families. Applied after the leakage moves so
    # the cap holds on the final test set. Order = one fixed random permutation (own seed stream) of every unit
    # holding a miRNA family; walking it, a test unit keeps its miRNA families while the cap allows. A pure-miRNA
    # unit that is not kept goes to TRAIN whole; a mixed unit (miRNA + other families) stays in test without its
    # miRNA families, which are dropped (they cannot go to train while their unit-mates are test).
    cap_to_train, cap_drop_fams = [], []
    if MIRNA_MAX_TEST_FAMILY_FRAC:
        fam_ok = set(df[(df.length <= TEST_MAX_LEN) & ~df.uid.isin(moves["drop_seqs"]) & ~df.family.isin(moves["drop"])].family)
        uf = units[units.family.isin(fam_ok)].assign(mir=lambda x: x.rna_type.str.contains("miRNA"))
        mir = uf[uf.mir].groupby("unit").family.apply(sorted)
        n_oth = uf[~uf.mir].groupby("unit").size()
        tset = set(u.index[u.test])
        O = int(sum(n_oth.get(x, 0) for x in tset))
        perm = list(np.random.default_rng([SEED, 2]).permutation(sorted(mir.index)))
        kept_m = 0
        for x in perm:
            if x not in tset:
                continue
            m = len(mir[x])
            if kept_m + m <= MIRNA_MAX_TEST_FAMILY_FRAC * (O + kept_m + m):
                kept_m += m
            elif n_oth.get(x, 0) == 0:
                cap_to_train.append(x)
            else:
                cap_drop_fams.extend(mir[x])
        u.loc[u.index.isin(cap_to_train), "test"] = False
    u["mirna_cap_to_train"] = u.index.isin(cap_to_train)

    # (b): uniform cap per RNA class (full Rfam type string). Iterated to a fixed point, since moving units
    # shrinks the test set and with it the cap. Walking one fixed permutation of all units per class, a unit keeps
    # its class-c families while they fit under the cap; a unit whose every test family is cut goes to TRAIN whole;
    # a mixed unit loses only the cut classes' families (dropped: they cannot go to train beside test unit-mates).
    cls_to_train, cls_drop_fams, cls_before, cls_iters = [], [], {}, 0
    if CLASS_MAX_TEST_FAMILY_FRAC:
        uf = units[units.family.isin(fam_ok)][["family", "unit", "rna_type"]]
        tset = set(u.index[u.test])
        cls_before = uf[uf.unit.isin(tset)].rna_type.value_counts().to_dict()
        rank = {x: k for k, x in enumerate(np.random.default_rng([SEED, 3]).permutation(sorted(set(uf.unit))))}
        cut = {}
        while True:
            cls_iters += 1
            cur = uf[uf.unit.isin(tset)]
            cur = cur[[t not in cut.get(x, ()) for x, t in zip(cur.unit, cur.rna_type)]]
            n = len(cur)
            counts = cur.rna_type.value_counts()
            over = counts[counts > CLASS_MAX_TEST_FAMILY_FRAC * n]
            if over.empty:
                break
            for c in over.index:
                cu = cur[cur.rna_type == c].groupby("unit").size()
                kept = 0
                for x in sorted(cu.index, key=rank.get):
                    if kept + cu[x] <= CLASS_MAX_TEST_FAMILY_FRAC * n:
                        kept += int(cu[x])
                    else:
                        cut.setdefault(x, set()).add(c)
            for x in sorted(cut):
                if x in tset and set(uf[uf.unit == x].rna_type) <= cut[x]:
                    tset.discard(x)
                    cls_to_train.append(x)
        cls_drop_fams = sorted(uf[uf.unit.isin(tset) & [t in cut.get(x, ()) for x, t in zip(uf.unit, uf.rna_type)]].family)
        u.loc[u.index.isin(cls_to_train), "test"] = False
    u["class_cap_to_train"] = u.index.isin(cls_to_train)

    df["split"] = "train"
    df.loc[df.family.isin(EXCLUDE_FAMILIES), "split"] = "excluded"
    df.loc[df.unit.isin(u.index[u.test]), "split"] = "discard"
    df.loc[df.family.isin(moves["drop"]), "split"] = "dropped_leak"
    df.loc[df.family.isin(cap_drop_fams), "split"] = "dropped_mirna_cap"
    df.loc[df.family.isin(cls_drop_fams), "split"] = "dropped_class_cap"
    test_fams = sorted(df.loc[df.split == "discard", "family"].unique())
    chosen = []
    for f in test_fams:
        cand = df[(df.family == f) & (df.length <= TEST_MAX_LEN) & ~df.uid.isin(moves["drop_seqs"])].sort_values("uid")
        if len(cand) > TEST_PER_FAMILY:
            cand = cand.iloc[sorted(fam_rng(f).choice(len(cand), TEST_PER_FAMILY, replace=False))]
        chosen.extend(cand.uid)
    df.loc[df.uid.isin(chosen), "split"] = "test"
    df.loc[(df.split == "discard") & df.uid.isin(moves["drop_seqs"]), "split"] = "dropped_leak"
    df.to_parquet(WORK / "split.parquet", index=False)
    u.to_parquet(WORK / "split_units.parquet")

    tr, te = df[df.split == "train"], df[df.split == "test"]
    st = {
        "seed": SEED,
        "units_total": int(len(u)), "units_eligible": int(u.eligible.sum()),
        "units_with_rrna_excluded": sorted(u.index[u.has_rrna]),
        "units_no_short_seq": int((~u.has_rrna & (u.n_short == 0)).sum()),
        "pool_clan_units": len(pool_clan), "pool_clanless_units": len(pool_free),
        "drawn_clan_units": len(pick_clan), "drawn_clanless_units": len(pick_free),
        "forced_new_units": sorted(u.index[u.has_new & u.eligible]),
        "moved_to_train": moves["to_train"], "moved_to_test": moves["to_test"], "dropped_families": moves["drop"],
        "dropped_test_seqs": len(moves["drop_seqs"]),
        "test_units": int(u.test.sum()),
        "train_seqs": int(len(tr)), "train_families": int(tr.family.nunique()),
        "test_seqs": int(len(te)), "test_families": int(te.family.nunique()),
        "discarded_seqs_of_test_units": int((df.split == "discard").sum()),
        "dropped_leak_seqs": int((df.split == "dropped_leak").sum()),
        "mirna_cap": MIRNA_MAX_TEST_FAMILY_FRAC, "mirna_cap_units_to_train": len(cap_to_train),
        "mirna_cap_families_to_train": int(units[units.unit.isin(cap_to_train)].family.nunique()),
        "mirna_cap_dropped_families": sorted(cap_drop_fams),
        "test_mirna_families": int(te[te.rna_type.str.contains("miRNA")].family.nunique()),
        "single_seq_rule": DROP_SINGLE_SEQ_LABELS, "label_kinds_dropped": list(DROP_LABEL_KINDS),
        "label_rule_families_by_kind": pd.Series([src_kind.get(f) for f in ss_fams]).value_counts().to_dict() if ss_fams else {},
        "single_seq_units_to_train": ss_to_train, "single_seq_families": ss_fams,
        "single_seq_collateral_families": ss_collateral,
        "coverage_rule_from_units": MIN_TRAIN_UNITS_PER_CLASS_FROM, "coverage_units_to_train": cov_to_train,
        "class_cap": CLASS_MAX_TEST_FAMILY_FRAC, "class_cap_iterations": cls_iters,
        "class_cap_units_to_train": cls_to_train,
        "class_cap_families_to_train": int(units[units.unit.isin(cls_to_train)].family.nunique()),
        "class_cap_dropped_families": cls_drop_fams,
        "class_mix_before_cap": cls_before,
        "class_mix_final": te.drop_duplicates("family").rna_type.value_counts().to_dict(),
        "test_structure_source_kinds": pd.Series([src_kind.get(f, "?") for f in sorted(te.family.unique())]).value_counts().to_dict() if src_kind else {},
    }
    write_json(WORK / "split_stats.json", st)
    print({k: v for k, v in st.items() if not isinstance(v, list)})


if __name__ == "__main__":
    main()
