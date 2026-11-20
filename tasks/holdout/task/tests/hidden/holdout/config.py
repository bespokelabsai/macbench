"""Every tunable number of Holdout in one place. profile.json (next to this package) may override any
top-level key.

Units: time is GAME MINUTES unless a name ends in _sec (real seconds). Distances are tiles. A tile is about one
city block; the numbers are chosen for play at the benchmark's pace (one agent decision is 10-60 real seconds),
not for literal physics.
"""
import copy
import json
import os

DEFAULTS = {
    # ---- clock ----
    "real_sec_per_day": 900.0,        # 15 real minutes per game day: 16 days in a 4 h run
    "start_minute": 6 * 60,           # day 1 starts at 06:00
    "night_start": 20 * 60, "night_end": 6 * 60,
    "days": 16,                       # the game ends (frozen) after this many days even if the run continues
    "autostart_sec": 600.0,           # the world starts at the first call, or this long after the hub starts
    "frame_every_min": 5,             # replay frame period

    # ---- map ----
    "map_w": 96, "map_h": 96,
    "stronghold": [45, 45, 7, 7],     # x, y, w, h

    # ---- survivors ----
    "walk_min_per_tile": 3.0, "road_min_per_tile": 2.0, "rough_min_per_tile": 4.5,   # rough = tree/rock/field
    "carry_kg": 25.0,
    "sight_day": 7, "sight_night": 4, "sight_tower": 14, "sight_floodlit": 9,
    "satiety_per_day": 55.0,          # satiety 0..100 drops this much per game day
    "starving_hp_per_hour": 3.0, "hp_regen_per_hour": 2.0,
    "respawn_min": 720, "respawn_hour": 9, "respawn_satiety": 60.0,   # back at the stronghold a game day later, by daylight
    "queue_max": 8,
    "start_cans_each": 2, "start_spears_each": 1,

    # ---- items: kg, satiety when eaten (0 = not food), perishable in the stockpile without a storehouse ----
    "items": {
        "wood":    {"kg": 5.0}, "stone": {"kg": 8.0}, "scrap": {"kg": 3.0}, "parts": {"kg": 1.0},
        "fuel":    {"kg": 5.0}, "battery": {"kg": 0.2}, "medkit": {"kg": 1.0}, "seeds": {"kg": 0.2},
        "axe":     {"kg": 2.0}, "pickaxe": {"kg": 3.0}, "spear": {"kg": 2.0}, "rifle": {"kg": 4.0}, "ammo": {"kg": 0.05},
        "shotgun": {"kg": 3.5}, "pistol": {"kg": 1.0},
        "can":     {"kg": 0.5, "food": 25}, "berries": {"kg": 0.2, "food": 6, "perish": True},
        "fish":    {"kg": 1.0, "food": 15, "perish": True}, "meal": {"kg": 0.6, "food": 40, "perish": True},
        "produce": {"kg": 0.5, "food": 20, "perish": True},
    },
    "spoil_per_day": 0.25,            # share of perishables in the stockpile lost each dawn without a storehouse

    # ---- work (game minutes) ----
    "chop_min": 40, "chop_logs": 2, "tree_logs": 6,
    "quarry_min": 60, "rock_stone": 4,
    "search_min": 25, "forage_min": 15, "forage_berries": 4, "bush_regrow_days": 1,
    "fish_min": 30, "fish_chance": 0.6, "cook_min": 10, "craft_min": 30,
    "tool_speedup": 2.0,              # axe for chopping, pickaxe for quarrying
    "examine_min": 30,
    # best first. reach in tiles, hit = chance to kill, every = game minutes between strikes on guard, aim_min = minutes
    # to aim a `shoot` order, spread = zombies one shot can hit. A gun uses one round a shot, and every shot is loud.
    "weapons": {
        "rifle":   {"gun": True, "reach": 8, "hit": 0.8, "every": 2.0, "aim_min": 5},
        "shotgun": {"gun": True, "reach": 3, "hit": 0.9, "every": 3.0, "aim_min": 4, "spread": 2},
        "pistol":  {"gun": True, "reach": 5, "hit": 0.6, "every": 2.0, "aim_min": 3},
        "axe":     {"reach": 1, "hit": 0.5, "every": 2.0},
        "spear":   {"reach": 2, "hit": 0.5, "every": 2.0},
    },
    "tower_reach": 2, "helper_rounds": 12, "fx_per_frame": 80,
    "builders_max": 4,                # per site
    "solo_heavy_rate": 0.25,          # a two-person job with one builder goes at this rate

    # ---- zombies ----
    "zombie_wander_min_per_tile": 8.0, "zombie_chase_min_per_tile": 5.0, "horde_min_per_tile": 3.5,
    "zombie_sight_day": 3, "zombie_sight_night": 5, "zombie_hp": 3,   # hits to put one down
    "walkers_start": 20, "walkers_per_day": 3, "walkers_cap": 70, "spawn_per_hour": 2,
    "safe_radius_start": 12,          # no walker spawns this close to the stronghold
    "fight_every_min": 5,
    "kill_chance": 0.35, "kill_chance_armed": 0.6,
    "bite_chance_day": 0.015, "bite_chance_night": 0.02, "bite_chance_inside": 0.4,
    "noise": {"chop": 6, "quarry": 6, "shout": 12, "radio": 3, "build": 4, "shoot": 15},

    # ---- hordes ----
    "horde_every_days": 1, "horde_hour": 20, "horde_first_day": 3, "horde_base": 60, "horde_step": 25, "horde_max": 210,   # every night, growing to the most a large team behind a stone wall can hold, so more days always help
    "wall_dmg_per_zombie_min": 0.09,
    "scatter_tiles": 30, "scatter_min": 240,   # at dawn zombies near the base walk back out, for this long
    "horde_sight": 2,
    "floodlight_mult": 1.5,
    "breach_food_loss": 0.3,
    "raid_after_min": 20,             # no standing wall: horde zombies inside the stronghold this many minutes = a breach

    # ---- communication ----
    "comms": True,                    # False = a silent run: say, shout, radio and the noticeboard are closed
    "say_tiles": 10, "shout_tiles": 25,
    "radio_cost": 1.0, "radio_chars": 40, "radio_max_charge": 60.0, "battery_charge": 60.0,   # 1 charge per 40 characters; whole map
    "radio_reach_cap": 3,             # the cost is multiplied by how many it goes to, up to this: everyone costs at most 3 times one person
    "text_max": 400,
    "heard_max": 20,                  # messages carried on one API response

    # ---- strangers ----
    "stranger_count": 14, "stranger_gate_share": 0.6,
    "stranger_shares": {"worker": 0.25, "specialist": 0.30, "bitten": 0.20, "thief": 0.25},
    "trade_penalty": 3.0,             # a tier that calls for a trade takes this many times the labor with no such helper
    "stranger_trades": [["mechanic", "medic"], ["mechanic", "hunter"]],   # each pair, in arrival order: a mechanic in each half
    "stranger_town_story_share": 0.15,   # how many stories start in town rather than out in the valley
    "stranger_wait_min": 120, "stranger_talk_wait_min": 360, "stranger_town_wait_min": 720,
    "quarantine_food": ["meal", "can"], "ultimatum_min": 120,   # no berries; a refusal gives the team this long to decide
    "quarantine_refuse": {"bitten": 0.5, "worker": 0.25, "specialist": 0.25, "thief": 0.1},   # share of each type who refuse: a clue, never proof
    "stranger_ask_min": 5, "stranger_reach": 2, "detail_reach": 2,
    "honest_error_rate": 1 / 6,
    "stranger_melee_hits": 2, "word_gets_around": 2,
    "town_box": [60, 8, 90, 36],      # where "in town" is
    "turn_hour": 20, "turned_attack_min": 10, "turned_buildings": 2,
    "thief_stay_days": [1, 2], "thief_kg": 10.0, "thief_kg_storehouse": 5.0,

    # ---- helpers ----
    "helper_range": 25, "helper_home_at": 19 * 60, "helper_starve_min": 1440,
    "mechanic_mult": 1.5, "hunter_mult": 2,

    # ---- rate limit per seat (real seconds) ----
    "rate_per_sec": 2.0, "rate_burst": 12.0, "look_cost": 2,

    # ---- build ladder: weight (sums to 1), materials, labor (survivor-minutes), prerequisites, heavy = two-person ----
    # Teams of 4 and 8 reached 1.0 under an earlier version, so the middle five take 3 times and the top five 5 times
    # that version's labor, and the top five cost 1.5 times its wood, stone and scrap.
    "tiers": [
        {"id": "campfire", "name": "Campfire", "weight": 0.03, "cost": {"wood": 5}, "labor": 450, "needs": []},
        {"id": "palisade", "name": "Wooden palisade", "weight": 0.08, "cost": {"wood": 30}, "labor": 2400, "needs": ["campfire"]},
        {"id": "storehouse", "name": "Storehouse", "weight": 0.06, "cost": {"wood": 36, "stone": 9}, "labor": 12300, "needs": ["campfire"]},
        {"id": "watchtower", "name": "Watchtower", "weight": 0.06, "cost": {"wood": 28}, "labor": 12300, "needs": ["palisade"], "heavy": True},
        {"id": "workshop", "name": "Workshop", "weight": 0.08, "cost": {"wood": 28, "stone": 14, "scrap": 6}, "labor": 14340, "needs": ["campfire"]},
        {"id": "garden", "name": "Garden plots", "weight": 0.07, "cost": {"wood": 14, "seeds": 2}, "labor": 10200, "needs": ["storehouse"]},
        {"id": "infirmary", "name": "Infirmary", "weight": 0.07, "cost": {"wood": 22, "stone": 14, "medkit": 2}, "labor": 12300, "needs": ["workshop"]},
        {"id": "stone_wall", "name": "Stone wall", "weight": 0.1, "cost": {"stone": 93}, "labor": 51250, "needs": ["palisade", "workshop"], "heavy": True},
        {"id": "forge", "name": "Forge and iron gate", "weight": 0.08, "cost": {"stone": 33, "scrap": 15}, "labor": 23900, "needs": ["workshop"], "trade": "mechanic"},
        {"id": "generator", "name": "Generator and floodlights", "weight": 0.09, "cost": {"scrap": 9, "parts": 4, "fuel": 3}, "labor": 20500, "needs": ["forge"], "trade": "mechanic"},
        {"id": "radio_mast", "name": "Radio mast", "weight": 0.12, "cost": {"wood": 33, "scrap": 12, "parts": 6, "battery": 4}, "labor": 30750, "needs": ["generator", "watchtower"], "heavy": True, "trade": "mechanic"},
        {"id": "helipad", "name": "Helipad", "weight": 0.16, "cost": {"stone": 54, "wood": 27, "fuel": 3}, "labor": 30750, "needs": ["radio_mast", "stone_wall"]},
    ],
    "max_sites": 2,
    "repair_share": 0.3,
    "wall_hp": {"palisade": 500, "stone_wall": 1500, "forge": 400},   # per side; forge adds the iron gate to whichever wall stands
    "patch_wood": 60, "patch_stone": 150,
    "crafts": {"axe": {"scrap": 2, "wood": 1}, "pickaxe": {"scrap": 3, "wood": 1}, "spear": {"wood": 2}, "ammo": {"scrap": 1, "parts": 1}},
    "craft_at": {"axe": "workshop", "pickaxe": "workshop", "spear": "campfire", "ammo": "forge"},
    "craft_yield": {"ammo": 6},
    "garden_produce_per_day": 8,
}


def load(path=None):
    cfg = copy.deepcopy(DEFAULTS)
    path = path or os.environ.get("HOLDOUT_PROFILE")
    if path and os.path.exists(path):
        with open(path) as f:
            cfg.update(json.load(f))
    total = sum(t["weight"] for t in cfg["tiers"])
    assert abs(total - 1.0) < 1e-9, f"tier weights sum to {total}, not 1"
    return cfg
