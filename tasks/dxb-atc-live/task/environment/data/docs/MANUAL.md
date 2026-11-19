# Dubai International: controller's manual

API times (`t`) are seconds of airport time since the start of the day. `clock` fields give the same time in UTC. Dubai local time is UTC + 4. Distances are in nautical miles (NM), altitudes in feet and speeds in knots.

## 1. The day

- Traffic is half arrivals and half departures, in the Emirates banks (waves of arrivals, then waves of departures).
- Most arrivals are linked to the departure their aircraft flies next (`next_dep` and `prev_arr` in the flight strips).
- Weather comes as METARs and SPECIs at their issue times. Each holds until the next one. TAFs (forecasts) are released at their issue times and can be wrong.
- The day lasts up to 24 hours of airport time.

## 2. The airport

| Runway | Heading (true) | Length | Use |
|---|---|---|---|
| 12L / 30R (north) | 121 / 301 | 4,351 m | 30R: departures in the 30 direction. 12L: arrivals in the 12 direction |
| 12R / 30L (south) | 121 / 301 | 4,447 m | 30L: arrivals in the 30 direction. 12R: departures in the 12 direction |

- The runways are 385 m apart, too close for independent use. They work in segregated mode, one for arrivals and one for departures. For wake turbulence they count as one runway.
- Configuration 30 is arrivals on 30L and departures on 30R. Configuration 12 is arrivals on 12L and departures on 12R. The SUP sector chooses.
- When one runway is closed, SUP can have arrivals and departures share the other in mixed mode (`single:<runway>`).
- All runway ends have a CAT III ILS with a 3 degree glide path.
- The ground network (taxiways, stands and runway holding points) is in `docs/taxi_graph.json`. Terminal 3 (Concourses A, B and C) is Emirates. Terminal 2 is flydubai. Terminal 1 with Concourse D serves the other airlines.
- Most stands are south of both runways. Departures from the south side to 30R or 12R cross the south runway. When it is the arrival runway, they cross the active arrival runway.

`query of=chart` and `docs/airport.json` give the position of every fix, hold, runway exit, runway entry and stand.

## 3. Airspace and procedures

Arrivals enter the Dubai terminal area at three entry fixes:

| Entry | Hold |
|---|---|
| VUTEB | inbound 107, right turns |
| ITLAP | inbound 204, left turns |
| IMPED | inbound 298, right turns |

Center is en-route control and is not part of your team. It times arrivals at each entry fix `mit_nm` apart at 250 kt (about 86 seconds for 6 NM), and adjusts their speed to meet those times. `mit_nm` is 6 NM unless FLOW asks for more. The distance between two arrivals at the fix depends on their speeds and can be a little less than `mit_nm`. The terminal area is the airspace within 80 NM of the airport, at FL250 and below.

Center gives each arrival its own level:

- The level is the lowest one from 6,000 ft up that no other aircraft for that side's feeder is at, cleared to, or being told to go to.
- Each entry feeds one side, except VUTEB, which feeds both sides.
  - Configuration 30: ITLAP feeds the N side, IMPED the S side, and VUTEB either side.
  - Configuration 12: ITLAP and IMPED feed the N side, and VUTEB either side.
  - Center picks the side for each VUTEB arrival when it releases the flight, about 15 minutes before the entry fix: the side with fewer arrivals planned to it then (N on a tie). The `flights` row gives the side as `feeder_side`.
- A level stays reserved until its aircraft lands or diverts, or is cleared for an approach and is either within 10 NM of a feeder or 600 ft or more off that level.
- When no level up to 22,000 ft is free, Center holds the flight upstream until one is. Center reports that delay only in `query of=flow`.
- A short or medium haul flight (one with an `origin_dep_t`) takes that delay on the ground at its origin, so its fuel is untouched.
- A long-haul flight burns 0.6 minutes of its margin for every minute held upstream. When its margin runs low, it diverts en route without reaching Dubai airspace and comes back later as a return flight.

At its entry fix, an arrival calls Dubai Arrivals (ARR).

- **Feeders and terminal holds.** Each arrival runway has a north and a south feeder: F30N, F30S, F12N and F12S. Each feeder has a published hold (usable at any level). Unless told otherwise, an arrival flies from its entry fix to its feeder and holds there at its last assigned altitude.
- **Arrivals from the approach end.** IMPED arrivals for runway 30 and VUTEB arrivals for runway 12 come from the end of the final, so a direct track to their feeder would cross a downwind. They fly via a point beyond the far end of the downwind and outside it (X30S for runway 30; X12N or X12S for runway 12), then along the outside of the downwind to the feeder. The chart lists these as `via` in `procedures`. A `hold` at the feeder sends the crew direct, across the downwind.
- **Downwind (trombone) points.** D30N1 to D30N9 and D30S1 to D30S9 (D12... for runway 12) lie on downwind legs 7 NM either side of the final, every 2 NM from 4 NM to 20 NM out from the threshold along the final.
- **Final approach points.** 30F05 to 30F25 (and 12F05 to 12F25) lie on the extended centreline, 5 to 25 NM from the threshold. IF30 and IF12 are at 12 NM.
- **The approach.** An aircraft cleared for the ILS (`approach`) captures the localizer between 3 and 30 NM out, when it is within 3 NM of the centreline and converging on it, or when it reaches a final approach point on its route. It captures the glide path when it meets it: normally from below, and from above only if it is cleared to an altitude below the glide path and descends into it. The glide path descends about 318 ft per NM (3,000 ft at 9 NM).
- **Speeds.** Crews fly 250 kt below 10,000 ft unless told otherwise, 220 kt in holds and 160 kt on the final. Assigned speeds end at 4 NM, where crews slow to their final approach speed (130 to 154 kt by type, listed in `docs/aircraft_types.json` with each type's wake group, size code and wingspan).
- **Missed approach.** Straight ahead to 3,000 ft, then to the missed approach hold at 3,000 ft: EGNOT for runway 30, TALTU for runway 12. Each is about 14 NM beyond the far end of the runway and 7 NM to the side, where departures pass 1,000 ft above at their SID altitude. A second hold for when the first is in use, ADVAX for runway 30 and RIMOL for runway 12, lies 12 NM past the threshold in the landing direction and 16 NM to the same side. Crews use the first unless told otherwise.
- **Departures.** Straight ahead to a turn point 8 NM out (S30 or S12), then to their exit gate (GN, GE, GS or GW), 35 NM out. GE from runway 30 goes via E30, and GW from runway 12 via W12, both 25 NM out. Departures climb to 4,000 ft and stay there until DEP clears them higher. DEP hands them to CENTER once they are 25 NM or more from the airport and at or above 9,000 ft.

## 4. Positions, frequencies and radio time

| Sector | Kind | Works |
|---|---|---|
| ARR | approach | Dubai Arrivals: all arrivals from the entry fix towards the final |
| DIR | approach | Dubai Director: arrivals handed to it, sequenced onto the ILS |
| TWR_N | tower | Tower Air North: runway 12L/30R |
| TWR_S | tower | Tower Air South: runway 12R/30L |
| GND | ground | Ground GMC-1: start-up, pushback and taxiing; stand assignment and towing (no radio) |
| GND_2 | ground | Ground GMC-2: the same, for aircraft on taxilane P and aprons C, E, Q and S (stands whose names start with C, E, Q or S) |
| DEL | delivery | Dubai Delivery: route clearances by datalink (`reroute`), no radio |
| DEP_N | departure radar | Departures North: departures to the NORTH and EAST exits, from take-off to the exit |
| DEP_S | departure radar | Departures South: departures to the SOUTH and WEST exits, from take-off to the exit |
| FLOW | flow management | ground delays and miles in trail (no radio) |
| SUP | supervisor | runway configuration and low visibility procedures (no radio) |

Either tower can clear either runway, and either departure position can work any departure.

- An aircraft is "on" a sector once handed to it, and only that sector's controller can instruct it. A handoff (`handoff`, or the `then` field of `approach`, `land`, `takeoff`, `taxi`, `cross` and `pushback`) tells the crew to call the next sector. Its check-in call then takes radio time on that sector's frequency.
- **Handoffs.** A handoff that meets these conditions is standard:
  - Center to ARR: at the entry fix, at the level Center gave it. Center does this. The crew checks in on ARR, and there is no `handoff` event.
  - ARR to DIR: at 230 kt or less and below 10,000 ft.
  - ARR or DIR to a tower: established on the localizer.
  - Tower to ground, for an arrival: clear of the runway.
  - Ground to tower, for a departure: at or close to the runway holding point.
  - Tower to departures: airborne.
  
  Every handoff between your sectors goes through, standard or not. A handoff to CENTER is refused until the departure is 25 NM or more from the airport and at or above 9,000 ft. The receiving seat gets a `handoff` event: `standard`, the conditions not met (`not_met`), and what the crew reported on earlier frequencies (`reported`: its fuel at check-in, "minimum fuel", "mayday", requests). The strip keeps the latest handover (`handover`).
- Every instruction takes radio time: the transmission and the crew's readback, several seconds for a heading and longer for an approach or taxi clearance. Crew calls take a few seconds each. `airtime_s` in each `act` reply gives the time an instruction will take.
- A frequency carries one transmission at a time. An instruction to an aircraft queues behind the transmissions already waiting on its sector's frequency. Crew calls for a go-around, a MAYDAY, a weather deviation or a TCAS advisory go to the front of the queue. Transmissions on different sectors' frequencies run at the same time. An instruction that would wait more than 40 s is refused with `frequency congested`.
- An instruction takes effect when its readback ends. The crew checks it again then and, if the situation has changed, answers "unable" and does nothing.
- **Weather deviations.** In the terminal area a crew (an arrival, or a departure still with Dubai) asks for a deviation before it reaches a storm cell. A crew on the localizer or in a hold does not ask and does not turn away. Crews differ: some ask early and some late, and they ask for different amounts. Answer with `deviation` or a `heading` clear of the weather. With no answer, or a refusal, the crew turns away on its own when the cell gets close, by as much as it judges it needs, and says so. A departure that turned asks for a direct route to its exit once clear of the weather, and if it hears nothing for a while it resumes its own navigation to it.
- **Standing water.** After heavy rain the runway can hold standing water. The first crew to land then reports poor braking, on the arrival tower's frequency only. Until the runway has been inspected (`inspect`, SUP: closed 15 minutes), crews refuse a new landing clearance for it. A crew cleared to land before the report still lands. After an inspection, heavy rain 30 minutes or more later can bring a new report.
- **Take-off slots (DFLOW).** The UAE departure flow service plans every departure's take-off when the crew asks for start-up. When that pushes a take-off later than the flight could go, the flight gets a slot (`ctot_t`, and a `flow` event). The tower can clear it for take-off only from 2.5 minutes before to 2.5 minutes after the slot. DFLOW plans the take-off again when the crew pushes back late, and the slot can then move or be cancelled (a `flow` event says so). A flight that misses its slot gets a new one, or none if DFLOW has room for it then.
- **Departure exits in storms.** A storm on a departure exit sends every departure to it around the weather, often towards the arrival holds. DEP_N, DEP_S or SUP can close an exit (`exit`): no take-off is cleared to it until it reopens. A departure not yet lined up can be given a revised clearance to a neighbouring exit (`reroute`, DEL, GND or GND_2).

## 5. Separation and safety rules

Airport safety monitoring checks these continuously.

**Airborne.** 3 NM or 1,000 ft between aircraft (900 ft counts) when both are in the terminal area. Not checked: aircraft on the same final (the in-trail rule covers them) and aircraft within 4 NM of the airport below 1,500 ft (runway separation covers them).

**In trail on the same final**, measured along the final:

| Leader \ Follower | A | B | C | D | E | F | G |
|---|---|---|---|---|---|---|---|
| A (A380) | 3 | 4 | 5 | 5 | 6 | 6 | 8 |
| B (B777, A350, B787) | 3 | 3 | 4 | 4 | 5 | 5 | 7 |
| C (B767) | 3 | 3 | 3 | 3 | 3.5 | 3.5 | 6 |
| D, E (A320, B737) | 3 | 3 | 3 | 3 | 3 | 3 | 4 |

- The table is in NM.
- Where the table gives 3 NM, the minimum is 2.5 NM while the leader is inside 10 NM, when both aircraft are on the same runway, the runway is dry, visibility is 5 km or more and there is no wind shear.
- The minimum is 6 NM in low visibility procedures.

**Departures.** Time from the previous take-off on either runway. Wake minima, in seconds:

- A380 followed by B: 100. A380 followed by C: 120. A380 followed by D: 140.
- B followed by D: 100. C followed by D: 80.
- Add 60 s when the follower starts its take-off 500 m or more further down the runway.

Route separation: 120 s to the same exit gate, 60 s otherwise.

**Runways.**

- A landing needs a landing clearance, and the runway must be clear when the aircraft crosses the threshold.
- Reduced runway separation is allowed when all of these hold: tailwind 5 kt or less, visibility 5 km or more, ceiling 1,000 ft or more, a dry runway and no wind shear. The runway then counts as clear when the aircraft ahead has landed, is 2,500 m or more down the runway and is still rolling.
- Lining up or crossing in front of an aircraft cleared to land and within 2 NM (1.5 NM for crossings), or while an aircraft is on its take-off roll on that runway, is a runway incursion. So is a take-off clearance while another aircraft is crossing the runway.
- A crew will not start its take-off roll while another aircraft is on its landing roll, taking off or lined up ahead on the runway. It does not check for aircraft on final.
- In low visibility procedures, the landing runway may be crossed only within 500 m of either end.

**Severity classes:**

| Class | Events | Score factor |
|---|---|---|
| C | Loss of separation; final spacing or departure wake spacing below the minimum (but above 75% of it); departure route spacing below the minimum | 0.995 |
| B | Separation under 1.5 NM and 600 ft; final spacing or departure wake spacing under 75% of the minimum; a runway incursion; landing clearance on an occupied runway; MAYDAY FUEL | 0.97 |
| A- | Near collision: under 1 NM and 500 ft | 0.9 |
| A | Accident: a mid-air collision, a collision on the runway, fuel exhaustion | 0.5 |

An accident on a runway closes that runway for 4 hours.

One encounter counts once. Within 5 minutes, events for the same pair of aircraft (of any kind), or for the same aircraft and kind, are one event. If the encounter gets worse, the worse class replaces the earlier one. The `safety` events report each step as it happens, so they can list more events than the score counts.

## 6. Pilots

Crews differ: when they ask, how much they ask for, when they call low fuel and when they act on their own vary from crew to crew. None of this is announced in advance, except through the forecasts and what pilots report on the frequency.

- **Fuel.** Each arrival has a margin above its diversion fuel, in minutes, and reports it at check-in. The margin drops with every minute beyond the planned arrival time (holding, extra vectors) and with each go-around. Operators carried more fuel when the forecast at dispatch showed thunderstorms or fog. Crews use the ICAO calls, and each crew judges its own fuel:
  - MINIMUM FUEL: the crew can accept little more delay.
  - Near its diversion fuel the crew diverts, unless it is cleared for an approach or established on the final. A crew cleared for an approach but not yet established may continue a little below diversion fuel before it gives up. A go-around cancels the approach clearance.
  - If you gave an expected approach time (`eat`, or `efc` in `hold`) later than its fuel allows, the crew diverts earlier.
  - MAYDAY FUEL (class B): the crew will land with less than its final reserve. It says MAYDAY on its frequency. An aircraft whose final reserve runs out is a fuel exhaustion (class A).
- **Diversions.** Crews try their operator's alternates in this order:
  - Emirates: Al Maktoum (OMDW), Abu Dhabi (OMAA), Sharjah (OMSJ), then Muscat (OOMS).
  - flydubai: OMDW, OMSJ, Ras Al Khaimah (OMRK), OMAA, then OOMS.
  - Others: OMSJ, OMAA, OMDW, OOMS, then OMRK.

  An alternate is skipped when it is full, when the aircraft is an A380 and the alternate cannot take one, or when its METAR is below the aircraft's minima. Only OMDW, OMSJ and OMAA have METARs here. Sharjah and Ras Al Khaimah have CAT I ILS only. Each alternate has a fixed number of free stands for diversions, and a diverted aircraft keeps its stand until its return flight leaves for Dubai. When all of these are full, crews go to Doha (OTHH) or Bahrain (OBBI), 200 to 270 NM away.

  The fuel margin is counted to the planned alternate, about 25 NM away. Going farther needs more margin, so the crew leaves the hold earlier. A diverted aircraft returns to DXB an hour or more after it lands, as a new arrival with an R suffix (e.g., UAE202R). Its strip has `return_of`. Some scheduled flights also end in R, and they have no `return_of`.
- **Approach minima.** Each operator is approved for an approach category: Emirates and flydubai CAT IIIB, others down to CAT I. `approach_cat` is in the strips. The crew refuses an approach when the RVR (or the visibility) is below its minimum: 550 m for CAT I, 300 m for CAT II, 175 m for CAT IIIA, 50 m for CAT IIIB. Below 550 m nobody can land unless low visibility procedures (LVP) are in force.
- **Go-arounds.** The crew goes around when:
  - It still has no landing clearance late on the final.
  - It is well above the 3 degree glide path late on the approach (an unstable approach). Crews draw this line in different places.
  - The runway is occupied and the crew can see it (visibility 1,500 m or more, no LVP).
  - It is below minima.
  - Very likely (85%) when the crosswind with gusts is over 35 kt on a dry runway, 30 kt on a wet one or 20 kt with standing water, or when the tailwind with gusts is over 10 kt. Crews also refuse an approach clearance with a tailwind over 10 kt.
  - Often (50%) when it is more than 25 kt above its final approach speed at the threshold.
  - At random, more often with wind shear, gusts and thunderstorms at the airport.

  After a go-around the crew flies the missed approach and calls the sector it is on.
- **How crews fly.** A crew acts a few seconds after its readback, not at once. It flies an assigned heading to within a few degrees and an assigned speed to within a few knots, and climbs and descends at its own rate; heavy aircraft climb and slow down more slowly. Far from the airport, a crew may refuse a speed below what it can fly clean ("unable speed 180, minimum 195").
- **Unusual calls.** Crews ask for shortcuts and levels, and report turbulence near storms; a crew in turbulence may refuse to descend for a while. Rarely, an arrival declares an urgency (PAN PAN, e.g., smoke in the cabin: it wants to land soon, and diverts to its operator's first available alternate if it is not cleared for an approach within 20 minutes), or loses its radio for a few minutes (squawk 7600: it hears nothing and says nothing, flies its last clearance, then calls back). Now and then a crew takes an instruction meant for a similar callsign on the same frequency. Occasionally a crew reads back a wrong altitude, heading or speed and flies it unless the controller corrects it, and occasionally a crew misses an instruction altogether.
- **TCAS.** Every aircraft here carries TCAS, the airborne collision avoidance system. When two aircraft get very close, or will be within seconds, both crews get a resolution advisory, usually one to climb and one to descend. An aircraft on the localizer that must climb goes around. The crews follow the advisory, not your clearance, while the threat lasts. They say "TCAS RA" on the frequency, and "clear of conflict" when they return to their cleared altitude. How close the pair came still counts as a safety event.
- **Weather.** A crew will not fly into a thunderstorm cell, except on the localizer. When one gets close ahead, it turns away, says so on the frequency, and holds that heading until you give it something else. Precipitation is on the weather radar picture (`query of=weather`).
- **Departures.** A departure is ready 15 minutes before its scheduled take-off time (`sched_t`) or when its aircraft has turned around, whichever is later:
  - The turnaround starts when the aircraft is on the stand. It takes about 40 minutes to an hour for a 737 or A320 and about an hour and a half to over two hours for an A380, and it varies from flight to flight.
  - When ready, the crew calls its ground position for start-up and pushback: GND_2 on aprons C, E, Q and S (stands whose names start with those letters), GND elsewhere. A stand's `area` in the data is its terminal area, not its apron. Pushback and start take a few minutes. The crew then calls the ground sector it was told to.
  - Each crew has a duty limit, which the airline knows and ATC does not. A departure that has not lined up by then needs a reserve crew, which takes the operator well over an hour to call in. Emirates and flydubai keep a few reserve crews for the day. Others have none, so their flight is cancelled.
- **Big aircraft on the ground.** Taxilane J is the code F taxilane and U the code E one: a code F aircraft is never routed on U. A code F aircraft may not be side by side with a code E or F aircraft on J and U: within 100 m of each other, the one that came onto its taxilane later stops and waits. A code F aircraft on taxiway K passes behind holding points K1 to K5 only when the aircraft waiting there is code D or smaller: otherwise it stops short of that link and waits. Stands B18R, B21R, B26R and F26R take only the A380.
- **Taxiing.** Crews follow their cleared route, stop behind traffic, and queue at a holding point or behind an aircraft waiting for its stand. At a crossing, the arrival goes first, then the aircraft that has waited longer. Two aircraft nose to nose on one taxiway cannot pass. After a while a tug pulls one aside (usually the departure), where it waits some minutes. A long jam, or an aircraft blocking the exit of a landed aircraft, is broken up the same way, later and at a cost in time. An arrival whose stand is still occupied waits on a remote spot off the taxiway.

## 7. Weather and the airport

- **Low visibility procedures (LVP).** SUP declares LVP with the `lvp` op, and preparation takes 10 minutes. In LVP, and whenever the visibility is under 1,000 m, pilots taxi at 10 kt and stop closer behind traffic.
- **Lightning.** While a thunderstorm cell is within 5 NM of the airport, and for 15 minutes after, ramp work stops:
  - no pushback;
  - no towing;
  - no marshalling onto stands, so arriving aircraft wait on the stand entrance.
- **Runway state.** Airport operations report the runway as WET after rain and CONTAMINATED (standing water) after heavy rain (`query of=weather`, `runway_state`). On a wet or contaminated runway, landing aircraft take longer to slow down and use later exits.
- **Wind.** The surface wind is the METAR's. The wind aloft is not reported to ATC: it usually veers and strengthens with height, but it differs from what the surface wind suggests and changes through the day. Crews often report the wind they meet ("wind at 3,000 ft 290 at 35 knots"). Ground speed on the final is airspeed minus headwind, so a strong headwind adds time between landings at a given spacing.
- **Heat.** Above 40 C, take-off rolls are longer.

## 8. Things that happen on the day

Besides the weather, the day may bring:

- medical emergencies (priority);
- bird strikes, after which the runway is closed for an inspection;
- inspections after an emergency landing or a rejected take-off.

## 9. Ground delays and flow

- FLOW can give an arrival an expected departure clearance time (`edct`) up to 10 minutes before it leaves its origin. The flight then leaves and arrives later, without holding over Dubai. Only flights with an `origin_dep_t` (short and medium haul) can get one. A ground delay costs no fuel, but the delay still counts in the score.
- FLOW can ask Center for more miles in trail at an entry fix (`mit`). Center then holds the extra arrivals before they reach Dubai.
