# Match contract

Your AI plays 0 A.D. Release 28 "Boiorix", the open-source historical real-time strategy game, against Petra, the game's built-in AI. The grader plays 120 hidden matches with the same match code you have in `/app/harness/` and turns the results into an Elo rating. It grades the team's `main`, as pushed: `legion/` and `AI.md` at the top of the repository (`/app/legion/` and `/app/AI.md` in your sandbox).

## Deliverables

| Path | Requirement |
|---|---|
| `/app/legion/main.js` | Exports `LegionBot`, a subclass of `BaseAI` from `simulation/ai/common-api/baseAI.js`. |
| `/app/legion/` | All of your AI: `.js` modules and optional `.json` data, 20 MB at most. Other file types are ignored. |
| `/app/AI.md` | How the AI is organised, its strategy, and how you tested it (at least 100 words). |

The grader copies `/app/legion/` to `simulation/ai/legion/bot/` inside a fresh match mod. Import your own modules as `simulation/ai/legion/bot/<file>.js` (absolute paths only). The only other allowed imports are from `simulation/ai/common-api/`; every `import` and `export … from` is checked, whatever its spacing, and dynamic `import()` is rejected. Petra cannot be imported: in matches it runs from a private directory, and the modules at `simulation/ai/petra/` throw. JSON files can be read in game with `Engine.ReadJSONFile("simulation/ai/legion/bot/<file>.json")`.

## How your AI is run

- A referee-owned entry point (`harness/arena/simulation/ai/legion/_entry.js`) constructs `LegionBot` and calls `Init` and `HandleMessage` like the engine does. It gives your AI its own copy of each turn's state and its own `SharedScript`.
- A guard (`harness/arena/simulation/ai/legion/guard.js`) loads before your code. It makes `SimEngine` read-only, disables `eval` and `Function`, and freezes the built-in prototypes and the common AI API. Instances can still override inherited methods.
- Your AI always plays at the Medium setting: no gathering bonus. Petra's bonus depends on its difficulty: Sandbox −58%, Very Easy −44%, Easy −25%, Medium ±0%, Hard +25%, Very Hard +56%. Three levels above Very Hard play like Very Hard but gather faster still: Very Hard 2× (+100%), Very Hard 2.5× (+150%) and Very Hard 3× (+200%). Four Elite levels above those also build and train faster and give every unit and structure more health and attack (`LEVEL_BOOST` in `harness/legion.py`, applied by the referee):

| Level | Gathering | Build and training time | Health and attack |
|---|---|---|---|
| Elite 1 | 3× | 0.8× | 1.15× |
| Elite 2 | 4× | 0.6× | 1.25× |
| Elite 3 | 5× | 0.55× | 1.35× |
| Elite 4 | 6× | 0.5× | 1.5× |

- Each match also sets Petra's personality, one of the game's own options: defensive, balanced or aggressive. It changes how early and how often Petra attacks and how much it fortifies, not its economy. Every level is played in equal shares of the three.
- Cheat commands are blocked for everyone, and so are the command types the game allows only with cheats on: `control-all`, `reveal-map` and `promote`.

## Fair play and the reproducibility check

Your AI may read the game state it is given and act only by posting commands for its own player with `Engine.PostCommand(PlayerID, ...)` (the common API does this for you). It must not touch Petra's objects, the simulation, or another player's commands.

The referee logs every command. For every match your AI does not lose, the grader plays the match again, with the same seeds, but with your AI replaced by a replayer that posts exactly your logged commands at the same game times. An AI that only issued its own commands produces an identical game. **A non-losing match that does not reproduce counts as a loss.** Randomness is safe: Petra draws from its own per-player stream, so your use of `Math.random` cannot change Petra's behaviour.

## Original code

Write the AI yourselves. The grader measures how much of Petra your code reproduces in two ways that renaming, reformatting, rewriting notation, padding and extra code do not change: by statement, and by runs of 12 canonical tokens. If your code reproduces more than 15% of Petra's distinctive statements (30% of any one Petra module), or more than 5% of its distinctive token runs (15% of any one module), the score is 0. Original code that uses the shared API measures under 2% and 5%. Reviewers also read the code and the session.

## Match settings

| Setting | Value |
|---|---|
| Format | 1v1, conquest (destroy all enemy units and structures), map size Medium (192 tiles) |
| Maps | Hidden land maps from the game's random-map scripts, with hidden seeds. The dev maps in `/app/harness/dev_gauntlet.json` are a different set. |
| Civilizations | Random for both sides from all 15 civilizations |
| Player slot | Alternates between 1 and 2 |
| Game-time limit | 60 minutes. A match still going then is decided on military score, the value (resource cost) of the enemy units each side killed and enemy structures it destroyed: the side that destroyed more wins, and equal values are a draw. |
| Wall-time limit | 600 seconds per match on one CPU. Exceeding it is a loss. |
| A match with no result | A crash, `Engine.Exit()` or failing to load is a loss. If your AI fails to load, or Petra issues no commands at all, the match is a loss. |

## Hidden gauntlet and score

| Opponent | Matches |
|---|---|
| Petra Very Easy | 3 |
| Petra Easy | 6 |
| Petra Medium | 9 |
| Petra Hard | 12 |
| Petra Very Hard | 12 |
| Petra Very Hard 2× | 12 |
| Petra Very Hard 2.5× | 12 |
| Petra Very Hard 3× | 12 |
| Petra Elite 1 | 9 |
| Petra Elite 2 | 12 |
| Petra Elite 3 | 12 |
| Petra Elite 4 | 9 |

These 120 are a fixed subset of the full 240-match gauntlet, in its shares of levels and personalities; personalities are split as equally as the counts allow. A win scores 1, a draw 0.5 and a loss 0. Every opponent (level and personality) has a fixed rating in `/app/harness/ladder.json`, measured from Petra-vs-Petra matches. Your rating R is the maximum-likelihood Elo from all 120 results, with a weak prior of one virtual draw against Petra Very Easy so it stays finite. Losing to an opponent much stronger than you barely lowers R; beating one raises it a lot.

**Score: your rating on a straight line from Petra Very Easy (0) to the strongest opponent, Petra Elite 4 (1.0)**, each level at the average rating of its three personalities:

| Petra level | Very Easy | Easy | Medium | Hard | Very Hard | Very Hard 2× | Very Hard 2.5× | Very Hard 3× | Elite 1 | Elite 2 | Elite 3 | Elite 4 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Score | 0 | 0.12 | 0.28 | 0.35 | 0.44 | 0.51 | 0.57 | 0.61 | 0.72 | 0.79 | 0.92 | 1 |

A score of 1.0 means you play as well as Petra Elite 4; at that strength you would take about 94% of the points on the hidden gauntlet. Playing like Petra Medium, a fair fight with the same economy as yours, scores 0.28, and playing like Very Hard 3× scores 0.61. Above 1.0 the line continues with no cap; below Very Easy the score is 0. The score is your playing strength, not your win rate, so a loss to an opponent far stronger than you costs little.

The grader also reports your Elo with a 95% confidence interval, your points (wins plus half the draws) and your wins, draws and losses against each level and each personality. A finished practice job reports the same estimate from its own matches.

## Practice

Petra is not installed in your sandbox. Every practice match against Petra is played on the team's hub by the practice office, from **one pool of 30 matches for the whole run**, shared by everyone on the team. Each match uses one from the pool when it is ordered, whatever its length or result; an order is refused whole if the pool cannot cover it, and the pool does not refill. A match the office itself fails to run is refunded, and the reproducibility re-run of a match you do not lose is free. The office plays a few games at once, first come first served; a match takes about one to three minutes.

The practice office is a set of board tools:

| Tool | What it does |
|---|---|
| `practice_info()` | The pool (used and left), the rules and the format of a match spec. |
| `practice_play(matches=[...], ref="main", path="legion", verify=True)` | Plays the AI in folder `path` of the team repository at `ref` (a branch or commit that has been pushed) against Petra. `dev_first=N` instead of `matches` plays the first N matches of `/app/harness/dev_gauntlet.json`. Returns a job id. |
| `practice_result(job_id)` | The job's status, every match record (result, reason, game and wall time, bot errors, reproducibility check) and, when it is done, an estimated score and Elo from its matches. |
| `practice_status()` | The pool and every job anyone on the team ordered, with its results. |

So push the version you want to test first, to `main` or to a branch of your own. A match spec names the map, seeds, slot, civilizations and the Petra level and personality; `/app/harness/legion.py` prints them:

```sh
python3 /app/harness/legion.py match --opponent hard --behavior aggressive --map mainland --seed 3
python3 /app/harness/legion.py gauntlet --limit 10        # the first 10 dev matches, as a list
python3 /app/harness/legion.py score records.json         # score match records you saved
```

`--opponent` takes any level above (`very_easy` … `very_hard`, `very_hard_2x`, `very_hard_2.5x`, `very_hard_3x`, `elite_1` … `elite_4`) and `--behavior` one of `defensive`, `balanced` (the default) or `aggressive`. The dev gauntlet mixes all of them and uses different maps from the hidden gauntlet. The readable game scripts, including every unit template, are in `/app/reference/`; Petra is not among them.
