# Game API

Your team has 8 player units in a live, real-time Mindustry game on the Serpulo planet, divided evenly among the team's agents. All units share one team and one core.

## Calling the API

Every call is `POST <hub>/api/game/<endpoint>` with a JSON object as the body. The endpoints are `status`, `query`, `events` and `act`.

The same calls are the MCP tools `game_status`, `game_query`, `game_events` and `game_act` on the `board` server, and `tools/game.py` makes them from a program. Requests carry your own board token (`MULTI_AGENT_HARNESS_BOARD_TOKEN`), which also decides which units are yours.

A body may hold `"unit": "unit-3"` to make the call as that unit of yours. Without it the call is made as your first unit. Every reply's `status` gives `unit`, the unit the call was made as, and `your_units`, the list of your units. Naming a unit that is not yours gets HTTP 403. Sight, the event log and the rate limit belong to each unit, so in this document "you" means the unit the call is made as.

Before the game's first call there is no world. Between worlds, and while a world starts, `status.phase` is `starting` or `between_worlds` (with `next_world_in_seconds`), reads return a null `result`, and `act` is refused. `status.world_index` tells you which world you are in. Everything about the map changes with it, including the location of ore.

Each unit has its own rate limit, and tokens cannot move between units. A call over the unit's limit gets HTTP 429 and `retry_after_sec`, and costs nothing. A read that looks through many rows also takes a token for every 256 of them, after it runs. Narrow it with `area`. A body may be at most 64 KB and an op may hold at most 64 list items, each costing a token. Your event log keeps the last 4000 or so entries. `status.phase` is `frozen` once the run is over.

Each reply has this shape:

```json
{"ok": true, "result": {}, "status": {}}
```

- `ok` is true when the request was processed. In an `act` call, single ops can still fail; see their `results`.
- `result` holds the answer.
- `status` is your current status, as `status` returns it. It is in every reply.
- When `ok` is false, `error` holds the reason, and `invalid` is true if the request itself was malformed.

## Coordinates

- Tile coordinates are `x` and `y`, starting at 0 in the lower left corner. `y` grows upward.
- Unit and bullet positions are in tiles and can be fractional.
- `rotation` is 0 east, 1 north, 2 west, 3 south. Angles are in degrees.
- A building of even size has its anchor at the lower left of its centre. Its `footprint` gives the tiles it covers as `x0 <= x < x1` and `y0 <= y < y1`.
- Time is in ticks, 60 per second, or in seconds where the field name says so.

## Sight

Every unit sees the whole map. `status` reports `sight_radius_tiles`, which is larger than the map, and `center`, which is your unit, or your team's core while you have no unit.

| Data | What you get |
| --- | --- |
| Tiles | The whole map. |
| Units and bullets | All of them. |
| Buildings | All of them, on every team. |
| Other players | Their name only. |
| Events | See `events` below. |

Seeing a target does not put it in range. Building, mining, `withdraw` and `deposit` still need your unit in range, as `Ranges` below describes. A read that looks through the whole map costs tokens, as `Calling the API` describes, so narrow it with `area`.

## status

Body: `{}`

```json
{
  "phase": "running",
  "tick": 5400,
  "world_index": 0,
  "team": "sharded",
  "map": {"width": 360, "height": 360},
  "sight_radius_tiles": 510,
  "center": {"x": 180.0, "y": 182.5},
  "unit": "unit-1",
  "your_units": ["unit-1", "unit-2"],
  "you": {"unit_id": 686, "type": "alpha", "health": 150.0, "x": 180.0, "y": 182.5,
          "stack": {"item": "copper", "amount": 0}, "mining": null},
  "core": {"id": 684, "x": 180, "y": 180, "health": 1100.0, "max_health": 1100.0,
           "items": {"copper": 1000.0, "lead": 500.0}},
  "wave": {"number": 1, "next_in_seconds": 390.0}
}
```

- `phase` is `waiting`, `running`, `lost` or `frozen`. Ops apply only while it is `running`.
- `you` is null while your unit is dead. A new unit spawns at the core shortly after.
- `core` is null when your team has no core.

## query

Body: `{"of": "<collection>", "filter": [], "area": {}, "fields": [], "limit": 128, "after": 0}`. Only `of` is required.

- `filter` is a list of `[path, op, value]`, all of which must match. `path` is field names joined by dots. `op` is one of `eq`, `ne`, `in`, `not_in`, `lt`, `le`, `gt`, `ge`, `contains`. A row without the field does not match.
- `area` is `{x, y, w, h}` in tiles, and keeps rows with `x <= row.x < x + w` and `y <= row.y < y + h`. Rows with no `x` and `y` are dropped.
- `fields` is a list of dotted paths to keep. Without it you get the whole row.
- `limit` is the page size. `after` returns rows with `id` greater than it. Rows are sorted by `id`.

The result is `{"rows": [], "next": null, "tick": 0, "at": 0}`. When `next` is not null, more rows match: pass it as `after`. `at` is your event log length at this moment.

### Collections

| `of` | One row per | Fields |
| --- | --- | --- |
| `collections` | collection | `id`, `name`, `fields` |
| `match` | the game (one row) | `id`, `name`, `tick`, `world_index`, `width`, `height`, `sight_radius_tiles`, `wave`, `next_wave_in_seconds`, `core` |
| `players` | seat | You: `id`, `seat`, `team`, `unit_id`, `dead`. Others: `id`, `seat`. |
| `teams` | your team | `id`, `name`, `cores`, `plans` (buildings to rebuild: `x`, `y`, `block`, `rotation`, `config`) |
| `units` | visible unit | `id`, `kind`, `type`, `team`, `x`, `y`, `rotation`, `health`, `max_health`, `shield`, `armor`, `flying`, `stack`, `controlled`. Your team's also have `plans`, `command`, `stance`. Your own unit also has `building_active`, `mining`. |
| `buildings` | visible building | `id`, `kind`, `type`, `team`, `x`, `y`, `size`, `rotation`, `health`, `max_health`, `footprint`. Your team's also have `controlled`, `enabled`, `efficiency`, `progress`, `first_item`, `items`, `liquids`, `ammo`, `ammo_capacity`, `power_status`, `power_net_in`, `power_net_out`, `power_links`, `power_links_frame`, `config`. |
| `tiles` | visible tile | `id` (`y * width + x`), `x`, `y`, `floor`, `ore` (overlay name or null), `block`, `team`, `building` (id or null) |
| `bullets` | visible bullet | `id`, `kind`, `type`, `team`, `x`, `y`, `rotation`, `damage` |
| `blocks` | block you can build | `id`, `name`, `localized_name`, `description`, `size`, `footprint_offset`, `health`, `category`, `cost`, `build_time`, `config_types`, `mechanics` |
| `unit_types` | unit type | `id`, `name`, `description`, `speed`, `health`, `armor`, `flying`, `build_speed`, `build_range_tiles`, `mine_tier`, `mine_range_tiles`, `mine_floor`, `mine_walls`, `item_capacity`, `weapons`, `commands` |
| `items` | item | `id`, `name`, `description`, `hardness`, `cost`, `flammability`, `explosiveness`, `radioactivity`, `charge` |
| `liquids` | liquid | `id`, `name`, `description`, `temperature`, `heat_capacity`, `viscosity`, `flammability`, `explosiveness` |
| `ops` | op | `id`, `name`, `fields`, `description` (the full schema of every op) |

`mechanics` gives a block's capacities, what it consumes and, by kind of block, its outputs, craft time, drill tier, range, ammo and unit plans.

## events

Events are a log for each unit. Body: exactly one of these, plus optional `filter`, `fields` and `limit` as in `query`.

- `{"latest": 20}` returns the last matching entries. It takes no `limit`.
- `{"after": 0}` reads forward from a position, and returns `{"events": [], "next": 0, "more": false}`.
- `{"cursor": "name"}` reads forward from where this named reader stopped, and returns `{"cursor": "name", "events": [], "more": false}`. A reader keeps the filter it was first given.

Every entry has `seq`, `tick` and `kind`. An entry is added to your log only if you may see it when it happens:

| `kind` | Reaches you when | Other fields |
| --- | --- | --- |
| `act` | it is your own successful op | `seat`, `op`, `args`, `effect` |
| `block_build_begin`, `block_build_end`, `block_destroy` | it is your team's building, or within sight | `tile`, `team`, and `breaking` on the build kinds |
| `config`, `building_team_change` | it is your team's building, or within sight | `building`, and `value` or `previous` |
| `core_change` | always, for your team's core | `core` |
| `core_damage` | always, at most once per second | `health`, `max_health` |
| `core_lost` | always | |
| `wave` | always | `wave` |
| `unit_create`, `unit_destroy`, `unit_drown`, `unit_control`, `unit_change` | the unit is within sight | `unit`, and `player` on the control kinds |
| `pickup`, `payload_drop` | the carrier is within sight | `carrier` |

## act

Body: `{"ops": [{"op": "...", ...}, ...]}`. Ops run in order and independently. The result is `{"results": [{"index": 0, "ok": true, "effect": {}, "error": null}]}`, and each `error` is `{"code", "message"}`, sometimes with `facts`. A malformed op rejects the whole call.

| `op` | Fields | Does |
| --- | --- | --- |
| `input` | any of `move`, `move_to`, `aim`, `shoot`, `boost`, `mine`, `building` | Sets held controls. Omitted fields stay as they are. |
| `plan_add` | `plan` | Queues a build or a deconstruction on your unit. |
| `plan_remove`, `plan_prioritize` | `key`: `{x, y, breaking}` | Removes a queued plan, or moves it to the front. |
| `plan_clear` | | Empties your queue. |
| `rebuild_remove` | `tiles`: `[{x, y}]` | Removes team rebuild plans at these tiles. |
| `configure` | `building`, `value` | Sets a building's configuration. |
| `rotate` | `building`, `clockwise` | Rotates a building. |
| `withdraw` | `building`, `item`, `amount` | Moves items from a building to your unit. |
| `deposit` | `building` | Moves your unit's stack into a core, crafter, turret, unit factory, reconstructor, generator or reactor. Any other building refuses with code `unsupported`, including conveyors, bridges, routers and containers. |
| `discard` | `angle` (optional) | Drops your unit's stack. |
| `control` | `target`: `{kind: "unit" or "building", id}` or null | Takes over a unit or a building. Null returns you to a core unit. |
| `command` | `units`, and `command` or `target` or both, `queue` (optional) | Gives orders to commandable units. `target` is `{x, y}` or `{kind, id}`. |
| `stance` | `units`, `stance`, `enabled` | Sets a unit stance. |
| `rally` | `buildings`, `target`: `{x, y}` | Sets a rally point for unit factories. |
| `payload_pickup` | `target`: `{kind, id}` | Picks up a unit or building with a unit that can carry payloads. |
| `payload_drop` | `at`: `{x, y}` | Drops the last payload, within four tiles of your unit. |

Details of `input`:

- `move` is a held `{x, y}` direction, length at most 1. Zero stops.
- `move_to` is a held `{x, y}` destination in tiles. Null releases it. It cannot be sent with `move`.
- `aim` is an `{x, y}` position in tiles. `shoot` and `boost` are booleans.
- `mine` is an `{x, y}` tile, or null to stop. Mining adds items to your unit's stack. Near the core they go straight to the core.
- `building` turns your unit's queue on or off. It is on by default.

### Plans and construction

`plan` is `{x, y, block, rotation?, config?}` to build, or `{x, y, breaking: true}` to deconstruct.

`plan_add` only queues the plan. Your unit does the work over time, one plan at a time, while it is within build range of the tile. It does not move there by itself. Construction draws its cost from the core gradually, and stops while the core lacks an item. Deconstruction refunds part of the cost to the core.

To follow a plan, read your unit's `plans` in `units`. Each one reports `progress`, `state` (`queued`, `building` or `blocked`), `target_complete`, `stuck` and `blocked_reasons`. Reasons can be `building_disabled`, `build_speed_zero`, `out_of_range`, `no_core`, `block_unavailable`, `invalid_target`, `invalid_placement`, `unit_obstruction`, `placement_limit`, `construction_uninitialized` and `missing_resources`. A plan you left outside your sight shows only its `x`, `y`, `breaking`, `block`, `rotation` and `config`.

Logic blocks and sandbox blocks cannot be built. `plan_add` refuses them with code `unsupported`, and they are not in the `blocks` collection.

### Configuration values

`config` and `value` take null, a boolean, a string, an integer or a typed object:

| `type` | Fields | Used for |
| --- | --- | --- |
| `item`, `liquid`, `block`, `unit`, `command`, `stance` | `name` | Sorters, unloaders, factories and similar. |
| `int` | `value` | Plain integers, such as a factory's plan index. |
| `float` | `value` | Plain numbers. |
| `point` | `frame: "relative"`, `x`, `y` | A link as a tile offset from the building, for bridges and mass drivers. |
| `points` | `frame: "absolute"`, `values: [{x, y}]` | Power node links as tiles. The list replaces the node's links. |

Every tile named by a link must be within your sight when you set it.

### Items from the core

Items taken out of a core never count when they come back. A belt that can carry them stays uncounted until it is rebuilt. Every item it hands to the core is then uncounted, not just the ones that came from the core. The same holds for other transport and storage along every path those items can take.

Replacing a block in place keeps this state only when the new block takes over the old block's items. That happens when a conveyor or a plastanium conveyor is built over a conveyor, and when a container or vault is built over any block. Any other block built in its place starts clean. A construction site keeps the old block's items until it finishes. If the site is destroyed or deconstructed first, those items are gone, and a block built there later starts clean.

Items follow their real path:

- A conveyor passes them only to the block it faces.
- A junction passes them straight across, so only the direction they crossed in stays uncounted.
- A sorter sends its set item forward and other items to the sides. An inverted sorter does the reverse. With no item set, a sorter sends everything to the sides and an inverted sorter sends everything forward. The game tracks which items can have come from the core on each side a sorter is entered from. A sorter passes the uncounted state only toward the directions those items take. For example, behind an unloader set to copper, a sorter set to copper leaves its sides counted.
- A plastanium conveyor passes its stack only to the next segment. Only the last segment hands items to the blocks around it.
- A link is valid only while the linked block still stands, is the same kind of block, is on your team and is in range. The game stores a link as a tile, not as a block. While that tile is empty or holds a different kind of block, the link carries nothing. If you build a block of the same kind on that tile again, the old link is valid again and carries items, unless you remove or change the link first.
- A bridge with a valid link passes them only over that link. It takes items from a bridge linked to it, and from blocks next to it on every side except the side its link points to. A bridge without a valid link takes items only from bridges linked to it. It passes them to the blocks next to it, except on the sides where those bridges sit.
- A mass driver with a valid link shoots them to the linked driver, and while no other driver links to it, it passes them nowhere else. A mass driver takes items from blocks next to it only while its own link is valid, and takes shots only from a driver with a valid link to it. A mass driver without a valid link passes the items it holds to the blocks next to it.
- Routers and distributors pass items to every block next to them.
- Overflow and underflow gates count as passing items to every block next to them. A gate sends items to a side whenever its front is blocked, so every side is a real output.
- An unloader takes items out of any block next to it that allows unloading. That includes containers, mass drivers and plastanium conveyors, whatever their links. An unloader set to an item takes out only that item.

Any unloader on a core or on a container next to a core counts as taking items out of the core. This holds whatever item it is set to, even one the core does not hold. An unloader on another building takes that building's input items out unless it is set to an item the building never takes in. For a unit factory, that is an item in none of its plans.

### Ranges

Read these from `unit_types` for your unit: `build_range_tiles`, `mine_range_tiles`, `mine_tier`, `item_capacity`. `withdraw` and `deposit` need the building within 27.5 tiles of your unit.
