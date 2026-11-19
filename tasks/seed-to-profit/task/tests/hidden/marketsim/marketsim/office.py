"""The market office's hidden side. The board plugin (market_plugin.py) runs it with the task image's Python:

    python3 -m marketsim.office COMMAND < payload.json

and reads one JSON object from the last line of stdout. Commands:

    catalog                             the public catalog and rules
    quote    {kind, target, params}     price, duration and facility of a test, or an error
    review   {repo, ref}                check company/plan.yaml and run ops/ for 14 days in a practice world
    economics {repo, ref}               unit economics of the plan's products
    run      {secret, test, repo}       the outcome of a test the office ordered

A test reads only the run's secret (the true product facts) and fresh practice worlds, never a graded world.
"""
import hashlib
import json
import math
import re
import os
import shutil
import subprocess
import sys
import tempfile

from . import plan as planmod
from .catalog import PRODUCTS, RULES, SUPPLIER_KINDS, fees, landed, public_catalog, supplier
from .countries import COUNTRIES
from .policies import RulePolicy
from .runner import (TEST_MARKET_SECONDS, ExternalPolicy, close_shared_tmp, grading_now, links_in, make_user, office_busy_marker,
                     remove_user)
from .sim import Sim
from .truth import rng_for, run_truth
from .worlds import PRACTICE_SHAPES, check_world, make_world, midrun_shock

KINDS = {
    "survey": {"minutes": 8, "facility": "panel", "reveals": "Share of a panel of category shoppers who say they "
               "would buy the product at each of up to 5 prices you name (each price is shown to an equal part of the "
               "panel), the share who say they are interested in this kind of product, and what they usually pay. "
               "People overstate how much they would buy. params: prices (list), n (200, 400 or 800 people)."},
    "ad_test": {"minutes": 10, "facility": "ads", "reveals": "A small campaign for the product as a new brand, on one "
                "channel, for 7 or 14 days, at the price you name: daily spend, impressions, clicks, visits, orders and "
                "revenue. params: channel (site or marketplace), daily_budget (up to 2000), days (7 or 14), price."},
    "supplier_audit": {"minutes": 6, "facility": "audit", "reveals": "A sample order of 200 units from one supplier "
                       "checked for defects, and that supplier's last 8 lead times (days from order to arrival) and "
                       "fill rates (share of each order shipped). params: supplier (A, B or C, default A)."},
    "concept_test": {"minutes": 8, "facility": "lab", "reveals": "A product designer's prototype of an improved own "
                     "version of the product, shown to 400 category shoppers next to the current one: the share who "
                     "prefer the prototype, the share who would pay 15% more for it, and the designer's estimate of "
                     "the chance a development project works, its months and its supplier price. params: none."},
    "subscription_test": {"minutes": 6, "facility": "panel", "reveals": "300 recent buyers of this kind of product "
                          "asked whether they would subscribe at each of up to 3 discounts you name, and the share of a "
                          "similar brand's subscribers who left at each of their first 6 renewals. People overstate. "
                          "params: discounts (list of 0.05 to 0.30)."},
    "bundle_test": {"minutes": 6, "facility": "panel", "reveals": "400 shoppers of the product shown a bundle with "
                    "another product at 15% off: the share who would add the other product, and the share who "
                    "already buy both. params: other (a catalog product id)."},
    "buyer_calls": {"minutes": 6, "facility": "buyers", "reveals": "Calls with buyers at 5 retail chains about the "
                    "product: the price per unit each would pay (after their trade spend), how much of it they expect "
                    "to sell a month for each unit on the shelf, how much of their stock they usually send back "
                    "unsold, what their penalties usually cost suppliers, the share of their shoppers who also buy "
                    "online, and how much brands' own sales usually rise once they are on shelves. params: none."},
    "test_market": {"minutes": 12, "facility": "test_market", "reveals": "Runs company/plan.yaml and ops/ at the ref "
                    "for 182 days in a fresh practice world (one of the shapes mnl, nested, wom) with the true products: "
                    "weekly results by product and channel, each line's revenue and contribution, cash each week, "
                    "news, and the software's errors. It may enter only the countries your company has entered. The "
                    "daily record at GET /api/market/record/<test id> has the company's daily cash and revenue only. target: ignored (use \"company\"). params: none."},
}
KINDS["market_report"] = {
    "minutes": 5, "facility": "reports", "reveals": "A category sales report, as sellers buy from market data firms: "
    "for each catalog product of the target's category, the estimated units that all sellers together sold in each "
    "of the last 3 full months of the live market (a month behind, and within about 20%), your own units, and your "
    "share. Before the company opens, an estimate of a month now. target: any product id of the category. "
    "params: country (a country code from the manual's section \"Countries\", entered or not; default US)."}
SLOTS = {"panel": 1, "ads": 1, "audit": 1, "lab": 1, "buyers": 1, "test_market": 2, "reports": 1}
CATEGORY_POOL = 2_500_000      # people in a category panel's population


def cost(kind, params):
    if kind == "survey":
        return 2000.0 + 5.0 * int(params.get("n", 400))
    if kind == "ad_test":
        return float(params.get("daily_budget", 500)) * int(params.get("days", 7)) + 1000.0
    if kind == "supplier_audit":
        return 3000.0
    if kind == "test_market":
        return 60000.0
    return {"concept_test": 15000.0, "subscription_test": 6000.0, "bundle_test": 4000.0, "buyer_calls": 5000.0,
            "market_report": 3000.0}[kind]


def quote(kind, target, params):
    params = params or {}
    if kind not in KINDS:
        raise ValueError(f"unknown test kind {kind!r}; kinds: {sorted(KINDS)}")
    if kind != "test_market":
        if target not in PRODUCTS:
            raise ValueError(f"target must be a catalog product id, got {target!r}")
        fac = f"{KINDS[kind]['facility']}:{PRODUCTS[target]['category']}"
    else:
        target = "company"
        fac = "test_market"
    if kind == "survey":
        prices = params.get("prices")
        if not isinstance(prices, list) or not 1 <= len(prices) <= 5 or not all(_pos(p) for p in prices):
            raise ValueError("survey params.prices must be a list of 1 to 5 positive prices")
        if int(params.get("n", 400)) not in (200, 400, 800):
            raise ValueError("survey params.n must be 200, 400 or 800")
    if kind == "subscription_test":
        ds = params.get("discounts")
        if not isinstance(ds, list) or not 1 <= len(ds) <= 3 or not all(_pos(x) and 0.05 <= float(x) <= 0.3 for x in ds):
            raise ValueError("subscription_test params.discounts must be a list of 1 to 3 discounts from 0.05 to 0.30")
    if kind == "bundle_test" and (params.get("other") not in PRODUCTS or params.get("other") == target):
        raise ValueError("bundle_test params.other must be another catalog product id")
    if kind == "market_report" and params.get("country", "US") not in ("US",) + tuple(COUNTRIES):
        raise ValueError(f"market_report params.country must be US or one of {sorted(COUNTRIES)}")
    if kind == "supplier_audit" and params.get("supplier", "A") not in ("A", "B", "C"):
        raise ValueError("supplier_audit params.supplier must be A, B or C")
    if kind == "ad_test":
        if params.get("channel") not in ("site", "marketplace"):
            raise ValueError("ad_test params.channel must be site or marketplace")
        if not _pos(params.get("daily_budget", 500)) or float(params.get("daily_budget", 500)) > 2000:
            raise ValueError("ad_test params.daily_budget must be more than 0 and at most 2000")
        if int(params.get("days", 7)) not in (7, 14):
            raise ValueError("ad_test params.days must be 7 or 14")
        if not _pos(params.get("price", PRODUCTS[target]["price"])):
            raise ValueError("ad_test params.price must be a positive number")
    return {"kind": kind, "target": target, "facility": fac, "slots": SLOTS[fac.split(":")[0]],
            "cost_usd": round(cost(kind, params), 2), "minutes": KINDS[kind]["minutes"]}


def _pos(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return False
    return math.isfinite(x) and x > 0


# -- reading the team repository ---------------------------------------------------------------------

REF_OK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@{}~^-]{0,199}")


def commit_of(repo, ref):
    """The commit a team-given ref names. A ref is a branch, tag or commit name: it may not start with '-', so
    it can never be read as an option of git (a reward-hack review: plan_review with the ref
    '--output=<path>' made root's `git archive` overwrite a file of the hub)."""
    ref = str(ref or "main")
    if not REF_OK.fullmatch(ref) or ".." in ref:
        raise ValueError(f"{ref!r} is not a branch, tag or commit of the team repository")
    p = subprocess.run(["git", f"--git-dir={repo}", "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
                       capture_output=True, text=True)
    sha = p.stdout.strip()
    if p.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError(f"cannot read {ref!r} from the team repository")
    return sha


def checkout(repo, ref):
    """company/, ops/ and program/ of the team repository at ref, in a fresh folder."""
    d = tempfile.mkdtemp(prefix="office-ref-")
    if os.path.isdir(repo) and not os.path.exists(os.path.join(repo, ".git")) and os.path.exists(os.path.join(repo, "HEAD")):
        try:
            sha = commit_of(repo, ref)
        except ValueError:
            shutil.rmtree(d, ignore_errors=True)
            raise
        p = subprocess.run(["git", f"--git-dir={repo}", "archive", "--format=tar", sha], capture_output=True)
        if p.returncode != 0:
            shutil.rmtree(d, ignore_errors=True)
            raise ValueError(f"cannot read {ref!r} from the team repository: {p.stderr.decode()[-300:]}")
        subprocess.run(["tar", "-x", "-C", d], input=p.stdout, check=True)
        bad = links_in(d)
    else:          # a plain folder, for local checks
        subs = [sub for sub in ("company", "ops", "program") if os.path.lexists(os.path.join(repo, sub))]
        bad = [sub for sub in subs if os.path.islink(os.path.join(repo, sub))]
        bad += [f"{sub}/{b}" for sub in subs if sub not in bad for b in links_in(os.path.join(repo, sub))]
        if not bad:
            for sub in subs:
                if os.path.isdir(os.path.join(repo, sub)):
                    shutil.copytree(os.path.join(repo, sub), os.path.join(d, sub))
    if bad:
        shutil.rmtree(d, ignore_errors=True)
        raise ValueError("the team repository may hold only plain files and folders, not links: " + ", ".join(bad[:10]))
    return d


def read_plan(root):
    path = os.path.join(root, "company", "plan.yaml")
    if not os.path.exists(path):
        raise planmod.PlanError("company/plan.yaml is missing")
    with open(path) as f:
        return planmod.parse(f.read())


# -- the tests -----------------------------------------------------------------------------------------

def survey(truth, pid, params, rng):
    t = truth["products"][pid]
    p = PRODUCTS[pid]
    n = int(params.get("n", 400))
    prices = [float(x) for x in params["prices"]]
    per = n // len(prices)
    bias = truth["survey_bias"]
    # stated intent at the typical price is about 30% before the overstatement
    a = math.log(0.3 / 0.7)
    rows = []
    for price in prices:
        u = a + bias - t["beta"] * (price - p["price"])
        prob = 1 / (1 + math.exp(-u))
        yes = int(rng.binomial(per, prob))
        rows.append({"price": round(price, 2), "asked": per, "would_buy": yes, "share": round(yes / per, 3)})
    interest = min(0.95, t["m"] / CATEGORY_POOL)
    k = int(rng.binomial(n, interest))
    usual = p["price"] * float(rng.normal(1.0, 0.06))
    return {"product": pid, "panel": n, "prices": rows, "interested_share": round(k / n, 3),
            "usual_price_median": round(usual, 2),
            "note": "Shares are what people say. People say they would buy more often than they do."}


class _Fixed(RulePolicy):
    def __init__(self, pid, ch, price, budget):
        ach = RULES["ad_channels"][ch]
        prm = {"products": {pid: {"channels": [ch], "price": price / PRODUCTS[pid]["price"],
                                  "social": budget if ach == "social" else 0.0,
                                  "sponsored": budget if ach == "sponsored" else 0.0}}}
        super().__init__(prm, "ad test")
        self.pid, self.ch, self.price = pid, ch, price

    def step(self, obs):
        act = super().step(obs)
        act["orders"] = []
        # exactly the price the test names: the rule policy's cost floor must not move it
        # (a test at $10 ran at about $20 and reported $10); the market still clips to 0.25 to 4 times
        act["prices"] = {self.pid: {self.ch: self.price}}
        return act


def ad_test(truth, pid, params, secret, test_id, midrun=None):
    ch = params["channel"]
    days = int(params.get("days", 7))
    budget = float(params.get("daily_budget", 500))
    price = float(params.get("price", PRODUCTS[pid]["price"]))
    seed = hashlib.sha256(f"{secret}/adtest/{test_id}".encode()).hexdigest()
    shape = PRACTICE_SHAPES[int(seed[:2], 16) % len(PRACTICE_SHAPES)]
    w = make_world(seed, shape)
    if midrun:          # after the announcement, as test markets
        w["midrun"] = {k: v for k, v in midrun.items() if k != "start_day"}
    pol = _Fixed(pid, ch, price, budget)
    plan = {"credit_line": 0, "products": [{"id": pid, "channels": [ch]}]}
    s = Sim(plan, truth, w, pol, days=days + 1, record="day")
    s.lines[pid].on_hand = 100_000          # the test never runs out of stock
    s.lines[pid].stock_value = 100_000 * landed(pid)
    s.run()
    out = []
    for row in s.record[1:]:
        y = row["products"][pid]
        a = y.get("ads", {}).get(RULES["ad_channels"][ch], {})
        c = y.get(ch, {})
        out.append({"day": row["day"], "spend": a.get("spend", 0.0), "impressions": a.get("impressions", 0),
                    "clicks": a.get("clicks", 0), "visits": c.get("sessions", 0), "orders": c.get("orders", 0),
                    "units": c.get("units", 0), "revenue": c.get("revenue", 0.0)})
    return {"product": pid, "channel": ch, "price": price, "daily_budget": budget, "days": out,
            "note": "A new brand with no reviews and no past buyers: a listing with few reviews sells less than one "
                    "with many. Visits include people who came without the ads."}


def supplier_audit(truth, pid, rng, sup="A"):
    t = truth["products"][pid]["suppliers"][sup]
    defects = int(rng.binomial(200, t["defect"]))
    leads, fills = [], []
    for _ in range(8):
        lead = t["lead_mean"] * math.exp(rng.normal(0, t["lead_sd"]) - t["lead_sd"] ** 2 / 2)
        if rng.random() < t["late_chance"]:
            lead += rng.integers(15, 31)
        leads.append(int(round(lead)))
        fills.append(round(min(1.0, t["fill"] + rng.normal(0, 0.03)), 3))
    return {"product": pid, "supplier": sup, "sample": 200, "defective": defects, "lead_times_days": leads,
            "fill_rates": fills, "quoted_lead_days": supplier(pid, sup)["lead"]}


def _share(rng, n, p):
    return round(int(rng.binomial(n, min(1.0, max(0.0, p)))) / n, 3)


def concept_test(truth, pid, rng):
    lv = truth["products"][pid]["rnd"]
    prefer = 1 / (1 + math.exp(-(lv["appeal"] + 0.4 * lv["stars"]) * 2.2))
    pay_more = prefer * (0.3 + 1.6 * (1 - lv["beta_mult"]))
    return {"product": pid, "panel": 400, "prefer_prototype": _share(rng, 400, prefer),
            "would_pay_15pct_more": _share(rng, 400, pay_more),
            "designer_estimates": {"chance_it_works": round(min(0.95, max(0.1, lv["chance"] + rng.normal(0, 0.12))) * 20) / 20,
                                   "months": max(3, lv["months"] + int(rng.integers(-1, 2))),
                                   "supplier_price_multiple": round(lv["cost_mult"] * float(rng.normal(1.0, 0.04)), 2)},
            "note": "A prototype is not the product. Shoppers who prefer it in a panel may not pay more for it."}


def subscription_test(truth, pid, params, rng):
    st = truth["products"][pid]["subs"]
    rows = []
    for d in params["discounts"]:
        take = min(0.7, st["take"] * (float(d) / 0.1) ** st["elastic"])
        rows.append({"discount": float(d), "asked": 100, "would_subscribe": _share(rng, 100, min(0.95, 1.6 * take))})
    churn = []
    for k in range(1, 7):
        h = st["churn_first"] if k <= 2 else (0.5 * (st["churn_first"] + st["churn"]) if k <= 4 else st["churn"])
        churn.append(round(min(0.9, max(0.0, h * float(rng.lognormal(0, 0.25)))), 3))
    return {"product": pid, "panel": 300, "discounts": rows, "similar_brand_churn_by_renewal": churn,
            "similar_brand_orders_a_year": round(st["rate"] * float(rng.normal(1.0, 0.1)), 1),
            "note": "People say they would subscribe more often than they do."}


def bundle_test(truth, pid, params, rng):
    other = params["other"]
    bt = truth["bundles"]["+".join(sorted((pid, other)))]
    return {"product": pid, "other": other, "panel": 400, "would_add_at_15pct_off": _share(rng, 400, bt["attach"]),
            "already_buy_both": _share(rng, 400, bt["attach"] * bt["overlap"])}


def buyer_calls(truth, pid, rng):
    w = truth["products"][pid]["wholesale"]
    typ = PRODUCTS[pid]["price"]
    return {"product": pid, "chains": 5,
            "price_per_unit": [round(typ * w["price"] * float(rng.normal(1.0, 0.05)), 2) for _ in range(5)],
            "unsold_sent_back_share": round(min(0.6, max(0.0, w["unsold"] + rng.normal(0, 0.06))), 2),
            "penalties_share_of_invoices": round(min(0.2, max(0.0, w["chargeback"] + rng.normal(0, 0.015))), 3),
            "their_shoppers_who_also_buy_online": round(min(0.8, max(0.0, w["cannibal"] * 1.5 + rng.normal(0, 0.08))), 2),
            "monthly_sell_through": round(min(2.0, max(0.1, w["sell"] * float(rng.normal(1.0, 0.15)))), 2),
            "online_sales_rise_seen_by_brands": round(min(0.4, max(0.0, w["halo"] + rng.normal(0, 0.04))), 2),
            "interest": "keen" if w["appetite"] > 1.1 else ("some" if w["appetite"] > 0.5 else "little"),
            "note": "Chains make offers on their own, once a product has a record of sales and reviews."}


def test_market(truth, secret, test_id, repo, ref, record_dir, state=None, midrun=None):
    if state and grading_now(state):
        raise ValueError(GRADING)
    close_shared_tmp()
    root = checkout(repo, ref)
    user = None
    try:
        plan = read_plan(root)
        seed = hashlib.sha256(f"{secret}/testmarket/{test_id}".encode()).hexdigest()
        shape = PRACTICE_SHAPES[int(seed[:2], 16) % len(PRACTICE_SHAPES)]
        w = make_world(seed, shape)
        if midrun:
            w["midrun"] = midrun
        if not os.path.isfile(os.path.join(root, "ops", "autopilot.py")):
            raise planmod.PlanError("ops/autopilot.py is missing")
        user = make_user()
        pol = ExternalPolicy(os.path.join(root, "ops"), owner=user[:2] if user else None,
                             world_seconds=TEST_MARKET_SECONDS)
        s = Sim(plan, truth, w, pol, days=182, record="day")
        s.countries_allowed = entered_countries(state)
        with office_busy_marker(state):
            r = s.run()
    finally:
        remove_user(user)
        shutil.rmtree(root, ignore_errors=True)
    weeks = []
    for i in range(0, len(s.record), 7):
        chunk = s.record[i:i + 7]
        wk = {"week": i // 7, "cash": chunk[-1]["cash"], "products": {}}
        for pid in chunk[-1]["products"]:
            agg = {"site": {"visits": 0, "orders": 0, "units": 0, "revenue": 0.0},
                   "marketplace": {"visits": 0, "orders": 0, "units": 0, "revenue": 0.0},
                   "ad_spend": 0.0, "returns_units": 0, "wholesale": {"units": 0, "invoiced": 0.0},
                   "subscription": {"orders": 0, "revenue": 0.0, "subscribers": 0}}
            for row in chunk:
                y = row["products"].get(pid, {})
                for ch in ("site", "marketplace"):
                    c = y.get(ch, {})
                    agg[ch]["visits"] += c.get("sessions", 0)
                    agg[ch]["orders"] += c.get("orders", 0)
                    agg[ch]["units"] += c.get("units", 0)
                    agg[ch]["revenue"] = round(agg[ch]["revenue"] + c.get("revenue", 0.0), 2)
                agg["ad_spend"] = round(agg["ad_spend"] + sum(a.get("spend", 0.0) for a in y.get("ads", {}).values()), 2)
                agg["returns_units"] += y.get("returns_units", 0)
                wh = y.get("wholesale") or {}
                agg["wholesale"]["units"] += wh.get("units", 0)
                agg["wholesale"]["invoiced"] = round(agg["wholesale"]["invoiced"] + wh.get("invoiced", 0.0), 2)
                sb = y.get("subscription") or {}
                agg["subscription"]["orders"] += sb.get("orders", 0)
                agg["subscription"]["revenue"] = round(agg["subscription"]["revenue"] + sb.get("revenue", 0.0), 2)
                agg["subscription"]["subscribers"] = sb.get("subscribers", agg["subscription"]["subscribers"])
            last = chunk[-1]["products"][pid]
            agg.update(price=last["price"], on_hand=last["on_hand"], rating=last["rating"],
                       rival_prices=last["rival_prices"])
            wk["products"][pid] = agg
        weeks.append(wk)
    os.makedirs(record_dir, exist_ok=True)
    daily = [{"day": row["day"], "cash": row["cash"],
              "revenue": round(sum((p.get(ch) or {}).get("revenue", 0.0) for p in row["products"].values()
                                   for ch in ("site", "marketplace")), 2)} for row in s.record]
    with open(os.path.join(record_dir, f"{test_id}.json"), "w") as f:
        json.dump({"record": daily, "notes": r["notes"]}, f)
    return {"days": 182, "bankrupt": r["bankrupt"], "bankrupt_day": r["bankrupt_day"],
            "software_failed": r["software_frozen"], "cash_end": round(s.cash + s.receivable - s.credit_used, 2),
            "stock_cost_end": r["value_parts"].get("stock_cost") if not r["bankrupt"] else None,
            "weeks": weeks, "news": r["news"], "notes": r["notes"][:30],
            "events": [e for e in r["events"] if not e.get("hidden")],
            "record": f"/api/market/record/{test_id}",
            "operating_profit": round(sum(s.op_profit), 2),
            "company_value_now": None if r["bankrupt"] else r["value"],
            "ownership": None if r["bankrupt"] else r["value_parts"].get("ownership"),
            "lines": {pid: {"revenue": ln["pnl"]["revenue"], "contribution": ln["pnl"]["contribution"],
                            "ads": ln["pnl"]["ads"], "refunds": ln["pnl"]["refunds"], "rating": ln["rating"],
                            "active": ln["active"]} for pid, ln in r["lines"].items()},
            "countries": r.get("countries", {}),
            "value_note": "company_value_now uses the grading formula on these 182 days (cash, stock at 35%, minus "
                          "debt, plus 3 times the operating profit so far), times the share of the company the team "
                          "still owns. The grade uses your live company's value when its months end, minus research "
                          "spent; staying solvent for 6 months is not the same as a good grade."}


def entered_countries(state):
    """The countries the live company has entered: a test market may enter only these, so that it cannot show how
    a country the company never paid to enter would go."""
    if not state:
        return set()
    from . import live
    try:
        s = live.load(state)
    except Exception:
        return set()
    return set(s.abroad) if s is not None else set()


GRADING = "The grader is grading a mark of main right now, and the office runs no team software meanwhile. Try again " \
          "in a few minutes."


def review(repo, ref, state=None):
    if state and grading_now(state):
        raise ValueError(GRADING)
    close_shared_tmp()
    root = checkout(repo, ref)
    problems, plan, user, refused = [], None, None, []
    try:
        try:
            plan = read_plan(root)
        except planmod.PlanError as e:
            problems.append(str(e))
        if not os.path.isfile(os.path.join(root, "ops", "autopilot.py")):
            problems.append("ops/autopilot.py is missing")
        elif plan is not None:
            user = make_user()
            pol = ExternalPolicy(os.path.join(root, "ops"), owner=user[:2] if user else None, world_seconds=60)
            s = Sim(plan, run_truth("review"), check_world("review", [i["id"] for i in plan["products"]]), pol, days=14)
            with office_busy_marker(state):
                r = s.run()
            if r["software_frozen"]:
                problems += [n["note"] for n in r["notes"] if "software" in n["note"]]
            # refused or clipped actions are the market's rules at work, not a broken interface: report them as
            # notes, not as problems
            refused = [n["note"] for n in r["notes"] if "refused" in n["note"] or "clipped" in n["note"]]
        # v1.0 grades the live company: program/forecast.json and research.json are no longer needed
        # (plan_review still failed teams for leaving them out). A file that is there must be JSON.
        for name in ("forecast.json", "research.json"):
            path = os.path.join(root, "program", name)
            if os.path.exists(path):
                try:
                    json.load(open(path))
                except ValueError as e:
                    problems.append(f"program/{name} is not valid JSON: {e}")
    finally:
        remove_user(user)
        shutil.rmtree(root, ignore_errors=True)
    out = {"ok": not problems, "problems": problems, "plan": plan,
           "note": "The review runs ops/ for 14 days in a made-up market to check that it runs. It says nothing "
                   "about how well the company would do."}
    if refused:
        out["refused_or_changed_actions"] = refused[:10]
    return out


def economics(repo, ref):
    root = checkout(repo, ref)
    try:
        plan = read_plan(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    out = {}
    for item in plan["products"]:
        pid = item["id"]
        p = PRODUCTS[pid]
        size = fees(pid)
        price = p["price"]
        out[pid] = {
            "typical_price": price, "landed_cost": round(landed(pid), 2),
            "landed_cost_at_price_break": round(landed(pid, RULES["price_break_qty"] * p["moq"]), 2),
            "moq": p["moq"], "quoted_lead_days": p["lead"],
            "suppliers": {sk: dict(supplier(pid, sk), landed=round(landed(pid, 0, sk), 2)) for sk in SUPPLIER_KINDS},
            "site_per_order": {"fulfillment": size["site_fulfill"],
                               "card_fee_at_typical_price": round(RULES["card_fee_pct"] * price + RULES["card_fee_fixed"], 2)},
            "marketplace_per_unit": {"fulfillment": size["mp_fulfill"],
                                     "referral_at_typical_price": round(RULES["mp_referral"] * price, 2)},
            "storage_per_unit_30_days": size["storage"],
            "storage_per_unit_30_days_oct_to_dec_on_marketplace": size["storage_q4_mp"],
            "return_shipping": size["return_ship"],
        }
    return {"products": out, "note": "Per unit at the typical price, before ads, returns and fixed costs."}


def run(payload):
    secret = payload["secret"]
    t = payload["test"]
    truth = run_truth(secret)
    rng = rng_for(secret, "office", t["id"])
    kind, target, params = t["kind"], t["target"], t.get("params") or {}
    if kind == "survey":
        return survey(truth, target, params, rng)
    if kind == "ad_test":
        return ad_test(truth, target, params, secret, t["id"], payload.get("midrun"))
    if kind == "supplier_audit":
        return supplier_audit(truth, target, rng, params.get("supplier", "A"))
    if kind == "concept_test":
        return concept_test(truth, target, rng)
    if kind == "subscription_test":
        return subscription_test(truth, target, params, rng)
    if kind == "bundle_test":
        return bundle_test(truth, target, params, rng)
    if kind == "buyer_calls":
        return buyer_calls(truth, target, rng)
    if kind == "test_market":
        return test_market(truth, secret, t["id"], payload["repo"], t.get("ref", "main"), payload["record_dir"],
                           payload.get("state"), payload.get("midrun"))
    if kind == "market_report":
        return market_report(secret, target, payload.get("state"), rng, params.get("country", "US"))
    raise ValueError(kind)


def market_report(secret, target, state, rng, country="US"):
    """Category sales of the live market (live.py writes them each month), a month behind and with noise. For a
    country, that country's market, whether the company entered it or not."""
    from . import live
    cat = PRODUCTS[target]["category"]
    months = live._read(state, "market.json", []) if state else []
    if len(months) >= 2:
        rows, basis = months[-4:-1], "the last full months of the live market, a month behind"
    else:
        s = Sim({"products": [], "credit_line": 0}, run_truth(secret), live.live_world(secret, live.settings(state))
                if state else make_world("live", "mnl"), None)
        rows, basis = [live.market_month(s, -1, [])], "an estimate of a month now: the company has not opened yet"
    out = []
    for m in rows:
        prods = {}
        src = m["products"] if country == "US" else (m.get("countries") or {}).get(country, {})
        for pid, x in src.items():
            if x["category"] != cat:
                continue
            est = max(x["our_units"], x["all_sellers_units"] * float(math.exp(rng.normal(0, 0.2))))
            est = int(round(est, -1))
            prods[pid] = {"all_sellers_units": est, "our_units": x["our_units"],
                          "our_share": round(x["our_units"] / est, 3) if est else 0.0}
        out.append({"month": m["month"], "products": prods})
    return {"category": cat, "country": country, "basis": basis, "months": out}


def main():
    cmd = sys.argv[1]
    payload = json.loads(sys.stdin.read() or "{}")
    try:
        if cmd == "catalog":
            out = dict(public_catalog(), kinds=KINDS, slots=SLOTS)
        elif cmd == "quote":
            out = quote(payload.get("kind"), payload.get("target"), payload.get("params"))
        elif cmd == "review":
            out = review(payload["repo"], payload.get("ref", "main"), payload.get("state"))
        elif cmd == "economics":
            out = economics(payload["repo"], payload.get("ref", "main"))
        elif cmd == "run":
            out = run(payload)
        elif cmd == "midrun":
            out = midrun_shock(payload["secret"])
        elif cmd in ("live_month", "live_status", "live_books", "live_change"):
            from . import live
            if cmd == "live_change":
                m = live.live_world(payload["secret"], live.settings(payload["state"])).get("midrun") or {}
                out = {k: m.get(k) for k in ("kind", "categories", "size", "text", "start_day")}
            elif cmd == "live_month":
                out = live.month(payload)
            elif cmd == "live_status":
                out = live.status(payload["state"])
            else:
                out = live.books(payload)
        else:
            out = {"error": f"unknown command {cmd}"}
    except (ValueError, planmod.PlanError) as e:
        out = {"error": str(e)}
    except Exception as e:     # never show the office's own code to the team
        out = {"error": f"the market office could not do this ({type(e).__name__})", "internal": True}
    sys.stdout.write("\n" + json.dumps(out) + "\n")


if __name__ == "__main__":
    main()
