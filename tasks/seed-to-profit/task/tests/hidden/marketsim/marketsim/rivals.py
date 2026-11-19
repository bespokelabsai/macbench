"""Rival sellers and how they set prices. Every rival sets its price once a week.

- fixed: keeps its price, with a holiday sale in late November.
- follower: sets the price that is best against our price of last week (a lagged best response).
- learner: learns its price by Q-learning on its weekly profit, with our last week's price as the state,
  in the style of Calvano, Calzolari, Denicolo and Pastorello (2020, AER). Some learn fast, some slowly.
- undercutter: when we are cheaper, it prices just below us, down to its cost; otherwise it drifts back.

Every rival also has weekly fixed costs and some cash. A rival that keeps losing money runs through its cash,
and once the cash is gone it may close its listing (tricks.py). New sellers can come in later.

All randomness comes from the world's generator, so a world is the same every time it is run.
"""
import math

GRID = (0.70, 0.78, 0.86, 0.94, 1.00, 1.06, 1.14, 1.22, 1.30)   # price as a share of the typical price
BUCKETS = (0.80, 0.92, 1.04, 1.16)                                  # our price over the typical price


def _bucket(ratio):
    if ratio is None:
        return len(BUCKETS) + 1        # we do not sell in this market
    for i, b in enumerate(BUCKETS):
        if ratio < b:
            return i
    return len(BUCKETS)


class Rival:
    def __init__(self, spec: dict, typical: float):
        self.spec = dict(spec)
        self.kind = spec["kind"]
        self.typical = typical
        self.cost = spec["cost"]
        self.base = spec["price"]
        self.price = spec["price"]
        self.appeal = spec["appeal"]
        self.rating = spec["rating"]
        self.visibility = spec["visibility"]
        self.own_demand = spec["own_demand"]
        self.q = None
        self.state = None
        self.action = None
        self.sales_week = 0.0
        self.profit_week = 0.0
        self.fixed_week = 0.0     # weekly fixed costs, set from the market's size when the line starts
        self.cash = 0.0
        self.gone = False         # closed its listing

    def grid_prices(self):
        floor = self.cost * 1.05
        return [max(floor, g * self.typical) for g in GRID]

    def weekly(self, day: int, our_price, profit_fn, rng, month: int, mday: int):
        """Set this week's price. our_price is our average price last week in this market (None if we do
        not sell there). profit_fn(price, our_price) is the rival's expected weekly profit at a price."""
        if self.kind == "fixed":
            self.price = self.base * (0.85 if (month == 11 and mday >= 20) else 1.0)
        elif self.kind == "follower":
            prices = self.grid_prices()
            self.price = max(prices, key=lambda p: profit_fn(p, our_price))
        elif self.kind == "undercutter":
            if our_price is not None and our_price < self.price:
                self.price = max(self.cost * 1.08, our_price * 0.97)
            else:
                self.price += 0.05 * (self.base - self.price)
        elif self.kind == "learner":
            self._learn(day, our_price, profit_fn, rng)
        self.price = float(min(max(self.price, self.cost * 1.02), self.typical * 1.6))

    def _learn(self, day, our_price, profit_fn, rng):
        prices = self.grid_prices()
        n_states = len(BUCKETS) + 2
        scale = max(1.0, profit_fn(self.base, None))
        if self.q is None:
            # Start from what each price would earn against a seller at the typical price.
            self.q = [[profit_fn(p, self.typical) / scale for p in prices] for _ in range(n_states)]
        s = _bucket(None if our_price is None else our_price / self.typical)
        if self.state is not None and self.action is not None:
            reward = self.profit_week / scale
            target = reward + 0.85 * max(self.q[s])
            self.q[self.state][self.action] += self.spec["speed"] * (target - self.q[self.state][self.action])
        eps = max(0.03, 0.15 * math.exp(-day / 365.0))
        if rng.random() < eps:
            a = int(rng.integers(0, len(prices)))
        else:
            row = self.q[s]
            a = max(range(len(prices)), key=lambda i: row[i])
        self.state, self.action = s, a
        self.price = prices[a]
