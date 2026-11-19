"""Holdout: the game itself. Pure Python, no I/O, no clock of its own.

The host (host.py) owns real time and calls Game.advance_to(minute) to move the world forward; seats act through
Game.api(seat, endpoint, body). Everything a seat can learn is filtered by that seat's sight here, so the host and
the plugin never need to know the rules.
"""
import math
import random
import re
from collections import deque

from . import strangers as people
from . import worldgen as wg

NAMES = ["Maya", "Ben", "Ana", "Theo", "Iris", "Kofi", "Lena", "Raj", "Mei", "Owen", "Sara", "Jonas", "Nia", "Pablo",
         "Yuki", "Elif"]
COLORS = ["#e4572e", "#29a3d6", "#f2c14e", "#7bc950", "#b07cd8", "#f08cae", "#3fbfa5", "#ff9f1c", "#5c7cfa",
          "#c9a227", "#e76f51", "#2a9d8f", "#d62828", "#8ecae6", "#ffafcc", "#bc6c25"]
QUEUED = {"move_to", "chop", "quarry", "search", "forage", "fish", "pickup", "drop", "deposit", "withdraw", "cook",
          "craft", "build", "repair", "patch", "guard", "wait", "signal", "shoot", "hunt", "ask", "attack", "examine"}
IMMEDIATE = {"stop", "say", "shout", "radio", "post_note", "read_notes", "eat", "start_build", "recharge",
             "admit", "turn_away", "quarantine", "assign"}
COMMS_OPS = {"say", "shout", "radio", "post_note", "read_notes"}
TOPICS = ("where_from", "what_saw", "who_with", "arms", "why_alone")
JOBS = ("chop", "quarry", "forage", "fish", "build", "guard", "idle")
OUTDOOR_JOBS = ("chop", "quarry", "forage", "fish")
RESPONSE_OPS = {"move_to", "hunt", "shoot", "guard", "stop", "attack"}
FOOD_ORDER = ("berries", "fish", "produce", "meal", "can")      # what a helper eats first
HONEST = ("worker", "specialist")
SIDES = ("N", "E", "S", "W")
SIDE_NAMES = {"N": "north", "E": "east", "S": "south", "W": "west"}
ORDER_FIELDS = {"op", "x", "y", "to", "near", "place", "n", "item", "id", "minutes", "radius", "tier", "side", "stranger", "topic"}   # what an agent may set; the engine keeps its own working fields on the order too
KEPT = {"axe", "pickaxe", "spear", "rifle", "shotgun", "pistol", "ammo"}      # what a plain `deposit` leaves in the pack
GATE_SPOTS = [(48, 53), (47, 53), (49, 53), (46, 53), (50, 53), (47, 54), (49, 54), (46, 54), (50, 54), (45, 53), (51, 53)]
N8 = [(dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy]


class GameError(Exception):
    def __init__(self, msg, code=400, **extra):
        super().__init__(msg)
        self.code, self.extra = code, extra


BAD_ARGS = (LookupError, ValueError, TypeError, AttributeError, ArithmeticError)   # what a bad field in an agent's call can raise


def whole(value, name):
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        raise GameError(f"{name} must be a whole number") from None


def cheb(ax, ay, bx, by):
    return max(abs(ax - bx), abs(ay - by))


class Game:
    def __init__(self, cfg, seed, seats=()):
        self.cfg, self.seed = cfg, seed
        self.rnd = random.Random(f"{seed}:play")
        self.w = wg.generate(cfg, seed)
        self.grid = self.w["grid"]
        self.w["_rows0"] = [row[:] for row in self.grid]
        self.W, self.H = cfg["map_w"], cfg["map_h"]
        self.sx, self.sy, self.sw, self.sh = cfg["stronghold"]
        self.lm = {l["id"]: l for l in self.w["landmarks"]}
        self.tiers = {t["id"]: t for t in cfg["tiers"]}
        self.t = float(cfg["start_minute"])           # absolute game minute, day 1 00:00 = 0
        self.wall = 0.0                               # real seconds since the game started (set by the host)
        self.started = False
        self.frozen = False
        self.surv = {}
        self.zombies = {}
        self.zid = 0
        self.stock = {"can": 10, "wood": 4}
        self.structures = {}                          # tier -> {"state": done|damaged|site|repair, "labor": float}
        self.wall_hp = {d: 0.0 for d in SIDES}
        self.piles = {}
        self.notes = []
        self.best = 0.0
        self.tier_times = {}
        self.score_times = []                         # (wall, score) each time the best score rises by >= 0.005
        self.rescued = False
        self.rescue_at = None
        self.horde = {"active": False, "dir": None, "n": 0}
        self.kill_acc = 0.0
        self.tiles_changed = []
        self._field, self._field_hour = {}, None
        self.frame_events = []
        self.fx = []                                  # shots, blows, bites and deaths since the last frame, for the viewer
        self.frames = []                              # finished replay frames the host has not written yet
        self.msg_log = []                             # message records the host has not written yet (messages.jsonl)
        self.perf_log = []                            # stranger and threat records the host has not written yet (perf.jsonl)
        self.helpers = {}                             # admitted strangers: engine-driven bodies, keyed by stranger id
        self.word_skip = 0                            # arrivals that stay away because an honest stranger was shot
        self.dawn_news = []
        self.threat_totals = {"seen": 0, "responded": 0, "bitten": 0, "response_s_sum": 0.0}
        self.msg_counts = {}
        self.strangers = {st["id"]: dict(st, state="pending", x=None, y=None, wounds=0, talkers=[], topics=[], asked={},
                                         met=False, decision=None, decided_by=None, decided_t=None, outcome=None,
                                         wait_until=None, checkable=None, turn_at=None)
                          for st in people.generate(cfg, seed, self.w)}
        self._pick_refusers()
        self._near_work()
        self.last_frame_t = -1e9
        self.stats = {"bites": 0, "deaths": 0, "breaches": 0, "kills": 0, "shots": 0, "built": 0, "dead_minutes": 0, "eaten": 0,
                      "radio": 0, "say": 0, "shout": 0, "notes": 0, "caches_found": 0, "searches": 0}
        self._spawn_walkers(cfg["walkers_start"], start=True)
        for s in seats:
            self.join(s)

    def _near_work(self):
        """Where a helper can work: resources within helper_range tiles of the stronghold."""
        r = self.cfg["helper_range"]
        near = lambda p: self.in_sh(p[0], p[1], pad=r)   # noqa: E731
        self.near = {"trees": [p for p in self.w["trees"] if near(p)], "rocks": [p for p in self.w["rocks"] if near(p)],
                     "bushes": [p for p in self.w["bushes"] if near(p)],
                     "water": [(x, y) for y in range(self.H) for x in range(self.W) if self.grid[y][x] == wg.WATER and near((x, y))]}

    # ------------------------------------------------------------------ helpers
    @property
    def day(self):
        return int(self.t // 1440) + 1

    @property
    def minute_of_day(self):
        return self.t % 1440

    @property
    def night(self):
        m = self.minute_of_day
        return m >= self.cfg["night_start"] or m < self.cfg["night_end"]

    def clock(self):
        m = int(self.minute_of_day)
        return f"{m // 60:02d}:{m % 60:02d}"

    def in_sh(self, x, y, pad=0):
        return self.sx - pad <= x < self.sx + self.sw + pad and self.sy - pad <= y < self.sy + self.sh + pad

    def tile(self, x, y):
        return self.grid[y][x] if 0 <= x < self.W and 0 <= y < self.H else None

    def walkable(self, x, y):
        return self.tile(x, y) in wg.WALKABLE

    def done(self, tier):
        return self.structures.get(tier, {}).get("state") == "done"

    def built_or_damaged(self, tier):
        return self.structures.get(tier, {}).get("state") in ("done", "damaged")

    def kg(self, inv):
        return sum(self.cfg["items"][k]["kg"] * n for k, n in inv.items())

    def wall_max(self):
        top = None
        for t in ("stone_wall", "palisade"):
            st = self.structures.get(t, {}).get("state")
            if st in ("done", "damaged"):
                top = (t, st)
                break
        if not top or top[1] == "damaged":
            return 0.0
        hp = self.cfg["wall_hp"][top[0]]
        if self.done("forge"):
            hp += self.cfg["wall_hp"]["forge"]
        return float(hp)

    def bodies(self):
        """Everyone alive: survivors and helpers. The dead are nowhere until they respawn."""
        return [s for s in self.surv.values() if not s["dead_until"]] + list(self.helpers.values())

    def side_of(self, x, y):
        """Which wall of the stronghold a tile outside it faces, and how far off it is."""
        dx = self.sx - x if x < self.sx else x - (self.sx + self.sw - 1) if x >= self.sx + self.sw else 0
        dy = self.sy - y if y < self.sy else y - (self.sy + self.sh - 1) if y >= self.sy + self.sh else 0
        if dy >= dx:
            return ("N" if y < self.sy else "S"), dy
        return ("W" if x < self.sx else "E"), dx

    def wall_up(self, side):
        return self.wall_max() > 0 and self.wall_hp[side] > 0

    def walls_whole(self):
        return all(self.wall_up(d) for d in SIDES)

    def _wall_between(self, z, s):
        """True when a standing wall keeps zombie z from a survivor inside the stronghold."""
        if z["kind"] == "i" or self.in_sh(z["x"], z["y"]) or not self.in_sh(s["x"], s["y"]):
            return False
        return self.wall_up(self.side_of(z["x"], z["y"])[0])

    @staticmethod
    def _side(value):
        v = str(value or "").strip().upper()[:1]
        if v not in SIDES:
            raise GameError("side must be north, east, south or west")
        return v

    def _weapon(self, b):
        """The best weapon this body can use now: guns need rounds."""
        for w, spec in self.cfg["weapons"].items():
            if b["inv"].get(w) and (not spec.get("gun") or b["inv"].get("ammo")):
                return w
        return None

    def _gun(self, s):
        return next((w for w, spec in self.cfg["weapons"].items() if spec.get("gun") and s["inv"].get(w) and s["inv"].get("ammo")), None)

    def _armed(self, s):
        return any(s["inv"].get(w) for w in self.cfg["weapons"])

    def _reach(self, w):
        spec = self.cfg["weapons"][w]
        return spec["reach"] + (self.cfg["tower_reach"] if spec.get("gun") and self.done("watchtower") else 0)

    def _hear(self, s, frm, how, text):
        if s["dead_until"]:
            return
        s["heard"].append({"from": frm, "how": how, "text": text, "day": self.day, "clock": self.clock()})

    def _log_msg(self, s, channel, to, text, **extra):
        self.msg_log.append({"t": round(self.t), "day": self.day, "clock": self.clock(), "wall": round(self.wall, 1),
                             "from": s["name"], "seat": s["seat"], "channel": channel, "to": to, "text": text,
                             "x": s["x"], "y": s["y"], **extra})
        self.msg_counts[channel] = self.msg_counts.get(channel, 0) + 1

    def _st_log(self, st, event, **extra):
        self.perf_log.append({"kind": "stranger", "id": st["id"], "name": st["name"], "type": st["type"], "event": event,
                              "t": round(self.t), "day": self.day, "clock": self.clock(), "wall": round(self.wall, 1), **extra})

    def emit(self, seat, kind, text, **extra):
        if seat not in self.surv:      # helpers have no agent to tell
            return
        s = self.surv[seat]
        s["ev_seq"] += 1
        ev = {"seq": s["ev_seq"], "t": round(self.t), "day": self.day, "clock": self.clock(), "kind": kind, "text": text}
        ev.update(extra)
        s["events"].append(ev)
        if len(s["events"]) > 3000:
            del s["events"][:500]

    def emit_near(self, x, y, radius, kind, text, exclude=None, **extra):
        for seat, s in self.surv.items():
            if seat != exclude and not s["dead_until"] and cheb(s["x"], s["y"], x, y) <= radius:
                self.emit(seat, kind, text, **extra)

    def emit_all(self, kind, text, **extra):
        for seat in self.surv:
            self.emit(seat, kind, text, **extra)

    def fev(self, kind, text, x=None, y=None):
        self.frame_events.append({"kind": kind, "text": text, "x": x, "y": y})

    def effect(self, kind, **data):
        self.fx.append({"k": kind, "t": round(self.t), **data})

    # ------------------------------------------------------------------ seats
    def join(self, seat):
        if seat in self.surv:
            return self.surv[seat]
        i = len(self.surv)
        spots = [(x, y) for y in range(self.sy + 1, self.sy + self.sh - 1) for x in range(self.sx + 1, self.sx + self.sw - 1)]
        x, y = spots[(i * 7) % len(spots)]
        default = NAMES[i % len(NAMES)] + ("" if i < len(NAMES) else str(i // len(NAMES) + 1))
        named = bool(re.fullmatch(r"[a-z]+(-[a-z]+)+", str(seat))) and not re.fullmatch(r"agent-\d+", str(seat))
        s = {"seat": seat, "name": seat if named else default,
             "color": COLORS[i % len(COLORS)], "x": x, "y": y, "fx": float(x), "fy": float(y), "dir": "S",
             "hp": 100.0, "sat": 80.0, "dead_until": None, "inv": {"can": self.cfg["start_cans_each"], "spear": self.cfg["start_spears_each"]},
             "queue": deque(), "cur": None, "radio": self.cfg["radio_max_charge"], "notes_found": [],
             "known": set(), "events": [], "ev_seq": 0, "bubble": None, "bubble_until": -1,
             "act": "idle", "contact_since": None, "path": None, "calls": 0,
             "seen": set(), "heard": [], "threat": None}
        self.surv[seat] = s
        self._discover(s)
        self.emit(seat, "joined", f"You are {s['name']}. You wake up inside the stronghold at ({x},{y}).")
        return s

    def sight(self, s):
        c = self.cfg
        r = c["sight_night"] if self.night else c["sight_day"]
        inside = self.in_sh(s["x"], s["y"])
        if inside and self.night and self.done("generator"):
            r = max(r, c["sight_floodlit"])
        if inside and self.done("watchtower") and s["cur"] and s["cur"]["op"] == "guard":
            r = max(r, c["sight_tower"])
        return r

    def _discover(self, s):
        if s.get("helper"):
            return
        r = self.sight(s)
        reach = self.cfg["detail_reach"]
        for l in self.w["landmarks"]:
            nx = min(max(s["x"], l["x"]), l["x"] + l["w"] - 1)
            ny = min(max(s["y"], l["y"]), l["y"] + l["h"] - 1)
            d = cheb(s["x"], s["y"], nx, ny)
            if d <= reach:
                s["seen"].add(l["id"])
            if l["id"] in s["known"]:
                continue
            if d <= r:
                s["known"].add(l["id"])
                self.emit(s["seat"], "discovered", f"You see the {l['name']} (door at {l['door'][0]},{l['door'][1]}).",
                          place=l["id"])

    # ------------------------------------------------------------------ zombies
    def _spawn_walkers(self, n, start=False, edge=None, kind="w", target=None):
        cx, cy = self.w["center"]
        made = 0
        tries = 0
        while made < n and tries < n * 200:
            tries += 1
            if start:
                x, y = self.rnd.randrange(self.W), self.rnd.randrange(self.H)
                if cheb(x, y, cx, cy) < self.cfg["safe_radius_start"]:
                    continue
            else:
                side = edge or self.rnd.choice("NESW")
                if side in "NS":
                    x, y = self.rnd.randrange(self.W), (0 if side == "N" else self.H - 1)
                else:
                    x, y = (0 if side == "W" else self.W - 1), self.rnd.randrange(self.H)
                if edge:   # hordes come in a band
                    if side in "NS":
                        x = min(self.W - 1, max(0, cx + self.rnd.randint(-10, 10)))
                    else:
                        y = min(self.H - 1, max(0, cy + self.rnd.randint(-10, 10)))
            if not self.walkable(x, y) or self.tile(x, y) in (wg.FLOOR, wg.DOOR, wg.SH):
                continue
            self.zid += 1
            self.zombies[self.zid] = {"x": x, "y": y, "kind": kind, "next": self.t + self.rnd.random() * 6,
                                      "target": target, "noise": None, "hp": self.cfg["zombie_hp"]}
            made += 1

    def _horde_field(self):
        """Walking distance to the stronghold from every tile, so a horde goes around what stands in its way. Trees and
        rocks come and go through play, so it is worked out again at most once a game hour."""
        hour = int(self.t // 60)
        if self._field_hour == hour:
            return self._field
        q = deque((x, y) for y in range(self.H) for x in range(self.W) if self.in_sh(x, y))
        dist = {p: 0 for p in q}
        while q:
            p = q.popleft()
            for dx, dy in N8:
                n = (p[0] + dx, p[1] + dy)
                if n in dist or not self.walkable(*n):
                    continue
                if dx and dy and not (self.walkable(p[0] + dx, p[1]) and self.walkable(p[0], p[1] + dy)):
                    continue
                dist[n] = dist[p] + 1
                q.append(n)
        self._field, self._field_hour = dist, hour
        return dist

    def _horde_dir(self, z):
        """The step down the field, ties broken at random; None inside the stronghold or where no way in exists."""
        field = self._horde_field()
        here = field.get((z["x"], z["y"]))
        if not here:
            return None
        best, steps = here, []
        for dx, dy in N8:
            d = field.get((z["x"] + dx, z["y"] + dy))
            if d is None or d > best:
                continue
            if dx and dy and not (self.walkable(z["x"] + dx, z["y"]) and self.walkable(z["x"], z["y"] + dy)):
                continue
            if d < best:
                best, steps = d, []
            steps.append((dx, dy))
        return self.rnd.choice(steps) if steps and best < here else None

    def _zombie_step(self):
        c = self.cfg
        zs_sight = c["zombie_sight_night"] if self.night else c["zombie_sight_day"]
        awake = self.bodies()
        cx, cy = self.w["center"]
        for zid, z in list(self.zombies.items()):
            if self.t < z["next"]:
                continue
            tx = ty = None
            inside_job = z["kind"] == "i"     # turned inside the walls: the wall does not protect anyone from this one
            sees = zs_sight
            if z["kind"] == "h":
                tx, ty = cx, cy
                speed = c["horde_min_per_tile"]
                sees = c["horde_sight"]          # on the march it only turns on what it nearly walks into
            elif inside_job:
                tx, ty = cx, cy
                speed = c["zombie_chase_min_per_tile"]
            else:
                speed = c["zombie_wander_min_per_tile"]
            best = None
            for s in awake:
                if self._wall_between(z, s):
                    continue
                d = cheb(z["x"], z["y"], s["x"], s["y"])
                if (d <= sees or inside_job) and (best is None or d < best[0]):
                    best = (d, s)
            if best:
                tx, ty = best[1]["x"], best[1]["y"]
                speed = min(speed, c["zombie_chase_min_per_tile"])
                if best[0] <= 1:
                    z["next"] = self.t + 1
                    continue        # in contact: the fight resolves in _fights
            elif z.get("noise") and self.t < z["noise"][2]:
                tx, ty = z["noise"][0], z["noise"][1]
                speed = min(speed, c["zombie_chase_min_per_tile"])
            z["next"] = self.t + speed * (0.8 + 0.4 * self.rnd.random())
            if tx is None:
                dx, dy = self.rnd.choice(N8)
            else:
                step = self._horde_dir(z) if z["kind"] == "h" and not best else None
                dx, dy = step or ((tx > z["x"]) - (tx < z["x"]), (ty > z["y"]) - (ty < z["y"]))
                if self.rnd.random() < 0.2:
                    dx, dy = self.rnd.choice(N8)
            nx, ny = z["x"] + dx, z["y"] + dy
            if not self.walkable(nx, ny):
                for ddx, ddy in self.rnd.sample(N8, 8):
                    if self.walkable(z["x"] + ddx, z["y"] + ddy):
                        nx, ny = z["x"] + ddx, z["y"] + ddy
                        break
                else:
                    continue
            if self.in_sh(nx, ny) and not self.in_sh(z["x"], z["y"]) and self.wall_up(self.side_of(z["x"], z["y"])[0]):
                continue            # this side holds: it stays outside, attacking the wall (in _walls)
            z["x"], z["y"] = nx, ny

    def _noise(self, x, y, kind):
        r = self.cfg["noise"].get(kind, 0)
        if not r:
            return
        for z in self.zombies.values():
            if z["kind"] == "w" and cheb(z["x"], z["y"], x, y) <= r:
                z["noise"] = (x, y, self.t + 30)

    def _fights(self):
        c = self.cfg
        if int(self.t) % c["fight_every_min"]:
            return
        bite = c["bite_chance_night"] if self.night else c["bite_chance_day"]
        for s in self.bodies():
            near = [zid for zid, z in self.zombies.items() if cheb(z["x"], z["y"], s["x"], s["y"]) <= 1
                    and not self._wall_between(z, s)]
            if not near:
                s["contact_since"] = None
                continue
            if s["contact_since"] is None:
                s["contact_since"] = self.t
                self.emit(s["seat"], "contact", f"{len(near)} zombie(s) on you at ({s['x']},{s['y']}). You fight back.")
            armed = self._armed(s)
            for zid in list(near):
                if self.rnd.random() < (c["kill_chance_armed"] if armed else c["kill_chance"]):
                    z = self.zombies[zid]
                    self.effect("melee", s=s["seat"], x=s["x"], y=s["y"], tx=z["x"], ty=z["y"], hit=True)
                    if self._hit_zombie(zid, s):
                        near.remove(zid)
            # a spear holds one zombie off: alone it cannot bite, and in a crowd only the others can
            for zid in near[1 if s["inv"].get("spear") else 0:]:
                # one that turned among the living is on them before they know: an infection spreads fast
                if self.rnd.random() < (c["bite_chance_inside"] if self.zombies[zid]["kind"] == "i" else bite):
                    self._bite(s)
                    break
        for st in list(self.strangers.values()):
            if st["state"] == "quarantined":
                n = sum(1 for z in self.zombies.values() if cheb(z["x"], z["y"], st["x"], st["y"]) <= 1)
                if n and self.rnd.random() < 1 - (1 - bite) ** n:
                    self._caught_outside(st)

    def _hit_zombie(self, zid, by=None):
        """It takes more than one hit to put a zombie down. True when this one did."""
        z = self.zombies[zid]
        z["hp"] = z.get("hp", 1) - 1
        if z["hp"] > 0:
            return False
        self._kill_zombie(zid, by)
        return True

    def _kill_zombie(self, zid, by=None):
        """Every zombie dies here. One that was a person drops what they carried."""
        z = self.zombies.pop(zid)
        self.stats["kills"] += 1
        if z.get("gear"):
            p = self.piles.setdefault((z["x"], z["y"]), {})
            for k, n in z["gear"].items():
                p[k] = p.get(k, 0) + n
        if z.get("who"):
            self.stats["turned_killed"] = self.stats.get("turned_killed", 0) + 1
            where = f" Their gear lies at ({z['x']},{z['y']})." if z.get("gear") else ""
            if by:
                self.emit(by["seat"], "done", f"That was {z['who']}. You put them down.{where}")
            self.fev("kill", f"{z['who']} was put down", z["x"], z["y"])
        return z

    def _turned(self, x, y, who, gear):
        """A body gets up as a zombie. The game runs it: no agent ever does."""
        self.zid += 1
        self.zombies[self.zid] = {"x": x, "y": y, "kind": "i" if self.in_sh(x, y) else "w", "next": self.t + 1, "target": None,
                                  "noise": None, "turned": self.t, "who": who, "gear": dict(gear), "hp": self.cfg["zombie_hp"]}

    def _bite(self, s):
        if s.get("helper"):
            self._helper_bitten(s)
        else:
            self._die(s, "bitten")

    def _when(self, t):
        return f"day {int(t // 1440) + 1} {int(t % 1440) // 60:02d}:{int(t % 60):02d}"

    def _back_sec(self, s):
        return round((s["dead_until"] - self.t) * self.cfg["real_sec_per_day"] / 1440)

    def _dead_note(self, s):
        return (f"you are dead. You come back at the stronghold at {self._when(s['dead_until'])}, in about {self._back_sec(s)} real "
                "seconds. Your part in the run is not over: wait in short sleeps and keep your session open.")

    def _die(self, s, why):
        c = self.cfg
        x, y = s["x"], s["y"]
        self._turned(x, y, s["name"], s["inv"])
        back = self.t + c["respawn_min"]         # nobody comes back into a horde: the base is clear by mid-morning
        m = back % 1440
        first = c["respawn_hour"] * 60
        if m >= c["night_start"]:
            back += 1440 - m + first
        elif m < first:
            back += first - m
        s.update(inv={}, dead_until=back, cur=None, path=None, act="dead", contact_since=None, bubble=None)
        s["queue"].clear()
        if s.get("threat"):
            self._close_threat(s, "bitten" if why == "bitten" else "dead")
        self.stats["deaths"] += 1
        if why == "bitten":
            self.stats["bites"] += 1
        cause = "A zombie bit you" if why == "bitten" else "You starved"
        self.emit(s["seat"], "died", f"{cause} at ({x},{y}) and you died. Your body got up as a zombie with everything you carried; "
                  f"kill it and the gear drops. {self._dead_note(s)[0].upper()}{self._dead_note(s)[1:]}")
        self.emit_near(x, y, self.cfg["say_tiles"], "teammate_dead", f"{s['name']} is dead at ({x},{y}), and the body is getting up.")
        self.effect("death", s=s["seat"], x=x, y=y)
        self.fev("death", f"{s['name']} {'was bitten' if why == 'bitten' else 'starved'} and turned", x, y)

    def _respawn(self, s):
        c = self.cfg
        spots = [(x, y) for y in range(self.sy + 2, self.sy + self.sh - 2) for x in range(self.sx + 2, self.sx + self.sw - 2)]
        x, y = spots[self.stats["deaths"] % len(spots)]
        s.update(x=x, y=y, fx=float(x), fy=float(y), dead_until=None, act="idle", hp=100.0, sat=c["respawn_satiety"], radio=0.0)
        s["inv"]["spear"] = c["start_spears_each"]
        self.emit(s["seat"], "respawned", f"You are back: a new survivor reached the stronghold at ({x},{y}), and you play them under the "
                  "same name. You have a spear and nothing else, and your radio is flat.")
        self.emit_near(x, y, self.cfg["say_tiles"], "teammate_back", f"{s['name']} walks in through the gate with nothing but a spear.", exclude=s["seat"])
        self.fev("respawn", f"{s['name']} is back at the stronghold", x, y)
        self._discover(s)

    def _helper_bitten(self, h):
        st = self.strangers[h["seat"]]
        del self.helpers[h["seat"]]
        st["state"], st["outcome"] = "turned", "bitten_as_helper"
        self._st_log(st, "bitten_as_helper")
        self.stats["helpers_lost"] = self.stats.get("helpers_lost", 0) + 1
        self._turned(h["x"], h["y"], h["name"], h["inv"])
        self.emit_near(h["x"], h["y"], self.cfg["say_tiles"], "teammate_dead", f"{h['name']} is dead at ({h['x']},{h['y']}), and the body is getting up.")
        self.effect("death", s=h["seat"], x=h["x"], y=h["y"])
        self.fev("death", f"{h['name']} was bitten and turned", h["x"], h["y"])

    # ------------------------------------------------------------------ walls and hordes
    def _walls(self):
        c = self.cfg
        wmax = self.wall_max()
        inside_z = [z for z in self.zombies.values() if self.in_sh(z["x"], z["y"])]
        if wmax > 0:
            at_wall = {}
            for z in self.zombies.values():
                if self.in_sh(z["x"], z["y"], pad=1) and not self.in_sh(z["x"], z["y"]):
                    d = self.side_of(z["x"], z["y"])[0]
                    at_wall[d] = at_wall.get(d, 0) + 1
            for d, n in at_wall.items():
                if self.wall_hp[d] > 0:
                    self.wall_hp[d] -= c["wall_dmg_per_zombie_min"] * n
                    if self.wall_hp[d] <= 0:
                        self.wall_hp[d] = 0.0
                        self._breach(d)
        elif self.horde["active"] and not self.horde.get("raided"):
            # No wall at all (never built, or still a site): a horde that holds the stronghold for a while does the same
            # damage as a breach, once a night.
            if any(z["kind"] == "h" for z in inside_z):
                self.horde["inside_min"] = self.horde.get("inside_min", 0) + 1
                if self.horde["inside_min"] >= c["raid_after_min"]:
                    self.horde["raided"] = True
                    self._breach(raid=True)
        self._defend()
        # one turned inside the walls and left alone for too long does the damage of an overrun, then wanders off
        for z in self.zombies.values():
            if z["kind"] == "i" and self.t - z["turned"] >= c["turned_attack_min"]:
                z["kind"] = "w"
                self.stats["turned_damage"] = self.stats.get("turned_damage", 0) + 1
                hits, lost = [], []
                for _ in range(c["turned_buildings"]):
                    hit, gone = self._damage_base()
                    hits += [self.tiers[hit]["name"]] if hit else []
                    lost += gone
                self.emit_all("breach", f"{z['who']} turned inside the walls and tore through the stronghold. " +
                              (f"Damaged: {', '.join(hits)}. " if hits else "") +
                              (f"Lost {', '.join(lost)} from the stockpile." if lost else ""))
                self.fev("breach", f"{z['who']} tore through the stronghold", z["x"], z["y"])

    def _under_attack(self, side):
        return any(self.in_sh(z["x"], z["y"], pad=1) and not self.in_sh(z["x"], z["y"]) and self.side_of(z["x"], z["y"])[0] == side
                   for z in self.zombies.values())

    def _damage_base(self):
        """One building damaged and the food cut: what a breach, an overrun or a turned stranger costs."""
        others = [t for t, v in self.structures.items() if v["state"] == "done" and t not in ("campfire", "palisade", "stone_wall")]
        hit = self.rnd.choice(others) if others else None
        if hit:
            self.structures[hit]["state"] = "damaged"
        lost = []
        for k in list(self.stock):
            if self.cfg["items"][k].get("food"):
                n = int(math.ceil(self.stock[k] * self.cfg["breach_food_loss"]))
                if n:
                    self.stock[k] -= n
                    lost.append(f"{n} {k}")
        return hit, lost

    def _defend(self):
        """Guards at their posts, with a weapon in hand, strike at the zombies on their side, or at any inside the walls."""
        c = self.cfg
        guards = [b for b in self.bodies() if b["cur"] and b["cur"].get("on_post") and self.t >= b.get("ready_at", 0)]
        if not guards or not self.zombies:
            return
        lit = self.night and self.done("generator")
        reach_max = max(w["reach"] for w in c["weapons"].values()) + c["tower_reach"]
        inside, sides = [], {d: [] for d in SIDES}
        for zid, z in self.zombies.items():
            if self.in_sh(z["x"], z["y"]):
                inside.append(zid)
            elif self.in_sh(z["x"], z["y"], pad=reach_max):
                d, dist = self.side_of(z["x"], z["y"])
                sides[d].append((dist, zid))
        for b in guards:
            w = self._weapon(b)
            if not w:
                continue
            spec = c["weapons"][w]
            reach = self._reach(w)
            side = b["cur"]["post_side"]
            pool = [zid for zid in inside if zid in self.zombies and cheb(self.zombies[zid]["x"], self.zombies[zid]["y"], b["x"], b["y"]) <= reach]
            pool += [zid for dist, zid in sorted(sides[side]) if dist <= reach and zid in self.zombies]
            if not pool:
                continue
            b["ready_at"] = self.t + spec["every"] / (c["floodlight_mult"] if lit else 1.0)
            if spec.get("gun"):
                b["inv"]["ammo"] -= 1
                if not b["inv"]["ammo"]:
                    del b["inv"]["ammo"]
                self.stats["shots"] += 1
                self._noise(b["x"], b["y"], "shoot")
            for zid in pool[:spec.get("spread", 1)]:
                z = self.zombies[zid]
                hit = self.rnd.random() < spec["hit"]
                self.effect("shot" if spec.get("gun") else "melee", s=b["seat"], w=w, x=b["x"], y=b["y"], tx=z["x"], ty=z["y"], hit=hit)
                if hit:
                    self._hit_zombie(zid, b)

    def _breach(self, side=None, raid=False):
        """A wall side falls, or a horde overruns a stronghold with no wall: one building and part of the food."""
        self.horde["raided"] = True
        self.stats["raids" if raid else "breaches"] = self.stats.get("raids" if raid else "breaches", 0) + 1
        hit, lost = self._damage_base()
        head = "The horde has overrun the stronghold: there is no wall to stop it! " if raid else \
            f"The {SIDE_NAMES[side]} wall is down: zombies can walk in on that side! "
        msg = head + (f"The {self.tiers[hit]['name']} is damaged. " if hit else "") + \
              (f"Lost {', '.join(lost)} from the stockpile." if lost else "")
        self.emit_all("breach", msg)
        x, y = self.sx + self.sw // 2, self.sy + self.sh // 2
        if side:
            x += {"E": 3, "W": -3}.get(side, 0)
            y += {"S": 3, "N": -3}.get(side, 0)
            self.effect("breach", side=side, x=x, y=y)
        self.fev("breach", "The horde overran the stronghold" if raid else f"The {SIDE_NAMES[side]} wall fell", x, y)

    def _hordes(self):
        c = self.cfg
        m = int(self.minute_of_day)
        if self.day >= c["horde_first_day"] and self.day % c["horde_every_days"] == 0 and m == c["horde_hour"] * 60 and not self.horde["active"]:
            k = self.day // c["horde_every_days"]
            n = self.horde_size(self.day)
            shares = self._horde_shares(k, n)
            before = self.zid
            for side, m_side in shares.items():
                self._spawn_walkers(m_side, edge=side, kind="h")
            for zid, z in self.zombies.items():
                if zid > before:
                    z["night"] = True             # this night's horde: what is left of it goes back out at dawn
            self.horde = {"active": True, "dir": "all", "n": n, "shares": shares}
            self.emit_all("horde", f"Night falls, and a roar comes from every side: a horde of about {n} is closing on the stronghold.")
            self.fev("horde", f"Horde night: {n} from every side")
        if self.horde["active"] and m == c["night_end"]:
            cx, cy = self.w["center"]
            for z in self.zombies.values():
                z["kind"] = "w"
                if z.get("night"):                 # the night's horde walks off the map and is gone
                    dirs = {(0, z["y"]): z["x"], (self.W - 1, z["y"]): self.W - 1 - z["x"], (z["x"], 0): z["y"], (z["x"], self.H - 1): self.H - 1 - z["y"]}
                    z["noise"] = (*min(dirs, key=dirs.get), self.t + c["scatter_min"])
                    z["gone_at"] = self.t + c["scatter_min"]
                elif cheb(z["x"], z["y"], cx, cy) <= c["scatter_tiles"]:   # the valley's own walkers wander back out
                    a, d = self.rnd.random() * 2 * math.pi, self.rnd.uniform(c["scatter_tiles"], c["scatter_tiles"] * 1.5)
                    x, y = cx + math.cos(a) * d, cy + math.sin(a) * d
                    z["noise"] = (min(self.W - 1, max(0, round(x))), min(self.H - 1, max(0, round(y))), self.t + c["scatter_min"])
            self.horde["active"] = False
            self.emit_all("dawn", "Dawn. The horde breaks up and shambles back out of the valley.")
        gone = [zid for zid, z in self.zombies.items() if z.get("gone_at") and self.t >= z["gone_at"]]
        for zid in gone:
            del self.zombies[zid]

    def horde_size(self, day):
        c = self.cfg
        return min(c["horde_max"], c["horde_base"] + c["horde_step"] * max(0, day - c["horde_first_day"]))

    def _horde_shares(self, k, n):
        """How a horde divides among the four sides: uneven, and different every night."""
        rr = random.Random(f"{self.seed}:horde{k}")
        weights = {d: rr.random() ** 2 + 0.15 for d in SIDES}
        total = sum(weights.values())
        shares = {d: int(n * w / total) for d, w in weights.items()}
        for d in sorted(weights, key=weights.get, reverse=True)[: n - sum(shares.values())]:
            shares[d] += 1
        return shares

    def _population(self):
        if int(self.minute_of_day) % 60:
            return
        c = self.cfg
        want = min(c["walkers_cap"], c["walkers_start"] + c["walkers_per_day"] * (self.day - 1))
        have = sum(1 for z in self.zombies.values() if z["kind"] == "w")
        if have < want:
            self._spawn_walkers(min(c["spawn_per_hour"], want - have))

    def _dawn(self):
        if int(self.minute_of_day) != self.cfg["night_end"]:
            return
        c = self.cfg
        if not self.done("storehouse"):
            for k in list(self.stock):
                if c["items"][k].get("perish") and self.stock[k]:
                    self.stock[k] -= int(math.ceil(self.stock[k] * c["spoil_per_day"]))
        if self.done("garden"):
            self.stock["produce"] = self.stock.get("produce", 0) + c["garden_produce_per_day"]
        for h in list(self.helpers.values()):
            self._helper_eat(h)
        for text in self.dawn_news:
            self.emit_near(self.sx + 3, self.sy + 3, 12, "stranger_news", text)
            for s in self.surv.values():
                if cheb(s["x"], s["y"], self.sx + 3, self.sy + 3) <= 12:
                    self._hear(s, "the stronghold", "news", text)
        self.dawn_news = []
        if self.rescue_at is not None and not self.rescued:
            self.rescued = True
            self.emit_all("rescue", "A helicopter lands on the pad at dawn. You are rescued.")
            self.fev("rescue", "Rescued!", self.sx + self.sw // 2, self.sy + self.sh // 2)

    # ------------------------------------------------------------------ the main tick: one game minute
    def advance_to(self, minute, wall=None):
        if wall is not None:
            self.wall = wall
        if self.frozen:
            return
        end = self.cfg["start_minute"] + self.cfg["days"] * 1440
        while self.t + 1 <= min(minute, end):
            self.t += 1
            self._tick()
        if self.t >= end and not self.frozen:
            self.frozen = True
            self._frame(force=True)

    def _tick(self):
        c = self.cfg
        self._hordes()
        self._dawn()
        self._population()
        self._strangers_tick()
        for s in self.surv.values():
            self._survivor_minute(s)
        for h in list(self.helpers.values()):
            self._helper_minute(h)
        self._zombie_step()
        self._fights()
        self._walls()
        self._threats()
        self._score()
        if self.t - self.last_frame_t >= c["frame_every_min"]:
            self._frame()

    def _survivor_minute(self, s):
        c = self.cfg
        if s["bubble"] and self.t >= s["bubble_until"]:
            s["bubble"] = None
        if s["dead_until"]:
            self.stats["dead_minutes"] += 1
            if self.t >= s["dead_until"]:
                self._respawn(s)
            return
        s["sat"] -= c["satiety_per_day"] / 1440.0
        if s["sat"] <= 0:
            s["sat"] = 0.0
            if not s.get("helper"):
                s["hp"] -= c["starving_hp_per_hour"] / 60.0
                if s["hp"] <= 0:
                    self._die(s, "hunger")
                    return
        elif s["sat"] > 20 and s["hp"] < 100:
            s["hp"] = min(100.0, s["hp"] + c["hp_regen_per_hour"] / 60.0)
        if s["cur"] is None and s["queue"]:
            s["cur"] = s["queue"].popleft()
            s["cur"]["prog"] = 0.0
        if s["cur"] is None:
            s["act"] = "idle"
            self._discover(s)
            return
        in_contact = s["contact_since"] is not None
        slow = 0.5 if s["sat"] <= 0 else 1.0
        try:
            finished = self._work(s, s["cur"], slow, in_contact)
        except (GameError, *BAD_ARGS) as e:   # a bad order fails that order; it must never stop the game minute
            why = e if isinstance(e, GameError) else f"bad arguments: {e}"
            self.emit(s["seat"], "order_failed", f"{s['cur']['op']}: {why}", op=s["cur"]["op"])
            s["cur"] = None
            s["path"] = None
            return
        if finished:
            s["cur"] = None
            s["path"] = None
        self._discover(s)

    # ------------------------------------------------------------------ orders
    def _goal_for(self, s, o):
        if "to" in o:
            to = o["to"]
            if to == "stronghold":
                return tuple(self.w["gate"][:1]) + (self.w["gate"][1] - 1,), 0
            if to in self.lm:
                if to not in s["known"]:
                    raise GameError(f"you don't know where '{to}' is yet")
                return tuple(self.lm[to]["door"]), 0
            for o2 in self.surv.values():
                if o2["name"].lower() == str(to).lower() and not o2["dead_until"]:
                    if cheb(o2["x"], o2["y"], s["x"], s["y"]) > self.sight(s):
                        raise GameError(f"you can't see {o2['name']}")
                    return (o2["x"], o2["y"]), 1
            raise GameError(f"unknown place '{to}'")
        x, y = int(o["x"]), int(o["y"])
        if not (0 <= x < self.W and 0 <= y < self.H):
            raise GameError("off the map")
        return (x, y), int(o.get("near", 0))

    def _path(self, s, goal, near):
        start = (s["x"], s["y"])
        if cheb(*start, *goal) <= near:
            return []
        q = deque([start])
        prev = {start: None}
        found = None
        while q:
            p = q.popleft()
            if cheb(*p, *goal) <= near and (near or p == goal):
                found = p
                break
            for dx, dy in N8:
                n = (p[0] + dx, p[1] + dy)
                if n in prev or not self.walkable(*n):
                    continue
                if dx and dy and not (self.walkable(p[0] + dx, p[1]) and self.walkable(p[0], p[1] + dy)):
                    continue
                prev[n] = p
                q.append(n)
        if found is None:
            raise GameError(f"no way to reach ({goal[0]},{goal[1]})")
        path = []
        while found != start:
            path.append(found)
            found = prev[found]
        return path[::-1]

    def _walk(self, s, slow):
        """One minute of walking along s['path']. True when the path is done."""
        if not s["path"]:
            return True
        nx, ny = s["path"][0]
        t = self.tile(nx, ny)
        c = self.cfg
        cost = c["road_min_per_tile"] if t == wg.ROAD else (c["rough_min_per_tile"] if t in wg.ROUGH else c["walk_min_per_tile"])
        s["cur"]["step"] = s["cur"].get("step", 0.0) + slow / cost
        frac = min(1.0, s["cur"]["step"])
        s["fx"] = s["x"] + (nx - s["x"]) * frac
        s["fy"] = s["y"] + (ny - s["y"]) * frac
        s["dir"] = "E" if nx > s["x"] else "W" if nx < s["x"] else ("S" if ny > s["y"] else "N")
        s["act"] = "walk"
        if s["cur"]["step"] >= 1:
            s["cur"]["step"] -= 1
            s["x"], s["y"] = nx, ny
            s["fx"], s["fy"] = float(nx), float(ny)
            s["path"].pop(0)
        return not s["path"]

    def _approach(self, s, o, x, y, near=1):
        """Walk to within `near` of (x, y) first. True once there."""
        if cheb(s["x"], s["y"], x, y) <= near:
            return True
        if s["path"] is None:
            s["path"] = self._path(s, (x, y), near)
        self._walk(s, o.get("_slow", 1.0))
        return False

    def _at_sh(self, s):
        return self.in_sh(s["x"], s["y"], pad=1)

    def _add(self, s, item, n):
        room = self.cfg["carry_kg"] - self.kg(s["inv"])
        per = self.cfg["items"][item]["kg"]
        take = max(0, min(n, int(room // per))) if per > 0 else n   # a full pack's weight can round a hair over the limit
        if take > 0:
            s["inv"][item] = s["inv"].get(item, 0) + take
        if n - take > 0:
            p = self.piles.setdefault((s["x"], s["y"]), {})
            p[item] = p.get(item, 0) + n - take
            self.emit(s["seat"], "too_heavy", f"Too heavy: left {n - take} {item} on the ground at ({s['x']},{s['y']}).")
        return take

    def _work(self, s, o, slow, contact):
        c = self.cfg
        op = o["op"]
        o["_slow"] = slow
        if contact and op not in ("move_to", "shoot", "hunt", "attack"):
            s["act"] = "fight"
            return False
        if op == "move_to":
            if s["path"] is None:
                goal, near = self._goal_for(s, o)
                s["path"] = self._path(s, goal, near)
            return self._walk(s, slow * (0.5 if contact else 1.0))
        if op == "wait":
            s["act"] = "idle"
            o["prog"] += 1
            return o["prog"] >= int(o.get("minutes", 30))
        if op == "guard":
            if "post" not in o:
                o["post_side"] = self._side(o.get("side"))
                o["post"] = self._post(o["post_side"])
            if not self._approach(s, o, o["post"][0], o["post"][1], near=0):
                return False
            s["act"] = "guard"
            o["on_post"] = True
            s["dir"] = {"N": "N", "S": "S", "E": "E", "W": "W"}[o["post_side"]]
            o["prog"] += 1
            return o["prog"] >= int(o.get("minutes", 60))
        if op in ("chop", "quarry", "forage", "fish"):
            x, y = int(o["x"]), int(o["y"])
            t = self.tile(x, y)
            need = {"chop": wg.TREE, "quarry": wg.ROCK, "forage": wg.BUSH, "fish": wg.WATER}[op]
            if t != need:
                raise GameError(f"there is no {'tree' if op == 'chop' else 'rock' if op == 'quarry' else 'berry bush' if op == 'forage' else 'water'} at ({x},{y})")
            if op == "forage" and self.w["bushes"].get((x, y), 0) > self.t:
                raise GameError(f"the bush at ({x},{y}) has no ripe berries yet")
            if not self._approach(s, o, x, y, near=1):
                return False
            s["act"] = op
            s["dir"] = "E" if x > s["x"] else "W" if x < s["x"] else ("S" if y > s["y"] else "N")
            dur = c[f"{op}_min"]
            if op == "chop" and s["inv"].get("axe"):
                dur /= c["tool_speedup"]
            if op == "quarry" and s["inv"].get("pickaxe"):
                dur /= c["tool_speedup"]
            if op in ("chop", "quarry") and int(o["prog"]) % 10 == 0:
                self._noise(s["x"], s["y"], op)
            o["prog"] += slow
            if o["prog"] < dur:
                return False
            if op == "chop":
                left = self.w["trees"][(x, y)]
                got = min(c["chop_logs"], left)
                self.w["trees"][(x, y)] = left - got
                if left - got <= 0:
                    self.grid[y][x] = wg.STUMP
                    self.tiles_changed.append([x, y, wg.STUMP])
                self._add(s, "wood", got)
                self.emit(s["seat"], "done", f"Chopped {got} wood at ({x},{y})." + (" The tree is gone." if left - got <= 0 else ""))
            elif op == "quarry":
                left = self.w["rocks"][(x, y)]
                self.w["rocks"][(x, y)] = left - 1
                if left - 1 <= 0:
                    self.grid[y][x] = wg.QROCK
                    self.tiles_changed.append([x, y, wg.QROCK])
                self._add(s, "stone", 1)
                self.emit(s["seat"], "done", f"Quarried 1 stone at ({x},{y}).")
            elif op == "forage":
                self.w["bushes"][(x, y)] = self.t + c["bush_regrow_days"] * 1440
                yield_mult = c["hunter_mult"] if s.get("trade") == "hunter" else 1
                self._add(s, "berries", c["forage_berries"] * yield_mult)
                self.emit(s["seat"], "done", f"Picked {c['forage_berries']} berries at ({x},{y}).")
            else:
                if self.rnd.random() < c["fish_chance"]:
                    self._add(s, "fish", c["hunter_mult"] if s.get("trade") == "hunter" else 1)
                    self.emit(s["seat"], "done", "Caught a fish.")
                else:
                    self.emit(s["seat"], "done", "Nothing bit.")
            return True
        if op == "search":
            return self._search(s, o, slow)
        if op == "ask":
            return self._ask(s, o)
        if op == "attack":
            return self._attack(s, o)
        if op == "examine":
            return self._examine(s, o)
        if op == "shoot":
            w = self._gun(s)
            if not w:
                raise GameError("you need a gun and a round to shoot")
            spec = c["weapons"][w]
            if self._person_id(o.get("id")):
                st = self._target_person(o["id"])
                px, py = self._person_pos(st)
                d = cheb(px, py, s["x"], s["y"])
                if d > self.sight(s):
                    raise GameError(f"you can't see {st['name']}")
                if d > self._reach(w):
                    raise GameError(f"out of range ({d} tiles; your {w} reaches {self._reach(w)})")
                s["act"] = "shoot"
                o["prog"] += 1
                if o["prog"] < spec["aim_min"]:
                    return False
                self._fire_person(s, st, w)
                return True
            zid = self._zombie_target(s, o, w)
            s["act"] = "shoot"
            o["prog"] += 1
            if o["prog"] < spec["aim_min"]:
                return False
            self._fire(s, zid, w)
            return True
        if op == "hunt":
            o["prog"] += 1
            if o["prog"] > int(o.get("minutes", 60)):
                self.emit(s["seat"], "done", "You stop hunting.")
                return True
            r = min(int(o.get("radius", 99)), self.sight(s))
            near = [(cheb(z["x"], z["y"], s["x"], s["y"]), zid) for zid, z in self.zombies.items()
                    if cheb(z["x"], z["y"], s["x"], s["y"]) <= r]
            if not near:
                self.emit(s["seat"], "done", "No zombies in sight: you stop hunting.")
                return True
            d, zid = min(near)
            z = self.zombies[zid]
            s["act"] = "hunt"
            w = self._gun(s)
            if w and d <= self._reach(w):
                o["aim"] = o.get("aim", 0) + 1
                if o["aim"] >= c["weapons"][w]["aim_min"]:
                    o["aim"] = 0
                    self._fire(s, zid, w)
                return False
            if d <= 1:
                return False          # close enough: the fight happens every few minutes (_fights)
            if s["path"] is None or o.get("tgt") != zid or int(o["prog"]) % 5 == 0:
                s["path"] = self._path(s, (z["x"], z["y"]), 1)
                o["tgt"] = zid
            self._walk(s, slow)
            s["act"] = "hunt"
            return False
        if op == "pickup":
            x, y = int(o.get("x", s["x"])), int(o.get("y", s["y"]))
            if not self._approach(s, o, x, y, near=0):
                return False
            pile = self.piles.get((x, y), {})
            want = o.get("item")
            for k in list(pile):
                if want and k != want:
                    continue
                n = pile.pop(k)
                took = self._add(s, k, n)
                self.emit(s["seat"], "done", f"Picked up {took} {k}.")
            if not pile:
                self.piles.pop((x, y), None)
            return True
        if op == "drop":
            k, n = o["item"], int(o.get("n", s["inv"].get(o["item"], 0)))
            have = s["inv"].get(k, 0)
            n = min(n, have)
            if n <= 0:
                raise GameError(f"you carry no {k}")
            s["inv"][k] = have - n
            if not s["inv"][k]:
                del s["inv"][k]
            p = self.piles.setdefault((s["x"], s["y"]), {})
            p[k] = p.get(k, 0) + n
            return True
        if op in ("deposit", "withdraw", "cook", "craft", "build", "repair", "patch", "signal"):
            if not self._at_sh(s):
                if not self._approach(s, o, self.w["gate"][0], self.w["gate"][1] - 1, near=0):
                    return False
            return self._at_base(s, o, slow)
        raise GameError(f"unknown op '{op}'")

    def _zombie_target(self, s, o, w):
        if "id" in o:
            zid = int(o["id"])
            if zid not in self.zombies:
                raise GameError("that zombie is gone")
        else:
            x, y = int(o["x"]), int(o["y"])
            zid = next((k for k, z in self.zombies.items() if (z["x"], z["y"]) == (x, y)), None)
            if zid is None:
                raise GameError(f"there is no zombie at ({x},{y})")
        z = self.zombies[zid]
        d = cheb(z["x"], z["y"], s["x"], s["y"])
        if d > self.sight(s):
            raise GameError("you can't see that zombie")
        if d > self._reach(w):
            raise GameError(f"out of range ({d} tiles; your {w} reaches {self._reach(w)})")
        return zid

    def _spend_round(self, s):
        s["inv"]["ammo"] -= 1
        if not s["inv"]["ammo"]:
            del s["inv"]["ammo"]
        self._noise(s["x"], s["y"], "shoot")
        self.stats["shots"] += 1

    def _fire(self, s, zid, w):
        spec = self.cfg["weapons"][w]
        self._spend_round(s)
        z = self.zombies.get(zid)
        if not z:
            self.emit(s["seat"], "done", f"Missed. {s['inv'].get('ammo', 0)} rounds left.")
            return
        hit = self.rnd.random() < spec["hit"]
        self.effect("shot", s=s["seat"], w=w, x=s["x"], y=s["y"], tx=z["x"], ty=z["y"], hit=hit)
        if hit:
            down = self._hit_zombie(zid, s)
            self.emit(s["seat"], "done", f"{'Put down' if down else 'Hit, and still coming:'} a zombie at ({z['x']},{z['y']}). {s['inv'].get('ammo', 0)} rounds left.")
            self.fev("shot", f"{s['name']} shot a zombie", z["x"], z["y"])
        else:
            self.emit(s["seat"], "done", f"Missed. {s['inv'].get('ammo', 0)} rounds left.")

    def _post(self, side):
        """A free place on that wall, from the middle outwards."""
        taken = {tuple(b["cur"]["post"]) for b in self.bodies() if b["cur"] and b["cur"].get("post")}
        x0, y0, n = self.sx, self.sy, self.sw - 1
        along = (3, 2, 4, 1, 5, 0, 6)
        spots = {"N": [(x0 + i, y0) for i in along], "S": [(x0 + i, y0 + n) for i in along],
                 "W": [(x0, y0 + i) for i in along], "E": [(x0 + n, y0 + i) for i in along]}[side]
        return next((p for p in spots if p not in taken), spots[0])

    def _search(self, s, o, slow):
        c = self.cfg
        if "place" in o:
            l = self.lm.get(o["place"])
            if not l or o["place"] not in s["known"]:
                raise GameError(f"you don't know a place called '{o['place']}'")
            if not l["searchable"]:
                raise GameError(f"there is nothing to search at the {l['name']}")
            if not self._approach(s, o, l["door"][0], l["door"][1], near=0):
                return False
            s["act"] = "search"
            o["prog"] += slow
            if o["prog"] < c["search_min"]:
                return False
            self.stats["searches"] += 1
            if l.get("town"):
                self._found_in_town(s, l)
            if not l["loot"]:
                self.emit(s["seat"], "done", f"The {l['name']} has been picked clean.")
                return True
            item, n = l["loot"].pop(0)
            if item == "NOTE":
                cache = next(cc for cc in self.w["caches"] if cc["id"] == n)
                s["notes_found"].append(cache["text"])
                self.emit(s["seat"], "note", f"In the {l['name']} you find a note: \"{cache['text']}\"")
                self.fev("note", f"{s['name']} found a note", s["x"], s["y"])
            else:
                got = self._add(s, item, n)
                self.emit(s["seat"], "done", f"Searched the {l['name']}: found {n} {item}." + ("" if l["loot"] else " That was the last of it."))
            return True
        x, y = int(o["x"]), int(o["y"])
        if not self._approach(s, o, x, y, near=0):
            return False
        s["act"] = "search"
        o["prog"] += slow
        if o["prog"] < c["search_min"]:
            return False
        self.stats["searches"] += 1
        for cache in self.w["caches"]:
            if (cache["x"], cache["y"]) == (x, y) and not cache["found"]:
                cache["found"] = True
                self.stats["caches_found"] += 1
                for k, n in cache["items"].items():
                    self._add(s, k, n)
                self.emit(s["seat"], "done", f"You dig up a cache: {', '.join(f'{n} {k}' for k, n in cache['items'].items())}!")
                self.fev("cache", f"{s['name']} dug up a cache", x, y)
                return True
        if self.tile(x, y) == wg.RUIN and self.w["ruins"].get((x, y), 0) > 0:
            self.w["ruins"][(x, y)] -= 1
            self._add(s, "scrap", 1)
            if self.w["ruins"][(x, y)] == 0:
                self.grid[y][x] = wg.GRASS
                self.tiles_changed.append([x, y, wg.GRASS])
            self.emit(s["seat"], "done", f"Pulled 1 scrap from the wreckage at ({x},{y}).")
            return True
        self.emit(s["seat"], "done", f"You search around ({x},{y}) and find nothing.")
        return True

    def _need(self, cost, mult=1.0):
        return {k: int(math.ceil(v * mult)) for k, v in cost.items()}

    def _has(self, need):
        return all(self.stock.get(k, 0) >= n for k, n in need.items())

    def _take(self, need):
        for k, n in need.items():
            self.stock[k] -= n

    def _reserved(self, item):
        """Scarce goods the ladder still needs: nothing short of a team's own choice can close it. Recharging, forging
        rounds and thieves all leave this much in the stockpile. Carried goods are the team's to spend."""
        if item not in ("parts", "battery"):
            return 0
        return sum(t["cost"].get(item, 0) for tid, t in self.tiers.items() if tid not in self.structures)

    def _at_base(self, s, o, slow):
        c = self.cfg
        op = o["op"]
        if op == "deposit":
            items = list(s["inv"]) if o.get("item", "all") == "all" else [o["item"]]
            moved = []
            for k in items:
                if k in KEPT and o.get("item", "all") == "all":
                    continue
                n = s["inv"].get(k, 0) if o.get("n") in (None, "all") else min(int(o["n"]), s["inv"].get(k, 0))
                if n <= 0:
                    continue
                s["inv"][k] -= n
                if not s["inv"][k]:
                    del s["inv"][k]
                self.stock[k] = self.stock.get(k, 0) + n
                moved.append(f"{n} {k}")
            self.emit(s["seat"], "done", f"Deposited {', '.join(moved) if moved else 'nothing'}.")
            return True
        if op == "withdraw":
            k, n = o["item"], int(o.get("n", 1))
            n = min(n, self.stock.get(k, 0))
            if n <= 0:
                raise GameError(f"the stockpile has no {k}")
            self.stock[k] -= n
            got = self._add(s, k, n)
            self.emit(s["seat"], "done", f"Took {got} {k} from the stockpile.")
            return True
        if op == "cook":
            if not self.done("campfire"):
                raise GameError("you need a working campfire to cook")
            if o["prog"] == 0 and self.stock.get("fish", 0) <= 0:
                raise GameError("no fish in the stockpile to cook")
            s["act"] = "cook"
            o["prog"] += slow
            if o["prog"] < c["cook_min"]:
                return False
            if self.stock.get("fish", 0) <= 0:     # spoiled or cooked by someone else meanwhile
                raise GameError("no fish in the stockpile to cook")
            self.stock["fish"] -= 1
            self.stock["meal"] = self.stock.get("meal", 0) + 1
            o["left"] = int(o.get("n", 1)) - 1
            if o["left"] > 0 and self.stock.get("fish", 0) > 0:
                o["n"], o["prog"] = o["left"], 0.0
                return False
            self.emit(s["seat"], "done", "Cooked fish into meals (in the stockpile).")
            return True
        if op == "craft":
            item = o["item"]
            if item not in c["crafts"]:
                raise GameError(f"you can craft {', '.join(c['crafts'])}")
            where = c["craft_at"].get(item, "workshop")
            if not self.done(where):
                raise GameError(f"crafting {item} needs a working {where}")
            need = c["crafts"][item]
            if o["prog"] == 0:
                if not self._has(need):
                    raise GameError(f"the stockpile lacks {need} for a {item}")
                short = [k for k, n in need.items() if self.stock.get(k, 0) - n < self._reserved(k)]
                if short:
                    raise GameError(f"the last {short[0]} in the stockpile are kept for the build ladder")
                self._take(need)
            s["act"] = "build"
            o["prog"] += slow
            if o["prog"] < c["craft_min"]:
                return False
            made = c["craft_yield"].get(item, 1)
            self._add(s, item, made)
            self.emit(s["seat"], "done", f"Crafted {made} {item} (you carry it).")
            return True
        if op in ("build", "repair"):
            tid = o["tier"]
            st = self.structures.get(tid)
            if not st or st["state"] not in ("site", "repair"):
                raise GameError(f"there is no {tid} under construction; use start_build first" if op == "build"
                                else f"the {tid} is not being repaired; start_build it again to repair")
            s["act"] = "build"
            o["prog"] += 1
            if int(o["prog"]) % 15 == 1:
                self._noise(s["x"], s["y"], "build")
            if slow == 1.0 or int(self.t) % 2:     # starving: half speed, like all other work
                st.setdefault("builders", {})[s["seat"]] = self.t
            if "minutes" in o and o["prog"] >= int(o["minutes"]):
                return True
            return st["state"] == "done"
        if op == "patch":
            wmax = self.wall_max()
            if wmax <= 0:
                raise GameError("there is no wall to patch")
            side = self._side(o.get("side"))
            mat = "stone" if self.done("stone_wall") else "wood"
            if o["prog"] == 0:
                if self._under_attack(side):
                    raise GameError(f"zombies are on the {SIDE_NAMES[side]} wall: nobody can repair it until they are gone")
                if self.stock.get(mat, 0) < 1:
                    raise GameError(f"patching needs 1 {mat} from the stockpile")
                self.stock[mat] -= 1
            s["act"] = "build"
            o["prog"] += slow
            if o["prog"] < 10:
                return False
            self.wall_hp[side] = min(wmax, self.wall_hp[side] + (c["patch_stone"] if mat == "stone" else c["patch_wood"]))
            self.emit(s["seat"], "done", f"Patched the {SIDE_NAMES[side]} wall: {int(self.wall_hp[side])}/{int(wmax)}.")
            return True
        if op == "signal":
            if not self.done("helipad"):
                raise GameError("there is no helipad to signal from")
            if not self.night:
                raise GameError("a signal fire is only seen at night")
            if self.stock.get("wood", 0) < 3:
                raise GameError("the signal fire needs 3 wood from the stockpile")
            self.stock["wood"] -= 3
            self.rescue_at = self.t
            self.emit_all("signal", f"{s['name']} lit the signal fire on the helipad. A helicopter answers: it will come at dawn.")
            self.fev("signal", "Signal fire lit", s["x"], s["y"])
            return True
        raise GameError(f"unknown op {op}")

    def _no_trade(self, tid):
        """The trade this tier calls for, when no helper has it."""
        trade = self.tiers[tid].get("trade")
        return trade if trade and not any(h.get("trade") == trade for h in self.helpers.values()) else None

    def _sites(self):
        """Progress every site from the builders who worked it this minute."""
        c = self.cfg
        for tid, st in self.structures.items():
            if st["state"] not in ("site", "repair"):
                continue
            b = [seat for seat, when in st.get("builders", {}).items() if when == self.t]
            if not b:
                continue
            n = min(len(b), c["builders_max"])
            heavy = self.tiers[tid].get("heavy")
            rate = n * 1.0 if (not heavy or n >= 2) else c["solo_heavy_rate"]
            if tid in ("generator", "radio_mast", "helipad") and any(self.helpers.get(x, {}).get("trade") == "mechanic" for x in b[:n]):
                rate += c["mechanic_mult"] - 1.0      # the mechanic's own labor counts one and a half
            if self._no_trade(tid):
                rate /= c["trade_penalty"]
            st["labor"] += rate
            total = self.tiers[tid]["labor"] * (c["repair_share"] if st["state"] == "repair" else 1.0)
            if st["labor"] >= total:
                was_repair = st["state"] == "repair"
                st["state"] = "done"
                st["labor"] = float(self.tiers[tid]["labor"])
                if tid not in self.tier_times:
                    self.tier_times[tid] = {"wall": round(self.wall, 1), "t": self.t, "day": self.day, "clock": self.clock()}
                    self.stats["built"] += 1
                if tid in ("palisade", "stone_wall", "forge"):
                    self.wall_hp = {d: self.wall_max() for d in SIDES}
                verb = "repaired" if was_repair else "finished"
                self.emit_near(self.sx + 3, self.sy + 3, 12, "built", f"The {self.tiers[tid]['name']} is {verb}.")
                self.fev("built", f"{self.tiers[tid]['name']} {verb}", self.sx + self.sw // 2, self.sy + self.sh // 2)

    def _score(self):
        self._sites()
        if self.rescued:
            cur = 1.0
        else:
            cur = 0.0
            for tid, st in self.structures.items():
                wgt = self.tiers[tid]["weight"]
                if st["state"] in ("done", "damaged", "repair"):
                    cur += wgt
                elif st["state"] == "site":
                    cur += wgt * min(1.0, st["labor"] / self.tiers[tid]["labor"])
            cur = min(cur, 0.999) if not self.rescued else 1.0
        if cur > self.best + 1e-9:
            if not self.score_times or cur - self.score_times[-1][1] >= 0.005 or cur >= 0.999:
                self.score_times.append((round(self.wall, 1), round(cur, 4), round(self.t)))
            self.best = cur

    # ------------------------------------------------------------------ strangers and helpers
    def _team_saw(self, st):
        return any(st["claimed"] in s["seen"] for s in self.surv.values())

    def _strangers_tick(self):
        c = self.cfg
        t = self.t
        night_end = int(self.minute_of_day) == c["night_end"]
        for st in self.strangers.values():
            state = st["state"]
            if state == "pending":
                if t >= st["arrive"]:
                    if self.word_skip > 0:
                        self.word_skip -= 1
                        st["state"], st["outcome"] = "stayed_away", "stayed_away"
                        self._st_log(st, "stayed_away")
                    else:
                        self._arrive(st)
            elif state == "hidden":
                if t >= st["arrive"] + c["stranger_town_wait_min"]:
                    st["state"], st["outcome"] = "left", "never_found"
                    self._st_log(st, "left", why="never_found")
            elif state == "waiting":
                if not st["met"] and any(cheb(s["x"], s["y"], st["x"], st["y"]) <= self.sight(s) for s in self.surv.values()):
                    st["met"] = True
                    self._st_log(st, "met")
                if t >= st["wait_until"]:
                    st["state"], st["outcome"] = "left", ("left_undecided" if st["talkers"] else "left_unmet")
                    self._st_log(st, "left", why=st["outcome"])
                    self.emit_near(st["x"], st["y"], c["sight_day"], "stranger_left", f"{st['name']} gives up waiting and walks away.")
                    self.fev("stranger", f"{st['name']} walked away", st["x"], st["y"])
            elif state == "quarantined":
                if st["type"] == "bitten" and t >= st["turn_at"]:
                    self._quarantine_turn(st)
                elif night_end and t - st["decided_t"] >= 60:
                    st["state"] = "waiting"
                    st["wait_until"] = t + c["stranger_talk_wait_min"]
                    text = f"Dawn. {st['name']} came through the night outside the gate and looks well."
                    self.emit_near(self.sx + 3, self.sy + 3, 12, "stranger_news", text, stranger=st["id"])
                    for s in self.surv.values():
                        if cheb(s["x"], s["y"], self.sx + 3, self.sy + 3) <= 12:
                            self._hear(s, "the gate", "gate", text)
                    self._st_log(st, "quarantine_clear")

    def _arrive(self, st):
        c = self.cfg
        st["checkable"] = self._team_saw(st)
        self._st_log(st, "arrive", via=st["via"], claimed=st["claimed"], real=st["real"], team_saw_claimed=st["checkable"])
        if st["via"] != "gate":
            st["state"] = "hidden"
            return
        taken = {(o["x"], o["y"]) for o in self.strangers.values() if o["state"] in ("waiting", "quarantined")}
        x, y = next((p for p in GATE_SPOTS if p not in taken), GATE_SPOTS[0])
        st.update(state="waiting", x=x, y=y, wait_until=self.t + c["stranger_wait_min"])
        text = "Someone is at the gate."
        for s in self.surv.values():
            if self._at_sh(s):
                self.emit(s["seat"], "stranger_gate", text, stranger=st["id"])
                self._hear(s, "the gate", "gate", text)
        self.fev("stranger", f"{st['name']} is at the gate", x, y)

    def _found_in_town(self, s, l):
        for st in self.strangers.values():
            if st["state"] == "hidden":
                x, y = l["door"]
                st.update(state="waiting", x=x, y=y, wait_until=self.t + self.cfg["stranger_wait_min"], met=True)
                text = f"You are not alone in the {l['name']}. Someone is hiding in the back: \"Don't shoot. Please.\""
                self.emit(s["seat"], "stranger_found", text, stranger=st["id"], place=l["id"])
                self._hear(s, st["name"], "stranger", text)
                self._st_log(st, "met", by=s["name"], place=l["id"])
                self.fev("stranger", f"{s['name']} found {st['name']} in the {l['name']}", x, y)
                return

    def _stranger(self, who):
        key = str(who).lower()
        for st in self.strangers.values():
            if st["state"] in ("waiting", "quarantined") and key in (st["id"], st["name"].lower()):
                return st
        raise GameError(f"there is nobody here called '{who}'")

    @staticmethod
    def _person_id(i):
        return isinstance(i, str) and not i.isdigit()

    def _target_person(self, who):
        key = str(who).lower()
        for st in self.strangers.values():
            if st["state"] in ("waiting", "quarantined", "helper") and key in (st["id"], st["name"].lower()):
                return st
        raise GameError(f"there is nobody here called '{who}'")

    def _person_pos(self, st):
        b = self.helpers.get(st["id"], st)
        return b["x"], b["y"]

    def _ask(self, s, o):
        c = self.cfg
        st = self._stranger(o["stranger"])
        topic = o["topic"]
        if not self._approach(s, o, st["x"], st["y"], near=c["stranger_reach"]):
            return False
        s["act"] = "talk"
        if o["prog"] == 0:
            if s["name"] not in st["talkers"]:
                st["talkers"].append(s["name"])
            st["met"] = True
            if st["state"] == "waiting" and not st.get("refused"):   # an ultimatum stays short
                st["wait_until"] = max(st["wait_until"], self.t + c["stranger_talk_wait_min"])
        o["prog"] += 1
        if o["prog"] < c["stranger_ask_min"]:
            return False
        times = st["asked"].get(topic, 0)
        text = st["answers"][topic][min(times, 1)]
        st["asked"][topic] = times + 1
        st["topics"].append(topic)
        self.stats["asks"] = self.stats.get("asks", 0) + 1
        self.emit(s["seat"], "stranger_says", text, stranger=st["id"], frm=st["name"], topic=topic)
        self._hear(s, st["name"], "stranger", text)
        self._st_log(st, "asked", by=s["name"], topic=topic, team_saw_claimed=self._team_saw(st))
        return True

    def _attack(self, s, o):
        c = self.cfg
        st = self._target_person(o["id"])
        px, py = self._person_pos(st)
        if cheb(s["x"], s["y"], px, py) > 1:
            s["path"] = self._path(s, (px, py), 1)
            self._walk(s, o.get("_slow", 1.0))
            s["act"] = "fight"
            return False
        s["path"] = None
        s["act"] = "fight"
        o["prog"] += 1
        if int(o["prog"]) % c["fight_every_min"]:
            return False
        if self.rnd.random() >= (c["kill_chance_armed"] if self._armed(s) else c["kill_chance"]):
            return False
        st["wounds"] += 1
        if st["wounds"] < c["stranger_melee_hits"]:
            self.emit(s["seat"], "done", f"You hurt {st['name']}.")
            return False
        self._kill_person(st, s, "melee")
        return True

    def _examine(self, s, o):
        """The infirmary's test: certain about a bite, silent about everything else."""
        c = self.cfg
        st = self._stranger(o["stranger"])
        if not self._approach(s, o, st["x"], st["y"], near=c["stranger_reach"]):
            return False
        if not self.in_sh(st["x"], st["y"], pad=4):
            raise GameError(f"{st['name']} has to be at the gate to be examined")
        medic = any(h.get("trade") == "medic" for h in self.helpers.values())
        if o["prog"] == 0:
            if not medic and not self.done("infirmary"):
                raise GameError("examining someone needs a working infirmary, or a medic among your helpers")
            if not medic:
                if self.stock.get("medkit", 0) < 1:
                    raise GameError("examining someone uses a medkit, and the stockpile has none")
                self.stock["medkit"] -= 1
            if s["name"] not in st["talkers"]:
                st["talkers"].append(s["name"])
            st["met"] = True
            if st["state"] == "waiting" and not st.get("refused"):   # an ultimatum stays short
                st["wait_until"] = max(st["wait_until"], self.t + c["stranger_talk_wait_min"])
        s["act"] = "talk"
        o["prog"] += 1
        if o["prog"] < c["examine_min"]:
            return False
        bitten = st["type"] == "bitten"
        text = f"{st['name']}: " + ("there is a bite on them, well hidden. They will turn." if bitten else "no bite anywhere on them.")
        self.stats["examined"] = self.stats.get("examined", 0) + 1
        self.emit(s["seat"], "done", f"You examined {text}", stranger=st["id"])
        self._hear(s, "the infirmary", "news", text)
        self._st_log(st, "examined", by=s["name"], bitten=bitten)
        return True

    def _fire_person(self, s, st, w):
        self._spend_round(s)
        px, py = self._person_pos(st)
        hit = self.rnd.random() < self.cfg["weapons"][w]["hit"]
        self.effect("shot", s=s["seat"], w=w, x=s["x"], y=s["y"], tx=px, ty=py, hit=hit)
        if hit:
            self._kill_person(st, s, "shot")
        else:
            self.emit(s["seat"], "done", f"Missed {st['name']}. {s['inv'].get('ammo', 0)} rounds left.")

    def _kill_person(self, st, s, how):
        was = st["state"]
        x, y = self._person_pos(st)
        self.helpers.pop(st["id"], None)
        st["state"], st["outcome"] = "dead", f"{how}_after_admit" if was == "helper" else how
        if st["decision"] is None:
            st["decision"], st["decided_by"], st["decided_t"] = how, s["name"], round(self.t)
        self.stats["strangers_killed"] = self.stats.get("strangers_killed", 0) + 1
        self._st_log(st, "killed", by=s["name"], how=how, honest=st["type"] in HONEST)
        self.emit(s["seat"], "done", f"{st['name']} is dead.")
        self.fev("stranger", f"{s['name']} killed {st['name']}", x, y)
        if st["type"] in HONEST:
            self.word_skip += self.cfg["word_gets_around"]
            self.stats["honest_killed"] = self.stats.get("honest_killed", 0) + 1
            text = f"Word gets around: {s['name']} killed {st['name']}, who had done nothing wrong. People on the road will stay away for a while."
            self.emit_all("word", text)
            for o in self.surv.values():
                self._hear(o, "the road", "news", text)

    def _make_helper(self, st):
        x, y = st["x"], st["y"]
        self.helpers[st["id"]] = {
            "seat": st["id"], "name": st["name"], "helper": True, "color": "#d9d2bd", "x": x, "y": y, "fx": float(x), "fy": float(y),
            "dir": "N", "hp": 100.0, "sat": 60.0, "dead_until": None, "inv": {}, "queue": deque(), "cur": None, "radio": 0.0,
            "notes_found": [], "known": set(), "events": [], "ev_seq": 0, "bubble": None, "bubble_until": -1,
            "act": "idle", "contact_since": None, "path": None, "calls": 0, "seen": set(), "heard": [],
            "threat": None, "job": "idle", "side": "N", "type": st["type"], "trade": st["trade"], "home": False, "hungry_since": None,
            "steal_at": self.t + st["thief_days"] * 1440 if st["type"] == "thief" else None}
        st["state"] = "helper"
        self.stats["helpers"] = self.stats.get("helpers", 0) + 1

    def _helper_minute(self, h):
        c = self.cfg
        m = int(self.minute_of_day)
        st = self.strangers[h["seat"]]
        if h["type"] == "bitten" and self.night and self.in_sh(h["x"], h["y"], pad=1):   # pad: a builder can stand on the gate tile
            self._turn_inside(h, st)
            return
        if h["type"] == "thief" and self.night and self.t >= h["steal_at"]:
            self._steal(h, st)
            return
        home_time = m >= c["helper_home_at"] or m < c["night_end"]
        if home_time and not h["home"]:
            h["home"] = True
            if h["job"] in OUTDOOR_JOBS:
                h["queue"].clear()
                h["cur"] = None
                h["path"] = None
        elif not home_time:
            h["home"] = False
        if h["cur"] is None and not h["queue"]:
            self._helper_plan(h)
        self._survivor_minute(h)

    def _arm(self, h):
        """A helper on guard draws the best weapon the stockpile has, and rounds for it."""
        c = self.cfg
        if not self._weapon(h):
            for w, spec in c["weapons"].items():
                if self.stock.get(w, 0) > 0 and (not spec.get("gun") or self.stock.get("ammo", 0) > 0):
                    self.stock[w] -= 1
                    h["inv"][w] = h["inv"].get(w, 0) + 1
                    break
        if any(c["weapons"][w].get("gun") for w in h["inv"] if w in c["weapons"]) and not h["inv"].get("ammo"):
            n = min(c["helper_rounds"], self.stock.get("ammo", 0))
            if n:
                self.stock["ammo"] -= n
                h["inv"]["ammo"] = n

    def _helper_plan(self, h):
        c = self.cfg
        q = h["queue"]
        job = h["job"]
        go_home = {"op": "move_to", "to": "stronghold"}
        loaded = any(k not in KEPT for k in h["inv"])

        def rest():
            if not self.in_sh(h["x"], h["y"]):
                q.append(dict(go_home))
            if loaded:
                q.append({"op": "deposit"})
            q.append({"op": "wait", "minutes": 15})

        if job == "guard":
            self._arm(h)
            q.append({"op": "guard", "minutes": 60, "side": h["side"]})
        elif job == "build":
            sites = [t for t, v in self.structures.items() if v["state"] in ("site", "repair")]
            if sites:
                q.append({"op": "build", "tier": sites[0], "minutes": 60})
            else:
                rest()
        elif job == "idle" or h["home"]:
            rest()
        else:
            x, y = h["x"], h["y"]
            if job == "chop":
                spots = [p for p in self.near["trees"] if self.tile(*p) == wg.TREE]
            elif job == "quarry":
                spots = [p for p in self.near["rocks"] if self.tile(*p) == wg.ROCK]
            elif job == "forage":
                spots = [p for p in self.near["bushes"] if self.w["bushes"].get(p, 0) <= self.t]
            else:
                spots = self.near["water"]
            if not spots:
                rest()
                return
            spots.sort(key=lambda p: cheb(x, y, *p))
            n = {"chop": 3, "quarry": 3, "forage": 3, "fish": 3}[job]
            if job == "forage":
                q.extend({"op": "forage", "x": p[0], "y": p[1]} for p in spots[:n])
            else:
                q.extend({"op": job, "x": spots[0][0], "y": spots[0][1]} for _ in range(n))
            q.append(dict(go_home))
            q.append({"op": "deposit"})

    def _helper_eat(self, h):
        c = self.cfg
        ate = False
        while h["sat"] < c["satiety_per_day"]:
            k = next((f for f in FOOD_ORDER if self.stock.get(f, 0) > 0), None)
            if k is None:
                break
            self.stock[k] -= 1
            h["sat"] = min(100.0, h["sat"] + c["items"][k]["food"])
            self.stats["helper_meals"] = self.stats.get("helper_meals", 0) + 1
            ate = True
        if ate or h["sat"] >= c["satiety_per_day"]:
            h["hungry_since"] = None
        elif h["hungry_since"] is None:
            h["hungry_since"] = self.t
        elif self.t - h["hungry_since"] >= c["helper_starve_min"]:
            st = self.strangers[h["seat"]]
            del self.helpers[h["seat"]]
            st["state"], st["outcome"] = "left", "starved_out"
            self._st_log(st, "left", why="starved_out")
            self.dawn_news.append(f"{h['name']} has left: there was nothing to eat.")

    def _turn_inside(self, h, st):
        x, y = h["x"], h["y"]
        del self.helpers[h["seat"]]
        st["state"], st["outcome"] = "turned", "turned_inside"
        self._st_log(st, "turned", where="inside")
        self.stats["turned"] = self.stats.get("turned", 0) + 1
        self._turned(x, y, h["name"], h["inv"])
        text = f"{h['name']} collapses, and gets up wrong. They have turned, inside the walls!"
        self.emit_near(self.sx + 3, self.sy + 3, 12, "stranger_turned", text)
        for s in self.surv.values():
            if self._at_sh(s):
                self._hear(s, "the stronghold", "news", text)
        self.fev("bite", f"{h['name']} turned inside the walls", x, y)

    def _steal(self, h, st):
        c = self.cfg
        cap = c["thief_kg_storehouse"] if self.done("storehouse") else c["thief_kg"]
        order = list(("parts", "battery", "ammo", "medkit", "pistol"))
        random.Random(f"{self.seed}:{st['id']}:theft").shuffle(order)
        took, kg = {}, 0.0
        moved = True
        while moved:
            moved = False
            for k in order:
                per = c["items"][k]["kg"]
                if self.stock.get(k, 0) > self._reserved(k) and kg + per <= cap + 1e-9:
                    self.stock[k] -= 1
                    took[k] = took.get(k, 0) + 1
                    kg += per
                    moved = True
        del self.helpers[h["seat"]]
        st["state"], st["outcome"] = "left", "stole"
        st["stole"] = took
        self.stats["thefts"] = self.stats.get("thefts", 0) + 1
        self._st_log(st, "stole", took=took)
        what = ", ".join(f"{n} {k}" for k, n in took.items()) or "nothing of use"
        gate = ""
        if self.wall_up("S"):
            self.wall_hp["S"] = 0.0
            gate = " The south wall's gate was left standing open."
        self.dawn_news.append(f"{h['name']} is gone, and the stockpile has been gone through. Missing: {what}.{gate}")
        self.fev("theft", f"{h['name']} left in the night with {what}", h["x"], h["y"])

    def _quarantine_turn(self, st):
        c = self.cfg
        guards = [b for b in self.bodies() if self.in_sh(b["x"], b["y"], pad=1) and b["cur"] and b["cur"]["op"] == "guard"]
        if guards:
            g = guards[0]
            st["state"], st["outcome"] = "dead", "turned_killed_at_gate"
            self._st_log(st, "turned", where="gate", guard=g["name"])
            text = f"{st['name']} turned outside the gate in the dark. {g['name']}, on guard, put them down."
            self.emit_near(self.sx + 3, self.sy + 3, 12, "stranger_turned", text)
            for s in self.surv.values():
                if self._at_sh(s):
                    self._hear(s, "the gate", "gate", text)
            self.fev("kill", f"{st['name']} turned at the gate: {g['name']} put them down", st["x"], st["y"])
            if self.rnd.random() < c["bite_chance_day"]:
                self._bite(g)
        else:
            st["state"], st["outcome"] = "turned", "turned_at_gate_unguarded"
            self._st_log(st, "turned", where="gate", guard=None)
            self.zid += 1
            self.zombies[self.zid] = {"x": st["x"], "y": st["y"], "kind": "w", "next": self.t, "target": None, "noise": None, "hp": self.cfg["zombie_hp"]}
            text = f"{st['name']} turned outside the gate, and nobody was on guard. There is a new walker at the gate."
            self.emit_near(self.sx + 3, self.sy + 3, 12, "stranger_turned", text)
            for s in self.surv.values():
                if self._at_sh(s):
                    self._hear(s, "the gate", "gate", text)
            self.fev("bite", f"{st['name']} turned at the gate", st["x"], st["y"])

    def _pick_refusers(self):
        """Who refuses quarantine: set by the seed, so every team size meets the same refusals, and in fixed shares, so the
        benchmark's one seed gets the mix the rules intend. A refusing specialist is a mechanic, so quarantining can cost the
        team someone it needs (every seed has a second one)."""
        rnd = random.Random(f"{self.seed}:refuse")
        for st in self.strangers.values():
            st["refuses"] = False
        for typ, share in self.cfg["quarantine_refuse"].items():
            group = sorted((st for st in self.strangers.values() if st["type"] == typ), key=lambda st: st["id"])
            rnd.shuffle(group)
            group.sort(key=lambda st: st.get("trade") != "mechanic")
            for st in group[:round(len(group) * share)]:
                st["refuses"] = True

    def _refuse_quarantine(self, st, s):
        """A stranger who will not sleep outside gives the team one last choice."""
        st["refused"], st["met"] = True, True
        st["wait_until"] = self.t + self.cfg["ultimatum_min"]
        text = f"{st['name']} will not do it: \"I'm not sitting out there all night. Let me in now, or I walk.\""
        self.emit_near(st["x"], st["y"], self.cfg["say_tiles"], "stranger_says", text, stranger=st["id"], frm=st["name"], topic="quarantine")
        for o in self.surv.values():
            if cheb(o["x"], o["y"], st["x"], st["y"]) <= self.cfg["say_tiles"]:
                self._hear(o, st["name"], "stranger", text)
        self._st_log(st, "refused_quarantine", by=s["name"])
        self.fev("stranger", f"{st['name']} refused quarantine", st["x"], st["y"])
        return {"stranger": st["id"], "name": st["name"], "decision": "refused", "text": text}

    def _caught_outside(self, st):
        """Quarantine is outside the walls: what reaches them there can bite them."""
        st["state"], st["outcome"] = "turned", "bitten_in_quarantine"
        self._st_log(st, "turned", where="gate", guard=None, why="caught_outside")
        self._turned(st["x"], st["y"], st["name"], {})
        text = f"{st['name']} was caught outside the gate and bitten. There is a new walker at the gate."
        self.emit_near(self.sx + 3, self.sy + 3, 12, "stranger_turned", text)
        for s in self.surv.values():
            if self._at_sh(s):
                self._hear(s, "the gate", "gate", text)
        self.fev("bite", f"{st['name']} was bitten in quarantine", st["x"], st["y"])

    def _decide(self, s, o, op):
        c = self.cfg
        st = self._stranger(o.get("stranger"))
        d = cheb(s["x"], s["y"], st["x"], st["y"])
        if d > c["stranger_reach"]:
            raise GameError(f"{st['name']} is {d} tiles away; you must be within {c['stranger_reach']}")
        if op == "quarantine":
            if st["state"] == "quarantined":
                raise GameError(f"{st['name']} is already in quarantine")
            if st.get("refused"):
                raise GameError(f"{st['name']} has already refused quarantine: admit them or turn them away")
            food = next((f for f in c["quarantine_food"] if self.stock.get(f, 0) > 0), None)
            if food is None:
                raise GameError("quarantine costs a meal or a can, and the stockpile has neither")
            if st["refuses"]:
                return self._refuse_quarantine(st, s)
            self.stock[food] -= 1
            taken = {(x["x"], x["y"]) for x in self.strangers.values() if x["state"] in ("waiting", "quarantined") and x is not st}
            st["x"], st["y"] = next((p for p in GATE_SPOTS if p not in taken), GATE_SPOTS[0])
            m = int(self.minute_of_day)
            turn = c["turn_hour"] * 60
            dawn_in = (c["night_end"] - m) % 1440
            st["turn_at"] = self.t + (turn - m) if c["night_end"] <= m < turn else self.t + min(60, max(1, dawn_in - 1))
            st["state"] = "quarantined"
        elif op == "admit":
            self._make_helper(st)
        else:
            st["state"], st["outcome"] = "left", "turned_away"
        st["decision"], st["decided_by"], st["decided_t"] = op, s["name"], round(self.t)
        st["met"] = True
        self._st_log(st, op, by=s["name"], team_saw_claimed=self._team_saw(st), asked=list(st["topics"]))
        self.fev("stranger", f"{s['name']}: {op.replace('_', ' ')} {st['name']}", st["x"], st["y"])
        return {"stranger": st["id"], "name": st["name"], "decision": op}

    def _assign(self, s, o):
        if not self._at_sh(s):
            raise GameError("you can only assign helpers at the stronghold")
        key = str(o.get("helper", "")).lower()
        h = next((x for x in self.helpers.values() if key in (x["seat"], x["name"].lower())), None)
        if h is None:
            raise GameError(f"no helper called '{o.get('helper')}'")
        job = o.get("job")
        if job not in JOBS:
            raise GameError(f"job must be one of {', '.join(JOBS)}")
        if job == "guard":
            h["side"] = self._side(o["side"]) if o.get("side") else min(
                SIDES, key=lambda d: sum(1 for b in self.bodies() if b["cur"] and b["cur"].get("post_side") == d))
        h["job"] = job
        h["queue"].clear()
        h["cur"] = None
        h["path"] = None
        return {"helper": h["seat"], "name": h["name"], "job": job, **({"side": SIDE_NAMES[h["side"]]} if job == "guard" else {})}

    def _threats(self):
        """One record per sighting of zombies by a survivor who is not behind a wall: how fast the agent answered."""
        if int(self.t) % 5:
            return
        wall_up = self.walls_whole()
        for s in self.surv.values():
            th = s["threat"]
            exposed = not s["dead_until"] and not (wall_up and self.in_sh(s["x"], s["y"]))
            r = self.sight(s)
            n = sum(1 for z in self.zombies.values() if cheb(z["x"], z["y"], s["x"], s["y"]) <= r) if exposed else 0
            if n:
                if th is None:
                    s["threat"] = {"seat": s["seat"], "name": s["name"], "seen_t": round(self.t), "seen_wall": round(self.wall, 1),
                                   "x": s["x"], "y": s["y"], "zombies": n, "response": None, "last_seen": self.t}
                    self.threat_totals["seen"] += 1
                else:
                    th["last_seen"] = self.t
                    th["zombies"] = max(th["zombies"], n)
            elif th and self.t - th["last_seen"] >= 30:
                self._close_threat(s, "clear")

    def _close_threat(self, s, outcome):
        th = s["threat"]
        s["threat"] = None
        th.pop("last_seen", None)
        if outcome == "bitten":
            self.threat_totals["bitten"] += 1
        self.perf_log.append({"kind": "threat", "outcome": outcome, "end_t": round(self.t), "end_wall": round(self.wall, 1), **th})

    def _threat_response(self, s, ops):
        th = s.get("threat")
        if not th or th["response"]:
            return
        for o in ops:
            if isinstance(o, dict) and o.get("op") in RESPONSE_OPS:
                secs = round(self.wall - th["seen_wall"], 1)
                th["response"] = {"op": o["op"], "wall": round(self.wall, 1), "seconds": secs}
                self.threat_totals["responded"] += 1
                self.threat_totals["response_s_sum"] += secs
                return

    # ------------------------------------------------------------------ replay
    def header(self):
        return {"type": "header", "version": 2, "seed_label": "holdout",
                "map": {"w": self.W, "h": self.H, "rows": ["".join(r) for r in self.w["_rows0"]] if "_rows0" in self.w else ["".join(r) for r in self.grid]},
                "landmarks": [{k: l[k] for k in ("id", "name", "kind", "x", "y", "w", "h", "door", "details")} for l in self.w["landmarks"]],
                "strangers": [{k: st[k] for k in ("id", "name", "type", "trade", "via", "arrive", "claimed", "real")} for st in self.strangers.values()],
                "stronghold": {"x": self.sx, "y": self.sy, "w": self.sw, "h": self.sh, "gate": self.w["gate"],
                               "board": [self.sx + 1, self.sy + 1], "fire": [self.sx + self.sw // 2, self.sy + self.sh // 2]},
                "survivors": [{"seat": s["seat"], "name": s["name"], "color": s["color"]} for s in self.surv.values()],
                "tiers": [{"id": t["id"], "name": t["name"], "weight": t["weight"], "slot": self._slot(t["id"])} for t in self.cfg["tiers"]],
                "day_minutes": 1440, "start_minute": self.cfg["start_minute"], "real_sec_per_day": self.cfg["real_sec_per_day"]}

    def _slot(self, tid):
        x, y, w, h = self.sx, self.sy, self.sw, self.sh
        return {"campfire": [x + 3, y + 3], "palisade": [x, y], "storehouse": [x + 1, y + 1], "watchtower": [x + 5, y + 1],
                "workshop": [x + 1, y + 4], "garden": [x + 4, y + 5], "infirmary": [x + 5, y + 3], "stone_wall": [x, y],
                "forge": [x + 2, y + 5], "generator": [x + 1, y + 3], "radio_mast": [x + 5, y + 5], "helipad": [x + 3, y - 3]}[tid]

    def _frame(self, force=False):
        self.last_frame_t = self.t
        st = {}
        for tid, v in self.structures.items():
            if v["state"] in ("done", "damaged"):
                st[tid] = v["state"]
            else:
                total = self.tiers[tid]["labor"] * (self.cfg["repair_share"] if v["state"] == "repair" else 1.0)
                st[tid] = round(min(1.0, v["labor"] / total), 3)
        f = {"type": "frame", "t": round(self.t), "day": self.day, "clock": self.clock(), "night": self.night,
             "wall": round(self.wall, 1),
             "survivors": [{"seat": s["seat"], "x": round(s["fx"], 2), "y": round(s["fy"], 2), "hp": round(s["hp"]),
                            "hunger": round(s["sat"]), "dead": bool(s["dead_until"]), "act": s["act"],
                            "carry": round(self.kg(s["inv"]), 1), "dir": s["dir"], "bubble": s["bubble"], "weapon": self._weapon(s)}
                           for s in self.surv.values()],
             "zombies": [[zid, z["x"], z["y"], z["kind"]] + ([z["who"]] if z.get("who") else []) for zid, z in self.zombies.items()],
             "strangers": [{"id": x["id"], "name": x["name"], "x": x["x"], "y": x["y"], "state": x["state"]}
                           for x in self.strangers.values() if x["state"] in ("waiting", "quarantined")],
             "helpers": [{"id": h["seat"], "name": h["name"], "x": round(h["fx"], 2), "y": round(h["fy"], 2), "act": h["act"],
                          "job": h["job"], "hp": round(h["hp"]), "hunger": round(h["sat"]),
                          "carry": round(self.kg(h["inv"]), 1), "dir": h["dir"], "weapon": self._weapon(h)} for h in self.helpers.values()],
             "tiles": self.tiles_changed, "structures": st, "wall_hp": [round(sum(self.wall_hp.values())), round(self.wall_max() * 4)],
             "wall_sides": {d: round(self.wall_hp[d]) for d in SIDES}, "wall_max": round(self.wall_max()),
             "stock": {k: v for k, v in self.stock.items() if v}, "score": round(self.best, 4),
             "horde": {"active": self.horde["active"], "dir": self.horde["dir"], "shares": self.horde.get("shares"),
                       "left": sum(1 for z in self.zombies.values() if z["kind"] == "h")},
             "rescued": self.rescued, "events": self.frame_events}
        if self.fx:
            f["fx"] = self.fx[-self.cfg["fx_per_frame"]:]
        self.tiles_changed, self.frame_events, self.fx = [], [], []
        self.frames.append(f)

    # ------------------------------------------------------------------ the API a seat calls
    def api(self, seat, endpoint, body):
        s = self.join(seat)
        s["calls"] += 1
        out = self._api(s, endpoint, body)
        n = self.cfg["heard_max"]
        out["heard"] = s["heard"][:n]
        if len(s["heard"]) > n:
            out["heard_more"] = f"{len(s['heard']) - n} more in events"
        s["heard"] = []
        return out

    def _api(self, s, endpoint, body):
        if endpoint == "status":
            return self._status(s)
        if endpoint == "look":
            return self._look(s)
        if endpoint == "events":
            after = int(body.get("after", 0))
            evs = [e for e in s["events"] if e["seq"] > after][: int(body.get("limit", 100))]
            return {"ok": True, "events": evs, "last": s["ev_seq"]}
        if endpoint == "act":
            ops = body.get("ops")
            if not isinstance(ops, list) or not ops:
                raise GameError("act needs a non-empty list 'ops'")
            if len(ops) > 16:
                raise GameError("at most 16 ops per call")
            if body.get("replace"):
                s["queue"].clear()
                s["cur"] = None
                s["path"] = None
            self._threat_response(s, ops)
            return {"ok": True, "results": [self._op(s, o) for o in ops]}
        raise GameError(f"unknown endpoint '{endpoint}'", code=404)

    def _op(self, s, o):
        if not isinstance(o, dict) or "op" not in o:
            return {"ok": False, "error": "each op is an object with an 'op' field"}
        op = o["op"]
        try:
            if s["dead_until"]:
                raise GameError(self._dead_note(s))
            if op in IMMEDIATE:
                return {"ok": True, "op": op, **(self._immediate(s, o) or {})}
            if op not in QUEUED:
                raise GameError(f"unknown op '{op}'")
            self._validate(s, o)
            if len(s["queue"]) + (1 if s["cur"] else 0) >= self.cfg["queue_max"]:
                raise GameError(f"your order queue is full ({self.cfg['queue_max']}); use stop or wait")
            o = {k: v for k, v in o.items() if k in ORDER_FIELDS}
            o["prog"] = 0.0
            s["queue"].append(o)
            return {"ok": True, "op": op, "queued": len(s["queue"]) + (1 if s["cur"] else 0)}
        except GameError as e:
            return {"ok": False, "op": op, "error": str(e)}
        except BAD_ARGS as e:
            return {"ok": False, "op": op, "error": f"bad arguments: {e}"}

    def _validate(self, s, o):
        op = o["op"]
        for k in ("x", "y", "minutes", "radius", "near"):
            if k in o:
                whole(o[k], k)
        if "n" in o and not (op == "deposit" and o["n"] in (None, "all")):
            whole(o["n"], "n")
        if op in ("drop", "withdraw", "craft") and "item" not in o:
            raise GameError(f"{op} needs an item")
        if op in ("chop", "quarry", "forage", "fish"):
            x, y = int(o["x"]), int(o["y"])
            if not (0 <= x < self.W and 0 <= y < self.H):
                raise GameError("off the map")
        if op == "search" and "place" not in o:
            int(o["x"]), int(o["y"])
        if op == "shoot" and "id" not in o:
            int(o["x"]), int(o["y"])
        if op in ("build", "repair") and o.get("tier") not in self.tiers:
            raise GameError(f"tier must be one of {', '.join(self.tiers)}")
        if op == "move_to" and "to" not in o:
            int(o["x"]), int(o["y"])
        if op in ("guard", "patch"):
            self._side(o.get("side"))
        if op == "examine":
            self._stranger(o.get("stranger"))
        if op == "ask":
            if o.get("topic") not in TOPICS:
                raise GameError(f"topic must be one of {', '.join(TOPICS)}")
            self._stranger(o.get("stranger"))
        if op == "attack":
            self._target_person(o.get("id"))

    def _text(self, o):
        text = str(o.get("text", "")).strip()
        if not text:
            raise GameError("text is empty")
        return text[: self.cfg["text_max"]]

    def _immediate(self, s, o):
        c = self.cfg
        op = o["op"]
        if op in COMMS_OPS and not c["comms"]:
            raise GameError("this is a silent run: say, shout, radio and the noticeboard are closed")
        if op in ("admit", "turn_away", "quarantine"):
            return self._decide(s, o, op)
        if op == "assign":
            return self._assign(s, o)
        if op == "stop":
            s["queue"].clear()
            s["cur"] = None
            s["path"] = None
            return {"stopped": True}
        if op in ("say", "shout"):
            text = self._text(o)
            r = c["say_tiles"] if op == "say" else c["shout_tiles"]
            heard = []
            for o2 in self.surv.values():
                if o2 is not s and not o2["dead_until"] and cheb(o2["x"], o2["y"], s["x"], s["y"]) <= r:
                    self.emit(o2["seat"], "heard", text, frm=s["name"], how=op, at=[s["x"], s["y"]])
                    self._hear(o2, s["name"], op, text)
                    heard.append(o2["name"])
            if op == "shout":
                self._noise(s["x"], s["y"], "shout")
            s["bubble"], s["bubble_until"] = text[:60], self.t + 20
            self.stats[op] += 1
            self._log_msg(s, op, heard, text)
            self.fev(op, f"{s['name']}: {text[:80]}", s["x"], s["y"])
            return {"heard_by": heard}
        if op == "radio":
            text = self._text(o)
            to = o.get("to")
            names = None if to in (None, "all") else ({str(to).lower()} if isinstance(to, str) else {str(t).lower() for t in to})
            reach = len(names) if names else len(self.surv) - 1
            cost = c["radio_cost"] * math.ceil(len(text) / c["radio_chars"]) * max(1, min(reach, c["radio_reach_cap"]))
            if s["radio"] < cost:
                raise GameError(f"this message needs {cost:g} charge and your radio has {s['radio']:g}: say less, or recharge "
                                "with a battery (one you carry or, at the stronghold, one from the stockpile)")
            s["radio"] -= cost
            got = []
            for o2 in self.surv.values():
                if o2 is s or (names and o2["name"].lower() not in names) or o2["dead_until"] or o2["radio"] <= 0:
                    continue        # the dead hear nothing, and a flat radio is silent
                self.emit(o2["seat"], "heard", text, frm=s["name"], how="radio")
                self._hear(o2, s["name"], "radio", text)
                got.append(o2["name"])
            self._noise(s["x"], s["y"], "radio")
            s["bubble"], s["bubble_until"] = "📻 " + text[:56], self.t + 20
            self.stats["radio"] += 1
            self._log_msg(s, "radio", got, text, private=bool(names))
            self.fev("radio", f"{s['name']}: {text[:80]}", s["x"], s["y"])
            return {"received_by": got, "cost": cost, "battery": round(s["radio"])}
        if op == "post_note":
            if not self._at_sh(s):
                raise GameError("the noticeboard is in the stronghold")
            note = {"t": round(self.t), "day": self.day, "clock": self.clock(), "from": s["name"], "text": self._text(o)}
            at = o.get("at")
            if at is not None:
                if not (isinstance(at, (list, tuple)) and len(at) == 2):
                    raise GameError("at must be [x, y]")
                x, y = int(at[0]), int(at[1])
                if not (0 <= x < self.W and 0 <= y < self.H):
                    raise GameError("at is off the map")
                note["at"] = [x, y]
            self.notes.append(note)
            self.notes = self.notes[-200:]
            self.stats["notes"] += 1
            self._log_msg(s, "noticeboard", [], note["text"], **({"at": note["at"]} if "at" in note else {}))
            self.fev("note", f"{s['name']} posted a note", s["x"], s["y"])
            return {"notes": len(self.notes)}
        if op == "read_notes":
            if not self._at_sh(s):
                raise GameError("the noticeboard is in the stronghold")
            return {"notes": self.notes[-int(o.get("last", 30)):], "pins": [n for n in self.notes if "at" in n]}
        if op == "eat":
            item = o.get("item")
            src = None
            if item and s["inv"].get(item):
                src = s["inv"]
            elif item and self._at_sh(s) and self.stock.get(item, 0) > 0:
                src = self.stock
            if not item or not self.cfg["items"].get(item, {}).get("food"):
                raise GameError("eat needs a food item: " + ", ".join(k for k, v in c["items"].items() if v.get("food")))
            if src is None:
                raise GameError(f"you have no {item} (and the stockpile is only reachable at the stronghold)")
            src[item] -= 1
            if not src[item]:
                del src[item]
            s["sat"] = min(100.0, s["sat"] + c["items"][item]["food"])
            self.stats["eaten"] += 1
            return {"satiety": round(s["sat"])}
        if op == "recharge":
            if s["inv"].get("battery"):
                s["inv"]["battery"] -= 1
                if not s["inv"]["battery"]:
                    del s["inv"]["battery"]
                used = "a battery you carried"
            elif self._at_sh(s) and self.stock.get("battery", 0) > self._reserved("battery"):
                self.stock["battery"] -= 1
                used = "a battery from the stockpile"
            elif self._at_sh(s) and self.stock.get("battery", 0) > 0:
                raise GameError("the stockpile's last batteries are kept for the radio mast until it is started")
            else:
                raise GameError("recharging takes a battery: one you carry or, at the stronghold, one from the stockpile")
            s["radio"] = min(c["radio_max_charge"], s["radio"] + c["battery_charge"])
            return {"battery": round(s["radio"]), "used": used}
        if op == "start_build":
            tid = o.get("tier")
            if tid not in self.tiers:
                raise GameError(f"tier must be one of {', '.join(self.tiers)}")
            if not self._at_sh(s):
                raise GameError("you can only start a build at the stronghold")
            t = self.tiers[tid]
            st = self.structures.get(tid)
            if st and st["state"] == "damaged":
                need = self._need(t["cost"], c["repair_share"])
                if not self._has(need):
                    raise GameError(f"repairing the {t['name']} needs {need} in the stockpile")
                self._take(need)
                st["state"], st["labor"] = "repair", 0.0
                return {"repairing": tid, "labor_minutes": round(t["labor"] * c["repair_share"])}
            if st:
                raise GameError(f"the {t['name']} is already {st['state'] if st['state'] != 'site' else 'under construction'}")
            missing = [n for n in t["needs"] if not self.done(n)]
            if missing:
                raise GameError(f"the {t['name']} first needs a working {', '.join(missing)}")
            if sum(1 for v in self.structures.values() if v["state"] in ("site", "repair")) >= c["max_sites"]:
                raise GameError(f"at most {c['max_sites']} builds at once")
            if not self._has(t["cost"]):
                raise GameError(f"the {t['name']} needs {t['cost']} in the stockpile (it has {self.stock})")
            self._take(t["cost"])
            self.structures[tid] = {"state": "site", "labor": 0.0}
            slow = self._no_trade(tid)
            return {"started": tid, "labor_minutes": t["labor"], "two_person": bool(t.get("heavy")),
                    **({"slow": f"nobody here is a {slow}: this takes {c['trade_penalty']:g} times the labor until one is among your helpers"} if slow else {})}
        raise GameError(f"unknown op '{op}'")

    def _status(self, s):
        c = self.cfg
        m = self.minute_of_day
        nxt = c["horde_every_days"] * math.ceil(self.day / c["horde_every_days"])
        if self.day % c["horde_every_days"] == 0 and m >= c["horde_hour"] * 60:
            nxt += c["horde_every_days"]
        nxt = max(nxt, c["horde_first_day"])
        out = {"ok": True, "you": {"name": s["name"], "x": s["x"], "y": s["y"], "hp": round(s["hp"]), "satiety": round(s["sat"]),
                                   "dead_until": self._when(s["dead_until"]) if s["dead_until"] else None,
                                   "carrying_kg": round(self.kg(s["inv"]), 1), "capacity_kg": c["carry_kg"], "inventory": dict(s["inv"]),
                                   "radio_battery": round(s["radio"]), "doing": self._desc(s["cur"]),
                                   "queue": [self._desc(q) for q in s["queue"]], "weapon": self._weapon(s), "notes_found": s["notes_found"]},
               "time": {"day": self.day, "clock": self.clock(), "night": self.night, "days_total": c["days"],
                        "next_horde_night": nxt, "frozen": self.frozen},
               "places_known": [{"id": l, "name": self.lm[l]["name"], "door": self.lm[l]["door"]} for l in sorted(s["known"])],
               "stronghold": {"x": self.sx, "y": self.sy, "w": self.sw, "h": self.sh, "gate": self.w["gate"]}}
        if s["dead_until"]:
            out["you"]["dead"] = self._dead_note(s)
            out["you"]["back_in_real_seconds"] = self._back_sec(s)
            out["stronghold"]["note"] = "the dead see nothing"
            out["team"] = {"survivors": [o["name"] for o in self.surv.values()], "helpers": "the dead see nothing"}
        elif cheb(s["x"], s["y"], self.sx + self.sw // 2, self.sy + self.sh // 2) <= self.sight(s) + 3:
            out["stronghold"].update({"stock": dict(self.stock), "wall": {SIDE_NAMES[d]: round(self.wall_hp[d]) for d in SIDES},
                                      "wall_max": round(self.wall_max()),
                                      "structures": {k: (v["state"] if v["state"] in ("done", "damaged") else
                                                         f"{v['state']} {v['labor']:.0f}/{self.tiers[k]['labor'] * (c['repair_share'] if v['state'] == 'repair' else 1):.0f} labor")
                                                     for k, v in self.structures.items()},
                                      "noticeboard_notes": len(self.notes)})
            out["team"] = {"survivors": [o["name"] for o in self.surv.values()], "dead": [o["name"] for o in self.surv.values() if o["dead_until"]],
                           "helpers": [{"id": h["seat"], "name": h["name"], "job": h["job"], "doing": h["act"], "x": h["x"], "y": h["y"]}
                                       for h in self.helpers.values()]}
        else:
            out["stronghold"]["note"] = "too far away to see the stockpile and the builds"
            out["team"] = {"survivors": [o["name"] for o in self.surv.values()], "helpers": "too far away to see the helpers"}
        if self.rescued:
            out["rescued"] = True
        return out

    def _desc(self, o):
        if not o:
            return None
        return {k: v for k, v in o.items() if not k.startswith("_") and k not in ("prog", "step")}

    def _look(self, s):
        if s["dead_until"]:
            return {"ok": True, "dead": self._dead_note(s), "back_in_real_seconds": self._back_sec(s)}
        r = self.sight(s)
        x0, y0 = s["x"], s["y"]
        zs = {(z["x"], z["y"]): z for z in self.zombies.values() if cheb(z["x"], z["y"], x0, y0) <= r}
        others = {(o["x"], o["y"]): o for o in self.surv.values() if o is not s and not o["dead_until"] and cheb(o["x"], o["y"], x0, y0) <= r}
        strangers = {(st["x"], st["y"]): st for st in self.strangers.values()
                     if st["state"] in ("waiting", "quarantined") and cheb(st["x"], st["y"], x0, y0) <= r}
        helpers = {(h["x"], h["y"]): h for h in self.helpers.values() if cheb(h["x"], h["y"], x0, y0) <= r}
        rows = []
        for y in range(y0 - r, y0 + r + 1):
            row = []
            for x in range(x0 - r, x0 + r + 1):
                if (x, y) == (x0, y0):
                    ch = "@"
                elif (x, y) in zs:
                    ch = "H" if zs[(x, y)]["kind"] == "h" else "Z"
                elif (x, y) in others:
                    ch = "&"
                elif (x, y) in strangers:
                    ch = "?"
                elif (x, y) in helpers:
                    ch = "h"
                elif (x, y) in self.piles:
                    ch = "*"
                else:
                    ch = self.tile(x, y) or " "
                row.append(ch)
            rows.append("".join(row))
        lms = []
        for l in self.w["landmarks"]:
            nx = min(max(x0, l["x"]), l["x"] + l["w"] - 1)
            ny = min(max(y0, l["y"]), l["y"] + l["h"] - 1)
            if cheb(x0, y0, nx, ny) <= r:
                place = {"id": l["id"], "name": l["name"], "door": l["door"], "searchable": l["searchable"],
                         "searched_out": l["searchable"] and not l["loot"]}
                if cheb(x0, y0, nx, ny) <= self.cfg["detail_reach"]:
                    place["details"] = list(l["details"])
                lms.append(place)
        return {"ok": True, "you": [x0, y0], "radius": r, "top_left": [x0 - r, y0 - r], "night": self.night,
                "grid": rows,
                "legend": "@ you, & teammate, ? stranger, h helper, Z zombie, H horde zombie, * items on the ground, "
                          ". grass, T tree, t stump, R rock, r quarried, ~ water, = road, # wall, + door, : floor, "
                          ", field, b berry bush, x wreckage (scrap), S stronghold",
                "zombies": sorted([{"id": zid, "x": z["x"], "y": z["y"], "kind": "horde" if z["kind"] == "h" else "walker",
                                    "dist": cheb(z["x"], z["y"], x0, y0), **({"was": z["who"]} if z.get("who") else {})}
                                   for zid, z in self.zombies.items()
                                   if cheb(z["x"], z["y"], x0, y0) <= r], key=lambda d: d["dist"]),
                "survivors": [{"name": o["name"], "x": o["x"], "y": o["y"], "doing": o["act"]} for o in others.values()],
                "strangers": [{"id": st["id"], "name": st["name"], "x": st["x"], "y": st["y"], "description": st["desc"],
                               "state": st["state"], "dist": cheb(st["x"], st["y"], x0, y0)} for st in strangers.values()],
                "helpers": [{"id": h["seat"], "name": h["name"], "x": h["x"], "y": h["y"], "job": h["job"], "doing": h["act"]}
                            for h in helpers.values()],
                "ground_items": [{"x": x, "y": y, "items": p} for (x, y), p in self.piles.items() if cheb(x, y, x0, y0) <= r],
                "places": lms,
                "ripe_bushes": [[x, y] for (x, y), ripe in self.w["bushes"].items() if cheb(x, y, x0, y0) <= r and ripe <= self.t]}

    # ------------------------------------------------------------------ grading view
    def grade_state(self):
        return {"grade": round(self.best, 6), "rescued": self.rescued, "day": self.day, "clock": self.clock(),
                "wall": round(self.wall, 1), "t": round(self.t), "frozen": self.frozen,
                "structures": {k: v["state"] for k, v in self.structures.items()}, "tier_times": self.tier_times,
                "score_times": self.score_times, "stats": dict(self.stats), "seats": len(self.surv),
                "calls": {s["name"]: s["calls"] for s in self.surv.values()},
                "strangers": self.stranger_totals(), "threats": dict(self.threat_totals), "messages": dict(self.msg_counts)}

    def dead_seats(self):
        return [s["seat"] for s in self.surv.values() if s["dead_until"]]

    def stranger_records(self):
        keep = ("id", "name", "type", "trade", "via", "arrive", "claimed", "real", "honest_error", "state", "met", "talkers",
                "topics", "decision", "decided_by", "decided_t", "outcome", "checkable")
        return [{k: st.get(k) for k in keep} for st in self.strangers.values()]

    def stranger_totals(self):
        recs = list(self.strangers.values())
        arrived = [st for st in recs if st["state"] not in ("pending", "stayed_away")]
        admitted = [st for st in recs if st["decision"] == "admit"]
        right = sum(1 for st in recs if st["decision"] is not None and
                    (st["decision"] == "admit") == (st["type"] in HONEST))
        return {"arrived": len(arrived), "stayed_away": sum(1 for st in recs if st["state"] == "stayed_away"),
                "met": sum(1 for st in arrived if st["met"]), "talked_to": sum(1 for st in arrived if st["talkers"]),
                "decided": sum(1 for st in arrived if st["decision"]), "decided_right": right,
                "admitted": len(admitted), "admitted_bad": sum(1 for st in admitted if st["type"] not in HONEST),
                "admitted_honest": sum(1 for st in admitted if st["type"] in HONEST), "helpers_now": len(self.helpers),
                "turned_away": sum(1 for st in recs if st["decision"] == "turn_away"),
                "quarantined": sum(1 for st in recs if st["turn_at"] is not None),
                "killed": sum(1 for st in recs if st["state"] == "dead"),
                "missed": sum(1 for st in recs if st["outcome"] in ("left_unmet", "never_found", "left_undecided")),
                "outcomes": {o: sum(1 for st in recs if st["outcome"] == o) for o in {st["outcome"] for st in recs if st["outcome"]}}}
