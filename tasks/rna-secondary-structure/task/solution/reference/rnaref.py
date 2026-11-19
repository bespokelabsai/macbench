"""Reference solution: greedy stems, a simple honest baseline (scores 0.329). Standard library only.

    rnaref.py <in.jsonl> <out.jsonl>

For each sequence, repeatedly take the longest helix of canonical/GU pairs (>= MIN_STEM bp, hairpin loop >= 3)
that fits with the stems already taken (nested structures only)."""
import argparse
import json

PAIRS = {("G", "C"), ("C", "G"), ("A", "U"), ("U", "A"), ("G", "U"), ("U", "G")}
MIN_STEM = 4
MIN_LOOP = 3


def greedy(seq: str) -> str:
    s = seq.upper().replace("T", "U")
    n = len(s)
    # L[i][j]: length of the run of stacked pairs (i,j), (i+1,j-1), ... going inward
    L = [[0] * n for _ in range(n)]
    for i in range(n - 1, -1, -1):
        for j in range(i + MIN_LOOP + 1, n):
            if (s[i], s[j]) in PAIRS:
                L[i][j] = 1 + (L[i + 1][j - 1] if j - 1 - (i + 1) > MIN_LOOP else 0)
    cands = sorted(((L[i][j], i, j) for i in range(n) for j in range(n) if L[i][j] >= MIN_STEM),
                   key=lambda t: (-t[0], t[1], -t[2]))
    partner = [-1] * n
    for k, i, j in cands:
        # shrink the helix to free positions; reject if any pair would cross an existing pair
        ok_k = 0
        for q in range(k):
            a, b = i + q, j - q
            if b - a <= MIN_LOOP or partner[a] != -1 or partner[b] != -1:
                break
            ok_k = q + 1
        if ok_k < MIN_STEM:
            continue
        a0, b0 = i, j
        crossing = any(partner[x] != -1 and not (a0 <= partner[x] <= b0) for x in range(a0, b0 + 1))
        if crossing:
            continue
        for q in range(ok_k):
            partner[i + q], partner[j - q] = j - q, i + q
    return "".join("." if p == -1 else ("(" if p > x else ")") for x, p in enumerate(partner))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inp")
    ap.add_argument("out")
    a = ap.parse_args()
    with open(a.inp) as fin, open(a.out, "w") as fout:
        for line in fin:
            if not line.strip():
                continue
            d = json.loads(line)
            fout.write(json.dumps({"id": d["id"], "structure": greedy(d["sequence"])}) + "\n")


if __name__ == "__main__":
    main()
