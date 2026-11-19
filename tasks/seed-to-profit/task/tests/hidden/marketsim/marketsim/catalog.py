"""The public catalog: 30 candidate products in 6 categories, and the public rules of the market.

Everything in this module is public. docs/REFERENCE.md in the team image prints the same numbers
(build_docs.py writes them from here). The hidden facts of each product are drawn per run in truth.py.

Prices are in US dollars. `price` is the typical price of comparable products today, which is what
rivals charge at the start. `cost` is the supplier's unit price (FOB), `freight` the inbound freight
and duty per unit, `moq` the supplier's minimum order, `lead` the supplier's quoted lead time in days
from order to arrival at the warehouse (production, then about five weeks at sea and at the port), and `size`
the fulfillment size class. Each number is calibrated against public sources (README, Credits).
"""

CATEGORIES = ("kitchen", "pet", "fitness", "home_office", "beauty", "outdoor")

# id, name, category, cost, freight, price, moq, lead, size
_ROWS = [
    ("k1", "Cast iron skillet, 10 inch", "kitchen", 9.50, 2.10, 34.0, 500, 70, "medium"),
    ("k2", "Pour-over coffee set", "kitchen", 7.00, 1.40, 29.0, 300, 65, "medium"),
    ("k3", "Knife sharpener", "kitchen", 4.00, 0.60, 22.0, 500, 55, "small"),
    ("k4", "Silicone food storage bags, set of 8", "kitchen", 3.20, 0.50, 19.0, 1000, 55, "small"),
    ("k5", "Electric milk frother", "kitchen", 5.50, 0.80, 24.0, 500, 65, "small"),
    ("p1", "Orthopedic dog bed, large", "pet", 18.00, 6.50, 69.0, 200, 80, "large"),
    ("p2", "Slow feeder dog bowl", "pet", 2.50, 0.60, 16.0, 1000, 55, "small"),
    ("p3", "Cat water fountain", "pet", 8.00, 1.60, 32.0, 400, 70, "medium"),
    ("p4", "No-pull dog harness", "pet", 4.50, 0.50, 25.0, 500, 60, "small"),
    ("p5", "Pet hair remover roller", "pet", 2.20, 0.40, 15.0, 1000, 55, "small"),
    ("f1", "Adjustable dumbbells, pair", "fitness", 55.00, 14.00, 179.0, 100, 90, "large"),
    ("f2", "Resistance band set", "fitness", 3.50, 0.50, 22.0, 1000, 55, "small"),
    ("f3", "Yoga mat, 6 mm", "fitness", 6.00, 1.80, 32.0, 500, 65, "medium"),
    ("f4", "Foam roller", "fitness", 4.00, 1.50, 21.0, 500, 65, "medium"),
    ("f5", "Massage gun", "fitness", 28.00, 3.00, 89.0, 200, 75, "medium"),
    ("h1", "Laptop stand", "home_office", 7.00, 1.20, 35.0, 500, 65, "medium"),
    ("h2", "Monitor light bar", "home_office", 11.00, 1.30, 45.0, 300, 70, "medium"),
    ("h3", "Ergonomic footrest", "home_office", 9.00, 2.40, 38.0, 300, 70, "medium"),
    ("h4", "Cable management kit", "home_office", 2.80, 0.40, 17.0, 1000, 55, "small"),
    ("h5", "Standing desk converter", "home_office", 48.00, 16.00, 169.0, 100, 90, "large"),
    ("b1", "Facial cleansing brush", "beauty", 6.00, 0.60, 34.0, 500, 60, "small"),
    ("b2", "Hair dryer brush", "beauty", 14.00, 1.80, 55.0, 300, 70, "medium"),
    ("b3", "Jade roller set", "beauty", 2.50, 0.40, 18.0, 1000, 55, "small"),
    ("b4", "LED makeup mirror", "beauty", 9.00, 2.00, 39.0, 300, 70, "medium"),
    ("b5", "Satin pillowcase set", "beauty", 4.00, 0.50, 24.0, 500, 55, "small"),
    ("o1", "Camping hammock", "outdoor", 8.00, 1.00, 36.0, 500, 65, "medium"),
    ("o2", "Insulated water bottle, 32 oz", "outdoor", 5.50, 1.10, 29.0, 500, 65, "medium"),
    ("o3", "Solar lantern, set of 2", "outdoor", 7.00, 1.20, 32.0, 500, 70, "medium"),
    ("o4", "Portable camping stove", "outdoor", 16.00, 2.60, 59.0, 300, 75, "medium"),
    ("o5", "Hiking daypack, 25 L", "outdoor", 12.00, 2.20, 49.0, 300, 70, "medium"),
]

PRODUCTS = {
    r[0]: {"id": r[0], "name": r[1], "category": r[2], "cost": r[3], "freight": r[4], "price": r[5],
           "moq": r[6], "lead": r[7], "size": r[8]}
    for r in _ROWS
}

# Size classes. site_fulfill: pick, pack and ship one order from our warehouse. mp_fulfill: the
# marketplace's fulfillment fee per unit. storage: warehouse storage per unit per 30 days. storage_q4_mp: the
# same in October to December for a line that sells on the marketplace (its stock sits in the marketplace's
# warehouses, which charge about 3 times as much then, 2.5 times for oversize). return_ship: what a return costs
# us in shipping and handling. wholesale_ship: pallet freight per unit to a retail chain's warehouse.
SIZES = {
    "small": {"site_fulfill": 6.50, "mp_fulfill": 4.10, "storage": 0.15, "storage_q4_mp": 0.45, "return_ship": 4.00,
              "wholesale_ship": 0.60},
    "medium": {"site_fulfill": 8.50, "mp_fulfill": 6.20, "storage": 0.45, "storage_q4_mp": 1.35, "return_ship": 7.00,
               "wholesale_ship": 1.20},
    "large": {"site_fulfill": 15.00, "mp_fulfill": 11.50, "storage": 1.50, "storage_q4_mp": 3.75, "return_ship": 12.00,
              "wholesale_ship": 3.00},
}

# Heavy or bulky products whose marketplace fulfillment fee is above their size class's: the marketplace charges
# large bulky items a base fee plus a fee per pound (adjustable dumbbells about 50 lb, a standing desk converter
# about 33 lb, a large dog bed by its dimensional weight).
PRODUCT_FEES = {"f1": {"mp_fulfill": 28.00}, "h5": {"mp_fulfill": 22.00}, "p1": {"mp_fulfill": 13.50}}


def fees(pid: str) -> dict:
    """Fulfillment, storage and shipping costs of one product: its size class, with any product's own fee."""
    return dict(SIZES[PRODUCTS[pid]["size"]], **PRODUCT_FEES.get(pid, {}))


def storage(pid: str, month: int, on_marketplace: bool) -> float:
    """Storage per unit per 30 days in a month (1 to 12)."""
    f = fees(pid)
    return f["storage_q4_mp"] if on_marketplace and month >= 10 else f["storage"]

# Monthly demand factors by category, January first. The base row is close to the Census monthly
# series for nonstore retailers (NAICS 4541), not seasonally adjusted over seasonally adjusted, and
# each category moves its peak (public retail data). Gift categories (kitchen, beauty) put
# about a quarter of their holiday rise in November and three quarters in December, as online retail does.
SEASON = {
    "kitchen": [0.90, 0.85, 0.94, 0.93, 0.98, 0.94, 1.00, 0.96, 0.94, 1.00, 1.19, 1.56],
    "pet": [0.97, 0.92, 0.99, 0.98, 1.00, 0.98, 1.02, 1.00, 0.97, 1.00, 1.08, 1.15],
    "fitness": [1.40, 1.15, 1.02, 0.95, 0.95, 0.90, 0.92, 0.90, 0.92, 0.90, 1.00, 1.04],
    "home_office": [1.12, 1.00, 0.98, 0.94, 0.92, 0.90, 0.98, 1.12, 1.10, 0.98, 1.00, 1.04],
    "beauty": [0.92, 0.98, 0.94, 0.94, 1.00, 0.92, 0.96, 0.94, 0.92, 0.98, 1.18, 1.52],
    "outdoor": [0.72, 0.72, 0.88, 1.06, 1.25, 1.35, 1.35, 1.18, 0.96, 0.84, 0.88, 0.95],
}

# Typical share of orders returned, by category. Used as the center of each product's hidden rate.
RETURN_RATE = {"kitchen": 0.07, "pet": 0.09, "fitness": 0.11, "home_office": 0.08, "beauty": 0.10, "outdoor": 0.09}

RULES = {
    "start_date": "2027-03-01",
    "days": 1095,                  # three years
    "seed_money": 2_000_000.0,
    "research_budget": 250_000.0,
    "max_products": 10,
    "channels": ("site", "marketplace"),
    "ad_channels": {"site": "social", "marketplace": "sponsored"},
    "launch_cost": 6_000.0,        # per product line, paid on day 0: samples, photos, listing
    "overhead_month": 35_000.0,    # fixed cost per 30 days once at least one product is launched
    "line_month": 1_500.0,         # fixed cost per 30 days for each product line that is not stopped
    "overhead_share": 0.10,        # staff and running costs grow with the business: overhead per 30 days is the larger
                                   # of overhead_month and this share of the average 30 days' revenue over the last
    "overhead_window": 90,         # this many days (v0.3: overhead_month at any scale)
    "list_cost_month": 0.015,      # email and SMS software per own-site customer on the list, per 30 days
    "card_fee_pct": 0.029,         # own site: card processing
    "card_fee_fixed": 0.30,
    "mp_referral": 0.15,           # marketplace: referral fee on the sale price
    "mp_payout_days": 14,          # marketplace pays out every 14 days
    "mp_hold_days": 10,            # what sold in the last this many days is held to the next payout (delivery + 7 days)
    "deposit": 0.30,               # share of a supplier order paid when it is placed
    "transit_days": 35,            # the balance is paid when goods ship, this many days before arrival
    "price_break_qty": 5,          # orders of at least this many MOQs get the price break
    "price_break": 0.08,
    "credit_max": 500_000.0,       # the largest line a plan may ask for
    "credit_advance_stock": 0.5,   # borrowing base: this share of the landed cost of stock on hand
    "credit_advance_receivables": 0.8,   # plus this share of what the marketplace and chains owe
    "credit_apr": 0.105,           # prime plus 3 points
    "credit_fee": 0.005,           # yearly commitment fee on the limit
    "tax_rate": 0.21,              # on positive yearly profit, paid at the end of each year
    "liquidation": 0.35,           # stock at the end is valued at this share of its landed cost
    "profit_multiple": 3.0,        # company value counts this many years of trailing operating profit
    "resell_share": 0.6,           # share of returned units that can be sold again
    "max_daily_ad": 50_000.0,      # per product and ad channel
    "max_order_units": 200_000,
    # growing the company (REFERENCE.md, "Growing the company")
    "rnd_cost": 90_000.0,          # one development project of an improved own version of a product
    "rnd_max_running": 3,
    "wholesale_pay_days": 60,      # chains usually pay invoices this many days after delivery
    "wholesale_pay_days_choices": (30, 60, 90),   # each offer names one of these
    "wholesale_max_contracts": 6,
    "wholesale_penalty": 0.10,     # of the value of units short in a delivery
    "equity_first_day": 120,
    "equity_every_days": 180,      # at most one term sheet in this many days
    "equity_min_run_rate": 1_000_000.0,   # yearly revenue, from the last 90 days, before investors make offers
    "equity_fee": 0.04,            # legal and placement fees, of the amount raised
    "equity_max_share": 0.3,       # a term sheet sells at most this share of the pre-money valuation in new shares
    "equity_max": 4_000_000.0,
    "venture_debt_min_run_rate": 1_500_000.0,
    "venture_debt_share": 0.30,    # of the yearly revenue run rate, up to venture_debt_max
    "venture_debt_max": 2_000_000.0,
    "venture_debt_apr": 0.13,
    "venture_debt_warrant_coverage": (0.03, 0.10),   # warrants worth this share of the loan, at the lender's valuation
    "discount_range": (0.05, 0.30),   # subscription and bundle discounts
    "max_bundles": 5,
}


# Every product has three suppliers. A is the standard supplier (the catalog row). B is a budget supplier: cheaper,
# slower, with a larger minimum order and, usually, more defects. C is a premium supplier: dearer, faster, with a
# smaller minimum order and, usually, fewer defects. How much worse or better B and C really are differs by
# product and is hidden; supplier audits measure it.
SUPPLIER_KINDS = {
    "A": {"name": "standard", "cost": 1.00, "lead": 0, "moq": 1.0},
    "B": {"name": "budget", "cost": 0.80, "lead": 12, "moq": 2.0},
    "C": {"name": "premium", "cost": 1.18, "lead": -10, "moq": 0.5},
}


def supplier(pid: str, s: str = "A") -> dict:
    """The public quote of supplier s for product pid."""
    p, k = PRODUCTS[pid], SUPPLIER_KINDS[s]
    return {"supplier": s, "name": k["name"], "cost": round(p["cost"] * k["cost"], 2), "freight": p["freight"],
            "moq": int(p["moq"] * k["moq"]), "lead": max(20, p["lead"] + k["lead"])}


def landed(pid: str, qty: int = 0, s: str = "A") -> float:
    """Landed cost per unit from supplier s: its price (with the price break at large orders) plus freight."""
    q = supplier(pid, s)
    cost = q["cost"]
    if qty >= RULES["price_break_qty"] * q["moq"]:
        cost *= 1 - RULES["price_break"]
    return cost + q["freight"]


def public_catalog() -> dict:
    from .countries import public_table
    return {"products": [dict(p, suppliers={s: supplier(p["id"], s) for s in SUPPLIER_KINDS}, fees=fees(p["id"]))
                         for p in PRODUCTS.values()],
            "sizes": SIZES, "season": SEASON,
            "return_rate": RETURN_RATE, "rules": {k: (list(v) if isinstance(v, tuple) else v)
                                                   for k, v in RULES.items()},
            "countries": public_table()}
