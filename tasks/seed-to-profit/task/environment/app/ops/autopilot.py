"""The company's operating software. The grader calls step(obs) once per simulated day and does what it
returns (docs/REFERENCE.md, "The software"). This is a minimal example that runs: it keeps prices at the
typical price, spends nothing on ads, and reorders the minimum when stock runs low. Replace it.
"""

SETUP = {}


def init(setup):
    SETUP.clear()
    SETUP.update(setup)


def step(obs):
    act = {"prices": {}, "ads": {}, "orders": []}
    for pid, p in obs["products"].items():
        if not p["active"]:
            continue
        info = SETUP["products"].get(pid) or SETUP["catalog"][pid]
        act["prices"][pid] = {ch: info["price"] for ch in p["channels"]}
        act["ads"][pid] = {"social": 0, "sponsored": 0}
        inbound = sum(i["qty"] for i in p["inbound"])
        if p["on_hand"] + inbound < info["moq"] // 2:
            act["orders"].append({"product": pid, "qty": info["moq"]})
    return act
