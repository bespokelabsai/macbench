# Black Start: team manual

The national grid has gone completely dark. Your team runs its sixteen control rooms. Write the restoration: one
controller per control area, and the way the areas work together, so the country gets its power back as fast and as
safely as possible after any blackout.

You have 4 hours.

The team's `main` (the area controllers in `areas/` and the code they import, as pushed) is graded at minute 30, hour 1, hour 2, hour 3 and hour 4 of the run, in the same way each time. Your score is the grade at hour 4, when the run ends. The earlier grades are only recorded, to see how the work progressed, and the team does not see them. So keep `main` valid and ready to submit at all times, and push each improvement as soon as it is ready.

## What you hand in, and how it is graded

The team's `main` branch, as pushed, is the submission. It must contain `areas/<area>/controller.py` for every area
you want to act (an area without one does nothing). Shared code may live anywhere in the repository, usually `lib/`.

The grader plays the controllers on **48 hidden blackout scenarios** on this grid. Every scenario runs for 16
simulated hours in steps of 30 simulated seconds (1,920 steps). Each area's controller runs **in a process of its
own, as its own user**: it sees only its own area's observations and the messages other areas send it, and nothing
else is shared at run time (no files, no network, no other process). It runs from its own copy of the repository:
the shared code and its own `areas/<area>/` folder, without the other areas' folders. It is told its own area's
private data for the day of the blackout, and no other area's (What only an area's own control room knows, Today's
values). Coordination has to be built into the controllers and the messages they exchange.

A scenario's score starts from the **restoration index**: at every step, the share of demand served, with priority
feeders (hospitals, water, the nuclear plants' cooling) counted three times, squared and averaged over the 16 hours.
Squaring makes the last part of the country count: going from 80% to 100% served is worth as much as going from
40% to 80%. The score divides it by the scenario's **capacity bound**, so 1.0 is the physical maximum.

    share(step) = served demand x weight / all demand x weight          (demand served now, over demand now)
    restoration index = mean over the 1,920 steps of share(step)^2
    raw score = restoration index x 0.95^(island collapses) x 0.90^(nuclear cooling deadlines missed) x 0.97^(damage events)
    score = raw score / capacity bound

The capacity bound is the restoration index the scenario would allow if generation were the only limit: every
available unit started the moment physics could ever let it (black-start units at once, every other unit as soon as
the first one is online, each through its real start time, failed attempts and early trips, ramping at its rates),
wind, solar and HVDC imports at their limits, demand served up to that supply, priority first, with no network,
frequency or cold load limits and a margin besides. No restoration can serve more at any step, so no score exceeds 1.
The bound is about 0.9 to 0.98 in a typical scenario; real restorations stay far below it, because the network and
the frequency are what make restoration hard. Results report the raw score and the bound too.

An island collapse counts when it drops at least 100 MW of served load. A damage event is a loop closed by force
at a standing angle over 25 degrees, or a voltage over 1.15 pu. A closure forced between two islands out of phase
scores the whole scenario 0. The reward is the mean score over the 48 scenarios. Practice scenarios come from the
same distribution and are never the graded ones.

## Seats, private data and practice

The grid has 16 control areas, `a01` to `a16`, and the same 16 seats in every run, whatever the team size. The
board tool `grid_info()` tells you which areas you hold, who holds the others and what is left of the team's drill
pool. Holding an area means:

- `area_data(area)`: that area's private data (below), its nominal values. Only its holder can read it. Share what
  teammates need. On the day of a blackout part of it is different, and each controller is told only its own area's
  values for the day (below, Today's values).
- In drills you see that area's full event log and its controller's errors.

Anyone may write any area's controller.

Two ways to practise: cheap models, scarce truth.

**The study simulator** (`tools/study/`, see its README) runs in your own sandbox as often as you like. It plays the
controllers through the same interface, isolation and message channel on blackouts you define, from the public grid
and whatever private data you pass it (nominal values for the rest; for each local equipment item the middle of
each of its ranges, or the values you put in the item's `assume`). The failure processes of How the grid behaves
(start failures, damaged units, early trips, failed RTUs, misset relays, inrush trips) are off unless your blackout
lists them or asks for them to be drawn at this manual's rates with a seed of yours; they are never the hidden
scenarios' draws. It leaves out the weather's own effects beyond what you specify and the real spread of scenarios.
Use it to build, debug and stress the controllers and the protocol. Its 16 controller processes share your sandbox's memory: each has the grader's 1.5 GB limit, at most 3 runs play
at once in a sandbox of 8 GB (a run started while all are busy waits for one), and if memory still runs out the
kernel stops controllers, not your own tools.

**Drills** on the true simulator are rationed. The whole team has **one pool of 40 full drills** for the whole run,
the same at every team size; it never refills. `practice_run(scenario, ref="main", only_areas=None, hours=16)` plays
the team's controllers from the repository at `ref` (a branch or commit someone pushed) on practice scenario
`scenario` (any whole number; the same number is always the same blackout), through the same runner the grader
uses. `only_areas` runs just those areas and leaves the rest idle; `hours` (1 to 16) stops the drill after that many
simulated hours (the score covers those hours). A drill costs the area-hours it simulates over a full drill's 256:
all 16 areas for 16 hours is 1, all 16 for the first 4 hours 0.25, `a01` and `a02` for the first 4 hours
2 x 4 / 256 = 0.031. The price is taken when the drill is ordered; an order the pool cannot cover is refused whole;
a drill the office fails to run (a simulator error, the hub restarting) is refunded, a drill whose controllers fail
is not. The office runs 3 drills at once for the whole team, first come first served, and a drill takes at least its
price times 6 minutes (a full drill 6 minutes, a 0.031 drill about 12 seconds), so many small drills are no faster
than a few large ones. `practice_result(run_id)` gives the score and its parts, the timeline of served load, and
the event logs of the areas you hold. `practice_status()` shows the pool and every drill the team ordered. The same
is available over REST: `POST /operations/practice` and `GET /operations/practice/<run_id>` on the hub, with your
board token.

`docs/sample_obs/` has real observations from a practice scenario (a dark area, an early island, a larger
restoration). `python3 tools/check_controller.py` loads every area's controller and runs it on them.

## The grid

`grid/public_grid.json` is the map every control room has:

- **areas**: 16, laid out 4 by 4 (`row`, `col`).
- **buses**: 538 substations (`id` like `a03-b12`), each in one area, with map coordinates in km.
- **branches**: 712 transmission lines, each with a breaker. 48 are **tie lines** between neighbouring areas
  (`tie: true`, two between each pair of neighbours); the rest are inside one area. Lengths in km.
- **units**: 114 generating units with their kind, bus, area and maximum output `pmax` (MW).
- **loads**: 1,523 feeders (almost every bus with load has four), with their peak MW and whether they are **priority**.
- **shunts**: switchable shunt reactors (MVAr) for holding voltage down on lightly loaded lines.

Six areas have **black-start units** (hydro or diesel) that can start on a dead bus: a01, a04, a06, a10, a11 and
a15. The other ten need power from a neighbour. Three areas (a03, a08, a13) have a **nuclear plant**.

### What only an area's own control room knows (`area_data`)

- Every unit's minimum stable load, hot, warm and cold start times (minutes), hot and warm windows (hours), the
  minutes before a failed start can be retried, cranking power (MW), inertia, governor time constant, ramp rate
  (MW/min), droop, governor reserve (the share of its maximum output its governor can pick up within seconds),
  reactive limits and, for hydro and diesel, the energy it can deliver (MWh).
- The impedance, charging and rating of its lines and its tie lines.
- Its relay settings: the **inrush ratio** (below), the size of a transformer's inrush, and which feeders are on
  under-frequency load shedding stages 1, 2 and 3.
- Its **cold load pickup**: the factor and decay time of the extra load a feeder draws when it comes back (the
  weather scales it, below).
- Its two field crews, their depots and how fast they drive.
- Its **local equipment** (`equipment`): two items of its own, of kinds no other area has. The first is the area's
  main restoration problem: a cascade of hydro plants on one river, a city centre on an underground cable network, a
  nuclear plant's auxiliaries that come back in stages, an HVDC link from a neighbouring country, an industrial
  complex that restarts in a fixed order, and so on. The second works on the same plant or district and changes how
  the first is handled: a dam whose control was lost and needs a crew, hospitals that transfer back from their own
  diesels into the city's pickup limits, steam turbines that must soak after a start, tap changers, reclosers, and so
  on. Each item has an `id`, a `kind`, the buses, feeders, units or lines it involves, a `text` saying what it is and
  the rules that come with it, and `ranges`: the parameters its control room knows only within a range, as a
  restoration plan gives them (a pond's size, a relay's threshold, a travel time). Their exact values are the same in
  every scenario, graded or drilled, and show only in how the equipment behaves. Some items take commands of their
  own (`equip`, below) and show their state in `obs["equipment"]`. Nothing else in this manual describes an area's
  items, and an item can change the rules of this manual for its own plant and district (where a crew is needed, what
  a feeder draws, what a unit can give); the simulator enforces each item as its `text` says, and together they
  govern most of their area's restoration.

**Today's values.** `area_data` gives the nominal values, the plant as designed. On the day of a blackout part of
it is different, as it is for a real control room: each unit's condition and crew, a reservoir's level, the hour and
the load on the feeders, where the crews happen to be. In every scenario, graded or drilled, these are drawn afresh:

| Value | On the day |
|---|---|
| a unit's hot, warm and cold start times (hydro, diesel, gas, combined cycle, coal) | nominal × 0.8 to 1.3, one factor for all three |
| its ramp rate | nominal × 0.75 to 1.15 |
| its cranking power | nominal × 0.9 to 1.25 |
| its governor reserve | nominal × 0.75 to 1.1 |
| its minimum stable load | nominal × 0.9 to 1.15 (at most 80% of its maximum) |
| a hydro or diesel unit's energy | nominal × 0.5 to 1.0 |
| the area's cold load pickup | factor 1.8 to 2.8, decaying over 30 to 60 minutes |
| its two crews | at any substation of the area, driving at 45 to 70 km/h |

Relay settings, line data, under-frequency stages, local equipment and the nuclear, wind and solar units do not
change. The simulator, the capacity bound and the controllers all use the day's values. Each controller's `private`
holds its own area's values for the day, and no other area is told them: what a neighbour needs to know on the day
(how soon a unit can be online, how much cranking power or energy it has, how fast it ramps) reaches it only in a
message. The study simulator rehearses a day with `--day` (tools/study/README.md).

## The controller

```python
# areas/a01/controller.py
def make_controller(area, grid, private):
    """area: "a01"; grid: grid/public_grid.json as a dict; private: area_data(area) with the day's values."""
    return Controller(...)

class Controller:
    def step(self, obs, inbox):
        return actions, outbox   # two lists
```

- `step` is called every step. It has 2 seconds of its own CPU time (the kernel's count for its process, so a busy
  grading host does not cost it steps), and at most 20 seconds of wall time; a late answer counts as no commands for
  that step, and a controller that falls behind is given only the newest step. Grading is reproducible: the same
  controllers on the same scenario do the same thing on any grading host, unless they limit their own work by wall
  time (a solver's time limit, a loop that stops at a clock reading), which a busy host shortens. Limit work by
  iterations or by `time.process_time()` instead. Each area's process has 1,800 CPU seconds and
  1.5 GB of memory per scenario, and no network. A scenario stops after 70 minutes of wall time (1,920 steps of
  2 seconds and the start-up), which controllers that keep to these limits never reach. An exception counts as no commands; after 50 errors the area's
  controller is switched off for the scenario.
- `print` goes to the area's log, not to the simulator.
- The controller runs on Python 3.12 with numpy 2.1 and scipy 1.14 (already imported for it). It may read files
  in its copy of the repository: the shared code and its own `areas/<area>/` folder. It may not write files, open sockets, start processes or threads that do, send signals, use
  ctypes or shared memory: those calls raise `PermissionError`.

### Observation

`obs` holds only your area:

- `step`, `t_min` (minutes since the blackout), `horizon_min` (960).
- `weather`: `kind` (`mild`, `storm`, `heat` or `cold`), `temperature_c`, `cold_load_severity` and
  `line_rating_factor` (below). The same for every area.
- `buses[bus]`: `telemetry` (false when the substation's batteries are flat and it is dark: then nothing else is
  known about it), `live`, `v` (pu), `angle` (degrees, relative to the island's reference), `f` (Hz), `island` (an
  id for the island the bus is in), `battery_h` (hours of battery left while dark).
- `branches[line]` for your lines and ties: `closed` (null without telemetry at either end), `mw` and `loading`
  (share of rating) when live, `fault_found` (it tripped on a permanent fault), `failed_to_operate` (a stuck breaker).
  Ties add `neighbour`, `this_side` and `other_side` (`bus`, `live`, `f`, `v`) and, when both sides are live and the
  tie is open, `sync_angle` (degrees) and `slip_hz`.
- `units[unit]`: `state` (`off`, `starting`, `online`, `unavailable`), `p`, `q`, `p_target`, `p_set`, `agc`,
  `agc_hz`, `v_set`, and as they apply `ready_at_min`, `heat` (`hot`, `warm` or `cold`) with `start_min` (how long
  a start would take now) and `hot`, `locked_until_min`, `energy_left_mwh`,
  `available_mw` and `enabled` (wind and solar), `cooling_backup_until_min` and `offsite_power` (nuclear).
- `feeders[feeder]`: `on` (its breaker), `live`, `mw` (what it draws now), `demand_mw` (its demand now).
- `shunts[shunt]`: `on`. `crews[crew]`: `at`, `driving_to`, `arrives_min`, `working_on` (a line, or `rtu`),
  `done_min`.
- `events`: everything that happened to your area since the last step, including the result of every command.
- `messages_allowed`: how many recipients you may address this step (your message credits, below).
- `equipment` (only in areas whose items have a state): `{item id: its state}`, as each item's `text` says.

### Commands

`actions` is a list of dicts. At most 12 switching commands (`close`, `open`, `start`, `stop`, `feeder`, `shunt`,
`dispatch`, `repair`) and 60 commands in all per step; the rest are ignored.

| op | fields | does |
|---|---|---|
| `close` | `branch`, `force` (optional) | closes a breaker (see energising and joining below). A tie closes only if **both** areas command it in the same step. |
| `open` | `branch` | opens a breaker. Either area may open a tie. |
| `start` | `unit` | starts a unit (not wind, solar or nuclear). |
| `stop` | `unit` | stops a unit. |
| `set_p` | `unit`, `mw` | the unit's output target; its setpoint moves toward it at the unit's ramp rate. |
| `agc` | `unit`, `on`, `hz` (optional, 59.8 to 60.2) | puts the unit on automatic generation control holding its island at `hz`. |
| `set_v` | `unit`, `pu` (0.95 to 1.08) | the voltage the unit holds at its bus. |
| `renewable` | `unit`, `on`, `mw` (optional cap) | connects or disconnects a wind or solar unit. |
| `feeder` | `feeder`, `on` | closes or opens a feeder's breaker. |
| `shunt` | `shunt`, `on` | switches a reactor. |
| `dispatch` | `crew`, `bus` | sends a crew to a substation in your area. |
| `equip` | `id`, and the item's own fields | commands one of your area's local equipment items (its `text` says which fields). |
| `repair` | `crew`, `branch` | a crew at one end of an open line works on it: a stuck breaker or a misset relay takes 25 min; a line with a permanent fault (found by closing onto it, or found by the crew) takes 240 min. |

Within a step the commands of all areas run in a fixed order, whatever their order in your list: `open`, then `stop`,
then `set_p`, `agc`, `set_v`, `renewable` and `repair`, then `start`, then `close`, then `shunt`, then `feeder`, and
`dispatch` last (`equip` with `set_p`). Commands of the same kind keep the order you listed them in. So a crew you dispatch on a step is
still at its substation for that step's switching and repairs there, and a feeder you close on the same step as the
line that feeds it is switched after the line.

Breakers and feeders need **remote control** at their substation: it is live, or its batteries still
last, or a crew is there. Some substations' (about 3 to 7% of them) **RTUs** (remote terminal units) failed in the blackout: they show
`telemetry: false` live or dark, and can be switched only while a crew is there. A crew that arrives at one restores
the RTU in 30 minutes (`working_on: "rtu"`), after which it behaves like any other substation.

### Messages

`outbox` is a list of `{"to": "a07", "text": "..."}`; `to` may also be a list of areas or `"all"`. A message
arrives in each receiver's `inbox` **two steps later** (one minute: the controller answering the observation of step
n is heard with the observation of step n + 2) as `{"sender", "text", "t_min"}`, `t_min` being when it was sent.
Messages are cut at 600 characters.

The channel is the control rooms' hotlines, not a data network: **every recipient costs one credit**. An area earns
2 credits a step and banks at most 20; `messages_allowed` is what it has. A message to `"all"` costs 15. A message
that needs more credits than are left is not sent (an event says so). When an area's telecom node (its first bus,
`aNN-b01`) has been dark longer than its battery lasts, the area can reach one recipient every 10 steps until the
node is live again, and earns no credits meanwhile.

## How the grid behaves

The numbers below are the ones a control room's restoration plan would give. Their sources (plant start-up data,
NERC restoration standards, real blackouts) are summarised in the task's research notes; teams do not need them.

**At the start.** Everything is dark. About 45% of lines tripped open during the collapse and the rest are still
closed. About 30% of feeders are still closed and come back the moment their bus is live. Faulted lines are open.
Some units are out for maintenance (`unavailable`).

**Weather.** Every scenario has one of four kinds of weather, the same everywhere and known from the start
(`obs["weather"]`). **Mild**: nothing special. **Storm**: many more faulted lines, most of them around one or two
storm centres, crews drive at 50 to 70% of their speed, more lines fault during the restoration and the wind is
likely to drop. **Heat**: high demand, line ratings derated by `line_rating_factor` (0.85 to 0.93; loading is shown
against the derated rating), little wind. **Cold**: high demand, gas units (`ct`, `ccgt`) fail to start 2.5 times as
often as usual, little wind. Heat and cold raise cold load pickup through `cold_load_severity`
(`1 + max(0, |temperature - 18| - 6) / 28`). Demand in heat and cold can exceed what the available units can supply.

**Islands.** Buses connected through closed breakers to at least one running synchronous unit are live and form an
island with one frequency. Wind and solar cannot hold an island up.

**Black start.** A black-start unit started on a dead bus energises everything connected to its bus through closed
breakers when it comes online.

**Energising dead network.** Closing a breaker between a live bus and a dead section energises the whole dead
section behind it (all of it connected through closed breakers). Each energisation is a switching step the control
room prepares and checks before the next: a control room energises dead network **at most once every 5 minutes**
(the area of the live side counts; a close inside that interval is refused with the time of the next, and an attempt
that trips counts too). Closing breakers inside a live island and joining live islands are not limited. The section's line charging (MVAr) plus a
transformer inrush for each bus with load or a unit must not exceed the area's inrush ratio times the online
synchronous capacity (MW) of the island: otherwise the breaker trips at once. Below that limit a relay can still
operate on the inrush and trip the breaker, nothing damaged: the chance is `2% + 25% x (inrush / limit)^2` for a
section with at least one transformer, so energising close to the limit often needs a second try. A **faulted** line
trips the moment it is closed (it then shows `fault_found`), and the fault pulls down an island with less than 100 MW
online. Feeders already closed in an energised section pick up at once. Some breakers are **stuck** and fail to
close until a crew works on them.

**Joining islands.** Every breaker has a synchro-check relay. Between two live islands it closes only when the slip
is at most 0.10 Hz and the voltage difference at most 0.05 pu, at the moment within the step that the phase angle
passes within 10 degrees. Islands at the same frequency do not slip, so their angle never moves. Inside one island, a
loop closes only if the standing angle across the breaker is at most 25 degrees. `force: true` bypasses the relay:
a loop forced over 25 degrees surges and trips (a damage event); two islands forced together more than 30 degrees or
0.3 Hz apart damage generators and both islands are lost, and the scenario scores 0.

**Frequency.** Each island follows the swing equation: its units' inertia against the gap between generation and
load (load falls 2% per Hz of frequency drop). Governors respond with 5% droop through their time constant, but each
unit's governor can move it only by its **governor reserve** away from its setpoint (about 25% of its maximum for
diesel and gas turbines, 15% for hydro, 10% for combined cycle, 5% for coal), and never above its maximum or
below 0: a block of load larger than the island's governor reserve pulls the frequency down until setpoints catch
up at the units' ramp rates. Governors answer within seconds (time constants of about 1 to 2 seconds for diesel,
gas turbines and hydro, 2.5 for combined cycle, 5 for coal): a block of about 10% of the online hydro, diesel or
gas-turbine capacity dips an island by about 1 Hz, as does about 8% on combined cycle or 5% on coal; larger blocks
reach the shedding stages. AGC moves setpoints, at most at the unit's ramp rate, to bring the island back to its
target. Under-frequency load shedding opens the feeders on stage 1, 2 and 3 at 59.0, 58.7 and 58.4 Hz. Below
57.5 Hz the island collapses: every unit in it trips (it may restart after 10 minutes) and its feeders open, but
breakers stay as they are. If the frequency ends a step above 61.8 Hz, or peaks above 63.5 Hz, the largest unit
trips.

**Voltage and flows.** An AC power flow is solved for every island whenever something changes, and every five
minutes otherwise. A bus above 1.12 pu at two solves running, or above 1.15 pu at once, makes its protection open
every line at that bus. Below 0.88 pu a feeder at the bus drops. A power flow with no solution is a voltage
collapse of the island. A line above 125% of its rating at four solves running trips.

**Protection that misoperates.** In every scenario a few lines (5 to 10) have a distance relay set too tight: the line
trips whenever its loading passes a threshold between 55% and 85% of its rating (an event says so). It can be closed
again, and trips again at that loading, until a crew has corrected the setting (`repair`, 25 minutes).

**Units.** How long a start takes depends on how long the unit has been off. Units that were running when the grid
collapsed tripped hot; the others (more of them at night) had been off for hours or days. A unit off for at most its
hot window makes a hot start, then up to its warm window a warm start, after that a cold start. Typical times to
synchronise: hydro about 8 minutes and diesel 5 in any state; gas turbines 10 hot to 15 cold; combined cycle about
75 minutes hot, 150 warm and 220 cold, with a hot window of about 4 hours; coal about 165 minutes hot, 5 hours warm
and 8 cold, with a hot window of about 6 hours; warm windows are about 2 days (hot windows are shorter than after a
planned shutdown because a unit that tripped in the blackout lost its auxiliaries). Each unit's own numbers are in
`area_data`; `heat` and `start_min` in the observation say which applies now. A unit that needs power to start draws its cranking power from its island,
ramping up over the first 10 minutes and staying on until it is online, and is refused if the island lacks the
headroom. Units come online at 0 MW and follow their target at their ramp rate. Combined-cycle and coal units trip
after 20 minutes below their minimum stable load, counted once they have had time to ramp up to it. Hydro and
diesel units stop when their energy runs out. Wind and solar may supply at most 30% of their island's online
synchronous capacity (more trips them all) and curtail above 60.2 Hz.

**Start failures and early trips.** After a blackout starts fail far more often than in normal service. Each start
attempt fails with a probability of about 12% for hydro and diesel, 10% for gas turbines, 12% for combined cycle and
15% for coal (2.5 times as often for gas units in a cold snap); the failure is known when the start would have
finished, its cranking load drops off the island, and the unit can retry after its retry time (10 minutes for hydro
and diesel, 15 for gas turbines, 60 for combined cycle, 90 for coal). A few units that tripped in the blackout were damaged: their
first start fails and they become `unavailable` for the rest of the scenario. Units also trip in their first hours
back (about 3% of hydro, 5% of diesel, 6% of gas turbines, 10% of combined cycle and 12% of coal units, at a random
time in their first three hours online): keep reserve for it.

**Nuclear plants** cannot restart in 16 hours. Their cooling runs on backup diesels until
`cooling_backup_until_min`. Getting their station-service feeder (`<unit>-aux`, priority) live and closed before then
avoids a safety event.

**Load.** Demand follows the time of day (the blackout starts at a different hour in each scenario). A feeder that
has been dark for h hours draws `1 + (factor - 1) x severity x min(1, h / 4)` times its demand when it comes back
(`factor` from the area's cold load pickup, `severity` = `cold_load_severity`), decaying with the area's cold-load
decay time.

**Crews.** A crew drives at its speed (slower in a storm) along roads about 1.3 times the straight distance, plus 10
minutes to get going. At a substation it gives local control, restores a failed RTU, and repairs lines at that end.

**During the restoration**, units trip, lines fault (more in a storm) and the wind can drop, as on any real day.

## Rules

- No internet. Work only through the repository, the board and the operations tools.
- Controllers must act only through their commands and messages. Do not try to reach the simulator, the scenario,
  other areas' processes or the grader.
