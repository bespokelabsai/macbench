# Mindustry: many fronts

Your team plays one live game of Mindustry together. Enemies attack your core from several directions. Keep the team producing as much as it can, for as long as it can.

The team has 8 player units, or one per agent if there are more than 8 of you, and they are divided evenly among you. You share one core, one stock of items and one map. Every unit can see the whole map, but a unit can build, mine and carry only within its own range. How you divide the fronts, the ore fields and the building work is for the team to decide.

You have 4 hours. The game's state is graded at minute 30, hour 1, hour 2, hour 3 and hour 4 of the run, in the same way each time. Your score is the grade at hour 4, when the run ends. The earlier grades are only recorded, to see how the team progressed, and the team does not see them.

The game runs in real time and never pauses. It starts at the team's first API call and keeps running whether or not anyone acts.

## Playing the game

Call the API with `POST $MULTI_AGENT_HARNESS_BOARD_URL/api/game/<endpoint>`, or use the MCP tools `game_status`, `game_query`, `game_events` and `game_act` on the `board` server. Your programs can call it through `tools/game.py`. The full reference is `docs/API.md`.

Each call acts as one of your units, which you name in its `unit` field. `status` lists your units in `your_units`. Programs you write may play for you, and they follow the same limits:

- Each unit has its own bucket of tokens. It holds 24 tokens and refills at 4 tokens a second. Units cannot share tokens.
- A read costs 1 token, plus 1 for every 256 rows it looks through.
- An op costs 1 token, plus 1 for every item in its list.
- A call may hold at most 16 ops, an op at most 64 list items, and a body at most 64 KB.
- A call over a limit is refused.

## Scoring

Your score counts each item that a drill or a crafter makes and that a building, e.g., a conveyor, delivers into a core. Each item counts at its weight.

| Item | Weight |
| --- | --- |
| copper, lead, sand, scrap, spore pod | 1 |
| coal | 2 |
| titanium, metaglass | 4 |
| graphite | 5 |
| silicon, pyratite | 8 |
| blast compound | 15 |
| plastanium | 25 |

Items your units mine, items withdrawn or deposited by a unit, starting items and construction refunds do not count. What a crafter makes from them does count. The score never goes down.

## Losing the core

When your team's last core is destroyed, that world ends, and what it produced stays in your score. Two minutes later the team starts over on a new map with a new core. Everything produced after that counts 15% less, and the cut compounds with each lost core, down to a floor of 25%. A crash of the game server counts as a lost core.

The rules, the map size, the waves, the number of units and their action rate are the same for every team size. The score is read from the game itself. The team repository is only for what you want to share with each other.
