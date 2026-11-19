"""World conditions: everything that differs between the 48 graded worlds, drawn from a world seed.

A world fixes the economy (recession timing), ad prices, the rivals in each product's market and how
they set prices, the hidden changes during the three years, the announced shocks, and the model shape.
The product facts come from the run's secret (truth.py), not from the world.

Model shapes (docs/REFERENCE.md lists all five, but not which world uses which):
- mnl: multinomial logit demand, geometric adstock.
- nested: nested logit. Customers pick between buying in the category and not buying, then between
  products, so products substitute more for each other than in mnl.
- wom: strong word of mouth, ads that cost more.
- delayed_ads: ads work with a delay of about a week (an adstock that peaks late), bring fewer clicks at
  once and more people who come back later. Never in test_market.
- deal_seekers: strong reference price effects, and a share of shoppers who wait for a discount of at
  least 10%. Never in test_market.
"""
from .catalog import CATEGORIES, PRODUCTS, RULES
from .truth import rng_for

SHAPES = ("mnl", "nested", "wom", "delayed_ads", "deal_seekers")
PRACTICE_SHAPES = ("mnl", "nested", "wom")
UNSEEN_SHAPES = ("delayed_ads", "deal_seekers")

RIVAL_TYPES = ("fixed", "follower", "learner", "undercutter")


def graded_shapes(n: int) -> list:
    """Shapes of n graded worlds: three quarters from the practice shapes, one quarter unseen."""
    n_unseen = n // 4
    seen = [PRACTICE_SHAPES[i % 3] for i in range(n - n_unseen)]
    unseen = [UNSEEN_SHAPES[i % 2] for i in range(n_unseen)]
    return seen + unseen


def make_world(seed: str, shape: str) -> dict:
    assert shape in SHAPES, shape
    r = rng_for(seed, "world")
    days = RULES["days"]
    w = {"seed": seed, "shape": shape}

    # Economy: about a third of worlds have a recession in the three years.
    if r.random() < 0.35:
        start = int(r.integers(120, days - 200))
        w["recession"] = {"start": start, "end": int(start + r.integers(180, 540)),
                          "demand": float(r.uniform(0.82, 0.90)), "beta": float(r.uniform(1.10, 1.25))}
    else:
        w["recession"] = None

    w["season_amp"] = {c: float(r.uniform(0.7, 1.3)) for c in CATEGORIES}
    # Ad prices: holiday rises in November and December, and in some worlds a lasting jump in social
    # ad prices (an ad platform change), with lower click rates.
    w["q4_cpm"] = float(r.uniform(1.25, 1.7))
    w["platform"] = ({"day": int(r.integers(150, days - 150)), "cpm": float(r.uniform(1.25, 1.5)),
                      "ctr": float(r.uniform(0.75, 0.9))} if r.random() < 0.3 else None)

    # Rivals in each product's market.
    rivals = {}
    for pid, p in PRODUCTS.items():
        rr = rng_for(seed, "rivals", pid)
        k = int(rr.integers(2, 4))
        lst = []
        for i in range(k):
            kind = str(rr.choice(RIVAL_TYPES, p=[0.25, 0.25, 0.35, 0.15]))
            lst.append({
                "name": f"{pid}-r{i + 1}",
                "kind": kind,
                "speed": float(rr.choice([0.05, 0.2])),        # learning rate of a learner
                "cost": float(p["price"] * rr.uniform(0.48, 0.62)),   # marginal cost with fees
                "appeal": float(rr.normal(0.0, 0.3)),          # utility against our product at the same price
                "rating": float(rr.uniform(3.9, 4.6)),
                "price": float(p["price"] * rr.uniform(0.88, 1.12)),
                "visibility": float(rr.uniform(0.8, 2.0)),     # marketplace standing of an established seller
                "own_demand": float(rr.uniform(0.6, 1.4)),     # sales it makes outside our markets, as a scale
            })
        rivals[pid] = lst
    w["rivals"] = rivals

    # Product life cycles: each product's market grows or fades through the three years (a yearly rate),
    # and a few products are fads that peak and then fall.
    trends = {}
    for pid in PRODUCTS:
        rr = rng_for(seed, "trend", pid)
        g = float(rr.normal(0.0, 0.4))
        fad = None
        if rr.random() < 0.15:
            fad = {"peak": int(rr.integers(200, 700)), "fall": float(rr.uniform(0.8, 1.6))}
        trends[pid] = {"growth": g, "fad": fad}
    w["trends"] = trends

    # Hidden changes during the world. None are announced.
    drifts = []
    for _ in range(int(r.integers(1, 4))):
        kind = str(r.choice(["market_shrink", "market_grow", "price_sensitive", "ads_fatigue", "rival_entry"]))
        day = int(r.integers(180, 900))
        if kind in ("market_shrink", "market_grow", "rival_entry"):
            target = str(r.choice(list(PRODUCTS)))
        elif kind == "price_sensitive":
            target = str(r.choice(CATEGORIES))
        else:
            target = "all"
        size = {"market_shrink": r.uniform(0.6, 0.8), "market_grow": r.uniform(1.3, 1.6),
                "price_sensitive": r.uniform(1.2, 1.4), "ads_fatigue": r.uniform(1.3, 1.6),
                "rival_entry": r.uniform(0.80, 0.90)}[kind]
        drifts.append({"kind": kind, "day": day, "target": target, "size": float(size)})
    w["drifts"] = sorted(drifts, key=lambda d: d["day"])

    # Announced shocks: the news says when they start, 30 days ahead.
    shocks = []
    if r.random() < 0.5:
        shocks.append({"kind": "tariff", "day": int(r.integers(200, 900)), "target": str(r.choice(CATEGORIES)),
                       "size": float(r.uniform(0.15, 0.25))})
    if r.random() < 0.35:
        shocks.append({"kind": "supplier_closure", "day": int(r.integers(150, 950)),
                       "target": str(r.choice(list(PRODUCTS)))})
    if r.random() < 0.35:
        shocks.append({"kind": "port_delay", "day": int(r.integers(150, 950)),
                       "length": int(r.integers(45, 90)), "size": int(r.integers(20, 36))})
    if r.random() < 0.3:
        shocks.append({"kind": "platform_fee", "day": int(r.integers(200, 900)), "size": 0.02})
    w["shocks"] = sorted(shocks, key=lambda s: s["day"])

    # Shape parameters.
    w["nest"] = float(r.uniform(0.45, 0.75)) if shape == "nested" else 1.0
    w["wom_mult"] = float(r.uniform(1.8, 2.6)) if shape == "wom" else 1.0
    w["ad_cost_mult"] = 1.25 if shape == "wom" else 1.0
    w["ad_delay"] = int(r.integers(5, 10)) if shape == "delayed_ads" else 0
    w["deal"] = ({"loss_mult": float(r.uniform(2.0, 3.0)), "share": float(r.uniform(0.25, 0.45)),
                  "release": float(r.uniform(0.15, 0.3))}
                 if shape == "deal_seekers" else None)
    return w


def check_world(seed, products):
    """A world for checking that software runs (the office's plan_review and the grader's start check): every kind
    of news arrives in the first days, so software that reads news is exercised."""
    w = make_world(seed, "mnl")
    pid = products[0] if products else "k1"
    cat = PRODUCTS[pid]["category"]
    w["shocks"] = [{"kind": "tariff", "day": 4, "target": cat, "size": 0.2},
                   {"kind": "supplier_closure", "day": 6, "target": pid},
                   {"kind": "port_delay", "day": 8, "length": 30, "size": 25},
                   {"kind": "platform_fee", "day": 10, "size": 0.02}]
    w["check_news"] = True
    return w


MIDRUN_KINDS = ("fee", "tariff", "ads", "big_rival")


def midrun_shock(secret):
    """The lasting change to the market that the office announces halfway through the run, drawn from the run's
    secret. It applies to every graded world from day 0, and to test markets started after the announcement."""
    r = rng_for(secret, "midrun")
    kind = str(r.choice(MIDRUN_KINDS))
    cats = [str(c) for c in r.choice(CATEGORIES, size=2, replace=False)]
    if kind == "fee":
        size = 0.05
        text = ("The marketplace raises its referral fee from 15% to 20% of the price, for good. This applies to every "
                "graded market from its first day.")
    elif kind == "tariff":
        size = 0.30
        text = (f"A tariff of 30% on the supplier price of {cats[0]} and {cats[1]} goods, from every supplier, applies "
                f"for good. This applies to every graded market from its first day.")
    elif kind == "ads":
        size = 1.4
        text = ("A change to ad platforms' privacy rules makes ads dearer for good: social ads cost 40% more per view, "
                "sponsored ads about 18% more per click. This applies to every graded market from its first day.")
    else:
        size = 0.85
        text = (f"A large established seller enters {cats[0]} and {cats[1]}: in every product of those categories, a "
                f"strong new rival sells on the marketplace at about 85% of the typical price and adjusts its prices "
                f"as it learns. This applies to every graded market from its first day.")
    return {"kind": kind, "categories": cats, "size": size, "text": text}
