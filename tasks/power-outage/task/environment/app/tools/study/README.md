# The study simulator

A low-fidelity stand-in for the true simulator, for developing controllers, the protocol between areas and the
restoration sequence as often as you like, in your own sandbox, at no cost to the team's drill pool. Drills on the
true simulator (`practice_run`) are rationed; use them to check what this cannot tell you.

```bash
python3 tools/study/run.py tools/study/example_blackout.json
python3 tools/study/run.py my_blackout.json --private private.json --only a01,a02 --hours 4 --out result.json
```

It plays `areas/<area>/controller.py` through the same `step(obs, inbox)` interface, observation, commands and
message channel as grading, each area in its own process through the grader's controller host (with a 2-second
wall-time step limit, stricter than grading's 2 seconds of CPU time; `--step-timeout` changes it, `--inprocess` runs everything in one process, faster but with shared
module globals). It prints the score (the same restoration index, penalties and capacity bound as grading, the bound computed for
your blackout) and the served load every hour;
`--out` writes every area's events too, `--logs DIR` keeps the controllers' print output.

Memory: each controller process has the grader's address-space limit (1.5 GB) and is the first process the kernel
kills if the sandbox runs out of memory. At most `BLACKSTART_STUDY_SLOTS` runs play at once in a sandbox (default:
one per 1.5 GB of its memory beyond 3 GB, so 3 in 8 GB); a run started while all are busy prints a notice and waits.
`--inprocess` runs take no slot.

## What it knows

- **The grid**: `grid/public_grid.json`, exactly the grader's network.
- **Private data**: only what you pass with `--private`, a JSON object `{"a01": <area_data("a01")>, ...}` (save what
  `area_data` returned for the areas you hold, and what teammates shared). Every other area gets nominal values:
  line impedances and charging from their length (x = 0.00085 pu/km, r = 0.115 x, b = 0.0017 pu/km), ratings 360 MW
  (450 on ties), units the typical data of their kind (MANUAL.md, `engine/grid.py`), inrush ratio 0.8 with 18 MVA
  per transformer, cold load pickup 2.3 decaying over 45 min, no under-frequency stages, two crews at the area's
  first bus driving at 55 km/h. Feeders' power factors are nominal (tan 0.3) everywhere.
- **Today's values**: in grading, each unit's start times, ramp rate, cranking power, governor reserve, minimum
  stable load and stored energy, each area's cold load pickup and where its crews start are the day's, not the
  nominal values `area_data` gives (MANUAL.md, What only an area's own control room knows). `--day N` rehearses
  a day: it varies the `--private` data in the same ranges (start times x0.8-1.3, ramp x0.75-1.15, cranking
  x0.9-1.25, governor reserve x0.75-1.1, minimum load x0.9-1.15, energy x0.5-1.0, cold load pickup factor 1.8-2.8
  over 30-60 min, crews anywhere in the area at 45-70 km/h) and gives each controller its own area's varied data.
- **The physics**: the rules in MANUAL.md, How the grid behaves: islands, black start, energisation limits,
  synchro-check, frequency with governor reserve, AGC and UFLS, AC power flow with voltage and overload trips,
  unit start-up timing (hot, warm and cold), cranking, minimum load, energy limits, cold load pickup, crews,
  substation and telecom batteries, nuclear cooling deadlines, the message credits and delay, and the score.

## Failures

The failure processes of MANUAL.md (How the grid behaves) are the true simulator's own code, off unless the
blackout asks for them:

- `"failures": {"seed": 7}` draws every process the spec does not list at the rates MANUAL.md states (start attempts
  failing 10 to 15% of the time by kind, 2.5 times as often for gas units when `weather` is `cold`; 1 to 3 units that
  tripped hot damaged; early trips of 3 to 12% of units in their first 3 hours; 15 to 35 failed RTUs; 5 to 10 lines
  with a misset distance relay; relays tripping on inrush), with your seed;
- or list them: `start_fail` (`{unit: [true, false, ...]}`, attempt by attempt), `damaged` (units), `early_trip`
  (`{unit: minutes after first online}`), `rtu_failed` (bus ids), `relay_misset` (`{line: loading share}`),
  `inrush_trips` (true or false). A listed process is used as given; the seed draws the rest.

They are not the hidden scenarios' failures: those come from seeds only the grader has.

## Local equipment

An area's local equipment (`area_data()["equipment"]`, two items per area) is in the study simulator for the areas
whose private data you pass with `--private`, with the same rules as in grading; areas without data have none. The
exact values of an item's parameters are not in anyone's data (`ranges` gives what the control room knows): the study
simulator uses the middle of each range, or the values in the item's `assume` dict if you add one
(`"assume": {"pond_mwh": 14}`), so you can rehearse what your drills taught you.

## What it leaves out

These are what the true simulator adds, and what drills are for:

- which units, relays and RTUs fail in the real scenarios, and when;
- the weather's own effects beyond what you put in the spec (storm-clustered faults, slower crews, derated lines,
  gas units failing in the cold), and the real spread of every scenario: which lines are faulted or stuck, which
  units are out, what each unit was doing before the blackout, battery lives, deadlines and events;
- other areas' real private data (unless you pass it) and each feeder's real power factor.

There is no scenario generator and nothing from the hidden or practice scenarios here: every blackout is the one you
write.

## The blackout spec

Every field is optional; `example_blackout.json` uses most of them.

| Field | Meaning (default) |
|---|---|
| `start_hour` | hour of day the blackout starts (18) |
| `weather`, `temperature_c` | shown to the controllers; the temperature sets `cold_load_severity` (mild, 18) |
| `load_scale`, `wind`, `solar` | demand scale and the share of wind and solar capacity available (0.9, 0.5, 0.6) |
| `line_rating_factor`, `crew_speed` | heat derating and storm slow-down (1, 1) |
| `open_at_start` | line ids open after the collapse, or `{"fraction": f, "seed": s}` for a random share (0.45) |
| `feeders_closed` | feeder ids whose breakers stayed closed, or `{"fraction", "seed"}` (0.3) |
| `faulted`, `stuck` | line ids with a permanent fault, and lines whose breaker fails to close until a crew fixes it |
| `unavailable` | unit ids out for maintenance |
| `offline_h` | `{unit: hours}` it had been off before the blackout (units not listed tripped hot) |
| `battery_h`, `telecom_h`, `nuclear_deadline_h` | hours, one number for all or `{id: hours}` (7, 5, 8) |
| `events` | `[[minute, "unit_trip", unit], [minute, "line_trip", line], [minute, "wind_drop", null]]` |
| `seed` | only for the random choices above |
| `failures`, `start_fail`, `damaged`, `early_trip`, `rtu_failed`, `relay_misset`, `inrush_trips` | the failure processes (above; none) |
