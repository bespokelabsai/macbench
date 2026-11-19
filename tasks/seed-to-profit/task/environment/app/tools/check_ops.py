"""Check that ops/autopilot.py runs and returns valid actions, on made-up observations.

    python3 tools/check_ops.py [--days 30]

This does not simulate a market. Every day has made-up sales, so it only checks the interface: that init()
and step() run, return in time, and return actions of the right shape. Halfway through it starts the software
again in a fresh process, as each month of the live company does, with the same state folder
(setup["state_dir"]) and setup["live"], so you can check that the software picks up where it left off. The market
office's plan_review tool runs the same check against the real rules for 14 days. Neither says how well the
company would do.
"""
import argparse
import datetime
import importlib
import json
import math
import os
import random
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load():
    import yaml
    catalog = json.load(open(os.path.join(ROOT, "docs", "catalog.json")))
    plan = yaml.safe_load(open(os.path.join(ROOT, "company", "plan.yaml"))) or {}
    return catalog, plan


def setup_for(catalog, plan):
    prods = {p["id"]: p for p in catalog["products"]}
    chosen = {}
    for item in plan.get("products") or []:
        p = dict(prods[item["id"]])
        p["channels"] = sorted(item.get("channels", ["site", "marketplace"]))
        p["supplier"] = item.get("supplier", "A")
        p["landed"] = p["cost"] + p["freight"]
        p["landed_break"] = p["cost"] * (1 - catalog["rules"]["price_break"]) + p["freight"]
        chosen[p["id"]] = p
    return {"days": catalog["rules"]["days"], "start_date": catalog["rules"]["start_date"], "rules": catalog["rules"],
            "sizes": catalog["sizes"], "season": catalog["season"], "shapes": catalog["shapes"],
            "products": chosen, "catalog": {p["id"]: dict(p, landed=p["cost"] + p["freight"]) for p in catalog["products"]},
            "credit_line": float(plan.get("credit_line", 0) or 0), "credit_limit": 0.0,
            "cash": catalog["rules"]["seed_money"], "countries": catalog.get("countries", {})}


# One of each kind of news item, as the market sends them (docs/REFERENCE.md, "The software").
NEWS_DAYS = {
    2: [{"kind": "tariff", "starts": 32, "text": "A tariff of 20% on the supplier price of kitchen goods applies to "
                                                 "orders placed from day 32."},
        {"kind": "holiday_ads", "text": "Ad prices usually rise in November and December. Ad platforms expect the "
                                        "same this year."}],
    3: [{"kind": "supplier_closure", "starts": 33, "text": "The supplier of k1 closes on day 33. ..."},
        {"kind": "port_delay", "starts": 33, "text": "Port congestion from day 33 for 60 days: ..."},
        {"kind": "platform_fee", "starts": 33, "text": "The marketplace raises its referral fee by 2% from day 33."}],
    4: [{"kind": "development", "product": None, "worked": True, "text": "The improved product is ready and sells "
                                                                          "from now on. Its supplier prices are 1.20 times "
                                                                          "the old ones."},
        {"kind": "term_sheet", "text": "Investors offer to buy new shares at a $3,000,000 valuation before the money, "
                                       "up to $1,000,000, open for 30 days."},
        {"kind": "wholesale", "text": "Contract W1 with Brightmart ended; it sent back 40 unsold units."},
        {"kind": "vendor", "offer": "V1", "text": "A made-up pitch."},
        {"kind": "suspension", "product": None, "until": 40, "text": "The marketplace suspended a listing until day 40."},
        {"kind": "market_change", "text": "A made-up lasting change to the market, starting in 30 days."},
        {"kind": "rival_closed", "product": None, "text": "A rival seller closed its listing."},
        {"kind": "rival_entered", "product": None, "text": "A new seller listed a rival product."},
        {"kind": "rival_closed", "product": None, "text": "A rival seller closed its listing."},
        {"kind": "rival_entered", "product": None, "text": "A new seller listed a rival product."}],
}


def fake_obs(day, setup, rnd, stopped=()):
    """Made-up days shaped as the market gives them, for every line (REFERENCE.md). What can be missing is missing
    on some days for each line, so software must check before it reads:

    - a line the software stopped stays stopped, and line i is also paused on days 8 + 3i and 9 + 3i: a line not on
      sale shows {} for each channel and no ad, and no `subscription`;
    - line i's marketplace listing is suspended from day 4 + 5i until day 8 + 5i (`suspended_until`); yesterday's
      marketplace block is {} on days 5 + 5i to 8 + 5i;
    - subscribe and save runs on line i in alternating 3-day windows from day 4; `yesterday.subscription` comes
      whenever the line is on sale on the site with it running (even with 0 subscribers), and a stopped line keeps
      its subscribers;
    - each line has a retail chain contract from day 4, delivering every 5 days (staggered), and
      `yesterday.wholesale` comes only on its delivery day;
    - a bundle of two site lines (a different pair every 5 days) is on the site from day 6, with `bundle_orders` on
      some days, and `addon_units` on some others;
    - line i has no rivals left (`rival_prices` and `rival_ratings` are []) on days where (day + i) % 7 == 6;
    - `funding.equity_offer` is null on days below 4 and where day % 5 == 2, `funding.venture_debt` on days below 5
      and where day % 4 == 1."""
    start = datetime.date.fromisoformat(setup["start_date"])
    pids = list(setup["products"])
    first = pids[0] if pids else None
    site_lines = [pid for pid in pids if "site" in setup["products"][pid]["channels"]]
    bundles = []
    if day >= 6 and len(site_lines) >= 2:
        k = (day // 5) % (len(site_lines) - 1)
        bundles = [{"products": site_lines[k:k + 2], "discount": 0.1}]
    prods = {}
    for i, (pid, p) in enumerate(setup["products"].items()):
        # The team's actions apply before the day's sales, so yesterday's numbers match `active` and the discounts
        # today. A suspension is set after the day's sales, so the marketplace shows {} from the day after it starts
        # through the day `suspended_until` is reached.
        active = pid not in stopped and day not in (8 + 3 * i, 9 + 3 * i)
        suspended = "marketplace" in p["channels"] and 5 + 5 * i <= day <= 8 + 5 * i
        sns = day >= 4 and ((day - 4) // 3 + i) % 2 == 0          # subscribe and save running
        delivery = day >= 4 and (day + i) % 5 == 0
        y = {}
        if day:     # as the market gives it: both channels always, {} where the product is not on sale
            y = {"site": {}, "marketplace": {}, "ads": {}}
            for ch in p["channels"]:
                o = rnd.randint(0, 20)
                if not active or (ch == "marketplace" and suspended):
                    continue
                y[ch] = {"sessions": o * 15, "orders": o, "units": o, "revenue": round(o * p["price"], 2), "in_stock": True}
                y["ads"]["social" if ch == "site" else "sponsored"] = {"spend": 0.0, "impressions": 0, "clicks": 0,
                                                                       "attributed_orders": 0}
            y["returns_units"], y["refunds"] = 0, 0.0
            if sns and active and "site" in p["channels"]:
                y["subscription"] = {"subscribers": (day + i) % 4 * 5, "orders": 1, "unfilled": 0, "revenue": 30.6,
                                     "left": 0.4}
            if delivery:
                y["wholesale"] = {"contract": f"W{i + 1}", "units": 260 if active else 0,
                                  "short": 0 if active else 260, "invoiced": 3900.0 if active else 0.0,
                                  "shelf_sales": 260}
            if y["site"]:
                if day % 3 != 1 and any(pid in b["products"] for b in bundles):
                    y["site"]["bundle_orders"] = 1
                if (day + i) % 3 == 1:
                    y["site"]["addon_units"] = 2
        rivals = [round(p["price"] * rnd.uniform(0.9, 1.1), 2) for _ in range(2)]
        no_rivals = (day + i) % 7 == 6
        # open supplier orders: `arrives` is None until an order ships
        inbound = []
        if day >= 2 and (day + i) % 5 != 4:          # and on some days no order is open at all
            ph = (day + i) % 4
            sup = p.get("supplier", "A")
            po = {"qty": 1000, "expected": day + p["lead"], "unit_cost": round(p["landed"], 2), "supplier": sup}
            if ph in (0, 1, 3):
                inbound.append(dict(po, po=f"PO{i + 1}-{day // 4}a", status="ordered", arrives=None))
            if ph in (2, 3):
                inbound.append(dict(po, po=f"PO{i + 1}-{day // 4}b", status="shipped", arrives=day + 5))
        prods[pid] = {"active": active, "channels": p["channels"], "price": {"site": p["price"], "marketplace": p["price"]},
                      "ad_budget": {"social": 0.0, "sponsored": 0.0}, "on_hand": rnd.randint(0, 3000), "inbound": inbound,
                      "rating": 4.2, "reviews": day, "unit_cost_now": p["landed"], "lead_quote": p["lead"],
                      "supplier": p.get("supplier", "A"),
                      "supplier_quotes": {s: {"unit_cost": q["cost"] + q["freight"], "lead": q["lead"], "moq": q["moq"]}
                                          for s, q in p["suppliers"].items()},
                      # closed rivals drop out, so on some days a line has no rival left
                      "rival_prices": [] if no_rivals else rivals,
                      "rival_ratings": [] if no_rivals else [4.3, 4.1], "yesterday": y,
                      "improved": day >= 4 and pid == first,
                      "development": {"started": 2, "result_day": 150} if day >= 4 and i == len(pids) - 1 and i else None,
                      "subscription_discount": 0.1 if sns and "site" in p["channels"] else 0.0,
                      "subscribers": (day + i) % 4 * 5 if day >= 4 and "site" in p["channels"] else 0,
                      "site_customers": 3 * day if "site" in p["channels"] else 0,
                      "suspended_until": 8 + 5 * i if day >= 4 + 5 * i and "marketplace" in p["channels"] else 0}
    contracts = [{"id": f"W{i + 1}", "chain": "Brightmart", "product": pid, "units_per_month": 300, "price": 15.0,
                  "months": 6, "deliveries": 1, "next_delivery": day + ((-(day + i)) % 5 or 5), "strikes": 0,
                  "shelf_sales": 260} for i, pid in enumerate(pids)] if day >= 4 else []
    news = []
    if day in NEWS_DAYS:
        news = [dict(n, day=day) for n in NEWS_DAYS[day]]
        for n in news:
            if "product" in n:
                n["product"] = first
    offers = [{"id": "W2", "chain": "Norland Home", "product": first, "units_per_month": 500,
               "price": round(0.45 * setup["products"][first]["price"], 2), "months": 6, "pay_days": 60,
               "expires": day + 10}] if first and day >= 3 else []
    return {"day": day, "date": (start + datetime.timedelta(days=day)).isoformat(), "days_left": setup["days"] - day,
            "cash": 1_500_000.0, "credit_line": setup["credit_line"],
            "credit_limit": min(setup["credit_line"], 1500.0 * day), "credit_used": 0.0,
            "marketplace_receivable": 0.0, "marketplace_fee": setup["rules"]["mp_referral"], "products": prods,
            "news": news, "wholesale_offers": offers,
            "wholesale_contracts": contracts,
            "wholesale_receivable": 4320.0 if day >= 4 else 0.0,
            "funding": {"equity_offer": {"pre_money": 3_000_000.0, "max_amount": 1_000_000.0, "fee": 0.04,
                                         "expires": day + 20} if day >= 4 and day % 5 != 2 else None,
                        "venture_debt": {"max_amount": 450_000.0, "apr": 0.13, "warrant_coverage": 0.06,
                                         "valuation": 3_200_000.0, "warrant_share": 0.0084}
                        if day >= 5 and day % 4 != 1 else None},     # either can be null on any day
            "ownership": 1.0, "equity_rounds": [], "venture_debt_used": 0.0, "bundles": bundles, "countries": fake_countries(day, prods, setup),
            "transfers": [],
            "vendor_offers": [{"id": "V1", "vendor": "the marketplace", "kind": "early_reviewers", "product": first,
                               "expires": day + 30, "text": "A made-up pitch: reply with {\"vendors\": {\"V1\": "
                               "{\"units\": <1 to 30>}}}."},
                              {"id": "V2", "vendor": "a vendor", "kind": "review_service", "product": None,
                               "expires": day + 30, "text": "A made-up pitch: reply with {\"vendors\": {\"V2\": "
                               "{\"product\": \"<id>\", \"reviews\": <n>}}}."}] if first and day >= 3 else []}


def fake_countries(day, prods, setup):
    """Every country in the catalog, entered early so the days carry as much as a real run can (one made-up country timed software whose work grows with the countries ~7x too low). GB is entered
    on day 6 and opens on day 12; the others are entered on days 3 to 8 and open 6 days later. A country's lines
    have empty `yesterday` blocks from entry through its opening day, and no supplier order until 2 days later."""
    codes = sorted(setup.get("countries", {}), key=lambda cc: (cc != "GB", cc))
    out = {}
    for k, cc in enumerate(codes):
        entered = 6 if cc == "GB" else 3 + k % 6
        if day < entered:
            continue
        opens = entered + 6
        info = setup["countries"][cc]
        pf = info.get("price_factor", 1.0)
        cp = {}
        for pid, p in prods.items():
            q = json.loads(json.dumps(p))
            q["typical_price"] = round(setup["products"][pid]["price"] * pf, 2)
            q["price"] = {ch: q["typical_price"] for ch in q["price"]}
            # as the market gives it: from entry, empty blocks (no sales yet), through the opening day; numbers from
            # the day after it opens
            if day <= opens:
                q["yesterday"] = {"site": {}, "marketplace": {}, "ads": {}, "returns_units": 0, "refunds": 0.0}
            else:
                q["yesterday"].pop("wholesale", None)        # retail chains are in the US only
            # a country line has its own supplier orders: none until its first one, then on some days only
            q["inbound"] = [] if day <= opens + 2 or day % 3 == 0 else q["inbound"]
            cp[pid] = q
        out[cc] = {"opens": opens, "selling": day >= opens, "left": False, "fx": round(1 + 0.001 * (day - entered), 4),
                   "vat": info.get("vat", 0.0), "marketplace_fee": info.get("referral", 0.15),
                   "marketplace_receivable": 0.0, "products": cp, "news": [], "bundles": [], "vendor_offers": []}
    return out


MARKET_ACTIONS = ("prices", "ads", "orders", "stop", "launch", "subscriptions", "bundles", "vendors")


def check_actions(act, setup):
    problems = []
    if not isinstance(act, dict):
        return ["step() must return a dict"]
    known = {"prices", "ads", "orders", "credit", "credit_line", "stop", "launch", "develop", "accept_offers",
             "subscriptions", "vendors", "bundles", "raise", "enter", "leave", "countries", "transfer"}
    if "credit_line" in act and (not isinstance(act["credit_line"], (int, float)) or act["credit_line"] < 0
                                 or act["credit_line"] > setup["rules"].get("credit_max", 500_000)):
        problems.append(f"credit_line {act['credit_line']!r} must be a number from 0 to {setup['rules']['credit_max']}")
    codes = set(setup.get("countries", {}))
    code = lambda c, extra=(): isinstance(c, str) and (c in codes or c in extra)  # noqa: E731
    for k in ("enter", "leave"):
        v = act.get(k, [])
        if not isinstance(v, list) or not all(code(c) for c in v):
            problems.append(f"{k} must be a list of country codes from docs/catalog.json (countries)")
    transfers = act.get("transfer", [])
    if not isinstance(transfers, list):
        problems.append("transfer must be a list of {'product', 'qty', 'from', 'to'}")
        transfers = []
    for t in transfers:
        if not isinstance(t, dict) or not isinstance(t.get("qty"), (int, float)) or \
                not code(t.get("from", "US"), ("US",)) or not code(t.get("to"), ("US",)):
            problems.append(f"transfer {t!r} must be {{'product', 'qty', 'from', 'to'}} with country codes")
    subs = act.get("countries", {})
    if not isinstance(subs, dict):
        problems.append("countries must be {country code: {actions}}")
        subs = {}
    for cc, sub in subs.items():
        if cc not in codes or not isinstance(sub, dict):
            problems.append(f"countries[{cc!r}] must be a country code with an object of actions")
            continue
        for k in sub:
            if k not in MARKET_ACTIONS:
                problems.append(f"countries[{cc!r}][{k!r}] is not a country's action (those are {list(MARKET_ACTIONS)})")
        problems += [f"countries[{cc!r}]: {p}" for p in check_actions({k: v for k, v in sub.items()
                                                                        if k in MARKET_ACTIONS}, setup)]
    lo, hi = setup["rules"]["discount_range"]
    for pid in act.get("develop") or []:
        if pid not in setup["catalog"]:
            problems.append(f"develop: {pid!r} is not a product id")
    if not isinstance(act.get("accept_offers", []), list):
        problems.append("accept_offers must be a list of offer ids")
    for pid, d in (act.get("subscriptions") or {}).items():
        if not isinstance(d, (int, float)) or not (d == 0 or lo <= d <= hi):
            problems.append(f"subscriptions[{pid}] = {d!r} must be 0 or from {lo} to {hi}")
    for b in act.get("bundles") or []:
        if not isinstance(b, dict) or not isinstance(b.get("products"), list) or len(b["products"]) != 2 \
                or not isinstance(b.get("discount"), (int, float)) or not lo <= b["discount"] <= hi:
            problems.append(f"bundle {b!r} must be {{'products': [two ids], 'discount': {lo} to {hi}}}")
    for k, v in (act.get("raise") or {}).items():
        if k not in ("equity", "venture_debt") or not isinstance(v, (int, float)) or v <= 0:
            problems.append(f"raise {{{k!r}: {v!r}}} must be equity or venture_debt with a positive amount")
    v = act.get("vendors", {})
    if not isinstance(v, dict) or not all(isinstance(x, dict) for x in v.values()):
        problems.append("vendors must be {offer id: {...}}, a dict for each pitch you reply to")
    for k in act:
        if k not in known:
            problems.append(f"unknown action key {k!r} (ignored by the market)")
    for pid, chans in (act.get("prices") or {}).items():
        for ch, v in (chans or {}).items():
            if ch not in ("site", "marketplace") or not isinstance(v, (int, float)) or not math.isfinite(v):
                problems.append(f"prices[{pid}][{ch}] = {v!r} is not a finite number for site or marketplace")
    for pid, chans in (act.get("ads") or {}).items():
        for ch, v in (chans or {}).items():
            if ch not in ("social", "sponsored") or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
                problems.append(f"ads[{pid}][{ch}] = {v!r} must be a number of dollars per day for social or sponsored")
    for o in act.get("orders") or []:
        if isinstance(o, dict) and not code(o.get("to", "US"), ("US",)):
            problems.append(f"order {o!r}: 'to' must be US or a country code")
        pid = o.get("product") if isinstance(o, dict) else None
        cat = setup["catalog"].get(pid)
        sup = o.get("supplier", setup["products"].get(pid, {}).get("supplier", "A")) if isinstance(o, dict) else "A"
        moq = cat["suppliers"].get(sup, {}).get("moq") if cat and isinstance(sup, str) else None
        if cat is None or moq is None or not isinstance(o.get("qty"), (int, float)) or o["qty"] < moq \
                or o["qty"] > setup["rules"].get("max_order_units", 200_000):
            problems.append(f"order {o!r} needs a catalog product, a supplier A, B or C, and a qty from that "
                            f"supplier's moq to {setup['rules'].get('max_order_units', 200_000):,}")
        elif pid not in setup["products"]:
            problems.append(f"order {o!r}: {pid} is not one of the company's lines (launch it first)")
    try:
        json.dumps(act, allow_nan=False)
    except (TypeError, ValueError) as e:
        problems.append(f"actions are not plain JSON: {e}")
    return problems


def run(setup, days, first, json_out):
    """One process of the software: init(setup), then step() for days first .. first + days - 1."""
    sys.path.insert(0, os.path.join(ROOT, "ops"))
    mod = importlib.import_module("autopilot")
    if hasattr(mod, "init"):
        mod.init(setup)
    rnd = random.Random(first)
    problems, slowest, total, stopped = [], 0.0, 0.0, set()
    for d in range(first, first + days):
        obs = fake_obs(d, setup, rnd, stopped)
        t0 = time.time()
        act = mod.step(obs)
        slowest = max(slowest, time.time() - t0)
        total += time.time() - t0
        if isinstance(act, dict) and isinstance(act.get("stop"), list):
            stopped |= {x for x in act["stop"] if isinstance(x, str)} & set(setup["products"])
        if isinstance(act, dict) and isinstance(act.get("launch"), list):
            stopped -= {(x.get("id") if isinstance(x, dict) else x) for x in act["launch"]}
        for p in check_actions(act, setup):
            problems.append(f"day {d}: {p}")
    json.dump({"problems": problems, "slowest": slowest, "total": total}, open(json_out, "w"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--part", nargs=4, help=argparse.SUPPRESS)    # setup.json, days, first day, result.json
    a = ap.parse_args()
    if a.part:
        run(json.load(open(a.part[0])), int(a.part[1]), int(a.part[2]), a.part[3])
        return
    catalog, plan = load()
    setup = setup_for(catalog, plan)
    work = tempfile.mkdtemp(prefix="check-ops-")
    setup["state_dir"] = os.path.join(work, "state")
    os.makedirs(setup["state_dir"])
    problems, slowest, total, worst = [], 0.0, 0.0, 0.0
    half = a.days // 2
    for month, (first, days) in enumerate(((0, half), (half, a.days - half))):
        setup["live"] = {"month": month, "day": first, "commit": "check", "resumed": month > 0}
        sp, rp = os.path.join(work, "setup.json"), os.path.join(work, "result.json")
        json.dump(setup, open(sp, "w"))
        p = subprocess.run([sys.executable, os.path.abspath(__file__), "--part", sp, str(days), str(first), rp],
                           capture_output=True, text=True)
        if p.returncode != 0:
            problems.append(f"process {month + 1} (from day {first}) failed: {p.stderr.strip()[-1500:]}")
            break
        r = json.load(open(rp))
        problems += r["problems"]
        slowest = max(slowest, r["slowest"])
        total += r["total"]
        worst = max(worst, r["total"] / max(1, days))
    # The limits (REFERENCE.md): 20 s for one call, and the playbook's pace, 600 s for 1,095 days (the test markets
    # and the live company allow the same pace or more). Made-up days are lighter than real ones, so leave room.
    # The second process sees every country open, as many as a run can enter, so its pace counts on its own.
    per_day = total / max(1, a.days)
    if slowest > 20:
        problems.append(f"a step took {slowest:.1f} s; the limit is 20 s a call")
    if worst > 600 / 1095:
        problems.append(f"steps took {worst:.2f} s a day on average (in the busier process); a playbook world gives "
                        f"600 s for 1,095 days ({600 / 1095:.2f} s a day), so the software would be stopped")
    print(f"{a.days} days in 2 processes with one state folder, slowest step {slowest * 1000:.0f} ms, "
          f"{per_day * 1000:.0f} ms a day on average, {worst * 1000:.0f} ms in the busier process (limits: 20 s a step, {600 / 1095 * 1000:.0f} ms a day)")
    print("\n".join(problems[:40]) if problems else "no problems found")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
