"""Hand-worked tests for rna_score.py.  Run: python -m unittest -v test_rna_score"""
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rna_score as rs  # noqa: E402


def lab(rid, structure, family="RF0", clan=None):
    pairs = sorted(rs.parse_dot_bracket(structure))
    return {"id": rid, "family": family, "clan": clan,
            "pairs": [list(p) for p in pairs], "structure": structure}


def run(labels_rows, preds, counters=None):
    tmp = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    for r in labels_rows:
        tmp.write(json.dumps(r) + "\n")
    tmp.close()
    labels = rs.load_labels(tmp.name)
    os.unlink(tmp.name)
    return rs.score(labels, preds, counters)


class TestParsing(unittest.TestCase):
    def test_dot_bracket_all_bracket_types(self):
        s = "((..[[..{{..<<..))..]]..}}..>>"
        p = rs.parse_dot_bracket(s, len(s))
        self.assertEqual(len(p), 8)
        self.assertIn((1, 18), p)
        self.assertIn((5, 22), p)
        self.assertIn((9, 26), p)
        self.assertIn((13, 30), p)

    def test_invalid_dot_bracket(self):
        for s, reason in [("((...)", "unbalanced"), ("(...))", "unbalanced"),
                          ("((..x.))", "bad_char"), ("(((...)))", "wrong_length")]:
            with self.assertRaises(rs.InvalidPrediction) as cm:
                rs.parse_dot_bracket(s, 8 if reason == "wrong_length" else len(s))
            self.assertEqual(cm.exception.reason, reason, s)

    def test_mismatched_bracket_types_are_independent(self):
        # "([)]" is a legal pseudoknot: (1,3) and (2,4) cross.
        self.assertEqual(rs.parse_dot_bracket("([)]"), {(1, 3), (2, 4)})

    def test_pair_list(self):
        self.assertEqual(rs.parse_pair_list([[10, 1], [2, 9], [2, 9]], 10), {(1, 10), (2, 9)})
        for bad, reason in [([[1, 5], [5, 9]], "multi_paired"), ([[0, 5]], "out_of_range"),
                            ([[1, 11]], "out_of_range"), ([[3, 3]], "self_pair"),
                            ([[1, 2, 3]], "bad_pair"), ([["1", 5]], "bad_pair"),
                            ("(1-5....).", "bad_char")]:
            with self.assertRaises(rs.InvalidPrediction) as cm:
                rs.parse_prediction(bad, 10)
            self.assertEqual(cm.exception.reason, reason, bad)


class TestPerSequence(unittest.TestCase):
    T = "((((....))))."          # pairs (1,12),(2,11),(3,10),(4,9); L = 13

    def m(self, pred, true=None):
        true = true if true is not None else self.T
        return rs.score_pairs(rs.parse_prediction(pred, len(true)), rs.parse_dot_bracket(true))

    def test_perfect(self):
        m = self.m(self.T)
        self.assertEqual((m["precision"], m["recall"], m["f1"], m["exact"], m["f1_slip"]),
                         (1.0, 1.0, 1.0, 1.0, 1.0))

    def test_all_unpaired(self):
        m = self.m("." * 13)
        self.assertEqual((m["precision"], m["recall"], m["f1"], m["exact"], m["f1_slip"]),
                         (0.0, 0.0, 0.0, 0.0, 0.0))

    def test_half_right(self):
        # 2 of the 4 true pairs, nothing else: P = 1, R = 0.5, F1 = 2/3
        m = self.m("..((....))...")
        self.assertAlmostEqual(m["precision"], 1.0)
        self.assertAlmostEqual(m["recall"], 0.5)
        self.assertAlmostEqual(m["f1"], 2 / 3)
        self.assertEqual(m["exact"], 0.0)
        # 2 right + 2 wrong: P = R = F1 = 0.5
        m = self.m([[1, 12], [2, 11], [3, 9], [4, 8]])
        self.assertAlmostEqual((m["precision"], m["recall"], m["f1"]), (0.5, 0.5, 0.5))

    def test_shifted_by_one(self):
        # 3' strand shifted one base: pairs (1,13),(2,12),(3,11),(4,10)
        m = self.m("((((.....))))")
        self.assertEqual(m["f1"], 0.0)
        self.assertEqual(m["tp_slip"], 4)
        self.assertEqual(m["f1_slip"], 1.0)

    def test_shift_both_ends_is_not_slippage(self):
        m = self.m(".((((....))))")       # (2,13),(3,12),(4,11),(5,10): i+1 AND j+1
        self.assertEqual(m["f1"], 0.0)
        self.assertEqual(m["f1_slip"], 0.0)

    def test_primer_worked_example(self):
        T = {(2, 15), (3, 14), (4, 13), (5, 12), (6, 11)}
        P = {(2, 15), (3, 14), (4, 13), (5, 11)}
        m = rs.score_pairs(P, T)
        self.assertAlmostEqual(m["precision"], 0.75)
        self.assertAlmostEqual(m["recall"], 0.60)
        self.assertAlmostEqual(m["f1"], 2 / 3)
        self.assertEqual(m["tp_slip"], 4)
        self.assertAlmostEqual(m["f1_slip"], 8 / 9)

    def test_slippage_no_double_counting(self):
        # one predicted pair next to two true pairs counts once
        m = rs.score_pairs({(2, 10)}, {(1, 10), (2, 9)})
        self.assertEqual(m["tp_slip"], 1)
        self.assertAlmostEqual(m["precision_slip"], 1.0)
        self.assertAlmostEqual(m["recall_slip"], 0.5)
        # two predicted pairs next to one true pair count once
        m = rs.score_pairs({(1, 10), (2, 9)}, {(2, 10)})
        self.assertEqual(m["tp_slip"], 1)
        self.assertAlmostEqual(m["precision_slip"], 0.5)

    def test_slippage_uses_maximum_matching(self):
        # (6,20) can match (5,20) or (6,19); (5,21) can match only (5,20).
        # A greedy matcher might give 1; the maximum matching is 2.
        T = {(5, 20), (6, 19)}
        for _ in range(20):
            P = [(6, 20), (5, 21)]
            random.shuffle(P)
            self.assertEqual(rs.slip_matches(set(P), T), 2)

    def test_pseudoknot(self):
        T = "((..[[..))..]]"            # (1,10),(2,9) cross (5,14),(6,13)
        self.assertEqual(self.m(T, T)["f1"], 1.0)
        self.assertEqual(self.m("((..<<..))..>>", T)["f1"], 1.0)   # other bracket type
        self.assertEqual(self.m([[1, 10], [2, 9], [5, 14], [6, 13]], T)["f1"], 1.0)
        m = self.m("((......))....", T)  # nested part only
        self.assertAlmostEqual((m["precision"], m["recall"]), (1.0, 0.5))
        self.assertAlmostEqual(m["f1"], 2 / 3)

    def test_both_empty(self):
        m = rs.score_pairs(set(), set())
        self.assertEqual((m["f1"], m["exact"], m["f1_slip"]), (1.0, 1.0, 1.0))


class TestSubmission(unittest.TestCase):
    def test_invalid_and_missing_count_as_no_pairs(self):
        L = [lab("a", "((((....))))"), lab("b", "((((....))))"), lab("c", "((((....))))"),
             lab("d", "((((....))))"), lab("e", "((((....))))")]
        preds = {"a": "((((....)))",            # wrong length
                 "b": "((((....)))]",           # unbalanced
                 "c": [[1, 12], [1, 11]],       # base 1 twice
                 "d": "((((....))))",           # perfect
                 "zz": "....",                  # extra id, ignored
                 }                              # "e" missing
        r = run(L, preds)
        self.assertAlmostEqual(r["score"], 0.2)
        c = r["counters"]
        self.assertEqual(c["invalid"], 3)
        self.assertEqual(c["invalid_wrong_length"], 1)
        self.assertEqual(c["invalid_unbalanced"], 1)
        self.assertEqual(c["invalid_multi_paired"], 1)
        self.assertEqual(c["missing"], 1)
        self.assertEqual(c["extra_ids"], 1)

    def test_sharp_pairs_dropped_rest_kept(self):
        L = [lab("a", "((((....))))")]
        # (1,12),(2,11) correct; (5,8) has j-i = 3 -> dropped, not fatal
        r = run(L, {"a": [[1, 12], [2, 11], [5, 8]]})
        m = r["per_sequence"]["a"]
        self.assertEqual(m["n_pred"], 2)
        self.assertAlmostEqual(m["f1"], 2 / 3)
        self.assertEqual(r["counters"]["pred_sharp_pairs_dropped"], 1)
        # j - i = 4 (3 unpaired) is allowed
        r = run([lab("b", "(...)")], {"b": "(...)"})
        self.assertEqual(r["score"], 1.0)

    def test_aggregation(self):
        # family A: F1 1.0 and 0.0 -> mean 0.5 ; family B: 1.0 -> macro 0.75
        L = [lab("a1", "((((....))))", "A"), lab("a2", "((((....))))", "A"),
             lab("b1", "(((....)))", "B")]
        r = run(L, {"a1": "((((....))))", "a2": "." * 12, "b1": "(((....)))"})
        self.assertAlmostEqual(r["score"], 0.75)
        self.assertAlmostEqual(r["family_macro"]["exact"], 0.75)
        self.assertAlmostEqual(r["sequence_mean"]["f1"], 2 / 3)
        # micro: TP = 4 + 0 + 3 = 7, |P| = 7, |T| = 11 -> 14/18
        self.assertAlmostEqual(r["micro"]["f1"], 14 / 18)
        self.assertEqual(r["n_families"], 2)

    def test_duplicate_prediction_first_wins(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write(json.dumps({"id": "a", "structure": "((((....))))"}) + "\n")
            fh.write(json.dumps({"id": "a", "structure": "." * 12}) + "\n")
            fh.write("not json\n")
        preds, c = rs.load_predictions(fh.name)
        os.unlink(fh.name)
        self.assertEqual(c["duplicate_ids"], 1)
        self.assertEqual(c["unreadable_lines"], 1)
        self.assertEqual(run([lab("a", "((((....))))")], preds)["score"], 1.0)


def random_structure(rng, L, pk=True):
    """Random valid structure (with optional crossing pairs), no sharp pairs."""
    free = list(range(1, L + 1))
    pairs = set()
    used = set()
    for _ in range(L):
        i, j = sorted(rng.sample(free, 2))
        if j - i < 4 or i in used or j in used:
            continue
        if not pk and any(a < i < b < j or i < a < j < b for a, b in pairs):
            continue
        pairs.add((i, j)); used |= {i, j}
    return pairs


def pairs_to_db(pairs, L):
    """Write pairs as dot-bracket, putting crossing pairs on bracket pages."""
    pages = []
    for p in sorted(pairs):
        for page in pages:
            if not any(a < p[0] < b < p[1] or p[0] < a < p[1] < b for a, b in page):
                page.append(p); break
        else:
            pages.append([p])
    assert len(pages) <= 4
    s = ["."] * L
    for (o, c), page in zip(["()", "[]", "{}", "<>"], pages):
        for i, j in page:
            s[i - 1], s[j - 1] = o, c
    return "".join(s)


class TestOracle(unittest.TestCase):
    def test_oracle_is_exactly_one(self):
        rng = random.Random(0)
        rows = []
        while len(rows) < 300:
            L = rng.randint(20, 300)
            pr = random_structure(rng, L, pk=rng.random() < 0.3)
            if len(pr) < 3:
                continue
            try:
                db = pairs_to_db(pr, L)
            except AssertionError:
                continue
            rows.append({"id": f"s{len(rows)}", "family": f"RF{len(rows) % 17}",
                         "clan": None, "pairs": [list(p) for p in sorted(pr)], "structure": db})
        with tempfile.TemporaryDirectory() as d:
            lp = os.path.join(d, "labels.jsonl")
            with open(lp, "w") as fh:
                for r in rows:
                    fh.write(json.dumps(r) + "\n")
            for key in ("structure", "pairs"):
                pp = os.path.join(d, f"pred_{key}.jsonl")
                with open(pp, "w") as fh:
                    for r in rows:
                        fh.write(json.dumps({"id": r["id"], key: r[key]}) + "\n")
                labels = rs.load_labels(lp)
                preds, c = rs.load_predictions(pp)
                res = rs.score(labels, preds, c)
                self.assertEqual(res["score"], 1.0)
                for blk in ("family_macro", "sequence_mean"):
                    for k, v in res[blk].items():
                        self.assertEqual(v, 1.0, (blk, k))
                self.assertEqual(res["micro"]["f1"], 1.0)
            # CLI end to end
            out = subprocess.run([sys.executable, os.path.join(HERE, "rna_score.py"),
                                  "--pred", os.path.join(d, "pred_structure.jsonl"),
                                  "--labels", lp], capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(out.stdout)["score"], 1.0)
            # missing prediction file -> 0, not a crash
            out = subprocess.run([sys.executable, os.path.join(HERE, "rna_score.py"),
                                  "--pred", os.path.join(d, "nope.jsonl"),
                                  "--labels", lp], capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(out.stdout)["score"], 0.0)


if __name__ == "__main__":
    unittest.main()
