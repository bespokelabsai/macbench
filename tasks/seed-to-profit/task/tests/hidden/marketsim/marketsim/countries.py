"""Countries the company can enter .

The company starts in the US. It can enter any country below during the run: entering costs money and takes time
(tax registration, compliance, the marketplace account, translation), then the country is a market of its own, with
its own shoppers, rivals, marketplace fees and payouts, ad prices, shocks and currency. Cash, credit, debt and
ownership stay with the one company.

COUNTRIES is the one place for the countries' numbers. Each row is public (a company can look these up), and
calibration/countries/countries.md has the sources; "est." in a note means our own estimate where we found no good
source. The model that turns a row into a market:

- Shoppers: each product's market size is the US one times `size` (e-commerce sales against the US, over the
  square root of the price level, since a dollar buys more units where goods are cheaper, times `reach` where
  foreign sellers reach only part of the market), times a hidden factor per product and country, drawn per run
  (about 0.7 to 1.4 times: what sells in one country sells less in another).
- Prices: shelf prices include VAT. The typical price is the US one times the square root of the price level (goods
  that cross borders differ less in price than the whole basket) times 1 + VAT. Revenue is counted without VAT.
  Rivals' prices start at the same share of the typical price as in the US; their costs follow their own landed
  cost (the country's duty and freight, as ours) and the price net of VAT.
- Landed cost: the duty-free supplier price (the catalog's supplier prices include the US duty, US_DUTY) times 1 +
  the country's duty, plus the catalog freight times 0.4 + 0.6 times the country's sea freight rate against the US
  (port, customs and trucking do not move with the sea rate). Imports into a warehouse are bulk shipments: de
  minimis thresholds do not apply, and import VAT is reclaimed. Stock moved between warehouses pays the
  destination's duty and tariffs again, except within the EU, which has one external tariff (its tariff shocks are
  drawn once for every member).
- Lead time: the US quote less the 14 days of the sea leg to the US West Coast plus the country's sea days.
- Fulfillment, storage and return shipping: the US fees times `fulfil`. Referral fee, payout cycle and hold, return
  rates (times `returns`), social ad prices (cost per click, times `ads`) and sponsored ad prices (times
  `sponsored`) from the row.
- Currency: the real exchange rate (the local currency's value in dollars, after inflation) follows a random walk
  with the yearly spread `fx_sd`, plus now and then a sudden drop. Money is in dollars. The company sets prices in
  dollars; local prices (rivals', and what shoppers are used to paying) follow half of a currency move (FX_PASS:
  rivals that import pay in dollars too), so a weaker local currency makes the company's prices dearer there.
  Local costs (fulfillment, storage, ads) are in the local currency, so they get cheaper in dollars. Countries with
  one currency share it.
- Shocks: the world of each country is its own (recession, fee rise, tariffs, port delays, rivals, hidden changes),
  seeded from the live world's seed and the country. Global ones reach every country: a recession in the US is a
  world recession half of the time, the social ad price change, product life cycles, a supplier closing, and the
  office's announced change.
"""
import math

from .catalog import PRODUCTS
from .truth import rng_for

US_ECOM = 1083.3      # US B2C e-commerce, USD billion (ECDB 2025)
US_DUTY = 0.158       # US duty on the supplier price of goods from China, in the catalog's figures: MFN about 3.3%
                      # plus the 12.5% Section 301 tariff of July 2026 (goods on no older Section 301 list). Whether it
                      # stacks on the older lists is open (calibration/countries/verification.md)
US_SEA_DAYS = 14      # sea days to the US West Coast; the catalog's lead times include them
FX_DROP = (1.0, 2.0)  # a sudden drop takes this many times the currency's yearly spread off its value
PEG_DROP = (0.15, 0.30)

# Duties are the WTO's MFN applied simple averages for non-agricultural goods, 2024 (World Tariff Profiles, series
# TP_A_0430), which goods from China pay, as the US figure is MFN plus the China tariff: EU 4.1%, UK 2.9%, Japan 2.4%,
# Korea 6.5%, Canada 2.0%, Indonesia 7.9%, Saudi Arabia 5.7%, UAE 4.6%. Where China pays more (India, Mexico,
# Brazil, Turkey) or less (Australia, by its FTA) the row's note says so. Our goods' own lines are often higher
# (6.5% to 12% in the EU for plastics, bags, ceramics): an average, not a line rate (no exact public source).
# code: name, currency, ecom (USD bn), vat, duty (on the duty-free supplier price), ocean (sea freight rate against the US
# West Coast), sea (sea days from Shanghai), referral, fulfil, payout (days between payouts), hold (days a sale is
# held), returns, ads, sponsored, level (price level, household consumption PPP over FX), fx_sd (yearly spread of the
# real exchange rate), entry (USD, one time), days (to the first listing), month (USD per 30 days once entered),
# listing (USD per product line listed), english, eu (one customs union and one currency area for transfers), south
# (seasons flipped for outdoor goods), reach (share of the market foreign sellers reach), capital (USD a local company
# must hold as paid-up capital: it stays the company's money, but entering needs it free)
COUNTRIES = {
    "GB": dict(name="United Kingdom", currency="GBP", ecom=177.0, vat=0.20, duty=0.029, ocean=0.49, sea=32,
               referral=0.15, fulfil=1.0, payout=14, hold=10, returns=0.9, ads=0.81, sponsored=0.48, level=0.92,
               fx_sd=0.063, entry=8000, days=42, month=2500, listing=300, english=True,
               note="VAT 20%, no threshold for non-established sellers; Amazon.co.uk 15%, low-price FBA GBP 2.7; "
                    "sponsored CPC est. (DE proxy); entry and monthly costs est."),
    "DE": dict(name="Germany", currency="EUR", ecom=124.2, vat=0.19, duty=0.041, ocean=0.49, sea=33,
               referral=0.15, fulfil=1.1, payout=14, hold=10, returns=1.3, ads=0.84, sponsored=0.48, level=0.81,
               fx_sd=0.075, entry=15000, days=77, month=4000, listing=1000, eu=True,
               note="VAT agent EUR 300 + 49-119/mo, GPSR EUR 150-500/yr, packaging EUR 350/yr, 8-14 weeks to the first "
                    "sale; returns highest in Europe (factor est.); translation USD 0.08-0.25/word"),
    "FR": dict(name="France", currency="EUR", ecom=79.2, vat=0.20, duty=0.041, ocean=0.49, sea=33,
               referral=0.15, fulfil=1.5, payout=14, hold=10, returns=0.9, ads=0.67, sponsored=0.45, level=0.83,
               fx_sd=0.075, entry=18000, days=77, month=4500, listing=1000, eu=True,
               note="fiscal rep and EPR (about EUR 5.5-6k a year) for non-EU sellers; FBA the dearest in the EU"),
    "ES": dict(name="Spain", currency="EUR", ecom=56.4, vat=0.21, duty=0.041, ocean=0.55, sea=33,
               referral=0.15, fulfil=1.2, payout=14, hold=10, returns=0.9, ads=0.58, sponsored=0.31, level=0.69,
               fx_sd=0.075, entry=18000, days=77, month=4000, listing=1000, eu=True,
               note="fiscal rep for non-EU sellers; Genoa as the Mediterranean freight proxy"),
    "IT": dict(name="Italy", currency="EUR", ecom=43.4, vat=0.22, duty=0.041, ocean=0.55, sea=33,
               referral=0.15, fulfil=1.3, payout=14, hold=10, returns=0.9, ads=0.62, sponsored=0.31, level=0.73,
               fx_sd=0.075, entry=18000, days=77, month=4000, listing=1000, eu=True,
               note="fiscal rep for non-EU sellers"),
    "NL": dict(name="Netherlands", currency="EUR", ecom=23.8, vat=0.21, duty=0.041, ocean=0.49, sea=31,
               referral=0.12, fulfil=1.15, payout=14, hold=10, returns=1.1, ads=0.72, sponsored=0.72, level=0.87,
               fx_sd=0.075, entry=15000, days=77, month=4000, listing=1000, eu=True,
               note="a local marketplace leads (5-12.5% plus a fixed fee); payout est."),
    "PL": dict(name="Poland", currency="PLN", ecom=32.3, vat=0.23, duty=0.041, ocean=0.49, sea=40,
               referral=0.11, fulfil=0.7, payout=14, hold=10, returns=1.0, ads=0.51, sponsored=0.51, level=0.55,
               fx_sd=0.084, entry=18000, days=56, month=4000, listing=1000, eu=True,
               note="a local marketplace leads (8-15%); fulfillment and payout est."),
    "JP": dict(name="Japan", currency="JPY", ecom=127.5, vat=0.10, duty=0.024, ocean=0.09, sea=4,
               referral=0.154, fulfil=0.52, payout=14, hold=10, returns=0.7, ads=0.78, sponsored=0.78, level=0.69,
               fx_sd=0.082, entry=20000, days=42, month=4000, listing=1000,
               note="importer of record and tax representative needed; FBA JPY 288 small; returns lowest in Asia"),
    "KR": dict(name="South Korea", currency="KRW", ecom=112.2, vat=0.10, duty=0.065, ocean=0.06, sea=4,
               referral=0.11, fulfil=0.74, payout=30, hold=30, returns=1.0, ads=0.73, sponsored=0.73, level=0.62,
               fx_sd=0.048, entry=20000, days=60, month=4000, listing=1000,
               note="the leading marketplace charges 4-10.9% plus 3.3% on shipping and settles slowly (up to 60 "
                    "days); fulfillment, returns and entry est."),
    "CN": dict(name="China", currency="CNY", ecom=2044.5, vat=0.091, duty=0.0, ocean=0.05, sea=7,
               referral=0.08, fulfil=0.68, payout=7, hold=15, returns=1.2, ads=0.6, sponsored=0.6, level=0.48,
               fx_sd=0.037, entry=40000, days=90, month=6000, listing=1000, reach=0.05,
               note="foreign brands sell cross-border through a bonded warehouse (9.1% cross-border tax, no duty); "
                    "the reach of cross-border sales, fees, ads and freight est.; the refundable deposit is left out"),
    "IN": dict(name="India", currency="INR", ecom=138.4, vat=0.18, duty=0.22, ocean=0.37, sea=18,
               referral=0.12, fulfil=0.54, payout=7, hold=14, returns=1.6, ads=0.27, sponsored=0.27, level=0.23,
               fx_sd=0.029, entry=40000, days=70, month=5000, listing=800, reach=0.5,
               note="an Indian entity or importer of record is needed, and marketplaces may not hold foreign "
                    "sellers' inventory (FDI rules): reach 0.5 est.; MFN average 13.0%, basic duty 20% plus surcharges "
                    "on many consumer goods (22% est.); returns high (cash on delivery); fulfillment est."),
    "ID": dict(name="Indonesia", currency="IDR", ecom=68.2, vat=0.11, duty=0.079, ocean=0.21, sea=7,
               referral=0.10, fulfil=0.57, payout=7, hold=10, returns=1.0, ads=0.56, sponsored=0.56, level=0.31,
               fx_sd=0.041, entry=40000, days=120, month=5000, listing=800, capital=150000,
               note="cross-border sales are closed: a local company (PT PMA) is needed, with IDR 2.5bn (about USD "
                    "150k) of paid-up capital since BKPM regulation 5/2025; VAT 11% in practice"),
    "CA": dict(name="Canada", currency="CAD", ecom=64.5, vat=0.12, duty=0.020, ocean=0.9, sea=19,
               referral=0.15, fulfil=1.2, payout=14, hold=10, returns=1.0, ads=0.77, sponsored=0.77, level=0.90,
               fx_sd=0.054, entry=8000, days=42, month=2500, listing=300, english=True,
               note="GST 5%, 5-15% with provinces (12% est. weighted); freight est. (US West Coast proxy)"),
    "MX": dict(name="Mexico", currency="MXN", ecom=44.0, vat=0.16, duty=0.25, ocean=0.84, sea=25,
               referral=0.13, fulfil=0.65, payout=7, hold=15, returns=1.0, ads=0.47, sponsored=0.47, level=0.59,
               fx_sd=0.093, entry=20000, days=75, month=4000, listing=1000,
               note="MFN average 6.8%, and tariffs of 5-50% on 1,463 lines from non-FTA origins incl. China since "
                    "1 Jan 2026 (25% est. for our goods); a tax ID or importer of record is needed"),
    "BR": dict(name="Brazil", currency="BRL", ecom=77.9, vat=0.25, duty=0.25, ocean=2.3, sea=32,
               referral=0.13, fulfil=0.5, payout=7, hold=15, returns=1.0, ads=0.47, sponsored=0.47, level=0.46,
               fx_sd=0.149, entry=40000, days=120, month=5000, listing=1000, south=True,
               note="MFN average 12.5% plus IPI (25% est.) and ICMS and PIS/COFINS on the price (25% est.); a "
                    "local entity is needed"),
    "AU": dict(name="Australia", currency="AUD", ecom=49.7, vat=0.10, duty=0.0, ocean=0.66, sea=21,
               referral=0.13, fulfil=1.3, payout=14, hold=10, returns=1.0, ads=0.77, sponsored=0.77, level=0.94,
               fx_sd=0.072, entry=8000, days=42, month=2500, listing=300, english=True, south=True,
               note="most Chinese goods duty free under ChAFTA (MFN average 2.6%); GST registration for stock held "
                    "in Australia"),
    "TR": dict(name="Turkey", currency="TRY", ecom=48.0, vat=0.20, duty=0.25, ocean=0.34, sea=22,
               referral=0.17, fulfil=0.65, payout=3, hold=7, returns=1.0, ads=0.45, sponsored=0.45, level=0.45,
               fx_sd=0.21, entry=20000, days=75, month=4000, listing=1000,
               note="MFN average 13.6% plus additional duty on non-FTA origins incl. China (25% est.); the lira's "
                    "yearly change has a spread "
                    "of 21% (and a mean fall of 32% a year, about its inflation: the real rate has no trend here)"),
    "SA": dict(name="Saudi Arabia", currency="SAR", ecom=11.4, vat=0.15, duty=0.057, ocean=0.28, sea=20,
               referral=0.14, fulfil=0.55, payout=14, hold=10, returns=1.0, ads=0.80, sponsored=0.80, level=0.50,
               fx_sd=0.0, entry=12000, days=60, month=3500, listing=1000,
               note="pegged to the dollar; a tax representative is needed; Jeddah freight before the 2026 Gulf spike"),
    "AE": dict(name="United Arab Emirates", currency="AED", ecom=8.1, vat=0.05, duty=0.046, ocean=1.63, sea=17,
               referral=0.15, fulfil=0.56, payout=14, hold=10, returns=1.0, ads=0.81, sponsored=0.81, level=0.69,
               fx_sd=0.0, entry=12000, days=45, month=3500, listing=800,
               note="pegged to the dollar; Gulf freight raised by the 2026 Hormuz disruption"),
}
DEFAULTS = dict(english=False, eu=False, south=False, reach=1.0, capital=0)
for _c in COUNTRIES.values():
    for _k, _v in DEFAULTS.items():
        _c.setdefault(_k, _v)

# Moving stock between warehouses: within the EU (one customs area) or between neighbours by road, and the rest by
# sea. Cost per unit as a share of the catalog freight per unit, and days. Our estimates (no good source).
ROAD = {frozenset(("US", "CA")), frozenset(("US", "MX")), frozenset(("CA", "MX"))} | \
    {frozenset(("GB", cc)) for cc in ("DE", "FR", "ES", "IT", "NL", "PL")}       # the UK and the EU: road and ferry
TRANSFER_ROAD = (0.5, 6)
TRANSFER_SEA = (1.5, 30)
FX_PASS = 0.5         # share of a currency move that local prices (rivals', what shoppers are used to) follow: rivals
                      # that import pay in dollars too, local sellers do not (our choice)


def row(cc):
    return COUNTRIES[cc]


def size(cc):
    c = COUNTRIES[cc]
    return c["ecom"] / US_ECOM * c["reach"] / math.sqrt(c["level"])


def price_factor(cc):
    c = COUNTRIES[cc]
    return math.sqrt(c["level"]) * (1 + c["vat"])


def freight_factor(cc):
    return 0.4 + 0.6 * COUNTRIES[cc]["ocean"]


def lead_shift(cc):
    return COUNTRIES[cc]["sea"] - US_SEA_DAYS


def transfer_terms(src, dst):
    """(cost per unit as a share of the catalog freight, days, duty applies) for moving stock from src to dst."""
    eu = lambda c: c != "US" and COUNTRIES[c]["eu"]  # noqa: E731
    if eu(src) and eu(dst):
        return TRANSFER_ROAD[0], TRANSFER_ROAD[1], False
    share, days = TRANSFER_ROAD if frozenset((src, dst)) in ROAD else TRANSFER_SEA
    return share, days, True


def duty(cc):
    return US_DUTY if cc == "US" else COUNTRIES[cc]["duty"]


def duty_free(cost):
    """The supplier price before duty: the catalog's supplier prices include the US duty."""
    return cost / (1 + US_DUTY)


def landed_cost(cc, pid, qty=0, s="A"):
    """Landed cost per unit of an order into country cc's warehouse: the duty-free supplier price times 1 + the
    country's duty, plus the catalog freight times the country's freight factor."""
    from .catalog import RULES, supplier
    q = supplier(pid, s)
    cost = q["cost"]
    if qty >= RULES["price_break_qty"] * q["moq"]:
        cost *= 1 - RULES["price_break"]
    return duty_free(cost) * (1 + COUNTRIES[cc]["duty"]) + q["freight"] * freight_factor(cc)


def public_table():
    """What setup["countries"] and docs/catalog.json show: each row with the numbers the model derives from it."""
    out = {}
    for cc, c in COUNTRIES.items():
        out[cc] = {k: c[k] for k in ("name", "currency", "vat", "duty", "referral", "payout", "hold", "entry", "days",
                                     "month", "listing", "eu", "south")}
        out[cc].update(ecommerce_usd_bn=c["ecom"], price_level=c["level"], currency_yearly_spread=c["fx_sd"],
                       market_size=round(size(cc), 4), price_factor=round(price_factor(cc), 4),
                       paid_up_capital=c["capital"],
                       freight_factor=round(freight_factor(cc), 4), lead_days_vs_us=lead_shift(cc),
                       fulfillment_factor=c["fulfil"], return_factor=c["returns"], social_ad_price_factor=c["ads"],
                       sponsored_ad_price_factor=c["sponsored"], english=c["english"])
    return out


# -- the hidden side: a country's product facts and its world ----------------------------------------------------

def country_truth(truth, cc):
    """The run's product facts in country cc: the same products, in a market of the country's size, with its
    prices, ad prices, returns and lead times. The per-product demand factor is drawn from the run's key."""
    c = COUNTRIES[cc]
    sz, pf, shift = size(cc), price_factor(cc), lead_shift(cc)
    key = truth.get("key", "run")
    out = dict(truth, products={})
    for pid, t in truth["products"].items():
        t = dict(t)
        r = rng_for(key, "country", cc, pid)
        f = sz * math.exp(r.normal(0.0, 0.25))
        for k in ("m", "ad_half", "mp_half"):
            t[k] = t[k] * f
        t["beta"] = t["eps"] / (PRODUCTS[pid]["price"] * pf)
        t["cpm"] = t["cpm"] * c["ads"]
        t["cpc_mp"] = t["cpc_mp"] * c["sponsored"]
        t["returns"] = min(0.4, t["returns"] * c["returns"])
        t["lead_mean"] = max(20.0, t["lead_mean"] + shift)
        t["suppliers"] = {s: dict(v, lead_mean=max(20.0, v["lead_mean"] + shift)) for s, v in t["suppliers"].items()}
        t["country_demand"] = f / sz
        out["products"][pid] = t
    return out


def fx_path(seed, currency, sd, days):
    """Daily log value of a currency in dollars, against day 0: a random walk with yearly spread sd."""
    if sd <= 0:
        return [0.0] * (days + 1)
    r = rng_for(seed, "fx", currency)
    step = sd / math.sqrt(365.0)
    x, out = 0.0, [0.0]
    for z in r.normal(0.0, step, days):
        x += float(z)
        out.append(x)
    return out


def country_world(us_world, cc):
    """The world of country cc, seeded from the live world's seed and the country: its own economy, rivals, shocks
    and hidden changes, the global events of the US world, and its currency."""
    from . import worlds
    c = COUNTRIES[cc]
    seed = us_world["seed"]
    w = worlds.make_world(f"{seed}/{cc}", us_world["shape"])
    w["country"] = cc
    if "luck_seed" in us_world:
        w["luck_seed"] = f"{us_world['luck_seed']}/{cc}"
    pf = price_factor(cc)
    from .catalog import landed
    for pid, lst in w["rivals"].items():
        # Rivals import the same goods: about half their cost with fees is the landed cost (at the country's duty and
        # freight), half follows the price net of VAT. Their prices and costs are in shelf terms, with VAT.
        ratio = 0.5 * math.sqrt(c["level"]) + 0.5 * landed_cost(cc, pid) / landed(pid)
        for spec in lst:
            spec["price"] *= pf
            spec["cost"] *= (1 + c["vat"]) * ratio
    g = rng_for(seed, "global")
    if us_world["recession"] is not None and g.random() < 0.5:
        w["recession"] = dict(us_world["recession"])      # a world recession
    w["platform"] = us_world["platform"]                  # the social ad platform is the same everywhere
    w["trends"] = us_world["trends"]                      # product life cycles and fads are global
    w["shocks"] = [s for s in w["shocks"] if s["kind"] != "supplier_closure"]   # suppliers are the US world's
    if c["eu"]:       # one external tariff for the EU: its tariffs are drawn once, for every member
        eu = worlds.make_world(f"{seed}/EU", us_world["shape"])
        w["shocks"] = sorted([s for s in w["shocks"] if s["kind"] != "tariff"] +
                             [dict(s) for s in eu["shocks"] if s["kind"] == "tariff"], key=lambda s: s["day"])
    if us_world.get("midrun"):
        w["midrun"] = dict(us_world["midrun"])
    days = 1095
    w["fx"] = fx_path(seed, c["currency"], c["fx_sd"], days)
    r = rng_for(seed, "fx-drop", c["currency"])
    w["fx_drops"] = []
    chance = min(0.5, 2.0 * c["fx_sd"]) if c["fx_sd"] > 0 else 0.03
    if r.random() < chance:
        lo, hi = (FX_DROP[0] * c["fx_sd"], FX_DROP[1] * c["fx_sd"]) if c["fx_sd"] > 0 else PEG_DROP
        w["fx_drops"].append({"day": int(r.integers(90, days - 60)), "size": float(min(0.5, r.uniform(lo, hi)))})
    return w
