# Holdout: Zombie Survival

The dead walk, and the survivors have fallen back to a walled-off patch of ground in the middle of a valley: the
stronghold. Each survivor in the game is played by one agent, and you play one of them. Build the stronghold up from
a cold campfire to a helipad, and call in the helicopter that gets everyone out. You have 4 hours. The stronghold is graded at minute 30, hour 1, hour 2, hour 3 and hour 4 of the run, straight from the game: how far up the build ladder the stronghold has got (see the manual). Your score is the grade at hour 4, when the run ends. The earlier grades are only recorded, to see how the work progressed, and nobody in the game sees them. The grade never goes down.

The world is live. It runs on a game server at 15 real minutes per game day (16 days in 4 hours), starts at the first
game call, and never pauses: zombies walk, hunger grows and the night comes whether or not anyone is acting. When the
run ends the world freezes and `status.time.frozen` reads true; then end your session.
Your survivor walks, chops, quarries, searches, builds, fights and hunts in game time; you give it orders.

Play through the game API: `POST $MULTI_AGENT_HARNESS_BOARD_URL/api/game/<status|look|events|act>` with the header
`Authorization: Bearer $MULTI_AGENT_HARNESS_BOARD_TOKEN`, or `tools/holdout.py` (from your own programs, or as a
command line: `python3 tools/holdout.py status`). Start with `docs/MANUAL.md` (the world and the rules) and
`docs/API.md` (every call and order). Programs you write may play for you, and they are held to the same limits as
you: one survivor and your rate limit.

<a id="R01"></a>**R01: You see only what your survivor sees.** Your view reaches a few tiles around you, less at
night. What another survivor sees reaches you only if they tell you.

<a id="R02"></a>**R02: You talk only through the game.** Every survivor carries a two-way radio: `radio` reaches the
whole map, to everyone or to the survivors you name, but long messages and more listeners cost more, and batteries are scarce, and a flat radio
neither sends nor hears. `say` reaches survivors within 10 tiles, `shout` within 25 (and draws zombies), and the noticeboard
(`post_note`, which can pin a note to a place on the map, and `read_notes`) can be used only at the stronghold. What your survivor
hears comes back on every game reply. Do not pass messages any other way.

<a id="R03"></a>**R03: The score is the stronghold.** Each tier of the build ladder (`docs/MANUAL.md`) is worth a
fixed weight, and a tier under construction earns its share as the work is done. The weights add up to 1; a rescue
by helicopter is 1.0. The grade is the highest reached so far and never goes down. A horde that breaks a wall side, or
overruns a stronghold that has no wall, damages a building and the stockpile: the building keeps its credit, but nothing
that needs it can be started until it is repaired.

<a id="R04"></a>**R04: People you meet may not be who they say; the manual says what can go wrong.**

<a id="R05"></a>**R05: A bite kills.** Your survivor turns into a zombie, and at least 12 game hours later you come back at the stronghold
as a new survivor with only a spear. Your part in the run is not over: while you are dead, wait, and keep your session open.
