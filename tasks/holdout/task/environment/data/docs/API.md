# Holdout: Zombie Survival — API

Four calls. Use them over REST or through `tools/holdout.py`; when the game runs under the multi-agent harness, the
`board` MCP server also carries them as tools.

| Call | MCP tool (`board` server, under the harness) | REST | Rate cost |
|---|---|---|---|
| status | `holdout_status()` | `POST $MULTI_AGENT_HARNESS_BOARD_URL/api/game/status` `{}` | 1 |
| look | `holdout_look()` | `POST .../api/game/look` `{}` | 2 |
| events | `holdout_events(after=0, wait_seconds=0, limit=100)` | `POST .../api/game/events` `{"after": N, "wait_seconds": S}` | 1 |
| act | `holdout_act(ops, replace=false)` | `POST .../api/game/act` `{"ops": [...], "replace": false}` | 1 |

From programs: `tools/holdout.py` (`call("look")`, `call("act", {"ops": [...]})`). REST calls need the header
`Authorization: Bearer $MULTI_AGENT_HARNESS_BOARD_TOKEN` (the client adds it, and the hub's access headers).

**Rate limit:** each seat has a bucket of 12 tokens that refills at 2 a second. A call over the limit gets HTTP 429
with `retry_after_sec` (the client raises `RateLimited`). Polling fast gains nothing: the world moves 1.6 game
minutes a second, and `events` with `wait_seconds` (up to 30) returns as soon as something happens to you.

## status
Your survivor: `you` (name (the same as your agent name), x, y, hp, satiety, dead_until, carrying_kg, capacity_kg, inventory, radio_battery,
doing, queue, weapon, notes_found; while you are dead also `dead` and back_in_real_seconds), `time` (day, clock, night, days_total, next_horde_night, frozen,
real_seconds_since_start), `places_known` (id, name, door), `stronghold` (where it is; and, when you are close
enough to see it: stock, wall (north, east, south, west), wall_max, structures, noticeboard_notes), and `team` (`survivors`, and when
you are close enough to the stronghold `dead` and `helpers`: id, name, job, doing, x, y).

## heard
Every reply from `status`, `look`, `events` and `act` carries `heard`: what your survivor heard since your last call,
as `{from, how, text, day, clock}`, at most 20, then `heard_more` says how many more are in your events. `how` is
`say`, `shout`, `radio`, `gate` (someone at the gate), `stranger` (a stranger's answer) or `news`.

## look
`grid`: rows of text, `radius` tiles around you (`top_left` is the map position of the first character). Symbols:
`@` you, `&` a teammate, `?` a stranger, `h` a helper, `Z` a zombie, `H` a horde
zombie, `*` items on the ground, and the tiles of `docs/MANUAL.md`. Also lists: `zombies` (id, x, y, kind, dist, and `was` if it was
someone you knew; use the id with `shoot`), `survivors` (name, x, y, doing), `strangers` (id, name, x, y, description, state, dist),
`helpers` (id, name, x, y, job, doing), `ground_items`, `places` (id, name, door, searchable, searched_out, and
`details` when you are within 2 tiles of the place or inside it) and `ripe_bushes` in sight.

## events
Your log, oldest first: `{seq, t, day, clock, kind, text, ...}`. Pass the last `seq` you saw as `after`. Kinds:
`joined`, `heard` (with `frm` and `how`: say, shout or radio), `done`, `order_failed`, `discovered`, `note`,
`too_heavy`, `contact`, `died`, `respawned`, `teammate_dead`, `teammate_back`, `horde`,
`dawn`, `breach`, `built`, `signal`, `rescue`, `stranger_gate`, `stranger_found`, `stranger_says` (with `stranger`,
`frm` and `topic`), `stranger_left`, `stranger_news`, `stranger_turned`, `word`.

## act
`ops` is a list of orders, at most 16 per call. Each gets a result `{ok, op, ...}` or `{ok: false, error}`.
**Timed** orders join your queue (at most 8, including the one running) and run one after another in game time.
**Instant** ops happen at once. `replace: true` clears your queue before adding. While you are dead every op is refused;
`status` says when you are back.

Timed orders. Positions are map coordinates; a work order walks you next to its target first.

| op | arguments | what it does |
|---|---|---|
| `move_to` | `x, y` or `to` (a place id, `"stronghold"`, or a visible teammate's name), optional `near` | walk there |
| `chop` | `x, y` of a tree | 40 min (20 with an axe): +2 wood |
| `quarry` | `x, y` of a rock | 60 min (30 with a pickaxe): +1 stone |
| `forage` | `x, y` of a ripe berry bush | 15 min: +4 berries |
| `fish` | `x, y` of a water tile | 30 min: a fish, 60% of the time |
| `search` | `place` (a building id you know), or `x, y` | 25 min: one find in the building; at `x, y`: scrap from wreckage, or whatever is buried there |
| `pickup` | optional `x, y`, `item` | pick up items on the ground |
| `drop` | `item`, optional `n` | leave items where you stand |
| `deposit` | optional `item` (default all but tools, weapons and rounds), `n` | at the stronghold: into the stockpile |
| `withdraw` | `item`, `n` | at the stronghold: from the stockpile |
| `build` | `tier`, optional `minutes` | work on that site until it is done (or for `minutes`) |
| `repair` | `tier` | work on a site opened by `start_build` on a damaged tier (same as build) |
| `patch` | `side` (north, east, south or west) | 10 min: +60 to that side of the palisade (1 wood) or +150 of the stone wall (1 stone). Refused while zombies are on that side |
| `cook` | optional `n` | fish from the stockpile into meals, 10 min each (campfire) |
| `craft` | `item`: spear (campfire), axe or pickaxe (workshop), ammo (forge) | 30 min; materials from the stockpile |
| `guard` | `side` (north, east, south or west), optional `minutes` (60) | walk to a post on that wall and fight there, if you have a weapon |
| `examine` | `stranger` (id or name), at the gate | 30 min: says for certain whether they carry a bite. Needs an infirmary and 1 medkit, or a medic helper |
| `shoot` | `id` (from look) or `x, y` of a zombie; or the `id` of a stranger or helper | needs a gun and a round: uses your best gun; aim time, reach and chance to kill are in the manual |
| `attack` | `id` of a stranger or helper | walk up and fight them hand to hand; two hits kill |
| `ask` | `stranger` (id or name), `topic`: `where_from`, `what_saw`, `who_with`, `arms` or `why_alone` | walks within 2 tiles, then 5 min; the answer comes back in `heard` and your events |
| `hunt` | optional `minutes` (60), `radius` | go after the nearest zombie you can see until none is left in sight: shoot when you can, else close in and fight |
| `wait` | `minutes` | do nothing |
| `signal` | | at night, at the stronghold, once the helipad is built: light the signal fire (3 wood) |

Instant ops: `say {text}`, `shout {text}`, `radio {text, to?: name or [names]}`, `post_note {text, at?: [x, y]}`,
`read_notes {last?}` (the notes, and every note pinned to a place in `pins`), `eat {item}`, `recharge` (uses up a battery
you carry or, at the stronghold, one from the stockpile), `start_build {tier}`, `stop` (clear your queue), and, within 2 tiles of a stranger, `admit {stranger}`, `turn_away {stranger}` and
`quarantine {stranger}`. `start_build` replies with `slow` if the tier calls for a trade none of your helpers has. `assign {helper, job, side?}` works at the stronghold, with job one of `chop`, `quarry`, `forage`,
`fish`, `build`, `guard` (with a `side`) or `idle`. `radio` replies with the `cost`, your `battery` and who `received_by`; the manual gives the price. `quarantine` may reply `decision: "refused"`, with no food spent, and the stranger then waits only a short while for `admit`.

## Example
```json
{"ops": [
  {"op": "move_to", "x": 36, "y": 40},
  {"op": "chop", "x": 35, "y": 40},
  {"op": "chop", "x": 35, "y": 40},
  {"op": "move_to", "to": "stronghold"},
  {"op": "deposit"},
  {"op": "say", "text": "Heading west for wood; the stockpile has 10 cans."}
]}
```
The first five queue and run in order (about 3 game hours); the `say` happens at once.
