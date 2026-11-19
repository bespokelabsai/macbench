"""Reference software: plain rules from public facts only (no research, no learning, no growth levers).

Every product sells at its typical price on both channels with $30 a day of sponsored ads, keeps 30 days of sales
in stock beyond the supplier's quoted lead time, reorders 60 days of sales at a time, and repays the credit line
when cash is plentiful. Sales history is kept in setup["state_dir"], so the rules carry across the live company's
months. It is an honest baseline, not a ceiling.
"""
import json
import os

SETUP = {}
HIST = {}
COVER, BATCH, FIRST, SPONSORED = 30, 60, 2, 30.0


def _state():
    return os.path.join(SETUP.get("state_dir") or ".", "reference-history.json")


def init(setup):
    SETUP.clear()
    SETUP.update(setup)
    HIST.clear()
    try:
        HIST.update(json.load(open(_state())))
    except (OSError, ValueError):
        pass


def _info(pid):
    return SETUP["products"].get(pid) or SETUP["catalog"][pid]


def _floor(info):
    landed = info.get("landed") or info["cost"] + info["freight"]
    return 1.2 * landed + info["fees"]["mp_fulfill"]


def step(obs):
    act = {"prices": {}, "ads": {}, "orders": []}
    rules = SETUP["rules"]
    spare = obs["cash"] + obs["credit_limit"] - obs["credit_used"]
    for pid, po in obs["products"].items():
        if not po["active"]:
            continue
        info = _info(pid)
        y = po["yesterday"]
        units = sum(y.get(ch, {}).get("units", 0) for ch in ("site", "marketplace"))
        units += (y.get("subscription") or {}).get("orders", 0)
        hist = HIST.setdefault(pid, [])
        if obs["day"] > 0:
            hist.append(units)
            del hist[:-28]
        price = max(_floor(info), info["price"])
        act["prices"][pid] = {ch: round(price, 2) for ch in po["channels"]}
        act["ads"][pid] = {"social": 0.0, "sponsored": SPONSORED}
        moq = (po.get("supplier_quotes") or {}).get(po.get("supplier", "A"), {}).get("moq", info["moq"])
        if obs["day"] == 0:
            act["orders"].append({"product": pid, "qty": int(FIRST * moq)})
            continue
        rate = sum(hist) / max(1, len(hist))
        if obs["day"] < 28:
            rate = max(rate, FIRST * info["moq"] / 90)
        inbound = sum(i["qty"] for i in po["inbound"])
        if po["on_hand"] + inbound < rate * (po["lead_quote"] + COVER) and rate > 0 \
                and obs["days_left"] > po["lead_quote"] + 20:
            qty = max(moq, int(rate * BATCH))
            cost = qty * po["unit_cost_now"]
            if cost * rules["deposit"] < 0.5 * spare:
                act["orders"].append({"product": pid, "qty": qty})
                spare -= cost
    if obs["cash"] > 400_000 and obs["credit_used"] > 0:
        act["credit"] = {"repay": obs["credit_used"]}
    try:
        with open(_state(), "w") as f:
            json.dump(HIST, f)
    except OSError:
        pass
    return act
