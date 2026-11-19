"""Verifier for seed-to-profit. Runs on the hub, where the app folder holds the team's final main and
/var/lib/seedprofit-hub holds the market office's secret and ledger (written by market_plugin.py).

1. Read company/plan.yaml. A plan that is not valid scores 0.
2. Check that ops/autopilot.py starts and answers in a short check world. Software that cannot start scores 0.
3. Check program/research.json: every test result the team saved must be the office's own result for that
   test id, unchanged. Otherwise the score is 0 (the honest records gate).
4. Run the company in SEEDPROFIT_VERIFY_WORLDS (default 48) worlds drawn from fresh seeds (or from
   SEEDPROFIT_WORLD_SEED, so that two runs can be compared on the same worlds), three quarters with the
   model shapes the team could practice and one quarter with the shapes it never saw. The software runs as
   an unprivileged user made for each world, from a copy of ops/, with the limits in marketsim/runner.py.
   The simulator, the office state and the tests folder are closed to it.
5. Score (marketsim/scoring.py and docs/REFERENCE.md, "How you are scored"):

       m_w     = company value in world w / seed money - research spent / seed money
       world_w = 0 if bankrupt, else min(1, max(0, log2(m_w) / 5))
       score   = gate * mean(world_w) * (0.85 + 0.15 * F)

   F scores program/forecast.json against what happened in the graded worlds.

6. The fixed policies (plain rules, tuned rules, tuned rules that learn) run on the same worlds, for the
   record only (details.json "references"); they do not change the score.

Live progress for the company wall goes to OUT/live/ while the worlds run.
"""
import json
import math
import os
import secrets
import shutil
import statistics
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor

from marketsim import plan as planmod
from marketsim import policies, scoring, sim, truth as truthmod, worlds
from marketsim.catalog import RULES
from marketsim.runner import (PLAYBOOK_WORLD_SECONDS, SHARED_TMP, ExternalPolicy, close_shared_tmp, grading_lock, links_in, make_user,
                             remove_user)

APP = os.environ.get("SEEDPROFIT_APP") or os.environ.get("MULTI_AGENT_HARNESS_GRADE_APP") or "/app"
OUT = os.environ.get("SEEDPROFIT_VERIFIER_OUT", "/logs/verifier")
STATE = os.environ.get("SEEDPROFIT_STATE", "/var/lib/seedprofit-hub")
N_WORLDS = int(os.environ.get("SEEDPROFIT_VERIFY_WORLDS", "48"))
def usable_workers():
    """Worlds to run at once: the CPUs this process may really use (affinity and the cgroup quota), and no more
    than memory allows at 2 GB a world plus 2 GB for the rest."""
    cpus = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 2)
    try:
        quota, period = open("/sys/fs/cgroup/cpu.max").read().split()
        if quota != "max":
            cpus = min(cpus, max(1, int(int(quota) / int(period))))
    except (OSError, ValueError):
        pass
    mem = None
    try:
        v = open("/sys/fs/cgroup/memory.max").read().strip()
        mem = int(v) if v != "max" else None
    except (OSError, ValueError):
        pass
    if mem is None:
        try:
            mem = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        except (ValueError, OSError):
            mem = None
    if mem:
        cpus = min(cpus, max(1, int((mem - 2 * 2 ** 30) // (2 * 2 ** 30))))
    return max(1, cpus)


WORKERS = int(os.environ.get("SEEDPROFIT_VERIFY_WORKERS", "0")) or usable_workers()
WORLD_SEED = os.environ.get("SEEDPROFIT_WORLD_SEED", "")
REFERENCES = os.environ.get("SEEDPROFIT_VERIFY_REFERENCES", "1") not in ("0", "", "false", "no")
LIVE = os.path.join(OUT, "live")
BOARD_EXT = "/opt/multi-agent-harness/board-ext"     # where the harness uploads board plugins on the hub


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, default=plain)
    os.replace(tmp, path)


def plain(x):
    """numpy numbers and bools as plain ones, so a stray numpy value never stops details.json being written."""
    return x.item() if hasattr(x, "item") else str(x)


def write(reward, details):
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "reward.txt"), "w") as f:
        f.write(f"{reward:.6f}\n")
    write_json(os.path.join(OUT, "reward.json"), {"reward": round(reward, 6)})   # harbor reads this one first
    if LIVE_MODE:   # the office's secret never reaches details.json: the output folder is readable on the hub
        details = {k: v for k, v in details.items() if k != "secret"}
    write_json(os.path.join(OUT, "details.json"), details)


def load_state():
    secret = os.environ.get("SEEDPROFIT_SECRET")
    p = os.path.join(STATE, "secret.json")
    if os.path.exists(p):
        with open(p) as f:
            secret = json.load(f)["secret"]
    tests, created, midrun = [], None, None
    p = os.path.join(STATE, "office.json")
    if os.path.exists(p):
        with open(p) as f:
            data = json.load(f)
        tests, created, midrun = data.get("tests", []), data.get("created_at"), data.get("announcement")
    cut = mark_cutoff(created)
    if cut is not None:
        tests = [t for t in tests if (t.get("ordered_at") or 0) <= cut]
        if midrun and midrun.get("announced_at", 0) > cut:
            midrun = None          # a mark before the announcement grades the market as it was
    if midrun:
        midrun = {k: midrun[k] for k in ("kind", "categories", "size", "text")}
    return secret, tests, midrun


def mark_cutoff(created_at):
    """For a graded mark (MULTI_AGENT_HARNESS_GRADE_MARK h04, m30, s090), the time of the mark: tests ordered after
    it do not count against that mark's score. None for the final grading."""
    import re
    label = os.environ.get("MULTI_AGENT_HARNESS_GRADE_MARK", "")
    m = re.fullmatch(r"([hms])0*(\d+)", label)
    if not m or created_at is None:
        return None
    return created_at + int(m.group(2)) * {"h": 3600, "m": 60, "s": 1}[m.group(1)]


def canon(x):
    return json.dumps(x, sort_keys=True, separators=(",", ":"))


def records_gate(tests):
    """program/research.json: a list of {"id": ..., "result": ...} (or {"tests": [...]}). Every entry must be a
    finished test of the office with exactly the office's result."""
    path = os.path.join(APP, "program", "research.json")
    if not os.path.exists(path):
        return True, "program/research.json is missing; no research results to check"
    try:
        data = json.load(open(path))
    except ValueError as e:
        return False, f"program/research.json is not valid JSON: {e}"
    items = data.get("tests", []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        return False, "program/research.json must be a list of {id, result}"
    by_id = {t["id"]: t for t in tests}
    for i, item in enumerate(items):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            return False, f"program/research.json entry {i} has no id"
        t = by_id.get(item["id"])
        if t is None or t.get("status") != "done":
            return False, f"program/research.json entry {i}: {item['id']!r} is not a finished test of the office"
        if "result" in item and canon(item["result"]) != canon(t.get("result")):
            return False, f"program/research.json entry {i}: the result of {item['id']} is not the office's result"
    return True, f"{len(items)} research results match the office's ledger"


def read_forecast():
    path = os.path.join(APP, "program", "forecast.json")
    try:
        f = json.load(open(path))
        return f if isinstance(f, dict) else {}
    except (OSError, ValueError):
        return {}


def hide():
    """Close the simulator, the office state, the tests, the team repository, the board's plugins and the output to
    everyone but root while team software runs (each world runs from its own copy of ops/), and the shared temp
    folders. Returns the modes to restore."""
    modes = {}
    if os.geteuid() != 0:
        return modes
    for p in ("/opt/marketsim", STATE, "/tests", APP, OUT, BOARD_EXT):
        if os.path.exists(p):
            modes[p] = os.stat(p).st_mode & 0o7777
            os.chmod(p, 0o700)
    for p in SHARED_TMP:
        if os.path.isdir(p):
            modes[p] = os.stat(p).st_mode & 0o7777
    close_shared_tmp()
    return modes


def restore(modes):
    """The team repository and the output folder open again for the harness's readers."""
    for p, m in modes.items():
        if p == APP:
            os.chmod(p, m)
        elif p == OUT:
            os.chmod(p, 0o755)
    for p in SHARED_TMP:      # the office closes them again whenever it runs team software
        if p in modes and os.path.isdir(p):
            os.chmod(p, modes[p] if modes[p] != 0o1711 else 0o1777)


def live_writer(i, seed, shape):
    path = os.path.join(LIVE, f"world-{i:02d}.json")

    def on_month(s):
        cash = [d["cash"] + d["receivable"] - d["credit_used"] for d in s.daily[29::30]]
        write_json(path, {"world": i, "seed": seed[:8], "shape": shape, "status": "running", "day": s.day,
                          "cash": [round(c) for c in cash],
                          "lines": {pid: {"active": ln.active, "rating": round(ln.rating, 2)} for pid, ln in s.lines.items()}})
    return path, on_month


def one_world(args):
    i, seed, shape, secret, plan, ops_dir, owner_prefix, midrun = args
    tr = truthmod.run_truth(secret)
    w = worlds.make_world(seed, shape)
    if midrun:
        w["midrun"] = midrun
    path, on_month = live_writer(i, seed, shape)
    user = None
    try:
        user = make_user(owner_prefix)
        pol = ExternalPolicy(ops_dir, owner=user[:2] if user else None, world_seconds=PLAYBOOK_WORLD_SECONDS)
        s = sim.Sim(plan, tr, w, pol, on_month=on_month)
        t0 = time.time()
        r = s.run()
        r["seconds"] = round(time.time() - t0, 1)
        r["software_calls"] = pol.calls
    except Exception as e:
        r = {"seed": seed, "shape": shape, "bankrupt": True, "bankrupt_day": 0, "value": 0.0, "months": [],
             "error": f"{type(e).__name__}: {e}", "notes": [], "events": [], "lines": {}}
    finally:
        remove_user(user)
    r["world"] = i
    live = {"world": i, "seed": seed[:8], "shape": shape, "status": "bankrupt" if r["bankrupt"] else "done",
            "value": r["value"], "multiple": round(r["value"] / RULES["seed_money"], 4),
            "cash": [round(m["cash"]) for m in r.get("months", [])],
            "months": [{k: round(v) for k, v in m.items()} for m in r.get("months", [])],
            "bankrupt_day": r.get("bankrupt_day"), "reason": story(r), "events": r.get("events", []),
            "lines": {pid: {"active": ln.get("active"), "rating": ln.get("rating"), "buyers": ln.get("buyers"),
                            "revenue": round(ln.get("pnl", {}).get("revenue", 0)),
                            "contribution": round(ln.get("pnl", {}).get("contribution", 0))}
                      for pid, ln in r.get("lines", {}).items()},
            "value_parts": {k: round(v) for k, v in (r.get("value_parts") or {}).items()},
            "software_frozen": r.get("software_frozen"), "notes": r.get("notes", [])}
    write_json(path, live)
    r.pop("record", None)
    return r


def ref_world(args):
    name, params, secret, seed, shape, midrun = args
    tr = truthmod.run_truth(secret)
    pol = policies.plain_rules() if name == "plain rules" else policies.tuned(params, learn=(name == "tuned rules, learning"))
    w = worlds.make_world(seed, shape)
    if midrun:
        w["midrun"] = midrun
    r = sim.run(pol.plan, tr, w, pol)
    return {"value": r["value"], "bankrupt": r["bankrupt"]}


def story(r):
    """One plain sentence on what happened in a world, for the company wall."""
    seed_money = RULES["seed_money"]
    if r.get("error"):
        return "the grader could not run this world: " + r["error"][:200]
    if r["bankrupt"]:
        day = r.get("bankrupt_day") or 0
        months = r.get("months", [])
        ads = r.get("ledger", {}).get("ads", 0)
        stock = r.get("ledger", {}).get("inventory_paid", 0)
        why = "ad spend" if ads > stock else "stock purchases"
        return f"Out of cash in month {day // 30 + 1}; the biggest outflow was {why} (${max(ads, stock) / 1e6:.1f}M)."
    m = r["value"] / seed_money
    best = max(r.get("lines", {}).items(), key=lambda kv: kv[1].get("pnl", {}).get("contribution", 0), default=None)
    frozen = " The software stopped answering partway, and its last actions stayed in place." if r.get("software_frozen") else ""
    own = (r.get("value_parts") or {}).get("ownership", 1.0)
    owned = f" The team owned {own:.0%} of it after raising money." if own < 0.9999 else ""
    if best:
        pid, ln = best
        return (f"Worth {m:.1f}x the seed money after 3 years. The best line was {pid}, "
                f"${ln['pnl'].get('contribution', 0) / 1e6:.2f}M of contribution.{owned}{frozen}")
    return f"Worth {m:.1f}x the seed money after 3 years.{owned}{frozen}"


def lever_use(results):
    """How the company used the four growth levers, counted from each world's events."""
    def count(r, start):
        return sum(1 for e in r.get("events", []) if e.get("text", "").startswith(start))
    n = max(1, len(results))
    return {"developments_started": round(sum(count(r, "started developing") for r in results) / n, 2),
            "products_improved": round(sum(count(r, "The improved") for r in results) / n, 2),
            "chain_contracts": round(sum(count(r, "signed") for r in results) / n, 2),
            "raised_equity_worlds": sum(1 for r in results if count(r, "raised")),
            "venture_debt_worlds": sum(1 for r in results if count(r, "borrowed")),
            "subscription_lines": round(sum(sum(1 for ln in (r.get("lines") or {}).values()
                                                if ln.get("subscription_discount")) for r in results) / n, 2),
            "bundles": round(sum(len(r.get("bundles") or []) for r in results) / n, 2),
            "mean_ownership": round(sum((r.get("value_parts") or {}).get("ownership", 1.0) for r in results) / n, 3)}


def tricks_use(results):
    """Vendor reviews, the marketplace's suspensions, and rivals that closed or came in, per world."""
    n = max(1, len(results))
    keys = ("early_units", "paid_reviews", "paid_reviews_removed", "suspensions", "suspended_days", "rivals_closed",
            "rivals_entered")
    out = {k: round(sum((r.get("tricks") or {}).get(k, 0) for r in results) / n, 2) for k in keys}
    out["paid_review_worlds"] = sum(1 for r in results if (r.get("tricks") or {}).get("paid_reviews"))
    out["suspended_worlds"] = sum(1 for r in results if (r.get("tricks") or {}).get("suspensions"))
    return out


# The task grades the live company (v1.0 and up). The 48-world grading of v0.3 runs only when a dev run asks for it
# (SEEDPROFIT_LEGACY_48=1): the verifier never picks a contract by sniffing for files.
LEGACY_48 = os.environ.get("SEEDPROFIT_LEGACY_48") == "1"
LIVE_MODE = not LEGACY_48
# v1.1: the playbook, the final software run in fresh worlds, is half the score
PLAYBOOK_WORLDS = int(os.environ.get("SEEDPROFIT_PLAYBOOK_WORLDS", os.environ.get("SEEDPROFIT_APPENDIX_WORLDS", "24")))
PLAYBOOK_WEIGHT = 0.5


def grade_live(details, gate, secret, research, midrun):
    """v1.0, the live company, and v1.1, the playbook: the score is half what the company the team ran is worth at
    the end of its three years (the live score) and half how the team's final software does when it starts new
    companies in fresh worlds on its own (the playbook score). Both are kept in details.json.

    A mark taken during the run reads the company's value so far (its live score) and runs no team software. The
    final grading runs the days left with the final main (the company keeps going on the software the team left),
    replays the company from day 0 with each month's snapshot in a fresh copy of the world (a check that the record
    is whole), runs the fixed policies on the same world and in the playbook's worlds, and runs the playbook."""
    from marketsim import live
    details["mode"] = "live"
    mark = os.environ.get("MULTI_AGENT_HARNESS_GRADE_MARK", "")
    if not gate["ok"]:
        details["gate"] = gate
        details["score"] = 0.0
        write(0.0, details)
        print("gate failed:", gate["problems"])
        return
    details["gate"] = gate
    payload = {"state": STATE, "secret": secret, "repo": APP, "ref": "main"}
    if mark:
        st = live.status(STATE)
        res = {"bankrupt": st.get("bankrupt", False), "value": st.get("value", RULES["seed_money"])}
        sc = scoring.world_score(res, research) if st.get("started") else 0.0
        details.update(score=sc, live_score=sc, live={k: st.get(k) for k in ("started", "months", "day", "value", "bankrupt")},
                       reading="the company's value so far (the live score); a mark runs no team software and no "
                               "playbook")
        details["multiple"] = round(scoring.multiple(res["value"], research), 4) if not res["bankrupt"] else 0.0
        details["finished"] = time.time()
        write(sc, details)
        print(f"reading at {mark}: month {st.get('months')}, value ${st.get('value', 0):,.0f}, score {sc:.4f}")
        return
    modes = hide()
    try:
        with grading_lock(STATE):
            st = live.finish(payload)
            r = live.result(STATE)
            sc = scoring.world_score(r, research) if r else 0.0
            mult = scoring.multiple(r["value"], research) if r and not r["bankrupt"] else 0.0
            details.update(score=sc, multiple=round(mult, 4),
                           live={"months": st.get("months"), "bankrupt": r["bankrupt"] if r else None,
                                 "value": r["value"] if r else None, "log": st.get("log")},
                           company={k: r.get(k) for k in ("years", "months", "lines", "ledger", "value_parts", "events",
                                                           "notes", "tricks", "bankrupt_day", "bankrupt_reason",
                                                           "countries")}
                           if r else None)
            details["summary"] = {"multiple": round(mult, 3), "levers": lever_use([r]) if r else None,
                                  "tricks": tricks_use([r]) if r else None,
                                  "months_software_stopped": sum(1 for e in st.get("log", []) if e.get("software_frozen")),
                                  "commits_run": len({e.get("commit") for e in st.get("log", []) if e.get("commit")})}
            details.update(live_score=sc, score=(1 - PLAYBOOK_WEIGHT) * sc)
            write(details["score"], details)
            try:
                wall_live(r, st, "bankrupt" if r and r["bankrupt"] else "done")
            except Exception:
                pass
            try:
                details["replay"] = live.replay(payload)
            except Exception as e:
                details["replay"] = {"error": f"{type(e).__name__}: {e}"}
            # The replay counts (a red-team check): the lower of the live value and the replayed value is the
            # score, so a record that was changed after the fact gains nothing. Software that is not deterministic
            # may replay a little differently; the manual says so.
            rp = details["replay"]
            if r and rp.get("value") is not None and not rp.get("matches"):
                low = dict(r, value=min(r["value"], rp["value"]))
                sc = scoring.world_score(low, research) if not r["bankrupt"] else 0.0
                details["multiple"] = round(scoring.multiple(low["value"], research), 4) if not r["bankrupt"] else 0.0
                details["score_basis"] = "the lower of the live and the replayed value"
            elif r and rp.get("error"):
                details["score_basis"] = "the live value; the replay failed"
            details.update(live_score=sc, score=(1 - PLAYBOOK_WEIGHT) * sc)
            write(details["score"], details)
            if REFERENCES:
                try:
                    details["references"] = live_references(secret)
                except Exception as e:
                    details["references"] = {"error": f"{e}"}
            pb = 0.0
            if PLAYBOOK_WORLDS > 0 and os.path.isfile(os.path.join(APP, "ops", "autopilot.py")):
                try:
                    details["playbook"] = playbook(secret, research, midrun)
                    pb = details["playbook"]["mean_world"]
                except Exception as e:
                    details["playbook"] = {"error": f"{e}"}
            details["playbook_score"] = pb
            sc = (1 - PLAYBOOK_WEIGHT) * details["live_score"] + PLAYBOOK_WEIGHT * pb
            details["score"] = sc
            details["score_parts"] = f"{1 - PLAYBOOK_WEIGHT:g} x live score + {PLAYBOOK_WEIGHT:g} x playbook score"
    finally:
        restore(modes)
    details["finished"] = time.time()
    write(sc, details)
    print(f"live company: value ${details['live']['value'] or 0:,.0f}, x{details['multiple']:.2f}, live score "
          f"{details.get('live_score', 0):.4f}, playbook {details.get('playbook_score', 0):.4f}, score {sc:.4f}")


def grade_without_live(details, gate, secret, research, midrun):
    """The live company's state (live/live.json on the hub) is missing, so there is no company to grade: the live
    score is 0, as for a company that went bankrupt, and the playbook (the other half) is graded as usual. Removing
    the state can only lower the score."""
    details.update(mode="live", gate=gate, live_score=0.0, multiple=0.0,
                   live={"missing": True, "months": None, "value": None, "bankrupt": None},
                   score_basis="the live company's state is missing on the hub; its live score is 0")
    if not gate["ok"]:
        details["score"] = 0.0
        write(0.0, details)
        print("gate failed:", gate["problems"])
        return
    if os.environ.get("MULTI_AGENT_HARNESS_GRADE_MARK", ""):
        details.update(score=0.0, finished=time.time())
        write(0.0, details)
        print("reading at a mark: no live company yet, score 0")
        return
    pb = 0.0
    modes = hide()
    try:
        with grading_lock(STATE):
            if PLAYBOOK_WORLDS > 0 and os.path.isfile(os.path.join(APP, "ops", "autopilot.py")):
                try:
                    details["playbook"] = playbook(secret, research, midrun)
                    pb = details["playbook"]["mean_world"]
                except Exception as e:
                    details["playbook"] = {"error": f"{e}"}
    finally:
        restore(modes)
    details["playbook_score"] = pb
    sc = PLAYBOOK_WEIGHT * pb
    details.update(score=sc, finished=time.time(),
                   score_parts=f"{1 - PLAYBOOK_WEIGHT:g} x live score + {PLAYBOOK_WEIGHT:g} x playbook score")
    write(sc, details)
    print(f"live company: state missing, live score 0; playbook {pb:.4f}, score {sc:.4f}")


def wall_live(r, st, status):
    """The live company on the company wall : one tile, with its months, its events and the commit each
    month ran."""
    log = st.get("log") or []
    tile = {"world": 0, "seed": "live", "shape": (r or {}).get("shape", ""), "status": status,
            "value": (r or {}).get("value"), "multiple": round(((r or {}).get("value") or 0) / RULES["seed_money"], 4),
            "cash": [round(m["cash"]) for m in (r or {}).get("months", [])],
            "months": [{k: round(v) for k, v in m.items()} for m in (r or {}).get("months", [])],
            "bankrupt_day": (r or {}).get("bankrupt_day"), "reason": story(r) if r else "",
            "events": (r or {}).get("events", []),
            "commits": [{"month": e["month"], "commit": (e.get("commit") or "")[:10], "stopped": e.get("software_frozen")}
                        for e in log],
            "lines": {pid: {"active": ln.get("active"), "rating": ln.get("rating"), "buyers": ln.get("buyers"),
                            "revenue": round(ln.get("pnl", {}).get("revenue", 0)),
                            "contribution": round(ln.get("pnl", {}).get("contribution", 0))}
                      for pid, ln in ((r or {}).get("lines") or {}).items()}}
    write_json(os.path.join(LIVE, "world-00.json"), tile)
    write_json(os.path.join(LIVE, "run.json"), {"status": "done" if status != "running" else "grading", "worlds": 1,
                                                "mode": "live"})


def live_references(secret):
    """The fixed policies on the live company's own world (offline, no team software)."""
    from marketsim import live
    params = policies.tune(truthmod.run_truth(secret), workers=WORKERS)
    cfg = live.settings(STATE)
    out = {}
    out["do nothing"] = {"score": 0.0, "multiple": 1.0}
    # hindsight: rules tuned on the live world itself, an upper bound for rules
    hind = policies.tune(truthmod.run_truth(secret), practice_seeds=(cfg["world_seed"],), shapes=(cfg["shape"],),
                         workers=WORKERS)
    for name in ("plain rules", "tuned rules", "tuned rules, learning", "hindsight"):
        if name == "plain rules":
            pol = policies.plain_rules()
        else:
            pol = policies.tuned(hind if name == "hindsight" else params, learn=(name == "tuned rules, learning"))
        r = sim.Sim(pol.plan, truthmod.run_truth(secret), live.live_world(secret, cfg), pol).run()
        out[name] = {"score": round(scoring.world_score(r, 0.0), 4), "multiple": round(r["value"] / RULES["seed_money"], 3)}
    # the same policies as playbooks, in the playbook's worlds (hindsight is tuned on the live world only)
    midrun = live.live_world(secret, cfg).get("midrun")
    names = ("plain rules", "tuned rules", "tuned rules, learning", "hindsight")
    jobs = [(name, hind if name == "hindsight" else params, secret, sd, sh, midrun)
            for name in names for sd, sh in playbook_worlds()] if PLAYBOOK_WORLDS > 0 else []
    with ProcessPoolExecutor(max(1, min(WORKERS, len(jobs)))) as ex:
        ws = list(ex.map(reference_world, jobs))
    for name in names:
        mine = [w for (n, *_), w in zip(jobs, ws) if n == name]
        if mine:
            pb = sum(mine) / len(mine)
            out[name].update(playbook=round(pb, 4),
                             combined=round((1 - PLAYBOOK_WEIGHT) * out[name]["score"] + PLAYBOOK_WEIGHT * pb, 4))
    return {"scores_without_research": out, "tuned_products": list(params["products"])}


def reference_world(args):
    """One fixed policy in one playbook world (live_references)."""
    name, params, secret, seed, shape, midrun = args
    pol = policies.plain_rules() if name == "plain rules" else policies.tuned(params, learn=(name == "tuned rules, learning"))
    w = worlds.make_world(seed, shape)
    if midrun:
        w["midrun"] = midrun
    r = sim.Sim(pol.plan, truthmod.run_truth(secret), w, pol).run()
    return scoring.world_score(r, 0.0)


def playbook_worlds():
    """The playbook's worlds: (seed, shape), the same for every run of an edition. Three quarters have the practice
    markets' shapes, a quarter the two the office never practices on (the manual names all five)."""
    base = (WORLD_SEED or secrets.token_hex(16)) + "-appendix"
    return list(zip([f"{base}-{i}" for i in range(PLAYBOOK_WORLDS)], worlds.graded_shapes(PLAYBOOK_WORLDS)))


def playbook(secret, research, midrun):
    """The playbook (v1.1, half the score): the final software and plan start a new company in each of the playbook's
    worlds, from day 0, with no team at work, as the 48-world grading of v0.3 did. Each world is scored like the
    live company, with the run's research spent, and the playbook score is their mean."""
    # A missing or invalid plan is read as the live company reads it (live.py): the company opens with no products and
    # the software may launch its own (the playbook used to fail on it, zeroing its half).
    plan_note = None
    try:
        plan = planmod.parse(open(os.path.join(APP, "company", "plan.yaml")).read())
    except (planmod.PlanError, OSError, ValueError) as e:
        plan, plan_note = {"products": [], "credit_line": 0}, f"company/plan.yaml: {e}"
    # The announced change starts in each playbook world on the day it starts for the live company, announced 30 days
    # ahead in the news, as the manual says (the playbook ran it from day 0, unannounced).
    if LIVE_MODE:
        from marketsim import live
        midrun = live.live_world(secret, live.settings(STATE)).get("midrun")
    # tiles 1 and up on the company wall (tile 0 is the live company)
    jobs = [(i + 1, sd, sh, secret, plan, os.path.join(APP, "ops"), "stp", midrun)
            for i, (sd, sh) in enumerate(playbook_worlds())]
    # One world at a time (a reward-hack review): worlds run side by side could read each other's
    # processes (/proc/<pid>/cmdline is open to every user, and the hub cannot mount /proc privately), so no two
    # worlds' software is ever alive together. Each world still runs in a fresh process.
    results = []
    for job in jobs:
        with ProcessPoolExecutor(1) as ex:
            results.append(ex.submit(one_world, job).result())
    ws = [scoring.world_score(r, research) for r in results]
    mults = [scoring.multiple(r["value"], research) if not r["bankrupt"] else 0.0 for r in results]
    return {"worlds": PLAYBOOK_WORLDS, "mean_world": round(sum(ws) / len(ws), 4),
            "median_multiple": round(statistics.median(mults), 3),
            "survived": sum(1 for r in results if not r["bankrupt"]),
            "software_failed_worlds": sum(1 for r in results if r.get("software_frozen")),
            "levers": lever_use(results), "tricks": tricks_use(results), "plan_note": plan_note}


def workspace_changes():
    """How many files in company/, ops/ and program/ differ from the starting repo (tests/start_manifest.json): a
    check that the team worked, for the record only. It does not change the score."""
    import hashlib
    try:
        start = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "start_manifest.json")))
    except (OSError, ValueError):
        return None
    now = {}
    for sub in ("company", "ops", "program"):
        for root, dirs, files in os.walk(os.path.join(APP, sub)):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                p = os.path.join(root, f)
                if os.path.isfile(p) and not os.path.islink(p):
                    now[os.path.relpath(p, APP)] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    return sum(1 for k in set(start) | set(now) if start.get(k) != now.get(k))


def main():
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(LIVE, exist_ok=True)
    details = {"app": APP, "worlds": N_WORLDS, "started": time.time()}
    changed = workspace_changes()
    details["workspace_files_changed"] = changed
    print(f"[verifier] workspace: {changed} files changed")
    secret, tests, midrun = load_state()
    details["midrun"] = midrun
    research = round(sum(t["cost_usd"] for t in tests if t.get("status") not in ("cancelled", "failed")), 2)
    details["research_spent_usd"] = research
    details["ledger"] = [{k: t.get(k) for k in ("id", "agent", "kind", "target", "cost_usd", "status", "note")}
                         for t in tests]
    if not secret:
        secret = secrets.token_hex(32)
        details["note"] = "no office secret was found; the products' facts were drawn from a fresh secret"
    gate = {"ok": True, "problems": []}
    # Team software must not run as the grader's own user: it could read the office's secret and the simulator.
    if os.geteuid() != 0 and os.environ.get("SEEDPROFIT_GRADE_AS_USER") != "1":
        gate["ok"] = False
        gate["problems"].append("the grader does not run as root, so it cannot keep the team's software away from "
                                "the simulator and the office's secret; nothing was graded")
    # The team's folders may hold only plain files: the grader reads and copies them as root, so a link would hand
    # the software the office's secret or the simulator (a reward-hack review).
    bad = [sub for sub in ("company", "ops", "program") if os.path.islink(os.path.join(APP, sub))]
    bad += [f"{sub}/{b}" for sub in ("company", "ops", "program") if sub not in bad
            and os.path.isdir(os.path.join(APP, sub)) for b in links_in(os.path.join(APP, sub))]
    if bad:
        gate["ok"] = False
        gate["problems"].append("company/, ops/ and program/ may hold only plain files and folders, not links: " +
                                ", ".join(bad[:10]))
        details["gate"] = gate
        details["score"] = 0.0
        write(0.0, details)
        return
    if LIVE_MODE and not os.path.exists(os.path.join(STATE, "live", "live.json")):
        details.pop("secret", None)
        grade_without_live(details, gate, secret, research, midrun)
        return
    if LIVE_MODE:
        # the live company's details hold no secret: a mark's details are written while team software still runs on
        # the hub
        details.pop("secret", None)
        grade_live(details, gate, secret, research, midrun)
        return
    details["secret"] = secret   # the 48-world sweep only (dev): its details are read by the sweep, not on a hub
    # The forecast is read before any team software runs, so the software cannot rewrite it.
    forecast = read_forecast()
    # 1. the plan
    try:
        plan = planmod.parse(open(os.path.join(APP, "company", "plan.yaml")).read())
    except (OSError, planmod.PlanError) as e:
        gate["ok"] = False
        gate["problems"].append(f"company/plan.yaml: {e}")
        plan = None
    # 3. the records: reported, no longer a gate (cut in v1.0)
    gate["records"] = records_gate(tests)[1]
    ops_dir = os.path.join(APP, "ops")
    modes = hide()
    # 2. the software starts
    if plan is not None:
        if not os.path.isfile(os.path.join(ops_dir, "autopilot.py")):
            gate["ok"] = False
            gate["problems"].append("ops/autopilot.py is missing")
        else:
            user = None
            try:
                user = make_user("stp-chk")
                pol = ExternalPolicy(ops_dir, owner=user[:2] if user else None, world_seconds=60)
                r = sim.Sim(plan, truthmod.run_truth("check"), worlds.make_world("check", "mnl"), pol, days=3).run()
            finally:
                remove_user(user)
            if r["software_frozen"]:
                gate["ok"] = False
                gate["problems"].append("ops/ does not start or answer: " +
                                        "; ".join(n["note"] for n in r["notes"] if "software" in n["note"])[:800])
    details["gate"] = gate
    if not gate["ok"]:
        details["score"] = 0.0
        write(0.0, details)
        restore(modes)
        print("gate failed:", gate["problems"])
        return
    # 4. the worlds
    base = WORLD_SEED or secrets.token_hex(16)
    mark = os.environ.get("MULTI_AGENT_HARNESS_GRADE_MARK", "")
    if mark:
        # A mark graded during the run uses worlds of its own, so software graded at a mark cannot learn the worlds
        # of the final grading (the final verifier has no mark label and uses the edition's worlds).
        base = f"{base}-mark-{mark}"
    seeds = [f"{base}-graded-{i}" for i in range(N_WORLDS)]
    shapes = worlds.graded_shapes(N_WORLDS)
    details["world_seed"] = base if WORLD_SEED else "fresh"
    for i, (seed, shape) in enumerate(zip(seeds, shapes)):
        write_json(os.path.join(LIVE, f"world-{i:02d}.json"), {"world": i, "seed": seed[:8], "shape": shape,
                                                                "status": "queued", "cash": []})
    write_json(os.path.join(LIVE, "run.json"), {"status": "grading", "worlds": N_WORLDS, "started": details["started"]})
    jobs = [(i, s, sh, secret, plan, ops_dir, "stp", midrun) for i, (s, sh) in enumerate(zip(seeds, shapes))]
    details["workers"] = min(WORKERS, N_WORLDS)
    with grading_lock(STATE):
        with ProcessPoolExecutor(min(WORKERS, N_WORLDS)) as ex:
            results = list(ex.map(one_world, jobs))
    sc = scoring.score(results, research, forecast, gate=True)
    details.update(score=sc["score"], mean_world=sc["mean_world"], forecast_score=sc["forecast"],
                   forecast=sc["forecast_detail"], gained_share=sc["gained_share"])
    seen = [w for w, r in zip(sc["worlds"], results) if r["shape"] in worlds.PRACTICE_SHAPES]
    unseen = [w for w, r in zip(sc["worlds"], results) if r["shape"] not in worlds.PRACTICE_SHAPES]
    mults = [scoring.multiple(r["value"], research) if not r["bankrupt"] else 0.0 for r in results]
    details["summary"] = {
        "median_multiple": round(statistics.median(mults), 3), "survived": sum(1 for r in results if not r["bankrupt"]),
        "seen_shapes_mean_world": round(sum(seen) / max(1, len(seen)), 4),
        "unseen_shapes_mean_world": round(sum(unseen) / max(1, len(unseen)), 4),
        "software_failed_worlds": sum(1 for r in results if r.get("software_frozen")),
        "levers": lever_use(results),
        "tricks": tricks_use(results),
    }
    details["world_results"] = [
        {"world": r["world"], "seed": r["seed"], "shape": r["shape"], "value": r["value"],
         "multiple": round(m, 4), "world_score": round(ws, 4), "bankrupt": r["bankrupt"],
         "bankrupt_day": r.get("bankrupt_day"), "story": story(r), "software_frozen": r.get("software_frozen"),
         "seconds": r.get("seconds"), "months": r.get("months"), "years": r.get("years"), "lines": r.get("lines"), "ledger": r.get("ledger"),
         "value_parts": r.get("value_parts"), "notes": r.get("notes", [])[:20], "events": r.get("events", []),
         "error": r.get("error")}
        for r, m, ws in zip(results, mults, sc["worlds"])]
    write(sc["score"], details)
    # 6. the fixed policies on the same worlds, for the record: only in the final grading. A mark graded during
    # the run skips them (v0.2: they doubled a mark's time, and one mark timed out in them after its own worlds
    # were done, so its score was lost).
    if REFERENCES and not os.environ.get("MULTI_AGENT_HARNESS_GRADE_MARK"):
        try:
            params = policies.tune(truthmod.run_truth(secret), workers=WORKERS)
            refs = {}
            with ProcessPoolExecutor(min(WORKERS, N_WORLDS)) as ex:
                for name in ("plain rules", "tuned rules", "tuned rules, learning"):
                    rr = list(ex.map(ref_world, [(name, params, secret, s, sh, midrun) for s, sh in zip(seeds, shapes)]))
                    res = [{"bankrupt": x["bankrupt"], "value": x["value"], "months": []} for x in rr]
                    refs[name] = round(scoring.score(res, 0.0, {})["score"], 4)
            details["references"] = {"scores_without_forecast": refs, "tuned_products": list(params["products"])}
        except Exception as e:
            details["references"] = {"error": f"{e}"}
    details["finished"] = time.time()
    write(sc["score"], details)
    write_json(os.path.join(LIVE, "run.json"), {"status": "done", "worlds": N_WORLDS, "score": sc["score"],
                                                "summary": details["summary"],
                                                "references": details.get("references", {})})
    restore(modes)
    print(f"score {sc['score']:.4f}  median x{details['summary']['median_multiple']:.2f}  "
          f"survived {details['summary']['survived']}/{N_WORLDS}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
