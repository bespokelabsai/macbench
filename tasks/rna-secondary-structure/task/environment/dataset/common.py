"""Shared paths and constants for the dataset build.

Paths: everything lives under BUILD, which is RNA_DATASET_BUILD if set, else build/ at the top of the delivery
folder (three levels above this file, outside task/; gitignored). drun.sh mounts it at /build in the container and
sets RNA_DATASET_BUILD=/build.
  BUILD/raw/                     Rfam downloads for the split (00_download.sh)
  BUILD/homolog/                 Rfam.fa.gz + Rfam.full_region.gz (00_download.sh), the deduplicated pool and its
                                 BLAST index (10a_homolog_pool.py)
  BUILD/work/                    intermediates
  BUILD/dataset/                 the dataset: agent/ and grader/ (what prepare.sh reads)
"""
import gzip
import hashlib
import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILD = Path(os.environ.get("RNA_DATASET_BUILD") or HERE.parent.parent.parent / "build").resolve()
RAW = BUILD / "raw"  # 06_write.py hashes every file under RAW into the manifest: keep only the split's inputs here
HOMOLOG = BUILD / "homolog"
R151 = RAW / "rfam-15.1"
R150 = RAW / "rfam-15.0"
WORK = BUILD / "work"
OUT = BUILD / "dataset"
WORK.mkdir(parents=True, exist_ok=True)

SEED = 20261105          # our own fixed seed for every random choice in the build
MAX_NONACGU_FRAC = 0.02  # drop a sequence if more than 2% of its residues are not A/C/G/U
MIN_PAIRS = 3            # drop a sequence with fewer canonical pairs than this after projection
TEST_MAX_LEN = 1000      # hidden test sequences are at most this long
TEST_PER_FAMILY = 8      # at most this many test sequences per family
CLAN_TEST_FRAC = 0.70    # share of eligible clan units drawn for test (before the leakage, label and class rules)
CLANLESS_TEST_FRAC = 0.70  # share of eligible clan-less families drawn for test
NEAR_DUP_ID = 0.95       # within-family near-duplicate clustering identity
WORKERS = 8
MIN_HAIRPIN_SPAN = 4     # drop projected pairs with j - i < 4 (hairpin loop under 3 nt), train and test
MIRNA_MAX_TEST_FAMILY_FRAC = None  # no separate miRNA cap (miRNA is one class under the class cap below)
DROP_SINGLE_SEQ_LABELS = True      # units holding a family whose Rfam structure_source is a folding program go to
                                   # train whole (their labels would be circular)
DROP_LABEL_KINDS = ("single_sequence", "comparative")  # which structure_source kinds that rule removes: a
                                   # single-sequence folder, and alignment folding (a team has a homolog database)
CLASS_MAX_TEST_FAMILY_FRAC = 0.10  # no RNA class (full Rfam type string) above 10% of test families
MIN_TRAIN_UNITS_PER_CLASS_FROM = 1  # every RNA class keeps at least one unit in train

# Families removed from every split, with the reason (judgement calls).
EXCLUDE_FAMILIES = {
    "RF04321": "tmRNA_permuted is new in 15.1 but is a circular permutation of tmRNA (BLAST 100% identical stretches "
               "to alpha/beta/cyano tmRNA). Forcing it into test would pull the whole tRNA/tmRNA clan CL00001 "
               "out of training; it is not a novel fold, so it is dropped instead.",
}

# Rfam structure_source classification. A family's label counts as a single-sequence prediction when the
# field starts with "Predicted" and the programs it names are all single-sequence folders. Author names and PMIDs
# are ignored. Any comparative / alignment-based program named alongside makes it comparative (e.g. "BM VeGETA,
# LocARNA, RNAfold"). Patterns are matched case-insensitively against each ';'- or ','-separated token.
SINGLE_SEQ_FOLDERS = ["rnafold", "mfold", "unafold", "rnastructure", "sfold", "contrafold", "rnashapes",
                      "kinefold", "linearfold", "eternafold", "ipknot", "centroidfold", "nupack"]
COMPARATIVE_PROGRAMS = ["alifold", "aalfold", "pfold", "war", "cmfinder", "locarna", "r-scape", "rscape",
                        "cacofold", "ilm", "rnaz", "stemloc", "fsa", "rnie", "vegeta", "anchorna", "t-coffee",
                        "foldalign", "dynalign", "turbofold", "rnaforester", "infernal"]


def _names(tok, prog):
    import re
    # short names need word boundaries ("war", "ilm", "fsa"); longer ones may be embedded ("RNAalifold",
    # "Mafft_pfold", "clustal_alifold")
    return prog in tok if len(prog) > 4 else re.search(r"\b" + re.escape(prog) + r"\b", tok) is not None


def structure_source_kind(src):
    """'single_sequence', 'comparative', 'published' (Published / Pseudobase / curated / author only) or
    'predicted_unspecified' (Predicted, but no program named)."""
    import re
    s = (src or "").strip()
    toks = [t.strip().lower().replace("_", " ") for t in re.split(r"[;,]", s) if t.strip()]
    comp = any(_names(t, c) for t in toks for c in COMPARATIVE_PROGRAMS)
    if not toks or toks[0] != "predicted":
        return "comparative" if comp else "published"
    if comp:
        return "comparative"
    if any(re.search(r"\b" + re.escape(f) + r"\b", t) for t in toks[1:] for f in SINGLE_SEQ_FOLDERS):
        return "single_sequence"
    return "predicted_unspecified"


CANON = {"AU", "UA", "GC", "CG", "GU", "UG"}
BRACKETS = ["()", "[]", "{}", "<>"]  # page 0..3 in our dot-bracket output


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sql_columns(sql_path):
    cols = []
    for line in Path(sql_path).read_text().splitlines():
        s = line.strip()
        if s.startswith("`"):
            cols.append(s.split("`")[1])
    return cols


def read_table(txt_gz, sql_path):
    import pandas as pd
    cols = sql_columns(sql_path)
    rows = []
    with gzip.open(txt_gz, "rt", encoding="latin-1") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            rows.append(parts[: len(cols)] + [None] * (len(cols) - len(parts)))
    return pd.DataFrame(rows, columns=cols)


def write_json(path, obj):
    Path(path).write_text(json.dumps(obj, indent=2, sort_keys=False) + "\n")


def peak_mem_note():
    """Peak memory of this container (from the cgroup memory files), in GB, if readable."""
    for p in ("/sys/fs/cgroup/memory.peak", "/sys/fs/cgroup/memory/memory.max_usage_in_bytes"):
        try:
            return round(int(Path(p).read_text().strip()) / 1e9, 3)
        except Exception:
            pass
    return None


def pages_to_dotbracket(n, pages):
    """pages: list (index = page) of lists of 1-based (i, j). Returns dot-bracket string."""
    s = ["."] * n
    for k, plist in enumerate(pages):
        o, c = BRACKETS[k]
        for i, j in plist:
            s[i - 1] = o
            s[j - 1] = c
    return "".join(s)


def crosses(a, b):
    (i, j), (k, l) = a, b
    return i < k < j < l or k < i < l < j


def assign_pages(nested, extra):
    """nested: pairs known to be mutually non-crossing (page 0).
    extra: pseudoknot pairs, placed greedily (by opening position) on the lowest page they do not cross.
    Returns list of pages, or None if more than 4 pages would be needed."""
    pages = [sorted(nested)]
    for p in sorted(extra):
        for k in range(len(BRACKETS)):
            if k == len(pages):
                pages.append([])
            if k == 0 and not nested and not pages[0]:
                pass
            if all(not crosses(p, q) for q in pages[k]):
                pages[k].append(p)
                break
        else:
            return None
    return [sorted(x) for x in pages]
