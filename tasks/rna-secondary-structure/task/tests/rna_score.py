#!/usr/bin/env python3
"""Base-pair scorer for single-sequence RNA secondary-structure prediction.

Standard library only (numpy is not required). Python >= 3.9.

Inputs
------
labels       JSONL, one object per test sequence:
             {"id", "family", "clan", "pairs": [[i, j], ...] (1-based), "structure"}
             The sequence length is taken from "structure" (or from a
             sequences file, or a "length"/"sequence" field, in that order).
predictions  JSONL, one object per line: {"id": ..., "structure": "<dot-bracket>"}
             or {"id": ..., "pairs": [[i, j], ...]} (1-based). A .json file
             holding {id: "<dot-bracket>" | [[i, j], ...]} is also accepted.

Conventions
-----------
* Dot-bracket: '.' is unpaired; (), [], {}, <> are four independent bracket
  types, so crossing pairs (pseudoknots) can be written. Any other character,
  an unbalanced bracket, or a length different from the sequence length makes
  the WHOLE prediction invalid.
* Pair list: each entry must be two integers in 1..L with i != j; (j, i) is
  read as (i, j); an exact duplicate pair is collapsed. A base appearing in two
  different pairs, an out-of-range index or a malformed entry makes the WHOLE
  prediction invalid.
* An invalid prediction is scored as "no pairs" and counted in
  counters["invalid_<reason>"]. A missing id is scored as "no pairs" and
  counted in counters["missing"].
* Sharp pairs (j - i < 4, i.e. a hairpin with fewer than 3 unpaired bases) are
  physically impossible. They are DROPPED from the prediction (the rest of the
  structure is kept) and counted. The same rule is applied to labels so the
  oracle always scores exactly 1.0; labels should contain none (counted
  separately as label_sharp_pairs_dropped).
* Pair identity ignores the bases: non-canonical pairs are allowed.
* Per sequence with predicted set P and true set T:
    precision = |P & T| / |P|      (0 if P is empty)
    recall    = |P & T| / |T|      (0 if T is empty)
    F1        = 2 * |P & T| / (|P| + |T|)
    exact     = 1 if P == T else 0
  If both P and T are empty, precision = recall = F1 = exact = 1 (cannot
  happen with our labels, which always have >= 3 pairs).
* One-nucleotide slippage: a predicted pair (i, j) may match a true pair
  (i, j), (i+-1, j) or (i, j+-1). Matching is one-to-one (each predicted and
  each true pair used at most once) and the matched count is a MAXIMUM
  bipartite matching, so the result does not depend on the order of pairs.
* Aggregation:
    family_macro   = mean over families of (mean per-sequence value within
                     the family)     <- PRIMARY (reported as "score")
    sequence_mean  = mean over all sequences of the per-sequence value
    micro          = pooled counts over all sequences:
                     F1 = 2*sum|P&T| / (sum|P| + sum|T|)
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Set, Tuple

Pair = Tuple[int, int]
MIN_LOOP = 3                      # minimum unpaired bases in a hairpin
OPEN_TO_CLOSE = {"(": ")", "[": "]", "{": "}", "<": ">"}
CLOSE_TO_OPEN = {v: k for k, v in OPEN_TO_CLOSE.items()}


class InvalidPrediction(ValueError):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
def parse_dot_bracket(s: str, length: Optional[int] = None) -> Set[Pair]:
    """Parse dot-bracket with (), [], {}, <> into a set of 1-based pairs."""
    if not isinstance(s, str):
        raise InvalidPrediction("bad_type", "structure is not a string")
    s = s.strip()
    if length is not None and len(s) != length:
        raise InvalidPrediction("wrong_length", f"{len(s)} != {length}")
    stacks: Dict[str, List[int]] = {k: [] for k in OPEN_TO_CLOSE}
    pairs: Set[Pair] = set()
    for pos, ch in enumerate(s, start=1):
        if ch == ".":
            continue
        if ch in OPEN_TO_CLOSE:
            stacks[ch].append(pos)
        elif ch in CLOSE_TO_OPEN:
            st = stacks[CLOSE_TO_OPEN[ch]]
            if not st:
                raise InvalidPrediction("unbalanced", f"unmatched '{ch}' at {pos}")
            pairs.add((st.pop(), pos))
        else:
            raise InvalidPrediction("bad_char", f"'{ch}' at {pos}")
    for k, st in stacks.items():
        if st:
            raise InvalidPrediction("unbalanced", f"unmatched '{k}' at {st[-1]}")
    return pairs


def parse_pair_list(obj, length: int) -> Set[Pair]:
    """Validate a list of 1-based [i, j] pairs."""
    if not isinstance(obj, (list, tuple)):
        raise InvalidPrediction("bad_type", "pairs is not a list")
    pairs: Set[Pair] = set()
    for p in obj:
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise InvalidPrediction("bad_pair", repr(p))
        a, b = p
        if isinstance(a, bool) or isinstance(b, bool):
            raise InvalidPrediction("bad_pair", repr(p))
        if isinstance(a, float) and a.is_integer():
            a = int(a)
        if isinstance(b, float) and b.is_integer():
            b = int(b)
        if not isinstance(a, int) or not isinstance(b, int):
            raise InvalidPrediction("bad_pair", repr(p))
        if a == b:
            raise InvalidPrediction("self_pair", repr(p))
        i, j = (a, b) if a < b else (b, a)
        if i < 1 or j > length:
            raise InvalidPrediction("out_of_range", f"{p} with L={length}")
        pairs.add((i, j))
    check_one_partner(pairs)
    return pairs


def check_one_partner(pairs: Iterable[Pair]) -> None:
    seen: Set[int] = set()
    for i, j in pairs:
        for x in (i, j):
            if x in seen:
                raise InvalidPrediction("multi_paired", f"base {x} in two pairs")
            seen.add(x)


def drop_sharp(pairs: Set[Pair]) -> Tuple[Set[Pair], int]:
    kept = {(i, j) for i, j in pairs if j - i > MIN_LOOP}
    return kept, len(pairs) - len(kept)


def parse_prediction(value, length: int) -> Set[Pair]:
    """value is a dot-bracket string or a pair list. Raises InvalidPrediction."""
    if isinstance(value, str):
        return parse_dot_bracket(value, length)
    return parse_pair_list(value, length)


# --------------------------------------------------------------------------
# Per-sequence metrics
# --------------------------------------------------------------------------
def slip_matches(pred: Set[Pair], true: Set[Pair]) -> int:
    """Max one-to-one matching where pred (i,j) ~ true (i,j),(i+-1,j),(i,j+-1)."""
    exact = pred & true
    p_rest = [p for p in pred if p not in exact]
    t_rest = true - exact
    # Taking exact matches first is optimal: both sets are valid structures
    # (each base in at most one pair), so any other pair adjacent to an exact
    # pair would share a base with it. Exact pairs therefore have no other
    # edges, and the remaining graph is matched with augmenting paths.
    adj: Dict[Pair, List[Pair]] = {}
    for (i, j) in p_rest:
        cand = [(i - 1, j), (i + 1, j), (i, j - 1), (i, j + 1)]
        nb = [c for c in cand if c in t_rest]
        if nb:
            adj[(i, j)] = nb
    match_t: Dict[Pair, Pair] = {}

    def augment(p: Pair, visited: Set[Pair]) -> bool:
        for t in adj[p]:
            if t in visited:
                continue
            visited.add(t)
            if t not in match_t or augment(match_t[t], visited):
                match_t[t] = p
                return True
        return False

    n = 0
    for p in adj:                      # Kuhn's algorithm; degree <= 4
        if augment(p, set()):
            n += 1
    return len(exact) + n


def prf(tp: int, n_pred: int, n_true: int) -> Tuple[float, float, float]:
    if n_pred == 0 and n_true == 0:
        return 1.0, 1.0, 1.0
    p = tp / n_pred if n_pred else 0.0
    r = tp / n_true if n_true else 0.0
    f = 2 * tp / (n_pred + n_true)
    return p, r, f


def score_pairs(pred: Set[Pair], true: Set[Pair]) -> dict:
    tp = len(pred & true)
    tp_s = slip_matches(pred, true)
    p, r, f = prf(tp, len(pred), len(true))
    ps, rs, fs = prf(tp_s, len(pred), len(true))
    return {
        "n_pred": len(pred), "n_true": len(true), "tp": tp, "tp_slip": tp_s,
        "precision": p, "recall": r, "f1": f, "exact": 1.0 if pred == true else 0.0,
        "precision_slip": ps, "recall_slip": rs, "f1_slip": fs,
    }


# --------------------------------------------------------------------------
# I/O
# --------------------------------------------------------------------------
def read_jsonl(path: str) -> List[dict]:
    out = []
    with open(path) as fh:
        for ln, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def load_labels(path: str, sequences_path: Optional[str] = None) -> Dict[str, dict]:
    seq_len: Dict[str, int] = {}
    if sequences_path:
        for r in read_jsonl(sequences_path):
            seq_len[str(r["id"])] = len(r["sequence"])
    labels: Dict[str, dict] = {}
    for r in read_jsonl(path):
        rid = str(r["id"])
        if rid in labels:
            raise ValueError(f"duplicate label id {rid}")
        if "structure" in r and r["structure"] is not None:
            L = len(r["structure"])
        elif rid in seq_len:
            L = seq_len[rid]
        elif "length" in r:
            L = int(r["length"])
        elif "sequence" in r:
            L = len(r["sequence"])
        else:
            raise ValueError(f"label {rid}: cannot determine sequence length")
        if rid in seq_len and seq_len[rid] != L:
            raise ValueError(f"label {rid}: length mismatch with sequences file")
        if "pairs" in r and r["pairs"] is not None:
            true = parse_pair_list(r["pairs"], L)
        else:
            true = parse_dot_bracket(r["structure"], L)
        if "pairs" in r and r.get("structure") is not None:
            if parse_dot_bracket(r["structure"], L) != true:
                raise ValueError(f"label {rid}: 'pairs' and 'structure' disagree")
        labels[rid] = {"len": L, "pairs": true,
                       "family": str(r.get("family", "NA")),
                       "clan": r.get("clan")}
    return labels


def load_predictions(path: str) -> Tuple[Dict[str, object], Dict[str, int]]:
    """Returns {id: raw value} and counters (duplicate ids, unreadable lines)."""
    counters: Dict[str, int] = defaultdict(int)
    preds: Dict[str, object] = {}
    with open(path) as fh:
        text = fh.read()
    stripped = text.lstrip()
    if stripped.startswith("{") and path.endswith(".json"):
        obj = json.loads(text)
        for k, v in obj.items():
            preds[str(k)] = v
        return preds, counters
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            rid = str(r["id"])
            val = r["structure"] if "structure" in r else r["pairs"]
        except Exception:
            counters["unreadable_lines"] += 1
            continue
        if rid in preds:
            counters["duplicate_ids"] += 1      # first occurrence wins
            continue
        preds[rid] = val
    return preds, counters


# --------------------------------------------------------------------------
# Scoring a whole submission
# --------------------------------------------------------------------------
METRICS = ("f1", "precision", "recall", "exact", "f1_slip", "precision_slip", "recall_slip")


def _mean(xs: List[float]) -> float:
    return math.fsum(xs) / len(xs) if xs else 0.0


def score(labels: Dict[str, dict], preds: Dict[str, object],
          counters: Optional[Dict[str, int]] = None) -> dict:
    counters = defaultdict(int, counters or {})
    per_seq = {}
    for rid, lab in labels.items():
        true, nd = drop_sharp(lab["pairs"])
        counters["label_sharp_pairs_dropped"] += nd
        if rid not in preds:
            counters["missing"] += 1
            pred = set()
        else:
            try:
                pred = parse_prediction(preds[rid], lab["len"])
                pred, nd = drop_sharp(pred)
                counters["pred_sharp_pairs_dropped"] += nd
            except InvalidPrediction as e:
                counters["invalid"] += 1
                counters["invalid_" + e.reason] += 1
                pred = set()
        m = score_pairs(pred, true)
        m["family"] = lab["family"]
        per_seq[rid] = m
    counters["extra_ids"] = len(set(preds) - set(labels))

    by_fam: Dict[str, List[dict]] = defaultdict(list)
    for m in per_seq.values():
        by_fam[m["family"]].append(m)
    fam_means = {f: {k: _mean([m[k] for m in ms]) for k in METRICS} | {"n": len(ms)}
                 for f, ms in sorted(by_fam.items())}

    family_macro = {k: _mean([fm[k] for fm in fam_means.values()]) for k in METRICS}
    sequence_mean = {k: _mean([m[k] for m in per_seq.values()]) for k in METRICS}
    sp = sum(m["n_pred"] for m in per_seq.values())
    st = sum(m["n_true"] for m in per_seq.values())
    tp = sum(m["tp"] for m in per_seq.values())
    tps = sum(m["tp_slip"] for m in per_seq.values())
    p, r, f = prf(tp, sp, st)
    ps, rs, fs = prf(tps, sp, st)
    micro = {"precision": p, "recall": r, "f1": f,
             "precision_slip": ps, "recall_slip": rs, "f1_slip": fs}
    return {
        "score": family_macro["f1"],
        "n_sequences": len(per_seq),
        "n_families": len(fam_means),
        "family_macro": family_macro,
        "sequence_mean": sequence_mean,
        "micro": micro,
        "counters": dict(sorted(counters.items())),
        "per_family": fam_means,
        "per_sequence": per_seq,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pred", required=True, help="predictions .jsonl (or .json dict)")
    ap.add_argument("--labels", required=True, help="hidden_labels.jsonl")
    ap.add_argument("--sequences", help="hidden_sequences.jsonl (optional, for lengths)")
    ap.add_argument("--out", help="write full result JSON here")
    ap.add_argument("--per-sequence", action="store_true",
                    help="keep per-sequence rows in --out (large)")
    a = ap.parse_args(argv)
    labels = load_labels(a.labels, a.sequences)
    try:
        preds, counters = load_predictions(a.pred)
    except FileNotFoundError:
        preds, counters = {}, {"prediction_file_missing": 1}
    res = score(labels, preds, counters)
    summary = {k: res[k] for k in ("score", "n_sequences", "n_families",
                                   "family_macro", "sequence_mean", "micro", "counters")}
    print(json.dumps(summary, indent=2))
    if a.out:
        if not a.per_sequence:
            res = dict(res)
            res.pop("per_sequence")
        with open(a.out, "w") as fh:
            json.dump(res, fh, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
