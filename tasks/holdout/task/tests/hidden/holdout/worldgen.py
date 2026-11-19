"""The Holdout map, built from a seed. The same seed gives the same world for every team size.

Layout: the stronghold sits in the middle of a valley. A river runs down the west side, forests ring the
stronghold, a quarry lies to the south-east, a small town of searchable buildings to the north-east, scrap
fields (wrecks, a burnt farm) to the north-west and south-west, and berry fields to the south. A road joins the
stronghold gate to the town. Six caches are buried at spots described only in notes found in the town.
"""
import random

GRASS, TREE, STUMP, ROCK, QROCK, WATER, ROAD, WALL, DOOR, FLOOR, FIELD, BUSH, RUIN, SH = \
    ".", "T", "t", "R", "r", "~", "=", "#", "+", ":", ",", "b", "x", "S"
WALKABLE = set(".tTRr=+:,bxS")
ROUGH = set("TRb,x")

# kind: (display name pattern, loot list, size)
BUILDINGS = [
    ("pharmacy", "Corner pharmacy", [("medkit", 1), ("medkit", 1), ("medkit", 1), ("can", 2), ("battery", 1), ("NOTE", 0)]),
    ("hardware", "Hardware store", [("scrap", 2), ("scrap", 2), ("parts", 1), ("parts", 1), ("axe", 1), ("pickaxe", 1), ("ammo", 24), ("NOTE", 0)]),
    ("gas_station", "Gas station", [("fuel", 1), ("fuel", 1), ("fuel", 1), ("can", 2), ("can", 2), ("battery", 1), ("pistol", 1), ("ammo", 24)]),
    ("electronics", "Electronics repair shop", [("parts", 2), ("parts", 1), ("battery", 2), ("battery", 2), ("scrap", 1), ("NOTE", 0)]),
    ("garden_center", "Garden center", [("seeds", 1), ("seeds", 1), ("seeds", 1), ("can", 1)]),
    ("grocery", "Grocery", [("can", 3), ("can", 3), ("can", 3), ("can", 3), ("berries", 4)]),
    ("church", "St. Anne's church", [("can", 2), ("can", 1), ("medkit", 1), ("NOTE", 0)]),
    ("police", "Police station", [("spear", 1), ("rifle", 1), ("shotgun", 1), ("pistol", 1), ("ammo", 48), ("ammo", 48), ("battery", 2), ("medkit", 1), ("NOTE", 0)]),
    ("school", "Elementary school", [("can", 2), ("battery", 1), ("medkit", 1), ("scrap", 1)]),
    ("diner", "Route 9 diner", [("can", 3), ("can", 3), ("fuel", 1), ("shotgun", 1)]),
    ("house", "Blue house", [("can", 2), ("battery", 1), ("scrap", 1), ("pistol", 1)]),
    ("house", "Yellow house", [("can", 2), ("can", 1), ("ammo", 24), ("NOTE", 0)]),
    ("house", "Brick house", [("can", 2), ("scrap", 1), ("battery", 1)]),
    ("house", "Green-roof house", [("can", 1), ("can", 2), ("parts", 1)]),
]

CACHES = [
    {"items": {"fuel": 2, "parts": 2}, "what": "two jerry cans and a box of radio parts"},
    {"items": {"battery": 3, "parts": 1}, "what": "a pack of batteries and a circuit board"},
    {"items": {"medkit": 2, "seeds": 2}, "what": "two first-aid kits and seed packets"},
    {"items": {"scrap": 3, "ammo": 36}, "what": "sheet metal and a box of rifle rounds"},
    {"items": {"fuel": 2, "battery": 2}, "what": "fuel and batteries"},
    {"items": {"parts": 3}, "what": "spare electronics"},
]

DIRS = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}
WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve"]


# Place details: short visual facts, fixed for a run. Each template has a slot (a colour or a count), so an honest
# stranger can misremember one small thing and a liar can invent something that sounds right.
COLOURS = ["red", "blue", "green", "yellow", "white", "black", "orange", "grey", "rust-brown"]
COUNTS = ["two", "three", "four", "five", "six", "seven"]
DETAILS = {
    "farm": ["a {c} tractor sunk to its axles by the barn", "{n} grain sacks slumped against the porch",
             "a {c} tarp nailed across the barn roof", "{n} dead crows wired to the fence",
             "the farmhouse door painted {c}", "a {c} pickup with its hood up in the yard",
             "{n} milk churns lined up along the drive", "a windmill missing {n} of its blades",
             "a {c} child's bicycle on the henhouse roof", "{n} fence posts snapped off along the lane",
             "a {c} water tank on stilts behind the house", "{n} stiff shirts still on the washing line",
             "a {c} scarecrow in a hard hat", "{n} beehives knocked over by the gate"],
    "bridge": ["{n} sandbags piled at one end", "a {c} car left sideways across one lane",
               "a {c} ribbon tied to the middle lamp post", "{n} lamp posts bent out over the water",
               "the word STOP sprayed in {c} across the deck", "a {c} canoe caught under the pier",
               "{n} road cones in a line down the centre", "the north rail gone for {n} spans",
               "a {c} shopping trolley wedged in the railing", "{n} padlocks on the rail with names scratched in them"],
    "wreck": ["a {c} sedan crushed under the front axle", "{n} suitcases burst open on the verge",
              "a {c} child's seat hanging out of a window", "{n} tyres burned down to the wire",
              "a {c} jacket snagged on the wing mirror", "a {c} X sprayed on the driver's door",
              "{n} windows smashed along one side", "a {c} bedroll tied to the roof rack",
              "{n} jerry cans lying empty in the ditch", "a {c} scarf knotted to the aerial"],
    "ruins": ["{n} chimneys standing with nothing around them", "a {c} door lying flat on the ash",
              "{n} oil drums burned through at the seams", "a {c} refrigerator on its back in the weeds",
              "{n} car bodies stacked like plates", "a {c} tarpaulin snagged on a girder",
              "a crane arm with {n} chains hanging from it", "a {c} sign face down in the mud"],
    "quarry": ["a {c} excavator rusting at the rim", "{n} wooden cable drums by the ramp",
               "a {c} site hut with its windows out", "{n} survey stakes with {c} tape",
               "a {c} conveyor stopped mid-load", "{n} blast warnings wired to the fence"],
    "ranger": ["a {c} canoe upturned by the steps", "{n} antlers nailed over the door",
               "a {c} flag tied upside down on the pole", "{n} trail maps pinned inside the window",
               "a {c} first-aid box torn off the wall", "{n} rain barrels along the back wall"],
    "water_tower": ["{n} rungs missing from the ladder", "the town's name painted out in {c}",
                    "a {c} rope hanging from the catwalk", "{n} rust streaks down the tank"],
    "pond": ["a {c} rowing boat sunk at the jetty", "{n} fishing rods stuck in the mud",
             "a {c} mill wheel stopped for good", "{n} geese long dead in the reeds",
             "a {c} oil slick along the bank", "{n} planks missing from the jetty"],
    "outcrop": ["a {c} survey mark painted on the biggest boulder", "{n} iron rings set into the rock",
                "a {c} tarp weighted down with stones", "{n} cairns stacked on the flat rock",
                "initials scratched in {c} on a slab", "{n} rusted wedges left in a crack"],
    "hedgerow": ["a {c} glove stuck on a thorn", "{n} wicker baskets left to rot",
                 "a {c} stepladder folded in the brambles", "{n} jam jars on a fence rail",
                 "a {c} scarecrow bag on a stake", "{n} wasps' nests in the hedge"],
    "town": ["windows boarded with {c} planks", "{n} shopping carts jammed in the doorway",
             "a {c} awning torn down over the door", "a {c} handprint on the front glass",
             "{n} bullet holes in the sign", "a {c} bicycle chained to the drainpipe",
             "{n} mattresses stacked in the back room", "{n} coins scattered under the open till",
             "a {c} fire door wedged open with a brick", "a hand-lettered sign in {c} paint: NO FOOD HERE",
             "{n} empty water jugs by the counter", "a {c} umbrella propped inside the door",
             "{n} chairs nailed across the back window", "a {c} tarp over a hole in the roof",
             "a calendar stopped on the {c} page", "{n} rat traps along the skirting board"],
}


def detail_pool(kind):
    return DETAILS.get(kind) or DETAILS["town"]


def fill_detail(template, rnd):
    """A detail's text and the same detail with one small thing wrong (its colour or its count)."""
    a, b = rnd.sample(COLOURS, 2)
    m, n = rnd.sample(COUNTS, 2)
    return template.format(c=a, n=m), template.format(c=b, n=n)


def _add_places(g, cfg, seed, landmarks, river_cols, tx, inside):
    """Places beyond the town: farms, wrecks along the roads, bridges, the near pond, outcrop and hedgerow. They use
    their own random stream, so the rest of the map stays what it was."""
    r = random.Random(f"{seed}:places")
    def building(x0, y0, w, h):
        for yy in range(y0, y0 + h):
            for xx in range(x0, x0 + w):
                g[yy][xx] = WALL if yy in (y0, y0 + h - 1) or xx in (x0, x0 + w - 1) else FLOOR
        door = (x0 + 1 + r.randint(0, 1), y0 + h - 1)
        g[door[1]][door[0]] = DOOR
        return door

    def clear(x0, y0, w, h):
        return all(inside(x, y) and g[y][x] == GRASS for y in range(y0 - 1, y0 + h + 1) for x in range(x0 - 1, x0 + w + 1))

    for pid, name, ax, ay in (("north_farm", "North farm", 44, 6), ("east_farm", "East farm", 84, 44), ("south_farm", "South farm", 44, 87),
                              ("west_farm", "West farm", 23, 41), ("river_farm", "River farm", 3, 64), ("orchard", "Old orchard", 78, 87)):
        for _ in range(300):
            x0, y0 = ax + r.randint(-3, 3), ay + r.randint(-3, 3)
            if clear(x0, y0, 4, 4):
                break
        door = building(x0, y0, 4, 4)
        landmarks.append({"id": pid, "name": name, "kind": "farm", "x": x0, "y": y0, "w": 4, "h": 4, "door": list(door), "loot": [], "searchable": False})
    for pid, name, ax, ay in (("stalled_convoy", "Stalled convoy", 28, 47), ("jackknifed_truck", "Jackknifed truck", 20, 49),
                              ("overturned_bus", "Overturned bus", 67, 42), ("burned_ambulance", "Burned-out ambulance", 57, 55),
                              ("mail_van", "Crashed mail van", 67, 38)):
        landmarks.append({"id": pid, "name": name, "kind": "wreck", "x": ax, "y": ay, "w": 2, "h": 1, "door": [ax, ay], "loot": [], "searchable": False})
    for pid, name, by in (("north_bridge", "North bridge", 18), ("middle_bridge", "Middle bridge", tx["cy"]), ("south_bridge", "South bridge", 78)):
        rx = river_cols[by]
        landmarks.append({"id": pid, "name": name, "kind": "bridge", "x": rx, "y": by, "w": 3, "h": 1, "door": [rx + 1, by], "loot": [], "searchable": False})
    landmarks.append({"id": "old_depot", "name": "Old depot", "kind": "ruins", "x": 76, "y": 76, "w": 4, "h": 4, "door": [78, 78], "loot": [], "searchable": False})
    # Near the stronghold, so a helper's forage, fish and quarry jobs have somewhere to work within helper_range tiles.
    for yy in range(34, 37):
        for xx in range(56, 60):
            g[yy][xx] = WATER
    landmarks.append({"id": "mill_pond", "name": "Mill pond", "kind": "pond", "x": 56, "y": 34, "w": 4, "h": 3, "door": [55, 35], "loot": [], "searchable": False})
    for xx, yy in ((58, 58), (59, 58), (60, 58), (58, 59), (59, 59), (61, 59), (59, 60), (60, 60), (61, 60)):
        g[yy][xx] = ROCK
    landmarks.append({"id": "stone_outcrop", "name": "Stone outcrop", "kind": "outcrop", "x": 58, "y": 58, "w": 4, "h": 3, "door": [57, 59], "loot": [], "searchable": False})
    for yy in range(43, 47):
        for xx in range(33, 38):
            g[yy][xx] = FIELD
    for xx, yy in ((33, 44), (35, 43), (36, 45), (34, 46)):
        g[yy][xx] = BUSH
    landmarks.append({"id": "hedgerow", "name": "Bramble hedgerow", "kind": "hedgerow", "x": 33, "y": 43, "w": 5, "h": 4, "door": [38, 45], "loot": [], "searchable": False})


def _add_details(cfg, seed, landmarks):
    """2 to 4 details a place: town buildings the fewest, so most of what can be checked lies out in the valley."""
    r = random.Random(f"{seed}:details")
    used = set()
    pk = {"pharmacy": "town", "hardware": "town", "gas_station": "town", "electronics": "town", "garden_center": "town", "grocery": "town",
          "church": "town", "police": "town", "school": "town", "diner": "town", "house": "town"}
    tx0, ty0, tx1, ty1 = cfg["town_box"]
    for l in landmarks:
        kind = pk.get(l["kind"], l["kind"])
        l["town"] = tx0 <= l["x"] <= tx1 and ty0 <= l["y"] <= ty1
        n = 2 if l["town"] or l["kind"] == "water_tower" else r.choice((3, 4))
        pool = list(detail_pool(kind))
        r.shuffle(pool)
        l["details"], l["detail_alts"] = [], []
        for tmpl in pool:
            if len(l["details"]) >= n:
                break
            for _ in range(40):
                text, alt = fill_detail(tmpl, r)
                if text not in used:
                    used.add(text)
                    l["details"].append(text)
                    l["detail_alts"].append(alt)
                    break
        l["kind"] = l["kind"] if kind == "town" else kind


def generate(cfg, seed):
    rnd = random.Random(seed)
    W, H = cfg["map_w"], cfg["map_h"]
    g = [[GRASS] * W for _ in range(H)]
    sx, sy, sw, sh = cfg["stronghold"]
    cx, cy = sx + sw // 2, sy + sh // 2

    def inside(x, y):
        return 0 <= x < W and 0 <= y < H

    def near_sh(x, y, pad):
        return sx - pad <= x < sx + sw + pad and sy - pad <= y < sy + sh + pad

    def blob(x0, y0, r, ch, density=1.0, avoid=None):
        for y in range(y0 - r, y0 + r + 1):
            for x in range(x0 - r, x0 + r + 1):
                if not inside(x, y) or near_sh(x, y, 3):
                    continue
                d = ((x - x0) ** 2 + (y - y0) ** 2) ** 0.5
                if d <= r * (0.75 + 0.35 * rnd.random()) and rnd.random() < density and g[y][x] == GRASS:
                    if avoid is None or not avoid(x, y):
                        g[y][x] = ch

    # river down the west side
    rx = 16 + rnd.randint(-2, 2)
    for y in range(H):
        rx += rnd.choice([-1, 0, 0, 1]) if y % 3 == 0 else 0
        rx = max(10, min(24, rx))
        for x in range(rx, rx + 3):
            g[y][x] = WATER
    river_cols = [min(x for x in range(W) if g[y][x] == WATER) for y in range(H)]
    # three bridges (road over the water), and a road west from the stronghold to the middle one
    bridges = []
    for by in (18, cy, 78):
        for x in range(W):
            if g[by][x] == WATER:
                g[by][x] = ROAD
        bridges.append(by)
    for x in range(river_cols[cy] + 3, sx):
        g[cy][x] = ROAD
    # the far bank: a ranger station with a forest and berry bushes, a reason to cross
    rsx, rsy = max(2, river_cols[cy] - 9), cy - 7
    for yy in range(rsy, rsy + 5):
        for xx in range(rsx, rsx + 4):
            g[yy][xx] = WALL if yy in (rsy, rsy + 4) or xx in (rsx, rsx + 3) else FLOOR
    rdoor = (rsx + 2, rsy + 4)
    g[rdoor[1]][rdoor[0]] = DOOR
    for yy in range(rdoor[1] + 1, cy):
        g[yy][rdoor[0]] = ROAD
    for xx in range(rdoor[0], river_cols[cy]):
        if g[cy][xx] == GRASS:
            g[cy][xx] = ROAD

    # town block in the north-east: a grid of roads with buildings between
    tx0, ty0, tx1, ty1 = 60, 8, 90, 36
    for y in range(ty0, ty1 + 1):
        for x in (tx0, tx0 + 10, tx0 + 20, tx1):
            g[y][x] = ROAD
    for x in range(tx0, tx1 + 1):
        for y in (ty0, ty0 + 9, ty0 + 18, ty1):
            g[y][x] = ROAD
    # main road from the stronghold gate (south side) around to the town
    gate = (cx, sy + sh)
    x, y = cx, sy + sh + 1
    for yy in range(sy + sh, sy + sh + 3):
        g[yy][cx] = ROAD
    y = sy + sh + 2
    for xx in range(cx, tx0 + 6):
        g[y][xx] = ROAD
    for yy in range(ty1, y + 1):
        g[yy][tx0 + 6] = ROAD if g[yy][tx0 + 6] != WATER else ROAD

    landmarks = []
    lots = []
    for bx0 in (tx0 + 1, tx0 + 11, tx0 + 21):
        for by0 in (ty0 + 2, ty0 + 11, ty0 + 20):
            for dx in (0, 5):
                lots.append((bx0 + dx, by0))
    rnd.shuffle(lots)
    for (kind, name, loot), (bx, by) in zip(BUILDINGS, lots):
        w, h = 4, 5
        for yy in range(by, by + h):
            for xx in range(bx, bx + w):
                edge = yy in (by, by + h - 1) or xx in (bx, bx + w - 1)
                g[yy][xx] = WALL if edge else FLOOR
        door = (bx + 1 + rnd.randint(0, 1), by + h - 1)
        g[door[1]][door[0]] = DOOR
        lid = name.lower().replace("'", "").replace(".", "").replace(" ", "_").replace("-", "_")
        items = [list(e) for e in loot]
        rnd.shuffle(items)
        landmarks.append({"id": lid, "name": name, "kind": kind, "x": bx, "y": by, "w": w, "h": h,
                          "door": list(door), "loot": items, "searchable": True})
    ranger_loot = [["rifle", 1], ["shotgun", 1], ["ammo", 36], ["battery", 2], ["parts", 1], ["axe", 1], ["fuel", 1], ["can", 2], ["NOTE", 0]]
    rnd.shuffle(ranger_loot)
    landmarks.append({"id": "ranger_station", "name": "Ranger station", "kind": "ranger", "x": rsx, "y": rsy, "w": 4, "h": 5,
                      "door": list(rdoor), "loot": ranger_loot, "searchable": True})
    # a water tower: a landmark you can see from afar, not searchable
    wx, wy = tx0 - 4, ty0 + 14
    for yy in range(wy, wy + 2):
        for xx in range(wx, wx + 2):
            g[yy][xx] = WALL
    landmarks.append({"id": "water_tower", "name": "Water tower", "kind": "water_tower", "x": wx, "y": wy, "w": 2, "h": 2,
                      "door": [wx, wy + 2], "loot": [], "searchable": False})

    # quarry to the south-east
    qx, qy = cx + 22 + rnd.randint(-2, 2), cy + 20 + rnd.randint(-2, 2)
    blob(qx, qy, 5, ROCK, 0.85)
    landmarks.append({"id": "quarry", "name": "Old quarry", "kind": "quarry", "x": qx - 2, "y": qy - 2, "w": 4, "h": 4,
                      "door": [qx, qy], "loot": [], "searchable": False})
    # forests ringing the stronghold, mostly west and south
    for (fx, fy, r) in [(max(4, rx - 8), cy + 12, 5), (max(4, rx - 7), 30, 4), (cx - 14, cy - 12, 6), (cx - 13, cy + 8, 7), (cx + 4, cy + 16, 5), (cx - 4, cy - 20, 6),
                        (cx + 14, cy - 4, 4), (rx + 8, 72, 7), (rx + 7, 20, 6), (cx + 12, cy + 34, 6)]:
        blob(fx + rnd.randint(-2, 2), fy + rnd.randint(-2, 2), r, TREE, 0.8)
    # berry fields to the south
    for (fx, fy) in [(cx - 2, cy + 26), (cx - 18, cy + 30), (cx + 26, cy + 2), (max(5, rx - 6), cy - 18)]:
        blob(fx, fy, 5, FIELD, 0.9)
        for _ in range(9):
            bx_, by_ = fx + rnd.randint(-4, 4), fy + rnd.randint(-4, 4)
            if inside(bx_, by_) and g[by_][bx_] in (FIELD, GRASS):
                g[by_][bx_] = BUSH
    # scrap: wrecks by the road and a burnt farm
    for (fx, fy, r) in [(cx - 20, cy - 26, 3), (cx - 24, cy + 22, 3), (cx + 30, cy + 30, 2)]:
        blob(fx, fy, r, RUIN, 0.7)
    landmarks.append({"id": "burnt_farm", "name": "Burnt farm", "kind": "ruins", "x": cx - 26, "y": cy + 20, "w": 4, "h": 4,
                      "door": [cx - 24, cy + 22], "loot": [], "searchable": False})
    landmarks.append({"id": "wreck_yard", "name": "Wreck yard", "kind": "ruins", "x": cx - 22, "y": cy - 28, "w": 4, "h": 4,
                      "door": [cx - 20, cy - 26], "loot": [], "searchable": False})
    _add_places(g, cfg, seed, landmarks, river_cols, {"cy": cy}, inside)
    # stronghold ground
    for yy in range(sy, sy + sh):
        for xx in range(sx, sx + sw):
            g[yy][xx] = SH

    # resources on tiles
    trees = {(x, y): cfg["tree_logs"] for y in range(H) for x in range(W) if g[y][x] == TREE}
    rocks = {(x, y): cfg["rock_stone"] for y in range(H) for x in range(W) if g[y][x] == ROCK}
    bushes = {(x, y): 0 for y in range(H) for x in range(W) if g[y][x] == BUSH}   # value: game minute when ripe
    ruins = {(x, y): 2 for y in range(H) for x in range(W) if g[y][x] == RUIN}

    # caches: buried a few blocks from a town landmark; notes in the town say where
    caches = []
    # (only the places the map has always had: new v3 places must not move the caches)
    anchors = [l for l in landmarks if l["kind"] not in ("quarry", "farm", "wreck", "bridge", "pond", "outcrop", "hedgerow") and l["id"] != "old_depot"]
    rnd.shuffle(anchors)
    for i, spec in enumerate(CACHES):
        for _ in range(200):
            a = anchors[(i + _) % len(anchors)]
            d1, d2 = rnd.sample(list(DIRS), 2)
            if {d1, d2} in ({"north", "south"}, {"east", "west"}):
                continue
            n1, n2 = rnd.randint(3, 9), rnd.randint(2, 7)
            x = a["door"][0] + DIRS[d1][0] * n1 + DIRS[d2][0] * n2
            y = a["door"][1] + DIRS[d1][1] * n1 + DIRS[d2][1] * n2
            if inside(x, y) and g[y][x] in WALKABLE and g[y][x] not in (DOOR, FLOOR, SH) and \
                    all((c["x"], c["y"]) != (x, y) for c in caches):
                feature = {TREE: "the trees", ROCK: "a big rock", FIELD: "the edge of a field", BUSH: "a berry bush",
                           ROAD: "the side of the road", RUIN: "some wreckage"}.get(g[y][x], "an open patch of grass")
                text = (f"Hid {spec['what']} where nobody will look. From the door of the {a['name']}, walk "
                        f"{WORDS[n1]} blocks {d1}, then {WORDS[n2]} blocks {d2}. It's buried by {feature}.")
                caches.append({"id": f"cache{i + 1}", "x": x, "y": y, "items": dict(spec["items"]), "text": text,
                               "anchor": a["id"], "found": False})
                break
    _add_details(cfg, seed, landmarks)
    # put the notes into the loot of NOTE slots, in order
    note_slots = [(l, e) for l in landmarks for e in l["loot"] if e[0] == "NOTE"]
    rnd.shuffle(note_slots)
    for (l, e), c in zip(note_slots, caches):
        e[1] = c["id"]
    for l, e in note_slots[len(caches):]:
        e[0], e[1] = "can", 1
    return {"grid": g, "landmarks": landmarks, "trees": trees, "rocks": rocks, "bushes": bushes, "ruins": ruins,
            "caches": caches, "gate": list(gate), "center": [cx, cy], "river_cols": river_cols}
