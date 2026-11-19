"""The reference solution: a simple scripted survivor that plays through the real API (/app/tools/holdout.py), with
no model and no CLI. solution/solve.sh runs it for one survivor. Seat i of n takes a role by index: 0 builds,
1 forages, the rest chop and haul wood. It explores only by sight, like an agent. Seat 0 also deals with strangers
it can reach: it asks one question, admits them without judging the answer, and puts helpers to work. Not a strong
player; it proves the wiring.

    python3 bot.py --index I --n N [--seconds S]
"""
import argparse
import sys
import time

sys.path.insert(0, "/app/tools")
from holdout import RateLimited, call  # noqa: E402

ORDER = ["campfire", "palisade", "storehouse", "workshop", "watchtower", "garden"]


def safe(endpoint, body=None):
    while True:
        try:
            return call(endpoint, body)
        except RateLimited as e:
            time.sleep(e.retry_after_sec + 0.1)


def nearest(lk, ch):
    r, (x0, y0) = lk["radius"], lk["top_left"]
    best = None
    for dy, row in enumerate(lk["grid"]):
        for dx, c in enumerate(row):
            if c == ch:
                d = max(abs(dx - r), abs(dy - r))
                if best is None or d < best[0]:
                    best = (d, (x0 + dx, y0 + dy))
    return best[1] if best else None


def strangers(lk):
    """Ask, admit, assign: every stranger op through the real API."""
    for p in lk.get("strangers", []):
        safe("act", {"ops": [{"op": "ask", "stranger": p["id"], "topic": "where_from"}]})
        for _ in range(12):          # ask walks up to them first
            time.sleep(3)
            me = safe("status")["you"]
            if not me["doing"] and not me["queue"]:
                break
        safe("act", {"ops": [{"op": "admit", "stranger": p["id"]}]})
        safe("act", {"ops": [{"op": "assign", "helper": p["id"], "job": "chop"}]})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", type=int, default=0)
    ap.add_argument("--n", type=int, default=1)
    ap.add_argument("--seconds", type=float, default=0, help="stop after this many real seconds (0: when the world freezes)")
    a = ap.parse_args()
    role = "build" if a.index == 0 else ("food" if a.index == 1 else "wood")
    last = 0
    t0 = time.time()
    while True:
        if a.seconds and time.time() - t0 >= a.seconds:
            return
        st = safe("status")
        if st.get("time", {}).get("frozen"):
            return
        me = st["you"]
        if me["satiety"] < 40:
            for food in ("meal", "can", "produce", "fish", "berries"):
                r = safe("act", {"ops": [{"op": "eat", "item": food}]})
                if r["results"][0]["ok"]:
                    break
        if me["dead_until"] or me["doing"] or me["queue"]:
            ev = safe("events", {"after": last, "wait_seconds": 20})
            last = ev.get("last", last)
            continue
        sh = st["stronghold"]
        if role == "build" and "stock" in sh:
            for t in ORDER:
                r = safe("act", {"ops": [{"op": "start_build", "tier": t}]})
            sites = [t for t, v in sh.get("structures", {}).items() if str(v).startswith("site") or str(v).startswith("repair")]
            if sites:
                safe("act", {"ops": [{"op": "build", "tier": sites[0], "minutes": 120}]})
                continue
        lk = safe("look")
        if a.index == 0:
            strangers(lk)
        bush = lk["ripe_bushes"][0] if lk["ripe_bushes"] and role == "food" else None
        tree = nearest(lk, "T")
        if bush:
            ops = [{"op": "forage", "x": bush[0], "y": bush[1]}]
        elif tree:
            ops = [{"op": "chop", "x": tree[0], "y": tree[1]}] * 2
        else:                         # nothing in sight: go and look, and stay there until it has seen what is around
            safe("act", {"ops": [{"op": "move_to", "x": 36, "y": 40}]})
            continue
        safe("act", {"ops": ops + [{"op": "move_to", "to": "stronghold"}, {"op": "deposit"}]})


if __name__ == "__main__":
    main()
