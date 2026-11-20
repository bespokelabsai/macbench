"""The hidden facts of each product, drawn once per run from the run's secret.

Research measures these with noise. The verifier and the market office both rebuild them from the
secret in /var/lib/seedprofit-hub, so a team never sees them. Ranges are set so that the market model
reproduces public market numbers.
"""
import hashlib
import math

import numpy as np

from .catalog import PRODUCTS, RETURN_RATE

# Products people buy again: base yearly repeat purchases per buyer, the share of a year's buyers who buy again
# within 12 months at the typical price. Direct to consumer brands report about 25% to 45% for consumables, 30% to
# 45% in pet, 20% to 40% in beauty and 10% to 20% for home goods.
REPEAT = {"k4": 0.35, "p2": 0.30, "p5": 0.40, "b3": 0.18, "b5": 0.30, "f2": 0.25, "k2": 0.15, "p4": 0.20,
          "o2": 0.12, "h4": 0.12}
# Other products, by category: durables that a few buyers buy again (gifts, a second unit).
REPEAT_CATEGORY = {"kitchen": 0.08, "pet": 0.12, "fitness": 0.07, "home_office": 0.07, "beauty": 0.10, "outdoor": 0.06}
# Subscription orders a year, for the subscribe and save lever (the v0.2 repeat rates times 8, at least 2).
SUB_RATE = {"k4": 4.4, "p2": 2.0, "p5": 3.6, "b3": 2.0, "b5": 2.8, "f2": 2.0, "k2": 2.0, "p4": 2.0, "o2": 2.0, "h4": 2.0}


M_BASE = 200_000


def rng_for(*parts) -> np.random.Generator:
    h = hashlib.sha256("/".join(str(p) for p in parts).encode()).digest()
    return np.random.default_rng(int.from_bytes(h[:8], "little"))


def _clip(x, lo, hi):
    return float(min(hi, max(lo, x)))


def strata(secret: str, what: str) -> dict:
    """Spread one draw evenly over the catalog: each product gets a different quantile, in a secret order.

    Every run then has the same mix of large and small markets, of elastic and inelastic products, and of
    conversion and search rates; only which product is which changes. Independent draws made some runs much easier
    than others (v0.1: the tuned rules' median multiple was 2.5 in one run and 4.0 in another).
    """
    order = rng_for(secret, "strata", what).permutation(len(PRODUCTS))
    return {pid: (int(k) + 0.5) / len(PRODUCTS) for pid, k in zip(PRODUCTS, order)}


def _then(_old, new):
    """Use `new`, after the old independent draw `_old` was taken (so later draws do not move)."""
    return float(new)


def _normal_quantile(q):
    from statistics import NormalDist
    return NormalDist().inv_cdf(q)


def product_truth(secret: str, pid: str) -> dict:
    p = PRODUCTS[pid]
    r = rng_for(secret, "product", pid)
    price = p["price"]
    r.normal(0, 0.75), r.uniform(1.2, 3.0)          # the old independent draws, kept so later draws do not move
    # Addressable market: people who could want this product over the three years.
    m = M_BASE * (30.0 / price) ** 0.9 * float(np.exp(0.75 * _normal_quantile(strata(secret, "size")[pid])))
    eps = 1.2 + 1.8 * strata(secret, "eps")[pid]     # own price elasticity at the typical price
    q = {k: strata(secret, k)[pid] for k in ("conv", "conv_mp", "search")}
    r.normal(0, 0.35)                                 # the old independent draw
    conv_site = _clip(0.018 * np.exp(0.35 * _normal_quantile(q["conv"])), 0.006, 0.05)
    t = {
        "m": _clip(m, 60_000, 6_000_000),
        "eps": eps,
        "beta": eps / price,                         # logit price coefficient, per dollar
        "conv_site": conv_site,                      # chance a site visitor buys at the typical price
        # about 10% at the median, as Jungle Scout's "around 10%" average unit session rate (v0.3: 5 to 7 times the
        # site, clipped to 4% to 25%, centred near 11% with a tail to 23%)
        "conv_mp": _clip(conv_site * _then(r.uniform(5.0, 7.0), 4.75 + 1.5 * q["conv_mp"]), 0.04, 0.18),
        "satisfaction": float(r.uniform(3.8, 4.8)),  # mean review stars of a good unit
        "defect": _clip(0.025 * np.exp(r.normal(0, 0.6)), 0.004, 0.12),
        "returns": _clip(RETURN_RATE[p["category"]] * np.exp(r.normal(0, 0.3)), 0.02, 0.30),
        "bass_p": float(r.uniform(0.01, 0.04)),      # yearly discovery without ads
        "bass_q": _clip(0.38 * np.exp(r.normal(0, 0.4)), 0.08, 1.2),  # yearly word of mouth
        "repeat": _clip(REPEAT.get(pid, REPEAT_CATEGORY[p["category"]]) * np.exp(r.normal(0, 0.3)), 0.01, 0.8),
        "units_per_order": float(r.uniform(1.05, 1.35)),
        # social ads that send visitors to the own site
        "cpm": float(r.uniform(9.5, 15.0)),
        "ctr": float(r.uniform(0.012, 0.022)),
        "ad_half": float(m * r.uniform(0.004, 0.010)),    # daily spend where clicks get costly
        "ad_kappa": float(r.uniform(0.5, 0.9)),
        "adstock": float(r.uniform(0.5, 0.8)),           # daily carryover: a half-life of 1 to 3 days
        # daily site visits per aware person (direct and brand search). No good source for the rate; set so that these
        # visits bring about 40% to 45% of a site company's orders, as organic and direct traffic bring of e-commerce
        # revenue (v0.3: 0.0004 to 0.0012, about 25%). The same draw, scaled by 2.5.
        "visit_rate": float(r.uniform(0.0010, 0.0030)),
        # the marketplace
        "search_rate": _then(r.uniform(0.0006, 0.0018), 0.0006 + 0.0012 * q["search"]),  # daily searches for this kind of product per person in the market
        "cpc_mp": float(r.uniform(0.7, 1.3)),
        "mp_half": float(m * r.uniform(0.0003, 0.0008)),
        # the supplier
        "lead_mean": float(p["lead"] * r.uniform(1.0, 1.25)),
        "lead_sd": float(r.uniform(0.08, 0.25)),         # as a share of the mean
        "fill": float(r.uniform(0.85, 1.0)),
        "late_chance": float(r.uniform(0.0, 0.15)),
        # how customers judge a price against what they are used to paying
        "loss": float(r.uniform(0.3, 0.6)),               # extra price weight when above the reference, as a share of beta
    }
    # how much more a price above the reference hurts than the same price below it helps (v0.2: 2 for every product)
    t["loss_aversion"] = float(r.uniform(1.4, 2.2))
    # The three suppliers. A is the one above. B and C differ by product: drawn after everything else, so the facts
    # above are the same as before suppliers existed.
    sup = {"A": {k: t[k] for k in ("defect", "lead_mean", "lead_sd", "fill", "late_chance")}}
    rb = rng_for(secret, "supplier", pid)
    from .catalog import supplier as quote
    sup["B"] = {"defect": _clip(t["defect"] * rb.choice([1.1, 1.8, 2.6, 3.5]), 0.004, 0.2),
                "lead_mean": float(quote(pid, "B")["lead"] * rb.uniform(1.0, 1.3)),
                "lead_sd": float(min(0.45, t["lead_sd"] + rb.uniform(0.0, 0.1))),
                "fill": _clip(t["fill"] - rb.uniform(0.0, 0.1), 0.7, 1.0),
                "late_chance": _clip(t["late_chance"] + rb.uniform(0.02, 0.12), 0.0, 0.3)}
    sup["C"] = {"defect": _clip(t["defect"] * rb.choice([0.25, 0.4, 0.6, 0.9]), 0.002, 0.1),
                "lead_mean": float(quote(pid, "C")["lead"] * rb.uniform(1.0, 1.12)),
                "lead_sd": float(max(0.05, t["lead_sd"] - rb.uniform(0.0, 0.08))),
                "fill": _clip(t["fill"] + rb.uniform(0.0, 0.08), 0.85, 1.0),
                "late_chance": _clip(t["late_chance"] * rb.uniform(0.2, 0.6), 0.0, 0.2)}
    t["suppliers"] = sup
    t.update(lever_truth(secret, pid))
    return t


def lever_truth(secret: str, pid: str) -> dict:
    """Hidden facts of the four growth levers (REFERENCE.md, "Growing the company"), drawn from their own stream
    so that the facts above do not move."""
    r = rng_for(secret, "levers", pid)
    repeat_product = pid in REPEAT
    out = {
        # developing an own improved version of the product
        "rnd": {"chance": float(r.uniform(0.35, 0.9)),       # chance that a development project works
                "appeal": float(r.uniform(-0.05, 0.75)),      # utility a shopper gives the improved version
                "beta_mult": float(r.uniform(0.7, 1.0)),      # price sensitivity of its buyers, as a share
                "stars": float(r.uniform(-0.1, 0.45)),        # change in mean review stars
                "cost_mult": float(r.uniform(1.06, 1.35)),    # its supplier price, as a share of the old one
                "months": int(r.integers(4, 9))},             # months from start to result
        # selling to retail chains
        "wholesale": {"price": float(r.uniform(0.36, 0.52)),   # share of the typical price chains pay, net of trade spend
                      "appetite": float(r.uniform(0.2, 1.6)),   # how often and how much chains want it
                      "chargeback": float(r.uniform(0.01, 0.08)),   # share of invoices deducted (penalties, shortages)
                      "unsold": float(r.uniform(0.0, 0.45)),    # share of the chain's stock left at the end sent back
                      "cannibal": float(r.uniform(0.0, 0.20)),  # share of own site and marketplace demand lost
                      "exposure": float(r.uniform(0.0, 0.8))},  # people who notice the brand per unit on a shelf
        # subscribe and save on the own site
        "subs": {"take": float(r.uniform(0.12, 0.50) if repeat_product else r.uniform(0.0, 0.05)),   # at 10% off
                 "elastic": float(r.uniform(0.3, 1.4)),     # how take-up grows with the discount
                 # share who leave at each renewal once they have stayed a while (from the fifth renewal), with
                 # failed cards; the first renewals lose more (churn_first, below)
                 "churn": float(r.uniform(0.065, 0.105) if repeat_product else r.uniform(0.12, 0.25)),
                 "rate": float(SUB_RATE.get(pid, 2.0) * r.uniform(0.8, 1.2))},   # orders a year
    }
    # v0.3 facts, drawn after the ones above so they do not move
    w = out["wholesale"]
    w["sell"] = float(r.uniform(0.5, 1.3))     # chain shoppers' monthly purchases, as a share of the units on the shelf
    w["halo"] = float(r.uniform(0.03, 0.14))   # own site and marketplace demand gained from being on shelves
    out["subs"]["churn_first"] = float(r.uniform(0.215, 0.315) if repeat_product else r.uniform(0.25, 0.40))
    return out


def bundle_truth(secret: str, a: str, b: str) -> dict:
    """How a two-product bundle on the own site does: attach, the share of a's site orders that add b (and
    the other way round) at 15% off; overlap, the share of those that would have bought both anyway."""
    a, b = sorted((a, b))
    r = rng_for(secret, "bundle", a, b)
    same = PRODUCTS[a]["category"] == PRODUCTS[b]["category"]
    return {"attach": float(r.uniform(0.04, 0.22) if same else r.uniform(0.0, 0.06)),
            "overlap": float(r.uniform(0.15, 0.8))}


POTENTIAL_TARGET = 1_000_000.0    # yearly profit potential of the best 8 products, in every run


def potential(pid: str, t: dict) -> float:
    """A rough yearly profit of the product on the marketplace alone, at its best price: shoppers who search,
    the share who see the listing against two rivals, conversion, and the margin after fees, freight, returns
    and defects, with the repeat purchases of those buyers over the next two years (v0.3). It ranks runs much as
    the tuned rules' results do (v0.2: correlation 0.93 over six runs). Over the 30 products of a run it ranks
    them about as the tuned rules' single-product results do (rank correlation about 0.7)."""
    from .catalog import fees, landed
    p = PRODUCTS[pid]
    sz = fees(pid)
    best = 0.0
    for k in np.linspace(0.7, 1.6, 10):
        pr = p["price"] * k
        e = math.exp(-t["beta"] * (pr - p["price"]))
        conv = t["conv_mp"] * e / (1 + t["conv_mp"] * (e - 1))
        units = t["m"] * t["search_rate"] * 0.33 * 1.3 * conv
        unit = pr * 0.85 - sz["mp_fulfill"] - landed(pid) - t["returns"] * (pr + sz["return_ship"]) - t["defect"] * pr
        best = max(best, units * unit * 365 * (1 + 2 * t["repeat"]))
    return best


def run_truth(secret: str) -> dict:
    r = rng_for(secret, "run")
    products = {pid: product_truth(secret, pid) for pid in PRODUCTS}
    # Every run gets the same total potential: market sizes are scaled so the best 8 products' potential is the
    # same. Which products are good, and why, still differs by run. (Without this the tuned rules' median multiple
    # ranged from 0.6 to 4.6 over six runs.)
    top = sum(sorted((potential(pid, t) for pid, t in products.items()), reverse=True)[:8])
    f = POTENTIAL_TARGET / top if top > 0 else 1.0
    for t in products.values():
        for k in ("m", "ad_half", "mp_half"):
            t[k] *= f
    return {
        "products": products,
        # people overstate purchase intent in surveys by this much on the logit scale
        "survey_bias": float(r.uniform(0.8, 1.8)),
        # how much more than the company's value today (the grading formula, with this year's profit pace) investors
        # pay in this run's market, before the world's own mood and the company's growth
        "investor_premium": float(rng_for(secret, "investors").uniform(0.7, 1.4)),
        "bundles": {f"{a}+{b}": bundle_truth(secret, a, b) for a in PRODUCTS for b in PRODUCTS if a < b},
        # the run's key for facts drawn later from their own streams (countries.country_truth), not the secret itself
        "key": hashlib.sha256(f"{secret}/key".encode()).hexdigest(),
    }
