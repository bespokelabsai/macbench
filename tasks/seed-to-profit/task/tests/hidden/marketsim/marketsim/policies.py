"""Fixed policies for checking the task: the policy ladder, the strategy check and the gaming tests
(the checks run before any model). None of them is shown to the team.

Every policy has init(setup) and step(obs) -> actions, the same interface as the team's ops/autopilot.py,
and a `plan` (what company/plan.yaml would hold).
"""
import statistics

from .catalog import PRODUCTS, RULES, SEASON, fees, landed


class DoNothing:
    plan = {"company": "do nothing", "credit_line": 0, "products": []}

    def init(self, setup):
        pass

    def step(self, obs):
        return {}


def _floor(pid):
    return 1.2 * landed(pid) + fees(pid)["mp_fulfill"]


def _margin(pid, price, ch):
    """Contribution per unit sold before ads, as a share of the price."""
    size = fees(pid)
    if ch == "site":
        cost = landed(pid) + size["site_fulfill"] / 1.2 + RULES["card_fee_pct"] * price
    else:
        cost = landed(pid) + size["mp_fulfill"] + RULES["mp_referral"] * price
    returns = 0.1 * (price + size["return_ship"])
    return max(0.0, (price - cost - returns) / price)


class RulePolicy:
    """Fixed rules with knobs.

    params:
      products: {pid: {"channels": [...], "price": share of typical price, "social": $/day, "sponsored": $/day}}
      cover: days of sales to keep in stock and on order beyond the quoted lead time
      batch: days of sales per order
      first: the first order, as a number of minimum orders
      season_ads: scale ad budgets by the public season factor of the category
      credit: credit line to open
    """

    def __init__(self, params, name="rules"):
        self.prm = params
        self.name = name
        self.plan = {"company": name, "credit_line": params.get("credit", 250_000),
                     "products": [{"id": pid, "channels": v.get("channels", ["site", "marketplace"]),
                                   "supplier": v.get("supplier", "A")}
                                  for pid, v in params["products"].items()]}

    def init(self, setup):
        self.setup = setup
        self.hist, self.ads, self.price_mult, self.first_day = {}, {}, {}, {}
        for pid in setup["products"]:
            self.add_line(pid, 0)
        self.cash_low = False
        self.relaunched = set()

    def add_line(self, pid, day):
        v = self.prm.get("all", {}).get(pid) or self.prm["products"].get(pid) or {}
        self.hist[pid] = []
        self.ads[pid] = {"social": float(v.get("social", 0.0)), "sponsored": float(v.get("sponsored", 0.0))}
        self.price_mult[pid] = v.get("price", 1.0)
        self.first_day[pid] = day

    def price_for(self, pid, po, obs):
        return max(_floor(pid), PRODUCTS[pid]["price"] * self.price_mult[pid])

    def ads_for(self, pid, po, obs):
        s = 1.0
        if self.prm.get("season_ads"):
            s = SEASON[PRODUCTS[pid]["category"]][int(obs["date"][5:7]) - 1]
        scale = 0.3 if self.cash_low else 1.0
        return {ch: round(v * s * scale, 2) for ch, v in self.ads[pid].items()}

    def stock(self, pid, po, obs, rate, spare, act):
        p = PRODUCTS[pid]
        cover = self.prm.get("cover", 30)
        batch = self.prm.get("batch", 60)
        inbound = sum(i["qty"] for i in po["inbound"])
        need = rate * (po["lead_quote"] + cover)
        moq = (po.get("supplier_quotes") or {}).get(po.get("supplier", "A"), {}).get("moq", p["moq"])
        if obs["day"] == self.first_day.get(pid, 0):
            act["orders"].append({"product": pid, "qty": int(self.prm.get("first", 2) * moq)})
            return spare
        if po["on_hand"] + inbound < need and rate > 0 and obs["days_left"] > po["lead_quote"] + 20:
            qty = max(moq, int(rate * batch))
            cost = qty * po["unit_cost_now"]
            if cost * RULES["deposit"] < 0.5 * spare:
                act["orders"].append({"product": pid, "qty": qty})
                spare -= cost
        return spare

    def observe_line(self, pid, po, obs):
        pass

    def step(self, obs):
        act = {"prices": {}, "ads": {}, "orders": []}
        spare = obs["cash"] + obs["credit_limit"] - obs["credit_used"]
        for pid, po in obs["products"].items():
            if not po["active"]:
                continue
            if pid not in self.hist or (self.first_day.get(pid, 0) < obs["day"] and pid in self.relaunched):
                self.relaunched.discard(pid)
                self.add_line(pid, obs["day"])
            p = PRODUCTS[pid]
            y = po["yesterday"]
            units = sum(y.get(ch, {}).get("units", 0) for ch in ("site", "marketplace"))
            units += (y.get("subscription") or {}).get("orders", 0)
            units += sum(c["units_per_month"] for c in obs.get("wholesale_contracts", []) if c["product"] == pid) / 30
            self.hist[pid].append(units)
            self.observe_line(pid, po, obs)
            price = self.price_for(pid, po, obs)
            act["prices"][pid] = {ch: round(price, 2) for ch in po["channels"]}
            act["ads"][pid] = self.ads_for(pid, po, obs)
            recent = self.hist[pid][-28:]
            rate = sum(recent) / max(1, len(recent))
            if obs["day"] - self.first_day.get(pid, 0) < 28:
                rate = max(rate, self.prm.get("first", 2) * p["moq"] / 90)
            spare = self.stock(pid, po, obs, rate, spare, act)
        self.cash_low = obs["cash"] < 150_000 and obs["credit_used"] > 0.7 * max(1.0, obs["credit_limit"])
        if obs["cash"] > 400_000 and obs["credit_used"] > 0:
            act["credit"] = {"repay": obs["credit_used"]}
        stop = self.to_stop(obs)
        if stop:
            act["stop"] = stop
        return act

    def to_stop(self, obs):
        return []


class LearningPolicy(RulePolicy):
    """The tuned rules, plus learning from the company's own data. Every 28 days, for each line:

    - Demand signal: units sold over the last 28 days against the 56 days before, each divided by the
      public season factor of the category.
    - Ads: budgets move with that signal, by at most 15% each time, and are cut by 25% when the
      platform's own return on the spend (which counts too many orders) is below the line's breakeven.
    - Price: up 4% when the line ran out of stock in the period, down 4% when demand fell by more than 20%
      while a rival is more than 10% cheaper, and back toward the starting price otherwise.
    - A 6 day discount of 15% every 70 days, kept only while sales during it rise by more than half.
    - Lines whose estimated contribution over the last 120 days is negative are stopped after day 300.
    - Stock planning looks ahead with the public season factors.
    """

    PERIOD = 28

    def init(self, setup):
        super().init(setup)
        self.mult = {pid: 1.0 for pid in setup["products"]}
        self.units = {pid: [] for pid in setup["products"]}       # season-adjusted units per day
        self.short = {pid: 0 for pid in setup["products"]}
        self.plat = {pid: {"social": [0.0, 0.0], "sponsored": [0.0, 0.0]} for pid in setup["products"]}
        self.contrib = {pid: [] for pid in setup["products"]}
        self.promo = {pid: {"on": True, "before": [], "during": []} for pid in setup["products"]}

    def price_for(self, pid, po, obs):
        d = obs["day"]
        price = PRODUCTS[pid]["price"] * self.price_mult[pid] * self.mult[pid]
        if self.promo[pid]["on"] and d > 60 and d % 70 < 6 and po["on_hand"] > 0:
            price *= 0.85
        return max(_floor(pid), price)

    def observe_line(self, pid, po, obs):
        d = obs["day"] - 1
        y = po["yesterday"]
        if d < 0:
            return
        p = PRODUCTS[pid]
        m = int(obs["date"][5:7])
        season = SEASON[p["category"]][(m - 2) % 12] if obs["date"][8:10] == "01" else SEASON[p["category"]][m - 1]
        units = sum(y.get(ch, {}).get("units", 0) for ch in ("site", "marketplace"))
        self.units[pid].append(units / season)
        if any(not y.get(ch, {}).get("in_stock", True) for ch in ("site", "marketplace")):
            self.short[pid] += 1
        c = 0.0
        for ch, ach in (("site", "social"), ("marketplace", "sponsored")):
            yc = y.get(ch, {})
            rev = yc.get("revenue", 0.0)
            if rev:
                c += rev * _margin(pid, rev / max(1, yc.get("units", 1)), ch)
            a = y.get("ads", {}).get(ach)
            if a:
                self.plat[pid][ach][0] += a["spend"]
                self.plat[pid][ach][1] += a["attributed_orders"] * rev / max(1, yc.get("orders", 1))
        c -= sum(a.get("spend", 0.0) for a in y.get("ads", {}).values()) + RULES["line_month"] / 30
        self.contrib[pid].append(c)
        # promotion lift
        pr = self.promo[pid]
        if d > 60:
            phase = d % 70
            if phase < 6:
                pr["during"].append(units)
            elif phase >= 56:
                pr["before"].append(units)
            if phase == 6 and pr["during"] and pr["before"]:
                lift = (sum(pr["during"]) / len(pr["during"])) / max(0.5, sum(pr["before"]) / len(pr["before"]))
                pr["on"] = lift > 1.5
                pr["during"], pr["before"] = [], []
        if d % self.PERIOD != self.PERIOD - 1 or d < 3 * self.PERIOD:
            return
        u = self.units[pid]
        now = sum(u[-self.PERIOD:]) / self.PERIOD
        before = sum(u[-3 * self.PERIOD:-self.PERIOD]) / (2 * self.PERIOD)
        g = now / before if before > 0 else 1.0
        # ads follow demand, and are cut when the platform's own return is below breakeven
        for ach, ch in (("social", "site"), ("sponsored", "marketplace")):
            f = min(1.15, max(0.85, g))
            spend, rev = self.plat[pid][ach]
            if spend > 100:
                margin = _margin(pid, po["price"].get(ch, p["price"]), ch)
                if rev / spend * margin < 0.9:
                    f = min(f, 0.75)
            self.ads[pid][ach] = min(8000.0, self.ads[pid][ach] * f)
            self.plat[pid][ach] = [0.0, 0.0]
        # price: scarcity, undercutting rivals, or back toward the start
        rivals = po["rival_prices"]
        cheapest = min(rivals) if rivals else p["price"]
        if self.short[pid] >= 5:
            self.mult[pid] *= 1.04
        elif g < 0.8 and cheapest < 0.9 * po["price"].get("marketplace", po["price"].get("site", p["price"])):
            self.mult[pid] *= 0.96
        else:
            self.mult[pid] += 0.25 * (1.0 - self.mult[pid])
        self.mult[pid] = min(1.3, max(0.75, self.mult[pid]))
        self.short[pid] = 0

    def stock(self, pid, po, obs, rate, spare, act):
        p = PRODUCTS[pid]
        m = int(obs["date"][5:7])
        now = SEASON[p["category"]][m - 1]
        ahead = max(SEASON[p["category"]][(m - 1 + k) % 12] for k in (1, 2, 3))
        return super().stock(pid, po, obs, rate * max(1.0, ahead / now), spare, act)

    def add_line(self, pid, day):
        super().add_line(pid, day)
        if hasattr(self, "mult"):
            self.mult[pid] = 1.0
            self.units[pid] = []
            self.short[pid] = 0
            self.plat[pid] = {"social": [0.0, 0.0], "sponsored": [0.0, 0.0]}
            self.contrib[pid] = []
            self.promo[pid] = {"on": True, "before": [], "during": []}

    def to_stop(self, obs):
        """Stop lines that lose money. Every 112 days after day 224, also swap the weakest line for the next
        product in the tuned ranking that has not been tried."""
        d = obs["day"]
        if d < 300:
            return []
        active = [pid for pid, po in obs["products"].items() if po["active"]]
        out = [pid for pid in active if d - self.first_day.get(pid, 0) > 150 and sum(self.contrib[pid][-120:]) < 0]
        if len(out) >= len(active):
            out = out[:-1]
        self.pending_launch = []
        if d % 112 == 0 and d < 900:
            ranked = [pid for pid in self.prm.get("ranked", []) if pid not in obs["products"]]
            scores = {pid: sum(self.contrib[pid][-112:]) for pid in active
                      if pid not in out and d - self.first_day.get(pid, 0) > 150}
            if ranked and scores:
                worst = min(scores, key=scores.get)
                med = sorted(scores.values())[len(scores) // 2]
                if scores[worst] < 0.25 * med:
                    out.append(worst)
            free = RULES["max_products"] - (len(active) - len(out))
            self.pending_launch = ranked[:max(0, min(free, len(out)))]
        return out

    def step(self, obs):
        act = super().step(obs)
        launch = getattr(self, "pending_launch", [])
        if launch:
            act["launch"] = [{"id": pid, "channels": ["site", "marketplace"]} for pid in launch]
            self.pending_launch = []
        return act


class AllAds:
    """Spends as much as it can on ads at once, on 10 products. Should go bankrupt."""

    def __init__(self):
        ids = list(PRODUCTS)[:10]
        self.plan = {"company": "all ads", "credit_line": RULES["credit_max"],
                     "products": [{"id": pid, "channels": ["site", "marketplace"]} for pid in ids]}

    def init(self, setup):
        pass

    def step(self, obs):
        act = {"ads": {pid: {"social": RULES["max_daily_ad"], "sponsored": RULES["max_daily_ad"]}
                       for pid in obs["products"]}}
        if obs["day"] == 0:
            act["orders"] = [{"product": pid, "qty": 5 * PRODUCTS[pid]["moq"]} for pid in obs["products"]]
        return act


def public_rank(n=5):
    """What a team could pick from public facts alone: the largest dollar margin per unit on the marketplace."""
    def margin(pid):
        p = PRODUCTS[pid]
        return p["price"] * (1 - RULES["mp_referral"]) - landed(pid) - fees(pid)["mp_fulfill"]
    return sorted(PRODUCTS, key=lambda pid: -margin(pid))[:n]


def plain_rules():
    prods = {pid: {"price": 1.0, "social": 0.0, "sponsored": 30.0} for pid in public_rank(10)}
    return RulePolicy({"products": prods, "cover": 30, "batch": 60, "first": 2, "credit": 250_000}, "plain rules")


# -- rules tuned with the true product facts -------------------------------------------------------

GRID = [(pr, so, sp) for pr in (0.85, 1.0, 1.15) for so in (0, 100, 400) for sp in (0, 60, 250)]


def _single(args):
    from . import sim, worlds
    truth, pid, combo, wseed, shape, channels = args[:6]
    sup = args[6] if len(args) > 6 else "A"
    pr, so, sp = combo
    pol = RulePolicy({"products": {pid: {"price": pr, "social": so, "sponsored": sp, "channels": channels,
                                         "supplier": sup}},
                      "cover": 30, "batch": 60, "first": 2, "credit": 250_000})
    r = sim.run(pol.plan, truth, worlds.make_world(wseed, shape), pol)
    return -1e7 if r["bankrupt"] else r["lines"][pid]["pnl"]["contribution"]


def tune(truth, practice_seeds=("tune-a", "tune-b"), shapes=("mnl", "nested"), workers=1, max_products=10,
         channels=("site", "marketplace"), grid=None):
    """For each product alone, the price and ad budgets that earn the most on practice worlds; then the products
    that earn money, best first. Returns RulePolicy params. channels and grid restrict the search, for the
    strategy check."""
    channels = list(channels)
    grid = grid or GRID
    jobs = [(truth, pid, combo, s, sh, channels) for pid in PRODUCTS for combo in grid
            for s, sh in zip(practice_seeds, shapes)]
    if workers > 1:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(workers) as ex:
            vals = list(ex.map(_single, jobs, chunksize=16))
    else:
        vals = [_single(j) for j in jobs]
    k = len(practice_seeds)
    best = {}
    for i in range(0, len(jobs), k):
        pid, combo = jobs[i][1], jobs[i][2]
        v = sum(vals[i:i + k]) / k
        if pid not in best or v > best[pid][0]:
            best[pid] = (v, combo)
    # then the best supplier for each product, with its best price and ads
    sjobs = [(truth, pid, best[pid][1], sd, sh, channels, sup) for pid in PRODUCTS for sup in ("B", "C")
             for sd, sh in zip(practice_seeds, shapes)]
    if workers > 1:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(workers) as ex:
            svals = list(ex.map(_single, sjobs, chunksize=8))
    else:
        svals = [_single(j) for j in sjobs]
    sups = {pid: "A" for pid in PRODUCTS}
    for i in range(0, len(sjobs), k):
        pid, sup = sjobs[i][1], sjobs[i][6]
        v = sum(svals[i:i + k]) / k
        if v > best[pid][0]:
            best[pid] = (v, best[pid][1])
            sups[pid] = sup
    ranked = sorted((v, pid, c) for pid, (v, c) in best.items())[::-1]
    chosen = [(pid, c) for v, pid, c in ranked if v > 0][:max_products]
    as_params = lambda pid, c: {"price": c[0], "social": float(c[1]), "sponsored": float(c[2]),  # noqa: E731
                                "channels": channels, "supplier": sups[pid]}
    return {"products": {pid: as_params(pid, c) for pid, c in chosen},
            "all": {pid: as_params(pid, c) for v, pid, c in ranked},
            "ranked": [pid for v, pid, c in ranked if v > 0],
            "cover": 30, "batch": 60, "first": 2, "credit": 250_000,
            "contribution": {pid: round(v) for v, pid, c in ranked}}


def tuned(params, learn=False, name=None, start=None):
    if learn:
        p = dict(params)
        if start:
            p["products"] = dict(list(params["products"].items())[:start])
        return LearningPolicy(p, name or "tuned rules, learning")
    return RulePolicy(params, name or "tuned rules")


# -- countries  -----------------------------------------------------------------------------

class _CountryRules(RulePolicy):
    """The rules of one country's market: the same knobs as in the US, at the country's typical price and landed
    cost, with ad budgets scaled by the country's market size."""

    def __init__(self, params, scale):
        super().__init__(params, "country rules")
        self.scale = scale

    def price_for(self, pid, po, obs):
        floor = 1.2 * po["unit_cost_now"] + fees(pid)["mp_fulfill"]
        return max(floor, po["typical_price"] * self.price_mult[pid])

    def ads_for(self, pid, po, obs):
        return {ch: round(v * self.scale, 2) for ch, v in super().ads_for(pid, po, obs).items()}


class CountryPolicy:
    """A US policy that also enters countries on a day, and runs each country with the same rules (_CountryRules).

    base: the US policy (its plan is the company's plan). enter: country codes. day: the day it enters them."""

    def __init__(self, base, enter, day=60, name=None):
        self.base, self.enter, self.day = base, list(enter), day
        self.plan = base.plan
        self.name = name or f"{getattr(base, 'name', 'rules')} + {','.join(self.enter) or 'no countries'}"
        self.prm = getattr(base, "prm", {"products": {}})

    def init(self, setup):
        self.base.init(setup)
        self.table = setup["countries"]
        self.sub = {}

    def step(self, obs):
        act = self.base.step(obs)
        if obs["day"] == self.day and self.enter:
            act["enter"] = list(self.enter)
        for cc, c in obs.get("countries", {}).items():
            if c["left"]:
                continue
            pol = self.sub.get(cc)
            if pol is None:
                pol = self.sub[cc] = _CountryRules(self.prm, self.table[cc]["market_size"])
                pol.init({"products": {}})          # its lines start the day it sees them
            sub = {"day": obs["day"], "date": obs["date"], "days_left": obs["days_left"], "cash": obs["cash"],
                   "credit_limit": obs["credit_limit"], "credit_used": obs["credit_used"], "products": c["products"],
                   "wholesale_contracts": []}
            a = pol.step(sub)
            act.setdefault("countries", {})[cc] = {k: a[k] for k in ("prices", "ads", "orders") if k in a}
        return act
