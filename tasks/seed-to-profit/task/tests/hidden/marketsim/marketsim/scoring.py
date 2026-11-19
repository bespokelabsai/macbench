"""The score (docs/REFERENCE.md, "How you are scored").

    m_w      = company value in world w / seed money - research spent / seed money
    world_w  = 0 if bankrupt, else min(1, max(0, log2(m_w) / 5))
    score    = gate * mean(world_w) * (0.85 + 0.15 * F)

F is the forecast score: the team gives a probability for each event in FORECAST_EVENTS, and each is
scored 1 - 4 e^2, where e is how far it misses the share of graded worlds where the event happened,
beyond one binomial standard error. An honest forecast is the best one. (v0 used weight 0.1 and two
standard errors; the forecast then moved scores by at most 0.015, too little to say anything. v0.1 and v0.2 added
0.15 * F * the share of worlds above 1x, so a company that ended at 1.05x everywhere and forecast that scored about
0.165, twice the plain rules: forecasting a dull outcome paid more than running a business. From v0.3 the forecast
scales the business score instead, up to 15% of it.)
Each doubling of the seed money adds 0.2: keeping the seed (m = 1) or losing money scores 0, m = 2 scores 0.2,
m = 4 scores 0.4, and a company worth 32 times its seed money 1. That leaves room above the best fixed policies and the
best teams (v1.1 used 16 times as the top; Opus 5.5 at max effort reached about 7 times, 0.70, too near the top of the
scale; v1.2 moves the top to 32 times, which multiplies every score below the top by 0.8). (v0 and v0.1 used 0.2 min(1, m) + 0.8 (1 - exp(-(m - 1))), which gave 0.92 at m = 3.3:
good teams were near the top of the scale with plenty of value still to make.)
"""
import math

from .catalog import RULES

TOP = 32.0      # the multiple that scores 1
FORECAST_WEIGHT = 0.15
FORECAST_SE = 1.0

FORECAST_EVENTS = {
    "survive": "the company is not bankrupt at the end of the three years",
    "value_2x": "company value at the end is more than 2 times the seed money",
    "value_4x": "company value at the end is more than 4 times the seed money",
    "profit_y3": "operating profit in the third year is positive",
    "revenue_y1_1m": "revenue in the first year is more than $1M",
    "revenue_y3_3m": "revenue in the third year is more than $3M",
}


def multiple(value, research_spent):
    seed = RULES["seed_money"]
    return value / seed - research_spent / seed


def world_score(res, research_spent):
    if res["bankrupt"]:
        return 0.0
    m = multiple(res["value"], research_spent)
    return min(1.0, max(0.0, math.log(m) / math.log(TOP))) if m > 0 else 0.0


def events(res):
    years = res.get("years") or []
    year = {y["year"]: y for y in years if y.get("days") == 365}
    rev_y1 = year.get(1, {}).get("revenue", 0.0)
    rev_y3 = year.get(3, {}).get("revenue", 0.0)
    prof_y3 = year.get(3, {}).get("profit", 0.0)
    seed = RULES["seed_money"]
    alive = not res["bankrupt"]
    return {"survive": alive, "value_2x": alive and res["value"] > 2 * seed,
            "value_4x": alive and res["value"] > 4 * seed, "profit_y3": alive and prof_y3 > 0,
            "revenue_y1_1m": rev_y1 > 1e6, "revenue_y3_3m": alive and rev_y3 > 3e6}


def forecast_score(forecast, results):
    """forecast: {event: probability}. Missing or invalid entries score 0."""
    n = len(results)
    if not n:
        return 0.0, {}
    per = {}
    for ev in FORECAST_EVENTS:
        hits = sum(1 for r in results if events(r)[ev])
        freq = hits / n
        p = forecast.get(ev) if isinstance(forecast, dict) else None
        try:
            p = float(p)
        except (TypeError, ValueError):
            p = None
        if p is None or not 0 <= p <= 1 or math.isnan(p):
            per[ev] = {"forecast": None, "observed": freq, "score": 0.0}
            continue
        se = math.sqrt(max(p * (1 - p), 1e-9) / n)
        e = max(0.0, abs(p - freq) - FORECAST_SE * se)
        per[ev] = {"forecast": p, "observed": freq, "score": max(0.0, 1 - 4 * e * e)}
    return sum(v["score"] for v in per.values()) / len(per), per


def score(results, research_spent, forecast, gate=True):
    ws = [world_score(r, research_spent) for r in results]
    mean_w = sum(ws) / len(ws) if ws else 0.0
    f, per = forecast_score(forecast, results)
    gained = sum(1 for r in results if not r["bankrupt"] and multiple(r["value"], research_spent) > 1) / max(1, len(results))
    total = mean_w * ((1 - FORECAST_WEIGHT) + FORECAST_WEIGHT * f) if gate else 0.0
    return {"score": total, "mean_world": mean_w, "worlds": ws, "forecast": f, "forecast_detail": per,
            "gained_share": gained}
