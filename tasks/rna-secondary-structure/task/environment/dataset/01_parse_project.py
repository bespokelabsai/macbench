"""Step 1: parse Rfam 15.1 seed alignments and project each family's SS_cons onto its seed sequences.

Output: work/projected.parquet (one row per kept seed sequence) and work/project_stats.json.
"""
import gzip
import re
from collections import Counter, defaultdict

import pandas as pd

from common import (CANON, MAX_NONACGU_FRAC, MIN_HAIRPIN_SPAN, MIN_PAIRS, R150, R151, WORK, assign_pages,
                    pages_to_dotbracket, peak_mem_note, read_table, write_json)

OPEN_B = {"<": ">", "(": ")", "[": "]", "{": "}"}
CLOSE_B = {v: k for k, v in OPEN_B.items()}
UNPAIRED = set(".,_-:~")


def parse_stockholm(path):
    fam = None
    with gzip.open(path, "rt", encoding="latin-1") as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("# STOCKHOLM"):
                fam = {"gf": defaultdict(list), "seqs": {}, "order": [], "ss": []}
            elif line.startswith("//"):
                yield fam
                fam = None
            elif line.startswith("#=GF"):
                tag, val = line[5:].split(None, 1) if len(line[5:].split(None, 1)) == 2 else (line[5:].strip(), "")
                fam["gf"][tag].append(val.strip())
            elif line.startswith("#=GC ") and line.split()[1] == "SS_cons":
                fam["ss"].append(line.split()[-1])
            elif line.startswith("#") or not line.strip():
                continue
            else:
                name, aln = line.split()
                if name not in fam["seqs"]:
                    fam["seqs"][name] = []
                    fam["order"].append(name)
                fam["seqs"][name].append(aln)


def parse_wuss(ss):
    """Return (nested_pairs, pk_pairs, errors) as 0-based column pairs."""
    stacks = defaultdict(list)
    nested, pk, errors = [], [], 0
    for c, ch in enumerate(ss):
        if ch in OPEN_B:
            stacks[ch].append(c)
        elif ch in CLOSE_B:
            st = stacks[CLOSE_B[ch]]
            if st:
                nested.append((st.pop(), c))
            else:
                errors += 1
        elif ch.isalpha() and ch.isupper():
            stacks[ch].append(c)
        elif ch.isalpha() and ch.islower():
            st = stacks[ch.upper()]
            if st:
                pk.append((st.pop(), c))
            else:
                errors += 1
        elif ch in UNPAIRED:
            pass
        else:
            errors += 1
    errors += sum(len(v) for v in stacks.values())
    return nested, pk, errors


def main():
    fam_tab = read_table(R151 / "database_files/family.txt.gz", R151 / "database_files/family.sql")
    fam150 = read_table(R150 / "family.txt.gz", R150 / "family.sql")
    clan_mem = read_table(R151 / "database_files/clan_membership.txt.gz", R151 / "database_files/clan_membership.sql")
    clan_tab = read_table(R151 / "database_files/clan.txt.gz", R151 / "database_files/clan.sql")
    new151 = sorted(set(fam_tab.rfam_acc) - set(fam150.rfam_acc))
    fam2clan = {r.rfam_acc: r.clan_acc for r in clan_mem.itertuples()}
    meta = fam_tab.set_index("rfam_acc")

    st = Counter()
    nonacgu_chars = Counter()
    rows, fam_rows = [], []
    for fam in parse_stockholm(R151 / "Rfam.seed.gz"):
        acc = fam["gf"]["AC"][0]
        st["families_in_seed"] += 1
        ss = "".join(fam["ss"])
        nested_c, pk_c, err = parse_wuss(ss)
        fr = {"family": acc, "family_name": fam["gf"]["ID"][0], "n_seed": len(fam["order"]),
              "sscons_pairs": len(nested_c) + len(pk_c), "sscons_pk_pairs": len(pk_c), "wuss_errors": err,
              "ss_source": "; ".join(fam["gf"].get("SS", []))}
        fam_rows.append(fr)
        if err:
            st["families_with_wuss_errors"] += 1
        for name in fam["order"]:
            st["seqs_in"] += 1
            aln = "".join(fam["seqs"][name])
            if len(aln) != len(ss):
                st["drop_len_mismatch"] += 1
                continue
            colpos = [0] * len(aln)
            seq = []
            for c, ch in enumerate(aln):
                if ch.isalpha():
                    seq.append(ch.upper())
                    colpos[c] = len(seq)
            seq = "".join(seq).replace("T", "U")
            n = len(seq)
            nonacgu = sum(1 for ch in seq if ch not in "ACGU")
            for ch in seq:
                if ch not in "ACGU":
                    nonacgu_chars[ch] += 1
            if n == 0 or nonacgu / n > MAX_NONACGU_FRAC:
                st["drop_nonacgu_gt2pct"] += 1
                continue
            keep_n, keep_pk = [], []
            for src, out in ((nested_c, keep_n), (pk_c, keep_pk)):
                for a, b in src:
                    i, j = colpos[a], colpos[b]
                    if not i or not j:
                        st["pairs_dropped_gap"] += 1
                        continue
                    bp = seq[i - 1] + seq[j - 1]
                    if bp in CANON:
                        out.append((i, j))
                        st["pairs_kept"] += 1
                    elif seq[i - 1] in "ACGU" and seq[j - 1] in "ACGU":
                        st["pairs_dropped_noncanonical"] += 1
                        st[f"noncanon_{bp}"] += 1
                    else:
                        st["pairs_dropped_ambiguous_base"] += 1
            n_short_span = 0
            if MIN_HAIRPIN_SPAN:
                before = len(keep_n) + len(keep_pk)
                keep_n = [p for p in keep_n if p[1] - p[0] >= MIN_HAIRPIN_SPAN]
                keep_pk = [p for p in keep_pk if p[1] - p[0] >= MIN_HAIRPIN_SPAN]
                n_short_span = before - len(keep_n) - len(keep_pk)
                st["pairs_dropped_short_span"] += n_short_span
                st["pairs_kept"] -= n_short_span
            if len(keep_n) + len(keep_pk) < MIN_PAIRS:
                st["drop_lt3_pairs"] += 1
                continue
            pages = assign_pages(keep_n, keep_pk)
            if pages is None:
                st["drop_more_than_4_pages"] += 1
                continue
            pairs = sorted(p for pg in pages for p in pg)
            m = meta.loc[acc]
            rows.append({
                "seed_name": name, "family": acc, "family_name": fam["gf"]["ID"][0],
                "clan": fam2clan.get(acc), "rna_type": m["type"].strip().rstrip(";").strip(),
                "new_in_15_1": acc in new151, "sequence": seq, "length": n,
                "structure": pages_to_dotbracket(n, pages), "pairs": [list(p) for p in pairs],
                "n_pairs": len(pairs), "n_pk_pairs": sum(len(pg) for pg in pages[1:]),
                "n_pages": sum(1 for pg in pages if pg), "n_nonacgu": nonacgu, "n_short_span_dropped": n_short_span,
            })
    df = pd.DataFrame(rows)
    df.to_parquet(WORK / "projected.parquet", index=False)
    fams = pd.DataFrame(fam_rows)
    fams["clan"] = fams.family.map(fam2clan)
    fams["rna_type"] = fams.family.map(lambda a: meta.loc[a, "type"].strip().rstrip(";").strip())
    fams["new_in_15_1"] = fams.family.isin(new151)
    fams.to_parquet(WORK / "families.parquet", index=False)
    clan_tab[["clan_acc", "id", "description"]].to_parquet(WORK / "clans.parquet", index=False)
    canon_denom = (st["pairs_kept"] + st["pairs_dropped_short_span"] + st["pairs_dropped_noncanonical"]
                   + st["pairs_dropped_ambiguous_base"])
    out = dict(st)
    out.update({
        "seqs_kept": len(df), "families_kept": int(df.family.nunique()),
        "families_in_table": len(fam_tab), "families_new_in_15_1": new151,
        "families_dropped_from_15_0": sorted(set(fam150.rfam_acc) - set(fam_tab.rfam_acc)),
        "noncanonical_share_of_projected_pairs": st["pairs_dropped_noncanonical"] / canon_denom,
        "nonacgu_chars_all_input": dict(nonacgu_chars.most_common()),
        "nonacgu_residues_in_kept": int(df.n_nonacgu.sum()),
        "kept_seqs_with_any_nonacgu": int((df.n_nonacgu > 0).sum()),
        "peak_mem_gb": peak_mem_note(),
    })
    write_json(WORK / "project_stats.json", out)
    print({k: v for k, v in out.items() if not isinstance(v, (list, dict))})


if __name__ == "__main__":
    main()
