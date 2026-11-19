"""Scripted reference controller: plays through the public API only, like a team would.

    python3 bot.py --index I                seat I (seat 0 works FLOW, the others stay idle)
    python3 bot.py --idle                   start the day and do nothing

It works the flow position: when the holds are full and thunderstorms are near, it gives short-haul flights a
ground delay at their origin instead of letting them hold over Dubai. It works no traffic sector. In the oracle run
without the harness, the image's entrypoint adds the rule-based controller (environment/hub/server/standin.py) for
every sector no seat has open. On the harness hub there is no such controller, so the traffic sectors have none. It
is a reference, not a strong team.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atc import call, act, query  # noqa: E402


class Bot:
    def __init__(self, index):
        self.index = index

    def sectors(self):
        return ["FLOW"] if self.index == 0 else []

    def run(self):
        call("status")
        want = self.sectors()
        for _ in range(120):    # the day may still be starting: try again until the sectors are open
            if not want:
                break
            r = act([{"op": "open", "sectors": want}])
            print("open", want, r, flush=True)
            if r.get("ok") and all(x.get("ok") for x in r.get("result", {}).get("results", [])):
                break
            time.sleep(2)
        down = 0
        while True:
            try:
                self.tick(want)
                down = 0
            except OSError as e:
                down += 1
                print("tick error", type(e).__name__, e, flush=True)
                if down > 120:
                    return       # the server has been gone for two minutes: the day is over
            except Exception as e:  # noqa: BLE001 - keep playing
                print("tick error", type(e).__name__, e, flush=True)
            time.sleep(1.0)

    def tick(self, mine):
        st = call("status")
        if not st.get("ok") or st["status"]["phase"] != "running":
            time.sleep(2)
            return
        if "FLOW" not in mine:
            return
        ops = self.flow(query("traffic")["rows"], st["status"]["t"])
        if ops:
            print("flow", act(ops[:20]), flush=True)

    def flow(self, traffic, now):
        holding = sum(1 for r in traffic if r["cleared"]["mode"] == "hold")
        if holding < 12:
            return []
        wx = query("weather")
        if not any("TS" in m for m in wx["recent_metars"][-3:]) and not wx["weather_radar"]["squares"]:
            return []
        fl = query("flights", where={"kind": "ARR", "phase": "scheduled"})["rows"]
        ops = []
        for f in fl:
            if f.get("origin_dep_t") and not f.get("edct_t") and now + 900 < f["origin_dep_t"] < now + 5400:
                ops.append({"op": "edct", "flight": f["id"], "time": f["origin_dep_t"] + 1800})
            if len(ops) >= 6:
                break
        return ops


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", type=int, default=0)
    ap.add_argument("--idle", action="store_true")
    a = ap.parse_args()
    if a.idle:
        print(call("status"), flush=True)
        while True:
            time.sleep(60)
    for _ in range(60):
        try:
            call("status")
            break
        except OSError:
            time.sleep(2)   # the server is still starting
    Bot(a.index).run()


if __name__ == "__main__":
    main()
