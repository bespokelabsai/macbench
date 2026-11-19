# ATC API

Every call is `POST $MULTI_AGENT_HARNESS_BOARD_URL/api/atc/<endpoint>` with a JSON object body. The endpoints are `status`, `query`, `events` and `act`. They are also the MCP tools `atc_status`, `atc_query`, `atc_events` and `atc_act` on the `board` server. On some backends the hub also needs access headers: send each header that `/run/multi-agent-harness/gitconfig` lists as `extraHeader = Name: value` with every call. Your board token (`MULTI_AGENT_HARNESS_BOARD_TOKEN`) sets your seat. A seat named in a body is ignored.

Every reply has this shape:

```json
{"ok": true, "result": {}, "status": {"phase": "running", "t": 5321, "clock": "19:28:41Z", "runways": {"arr": "30L", "dep": "30R", "dir": "30", "mode": "segregated"}, "lvp": "off", "ramp_closed": false, "your_sectors": ["ARR"], "events_seq": 812}}
```

- `t` is seconds of airport time since the start of the day.
- `phase` is one of these:
  - `running`
  - `ended`: the day is over.
  - `frozen`: the run is over.
  - `crashed`: the ATC system failed and the day stopped.
- When `ok` is false, `error` holds the reason.

Rate limit: each seat has a bucket of 40 tokens that refills at 6 a second. A call costs 1 token, and an `act` costs 1 per op. A call over the limit gets HTTP 429 with `retry_after_sec` and costs nothing. An `act` holds at most 20 ops.

Size limits: in an `act` op, a list has at most 32 items (`via` at most 12), a string at most 64 characters, and `then` is a sector name (for `direct`, a list of fixes); a number must be finite. In `query` and `events`, `ids` and `kinds` are lists of at most 200 strings, each `where` field takes at most 200 values, and `limit`, `offset` and `after` are numbers. A call over a limit is refused with the reason.

## status

Body: `{}`. The result holds:

- the status fields;
- the current METAR;
- the runway state (`DRY`, `WET` or `CONTAMINATED`);
- `your_frequencies`: for each radio sector you work, its frequency's backlog in seconds and the flights on it;
- who works each sector.

## query

Body: `{"of": <collection>, "where": {field: value or [values]}, "ids": [...], "limit": n, "offset": n}`.

`where`, `ids` and `limit` apply to row lists. At most 400 rows come back. `total` gives the full count, and `offset` pages through the rest.

| of | What |
|---|---|
| `traffic` | The radar: every aircraft in the air (row below). |
| `flights` | The flight strips: every flight of the day, its schedule and state. |
| `ground` | Surface movement radar: aircraft taxiing or holding on the ground, lined up, landing or taking off. |
| `runways` | Configuration, occupancy, closures, recent take-offs and landings (for wake timing), landing clearances given, LVP. |
| `stands` | Every stand, its size code, area, position, and who occupies or has booked it. |
| `weather` | Current and recent METARs, the last four TAFs issued, the weather radar picture (`weather_radar`), ramp status, runway state, the alternates' METARs and free stands. |
| `sectors` | Each sector's kind, controller, frequency backlog and aircraft. |
| `flow` | Miles in trail at each entry, Center's current delays, reserve crews left, and the levels Center has reserved in each side's stack. |
| `chart` | Fixes (lat/lon), holds, runways, exits and entries (taxi graph nodes), exit gates. Also in `docs/airport.json`. |

A `traffic` row:

```json
{"id": "UAE202", "kind": "ARR", "type": "A388", "group": "A", "radar_t": 5320, "lat": 25.3102, "lon": 55.5207, "alt_ft": 6000, "gs_kt": 243, "ias_kt": 220,
 "trk": 219, "hdg": 225, "vs_fpm": 0, "sector": "ARR", "dist_arp_nm": 14.4,
 "cleared": {"mode": "hold", "alt_ft": 6000, "speed_kt": null, "heading": null, "route": [], "hold": "F30N", "approach": null, "landing": false}}
```

- `cleared.handover` is the strip's latest handover, as in the `handoff` event.
- `squawk` is `7600` for a crew whose radio has failed and `7700` for an emergency, else null.
- The radar picture updates once per antenna turn, every 4 airport seconds (`radar_t` is when it was taken), and every query between two turns returns the same picture. Positions carry the radar's own error, tens of metres. Altitudes come in 100 ft steps.
- `weather_radar` in `query of=weather` is the weather radar picture: `squares` of `grid_nm` with a precipitation `level` from 1 (light) to 6 (extreme, a thunderstorm core), refreshed every few minutes (`image_t`).
- `cleared` is the flight strip: what controllers have cleared the flight to, written when each instruction was given. It is not what the crew is flying. A misheard or missed instruction, a weather deviation the crew made on its own or a TCAS manoeuvre shows only in the radar fields (`trk`, `hdg`, `alt_ft`, `vs_fpm`) and on the radio. Before any instruction, an arrival is on the route to its entry fix and a departure on its SID. `mode` is one of these:
  - `route`: cleared to the fixes in `route` (a departure: `sid`, its SID to the gate in `route`).
  - `hold`: cleared to hold at `hold`.
  - `heading`: given the heading in `heading`.
  - `deviating`: a weather deviation was approved.
  - `approach`: cleared for the approach to `approach`.
  - `missed`: sent around.
  - `divert`: diverting to an alternate, whether sent there or by the crew's own decision.

An arrival's `flights` row has these fields:

- `sched_t`: the scheduled landing time.
- `entry` and `eta_entry_t`.
- `origin_dep_t`: the departure time from its origin, set for short and medium haul flights. Only these flights can get a ground delay. The flight has left its origin once `t` passes `edct_t` (if set) or `origin_dep_t`.
- `edct_t`, `stand`, `next_dep`, `approach_cat`, `go_arounds`, `diverted_to`.
- `return_of`: set for the return flight of a diverted arrival.
- `feeder_side`: `N` or `S`, the feeder side Center planned it to for the arrival runway in use (null until it is planned).

A departure's `flights` row has these fields:

- `sched_t`: the scheduled take-off time.
- `ctot_t`: its DFLOW take-off slot, when it has one.
- `gate`, `stand`, `prev_arr`.
- `cancel_reason`, when the flight is cancelled.

Both kinds also have `phase`, `sector`, `outcome` (`landed`, `departed`, `diverted`, `cancelled` or `crashed`) and `done_t`.

Phases:

- Arrivals: `scheduled` (not yet near Dubai), `enroute` (Center), `arrival`, `landing`, `taxi_in`, `on_stand`, `diverting`, `diverted`.
- Departures: `not_ready` (on its stand: the crew says on the radio when it is ready), `pushed`, `taxi_out`, `holding` (at the runway holding point), `lined_up`, `takeoff`, `airborne`, `gone`, `cancelled`.
- A flight in an accident has the phase `crashed`.

## events

Body: `{"after": seq, "kinds": [...], "limit": n}`. The result is `{"events": [...], "last_seq": n, "head_seq": n}`. Each event has `seq`, `t`, `clock` and `kind`, plus its own fields. Keep `last_seq` and pass it as `after` next time.

You get these events:

- **Radio traffic on your sectors** (`kind: radio`): what was said on the frequencies of sectors you work, as words: `frequency` (the sector) and `text`. There is no flight id or message type.
  - Crews use their airline's radio callsign (`docs/callsigns.json`), e.g. "Emirates 202" for UAE202, often with "heavy" or "super". Some calls give only the flight number ("202"), and numbers are often said as words ("two zero two").
  - The same call comes in different words from different crews. Part of a call can be lost ("[unreadable]") or a word garbled. `say_again` asks the crew to repeat it.
  - Most readbacks repeat the instruction. Some repeat only part of it, some say only "wilco" or "roger", and a few instructions are not heard at all: then there is no readback and nothing happens.
  - A readback can carry a wrong number: the crew will fly what it read back.
  - Arrivals check in with their fuel endurance. Crews also call when ready for pushback, ready to taxi, holding short, after vacating, going around, for weather, low on fuel, diverting and after a TCAS manoeuvre.
- **Handoffs to your sectors** (`kind: handoff`): `flight`, `from`, `to`, `standard` (whether it met the standard conditions in MANUAL section 4), `not_met`, and `reported`: what the crew said on earlier frequencies (fuel at check-in, "minimum fuel", "mayday", requests).
- **Conflict alerts on your sectors** (`kind: alert`). The radar projects aircraft between 1,500 and 20,000 ft within 50 NM up to 90 seconds ahead on their present track, speed and climb or descent. A pair that would come within 3.3 NM and 950 ft, unless both are established on a localizer, raises an alert on the frequency of each staffed sector involved, at most once every 2 minutes per pair. `flights` names the pair.
- **Airport events for everyone.**
  - Weather: `metar`, `taf`, `ramp` (lightning closures), `lvp`.
  - Operations: `notam`, `runway_config`, `flow`, `sector`.
  - Arrivals and departures: `inbound` (an arrival handed to Dubai is on its way), `landed`, `departed`, `go_around`, `diverting`, `diverted`, `cancelled`, `tcas` (a TCAS resolution advisory, or clear of conflict).
  - Other: `airline` (crew and operator messages), `ground` (tows), `pilot_report` (emergencies), `safety` (every safety event, with its class).

Radio traffic on frequencies you do not work never reaches you.

## act

Body: `{"ops": [op, ...]}`. The result has `results`, one per op, in order. A radio op returns `queued: true`, `starts_in_s` (its wait for the frequency) and `airtime_s`. It takes effect when the readback ends, and the readback appears in your events. A refused op returns `ok: false` and `error`.

### Positions

| op | Fields | |
|---|---|---|
| `open` | `sectors: [...]` | Work these sectors, each on its own frequency. Aircraft already on them now talk to you. |
| `close` | `sectors: [...]` | Close them. They are unstaffed until a seat opens them. |

### Radio instructions

Radio instructions apply only to aircraft on a sector you work, and only for the sector kinds listed. DIR is an ARR sector. A tower can also give `altitude`, `heading` and `hold` to an aircraft on its frequency after a go-around.

| op | Sector | Fields | Effect |
|---|---|---|---|
| `heading` | ARR, DEP | `flight`, `hdg` | Fly this heading (degrees true). |
| `altitude` | ARR, DEP | `flight`, `ft` | Climb or descend to this altitude. |
| `speed` | ARR, DEP | `flight`, `kt` (null: own speed) | Fly this indicated airspeed, from the type's final approach speed (`docs/aircraft_types.json`) up to 340 kt. Ends at 4 NM on the final. |
| `direct` | ARR, DEP | `flight`, `fix`, `then: [fixes]` | Fly direct to the fix, then the fixes in `then`. |
| `route` | ARR | `flight`, `fixes: [...]` | Fly via these fixes. At the last one, the crew holds if it has a published hold, else keeps its heading and asks. |
| `deviation` | ARR, DEP | `flight`, `approve` (true or false) | Answer a crew's weather deviation request. Approved, the crew turns as it asked and reports clear of the weather later. Refused, it waits for another instruction. A `heading` also answers the request, if it keeps clear of the weather. |
| `hold` | ARR | `flight`, `fix`, `efc` (optional sim time) | Hold at a published hold. `efc` is the expected further clearance time, which the crew uses for its fuel decision. |
| `approach` | ARR | `flight`, `runway`, `then` (optional sector) | Cleared ILS approach. With `then`, the crew calls that sector at 8 NM. |
| `eat` | ARR | `flight`, `time` | Expected approach time. The crew plans its fuel on it. |
| `divert` | ARR | `flight` | Divert now, to the operator's first available alternate. Refused ("unable") when no alternate can take it. |
| `handoff` | any radio | `flight`, `to` (sector, or `CENTER` for departures) | Contact that sector. Center takes a departure only 25 NM or more from the airport and at or above 9,000 ft. |
| `land` | TWR | `flight`, `exit` (optional node), `then` (optional sector) | Cleared to land. With `then`, the crew calls that sector once it has vacated. |
| `go_around` | TWR, ARR | `flight` | Go around. |
| `lineup` | TWR | `flight` | Line up and wait, at the entry it taxied to. |
| `takeoff` | TWR | `flight`, `then` (optional sector) | Cleared for take-off. With `then`, the crew calls that sector once airborne. The crew answers "unable" while another aircraft is landing, taking off or lined up ahead on the runway. |
| `cancel_takeoff` | TWR | `flight` | Stop. A take-off rejected at speed closes the runway about 15 minutes for inspection. |
| `cross` | TWR | `flight`, `then` (optional sector) | Cross the runway ahead (the aircraft is holding short of it). |
| `taxi` | GND, TWR | `flight`, `to` (stand, for an arrival), `runway` and `entry` (node, optional, for a departure), `via: [taxiway refs]` (at most 12), `then` | Taxi. A departure taxis to the holding point of the entry, which by default is the nearest full-length one. It stops at every runway it must cross until cleared. With `then`, it calls that sector at the holding point. |
| `hold_position` | GND, TWR | `flight` | Stop where you are. |
| `pushback` | GND | `flight`, `then` (optional sector) | Pushback and start-up approved. With `then`, the crew calls that sector when ready to taxi. |
| `say_again` | ARR, TWR, GND, DEP | `flight` | Ask the crew to repeat its last call. It answers with the call again, as words. |

### System actions

System actions use no radio and need the named sector.

| op | Sector | Fields | Effect |
|---|---|---|---|
| `assign_stand` | GND or GND_2 | `flight`, `stand` | Book a stand for an arrival. The stand's size code must fit the aircraft. A stand that is still occupied can be booked: the arrival waits short of the apron until it is free. |
| `tow` | GND or GND_2 | `flight`, `stand` | Tow a parked aircraft to a free stand. It takes 20 minutes and the ramp must be open. |
| `edct` | FLOW | `flight`, `time` | Ground delay at the origin, for an arrival that has an `origin_dep_t` and has not yet left. `time` is its new departure time from the origin, no earlier than `origin_dep_t`. It can be set or changed until 10 minutes before the flight's current departure time. |
| `mit` | FLOW | `entry`, `nm` (5 to 60) | Center's miles in trail at that entry fix. |
| `inspect` | SUP | `runway` | Close the runway for an inspection by airport operations, 15 minutes. It clears a poor braking report. |
| `exit` | DEP_N, DEP_S or SUP | `gate` (`NORTH`, `EAST`, `SOUTH` or `WEST`), `closed` (true or false), `reason` (optional) | Close or reopen a departure exit, e.g. for a storm on it. No take-off is cleared to a closed exit. The `flow` query lists closed exits. |
| `reroute` | DEL, GND or GND_2 | `flight`, `gate` | A revised departure clearance to a neighbouring exit (NORTH and SOUTH border EAST and WEST), for a departure not yet lined up. |
| `runway_config` | SUP | `dir` (`30` or `12`), `mode` (`segregated` or `single:<runway>`) | Change the runway configuration. Aircraft already cleared for an approach keep that clearance. |
| `lvp` | SUP | `on` (bool) | Start or cancel low visibility procedures. They take 10 minutes to come into force: the status `lvp` is `prep` until then, then `on`. |
