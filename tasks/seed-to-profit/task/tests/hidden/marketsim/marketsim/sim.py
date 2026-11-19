"""The market simulator. One call to `run` simulates one company in one world, one day at a time.

Each day:
1. The company's software sees yesterday's results and the state of the company, and returns its
   actions (prices, ad budgets, supplier orders, credit, stopping a line).
2. Supplier orders ship (the balance is paid) and arrive. Retail chains take their monthly deliveries.
3. Visitors come to the own site from social ads (adstock, then costs that rise with spend) and from
   people who know the brand (Bass awareness), and shoppers search the marketplace. Each one chooses
   between our product, the rivals' products and buying nothing (multinomial or nested logit, with a
   reference price). Past buyers buy again where they are customers: own-site buyers are the brand's list and buy
   again on the site, marketplace buyers mostly on the marketplace. Orders are Poisson draws, capped by stock. Site
   orders also carry items of the company's other site lines.
4. Fees, fulfillment, storage, overhead (which grows with revenue), interest and ad spend are paid. Marketplace
   sales are paid out every 14 days, once they are 10 days old. Returns and reviews arrive later. The credit
   line's limit follows the borrowing base (stock and receivables).
5. Development projects finish, chains and investors make offers, subscribers reorder and leave.
6. Rivals set prices once a week. A company whose cash stays below zero after drawing its whole credit
   line is bankrupt, and the world ends for it.

All randomness comes from the world's seed, so a run is the same every time.

Countries . The company sells in the US from day 0 and can enter other countries during the run
(countries.py). Each entered country is a Sim of its own (a market: its lines, shoppers, rivals, marketplace, shocks
and currency), held in the US Sim's `abroad` and stepped with it, day by day. The company's money (cash, credit,
debt, ownership) is one Treasury that every market shares, so a loss in one country can sink the company. The software
is called once a day, by the US Sim, with every country in its observation. A company that enters no country runs
exactly as before.
"""
import datetime
import math

from .catalog import PRODUCTS, RULES, SEASON, SIZES, SUPPLIER_KINDS, landed, storage, supplier
from .catalog import fees as product_fees
from . import countries as countrymod
from . import tricks
from .rivals import Rival
from .truth import rng_for
from .worlds import SHAPES

REF_SPEED = 0.02          # daily speed at which the reference price follows the price
RATING_PRIOR = (5, 4.2)   # a new listing shows this rating, worth this many reviews
REVIEW_CHANCE = 0.04      # chance that a unit sold gets a review, about 10 days later
RIVAL_SITE_WEIGHT = 0.3   # a visitor of our site weighs rival products this much (they must go and look)
RATING_UTILITY = 0.15     # utility of one star: about 16% more orders per star at a small share (v0.2: 0.5)
MP_RATING_WEIGHT = 0.1    # marketplace visibility per star (v0.2: 0.8); with RATING_UTILITY about 25% more sales per star
FEW_REVIEWS = (0.35, 2.5) # a listing with no reviews converts at 0.35 times (5 reviews: about 2.6 times as well as
                          # none); the gap shrinks e-fold every 2.5 reviews (v0.3: 0.5 and 4)
LOYAL_KEEP = 0.8          # a past buyer who needs the product again buys ours with this chance at the typical
                          # price, a rival's with 0.1 and goes without with 0.1 (CALIB_RIVALS rivals at the typical price)
RETURNING_CONV = 2.0      # past buyers' site visits convert this many times as well as new visitors' (for the
                          # session count only: they visit now and then and buy when they need it)
OWNED_LIFT = 1.3          # own-site buyers are on the brand's email and SMS list: they buy again this many times as
                          # often as the product's base repeat rate, and on the own site
MP_TO_SITE = 0.1          # share of marketplace buyers' repeat purchases made on the own site instead (the marketplace
                          # keeps their contact details); they join the list when they do
CART_ADDON = 0.8          # items from the company's other site lines per site order, with max_products site lines (and
                          # fewer with fewer lines), each in proportion to its price appeal
AD_AWARE = 3.0            # people who learn the brand from social ads, per click (delayed_ads worlds: 2.4 times)
CART_PICK = 0.3           # picking and packing one more item in the same parcel, as a share of an order's fulfillment
SPONSORED_ADSTOCK = 0.5   # daily carryover of sponsored ads (v0.2: 0.2)
REVENUE_MULTIPLE = 0.45   # investors' price of the business, per dollar of yearly revenue, at no growth
GROWTH_POWER = 0.8        # the multiple rises with yearly growth to this power
FORGET = 1 / 365          # daily share of aware people who forget the brand
MP_VIEWS = 1.3            # listings a marketplace shopper looks at
VEL_SCALE = 0.05          # marketplace sales per day, as a share of daily shoppers, that double a new listing's visibility
CALIB_RIVALS = 2          # conversion targets are set against this many rivals at the typical price


class Bankrupt(Exception):
    pass


class Treasury:
    """The company's money: one cash account, credit line, venture debt and ownership for all its markets."""

    def __init__(self):
        self.cash = 0.0
        self.credit_line = 0.0
        self.credit_limit = 0.0
        self.credit_used = 0.0
        self.venture_debt = 0.0
        self.ownership = 1.0
        self.equity_raised = 0.0
        self.last_post_money = None
        self.rounds = []            # investors' rounds: {"day", "amount", "share"}, share diluted by later rounds


def _money(name):
    return property(lambda s: getattr(s.treasury, name), lambda s, v: setattr(s.treasury, name, v))


def _date(day):
    return datetime.date.fromisoformat(RULES["start_date"]) + datetime.timedelta(days=day)


def _num(x, default=None):
    try:
        x = float(x)
    except (TypeError, ValueError, OverflowError):
        return default
    return x if math.isfinite(x) else default


class Line:
    """One product line of the company."""

    def __init__(self, pid, channels, truth, world, rng, default_supplier="A", p=None):
        self.pid = pid
        self.supplier = default_supplier if default_supplier in SUPPLIER_KINDS else "A"
        self.stock_defect = truth["suppliers"][self.supplier]["defect"]   # defect rate of the units on hand
        self.a_cost_mult = 1.0             # supplier A's own price changes (closure)
        self.a_lead_add = 0
        self.p = p or PRODUCTS[pid]          # a country's catalog entry has the country's typical price
        self.t = dict(truth)
        self.typ = self.p["price"]
        self.channels = set(channels)
        self.active = True
        self.price = {"site": self.typ, "marketplace": self.typ}
        self.ref = {"site": self.typ, "marketplace": self.typ}
        self.budget = {"social": 0.0, "sponsored": 0.0}
        self.spend_hist = {"social": [], "sponsored": []}
        self.on_hand = 0
        self.stock_value = 0.0            # landed cost of the units on hand
        self.pos = []                      # supplier orders
        self.aware = 0.0
        self.buyers = 0.0
        self.owned = 0.0                   # buyers on the own site (the brand's list), part of buyers
        self.reviews = []                  # (day due, stars)
        self.n_reviews = 0
        self.star_sum = 0.0
        self.returns = []                  # (day due, units, refund per unit, channel, defective units)
        self.mp_sales = []                 # marketplace units per day, for the sales rank
        self.pull = {"site": 0.0, "marketplace": 0.0}   # shoppers waiting for a discount (deal_seekers)
        self.sold = {"site": [], "marketplace": []}     # units sold per day, for the reference price
        self.week_px = {"site": [0.0, 0.0, 0.0, 0], "marketplace": [0.0, 0.0, 0.0, 0]}   # price*units, units, price, days
        self.rivals = [Rival(s, self.typ) for s in world["rivals"][pid]]
        self.extra_rivals = []
        self.copycats = 0
        self.m_mult = 1.0
        self.yesterday = {}
        self.lost = 0.0
        self.pnl = {k: 0.0 for k in ("revenue", "refunds", "cogs", "fulfillment", "fees", "ads", "storage", "fixed")}
        self.pnl_year = []                 # contribution of each 365 days
        self.cost_mult = 1.0               # supplier price changes (closure, tariff)
        self.lead_add = 0
        self.rnd = None                    # a development project: {"start", "done"}
        self.improved = False              # an own improved version replaced the catalog product
        self.rnd_cost_mult = 1.0
        self.subs = 0.0                    # subscribers on the own site
        self.subs_discount = 0.0
        self.subs_due = 0.0                # subscription orders due but not yet placed (fractions of an order)
        self.sub_cohorts = []              # [day joined, subscribers left], for churn by tenure
        self.contract_on = False           # a retail chain sells it now
        tricks.init_line(self)             # vendor reviews and marketplace suspensions
        # Calibrate the base utilities so that conversion at the typical price, with CALIB_RIVALS rivals
        # at the typical price, is the product's hidden conversion rate.
        c = self.t["conv_site"]
        self.a_site = math.log(c / (1 - c * (1 + RIVAL_SITE_WEIGHT * CALIB_RIVALS)))
        c = self.t["conv_mp"]
        self.a_mp = math.log(c / (1 - c * (1 + CALIB_RIVALS)))

    @property
    def rating(self):
        n0, r0 = RATING_PRIOR
        return (n0 * r0 + self.star_sum) / (n0 + self.n_reviews)

    def all_rivals(self):
        return [r for r in self.rivals + self.extra_rivals if not r.gone]

    def improve(self):
        """The development project worked: the improved version sells from now on."""
        lv = self.t["rnd"]
        self.improved = True
        self.a_site += lv["appeal"]
        self.a_mp += lv["appeal"]
        self.t["beta"] *= lv["beta_mult"]
        self.t["satisfaction"] = min(4.95, self.t["satisfaction"] + lv["stars"])
        self.rnd_cost_mult = lv["cost_mult"]


class Sim:
    cash = _money("cash")
    credit_line = _money("credit_line")       # the facility asked for
    credit_limit = _money("credit_limit")     # what may be drawn today: the line, up to the borrowing base
    credit_used = _money("credit_used")
    venture_debt = _money("venture_debt")
    ownership = _money("ownership")           # share of the company the team still owns
    equity_raised = _money("equity_raised")
    last_post_money = _money("last_post_money")

    def __init__(self, plan, truth, world, policy, days=None, record="month", log_errors=True, on_month=None,
                 country=None, parent=None):
        self.plan = plan
        self.truth = truth
        self.w = world
        self.policy = policy
        self.days = days or RULES["days"]
        self.record_kind = record
        self._luck = {}          # today's keyed random streams (luck)
        self.parent = parent     # the US market, for the market of a country the company entered
        self.cc = country or "US"
        self.abroad = {}         # the countries entered (US market only): code -> that country's market
        self.transfers = []      # stock moving between warehouses (US market only)
        self.transfer_seq = 0
        self.treasury = parent.treasury if parent is not None else Treasury()
        self.market_setup(country)
        if parent is None:
            self.cash = RULES["seed_money"]
            self.credit_line = min(float(plan.get("credit_line", 0) or 0), RULES["credit_max"])
            self.credit_limit = 0.0
            self.credit_used = 0.0
        self.receivable = 0.0              # what the marketplace owes us
        self.mp_pending = []               # (day it can be paid out, amount), summing to self.receivable
        self.tax_paid = 0.0
        self.year_profit = 0.0
        self.year_interest = 0.0
        self.day = 0
        self.bankrupt_day = None
        self.bankrupt_reason = ""
        self.lines = {}
        self.midrun = world.get("midrun")
        for item in plan.get("products", []):
            pid = item["id"]
            self.lines[pid] = Line(pid, item["channels"], truth["products"][pid], world, None,
                                   item.get("supplier", "A"), self.catalog[pid])
        self.po_seq = 0
        self.news = []
        self.notes = []          # errors and refused actions, for the record
        self.daily = []          # per day: cash and profit, always kept (company wall, value)
        self.record = []         # per day or per month, more detail
        self.op_profit = []      # operating profit per day
        self.ledger = {k: 0.0 for k in ("revenue", "refunds", "cogs", "fulfillment", "fees", "ads", "storage",
                                         "overhead", "launch", "interest", "tax", "writeoff", "rnd", "chargebacks",
                                         "financing_fees")}
        if parent is None:
            self.equity_raised = 0.0
        self.shocks = {s["kind"]: s for s in world["shocks"]}
        self.software_frozen = False
        self.frozen_until = "for the rest of the world"   # a live company: "until next month"
        self.next_day = 0        # the next day to run (advance)
        self.ended = False       # the world is over: its last day ran, or the company went bankrupt
        self._launch = 0.0
        self.launch_today = 0.0
        self.wstate = {"cat_cost": {}, "prod_cost": {}, "prod_lead": {}, "cat_beta": {}, "ads": 1.0, "prod_m": {},
                       "prod_rivals": {}}       # the world's changes so far, for lines launched later
        self.on_month = on_month        # called with the simulator every 30 days, for the live company wall
        self.log_errors = log_errors
        self.events = []         # world events in plain words, for the record
        self.rng_lever = rng_for(world["seed"], "levers")   # its own stream: a company that uses no lever sees the
        if parent is None:                                  # same world as before levers
            self.ownership = 1.0
            self.venture_debt = 0.0
            self.treasury.rounds = []
        self.offers = []         # retail chains' offers
        self.contracts = []
        self.wh_seq = 0
        self.wh_receivable = []  # (day due, amount)
        self.funding = None      # the investors' current term sheet
        self.last_term_sheet = -10_000
        self.bundles = []
        self.mood = float(math.exp(self.rng_lever.normal(0, 0.3)))   # investors' mood in this world
        self.tricks = tricks.Tricks(self)   # rivals' money, vendor pitches, the marketplace's review policing
        self.rng_invest = rng_for(world["seed"], "investors")     # its own stream (v0.3), so the others do not move
        self.debt_coverage = float(self.rng_invest.uniform(*RULES["venture_debt_warrant_coverage"]))
        if parent is None:
            self.last_post_money = None
            self.update_credit()
        else:
            self.ledger["inventory_paid"] = 0.0

    def market_setup(self, country):
        """The market's own terms: the US ones, or those of a country (countries.py). Every US value is the one the
        simulator always used, so a company that stays home runs exactly as before."""
        if country is None:
            self.catalog = PRODUCTS
            self.referral = RULES["mp_referral"]
            self.net = 1.0                     # revenue per dollar of shelf price: shelf prices include VAT abroad
            self.fx = 1.0                      # the local currency's value in dollars, against the start
            self.fx_level = 1.0                # what sudden currency drops took off it
            self.fx_local = 1.0                # our dollar price against local prices: fx to the power 1 - FX_PASS
            self.cost_fx = 1.0                 # local costs (fulfillment, storage, ads) in dollars, against the start
            self.countries_allowed = None      # a test market enters only the countries the live company is in
            self.transit = RULES["transit_days"]
            self.lead_shift = 0
            self.payout_days = RULES["mp_payout_days"]
            self.hold_days = RULES["mp_hold_days"]
            self.line_month = RULES["line_month"]
            self.fee_mult = None
            self.opens = 0                     # the first day the market's listings sell
            self.closed = False                # the company left the country
            self.po_prefix = "PO"
            return
        c = countrymod.row(country)
        pf = countrymod.price_factor(country)
        self.catalog = {pid: dict(p, price=p["price"] * pf) for pid, p in PRODUCTS.items()}
        self.referral = c["referral"]
        self.net = 1 / (1 + c["vat"])
        self.fx = 1.0
        self.fx_level = 1.0
        self.fx_local = 1.0
        self.cost_fx = 1.0
        self.countries_allowed = None
        self.transit = max(10, RULES["transit_days"] + countrymod.lead_shift(country))
        self.lead_shift = countrymod.lead_shift(country)
        self.payout_days = c["payout"]
        self.hold_days = c["hold"]
        self.line_month = 0.0                  # the line's own cost is the company's, paid once (US market)
        self.fee_mult = c["fulfil"]
        self.opens = 0
        self.closed = False
        self.po_prefix = f"{country}-PO"
        self.south = c["south"]

    # -- helpers ----------------------------------------------------------------------------------

    def luck(self, *key):
        """Today's random draws for one thing, e.g., one line's sales or one supplier order (v1.0). Each is keyed to
        the day and to what it is for, so two companies that make the same choice on the same day get the same luck,
        whatever else they did. Different choices still change the odds. The draws come from the world's seed, or
        from its `luck_seed` when one is set (the same world with other luck)."""
        k = (self.day,) + tuple(str(x) for x in key)
        g = self._luck.get(k)
        if g is None:
            if len(self._luck) > 4096:
                self._luck = {}
            g = self._luck[k] = rng_for(self.w.get("luck_seed", self.w["seed"]), "luck", *k)
        return g

    def note(self, msg):
        if self.parent is not None:             # a country's notes are the company's, with the country named
            self.parent.note(f"{self.cc}: {msg}")
            return
        if len(self.notes) < 200:
            self.notes.append({"day": self.day, "note": str(msg)[:300]})

    def spend(self, amount, kind):
        self.cash -= amount
        self.ledger[kind] = self.ledger.get(kind, 0.0) + amount

    def update_credit(self):
        """The credit line's limit today: the line asked for in the plan, up to the borrowing base (a share of the
        landed cost of stock on hand and of what the marketplace and chains owe). Debt above the limit is repaid
        from cash, as far as cash goes."""
        stock = sum(ln.stock_value for ln in self.lines.values())
        owed = max(0.0, self.receivable) + max(0.0, sum(a for _, a in self.wh_receivable))
        for m in self.abroad.values():
            stock += sum(ln.stock_value for ln in m.lines.values())
            owed += max(0.0, m.receivable)
        base = RULES["credit_advance_stock"] * stock + RULES["credit_advance_receivables"] * owed
        self.credit_limit = min(self.credit_line, base)
        over = self.credit_used - self.credit_limit
        if over > 0 and self.cash > 0:
            pay = min(over, self.cash)
            self.credit_used -= pay
            self.cash -= pay

    def season(self, cat, month):
        if cat == "outdoor" and self.parent is not None and self.south:
            month = (month + 5) % 12 + 1          # the southern summer
        s = SEASON[cat][month - 1]
        return 1 + self.w["season_amp"][cat] * (s - 1)

    def in_recession(self):
        r = self.w["recession"]
        return r is not None and r["start"] <= self.day < r["end"]

    def macro(self):
        """Demand and price sensitivity multipliers of the economy, with 60 day ramps."""
        r = self.w["recession"]
        if r is None:
            return 1.0, 1.0
        d = self.day
        if d < r["start"] or d >= r["end"] + 60:
            f = 0.0
        elif d < r["start"] + 60:
            f = (d - r["start"]) / 60
        elif d < r["end"]:
            f = 1.0
        else:
            f = 1 - (d - r["end"]) / 60
        return 1 - f * (1 - r["demand"]), 1 + f * (r["beta"] - 1)

    def cpm_mult(self, month):
        m = 1.0
        if month == 11:
            m = 1 + 0.6 * (self.w["q4_cpm"] - 1)
        elif month == 12:
            m = self.w["q4_cpm"]
        pl = self.w["platform"]
        ctr = 1.0
        if pl and self.day >= pl["day"]:
            m *= pl["cpm"]
            ctr = pl["ctr"]
        return m, ctr

    def effective_spend(self, hist):
        """Ad spend that works today: a normalized geometric adstock, or in delayed_ads worlds a lag that
        peaks after about a week. In delayed_ads worlds ads also bring fewer clicks at once and more people
        who remember the brand and come back later."""
        if not hist:
            return 0.0
        delay = self.w["ad_delay"]
        if delay:
            # weights of a gamma-shaped lag with its peak at `delay` days, over 4 weeks, summing to 1
            n = min(len(hist), 28)
            ws = [((i + 1) ** 2) * math.exp(-2 * (i + 1) / delay) for i in range(28)]
            tot = sum(ws)
            return sum(hist[-1 - i] * ws[i] for i in range(n)) / tot
        return None

    # -- the market's own terms (countries) ---------------------------------------------------------

    def local(self, ln, ch):
        """Our price as the market's shoppers see it, against rivals' prices and what they are used to paying: those
        follow part of a currency move (FX_PASS), our dollar price all of it. In the US, the price."""
        return ln.price[ch] / self.fx_local

    def fees(self, pid):
        """Fulfillment, storage and return costs per unit in this market."""
        f = product_fees(pid)
        if self.fee_mult is None:
            return f
        return {k: v * self.fee_mult * self.cost_fx for k, v in f.items()}

    def storage(self, pid, month, on_marketplace):
        x = storage(pid, month, on_marketplace)
        return x if self.fee_mult is None else x * self.fee_mult * self.cost_fx

    def landed(self, pid, qty=0, s="A"):
        """Landed cost per unit of an order into this market's warehouse: in a country, the supplier price with the
        country's duty in place of the US duty, and the country's freight (countries.py)."""
        if self.parent is None:
            return landed(pid, qty, s)
        return countrymod.landed_cost(self.cc, pid, qty, s)

    def selling(self):
        """The market's listings sell today: it is open and the company has not left."""
        return self.day >= self.opens and not self.closed

    def home(self):
        return self.parent if self.parent is not None else self

    def lines_selling(self):
        """Product lines selling anywhere: a line is the company's, in every country it is listed in."""
        h = self.home()
        ids = {pid for pid, ln in h.lines.items() if ln.active}
        for m in h.abroad.values():
            ids |= {pid for pid, ln in m.lines.items() if ln.active}
        return ids

    # -- the software's view and actions -------------------------------------------------------------

    def setup(self):
        prods = {}
        for pid, ln in self.lines.items():
            p = dict(ln.p)
            p["channels"] = sorted(ln.channels)
            p["supplier"] = ln.supplier
            p["landed"] = landed(pid, 0, ln.supplier)
            p["landed_break"] = landed(pid, RULES["price_break_qty"] * supplier(pid, ln.supplier)["moq"], ln.supplier)
            p["suppliers"] = {s: supplier(pid, s) for s in SUPPLIER_KINDS}
            p["fees"] = product_fees(pid)
            prods[pid] = p
        return {"days": self.days, "start_date": RULES["start_date"], "rules": {k: (list(v) if isinstance(v, tuple) else v)
                                                                               for k, v in RULES.items()},
                "sizes": SIZES, "season": SEASON, "shapes": list(SHAPES), "products": prods,
                "catalog": {pid: dict(p, landed=landed(pid), suppliers={s: supplier(pid, s) for s in SUPPLIER_KINDS},
                                      fees=product_fees(pid))
                            for pid, p in PRODUCTS.items()},
                "credit_line": self.credit_line, "credit_limit": self.credit_limit, "cash": self.cash,
                "countries": countrymod.public_table()}

    def observe(self):
        d = _date(self.day)
        prods = self.observe_products()
        return {
            "day": self.day, "date": d.isoformat(), "days_left": self.days - self.day,
            "cash": round(self.cash, 2), "credit_line": self.credit_line, "credit_limit": round(self.credit_limit, 2),
            "credit_used": round(self.credit_used, 2),
            "marketplace_receivable": round(self.receivable, 2),
            "marketplace_fee": self.referral,
            "products": prods,
            "news": [n for n in self.news if n["day"] == self.day],
            "wholesale_offers": [dict(o) for o in self.offers if o["expires"] >= self.day],
            "wholesale_contracts": [{k: c[k] for k in ("id", "chain", "product", "units_per_month", "price", "months",
                                                       "deliveries", "next_delivery", "strikes", "shelf_sales")}
                                    for c in self.contracts if c["status"] == "running"],
            "wholesale_receivable": round(sum(a for _, a in self.wh_receivable), 2),
            "funding": {"equity_offer": dict(self.funding) if self.funding and self.funding["expires"] >= self.day else None,
                        "venture_debt": self.venture_terms()},
            "ownership": round(self.ownership, 4),
            "equity_rounds": [{"day": r["day"], "amount": r["amount"], "share": round(r["share"], 4)}
                              for r in getattr(self.treasury, "rounds", [])],
            "venture_debt_used": round(self.venture_debt, 2),
            "bundles": [dict(b) for b in self.bundles],
            "vendor_offers": self.tricks.observe(),
            "countries": {cc: m.observe_market() for cc, m in self.abroad.items()},
            "transfers": [{k: t[k] for k in ("id", "product", "qty", "from", "to", "arrives")} for t in self.transfers],
        }

    def observe_market(self):
        """A country's part of the observation: the same per product as the US, with its typical price."""
        prods = self.observe_products()
        for pid, p in prods.items():
            p["typical_price"] = round(self.lines[pid].typ, 2)
        return {"opens": self.opens, "selling": self.selling(), "left": self.closed, "fx": round(self.fx, 4),
                "vat": countrymod.row(self.cc)["vat"], "marketplace_fee": self.referral,
                "marketplace_receivable": round(self.receivable, 2), "products": prods,
                "news": [n for n in self.news if n["day"] == self.day], "bundles": [dict(b) for b in self.bundles],
                "vendor_offers": self.tricks.observe()}

    def observe_products(self):
        prods = {}
        for pid, ln in self.lines.items():
            prods[pid] = {
                "active": ln.active,
                "channels": sorted(ln.channels),
                "price": dict(ln.price),
                "ad_budget": dict(ln.budget),
                "on_hand": ln.on_hand,
                "inbound": [{"po": po["id"], "qty": po["qty"], "status": po["status"],
                             "expected": po["eta"], "arrives": po["arrive"] if po["status"] == "shipped" else None,
                             "unit_cost": round(po["unit"], 2), "supplier": po.get("supplier", "A")}
                            for po in ln.pos if po["status"] in ("ordered", "shipped")],
                "rating": round(ln.rating, 2),
                "reviews": ln.n_reviews,
                "supplier": ln.supplier,
                "unit_cost_now": round(self.quote(ln, ln.supplier)[0], 2),
                "lead_quote": self.quote(ln, ln.supplier)[1],
                "supplier_quotes": {s: {"unit_cost": round(self.quote(ln, s)[0], 2), "lead": self.quote(ln, s)[1],
                                        "moq": supplier(pid, s)["moq"]} for s in SUPPLIER_KINDS},
                "rival_prices": [round(r.price, 2) for r in ln.all_rivals()],
                "rival_ratings": [round(r.rating, 2) for r in ln.all_rivals()],
                "improved": ln.improved,
                "development": None if ln.rnd is None else {"started": ln.rnd["start"], "result_day": ln.rnd["done"]},
                "subscription_discount": ln.subs_discount,
                "subscribers": int(ln.subs),
                "site_customers": int(ln.owned),
                "suspended_until": ln.suspended_until,
                "yesterday": ln.yesterday,
            }
        return prods

    def apply(self, act):
        if not isinstance(act, dict):
            self.note("actions were not a JSON object; nothing changed")
            return
        for pid, chans in self._mapping(act, "prices").items():
            ln = self.lines.get(pid) if isinstance(pid, str) else None
            if ln is None or not isinstance(chans, dict):
                continue
            for ch, v in chans.items():
                v = _num(v)
                if ch not in ln.price or v is None:
                    continue
                lo, hi = 0.25 * ln.typ, 4.0 * ln.typ
                if not lo <= v <= hi:
                    self.note(f"{pid} {ch} price {v} outside {lo:.2f} to {hi:.2f}; clipped")
                ln.price[ch] = min(hi, max(lo, v))
        for pid, chans in self._mapping(act, "ads").items():
            ln = self.lines.get(pid) if isinstance(pid, str) else None
            if ln is None or not isinstance(chans, dict):
                continue
            for ch, v in chans.items():
                v = _num(v)
                if ch not in ln.budget or v is None:
                    continue
                ln.budget[ch] = min(RULES["max_daily_ad"], max(0.0, v))
        for o in self._list(act, "orders")[:50]:
            if isinstance(o, dict):
                to = o.get("to")
                if to is not None and to != self.cc and self.parent is None:
                    m = self.abroad.get(to) if isinstance(to, str) else None
                    if m is None or m.closed:
                        self.note(f"order refused: {to!r} is not a country the company has entered")
                    else:
                        m.order(o.get("product"), o.get("qty"), o.get("supplier"))
                else:
                    self.order(o.get("product"), o.get("qty"), o.get("supplier"))
        line = _num(act.get("credit_line")) if "credit_line" in act else None
        if line is not None:       # ask for a different credit line (v1.0: a company can open one after its launch)
            self.credit_line = min(max(0.0, line), RULES["credit_max"])
            self.update_credit()
        cr = act.get("credit") or {}
        if isinstance(cr, dict):
            draw = _num(cr.get("draw"), 0.0)
            repay = _num(cr.get("repay"), 0.0)
            if draw and draw > 0:
                amt = min(draw, self.credit_limit - self.credit_used)
                self.credit_used += amt
                self.cash += amt
            if repay and repay > 0:
                amt = min(repay, self.credit_used, max(0.0, self.cash))
                self.credit_used -= amt
                self.cash -= amt
        for pid in self._list(act, "stop"):
            ln = self.lines.get(pid) if isinstance(pid, str) else None
            if ln is not None and ln.active:
                ln.active = False
                ln.budget = {"social": 0.0, "sponsored": 0.0}
                self.events.append({"day": self.day, "text": f"stopped selling {pid}"})
        for item in self._list(act, "launch")[:10]:
            self.launch(item)
        for item in self._list(act, "develop")[:10]:
            self.develop(item.get("product") if isinstance(item, dict) else item)
        for oid in self._list(act, "accept_offers")[:10]:
            self.accept_offer(oid)
        for pid, disc in self._mapping(act, "subscriptions").items():
            self.set_subscription(pid, disc)
        if "bundles" in act:
            self.set_bundles(self._list(act, "bundles"))
        self.tricks.apply(act)
        rs = self._mapping(act, "raise")
        for kind, amt in rs.items():
            self.raise_money(kind, amt)
        if self.parent is None and any(k in act for k in ("enter", "leave", "countries", "transfer")):
            self.apply_countries(act)

    # -- countries ----------------------------------------------------------------------------------

    MARKET_ACTIONS = ("prices", "ads", "orders", "stop", "launch", "subscriptions", "bundles", "vendors")

    def apply_countries(self, act):
        """Enter and leave countries, the actions of each country's market, and stock transfers."""
        for cc in self._list(act, "enter")[:20]:
            self.enter(cc)
        for cc in self._list(act, "leave")[:20]:
            self.leave(cc)
        for cc, sub in self._mapping(act, "countries").items():
            m = self.abroad.get(cc) if isinstance(cc, str) else None
            if m is None or m.closed or not isinstance(sub, dict):
                self.note(f"countries: {cc!r} is not a country the company has entered, or its actions are not an "
                          f"object")
                continue
            extra = sorted(k for k in sub if k not in self.MARKET_ACTIONS)
            if extra:
                self.note(f"countries[{cc!r}]: {extra} are actions of the whole company; give them at the top level")
            m.apply({k: v for k, v in sub.items() if k in self.MARKET_ACTIONS})
        for t in self._list(act, "transfer")[:50]:
            self.transfer(t)

    def market(self, cc):
        if cc == "US":
            return self
        m = self.abroad.get(cc) if isinstance(cc, str) else None
        return m if m is not None and not m.closed else None

    def enter(self, cc):
        """Enter a country: pay its entry cost now; its listings sell from `days` later. Every line selling in the US
        is listed there (at the country's listing cost per line), and stock can be ordered into its warehouse at
        once."""
        if not isinstance(cc, str) or cc not in countrymod.COUNTRIES:
            self.note(f"enter refused: {cc!r} is not one of {sorted(countrymod.COUNTRIES)}")
            return
        if self.countries_allowed is not None and cc not in self.countries_allowed:
            self.note(f"enter refused: a test market enters only the countries the company has entered "
                      f"({', '.join(sorted(self.countries_allowed)) or 'none yet'})")
            return
        m = self.abroad.get(cc)
        if m is not None and not m.closed:
            self.note(f"enter refused: the company is already in {cc}")
            return
        c = countrymod.row(cc)
        lines = [ln for ln in self.lines.values() if ln.active]
        cost = c["entry"] + c["listing"] * len(lines)
        if cost + c["capital"] > self.cash + (self.credit_limit - self.credit_used):
            need = f" and ${c['capital']:,.0f} of paid-up capital" if c["capital"] else ""
            self.note(f"enter refused: no cash for the ${cost:,.0f} cost of entering {cc}{need}")
            return
        self.spend(c["entry"], "entry")
        self.launch_today += c["entry"]
        if m is None:
            m = Sim({"products": [], "credit_line": 0}, countrymod.country_truth(self.truth, cc),
                    countrymod.country_world(self.w, cc), None, days=self.days, record="month", country=cc,
                    parent=self)
            m.catch_up(self.day)
            self.abroad[cc] = m
        m.closed = False
        m.opens = self.day + c["days"]
        for ln in lines:
            m.launch({"id": ln.pid, "channels": sorted(ln.channels), "supplier": ln.supplier})
        text = f"entered {cc} ({c['name']}): its listings sell from day {m.opens}"
        self.events.append({"day": self.day, "text": text})
        self.news.append({"day": self.day + 1, "kind": "country", "country": cc, "opens": m.opens,
                          "text": f"The company is entering {c['name']}. Its listings go live on day {m.opens}, once "
                                  f"its tax registration, compliance and listings are done."})

    def leave(self, cc):
        """Leave a country: its stock (on hand and at sea to it) is sold off at the liquidation rate, open supplier
        orders to it are cancelled (deposits lost), and its costs stop. Money the marketplace owes is still paid
        out, and returns still come back."""
        m = self.abroad.get(cc) if isinstance(cc, str) else None
        if m is None or m.closed:
            self.note(f"leave refused: the company is not in {cc!r}")
            return
        loss = 0.0
        for ln in m.lines.values():
            value = ln.stock_value + sum(po["shipped"] * po["unit"] for po in ln.pos if po["status"] == "shipped")
            lost = sum(po["deposit"] for po in ln.pos if po["status"] == "ordered")
            got = RULES["liquidation"] * value
            self.cash += got
            m.ledger["writeoff"] += value - got + lost
            loss += value - got + lost
            ln.on_hand, ln.stock_value, ln.pos = 0, 0.0, []
            ln.active = False
            ln.budget = {"social": 0.0, "sponsored": 0.0}
            ln.subs, ln.subs_due, ln.sub_cohorts = 0.0, 0.0, []
            ln.owned = 0.0      # the country's email list is closed too, so its cost stops
        m.bundles = []
        m.launch_today += loss
        m.closed = True
        self.events.append({"day": self.day, "text": f"left {cc}; its stock was sold off for "
                                                     f"{RULES['liquidation']:.0%} of its cost"})

    def transfer(self, t):
        """Move stock between the company's warehouses, by road within the EU and between the US and its
        neighbours, else by sea. The freight and the destination's duty are paid now and added to the units' cost;
        they arrive after the transit."""
        if not isinstance(t, dict):
            self.note(f"transfer refused: {t!r} must be {{'product', 'qty', 'from', 'to'}}")
            return
        src, dst = self.market(t.get("from", "US")), self.market(t.get("to"))
        pid, q = t.get("product"), _num(t.get("qty"))
        a = src.lines.get(pid) if src is not None and isinstance(pid, str) else None
        b = dst.lines.get(pid) if dst is not None and isinstance(pid, str) else None
        if a is None or b is None or src is dst or q is None or int(q) < 1:
            self.note(f"transfer refused: {t!r} needs a product listed in both countries, two different countries "
                      f"the company is in, and a quantity")
            return
        q = int(q)
        if q > a.on_hand:
            self.note(f"transfer refused: {pid} has {a.on_hand} units on hand in {src.cc}")
            return
        share, days, duty = countrymod.transfer_terms(src.cc, dst.cc)
        p = PRODUCTS[pid]
        per = share * p["freight"]
        if duty:          # the destination's duty and its tariffs now, as on an order into it
            base = supplier(pid, a.supplier)["cost"]
            per += countrymod.duty_free(base) * countrymod.duty(dst.cc) + base * (b.cost_mult - 1)
        if q * per > self.cash + (self.credit_limit - self.credit_used):
            self.note(f"transfer refused: no cash for the ${q * per:,.0f} freight and duty")
            return
        unit = a.stock_value / a.on_hand
        a.on_hand -= q
        a.stock_value = max(0.0, a.stock_value - q * unit)
        self.spend(q * per, "inventory_paid")
        self.transfer_seq += 1
        self.transfers.append({"id": f"T{self.transfer_seq}", "product": pid, "qty": q, "from": src.cc, "to": dst.cc,
                               "arrives": self.day + days, "unit": unit + per, "defect": a.stock_defect})

    def step_transfers(self):
        """Transfers that arrive today join the destination's stock; at a country the company left, they are sold
        off at the liquidation rate."""
        keep = []
        for t in self.transfers:
            if t["arrives"] > self.day:
                keep.append(t)
                continue
            m = self.abroad.get(t["to"]) if t["to"] != "US" else self
            ln = m.lines.get(t["product"]) if m is not None else None
            if m is None or ln is None or m.closed:
                value = t["qty"] * t["unit"]
                self.cash += RULES["liquidation"] * value
                (m or self).ledger["writeoff"] += (1 - RULES["liquidation"]) * value
                self.launch_today += (1 - RULES["liquidation"]) * value
                continue
            n = ln.on_hand + t["qty"]
            ln.stock_defect = (ln.on_hand * ln.stock_defect + t["qty"] * t["defect"]) / n
            ln.on_hand += t["qty"]
            ln.stock_value += t["qty"] * t["unit"]
        self.transfers = keep

    def catch_up(self, day):
        """A country's market made on day `day`: the world's changes before then (shocks, hidden changes, the
        announced change, the currency), so that it starts as the country is now. Its old news is dropped."""
        h = self.parent
        for k in ("prod_cost", "prod_lead"):           # a supplier that closed is closed for every country
            self.wstate[k] = dict(h.wstate[k])
        if self.midrun and not self.midrun.get("start_day"):
            self.apply_midrun()
        for d in range(day):
            self.day = d
            self.world_events()
            start = (self.midrun or {}).get("start_day")
            if start and d == start:
                self.apply_midrun()
            self.update_fx(news=False)
        self.day = day
        self.news = [n for n in self.news if n["day"] >= day or (n.get("starts") or -1) >= day]
        self.next_day = day
        self.market_events()

    def market_events(self):
        """A country's events of the day: its world's shocks and changes, the announced change, its currency."""
        d = self.day
        self.world_events()
        start = (self.midrun or {}).get("start_day")
        if start:
            if d == max(0, start - 30):
                self.news.append({"day": d, "kind": "market_change", "text": self.midrun["text"]})
            if d == start:
                self.apply_midrun()
        self.update_fx()

    def update_fx(self, news=True):
        d = self.day
        for drop in self.w["fx_drops"]:
            if d == drop["day"]:
                self.fx_level *= 1 - drop["size"]
                cur = countrymod.row(self.cc)["currency"]
                text = f"The {cur} fell {drop['size']:.0%} against the dollar today."
                self.events.append({"day": d, "text": text})
                if news:
                    self.news.append({"day": d, "kind": "currency", "country": self.cc, "text": text})
        path = self.w["fx"]
        self.fx = math.exp(path[min(d, len(path) - 1)]) * self.fx_level
        self.fx_local = self.fx ** (1 - countrymod.FX_PASS)
        self.cost_fx = self.fx

    def close_supplier(self, s):
        """The standard supplier of a product closed (a US world shock): the same factory for every country."""
        ws = self.wstate
        ws["prod_cost"][s["target"]] = ws["prod_cost"].get(s["target"], 1.0) * 1.12
        ws["prod_lead"][s["target"]] = ws["prod_lead"].get(s["target"], 0) + 15
        ln = self.lines.get(s["target"])
        if ln:
            for po in ln.pos:
                if po["status"] == "ordered" and po.get("supplier", "A") == "A":
                    po["ship"] += 60
                    po["arrive"] += 60
            ln.a_cost_mult *= 1.12
            ln.a_lead_add += 15

    def _mapping(self, act, key):
        v = act.get(key)
        if v is None:
            return {}
        if not isinstance(v, dict):
            self.note(f"actions[{key!r}] must be an object; ignored")
            return {}
        return v

    def _list(self, act, key):
        v = act.get(key)
        if v is None:
            return []
        if not isinstance(v, list):
            self.note(f"actions[{key!r}] must be a list; ignored")
            return []
        return v

    def launch(self, item):
        """Start a new product line during the world (or start a stopped one again)."""
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or item.get("id") not in PRODUCTS:
            self.note(f"launch refused: {item!r} is not {{'id': <catalog id>, 'channels': [...]}}")
            return
        pid = item["id"]
        chans = item.get("channels", list(RULES["channels"]))
        if isinstance(chans, str):
            chans = [chans]
        if not isinstance(chans, list) or not chans or any(c not in RULES["channels"] for c in chans):
            self.note(f"launch refused: {pid} channels must be a non-empty list of {list(RULES['channels'])}")
            return
        if pid in self.lines and self.lines[pid].active:
            self.note(f"launch refused: {pid} is already selling")
            return
        selling = self.lines_selling()
        if pid not in selling and len(selling) >= RULES["max_products"]:
            self.note(f"launch refused: {RULES['max_products']} lines are already selling")
            return
        # a line costs its launch the first time it launches anywhere (samples, photos, listing); in a country also
        # the listing there (translation, labels, compliance)
        h = self.home()
        before = pid in h.lines or any(pid in m.lines for m in h.abroad.values())
        if self.parent is None:
            price = RULES["launch_cost"]
        else:
            price = countrymod.row(self.cc)["listing"] + (0.0 if before else RULES["launch_cost"])
        if price > self.cash + (self.credit_limit - self.credit_used):
            self.note(f"launch refused: no cash for the launch cost of {pid}")
            return
        self.spend(price, "launch")
        self.launch_today += price
        old = self.lines.get(pid)
        sup = item.get("supplier", "A")
        if self.parent is not None and pid in self.parent.lines and "supplier" not in item:
            sup = self.parent.lines[pid].supplier
        ln = Line(pid, chans, self.truth["products"][pid], self.w, None, sup, self.catalog[pid])
        if old is not None:      # a line started again keeps everything it had, its market and its rivals
            for k in ("t", "on_hand", "stock_value", "pos", "aware", "buyers", "owned", "reviews", "n_reviews", "star_sum",
                      "returns", "pnl", "rivals", "extra_rivals", "copycats", "m_mult", "cost_mult", "lead_add", "lost",
                      "ref", "mp_sales", "pull", "spend_hist", "sold", "week_px", "stock_defect", "a_cost_mult",
                      "a_lead_add", "rnd", "improved", "rnd_cost_mult", "a_site", "a_mp", "contract_on", "subs",
                      "subs_discount", "subs_due", "early_enrolled", "early_pending", "paid_pending", "paid_live",
                      "paid_posted", "suspended_until", "reserve"):
                setattr(ln, k, getattr(old, k))
        else:
            self.world_effects(ln)
            home = self.parent.lines.get(pid) if self.parent is not None else None
            if home is not None and home.improved:      # the improved version is the company's, everywhere
                ln.improve()
        self.lines[pid] = ln
        self.events.append({"day": self.day, "text": f"launched {pid}" + (f" in {self.cc}" if self.parent else "")})

    def quote(self, ln, s, q=0):
        """Unit cost and quoted lead of supplier s today, with tariffs and supplier A's closure."""
        base = supplier(ln.pid, s)
        # surcharges (tariffs, an improved version, supplier A's closure) on the supplier price: abroad without the
        # US duty the catalog's prices include; in the US exactly as before
        cost = base["cost"] if self.parent is None else base["cost"] / (1 + countrymod.US_DUTY)
        unit = self.landed(ln.pid, q, s) + cost * (ln.cost_mult * ln.rnd_cost_mult - 1)
        lead = base["lead"] + ln.lead_add + self.lead_shift
        if s == "A":
            unit += cost * ln.cost_mult * (ln.a_cost_mult - 1)
            lead += ln.a_lead_add
        return unit, lead

    def order(self, pid, qty, sup=None):
        ln = self.lines.get(pid) if isinstance(pid, str) else None
        q = _num(qty)
        if ln is None or q is None:
            self.note(f"order refused: unknown product {pid!r} or quantity {qty!r}")
            return
        q = int(q)
        s = sup if sup is not None else ln.supplier
        if s not in SUPPLIER_KINDS:
            self.note(f"order refused: supplier {s!r} must be one of {sorted(SUPPLIER_KINDS)}")
            return
        if not ln.active:
            self.note(f"order refused: {pid} is stopped")
            return
        moq = supplier(pid, s)["moq"]
        if q < moq:
            self.note(f"order refused: {pid} quantity {q} is below supplier {s}'s minimum order {moq}")
            return
        if q > RULES["max_order_units"]:
            self.note(f"order refused: {pid} quantity {q} is above the largest order, {RULES['max_order_units']} units")
            return
        unit, quoted = self.quote(ln, s, q)
        deposit = RULES["deposit"] * q * unit
        if deposit > self.cash + (self.credit_limit - self.credit_used):
            self.note(f"order refused: {pid} deposit ${deposit:,.0f} is more than cash and credit")
            return
        self.spend(deposit, "inventory_paid")
        self.po_seq += 1
        t = ln.t["suppliers"][s]
        lead = t["lead_mean"] * math.exp(self.luck(ln.pid, "order", self.po_seq).normal(0, t["lead_sd"]) - t["lead_sd"] ** 2 / 2) + ln.lead_add
        if s == "A":
            lead += ln.a_lead_add
        if self.luck(ln.pid, "order", self.po_seq).random() < t["late_chance"]:
            lead += self.luck(ln.pid, "order", self.po_seq).integers(15, 31)
        lead = max(self.transit + 3, int(round(lead)))
        arrive = self.day + lead
        ship = arrive - self.transit
        sh = self.shocks.get("port_delay")
        if sh and sh["day"] <= arrive <= sh["day"] + sh["length"] and self.day >= sh["day"] - 30:
            arrive += sh["size"]
        ln.pos.append({"id": f"{self.po_prefix}{self.po_seq}", "qty": q, "unit": unit, "ordered": self.day, "ship": ship,
                       "arrive": arrive, "eta": self.day + quoted, "status": "ordered",
                       "shipped": 0, "deposit": deposit, "supplier": s, "defect": t["defect"], "fill": t["fill"]})

    # -- growing the company: development, retail chains, subscriptions, bundles, money ----------------------

    def develop(self, pid):
        ln = self.lines.get(pid) if isinstance(pid, str) else None
        if ln is None or not ln.active:
            abroad = [cc for cc, m in getattr(self, "abroad", {}).items() if pid in m.lines and m.lines[pid].active]
            self.note(f"develop refused: {pid!r} is not a US line that is selling" +
                      (f" (it sells in {', '.join(abroad)}; developments run through the product's US line)" if abroad else ""))
            return
        if ln.improved or ln.rnd is not None:
            self.note(f"develop refused: {pid} is already improved or in development")
            return
        if sum(1 for x in self.lines.values() if x.rnd is not None) >= RULES["rnd_max_running"]:
            self.note(f"develop refused: {RULES['rnd_max_running']} projects are already running")
            return
        if RULES["rnd_cost"] > self.cash + (self.credit_limit - self.credit_used):
            self.note(f"develop refused: no cash for the ${RULES['rnd_cost']:,.0f} project")
            return
        self.spend(RULES["rnd_cost"], "rnd")
        self.launch_today += RULES["rnd_cost"]
        ln.rnd = {"start": self.day, "done": self.day + 30 * ln.t["rnd"]["months"]}
        self.events.append({"day": self.day, "text": f"started developing an improved {pid}"})

    def set_subscription(self, pid, disc):
        ln = self.lines.get(pid) if isinstance(pid, str) else None
        d = _num(disc)
        lo, hi = RULES["discount_range"]
        if ln is None or "site" not in ln.channels or d is None or not (d == 0 or lo <= d <= hi):
            self.note(f"subscription refused: {pid!r} must be a line on the own site and the discount 0 (to end it) "
                      f"or from {lo} to {hi}")
            return
        if d == 0 and ln.subs_discount > 0:
            ln.subs = 0.0                  # subscribers go back to buying when they like
            ln.subs_due = 0.0
            ln.sub_cohorts = []
        ln.subs_discount = d

    def set_bundles(self, items):
        lo, hi = RULES["discount_range"]
        out = []
        for b in items[:RULES["max_bundles"]]:
            ps = b.get("products") if isinstance(b, dict) else None
            d = _num(b.get("discount")) if isinstance(b, dict) else None
            ok = isinstance(ps, list) and len(ps) == 2 and all(isinstance(p, str) for p in ps) and ps[0] != ps[1] \
                and all(p in self.lines and "site" in self.lines[p].channels for p in ps) and d is not None and lo <= d <= hi
            if not ok:
                self.note(f"bundle refused: {b!r} must be {{'products': [two lines on the own site], 'discount': "
                          f"{lo} to {hi}}}")
                continue
            if any(o["products"] == sorted(ps) for o in out):
                self.note(f"bundle refused: {sorted(ps)} is already a bundle")
                continue
            out.append({"products": sorted(ps), "discount": d})
        if len(items) > RULES["max_bundles"]:
            self.note(f"only the first {RULES['max_bundles']} bundles are kept")
        self.bundles = out

    def run_rate(self, days=90, back=0):
        """Yearly revenue at the pace of `days` days, ending `back` days ago."""
        chunk = self.daily[len(self.daily) - back - days:len(self.daily) - back] if len(self.daily) >= back + days else []
        return sum(c["revenue"] for c in chunk) * 365 / days if chunk else 0.0

    def growth(self):
        """Yearly revenue growth: the last 90 days against the same 90 days a year before. A company younger than
        that has no such record: the last 90 days against the 90 days before, squared (a quarter's growth is noisy
        and seasonal, so investors do not compound it over a whole year). Clipped to 0.3 to 3."""
        rr = self.run_rate()
        if len(self.daily) >= 365 + 90:
            prev, power = self.run_rate(back=365), 1.0
        else:
            prev, power = self.run_rate(back=90), 2.0
        if prev <= 0:
            return 1.5 if rr > 0 else 1.0
        return min(3.0, max(0.3, (rr / prev) ** power))

    def value_now(self):
        """What investors start from, in two parts: net cash, which they pay for at face value, and the business.
        They price the business at the larger of the grading formula today (stock at liquidation value plus 3 times
        the yearly pace of the last 90 days' operating profit) and a multiple of the yearly revenue run rate that
        rises with growth. Consumer investors price revenue and growth, not only profit, so a brand that grows but
        does not yet make money can raise; a shrinking one gets a low multiple. The multiple's level is set so
        that the price is about what such a company turns out to be worth by the grading formula
        (REVENUE_MULTIPLE). Returns (net cash, business, growth)."""
        stock = sum(ln.stock_value for ln in self.lines.values())
        pace = sum(self.op_profit[-90:]) * 365 / 90 if len(self.op_profit) >= 90 else 0.0
        net_cash = self.cash + self.receivable + sum(a for _, a in self.wh_receivable) - self.credit_used - \
            self.venture_debt
        for m in self.abroad.values():
            stock += sum(ln.stock_value for ln in m.lines.values())
            net_cash += m.receivable
        g = self.growth()
        profit_based = RULES["liquidation"] * stock + RULES["profit_multiple"] * max(0.0, pace)
        revenue_based = REVENUE_MULTIPLE * g ** GROWTH_POWER * self.run_rate()
        return net_cash, max(profit_based, revenue_based), g

    def venture_terms(self):
        rr = self.run_rate()
        if self.venture_debt > 0 or rr < RULES["venture_debt_min_run_rate"]:
            return None
        amount = round(min(RULES["venture_debt_max"], RULES["venture_debt_share"] * rr), -3)
        val = self.lender_valuation()
        return {"max_amount": amount, "apr": RULES["venture_debt_apr"], "warrant_coverage": round(self.debt_coverage, 4),
                "valuation": round(val, -3), "warrant_share": round(self.warrant_share(amount, val), 4)}

    def lender_valuation(self):
        """The valuation the lender's warrants are priced at: the last round's post-money valuation, or else the
        company's value before investors' mood (value_now)."""
        if self.last_post_money:
            return self.last_post_money
        net_cash, business, _ = self.value_now()
        return max(1.0, net_cash + business)

    def warrant_share(self, amount, val):
        """Share of the company the lender's warrants take: coverage times the loan, over the valuation (at most
        25%)."""
        return min(0.25, self.debt_coverage * amount / max(1.0, val))

    def raise_money(self, kind, amount):
        a = _num(amount)
        if kind == "equity":
            o = self.funding
            if not o or o["expires"] < self.day or a is None or not 0 < a <= o["max_amount"]:
                self.note(f"raise refused: no open term sheet, or {amount!r} is above its max_amount")
                return
            fee = a * RULES["equity_fee"]
            self.cash += a - fee
            self.ledger["financing_fees"] += fee
            keep = o["pre_money"] / (o["pre_money"] + a)
            self.ownership *= keep
            self.dilute_rounds(keep)
            self.treasury.rounds.append({"day": self.day, "amount": a, "share": a / (o["pre_money"] + a)})
            self.last_post_money = o["pre_money"] + a
            self.equity_raised += a
            self.funding = None
            self.events.append({"day": self.day, "text": f"raised ${a:,.0f} at a ${o['pre_money']:,.0f} valuation"})
        elif kind == "venture_debt":
            t = self.venture_terms()
            if t is None or a is None or not 0 < a <= t["max_amount"]:
                self.note(f"raise refused: venture debt is not offered now, or {amount!r} is above its max_amount")
                return
            share = self.warrant_share(a, self.lender_valuation())
            self.cash += a
            self.venture_debt = a
            self.ownership *= 1 - share
            self.dilute_rounds(1 - share)
            self.events.append({"day": self.day, "text": f"borrowed ${a:,.0f} of venture debt"})
        else:
            self.note(f"raise refused: kind {kind!r} must be equity or venture_debt")

    def dilute_rounds(self, keep):
        for r in getattr(self.treasury, "rounds", []):
            r["share"] *= keep

    def team_share_of(self, company):
        """The team's part of a company value. Investors' shares carry the usual venture terms, a 1x non-participating
        liquidation preference: each round takes the larger of the money it put in and its share of the company, and
        the team and the lender's warrants share what is left. So money raised and kept as cash goes back to the
        investors, and a high price helps the team only when the company ends up worth more than the investors'
        money. Returns (the team's value, the preferences paid ahead of the shares)."""
        rounds = getattr(self.treasury, "rounds", [])
        if not rounds or company <= 0:
            return company * self.ownership, 0.0
        prefer = list(range(len(rounds)))      # rounds that take their money back; the others convert to shares
        while True:
            pref = sum(rounds[i]["amount"] for i in prefer)
            rest = company - pref
            common = 1.0 - sum(rounds[i]["share"] for i in prefer)
            if rest <= 0:
                return 0.0, company
            convert = [i for i in prefer if rounds[i]["share"] / common * rest > rounds[i]["amount"]]
            if not convert:
                return self.ownership / common * rest, pref
            prefer = [i for i in prefer if i not in convert]

    def accept_offer(self, oid):
        o = next((x for x in self.offers if x["id"] == oid), None)
        if o is None or o["expires"] < self.day:
            self.note(f"accept refused: {oid!r} is not an open offer")
            return
        ln = self.lines.get(o["product"])
        if ln is None or not ln.active:
            self.note(f"accept refused: {o['product']} is not selling")
            return
        if sum(1 for c in self.contracts if c["status"] == "running") >= RULES["wholesale_max_contracts"]:
            self.note(f"accept refused: {RULES['wholesale_max_contracts']} contracts are already running")
            return
        self.offers.remove(o)
        c = dict(o, status="running", deliveries=0, next_delivery=self.day + 30, strikes=0, last=[], stock=0,
                 shelf_sales=0, shelf_hist=[])
        c.pop("expires")
        self.contracts.append(c)
        ln.contract_on = True
        self.events.append({"day": self.day, "text": f"signed {o['chain']} for {o['units_per_month']} units of "
                                                     f"{o['product']} a month at ${o['price']}"})

    CHAINS = ("Hartwell Stores", "Brightmart", "Norland Home", "Corner & Co", "Peakline Outfitters", "Valuway")

    def step_levers(self):
        """Development results, chains' offers and deliveries, invoices paid, investors' term sheets. Returns the
        day's operating profit from them. These happen after the software's step, so their news is dated the next
        day, when the software first sees it."""
        d, profit = self.day, 0.0
        if self.w.get("check_news") and d == 2 and self.lines:
            self.check_items()
        for ln in self.lines.values():
            if ln.rnd is not None and d >= ln.rnd["done"]:
                ok = self.luck(ln.pid, "development").random() < ln.t["rnd"]["chance"]
                ln.rnd = None
                if ok:
                    ln.improve()
                    for m in self.abroad.values():
                        x = m.lines.get(ln.pid)
                        if x is not None and not x.improved:
                            x.improve()
                    text = f"The improved {ln.pid} is ready and sells from now on. Its supplier prices are " \
                           f"{ln.rnd_cost_mult:.2f} times the old ones."
                else:
                    text = f"The development of an improved {ln.pid} did not work. Nothing changes; a new project " \
                           f"may be started."
                self.news.append({"day": d + 1, "kind": "development", "product": ln.pid, "worked": ok, "text": text})
                self.events.append({"day": d, "text": text})
        # chains' offers, once a month
        if d % 30 == 15 and d >= 90:
            self.offers = [o for o in self.offers if o["expires"] >= d]
            for ln in self.lines.values():
                wt = ln.t["wholesale"]
                if not ln.active or ln.n_reviews < 10 or ln.rating < 3.8:
                    continue
                if self.luck(ln.pid, "chains").random() >= min(0.6, 0.2 * wt["appetite"]):
                    continue
                month_units = sum(ln.sold["site"][-30:]) + sum(ln.sold["marketplace"][-30:])
                units = int(min(40_000, max(300, round(month_units * self.luck(ln.pid, "chains").uniform(0.4, 1.6) * wt["appetite"], -2))))
                self.wh_seq += 1
                self.offers.append({"id": f"W{self.wh_seq}", "chain": self.CHAINS[int(self.luck(ln.pid, "chains").integers(len(self.CHAINS)))],
                                    "product": ln.pid, "units_per_month": units,
                                    "price": round(ln.typ * wt["price"] * self.luck(ln.pid, "chains").uniform(0.93, 1.07), 2),
                                    "months": int(self.luck(ln.pid, "chains").choice([6, 12])),
                                    "pay_days": int(self.luck(ln.pid, "chains").choice(RULES["wholesale_pay_days_choices"],
                                                                          p=[0.2, 0.4, 0.4])),
                                    "expires": d + 14})
        # deliveries, once a month: the chain first sells from its shelf, then orders what refills the shelf to
        # units_per_month (the first order fills it), and may drop a product that sells slowly
        for c in self.contracts:
            if c["status"] != "running" or d < c["next_delivery"]:
                continue
            ln = self.lines[c["product"]]
            wt = ln.t["wholesale"]
            if c["deliveries"]:
                dem_mult, _ = self.macro()
                mean = c["units_per_month"] * wt["sell"] * self.season(ln.p["category"], _date(d).month) * dem_mult * \
                    math.exp(RATING_UTILITY * (ln.rating - RATING_PRIOR[1]))
                c["shelf_sales"] = min(c["stock"], int(self.luck(c["id"], "chain-sales").poisson(mean)))
                c["stock"] -= c["shelf_sales"]
                c["shelf_hist"] = (c["shelf_hist"] + [c["shelf_sales"]])[-2:]
            want = max(0, c["units_per_month"] - c["stock"])
            n = min(want, ln.on_hand) if ln.active else 0
            uc = ln.stock_value / ln.on_hand if ln.on_hand else 0.0
            short = want - n
            if n:
                cogs = n * uc
                ln.on_hand -= n
                ln.stock_value = max(0.0, ln.stock_value - cogs)
                c["stock"] += n
                ship = n * product_fees(ln.pid)["wholesale_ship"]
                invoice = n * c["price"]
                cb = invoice * wt["chargeback"]
                self.wh_receivable.append((d + c["pay_days"], invoice - cb))
                self.ledger["revenue"] += invoice
                self.ledger["chargebacks"] += cb
                self.ledger["cogs"] += cogs
                self.spend(ship, "fulfillment")
                ln.pnl["revenue"] += invoice
                ln.pnl["fees"] += cb
                ln.pnl["cogs"] += cogs
                ln.pnl["fulfillment"] += ship
                profit += invoice - cb - cogs - ship
                m_eff = ln.t["m"] * ln.m_mult
                ln.aware = min(m_eff, ln.aware + wt["exposure"] * n)
            ln.yesterday["wholesale"] = {"contract": c["id"], "units": n, "short": short, "invoiced": round(n * c["price"], 2),
                                         "shelf_sales": c["shelf_sales"]}
            c["deliveries"] += 1
            c["last"] = (c["last"] + [n])[-2:]
            c["next_delivery"] = d + 30
            why = ""
            if short > 0.1 * want:
                pen = RULES["wholesale_penalty"] * short * c["price"]
                self.spend(pen, "chargebacks")
                profit -= pen
                c["strikes"] += 1
                if c["strikes"] >= 2:
                    c["status"] = "cancelled"
                    why = "after two short deliveries"
            # delisting: about 2% a month on average, more for a product that sells slowly (a quarter of new
            # consumer products are gone from shelves within a year)
            if c["status"] == "running" and len(c["shelf_hist"]) == 2:
                through = sum(c["shelf_hist"]) / (2 * c["units_per_month"])
                if self.luck(c["id"], "delist").random() < 0.01 + 0.08 * max(0.0, 1 - through):
                    c["status"] = "dropped"
                    why = "because it sold too slowly" if through < 0.7 else "to make room for other products"
            if c["status"] == "running" and c["deliveries"] >= c["months"]:
                c["status"] = "ended"
            if c["status"] != "running":
                back = int(wt["unsold"] * c["stock"])
                if back:
                    refund = back * c["price"]
                    self.wh_receivable.append((d, -refund))
                    uc2 = ln.stock_value / ln.on_hand if ln.on_hand else landed(ln.pid)
                    ln.on_hand += back
                    ln.stock_value += back * uc2
                    self.ledger["refunds"] += refund
                    self.ledger["cogs"] -= back * uc2
                    ln.pnl["refunds"] += refund
                    ln.pnl["cogs"] -= back * uc2
                    profit += -refund + back * uc2
                c["stock"] = 0
                verb = {"ended": "ended", "cancelled": "was cancelled", "dropped": "was ended early by the chain"}[c["status"]]
                self.news.append({"day": d + 1, "kind": "wholesale", "text": f"Contract {c['id']} with {c['chain']} "
                                  f"{verb}{' ' + why if why else ''}; it sent back {back} unsold units of {c['product']}."})
            ln.contract_on = any(x["status"] == "running" and x["product"] == ln.pid for x in self.contracts)
        # invoices paid
        due = [a for t, a in self.wh_receivable if t <= d]
        if due:
            self.cash += sum(due)
            self.wh_receivable = [(t, a) for t, a in self.wh_receivable if t > d]
        # investors
        if d >= RULES["equity_first_day"] and d % 30 == 0 and d - self.last_term_sheet >= RULES["equity_every_days"]:
            rr = self.run_rate()
            if rr >= RULES["equity_min_run_rate"]:
                net_cash, business, g = self.value_now()
                # fewer investors look at a shrinking company, or at any company in a recession
                chance = (1.0 if g >= 0.9 else 0.2) * (0.35 if self.in_recession() else 1.0)
                if self.luck("investors").random() >= chance:
                    return profit
                mood = self.truth["investor_premium"] * self.mood * (0.6 if self.in_recession() else 1.0)
                pre = round(max(0.0, net_cash + max(0.0, business) * mood), -4)
                if pre < 500_000:
                    return profit
                self.funding = {"pre_money": pre,
                                "max_amount": round(min(RULES["equity_max_share"] * pre, RULES["equity_max"]), -4),
                                "fee": RULES["equity_fee"], "expires": d + 30}
                self.last_term_sheet = d
                self.news.append({"day": d + 1, "kind": "term_sheet", "text": f"Investors offer to buy new shares at a "
                                  f"${pre:,.0f} valuation before the money, up to ${self.funding['max_amount']:,.0f}, "
                                  f"open for 30 days."})
        return profit

    def check_items(self):
        """The plan review's check market: one of each lever item on day 2, so the software meets them once."""
        pid = next(iter(self.lines))
        ln = self.lines[pid]
        self.wh_seq += 1
        self.offers.append({"id": f"W{self.wh_seq}", "chain": self.CHAINS[0], "product": pid, "units_per_month": 300,
                            "price": round(0.45 * ln.typ, 2), "months": 6, "pay_days": RULES["wholesale_pay_days"],
                            "expires": self.day + 14})
        self.funding = {"pre_money": 3_000_000.0, "max_amount": 1_000_000.0, "fee": RULES["equity_fee"],
                        "expires": self.day + 30}
        for kind, text in (("development", f"The development of an improved {pid} did not work. Nothing changes; a "
                                            f"new project may be started."),
                           ("term_sheet", "Investors offer to buy new shares at a $3,000,000 valuation before the money, "
                                          "up to $1,000,000, open for 30 days."),
                           ("wholesale", f"Contract W0 with {self.CHAINS[0]} ended; it sent back 0 unsold units of {pid}.")):
            item = {"day": self.day + 1, "kind": kind, "text": text}
            if kind == "development":
                item.update(product=pid, worked=False)
            self.news.append(item)

    def step_subscriptions(self, ln, new_site_buyers, unit_cost, y):
        """Subscribers reorder on schedule at the discounted price, from what stock is left today, and some
        leave. Returns the day's operating profit from them."""
        st = ln.t["subs"]
        disc = ln.subs_discount
        if not ln.active or disc <= 0 or "site" not in ln.channels:
            return 0.0
        take = min(0.7, st["take"] * (disc / 0.1) ** st["elastic"])
        joined = new_site_buyers * take
        if joined > 0:
            if ln.sub_cohorts and self.day - ln.sub_cohorts[-1][0] < 30:
                ln.sub_cohorts[-1][1] += joined
            else:
                ln.sub_cohorts.append([self.day, joined])
        ln.subs += joined
        ln.subs_due += ln.subs * st["rate"] / 365
        orders = int(self.luck(ln.pid, "subscriptions").poisson(ln.subs_due)) if ln.subs_due > 0 else 0
        ln.subs_due = max(0.0, ln.subs_due - orders)
        n = min(orders, ln.on_hand)
        price = ln.price["site"] * (1 - disc)
        gross = n * price
        rev = gross * self.net
        fees = gross * RULES["card_fee_pct"] + n * RULES["card_fee_fixed"]
        fulfil = n * self.fees(ln.pid)["site_fulfill"]
        cogs = n * unit_cost
        ln.on_hand -= n
        ln.stock_value = max(0.0, ln.stock_value - cogs)
        self.cash += rev
        self.spend(fees, "fees")
        self.spend(fulfil, "fulfillment")
        self.ledger["revenue"] += rev
        self.ledger["cogs"] += cogs
        for k, v in (("revenue", rev), ("fees", fees), ("fulfillment", fulfil), ("cogs", cogs)):
            ln.pnl[k] += v
        if n:
            ret = int(self.luck(ln.pid, "subscriptions").binomial(n, min(1.0, ln.t["returns"] + ln.stock_defect)))
            if ret:
                ln.returns.append((self.day + int(self.luck(ln.pid, "subscriptions").integers(5, 26)), ret, price, "site",
                                   int(self.luck(ln.pid, "subscriptions").binomial(ret, min(1.0, ln.stock_defect / max(1e-9, ln.t["returns"] + ln.stock_defect))))))
        # leaving: at each renewal a share leaves that falls with tenure (most at the first two renewals, half way
        # between at the third and fourth, the settled rate from the fifth), more when the price rose above what
        # they are used to, and a third of those whose order could not be filled
        rise = max(0.0, (self.local(ln, "site") - ln.ref["site"]) / ln.ref["site"])
        per_day = st["rate"] / 365
        left = 0.0
        for co in ln.sub_cohorts:
            k = int((self.day - co[0]) * per_day) + 1          # the renewal they face next
            h = st["churn_first"] if k <= 2 else (0.5 * (st["churn_first"] + st["churn"]) if k <= 4 else st["churn"])
            x = co[1] * min(1.0, h * per_day * (1 + 3 * rise))
            co[1] -= x
            left += x
        unfilled = 0.3 * (orders - n)
        if unfilled > 0 and ln.subs - left > 0:
            f = max(0.0, 1 - unfilled / (ln.subs - left))
            for co in ln.sub_cohorts:
                co[1] *= f
            left += min(unfilled, ln.subs - left)
        ln.sub_cohorts = [co for co in ln.sub_cohorts if co[1] > 1e-6]
        ln.subs = sum(co[1] for co in ln.sub_cohorts)
        y["subscription"] = {"subscribers": int(ln.subs), "orders": n, "unfilled": orders - n,
                             "revenue": round(rev, 2), "left": round(left, 1)}
        return rev - fees - fulfil - cogs

    def step_cart(self):
        """Own-site orders often hold more than one item: each site order of a line adds items from the company's
        other site lines that have stock (CART_ADDON in all with max_products site lines, fewer with fewer lines; a
        line priced above its typical price is added less often). Packed in the same parcel. Returns the day's
        operating profit from them."""
        site = [ln for ln in self.lines.values() if ln.active and "site" in ln.channels]
        if len(site) < 2:
            return 0.0
        scale = CART_ADDON / (RULES["max_products"] - 1)
        adds = {}
        for x in site:
            orders = (x.yesterday.get("site") or {}).get("orders", 0)
            if not orders:
                continue
            for y_ in site:
                if y_ is x or y_.on_hand <= 0:
                    continue
                appeal = min(1.5, (y_.typ / max(0.01, self.local(y_, "site"))) ** y_.t["eps"])
                k = int(self.luck(x.pid, "cart", y_.pid).binomial(orders, min(0.5, scale * appeal)))
                if k:
                    adds[y_.pid] = adds.get(y_.pid, 0) + k
        profit = 0.0
        for pid, k in adds.items():
            y_ = self.lines[pid]
            n = min(k, y_.on_hand)
            y_.lost += k - n
            if not n:
                continue
            price = y_.price["site"]
            uc = y_.stock_value / y_.on_hand
            gross, cogs = n * price, n * uc
            rev = gross * self.net
            fees = gross * RULES["card_fee_pct"]
            pick = n * CART_PICK * self.fees(pid)["site_fulfill"]
            y_.on_hand -= n
            y_.stock_value = max(0.0, y_.stock_value - cogs)
            self.cash += rev
            self.spend(fees, "fees")
            self.spend(pick, "fulfillment")
            self.ledger["revenue"] += rev
            self.ledger["cogs"] += cogs
            for key, v in (("revenue", rev), ("fees", fees), ("fulfillment", pick), ("cogs", cogs)):
                y_.pnl[key] += v
            ret = int(self.luck(pid, "cart-returns").binomial(n, min(1.0, y_.t["returns"] + y_.stock_defect)))
            if ret:
                bad = int(self.luck(pid, "cart-returns").binomial(ret, min(1.0, y_.stock_defect / max(1e-9, y_.t["returns"] + y_.stock_defect))))
                y_.returns.append((self.day + int(self.luck(pid, "cart-returns").integers(5, 26)), ret, price, "site", bad))
            y_.buyers += n
            y_.owned += n
            y_.aware = min(y_.t["m"] * y_.m_mult, y_.aware + n)
            ys = y_.yesterday.setdefault("site", {})
            ys["addon_units"] = n
            ys["units"] = ys.get("units", 0) + n
            ys["revenue"] = round(ys.get("revenue", 0.0) + rev, 2)
            profit += rev - fees - pick - cogs
        return profit

    def step_bundles(self):
        """Bundles on the own site: some site orders of one product add the other at the bundle discount. Some of
        those would have bought both anyway and only pay less. Returns the day's operating profit."""
        profit = 0.0
        taken = {}          # add-on units per order so far today, by product: at most one add-on per order in all
        for b in self.bundles:
            a_id, b_id = b["products"]
            la, lb = self.lines.get(a_id), self.lines.get(b_id)
            if not (la and lb and la.active and lb.active and "site" in la.channels and "site" in lb.channels):
                continue
            bt = self.truth["bundles"][f"{a_id}+{b_id}"]
            disc = b["discount"]
            for x, y_ in ((la, lb), (lb, la)):
                site = x.yesterday.get("site") or {}
                orders = site.get("orders", 0)
                if not orders:
                    continue
                # a dearer add-on is added less often: the take-up falls with its bundle price against the typical price
                py_net = self.local(y_, "site") * (1 - disc)
                rate = bt["attach"] * (disc / 0.15) ** 0.7 * min(1.5, (0.85 * y_.typ / py_net) ** (2 * y_.t["eps"]))
                rate = min(0.5, rate, 1.0 - taken.get(x.pid, 0.0))
                taken[x.pid] = taken.get(x.pid, 0.0) + rate
                k = int(self.luck(x.pid, "bundle", y_.pid).binomial(orders, max(0.0, rate)))
                anyway = int(self.luck(x.pid, "bundle", y_.pid).binomial(k, bt["overlap"]))
                extra = min(k - anyway, y_.on_hand)
                px, py = x.price["site"], y_.price["site"]
                uc = y_.stock_value / y_.on_hand if y_.on_hand else 0.0
                rev = (extra * (py * (1 - disc) - px * disc) - anyway * (px + py) * disc) * self.net
                cogs = extra * uc
                fees = max(0.0, extra * py * (1 - disc)) * RULES["card_fee_pct"]
                pick = extra * 0.3 * self.fees(y_.pid)["site_fulfill"]     # one more item in the same parcel
                self.spend(pick, "fulfillment")
                y_.pnl["fulfillment"] += pick
                profit -= pick
                if extra:
                    ret = int(self.luck(x.pid, "bundle", y_.pid).binomial(extra, min(1.0, y_.t["returns"] + y_.stock_defect)))
                    if ret:
                        bad = int(self.luck(x.pid, "bundle", y_.pid).binomial(ret, min(1.0, y_.stock_defect / max(1e-9, y_.t["returns"] + y_.stock_defect))))
                        y_.returns.append((self.day + int(self.luck(x.pid, "bundle", y_.pid).integers(5, 26)), ret, py * (1 - disc), "site", bad))
                y_.on_hand -= extra
                y_.stock_value = max(0.0, y_.stock_value - cogs)
                self.cash += rev
                self.spend(fees, "fees")
                self.ledger["revenue"] += rev
                self.ledger["cogs"] += cogs
                y_.pnl["revenue"] += rev
                y_.pnl["cogs"] += cogs
                y_.pnl["fees"] += fees
                profit += rev - cogs - fees
                site["bundle_orders"] = site.get("bundle_orders", 0) + k
                site["revenue"] = round(site.get("revenue", 0.0) + rev, 2)
        return profit

    # -- world events -------------------------------------------------------------------------------

    def apply_midrun(self):
        """The mid-run shock (worlds.midrun_shock): a lasting change from day 0, kept in the world's state so lines
        launched later get it too."""
        m = self.midrun
        if not m:
            return
        ws = self.wstate
        if m["kind"] == "fee":
            self.referral += m["size"]
        elif m["kind"] == "tariff":
            for c in m["categories"]:
                ws["cat_cost"][c] = ws["cat_cost"].get(c, 1.0) * (1 + m["size"])
        elif m["kind"] == "ads":
            ws["ads"] *= m["size"]
        elif m["kind"] == "big_rival":
            for pid, p in self.catalog.items():
                if p["category"] in m["categories"]:
                    ws["prod_rivals"].setdefault(pid, []).append(
                        {"name": f"{pid}-major", "kind": "learner", "speed": 0.2, "cost": p["price"] * 0.5,
                         "appeal": 0.2, "rating": 4.5, "price": p["price"] * m["size"], "visibility": 2.0,
                         "own_demand": 1.5})
        for ln in self.lines.values():
            self.world_effects(ln)
        self.events.append({"day": self.day, "text": "market change announced during the run: " + m["text"]})

    def world_effects(self, ln):
        """Give a line launched during the world every change that already happened in its market."""
        ws = self.wstate
        cat, pid = ln.p["category"], ln.pid
        ln.cost_mult *= ws["cat_cost"].get(cat, 1.0)
        ln.a_cost_mult *= ws["prod_cost"].get(pid, 1.0)
        ln.a_lead_add += ws["prod_lead"].get(pid, 0)
        ln.t["beta"] *= ws["cat_beta"].get(cat, 1.0)
        ln.t["cpm"] *= ws["ads"]
        ln.t["cpc_mp"] *= ws["ads"] ** 0.5
        ln.m_mult *= ws["prod_m"].get(pid, 1.0)
        for spec in ws["prod_rivals"].get(pid, []):
            ln.extra_rivals.append(Rival(spec, ln.typ))

    def world_events(self):
        d = self.day
        for s in self.w["shocks"]:
            if d == max(0, s["day"] - 30):
                self.news.append({"day": d, "kind": s["kind"], "starts": s["day"], "text": self.shock_text(s)})
        month, mday = _date(d).month, _date(d).day
        if (month == 10 and mday == 1) or (self.w.get("check_news") and d == 1):
            self.news.append({"day": d, "kind": "holiday_ads", "text": "Ad prices usually rise in November and "
                              "December. Ad platforms expect the same this year."})
        for s in self.w["shocks"]:
            if d != s["day"]:
                continue
            k = s["kind"]
            ws = self.wstate
            if k == "tariff":
                ws["cat_cost"][s["target"]] = ws["cat_cost"].get(s["target"], 1.0) * (1 + s["size"])
                for ln in self.lines.values():
                    if ln.p["category"] == s["target"]:
                        ln.cost_mult *= 1 + s["size"]
            elif k == "supplier_closure":       # the standard supplier (A) closes; B and C are not affected
                ws["prod_cost"][s["target"]] = ws["prod_cost"].get(s["target"], 1.0) * 1.12
                ws["prod_lead"][s["target"]] = ws["prod_lead"].get(s["target"], 0) + 15
                ln = self.lines.get(s["target"])
                if ln:
                    for po in ln.pos:
                        if po["status"] == "ordered" and po.get("supplier", "A") == "A":
                            po["ship"] += 60
                            po["arrive"] += 60
                    ln.a_cost_mult *= 1.12
                    ln.a_lead_add += 15
                for m in self.abroad.values():
                    m.close_supplier(s)
            elif k == "port_delay":
                for ln in self.lines.values():
                    for po in ln.pos:
                        if po["status"] in ("ordered", "shipped") and d <= po["arrive"] <= d + s["length"]:
                            po["arrive"] += s["size"]
            elif k == "platform_fee":
                self.referral += s["size"]
            self.events.append({"day": d, "text": self.shock_text(s)})
        for dr in self.w["drifts"]:
            if d != dr["day"]:
                continue
            k = dr["kind"]
            ws = self.wstate
            if k in ("market_shrink", "market_grow"):
                ws["prod_m"][dr["target"]] = ws["prod_m"].get(dr["target"], 1.0) * dr["size"]
                ln = self.lines.get(dr["target"])
                if ln:
                    ln.m_mult *= dr["size"]
            elif k == "price_sensitive":
                ws["cat_beta"][dr["target"]] = ws["cat_beta"].get(dr["target"], 1.0) * dr["size"]
                for ln in self.lines.values():
                    if ln.p["category"] == dr["target"]:
                        ln.t["beta"] *= dr["size"]
            elif k == "ads_fatigue":
                ws["ads"] *= dr["size"]
                for ln in self.lines.values():
                    ln.t["cpm"] *= dr["size"]
                    ln.t["cpc_mp"] *= dr["size"] ** 0.5
            elif k == "rival_entry":
                typ = self.catalog[dr["target"]]["price"]
                spec = {"name": f"{dr['target']}-new", "kind": "learner", "speed": 0.2,
                        "cost": typ * 0.45, "appeal": 0.1, "rating": 4.4,
                        "price": typ * dr["size"], "visibility": 1.2, "own_demand": 0.8}
                ws["prod_rivals"].setdefault(dr["target"], []).append(spec)
                ln = self.lines.get(dr["target"])
                if ln:
                    ln.extra_rivals.append(Rival(spec, ln.typ))
            self.events.append({"day": d, "text": self.drift_text(dr), "hidden": True})

    @staticmethod
    def shock_text(s):
        k = s["kind"]
        if k == "tariff":
            return f"A tariff of {s['size']:.0%} on the supplier price of {s['target']} goods applies to orders " \
                   f"placed from day {s['day']}."
        if k == "supplier_closure":
            return f"The standard supplier (A) of {s['target']} closes on day {s['day']}. Its open orders move to a " \
                   f"backup, 60 days late, and new orders from A cost 12% more and take 15 days longer. Suppliers B " \
                   f"and C are not affected."
        if k == "port_delay":
            return f"Port congestion from day {s['day']} for {s['length']} days: shipments arriving then are " \
                   f"{s['size']} days late."
        if k == "platform_fee":
            return f"The marketplace raises its referral fee by {s['size']:.0%} from day {s['day']}."
        return k

    @staticmethod
    def drift_text(d):
        k = d["kind"]
        return {"market_shrink": f"the market for {d['target']} shrank to {d['size']:.0%}",
                "market_grow": f"the market for {d['target']} grew to {d['size']:.0%}",
                "price_sensitive": f"{d['target']} shoppers became {d['size']:.0%} as sensitive to price",
                "ads_fatigue": f"ads became {d['size']:.0%} as costly",
                "rival_entry": f"a new rival entered {d['target']} at {d['size']:.0%} of the typical price"}[k]

    # -- demand -----------------------------------------------------------------------------------

    def utilities(self, ln, ch, beta_mult):
        """Our utility and the rivals' utilities for a shopper on channel ch, centered on the typical price."""
        beta = ln.t["beta"] * beta_mult
        p = self.local(ln, ch)
        a = ln.a_site if ch == "site" else ln.a_mp
        loss = ln.t["loss"]
        if self.w["deal"]:
            loss *= self.w["deal"]["loss_mult"]
        ref = ln.ref[ch]
        gap = p - ref
        # a price below the reference helps less than a price above it hurts (by the product's loss aversion), and at
        # most as much as a 30% discount on the typical price
        ref_term = -loss * beta * gap if gap > 0 else loss / ln.t["loss_aversion"] * beta * min(-gap, 0.3 * ln.typ)
        u = a + RATING_UTILITY * (ln.rating - RATING_PRIOR[1]) - beta * (p - ln.typ) + ref_term
        rivals = [a + r.appeal + RATING_UTILITY * (r.rating - RATING_PRIOR[1]) - beta * (r.price - ln.typ)
                  for r in ln.all_rivals()]
        return u, rivals

    @staticmethod
    def few_reviews(ln):
        """Utility lost by a listing with few reviews: shoppers trust a product with no reviews much less (conversion
        about half), and most of the gap closes within the first 5 to 10 reviews."""
        lo, k = FEW_REVIEWS
        return math.log(1 - (1 - lo) * math.exp(-ln.n_reviews / k))

    def loyal_choice(self, ln, ch, beta_mult):
        """Chance that a past buyer who needs the product again buys ours: ours, a rival's (they know the product
        type now) or none, with the same price, reference price and rating terms as a new shopper, calibrated so
        that the chance is LOYAL_KEEP at the typical price against CALIB_RIVALS rivals at the typical price."""
        u, rv = self.utilities(ln, ch, beta_mult)
        a = ln.a_site if ch == "site" else ln.a_mp
        lose = (1 - LOYAL_KEEP) / 2
        k_you, k_r = math.log(LOYAL_KEEP / lose), math.log(1 / CALIB_RIVALS)
        xs = [u - a + k_you] + [x - a + k_r for x in rv]
        mx = max(xs + [0.0])
        ex = [math.exp(x - mx) for x in xs]
        return ex[0] / (sum(ex) + math.exp(-mx))

    def choice(self, u, rivals, rival_w, extra_you=0.0, extra_rivals=None):
        """Chance that a shopper picks our product: logit with a no purchase option of utility 0, nested
        when the world's nest parameter is below 1."""
        lam = self.w["nest"]
        er = extra_rivals or [0.0] * len(rivals)
        xs = [(u + extra_you) / lam] + [(ur + e + math.log(rival_w)) / lam for ur, e in zip(rivals, er)]
        mx = max(xs)
        ex = [math.exp(x - mx) for x in xs]
        inside = sum(ex)
        iv = mx + math.log(inside)          # log sum of the inside options, over lambda
        p_nest = 1 / (1 + math.exp(-lam * iv))
        return p_nest * ex[0] / inside, [p_nest * e / inside for e in ex[1:]]

    def trend(self, pid):
        """The product's life cycle: market size today over market size at the start."""
        tr = self.w["trends"][pid]
        f = math.exp(tr["growth"] * self.day / 365)
        fad = tr["fad"]
        if fad:
            if self.day <= fad["peak"]:
                f *= 1 + 0.8 * self.day / fad["peak"]
            else:
                f *= 1.8 * math.exp(-fad["fall"] * (self.day - fad["peak"]) / 365)
        return f

    def demand(self, ln, month):
        """Expected orders and visits today, by channel, before stock."""
        if not self.selling():
            return {}
        t = ln.t
        dem_mult, beta_mult = self.macro()
        season = self.season(ln.p["category"], month)
        m_eff = t["m"] * ln.m_mult * self.trend(ln.pid)
        out = {}
        cpm_m, ctr_m = self.cpm_mult(month)
        # a chain that sells the product takes some of the people who would have bought it from us, and being on
        # shelves makes the brand better known
        own = (1 - t["wholesale"]["cannibal"]) * (1 + t["wholesale"]["halo"]) if ln.contract_on else 1.0
        few = self.few_reviews(ln)
        # Past buyers who need the product again (the yearly repeat rate per buyer at the typical price). Own-site
        # buyers are on the brand's list: they buy again more often (OWNED_LIFT) and on the own site, at no ad cost.
        # Marketplace buyers belong to the marketplace: they buy again there (paying its fees), and a few on the own
        # site (MP_TO_SITE). Buyers whose channel the line is no longer on look for it on the other one.
        rate = t["repeat"] / LOYAL_KEEP / 365 * season * dem_mult if ln.active else 0.0
        owned = max(0.0, min(ln.owned, ln.buyers) - ln.subs)
        mp_buyers = max(0.0, ln.buyers - ln.owned)
        site_on = "site" in ln.channels
        mp_on = "marketplace" in ln.channels and ln.suspended_until <= self.day
        need_site = owned * OWNED_LIFT * rate if site_on else 0.0
        switch = mp_buyers * MP_TO_SITE * rate if site_on else 0.0
        need_mp = 0.0
        if mp_on:
            need_mp = mp_buyers * (1 - MP_TO_SITE if site_on else 1.0) * rate + (0.0 if site_on else owned * rate)
        # The own site.
        if "site" in ln.channels and ln.active:
            e = self.effective_spend(ln.spend_hist["social"])
            if e is None:
                e = self._adstock(ln, "social", t["adstock"])
            cpc0 = t["cpm"] / (1000 * t["ctr"]) * cpm_m / ctr_m * self.w["ad_cost_mult"] * self.cost_fx
            clicks = e / (cpc0 * (1 + e / t["ad_half"]) ** t["ad_kappa"]) if e > 0 else 0.0
            if self.w["ad_delay"]:
                clicks *= 0.7
            organic = ln.aware * t["visit_rate"] * season * dem_mult
            u, rv = self.utilities(ln, "site", beta_mult)
            p_new, _ = self.choice(u + few, rv, RIVAL_SITE_WEIGHT)
            keep = self.loyal_choice(ln, "site", beta_mult)
            repeat = (need_site + switch) * keep
            out["site"] = {"clicks": clicks, "sessions": clicks * 0.92 + organic + repeat / (RETURNING_CONV * t["conv_site"]),
                           "orders": ((clicks * 0.92 + organic) * p_new + repeat) * own,
                           "repeat_orders": repeat * own, "switch_orders": switch * keep * own, "conv": p_new}
        # The marketplace.
        if "marketplace" in ln.channels and ln.active and ln.suspended_until <= self.day:
            # Shoppers who need this kind of product today. People who bought ours mostly do not come back
            # to search (they buy again as repeat buyers, above).
            shoppers = m_eff * t["search_rate"] * season * dem_mult * max(0.2, 1 - ln.buyers / m_eff)
            e = self.effective_spend(ln.spend_hist["sponsored"])
            if e is None:
                e = self._adstock(ln, "sponsored", SPONSORED_ADSTOCK)
            # Visibility in search: sales rank (sales of the last 30 days), rating once there are reviews,
            # and sponsored placement. It sets how many shoppers look at our listing.
            boost = 1.0 * e / (e + t["mp_half"]) if e > 0 else 0.0
            vel_scale = max(1.0, t["m"] * t["search_rate"] * VEL_SCALE)
            vel = sum(ln.mp_sales[-30:]) / 30
            trust = min(1.0, ln.n_reviews / 20)
            v_you = math.log(0.35 + vel / vel_scale) + MP_RATING_WEIGHT * (ln.rating - RATING_PRIOR[1]) * trust + boost
            rivals = ln.all_rivals()
            v_r = [math.log(r.visibility) + MP_RATING_WEIGHT * (r.rating - RATING_PRIOR[1]) for r in rivals]
            vis = math.exp(v_you) / (math.exp(v_you) + sum(math.exp(v) for v in v_r))
            views = shoppers * vis * MP_VIEWS
            # A shopper who looks at our listing picks ours, a rival's, or nothing.
            u, rv = self.utilities(ln, "marketplace", beta_mult)
            conv, p_r = self.choice(u + few, rv, 1.0)
            cpc = t["cpc_mp"] * (1 + e / t["mp_half"]) ** t["ad_kappa"] * self.w["ad_cost_mult"] * self.cost_fx
            sp_clicks = e / cpc if e > 0 else 0.0
            repeat = need_mp * self.loyal_choice(ln, "marketplace", beta_mult) if need_mp > 0 else 0.0
            out["marketplace"] = {"shoppers": shoppers,
                                  "sessions": views + sp_clicks + repeat / (RETURNING_CONV * t["conv_mp"]),
                                  "orders": ((views + sp_clicks) * conv + repeat) * own, "clicks": sp_clicks,
                                  "repeat_orders": repeat * own, "conv": conv, "rival_share": p_r}
        return out

    def _adstock(self, ln, ch, theta):
        """Normalized geometric adstock: the same spend every day gives that spend back."""
        hist = ln.spend_hist[ch]
        e, wsum = 0.0, 0.0
        w = 1.0
        for s in reversed(hist[-30:]):
            e += w * s
            wsum += w
            w *= theta
        full = (1 - theta ** 30) / (1 - theta)
        return e / full if hist else 0.0

    # -- one day ------------------------------------------------------------------------------------

    def step_supply(self):
        d = self.day
        for ln in self.lines.values():
            for po in ln.pos:
                if po["status"] == "ordered" and d >= po["ship"]:
                    fill = min(1.0, po.get("fill", ln.t["fill"]) + self.luck(ln.pid, "ship", po["id"]).normal(0, 0.03))
                    shipped = int(po["qty"] * max(0.5, fill))
                    po["shipped"] = shipped
                    balance = (1 - RULES["deposit"]) * shipped * po["unit"]
                    refund = RULES["deposit"] * (po["qty"] - shipped) * po["unit"]
                    self.cash += refund
                    self.ledger["inventory_paid"] -= refund
                    self.spend(balance, "inventory_paid")
                    po["status"] = "shipped"
                if po["status"] == "shipped" and d >= po["arrive"]:
                    n = ln.on_hand + po["shipped"]
                    if n > 0:
                        ln.stock_defect = (ln.on_hand * ln.stock_defect + po["shipped"] * po.get("defect", ln.stock_defect)) / n
                    ln.on_hand += po["shipped"]
                    ln.stock_value += po["shipped"] * po["unit"]
                    po["status"] = "arrived"
            ln.pos = [po for po in ln.pos if po["status"] != "arrived" or d - po["arrive"] < 1]

    def step_sales(self, month):
        d = self.day
        day_profit = 0.0
        for ln in self.lines.values():
            g_sales = self.luck(ln.pid, "sales")
            y = {"site": {}, "marketplace": {}, "ads": {}}
            live = self.selling()
            spend_social = ln.budget["social"] if ("site" in ln.channels and ln.active and live) else 0.0
            spend_sp = ln.budget["sponsored"] if ("marketplace" in ln.channels and ln.active
                                                  and ln.suspended_until <= d and live) else 0.0
            ln.spend_hist["social"].append(spend_social)
            ln.spend_hist["sponsored"].append(spend_sp)
            ln.spend_hist["social"] = ln.spend_hist["social"][-40:]
            ln.spend_hist["sponsored"] = ln.spend_hist["sponsored"][-40:]
            dem = self.demand(ln, month)
            # Orders, then units, then stock.
            want = {}
            for ch, x in dem.items():
                lam = x["orders"]
                if self.w["deal"]:
                    lam = self._deal(ln, ch, lam)
                orders = int(g_sales.poisson(max(0.0, lam)))
                extra = int(g_sales.poisson(orders * (ln.t["units_per_order"] - 1))) if orders else 0
                want[ch] = (orders, orders + extra)
            total = sum(u for _, u in want.values())
            avail = ln.on_hand
            share = 1.0 if total <= avail else (avail / total if total else 0.0)
            unit_cost = ln.stock_value / ln.on_hand if ln.on_hand else 0.0
            sold_units = 0
            site_new = 0.0
            for ch, (orders, units) in want.items():
                u_sold = units if share >= 1 else int(units * share)
                o_sold = orders if share >= 1 else min(orders, u_sold)
                if share < 1:
                    ln.lost += units - u_sold
                sold_units += u_sold
                price = ln.price[ch]
                gross = u_sold * price              # what shoppers paid, with VAT abroad
                rev = gross * self.net
                size = self.fees(ln.pid)
                if ch == "site":
                    fees = gross * RULES["card_fee_pct"] + o_sold * RULES["card_fee_fixed"]
                    fulfil = o_sold * size["site_fulfill"]
                    self.cash += rev
                else:
                    fees = gross * self.referral
                    fulfil = u_sold * size["mp_fulfill"]
                    self.receivable += rev
                    self.mp_pending.append((d + self.hold_days, rev))
                    ln.mp_sales.append(u_sold)
                self.ledger["revenue"] += rev
                ln.pnl["revenue"] += rev
                ln.pnl["fees"] += fees
                ln.pnl["fulfillment"] += fulfil
                self.spend(fees, "fees")
                self.spend(fulfil, "fulfillment")
                day_profit += rev - fees - fulfil
                # returns and reviews come later
                if u_sold:
                    defective = int(g_sales.binomial(u_sold, ln.stock_defect))
                    ret = defective + int(g_sales.binomial(u_sold - defective, ln.t["returns"]))
                    if ret:
                        ln.returns.append((d + int(g_sales.integers(5, 26)), ret, price, ch, defective))
                    n_rev = int(g_sales.binomial(u_sold, REVIEW_CHANCE))
                    n_bad = int(g_sales.binomial(n_rev, min(1.0, defective / u_sold * 3))) if defective else 0
                    for i in range(n_rev):
                        star = 1.0 + g_sales.random() if i < n_bad else \
                            min(5.0, max(1.0, g_sales.normal(ln.t["satisfaction"], 0.8)))
                        ln.reviews.append((d + 10, star))
                x = dem[ch]
                new_buyers = max(0.0, o_sold - x.get("repeat_orders", 0.0) * (o_sold / max(orders, 1)))
                ln.buyers += new_buyers
                if ch == "site":
                    site_new = new_buyers
                    # new site buyers, and marketplace buyers who bought again on the site, join the list
                    ln.owned += new_buyers + x.get("switch_orders", 0.0) * (o_sold / max(orders, 1))
                ln.aware = min(ln.t["m"] * ln.m_mult, ln.aware + new_buyers)
                y[ch] = {"sessions": int(round(x["sessions"])), "orders": o_sold, "units": u_sold,
                         "revenue": round(rev, 2), "in_stock": share >= 1}
            if "marketplace" not in want:
                ln.mp_sales.append(0)
            ln.mp_sales = ln.mp_sales[-60:]
            # COGS at average cost
            cogs = sold_units * unit_cost
            ln.on_hand -= sold_units
            ln.stock_value = max(0.0, ln.stock_value - cogs)
            self.ledger["cogs"] += cogs
            ln.pnl["cogs"] += cogs
            day_profit -= cogs
            day_profit += self.step_subscriptions(ln, site_new, unit_cost, y)
            # ads
            cpm_m, ctr_m = self.cpm_mult(month)
            for ach, spend, key in (("social", spend_social, "site"), ("sponsored", spend_sp, "marketplace")):
                if spend <= 0 and key not in dem:
                    continue
                self.spend(spend, "ads")
                ln.pnl["ads"] += spend
                day_profit -= spend
                clicks = dem.get(key, {}).get("clicks", 0.0)
                cpm = (ln.t["cpm"] * cpm_m if ach == "social" else ln.t["cpc_mp"] * 1000 * 0.004) * self.cost_fx
                impressions = int(spend * 1000 / cpm) if spend > 0 else 0
                clicks_int = int(self.luck(ln.pid, "ads", ach).poisson(clicks)) if clicks > 0 else 0
                true_orders = y.get(key, {}).get("orders", 0) * (clicks * 0.92 / max(1e-9, dem.get(key, {}).get("sessions", 1.0)))
                attributed = int(self.luck(ln.pid, "ads", ach).poisson(max(0.0, true_orders * self.luck(ln.pid, "ads", ach).uniform(1.15, 1.5)))) if spend > 0 else 0
                y["ads"][ach] = {"spend": round(spend, 2), "impressions": impressions, "clicks": clicks_int,
                                 "attributed_orders": attributed}
            # returns that arrive today
            keep = []
            ret_units = 0
            refunds = 0.0
            for item in ln.returns:
                due, units, price, ch, defective = item
                if due > d:
                    keep.append(item)
                    continue
                refund = units * price * self.net
                ship = units * self.fees(ln.pid)["return_ship"]
                if ch == "site":
                    self.cash -= refund
                else:
                    self.receivable -= refund
                    self.mp_pending.append((d, -refund))
                self.ledger["refunds"] += refund
                ln.pnl["refunds"] += refund
                ln.pnl["fulfillment"] += ship
                self.spend(ship, "fulfillment")
                good = units - defective
                resell = int(good * RULES["resell_share"])
                uc = unit_cost or self.landed(ln.pid)
                if self.closed:        # the company left: returned units are sold off where they are
                    got = RULES["liquidation"] * resell * uc
                    self.cash += got
                    self.ledger["cogs"] -= got
                    ln.pnl["cogs"] -= got
                    day_profit += -refund - ship + got
                else:
                    ln.on_hand += resell
                    ln.stock_value += resell * uc
                    self.ledger["cogs"] -= resell * uc
                    ln.pnl["cogs"] -= resell * uc
                    day_profit += -refund - ship + resell * uc
                ret_units += units
                refunds += refund
            ln.returns = keep
            y["returns_units"] = ret_units
            y["refunds"] = round(refunds, 2)
            # reviews that arrive today
            keep = []
            for due, star in ln.reviews:
                if due <= d:
                    ln.n_reviews += 1
                    ln.star_sum += star
                else:
                    keep.append((due, star))
            ln.reviews = keep
            # awareness: Bass discovery and word of mouth, and ad clicks
            if ln.active:
                m_eff = ln.t["m"] * ln.m_mult * self.trend(ln.pid)
                q = ln.t["bass_q"] * self.w["wom_mult"]
                new = (ln.t["bass_p"] + q * ln.buyers / m_eff) / 365 * max(0.0, m_eff - ln.aware)
                new += dem.get("site", {}).get("clicks", 0.0) * AD_AWARE * (2.4 if self.w["ad_delay"] else 1.0)
                ln.aware = min(m_eff, ln.aware * (1 - FORGET) + new)
                ln.buyers = min(ln.buyers, m_eff)
                ln.owned = min(ln.owned, ln.buyers)
            # reference prices follow the prices people paid: a day's price moves the reference in proportion to
            # that day's sales against the usual day, so a price nobody pays does not become a reference
            for ch in ln.channels:
                today = y.get(ch, {}).get("units", 0) if isinstance(y.get(ch), dict) else 0
                hist = ln.sold[ch]
                usual = sum(hist[-28:]) / len(hist[-28:]) if hist else 0.0
                wgt = min(1.0, today / usual) if usual > 0 else (1.0 if today > 0 else 0.0)
                here = self.local(ln, ch)
                ln.ref[ch] += REF_SPEED * wgt * (here - ln.ref[ch])
                cap = 1.1 * max([ln.typ] + [r.price for r in ln.all_rivals()])
                ln.ref[ch] = min(ln.ref[ch], cap)
                hist.append(today)
                del hist[:-60]
                wp = ln.week_px[ch]
                wp[0] += here * today
                wp[1] += today
                wp[2] += here
                wp[3] += 1
            # storage, dearer in October to December for stock at the marketplace
            stor = ln.on_hand * self.storage(ln.pid, month, "marketplace" in ln.channels) / 30
            self.spend(stor, "storage")
            ln.pnl["storage"] += stor
            if ln.active:
                ln.pnl["fixed"] += self.line_month / 30
            day_profit -= stor
            ln.yesterday = y
        return day_profit

    def _deal(self, ln, ch, lam):
        """deal_seekers worlds: a share of shoppers only buy on a discount of at least 10% below the reference
        price. They wait (about 60 days, then they buy elsewhere), and a discount brings them back."""
        deal = self.w["deal"]
        p, ref = self.local(ln, ch), ln.ref[ch]
        share = deal["share"]
        ln.pull[ch] = ln.pull[ch] * (1 - 1 / 60) + share * lam
        if p <= 0.9 * ref:
            take = ln.pull[ch] * deal["release"]
            ln.pull[ch] -= take
            return lam + take
        return lam * (1 - share)

    def copycats(self):
        """A line that sells well on the marketplace draws a copycat: each week, after 60 days of strong
        sales, a 2.5% chance that a new seller enters at 82% to 92% of the typical price, learning fast."""
        for ln in self.lines.values():
            if not ln.active or "marketplace" not in ln.channels or ln.copycats >= 2:
                continue
            strong = ln.t["m"] * ln.t["search_rate"] * VEL_SCALE * 1.5
            if len(ln.mp_sales) >= 60 and sum(ln.mp_sales[-60:]) / 60 > strong and self.luck(ln.pid, "copycat").random() < 0.025:
                ln.copycats += 1
                spec = {"name": f"{ln.pid}-copy{ln.copycats}", "kind": "learner", "speed": 0.2,
                        "cost": ln.typ * 0.45, "appeal": -0.5 if ln.improved else -0.2, "rating": 4.1,
                        "price": ln.typ * float(self.luck(ln.pid, "copycat").uniform(0.82, 0.92)), "visibility": 0.8,
                        "own_demand": 0.5}
                ln.extra_rivals.append(Rival(spec, ln.typ))
                self.events.append({"day": self.day, "text": f"a copycat seller entered {ln.pid}", "hidden": True})

    def step_rivals(self, month, mday):
        self.copycats()
        for ln in self.lines.values():
            ch = "marketplace" if "marketplace" in ln.channels else "site"
            wp = ln.week_px[ch]
            if not ln.active or not self.selling():
                ours = None
            elif wp[1] > 0:
                ours = wp[0] / wp[1]
            elif wp[3] > 0:
                ours = wp[2] / wp[3]
            else:
                ours = self.local(ln, ch)
            for c in ln.week_px:
                ln.week_px[c] = [0.0, 0.0, 0.0, 0]
            for i, r in enumerate(ln.all_rivals()):
                # weekly profit: sales outside our markets plus the marketplace share against us
                r.profit_week = tricks.rival_profit(ln, r, r.price, ours)
                r.weekly(self.day, ours, lambda p, o, r=r: tricks.rival_profit(ln, r, p, o),
                         self.luck(ln.pid, "rivals", r.spec.get("name", i)), month, mday)
            self.tricks.weekly_rivals(ln)

    def step_tricks(self):
        """Vendor pitches, review service purchases toward the sales rank, posted reviews and the marketplace's
        look (tricks.py). Returns today's vendor spending, which counts in operating profit."""
        self.tricks.daily_pitches()
        for ln in self.lines.values():
            if ln.paid_today:
                if ln.mp_sales:
                    ln.mp_sales[-1] += ln.paid_today
                ln.paid_today = 0
            self.tricks.daily_reviews(ln)
        spent, self.tricks.spent_today = self.tricks.spent_today, 0.0
        return -spent

    def step_money(self):
        d = self.day
        # fixed costs once a product is launched
        n_active = len(self.lines_selling())
        fixed = 0.0
        if self.lines or any(m.lines for m in self.abroad.values()):
            # overhead grows with the business: the larger of the base and a share of the average 30 days' revenue
            # over the last overhead_window days (staff, customer service, software, rent)
            back = self.daily[-RULES["overhead_window"]:]
            month_rev = sum(x["revenue"] for x in back) / len(back) * 30 if back else 0.0
            fixed = max(RULES["overhead_month"], RULES["overhead_share"] * month_rev) / 30 + \
                n_active * RULES["line_month"] / 30
            for ln in self.lines.values():       # the email and SMS list of own-site customers
                lst = ln.owned * RULES["list_cost_month"] / 30
                ln.pnl["fixed"] += lst
                fixed += lst
        self.spend(fixed, "overhead")
        interest = self.credit_used * RULES["credit_apr"] / 365 + self.credit_line * RULES["credit_fee"] / 365 + \
            self.venture_debt * RULES["venture_debt_apr"] / 365
        self.spend(interest, "interest")
        self.year_interest += interest
        if d > 0 and d % self.payout_days == 0:
            self.pay_out_marketplace()
        return -fixed

    def step_money_abroad(self):
        """A country's own running costs (tax agent, compliance, local customer service), its email list, and its
        marketplace payouts. Overhead, interest and tax are the company's (the US market's step_money)."""
        d = self.day
        fixed = 0.0
        if not self.closed:
            fixed = countrymod.row(self.cc)["month"] / 30
        for ln in self.lines.values():
            lst = ln.owned * RULES["list_cost_month"] / 30
            ln.pnl["fixed"] += lst
            fixed += lst
        self.spend(fixed, "overhead")
        if d > 0 and d % self.payout_days == 0:
            self.pay_out_marketplace()
        return -fixed

    def step_abroad(self, month):
        """One day of every country's market, after the US market's. Returns the countries' operating profit."""
        total = 0.0
        for m in self.abroad.values():
            p = m.step_sales(month)
            if m.selling():
                p += m.step_cart()
                p += m.step_bundles()
                p += m.step_tricks()
            p += m.step_money_abroad()
            p -= m.launch_today
            m.launch_today = 0.0
            m.op_profit.append(p)
            m.daily.append(m.snapshot(p))
            total += p
        return total

    def pay_out_marketplace(self):
        """The marketplace's payout: sales at least mp_hold_days old (delivered, plus the marketplace's 7 day
        reserve), less refunds."""
        d = self.day
        due = sum(a for t, a in self.mp_pending if t <= d)
        self.mp_pending = [(t, a) for t, a in self.mp_pending if t > d]
        hold = min(max(0.0, due), self.tricks.held())     # a reserve while a listing is suspended
        if hold > 0:
            self.mp_pending.append((d + 1, hold))
            due -= hold
        self.cash += due
        self.receivable -= due

    def check_cash(self):
        self.update_credit()
        if self.cash < 0:
            need = -self.cash
            avail = self.credit_limit - self.credit_used
            take = min(need, avail)
            self.credit_used += take
            self.cash += take
            if take > 0:
                self.note(f"drew ${take:,.0f} of credit to cover a negative cash balance")
        if self.cash < -1e-6:
            raise Bankrupt(f"out of cash on day {self.day} with the credit line used up")

    def start(self, extra=None):
        """Day 0's setup: the change announced for this market, the launch costs, and the software's start."""
        if not (self.midrun or {}).get("start_day"):
            self.apply_midrun()
        self._launch = RULES["launch_cost"] * len(self.lines)
        self.spend(self._launch, "launch")
        self.ledger.setdefault("inventory_paid", 0.0)
        self.start_software(extra)

    def start_software(self, extra=None):
        """Start the software (again). A live company starts it each month, with the newest code (v1.0)."""
        self.software_frozen = False
        try:
            self.policy.init(dict(self.setup(), **(extra or {})))
        except Exception as e:
            self.note(f"software failed to start: {e}")
            self.software_frozen = True

    def stop_software(self):
        close = getattr(self.policy, "close", None)
        if close:
            try:
                close()
            except Exception:
                pass

    def advance(self, days):
        """Run up to `days` more days. Returns True while the world goes on (not at its end, not bankrupt)."""
        if self.ended:
            return False
        try:
            for _ in range(days):
                if self.next_day >= self.days:
                    break
                d = self.day = self.next_day
                self._luck = {}
                date = _date(d)
                self.update_credit()
                self.world_events()
                start = (self.midrun or {}).get("start_day")
                if start:       # a live company meets the announced change on its day (v1.0), told 30 days ahead
                    if d == max(0, start - 30):
                        self.news.append({"day": d, "kind": "market_change", "text": self.midrun["text"]})
                    if d == start:
                        self.apply_midrun()
                for m in self.abroad.values():
                    m.day = m.next_day = d
                    m._luck = {}
                    m.market_events()
                if not self.software_frozen:
                    try:
                        act = self.policy.step(self.observe())
                    except Exception as e:
                        self.note(f"software failed: {e}; its last actions stay in place {self.frozen_until}")
                        self.software_frozen = True
                        act = {}
                    try:
                        self.apply(act)
                    except Exception as e:     # a last guard: an action the checks above missed
                        self.note(f"actions could not be applied ({type(e).__name__}: {e}); the rest of today's "
                                  f"actions were skipped")
                self.check_cash()
                self.step_supply()
                if self.transfers:
                    self.step_transfers()
                for m in self.abroad.values():
                    m.step_supply()
                profit = self.step_sales(date.month)
                profit += self.step_cart()
                profit += self.step_bundles()
                profit += self.step_levers()
                profit += self.step_tricks()
                profit += self.step_money()
                profit -= (self._launch if d == 0 else 0.0) + self.launch_today
                self.launch_today = 0.0
                if self.abroad:
                    profit += self.step_abroad(date.month)
                if d % 7 == 0:
                    self.step_rivals(date.month, date.day)
                    for m in self.abroad.values():
                        m.step_rivals(date.month, date.day)
                self.op_profit.append(profit)
                self.year_profit += profit
                if (d + 1) % 365 == 0:
                    tax = RULES["tax_rate"] * max(0.0, self.year_profit - self.year_interest)
                    self.spend(tax, "tax")
                    self.year_profit = 0.0
                    self.year_interest = 0.0
                self.check_cash()
                self.daily.append(self.snapshot(profit))
                self.keep_record(d, date)
                self.next_day = d + 1
                if self.on_month and d % 30 == 29:
                    try:
                        self.on_month(self)
                    except Exception:
                        pass
        except Bankrupt as e:
            self.bankrupt_day = self.day
            self.bankrupt_reason = str(e)
            self.daily.append(self.snapshot(0.0))
            self.ended = True
        if self.next_day >= self.days:
            self.ended = True
        return not self.ended

    def run(self):
        self.start()
        try:
            self.advance(self.days)
        finally:
            self.stop_software()
        return self.result()

    def snapshot(self, profit):
        """One day of the company (of the US market, the whole company's; of a country, that country's)."""
        stock, revenue, receivable = self.market_totals()
        for m in self.abroad.values():
            s_, r_, rc_ = m.market_totals()
            stock += s_
            revenue += r_
            receivable += rc_
        return {"day": self.day, "cash": round(self.cash, 2), "credit_used": round(self.credit_used, 2),
                "receivable": round(receivable, 2), "profit": round(profit, 2),
                "stock_value": round(stock, 2), "revenue": round(revenue, 2)}

    def market_totals(self):
        """This market's stock on hand at cost, yesterday's revenue, and what its marketplace owes."""
        stock = sum(ln.stock_value for ln in self.lines.values())
        revenue = sum(sum(y.get("revenue", 0) for k, y in ln.yesterday.items()
                          if k in ("site", "marketplace", "subscription"))
                      + (ln.yesterday.get("wholesale") or {}).get("invoiced", 0.0)
                      for ln in self.lines.values())
        return stock, revenue, self.receivable

    def keep_record(self, d, date):
        if self.record_kind == "day":
            row = {"day": d, "date": date.isoformat(), "cash": round(self.cash, 2),
                   "products": {pid: {"price": dict(ln.price), "ad_budget": dict(ln.budget), "on_hand": ln.on_hand,
                                      "rating": round(ln.rating, 2), "rival_prices": [round(r.price, 2) for r in ln.all_rivals()],
                                      **ln.yesterday} for pid, ln in self.lines.items()}}
            self.record.append(row)

    def value(self):
        """The team's share of the company's value at the end. Company value: cash and money owed to us, plus stock
        at liquidation value, minus debt and tax due, plus a multiple of the trailing year's operating profit when it
        is positive. The team's share is what is left after the investors' preferences (team_share_of)."""
        cash = self.cash + self.receivable + sum(a for _, a in self.wh_receivable)
        stock, pending, pending_stock = self.market_value_parts()
        for m in self.abroad.values():         # every country's money owed, stock and returns to come
            cash += m.receivable
            s_, p_, ps_ = m.market_value_parts()
            stock += s_
            pending += p_
            pending_stock += ps_
        for t in self.transfers:
            stock += t["qty"] * t["unit"]
        # chains still selling send back their unsold units when the contract ends
        for c in self.contracts:
            if c["status"] == "running":
                ln = self.lines[c["product"]]
                back = int(ln.t["wholesale"]["unsold"] * c["stock"])
                uc = ln.stock_value / ln.on_hand if ln.on_hand else landed(ln.pid)
                pending += back * c["price"] - back * uc
                pending_stock += back * uc
        # the tax of the last part year, owed but not yet paid
        tax_due = RULES["tax_rate"] * max(0.0, self.year_profit - self.year_interest)
        trailing = sum(self.op_profit[-365:]) - pending
        company = cash + RULES["liquidation"] * (stock + pending_stock) - self.credit_used - self.venture_debt - \
            tax_due - (pending + pending_stock) + RULES["profit_multiple"] * max(0.0, trailing)
        team, pref = self.team_share_of(company)
        return {"value": team, "company_value": company, "ownership": self.ownership, "investor_preference": pref,
                "cash": cash, "stock_cost": stock, "debt": self.credit_used + self.venture_debt, "tax_due": tax_due,
                "trailing_profit": trailing, "returns_after_end": pending}

    def market_value_parts(self):
        """This market's stock (on hand and at sea to it), and the returns of sales already made that come back
        after the end: refunds and return shipping less the resellable units, and those units."""
        stock = 0.0
        for ln in self.lines.values():
            stock += ln.stock_value
            for po in ln.pos:
                if po["status"] == "shipped":
                    stock += po["shipped"] * po["unit"]
        pending, pending_stock = 0.0, 0.0
        for ln in self.lines.values():
            uc = ln.stock_value / ln.on_hand if ln.on_hand else self.landed(ln.pid)
            ret_ship = self.fees(ln.pid)["return_ship"]
            for due, units, price, ch, defective in ln.returns:
                resell = int((units - defective) * RULES["resell_share"])
                pending += units * price * self.net + units * ret_ship - resell * uc
                pending_stock += resell * uc
        return stock, pending, pending_stock

    def result(self):
        v = self.value() if self.bankrupt_day is None else {"value": 0.0}
        months = []
        for i in range(0, len(self.daily), 30):
            chunk = self.daily[i:i + 30]
            months.append({"month": i // 30, "cash": chunk[-1]["cash"] + chunk[-1]["receivable"] - chunk[-1]["credit_used"],
                           "revenue": round(sum(c["revenue"] for c in chunk), 2),
                           "profit": round(sum(c["profit"] for c in chunk), 2)})
        lines = {}
        for pid, ln in self.lines.items():
            pnl = {k: round(v, 2) for k, v in ln.pnl.items()}
            pnl["contribution"] = round(ln.pnl["revenue"] - sum(v for k, v in ln.pnl.items() if k != "revenue"), 2)
            lines[pid] = {"pnl": pnl, "active": ln.active, "buyers": int(ln.buyers), "rating": round(ln.rating, 2),
                          "reviews": ln.n_reviews, "lost_units": int(ln.lost), "on_hand": ln.on_hand,
                          "aware": int(ln.aware), "site_customers": int(ln.owned), "improved": ln.improved, "subscribers": int(ln.subs),
                          "subscription_discount": ln.subs_discount}
        years = []
        for i in range(0, len(self.daily), 365):
            chunk = self.daily[i:i + 365]
            years.append({"year": i // 365 + 1, "days": len(chunk), "revenue": round(sum(c["revenue"] for c in chunk), 2),
                          "profit": round(sum(c["profit"] for c in chunk), 2)})
        out = {
            "seed": self.w["seed"], "shape": self.w["shape"], "days": self.day + 1, "years": years,
            "bankrupt": self.bankrupt_day is not None, "bankrupt_day": self.bankrupt_day,
            "bankrupt_reason": self.bankrupt_reason, "software_frozen": self.software_frozen,
            "value": round(v["value"], 2), "value_parts": {k: round(x, 2) for k, x in v.items()},
            "ledger": {k: round(x, 2) for k, x in self.ledger.items()},
            "months": months, "lines": lines, "notes": self.notes[:50], "events": self.events,
            "news": self.news, "record": self.record, "bundles": self.bundles,
            "tricks": dict(self.tricks.stats),
        }
        if self.abroad:
            out["countries"] = {cc: m.market_result() for cc, m in self.abroad.items()}
        return out

    def market_result(self):
        """A country's part of the result: when it opened, its own profit and ledger, and its lines."""
        lines = {}
        for pid, ln in self.lines.items():
            pnl = {k: round(v, 2) for k, v in ln.pnl.items()}
            pnl["contribution"] = round(ln.pnl["revenue"] - sum(v for k, v in ln.pnl.items() if k != "revenue"), 2)
            lines[pid] = {"pnl": pnl, "active": ln.active, "buyers": int(ln.buyers), "rating": round(ln.rating, 2),
                          "reviews": ln.n_reviews, "on_hand": ln.on_hand}
        return {"opens": self.opens, "left": self.closed, "fx": round(self.fx, 4),
                "revenue": round(sum(x["revenue"] for x in self.daily), 2),
                "operating_profit": round(sum(self.op_profit), 2),
                "ledger": {k: round(x, 2) for k, x in self.ledger.items()}, "lines": lines}


def run(plan, truth, world, policy, days=None, record="month"):
    return Sim(plan, truth, world, policy, days=days, record=record).run()
