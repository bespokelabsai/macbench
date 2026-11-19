"""LEGION verifier. Run as `python3 -I -S /tests/verify.py` by test.sh, on the hub (the harness's shared
verifier). Trusts only /tests: the match harness used here is the copy in /tests/harness, never the team's
harness/, and Petra comes from /tests/hidden/petra.zip (LEGION_PETRA_ZIP).

A graded mark (multi-agent-harness --ak grade_at) grades a checkout of main pinned at the mark and writes to a
folder of its own: MULTI_AGENT_HARNESS_GRADE_APP and MULTI_AGENT_HARNESS_GRADE_OUT. The final grading has
neither, and grades /app into /logs/verifier.

LEGION_VERIFY_MATCHES (default all 240) grades on a fixed, stratified subset of the hidden gauntlet: the same
matches every time, in the same shares of levels and personalities. LEGION_VERIFY_JOBS sets how many games run at
once (default: one per usable CPU, keeping two free)."""
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS / "harness"))
import legion  # noqa: E402  (the trusted copy)

APP = Path(os.environ.get("MULTI_AGENT_HARNESS_GRADE_APP") or "/app")
LOGS = Path(os.environ.get("MULTI_AGENT_HARNESS_GRADE_OUT") or "/logs/verifier")
SUBMISSION = APP / "legion"
# A mark during the run (MULTI_AGENT_HARNESS_GRADE_APP set) is a quick grading on LEGION_MARK_MATCHES matches (when
# set); the final grading plays LEGION_VERIFY_MATCHES. Every grading saves the AI it graded (solution.tar.gz: legion/
# and AI.md) so every mark can be graded again in full after the run.
QUICK = bool(os.environ.get("MULTI_AGENT_HARNESS_GRADE_APP")) and bool(os.environ.get("LEGION_MARK_MATCHES"))
AI_MD = APP / "AI.md"
MAX_BYTES = 20 * 1024 * 1024
ALLOWED_IMPORTS = ("simulation/ai/common-api/", "simulation/ai/legion/bot/")
EXPORTS_BOT = re.compile(r"export\s+(?:function|class|const|let|var)\s+LegionBot\b|export\s*\{[^}]*\bLegionBot\b")
# sha256 of the starter bot's main.js, only to report whether the graded AI is still the starter (not scored)
STARTER_MAIN_SHA256 = "b204b1b59f8e9d32330d13fa8c9fed2bbdcf3e8d7c64c6a8e78b2ac25aff6f18"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def check_integrity():
    expected = json.loads((TESTS / "integrity.json").read_text())
    return [path for path, digest in expected.items() if not os.path.isfile(path) or sha256(path) != digest]


def module_specifiers(text):
    """Every module path a file imports or re-exports, and whether it uses
    dynamic import(). Uses the JS tokenizer, so spacing cannot hide an import
    (`import{X}from"path"` is valid JavaScript)."""
    tokens = [(m.lastgroup, m.group()) for m in JS_TOKEN.finditer(text) if m.lastgroup != "comment"]
    specifiers, dynamic = [], False
    for i, (kind, value) in enumerate(tokens):
        if kind != "ident" or value not in ("import", "export") or (i and tokens[i - 1][1] in (".", "?.")):
            continue
        following = tokens[i + 1] if i + 1 < len(tokens) else (None, None)
        if value == "import" and following[1] == "(":
            dynamic = True
            continue
        if value == "import" and following[1] == ".":  # import.meta
            continue
        if value == "import" and following[0] == "string":  # import "x"
            specifiers.append(following[1][1:-1])
            continue
        for j in range(i + 1, len(tokens)):  # ... from "x" before the statement ends
            k, v = tokens[j]
            if v == ";" or (k == "ident" and v in ("import", "export") and tokens[j - 1][1] not in (".", "?.")):
                break
            if value == "export" and k == "ident" and v in ("function", "class", "const", "let", "var", "default", "async"):
                break
            if k == "ident" and v == "from" and j + 1 < len(tokens) and tokens[j + 1][0] == "string":
                specifiers.append(tokens[j + 1][1][1:-1])
                break
    return specifiers, dynamic


def static_checks(bot):
    problems = []
    files = legion.bot_files(bot)
    if not (bot / "main.js").is_file():
        problems.append("missing legion/main.js")
    elif not EXPORTS_BOT.search((bot / "main.js").read_text(errors="replace")):
        problems.append("main.js does not export LegionBot")
    size = sum(p.stat().st_size for p in files)
    if size > MAX_BYTES:
        problems.append(f"legion/ is {size} bytes, over {MAX_BYTES}")
    for path in files:
        if path.suffix != ".js":
            continue
        text = path.read_text(errors="replace")
        rel = path.relative_to(bot)
        specifiers, dynamic = module_specifiers(text)
        if dynamic:
            problems.append(f"{rel}: dynamic import()")
        for target in specifiers:
            if not target.startswith(ALLOWED_IMPORTS) or ".." in target or "\\" in target:
                problems.append(f"{rel}: import of {target} is not allowed")
    words = len(AI_MD.read_text(errors="replace").split()) if AI_MD.is_file() else 0
    if words < 100:
        problems.append(f"AI.md has {words} words; at least 100 are required")
    return problems


# --- originality check ---------------------------------------------------------
# Statement fingerprints: each statement is tokenised, identifiers and literals
# are replaced by placeholders, and the result is hashed. The score is how much
# of Petra the submission reproduces, so:
#   renaming identifiers changes nothing (they are placeholders),
#   padding cannot dilute it (the denominator is Petra, not the submission),
#   inserted junk statements leave the copied statements' fingerprints intact.
# Statements that also occur anywhere else in the game's scripts are generic
# JavaScript or shared API idiom, so they are not counted as Petra's.

JS_TOKEN = re.compile(r"""
    (?P<comment>//[^\n]*|/\*.*?\*/)
  | (?P<string>"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`)
  | (?P<number>0[xX][0-9a-fA-F]+|\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?|\.\d+)
  | (?P<ident>[A-Za-z_$][\w$]*)
  | (?P<punct>>>>=|===|!==|\*\*=|<<=|>>=|>>>|\.\.\.|=>|==|!=|<=|>=|&&|\|\||\?\?|\?\.|\+\+|--|[-+*/%&|^]=|\*\*|<<|>>|[{}()\[\];,.<>+\-*/%&|^!~?:=@])
""", re.S | re.X)
JS_KEYWORDS = set("""break case catch class const continue debugger default delete do else export extends finally for
function if import in instanceof let new of return super switch this throw try typeof var void while with yield async
await static get set null true false undefined from as""".split())
MIN_STATEMENT_TOKENS = 6
COVERAGE_LIMIT = 0.15         # share of Petra's distinctive statements
MODULE_COVERAGE_LIMIT = 0.30  # share of any one sizeable Petra module
MODULE_MIN_STATEMENTS = 40


def fingerprints(text):
    statement, out = [], set()

    def flush():
        if len(statement) >= MIN_STATEMENT_TOKENS:
            out.add(hashlib.sha1(" ".join(statement).encode()).hexdigest()[:16])
        statement.clear()

    for m in JS_TOKEN.finditer(text):
        kind = m.lastgroup
        value = m.group()
        if kind == "comment":
            continue
        if kind == "punct" and value in ";{}":
            flush()
            continue
        if kind == "ident":
            statement.append(value if value in JS_KEYWORDS else "I")
        elif kind == "string":
            statement.append("S")
        elif kind == "number":
            statement.append("N")
        else:
            statement.append(value)
    flush()
    return out


def petra_fingerprints():
    """Petra's distinctive statements, per module, minus those found in any
    other script of the game (generic code and shared API idiom)."""
    petra, generic = {}, set()
    for name, text in legion.petra_sources().items():
        if name.endswith(".js") and "/tests/" not in name:
            petra[name.split("/")[-1]] = fingerprints(text)
    with zipfile.ZipFile(legion.PUBLIC_ZIP) as z:
        for name in z.namelist():
            if not name.endswith(".js") or "/tests/" in name or name.startswith("simulation/ai/petra/"):
                continue
            generic |= fingerprints(z.read(name).decode(errors="replace"))
    return {module: prints - generic for module, prints in petra.items()}


def _coverage(petra, mine, min_size):
    everything = set().union(*petra.values())
    modules = {m: round(len(p & mine) / len(p), 4) for m, p in petra.items() if len(p) >= min_size}
    worst = max(modules, key=modules.get) if modules else None
    return {"coverage": round(len(everything & mine) / len(everything), 4) if everything else 0.0,
            "worst_module": worst, "worst_module_coverage": modules.get(worst, 0.0)}


def petra_coverage(bot):
    mine = set()
    for path in legion.bot_files(bot):
        if path.suffix == ".js":
            mine |= fingerprints(path.read_text(errors="replace"))
    return _coverage(petra_fingerprints(), mine, MODULE_MIN_STATEMENTS)


# Window fingerprints: a second detector that ignores notation. Exact statement
# hashes break under rewrites that keep the code's meaning: `a.b` as `a["b"]`,
# dropped semicolons, braces added around one-line bodies, extra parentheses.
# Here the token stream is canonicalised (bracket member access becomes dot
# access, === becomes ==, var/let/const become one keyword) and the punctuation
# such rewrites add or remove is dropped: ( ) { } ; ,. Every run of
# WINDOW_TOKENS consecutive tokens is hashed, so statement boundaries do not
# matter. Petra's fingerprints are winnowed (the minimum hash of every WINNOW
# consecutive windows, as in MOSS), which guarantees that any shared run of at
# least WINDOW_TOKENS + WINNOW - 1 tokens is found; windows that also occur in
# the game's other scripts are generic and not counted.

WINDOW_TOKENS = 12
WINNOW = 4
# Honest agent bots measure at most 0.9% overall and 4.9% for one module; whole
# or partial Petra copies under every rewrite tried measure 28-100% (task README).
WINDOW_COVERAGE_LIMIT = 0.05         # share of Petra's distinctive windows
WINDOW_MODULE_COVERAGE_LIMIT = 0.15  # share of any one sizeable Petra module
WINDOW_MODULE_MIN = 200
SKELETON_DROP = set("(){};,")
SKELETON_SAME = {"?.": ".", "===": "==", "!==": "!=", "let": "var", "const": "var"}


def skeleton(text):
    """Canonical token stream: identifiers I, strings S, numbers N, keywords and
    operators kept, `[ "name" ]` as `. I`, droppable punctuation removed."""
    raw = [m for m in JS_TOKEN.finditer(text) if m.lastgroup != "comment" and m.group() not in SKELETON_DROP]
    out, i = [], 0
    while i < len(raw):
        kind, value = raw[i].lastgroup, raw[i].group()
        if value == "[" and i + 2 < len(raw) and raw[i + 1].lastgroup == "string" and raw[i + 2].group() == "]":
            out += [".", "I"]
            i += 3
            continue
        if kind == "ident":
            out.append(SKELETON_SAME.get(value, value) if value in JS_KEYWORDS else "I")
        elif kind == "string":
            out.append("S")
        elif kind == "number":
            out.append("N")
        else:
            out.append(SKELETON_SAME.get(value, value))
        i += 1
    return out


def window_hashes(tokens):
    return [int.from_bytes(hashlib.blake2b(" ".join(tokens[i:i + WINDOW_TOKENS]).encode(), digest_size=8).digest(), "big")
            for i in range(len(tokens) - WINDOW_TOKENS + 1)]


def winnow(hashes):
    return {min(hashes[i:i + WINNOW]) for i in range(len(hashes) - WINNOW + 1)} if len(hashes) >= WINNOW else set(hashes)


def petra_windows():
    """Petra's winnowed window fingerprints per module, minus every window that
    occurs in another script of the game."""
    petra, generic = {}, set()
    for name, text in legion.petra_sources().items():
        if name.endswith(".js") and "/tests/" not in name:
            petra[name.split("/")[-1]] = winnow(window_hashes(skeleton(text)))
    with zipfile.ZipFile(legion.PUBLIC_ZIP) as z:
        for name in z.namelist():
            if not name.endswith(".js") or "/tests/" in name or name.startswith("simulation/ai/petra/"):
                continue
            generic.update(window_hashes(skeleton(z.read(name).decode(errors="replace"))))
    return {module: prints - generic for module, prints in petra.items()}


def petra_window_coverage(bot):
    mine = set()
    for path in legion.bot_files(bot):
        if path.suffix == ".js":
            mine.update(window_hashes(skeleton(path.read_text(errors="replace"))))
    return _coverage(petra_windows(), mine, WINDOW_MODULE_MIN)


def subset(matches, n):
    """A fixed subset of n hidden matches with the gauntlet's shares of every opponent (level and personality):
    each opponent keeps round(count * n / total) matches by largest remainder, its first ones in gauntlet order,
    so a smaller grading is always the same matches and its scale is the full gauntlet's."""
    total = len(matches)
    if n >= total:
        return list(matches)
    if n < 1:
        raise ValueError("LEGION_VERIFY_MATCHES must be at least 1")
    groups = {}
    for m in matches:
        groups.setdefault(legion.opponent_key(m), []).append(m)
    quota = {k: len(v) * n / total for k, v in groups.items()}
    take = {k: int(q) for k, q in quota.items()}
    for k in sorted(quota, key=lambda k: (-(quota[k] - take[k]), k))[:n - sum(take.values())]:
        take[k] += 1
    keep = {id(m) for k, v in groups.items() for m in v[:take[k]]}
    return [m for m in matches if id(m) in keep]


def save_solution():
    """The graded AI (legion/ and AI.md), so the mark can be graded again in full later."""
    import tarfile
    path = LOGS / "solution.tar.gz"
    with tarfile.open(path, "w:gz") as tf:
        for name in ("legion", "AI.md"):
            if (APP / name).exists():
                tf.add(APP / name, arcname=f"app/{name}",
                       filter=lambda ti: None if "node_modules" in ti.name or "/.git" in ti.name else ti)
    os.chmod(path, 0o644)
    (LOGS / "solution.json").write_text(json.dumps(dict(
        mark=os.environ.get("MULTI_AGENT_HARNESS_GRADE_MARK") or "final",
        commit=os.environ.get("MULTI_AGENT_HARNESS_GRADE_COMMIT"), kind="quick" if QUICK else "full")) + "\n")


def main():
    LOGS.mkdir(parents=True, exist_ok=True)
    try:
        save_solution()
    except Exception as exc:  # noqa: BLE001 - never let saving cost the grade
        print(f"verifier: could not save the solution: {exc!r}")
    reward = LOGS / "reward.txt"
    reward.write_text("0\n")
    try:
        main_js = SUBMISSION / "main.js"
        starter = main_js.is_file() and sha256(main_js) == STARTER_MAIN_SHA256 and len(list(SUBMISSION.rglob("*"))) == 1
        print(f"verifier: submission {'is the starter bot' if starter else 'differs from the starter bot'}; "
              f"AI.md {'present' if AI_MD.is_file() else 'missing'}", flush=True)
    except OSError:
        pass
    report = {"score": 0.0}
    try:
        bad = check_integrity()
        if bad:
            report["failure"] = "game files were modified"
            report["modified"] = bad
            return report
        if not SUBMISSION.is_dir():
            report["failure"] = "missing legion/"
            return report
        workdir = Path(tempfile.mkdtemp(prefix="legion-verify-"))
        os.chmod(workdir, 0o700)
        bot = workdir / "legion"
        shutil.copytree(SUBMISSION, bot, symlinks=False, ignore=shutil.ignore_patterns("node_modules", ".git"))
        problems = static_checks(bot)
        report["static_checks"] = problems or "passed"
        if problems:
            report["failure"] = "R01"
            return report
        petra = petra_coverage(bot)
        report["petra_coverage"] = petra
        if petra["coverage"] > COVERAGE_LIMIT or petra["worst_module_coverage"] > MODULE_COVERAGE_LIMIT:
            report["failure"] = (f"R05: the AI reproduces {petra['coverage']:.0%} of Petra's distinctive statements "
                                 f"({petra['worst_module_coverage']:.0%} of {petra['worst_module']}); limits "
                                 f"{COVERAGE_LIMIT:.0%} overall, {MODULE_COVERAGE_LIMIT:.0%} per module")
            return report
        windows = petra_window_coverage(bot)
        report["petra_window_coverage"] = windows
        if windows["coverage"] > WINDOW_COVERAGE_LIMIT or windows["worst_module_coverage"] > WINDOW_MODULE_COVERAGE_LIMIT:
            report["failure"] = (f"R05: the AI reproduces {windows['coverage']:.0%} of Petra's distinctive code windows "
                                 f"({windows['worst_module_coverage']:.0%} of {windows['worst_module']}); limits "
                                 f"{WINDOW_COVERAGE_LIMIT:.0%} overall, {WINDOW_MODULE_COVERAGE_LIMIT:.0%} per module")
            return report
        config = json.loads((TESTS / "gauntlet.json").read_text())
        count = os.environ.get("LEGION_MARK_MATCHES") if QUICK else os.environ.get("LEGION_VERIFY_MATCHES")
        config["matches"] = subset(config["matches"], int(count or len(config["matches"])))
        report["matches_graded"] = len(config["matches"])
        report["kind"] = "quick" if QUICK else "full"
        # Never more games than the hub has CPUs right now (one per game), so a hub that did not grow for grading
        # plays fewer at once instead of pushing games past the wall-time limit.
        jobs = min(int(os.environ.get("LEGION_VERIFY_JOBS") or 0) or legion.default_jobs(), max(1, legion.usable_cpus() - 1))
        report["jobs"] = jobs
        print(f"verifier: {len(config['matches'])} hidden matches, {jobs} at once", flush=True)
        results = legion.run_gauntlet(config, bot, jobs, verify=True, log=print)
        out = LOGS / "artifacts"
        out.mkdir(parents=True, exist_ok=True)
        (out / "results.json").write_text(json.dumps(legion._strip(results), indent=1) + "\n")
        summary = legion.score_results(results, json.loads((TESTS / "harness" / "ladder.json").read_text()))
        report.update(summary)
        report["bot_errors_total"] = sum(r["bot_errors"] for r in results)
        report["guard_blocked"] = sorted({b for r in results for b in r["guard_blocked"]})[:20]
        report["cheats_blocked"] = sum(r["cheats_blocked"] for r in results)
        return report
    except Exception as exc:  # noqa: BLE001 - any verifier error scores 0
        report["verifier_error"] = repr(exc)
        report["score"] = 0.0
        return report
    finally:
        (LOGS / "checks.json").write_text(json.dumps(report, indent=2) + "\n")
        reward.write_text(f"{report.get('score', 0.0)}\n")


if __name__ == "__main__":
    result = main()
    print(json.dumps({k: v for k, v in result.items() if k != "per_level"}, indent=2))
