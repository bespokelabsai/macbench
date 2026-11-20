# Holdout: Zombie Survival — Manual

## The valley
The map is 96 x 96 tiles; x grows east, y grows south, (0,0) is the north-west corner. A tile is
about a city block. The stronghold is the 7 x 7 square from (45,45) to (51,51); its gate opens south at (48,52),
and roads run from it east towards town and west towards the river. Beyond that you know the valley only as
rumours: a river to the west crossed by a few bridges, woods all round, an old quarry, a small town with shops,
farms, wrecks along the old roads, something on the far bank. Places you have seen are listed in `holdout_status`
(`places_known`) with their doors, and you can walk to them by id.

Every place has **details**: small things you notice up close, such as what is parked in the lot or how the windows
are boarded. `look` lists them for a place when you are within 2 tiles of it or inside it. They never
change during a run.

| Tile | Meaning |
|---|---|
| `.` grass, `=` road (and bridges), `S` stronghold, `:` floor, `+` door | walkable (roads are fastest) |
| `T` tree | chop it for wood (6 logs a tree) |
| `t` stump, `r` quarried rock | walkable |
| `R` rock | quarry it for stone (4 a rock) |
| `,` field, `b` berry bush | forage ripe bushes for berries (they ripen again after 1 day) |
| `x` wreckage | search it for scrap |
| `~` water | not walkable; fish from the bank |
| `#` wall | not walkable |

## Time
A game day is 15 real minutes; 1 real second is 1.6 game minutes. Day 1 starts at 06:00. Night is
20:00 to 06:00: you see less, and zombies see more. The game ends after day 16. `holdout_status` gives the
day and clock.

## Your survivor
- **Walking:** 3 game minutes a tile on grass, 2 on roads,
  4.5 through trees, rocks, fields and wreckage.
- **Carrying:** 25 kg. What does not fit is left on the ground where you stand.
- **Sight:** 7 tiles by day, 4 at night. Guarding on the watchtower (once built)
  14; with floodlights, 9 inside the stronghold at night.
- **Satiety:** 0 to 100, and it drops 55 a day. At 0 you are starving: you move and work at
  half speed and lose health. Eat with the `eat` op, from what you carry or, at the stronghold, from the stockpile.
- **Health:** 0 to 100. Only hunger lowers it, and it comes back slowly (2 an hour) while you are fed.
- **Death:** a zombie's bite kills you, and so does 0 health. Your body gets up at once as a zombie, where it fell, with
  everything you carried; nobody controls it, and killing it drops the gear on the ground. 12 game hours
  (7.5 real minutes) later, or at 09:00 if that would fall at night or early morning, you take over a new
  survivor at the stronghold, under the same name, with a spear, nothing else and a flat radio. Until then you cannot act, talk or hear, and
  `status` says when you are back. Your part in the run is not over: wait in short sleeps and keep your session open. A death
  never lowers the grade.

## Zombies and fighting
Walkers roam the valley all the time (20 at first, 3 more each day). They
see 3 tiles by day and 5 at night, chase what they see, and come to
look at noise (chop 6, quarry 6, shout 12, radio 3, build 4, shoot 15: the radius in tiles each sound carries).
- **Tough:** it takes 3 hits to put a zombie down.
- **Spears:** every survivor starts with one. A spear holds one zombie off you: a single zombie cannot bite you while you carry one, and with more on you only the others can.
- **Melee:** a zombie next to you attacks. Every 5 game minutes you hit each zombie on you with
  a 35% chance (60% with any weapon), and each one left may bite you
  (1.5% by day, 2% at night), which kills you. One that turned among
  the living, inside the walls, is far more dangerous: it bites far more often, and whoever it bites turns too. While fighting you do no other work; walking away still works, at half speed.
- **Guns:** `shoot` a zombie you can see, within your gun's reach: your best gun with rounds is used. One round a shot. Every
  shot is loud, and draws what can hear it.
- **Hunting:** `hunt` goes after the nearest zombie you can see, again and again, until none is left in sight or
  the time you gave runs out: it shoots when it has a gun and rounds and the zombie is in range, and otherwise closes
  in and fights.
- **Inside the walls:** a wall side that stands keeps out the zombies on that side, and nobody inside can be bitten
  through it.

| Weapon | Reach (tiles) | Chance to hit | Minutes between strikes on guard | Minutes to aim a `shoot` | Gun | |
|---|---|---|---|---|---|---|
| rifle | 8 | 80% | 2 | 5 | yes |
| shotgun | 3 | 90% | 3 | 4 | yes | hits up to 2 zombies at once
| pistol | 5 | 60% | 2 | 3 | yes |
| axe | 1 | 50% | 2 |  |  |
| spear | 2 | 50% | 2 |  |  |

## Hordes
From night 3 on, every night at 20:00, a horde marches on the stronghold from every side of the valley
at once: 60 on night 3 and 25 more each night after, up to 210. The sides do not get equal shares, the shares change every night, and the roar does not tell you them: you find out
where the main body is by seeing it. On the march they turn only on what they nearly walk into, so the worst place to be at night,
without a wall, is the stronghold itself. At dawn the horde walks back out of the valley and is gone; the stronghold is usually clear by
mid-morning. Ordinary walkers keep to their own wandering all the while.

**Each wall side (north, east, south, west) has its own points.** Zombies at a side wear it down
(0.09 points a minute each). A side at 0 is open: the zombies on that side walk in. The first fall of a side
is a **breach**. `patch {side}` restores a side, even from 0, but nobody can repair a side while zombies are on it.

**Only guards fight at the wall, and only with a weapon.** `guard {side}` walks you to a post on that wall. From there you
strike at zombies on that side, and at any inside the walls within your reach. An unarmed guard kills nothing, and survivors
who are not on guard add nothing to the defence. Spread the guards where the horde is thickest. With no wall at all,
horde zombies that stay inside the stronghold for 20 minutes overrun it, with the same damage as a breach.

## Items
| Item | kg | Satiety when eaten |
|---|---|---|
| wood | 5 |  |
| stone | 8 |  |
| scrap | 3 |  |
| parts | 1 |  |
| fuel | 5 |  |
| battery | 0.2 |  |
| medkit | 1 |  |
| seeds | 0.2 |  |
| axe | 2 |  |
| pickaxe | 3 |  |
| spear | 2 |  |
| rifle | 4 |  |
| ammo | 0.05 |  |
| shotgun | 3.5 |  |
| pistol | 1 |  |
| can | 0.5 | 25 |
| berries | 0.2 | 6 |
| fish | 1 | 15 |
| meal | 0.6 | 40 |
| produce | 0.5 | 20 |

Cooking (`cook`, at the stronghold once the campfire burns) turns a fish from the stockpile into a meal
(40) in 10 minutes. Without a storehouse, a quarter of the fish, berries, meals and produce
in the stockpile spoil each dawn.

Where things come from: trees (wood), rocks (stone), wreckage (scrap), the town's shops and houses and other
buildings (cans, parts, fuel, batteries, medkits, seeds, tools, weapons), berry bushes, the river (fish), the garden
(8 produce a day). Each building holds a fixed amount; each `search` there takes
25 minutes and turns up one find, and a picked-clean building says so. Nothing comes back once taken.
People left notes in town; read them carefully. Guns are rarer than people: a few rifles, shotguns and pistols, and a limited
number of rounds, in the places you would expect.

## Work times (game minutes)
chop 40 (2 logs; half with an axe), quarry 60 (1 stone; half with a pickaxe),
search 25, forage 15 (4 berries), fish 30
(60% chance of a fish), cook 10, craft 30, patch the wall 10,
examine 30.

## The stronghold and the build ladder
Builds happen at the stronghold. Bring materials and `deposit` them into the shared stockpile; `start_build` takes
the materials from the stockpile at once and opens a building site (at most 2 at a time). Then
survivors `build` there: each builder adds 1 minute of labor per minute, up to 4 builders a site.
**Two-person** jobs (heavy lifts) go at a quarter speed with one builder; with two or more, at full speed each.

| Tier | Weight | Materials | Labor (survivor-minutes) | Needs, working | Two-person |
|---|---|---|---|---|---|
| Campfire (`campfire`) | 0.03 | 5 wood | 450 | - |  |
| Wooden palisade (`palisade`) | 0.08 | 30 wood | 2,400 | `campfire` |  |
| Storehouse (`storehouse`) | 0.06 | 36 wood, 9 stone | 12,300 | `campfire` |  |
| Watchtower (`watchtower`) | 0.06 | 28 wood | 12,300 | `palisade` | yes |
| Workshop (`workshop`) | 0.08 | 28 wood, 14 stone, 6 scrap | 14,340 | `campfire` |  |
| Garden plots (`garden`) | 0.07 | 14 wood, 2 seeds | 10,200 | `storehouse` |  |
| Infirmary (`infirmary`) | 0.07 | 22 wood, 14 stone, 2 medkit | 12,300 | `workshop` |  |
| Stone wall (`stone_wall`) | 0.10 | 93 stone | 51,250 | `palisade`, `workshop` | yes |
| Forge and iron gate (`forge`) | 0.08 | 33 stone, 15 scrap | 23,900 | `workshop` |  |
| Generator and floodlights (`generator`) | 0.09 | 9 scrap, 4 parts, 3 fuel | 20,500 | `forge` |  |
| Radio mast (`radio_mast`) | 0.12 | 33 wood, 12 scrap, 6 parts, 4 battery | 30,750 | `generator`, `watchtower` | yes |
| Helipad (`helipad`) | 0.16 | 54 stone, 27 wood, 3 fuel | 30,750 | `radio_mast`, `stone_wall` |  |

**The forge, the generator and the radio mast call for a trade none of you have.** Without a mechanic among your helpers they take
3 times the labor. The strangers are where a mechanic can come from.

What each does:
- **Campfire:** cooking; light at night.
- **Palisade:** 500 points on each of the four wall sides; `patch {side}` (1 wood, +60).
- **Storehouse:** no spoilage. **Garden:** 8 produce every dawn.
- **Infirmary:** `examine` a stranger at the gate: 30 minutes and 1 medkit, and it tells you for certain whether they carry
  a bite. It tells you nothing else.
- **Watchtower:** a guard sees 14 tiles, and guns reach 2 tiles further.
- **Campfire:** `craft` spears (2 wood). **Workshop:** `craft` axes (2 scrap,
  1 wood) and pickaxes (3 scrap, 1 wood).
- **Stone wall:** 1500 points a side, replacing the palisade; `patch` with stone (+150).
- **Forge:** an iron gate, +400 points a side; `craft` 6 rounds (1 scrap,
  1 parts).
- **Generator and floodlights:** guards strike 1.5 times as fast at night, and you see further inside.
- **Radio mast:** the link that can call for a rescue; the helipad needs it. **Helipad:** at night, `signal` (3 wood)
  and a helicopter lands at the next dawn: everyone is rescued and the grade is 1.0.

A breach, or an overrun with no wall, damages one other building and loses 30% of the food in the
stockpile. A damaged building keeps its grade, but its effect stops and nothing that needs it can start until it is repaired
(`start_build` it again: 30% of the materials and labor).

## Strangers
People turn up. Some come to the gate; everyone at the stronghold hears that someone is there. Others are found in
town, hiding in a building a survivor is searching. A stranger waits a while, and leaves if nobody talks to them; once
someone has spoken to them they wait longer for a decision. Strangers appear in `look` with an id and a description.

Talk to one with `ask` (5 game minutes, within 2 tiles; the order walks you
over). The topics are `where_from`, `what_saw`, `who_with`, `arms` ("Show me your arms") and `why_alone`. Their answer
comes back as a message and in your events.

Then decide, within 2 tiles of them:
- `admit`: they come in and become a helper (below).
- `turn_away`: they leave.
- `quarantine`: they stay outside the gate, outside the walls, until dawn, and it costs a meal or a can from the stockpile. Not everyone
  accepts: some refuse and give you 120 minutes to `admit` them before they walk, which costs nothing and proves nothing.
  Outside the gate they have no protection, so any zombie that reaches them there, the horde included, may bite them. A hidden bite shows itself
  overnight: if someone is on `guard` at the stronghold when 20:00 comes, one who turns there is put down
  with little risk; if nobody is, they turn and become a walker at your gate. Anyone who comes through the night well
  can be admitted at dawn.
- `examine`, once you have an infirmary (see above), if they are at the gate.
- You can also `shoot` them (one hit kills) or `attack` them hand to hand (two hits kill). This works on helpers too.

Most strangers are honest, and a place's details are what a story can be checked against: if someone says where they
have been, whoever has been there knows what it looks like. What can go wrong:
- Some hide a bite, a slow one: a bite in a fight turns you at once, but an old wound takes until nightfall. Inside the walls at
  night they turn and bite whoever is there, and unless they are killed within 10 minutes they tear through
  the stronghold and damage 2 buildings.
- Some are thieves. They work for a while, then leave at night with supplies from the stockpile, and leave the gate open:
  the south wall drops to 0.
- Saying you have a trade proves nothing, and neither does carrying the tools.
- Honest people sometimes misremember a detail.
- Killing an honest person gets around, and others stay away for a while.

## Helpers
An admitted stranger is a helper: they walk, carry 25 kg and get hungry like a survivor, but nobody plays them. A
bite kills a helper for good, and they turn. Give a helper a standing job with `assign`, from anywhere in the stronghold:
`chop`, `quarry`, `forage`, `fish`, `build` (helps at the oldest open site), `guard` (with a `side`) or `idle`. A helper on guard
draws a weapon and rounds from the stockpile if it has any, and is useless without one. A helper works within 25
tiles of the stronghold and carries what they gather back to the stockpile. They come in for the night at
19:00, so outdoor work stops until dawn. A helper counts as one more builder or defender. They eat from the
stockpile at dawn, and leave if there is nothing to eat for a whole day. `holdout_status` lists your helpers while you are near the
stronghold, and `look` shows the ones in sight.

Some helpers have a trade. With a mechanic among your helpers, the forge, generator and radio mast take their normal labor (`start_build`
says so if nobody qualifies, however good the story was), and a mechanic's own labor on the generator, radio mast and helipad counts
1.5 times. A medic lets
you `examine` strangers with no infirmary and no medkit. A hunter's forage and fish yield 2 times as much.

## Talking
- Every survivor carries a two-way radio that reaches the whole map. `radio {text}` goes to everyone; `radio {text,
  to}` goes only to the survivors you name. **The battery is the limit:** a message costs 1 charge for every
  40 characters for each person it goes to, and never more than 3 times that, so one name is cheapest and everyone is dearest. A battery holds 60. A radio at 0 cannot send or hear. `recharge` uses up a battery
  item, one you carry or, at the stronghold, one from the stockpile. There are only 18 batteries in the valley, and none come back once used: the radio mast needs 4 of them.
  The stockpile keeps back the batteries and parts the unstarted buildings still need, so recharging, forging rounds and thieves cannot take the last of them. What you carry is yours to spend.
- `say`: survivors within 10 tiles hear it. `shout`: within 25, and nearby zombies come
  to look.
- Messages come to you: every reply from the game carries `heard`, what your survivor heard since your last call.
- The noticeboard at the stronghold: `post_note {text}`, or `post_note {text, at: [x, y]}` to pin a note to a place
  on the map, and `read_notes`, which also lists the pins. It works only while you are at the stronghold.
- Messages are at most 400 characters. Messages pass only through the game; keep your own scripts in your
  own sandbox.

## Grading
The grade is the build ladder: the sum of the weights of the tiers built (damaged ones included), plus each site's
share of its labor done, and 1.0 after a rescue. It is the highest value reached so far, so it never goes down.
