"""The scripted reference player: no LLM, only the public API (/app/tools/game.py) and the team board.

    python3 bot.py --index I --n N [--idle]

It models what a player must do: it finds ore by flying and looking, tells teammates, builds drills and a belt back to
the core, and defends. With N=1 it does each job in turn; with N>1 seats split the jobs by index. --idle joins and does
nothing, for the idle baseline. It knows nothing the API does not show.
"""
import argparse
import collections
import math
import os
import sys
import time

sys.path.insert(0, os.environ.get("MMF_TOOLS", "/app/tools"))
from game import GameRateLimit, call, request  # noqa: E402

SOLID_OK = (None, "air")
ORDER = ["copper", "lead", "sand", "coal", "scrap"]
ORE = {"ore-copper": "copper", "ore-lead": "lead", "ore-coal": "coal", "ore-scrap": "scrap"}
COST = {"conveyor": 1, "mechanical-drill": 12, "duo": 35}  # copper, from the blocks collection
DIRS = [(1, 0, 0), (0, 1, 1), (-1, 0, 2), (0, -1, 3)]


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def api(endpoint, body=None):
    while True:
        try:
            r = call(endpoint, body)
        except GameRateLimit as e:
            time.sleep(e.retry_after_sec + 0.05)
            continue
        except OSError:
            time.sleep(1)
            continue
        return r


class Bot:
    def __init__(self, index, n):
        self.index, self.n = index, n
        self.tiles = {}       # (x, y) -> row, everything seen in this world
        self.fields = collections.defaultdict(set)   # resource -> ore tiles seen
        self.world = None
        self.core = None
        self.hints = {}       # resource -> field centre a teammate reported
        self.told = set()

    # -- reads ----------------------------------------------------------

    def status(self):
        while True:
            s = api("status").get("status") or {}
            if s.get("phase") == "running":
                if s.get("world_index") != self.world:
                    log("world", s.get("world_index"))
                    self.world = s["world_index"]
                    self.tiles.clear()
                    self.fields.clear()
                    self.hints.clear()
                self.core = s["core"]
                return s
            time.sleep(3)

    def look(self):
        """Read every tile in sight, in pages."""
        s = self.status()
        c, after = s["center"], 0
        r = min(int(s["sight_radius_tiles"]), 28)  # near the unit only: a whole-map read costs over 100 s of tokens
        area = {"x": int(c["x"]) - r, "y": int(c["y"]) - r, "w": 2 * r + 1, "h": 2 * r + 1}
        while after is not None:
            reply = api("query", {"of": "tiles", "area": area, "limit": 2000, "after": after,
                                  "fields": ["id", "x", "y", "floor", "ore", "block", "building"]})
            result = reply.get("result") or {"rows": [], "next": None}
            for t in result["rows"]:
                self.tiles[t["x"], t["y"]] = t
                kind = ORE.get(t.get("ore")) or ("sand" if t.get("floor") in ("sand-floor", "darksand") and not t.get("ore") else None)
                if kind and t.get("block") in SOLID_OK:
                    self.fields[kind].add((t["x"], t["y"]))
            after = result["next"]
        return s

    def share(self):
        """Tell teammates what this seat found, and read what they found. The board carries the map between seats."""
        if self.n == 1:
            return
        for kind, tiles in self.fields.items():
            if len(tiles) >= 6 and (self.world, kind) not in self.told:
                self.told.add((self.world, kind))
                x = sum(t[0] for t in tiles) // len(tiles)
                y = sum(t[1] for t in tiles) // len(tiles)
                request("/api/messages", {"text": f"field {self.world} {kind} {x} {y}"})
        inbox = request("/api/inbox?limit=100")
        for m in inbox if isinstance(inbox, list) else []:
            parts = str(m.get("text")).split()
            if len(parts) == 5 and parts[0] == "field" and parts[1] == str(self.world):
                self.hints[parts[2]] = (int(parts[3]), int(parts[4]))

    def go(self, x, y, near=4.0):
        api("act", {"ops": [{"op": "input", "move_to": {"x": x, "y": y}}]})
        for _ in range(120):
            s = self.status()
            you = s.get("you")
            if you and math.hypot(you["x"] - x, you["y"] - y) <= near:
                return True
            if you is None:
                time.sleep(1)
                api("act", {"ops": [{"op": "input", "move_to": {"x": x, "y": y}}]})
            time.sleep(0.5)
        return False

    # -- scouting -------------------------------------------------------

    def scout_for(self, wanted, start_ring=40):
        """Fly rings around the core, looking, until `wanted` ore is known."""
        cx, cy = self.core["x"], self.core["y"]
        self.share()
        if wanted in self.hints:
            self.go(*self.hints[wanted], near=8)
            self.look()
            if len(self.fields[wanted]) >= 6:
                return True
        for ring in range(start_ring, 150, 30):
            steps = max(8, int(2 * math.pi * ring / 40))
            offset = self.index * 2 * math.pi / self.n
            for i in range(steps):
                a = offset + 2 * math.pi * i / steps
                self.go(cx + ring * math.cos(a), cy + ring * math.sin(a), near=8)
                self.look()
                self.share()
                if len(self.fields[wanted]) >= 12:
                    return True
        return False

    # -- building -------------------------------------------------------

    def field_near_core(self, kind):
        """The densest known patch of `kind`, nearest the core."""
        tiles = self.fields[kind]
        cx, cy = self.core["x"], self.core["y"]
        return min(tiles, key=lambda t: (math.hypot(t[0] - cx, t[1] - cy) - 2 * sum((t[0] + dx, t[1] + dy) in tiles for dx in range(-2, 3) for dy in range(-2, 3))))

    def free(self, x, y):
        t = self.tiles.get((x, y))
        return t is not None and t.get("block") in SOLID_OK and t.get("building") is None

    def path_to_core(self, start):
        """Breadth-first over tiles seen, from start to a tile touching the core's 3x3 footprint."""
        cx, cy = self.core["x"], self.core["y"]
        q = collections.deque([start])
        prev = {start: None}
        while q:
            p = q.popleft()
            if abs(p[0] - cx) <= 2 and abs(p[1] - cy) <= 2 and (abs(p[0] - cx) == 2) != (abs(p[1] - cy) == 2):
                path = []
                while p is not None:
                    path.append(p)
                    p = prev[p]
                return path[::-1]
            for dx, dy, _ in DIRS:
                n = (p[0] + dx, p[1] + dy)
                if n not in prev and self.free(*n):
                    prev[n] = p
                    q.append(n)
        return None

    def corridor_scan(self, a, b):
        """Fly from a to b, looking, so a belt path between them is made of tiles seen."""
        d = math.hypot(a[0] - b[0], a[1] - b[1])
        for i in range(int(d // 30) + 2):
            f = min(1.0, i * 30 / max(d, 1))
            self.go(a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, near=10)
            self.look()

    def build(self, plans):
        """Queue plans a batch at a time, from where the unit is, and wait for them."""
        for i in range(0, len(plans), 12):
            batch = plans[i:i + 12]
            self.wait_for_stock(sum(COST.get(p["block"], 1) for p in batch))
            mx = sum(p["x"] for p in batch) / len(batch)
            my = sum(p["y"] for p in batch) / len(batch)
            self.go(mx, my, near=6)
            api("act", {"ops": [{"op": "plan_add", "plan": p} for p in batch]})
            self.wait_built(batch)

    def wait_for_stock(self, copper, limit=240):
        """Hold a batch back until the shared core can pay for it, rather than leave a belt half built."""
        for _ in range(limit // 5):
            have = (self.status().get("core") or {}).get("items", {}).get("copper", 0)
            # Seats racing for the first 1000 copper left every belt unfinished at N=16, so each seat leaves a reserve for lower seats.
            need = copper + 60 * self.index
            if have >= need:
                return
            log("waiting for copper", have, "of", need)
            time.sleep(5)

    def wait_built(self, batch, limit=90):
        keys = {(p["x"], p["y"]) for p in batch}
        for _ in range(int(limit)):
            s = self.status()
            you = s.get("you")
            if not you:
                return
            rows = api("query", {"of": "units", "filter": [["id", "eq", you["unit_id"]]], "fields": ["plans"]})
            rows = (rows.get("result") or {}).get("rows") or []
            plans = rows[0].get("plans") if rows else []
            if not any((p["x"], p["y"]) in keys for p in plans or []):
                return
            time.sleep(1.5)

    def mine_line(self, kind, drills=8):
        """Drills on `kind`, a belt from the patch to the core. Returns how many drills were queued."""
        patch = self.field_near_core(kind)
        self.corridor_scan(patch, (self.core["x"], self.core["y"]))
        path = self.path_to_core(patch)
        if not path:
            log("no belt path for", kind)
            return 0
        plans = []
        for a, b in zip(path, path[1:]):
            rot = next(r for dx, dy, r in DIRS if (a[0] + dx, a[1] + dy) == b)
            plans.append({"x": a[0], "y": a[1], "block": "conveyor", "rotation": rot})
        last, before = path[-1], path[-2]
        rot = next((r for dx, dy, r in DIRS if abs(last[0] + dx - self.core["x"]) <= 1 and abs(last[1] + dy - self.core["y"]) <= 1), 0)
        plans.append({"x": last[0], "y": last[1], "block": "conveyor", "rotation": rot})
        belt = set(path)
        placed, used = [], set()
        ore = self.fields[kind]
        for bx, by in path[:30]:
            for ax, ay in ((bx + 1, by), (bx - 2, by), (bx, by + 1), (bx, by - 2)):
                cells = {(ax, ay), (ax + 1, ay), (ax, ay + 1), (ax + 1, ay + 1)}
                if len(placed) < drills and not cells & belt and not cells & used and all(self.free(*c) for c in cells) and len(cells & ore) >= 2:
                    placed.append({"x": ax, "y": ay, "block": "mechanical-drill", "rotation": 0})
                    used |= cells
        log("line", kind, "belt", len(plans), "drills", len(placed))
        self.build(placed + plans)
        return len(placed)

    def defend(self, count=8):
        """Duos around the core, kept loaded with graphite or copper by hand."""
        cx, cy = self.core["x"], self.core["y"]
        self.go(cx, cy, near=6)
        self.look()
        ring = []
        for i in range(count):
            a = 2 * math.pi * i / count
            ring.append((int(cx + 8 * math.cos(a)), int(cy + 8 * math.sin(a))))
        self.build([{"x": x, "y": y, "block": "duo", "rotation": 0} for x, y in ring if self.free(x, y)])
        self.load_turrets()

    def load_turrets(self):
        s = self.status()
        self.go(s["core"]["x"], s["core"]["y"], near=10)
        rows = (api("query", {"of": "buildings", "filter": [["type", "eq", "duo"], ["team", "eq", s["team"]]], "fields": ["id", "ammo", "ammo_capacity"], "limit": 64}).get("result") or {}).get("rows") or []
        for b in rows:
            if (b.get("ammo") or 0) < 10:
                api("act", {"ops": [{"op": "withdraw", "building": s["core"]["id"], "item": "copper", "amount": 14}]})
                api("act", {"ops": [{"op": "deposit", "building": b["id"]}]})

    # -- the plan -------------------------------------------------------

    def jobs(self):
        if self.n == 1:
            return ["copper", "defend", "lead", "sand"]
        # One seat in eight defends: the core's stock is shared, and turrets from every seat would leave none for belts.
        roles = [["defend", "scrap"], ["copper", "coal"], ["lead", "copper"], ["sand", "lead"],
                 ["copper", "scrap"], ["coal", "lead"], ["sand", "copper"], ["scrap", "coal"]]
        return roles[self.index % len(roles)]

    def play(self):
        while True:
            start = self.world
            self.status()
            for job in self.jobs():
                if self.world != start and start is not None:
                    break
                try:
                    if job == "defend":
                        self.defend()
                    elif self.scout_for(job):
                        self.mine_line(job)
                except (KeyError, TypeError, ValueError) as e:
                    log("job", job, "failed:", repr(e))
            while True:
                self.status()
                if self.world != start and start is not None:
                    break
                if self.index % max(self.n, 1) == 0:
                    self.load_turrets()
                time.sleep(20)
                start = start if start is not None else self.world


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", type=int, default=0)
    ap.add_argument("--n", type=int, default=1)
    ap.add_argument("--idle", action="store_true")
    a = ap.parse_args()
    log("first status", api("status"))
    if a.idle:
        while True:
            time.sleep(60)
    Bot(a.index, a.n).play()


if __name__ == "__main__":
    main()
