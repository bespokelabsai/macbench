"""The live company (v1.0): one company in one world that advances a month at a time while the team works.

The market office's clock calls `month` once per simulated month (every few real minutes, after the launch
phase). Each call runs the next 30 days with the software on the team's `main` at that moment: it checks out
company/, ops/ and program/, starts the software with the company's saved state folder, runs the days, keeps
what the software wrote to the folder, and saves the world. The first call reads company/plan.yaml and opens
the company on day 0.

Everything lives in STATE/live/, which only root can read:

    live.json        the run's settings: the world's seed and shape, the day of the announced change
    company.pkl      the world and the company, between months
    log.json         one entry per month: the commit that ran, the days, the software's errors, the value
    books.json       what the company saw each day (its own books), for the team's free tools
    market.json      monthly category sales, for the market_report research
    snapshots/       the checkout each month ran, for the grader's replay
    software-state/  the software's state folder between months (company.pkl holds the copy that counts)

The grader reads the value, finishes any days left with the final `main`, and replays the log from day 0 in a
fresh copy of the world to check that it gives the same company.
"""
import contextlib
import fcntl
import io
import json
import math
import os
import pickle
import shutil
import subprocess
import tarfile

from . import countries as countrymod
from . import office as officemod
from . import plan as planmod
from . import sim as simmod
from . import truth as truthmod
from . import worlds
from .catalog import PRODUCTS, RULES
from .runner import ExternalPolicy, SoftwareError, close_shared_tmp, make_user, remove_user

STEP_DAYS = 30
MONTH_SECONDS = 60.0      # the software's time for one month (a graded world gives 300 s for three years)
BOOKS_DAYS = 120          # the most days one company_books call returns
CHANGE_DAY = 540          # the announced change starts in month 18 of the live company (announced 30 days ahead)


def folder(state):
    return os.path.join(state, "live")


@contextlib.contextmanager
def locked(state):
    os.makedirs(folder(state), exist_ok=True)
    os.chmod(folder(state), 0o700)
    with open(os.path.join(folder(state), "lock"), "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _read(state, name, default):
    try:
        with open(os.path.join(folder(state), name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write(state, name, data):
    p = os.path.join(folder(state), name)
    with open(p + ".tmp", "w") as f:
        json.dump(data, f)
    os.replace(p + ".tmp", p)


def settings(state):
    """live.json, written by the hub plugin (environment/hub/make_hub_plugin.py); missing keys get defaults."""
    cfg = _read(state, "live.json", {})
    cfg.setdefault("world_seed", "live")
    cfg.setdefault("shape", "mnl")
    cfg.setdefault("change_day", CHANGE_DAY)
    return cfg


def live_world(secret, cfg):
    """The live world: the edition's world, with the run's announced change starting on its day."""
    w = worlds.make_world(cfg["world_seed"], cfg["shape"])
    m = worlds.midrun_shock(secret)
    day = int(cfg["change_day"])
    if day:
        m["text"] = m["text"].replace("This applies to every graded market from its first day.",
                                      f"It starts on day {day} ({simmod._date(day).isoformat()}).")
        w["midrun"] = dict(m, start_day=day)
    return w


def load(state):
    p = os.path.join(folder(state), "company.pkl")
    if not os.path.exists(p):
        return None
    with open(p, "rb") as f:
        return pickle.load(f)


def save(state, s):
    p = os.path.join(folder(state), "company.pkl")
    pol, s.policy = s.policy, None
    try:
        with open(p + ".tmp", "wb") as f:
            pickle.dump(s, f)
        os.replace(p + ".tmp", p)
    finally:
        s.policy = pol


def resolve(repo, ref):
    """The commit a ref names, or None for a plain folder."""
    if not os.path.exists(os.path.join(repo, "HEAD")):
        return None
    try:
        return officemod.commit_of(repo, ref)
    except ValueError:
        return None


def keep_folder(path):
    """The software's kept state folder (plain files only, runner.save_state) as {relative path: bytes}, with
    folders as "name/": it is saved in company.pkl, with the company."""
    out = {}
    for d, dirs, files in os.walk(path):
        for name in dirs:
            out[os.path.relpath(os.path.join(d, name), path) + "/"] = b""
        for name in files:
            with open(os.path.join(d, name), "rb") as f:
                out[os.path.relpath(os.path.join(d, name), path)] = f.read()
    return out


def restore_folder(path, kept):
    """The state folder as keep_folder saw it, in place of whatever is there now."""
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, mode=0o700)
    for rel, data in sorted(kept.items()):
        p = os.path.join(path, rel)
        if rel.endswith("/"):
            os.makedirs(p, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)


def snapshot(root, path):
    with tarfile.open(path, "w") as t:
        for sub in ("company", "ops", "program"):
            if os.path.isdir(os.path.join(root, sub)):
                t.add(os.path.join(root, sub), arcname=sub)


def unpack(path):
    d = officemod.tempfile.mkdtemp(prefix="live-replay-")
    with tarfile.open(path) as t:
        t.extractall(d, filter="data")
    return d


class Idle:
    """The company's software when ops/ cannot run: nothing changes this month."""
    def init(self, setup):
        raise SoftwareError(self.why)

    def __init__(self, why):
        self.why = why

    def step(self, obs):
        return {}

    def close(self):
        pass

    def save_state(self):
        return None


BOOK_KEYS = ("active", "channels", "price", "ad_budget", "on_hand", "rating", "reviews", "supplier", "rival_prices",
             "subscribers", "site_customers", "suspended_until", "yesterday")


def _book_products(o):
    prods = {}
    for pid, p in o["products"].items():
        prods[pid] = {k: p[k] for k in BOOK_KEYS}
        prods[pid]["inbound_units"] = sum(x["qty"] for x in p["inbound"])
        if "typical_price" in p:
            prods[pid]["typical_price"] = p["typical_price"]
    return prods


def compact(s):
    """One day of the company's own books: what its software sees, without the market's hidden facts. Each country
    the company entered has its own books, under "countries"."""
    o = s.observe()
    out = {"day": o["day"], "date": o["date"], "cash": o["cash"], "credit_used": o["credit_used"],
           "credit_limit": o["credit_limit"], "marketplace_receivable": o["marketplace_receivable"],
           "ownership": o["ownership"], "products": _book_products(o),
           "news": [n for n in s.news if n["day"] == s.day + 1 or n["day"] == s.day],
           "notes": [n for n in s.notes if n["day"] == s.day]}
    if s.abroad:
        out["countries"] = {}
        for cc, m in s.abroad.items():
            c = o["countries"][cc]
            out["countries"][cc] = {"opens": c["opens"], "selling": c["selling"], "left": c["left"], "fx": c["fx"],
                                    "marketplace_receivable": c["marketplace_receivable"],
                                    "products": _book_products(c),
                                    "news": [n for n in m.news if n["day"] == s.day + 1 or n["day"] == s.day]}
        out["transfers"] = o["transfers"]
    return out


def book_revenue(row):
    """A day's revenue in the books, over the US and every country."""
    parts = [row["products"]] + [c["products"] for c in (row.get("countries") or {}).values()]
    return sum(sum(p["yesterday"].get(ch, {}).get("revenue", 0) for ch in ("site", "marketplace", "subscription"))
               for prods in parts for p in prods.values())


def scratch_market(s, cc):
    """A country's market as it is today, for the market report of a country the company has not entered. It is
    not part of the company: nothing in it runs."""
    m = simmod.Sim({"products": [], "credit_line": 0}, countrymod.country_truth(s.truth, cc),
                   countrymod.country_world(s.w, cc), None, days=s.days, country=cc, parent=s)
    m.day = s.day
    return m


def market_month(s, month, rows):
    """Category sales this month, for the market_report research: for each catalog product, the units that every
    seller together sold (an estimate from searches, conversion, season, trend and the economy), and the company's
    own units of its lines (from its books for the month)."""
    out = {"month": month, "day": s.day, "products": _market_products(s, rows, None)}
    out["countries"] = {cc: _market_products(s.abroad.get(cc) or scratch_market(s, cc), rows, cc)
                        for cc in countrymod.COUNTRIES}
    return out


def _market_products(m, rows, cc):
    cal = simmod._date(m.day).month
    macro = m.macro()[0]
    out = {}
    for pid, p in PRODUCTS.items():
        t = m.truth["products"][pid]
        total = t["m"] * t["search_rate"] * STEP_DAYS * t["conv_mp"] * m.season(p["category"], cal) * \
            m.trend(pid) * macro
        books = [r["products"] if cc is None else ((r.get("countries") or {}).get(cc) or {}).get("products", {})
                 for r in rows]
        ours = sum(b[pid]["yesterday"].get(ch, {}).get("units", 0)
                   for b in books if pid in b for ch in ("site", "marketplace")
                   if isinstance(b[pid]["yesterday"].get(ch), dict))
        out[pid] = {"category": p["category"], "all_sellers_units": round(max(total, ours)), "our_units": int(ours)}
    return out


def month(payload):
    """Run the live company's next month with the software at payload's ref (or, for the grader, a plain folder)."""
    state, secret, repo, ref = payload["state"], payload["secret"], payload["repo"], payload.get("ref", "main")
    with locked(state):
        cfg = settings(state)
        log = _read(state, "log.json", [])
        s = load(state)
        if s is not None and s.ended:
            return status(state)
        first = s is None
        # The company, its log and its software's state folder change together, when company.pkl is saved: a month
        # that failed part way (earlier runs: live_month timed out after the state folder was kept) left
        # no trace, so the month runs again from the same start, as the replay runs it.
        log = [] if first else [e for e in log if e["last_day"] < s.next_day]
        k = len(log)
        soft = os.path.join(folder(state), "software-state")
        kept_state = {} if first else getattr(s, "software_state", None)
        if kept_state is not None:
            restore_folder(soft, kept_state)
        root, why = None, None
        try:
            root = officemod.checkout(repo, ref)
        except ValueError as e:
            why = str(e)
        commit = resolve(repo, ref) if root else None
        if first:
            plan = {"products": [], "credit_line": 0}
            if root:
                try:
                    plan = officemod.read_plan(root)
                except (planmod.PlanError, OSError, ValueError) as e:
                    why = why or f"company/plan.yaml: {e}"
            s = simmod.Sim(plan, truthmod.run_truth(secret), live_world(secret, cfg), None, record="month")
            s.plan_note = why
        s.frozen_until = "until the next month, when the newest main takes over"
        snaps = os.path.join(folder(state), "snapshots")
        os.makedirs(snaps, exist_ok=True)
        if root:
            snapshot(root, os.path.join(snaps, f"m{k:02d}.tar"))
        user = None
        start_notes = len(s.notes)
        close_shared_tmp()      # as a test market does: team software gets its own temporary folder only
        try:
            if root and os.path.isfile(os.path.join(root, "ops", "autopilot.py")):
                user = make_user("stp-live")
                try:
                    pol = ExternalPolicy(os.path.join(root, "ops"), owner=user[:2] if user else None,
                                         world_seconds=MONTH_SECONDS,
                                         state=soft)
                except SoftwareError as e:
                    pol = Idle(str(e))
            else:
                pol = Idle(why or "ops/autopilot.py is missing")
            s.policy = pol
            info = {"live": {"month": k, "day": s.next_day, "commit": commit, "resumed": not first}}
            if first:
                s.start(info)
            else:
                s.start_software(info)
            first_day = s.next_day
            books = _read(state, "books.json", [])[:first_day]      # one row a day
            for _ in range(STEP_DAYS):
                if not s.advance(1):
                    if s.daily:
                        books.append(compact(s))
                    break
                books.append(compact(s))
            stop = getattr(pol, "stop", None)
            if stop:
                stop()
            kept = pol.save_state()
            s.stop_software()
            if kept:
                s.note(kept)
            s.software_state = keep_folder(soft)
        finally:
            remove_user(user)
            if root:
                shutil.rmtree(root, ignore_errors=True)
        _write(state, "books.json", books)
        mk = [m for m in _read(state, "market.json", []) if m["month"] < k]
        mk.append(market_month(s, k, [r for r in books if r["day"] >= first_day]))
        _write(state, "market.json", mk)
        v = s.value() if s.bankrupt_day is None else {"value": 0.0}
        log.append({"month": k, "ref": ref, "commit": commit, "first_day": first_day, "last_day": s.day,
                    "software_frozen": s.software_frozen, "notes": [n["note"] for n in s.notes[start_notes:]][:20],
                    "value": round(v["value"], 2), "bankrupt": s.bankrupt_day is not None})
        _write(state, "log.json", log)
        if len(log) >= int(cfg.get("months", 37)):
            s.ended = True          # the run's last month (a shorter live run, LIVE_MONTHS, ends early)
        s.policy = None
        save(state, s)
        return status(state)


def status(state):
    """The company now, from root's side: the log, the value, whether the world is over."""
    s = load(state)
    log = _read(state, "log.json", [])
    if s is None:
        return {"started": False, "months": 0, "log": []}
    v = s.value() if s.bankrupt_day is None else {"value": 0.0}
    return {"started": True, "months": len(log), "day": s.day, "next_day": s.next_day, "ended": s.ended,
            "bankrupt": s.bankrupt_day is not None, "value": round(v["value"], 2), "log": log}


# -- the team's view: its own books ----------------------------------------------------------------------------------

def books(payload):
    """The company's books for days [since, until] (at most BOOKS_DAYS), and a monthly summary."""
    state = payload["state"]
    rows = _read(state, "books.json", [])
    log = _read(state, "log.json", [])
    if not rows:
        return {"started": False, "note": "the company opens when the launch phase ends"}
    last = rows[-1]["day"]
    until = min(last, int(payload["until"])) if payload.get("until") is not None else last
    since = int(payload.get("since", max(0, until - 29)) if payload.get("since") is not None else max(0, until - 29))
    since = max(since, until - BOOKS_DAYS + 1)
    days = [r for r in rows if since <= r["day"] <= until]
    months = []
    for e in log:
        chunk = [r for r in rows if e["first_day"] <= r["day"] <= e["last_day"]]
        rev = sum(book_revenue(r) for r in chunk)
        months.append({"month": e["month"], "days": [e["first_day"], e["last_day"]], "commit": e["commit"],
                       "software_stopped": e["software_frozen"], "software_notes": e["notes"][:5],
                       "revenue": round(rev, 2), "cash_at_end": chunk[-1]["cash"] if chunk else None})
    return {"started": True, "today": last, "days": days, "months": months}


# -- the grader ------------------------------------------------------------------------------------------------------

def finish(payload):
    """Run the days left with the software at payload's repo (the final main), month by month."""
    out = status(payload["state"])
    while out.get("started") is not False and not out.get("ended"):
        out = month(payload)
    return out


def replay(payload):
    """The live company again, from day 0, in a fresh copy of the world: the same plan, and each month the snapshot
    that month ran, with a fresh software state folder. Returns the value and whether it matches."""
    state, secret = payload["state"], payload["secret"]
    log = _read(state, "log.json", [])
    cfg = settings(state)
    snaps = os.path.join(folder(state), "snapshots")
    work = officemod.tempfile.mkdtemp(prefix="live-replay-state-")
    soft = os.path.join(work, "software-state")
    s = None
    try:
        for e in log:
            path = os.path.join(snaps, f"m{e['month']:02d}.tar")
            root = unpack(path) if os.path.exists(path) else None
            try:
                if s is None:
                    plan = {"products": [], "credit_line": 0}
                    if root:
                        try:
                            plan = officemod.read_plan(root)
                        except (planmod.PlanError, OSError, ValueError):
                            pass
                    s = simmod.Sim(plan, truthmod.run_truth(secret), live_world(secret, cfg), None, record="month")
                s.frozen_until = "until the next month, when the newest main takes over"
                user = None
                try:
                    if root and os.path.isfile(os.path.join(root, "ops", "autopilot.py")):
                        user = make_user("stp-replay")
                        try:
                            pol = ExternalPolicy(os.path.join(root, "ops"), owner=user[:2] if user else None,
                                                 world_seconds=MONTH_SECONDS, state=soft)
                        except SoftwareError as err:
                            pol = Idle(str(err))
                    else:
                        pol = Idle("ops/autopilot.py is missing")
                    s.policy = pol
                    info = {"live": {"month": e["month"], "day": s.next_day, "commit": e["commit"],
                                     "resumed": e["month"] > 0}}
                    if e["month"] == 0:
                        s.start(info)
                    else:
                        s.start_software(info)
                    s.advance(e["last_day"] - e["first_day"] + 1)
                    stop = getattr(pol, "stop", None)
                    if stop:
                        stop()
                    pol.save_state()
                    s.stop_software()
                finally:
                    remove_user(user)
            finally:
                if root:
                    shutil.rmtree(root, ignore_errors=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if s is None:
        return {"value": None, "matches": None}
    v = s.value() if s.bankrupt_day is None else {"value": 0.0}
    live = status(state)
    value = round(float(v["value"]), 2)        # plain floats and bools: details.json is plain JSON
    return {"value": value, "live_value": live.get("value"),
            "matches": bool(live.get("value") is not None and abs(value - live["value"]) < 0.01),
            "days": s.day + 1}


def result(state):
    """The live company's full result (sim.result()), for the grader's details."""
    s = load(state)
    return s.result() if s is not None else None
