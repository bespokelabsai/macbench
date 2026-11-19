# Plans

One CSV per drone: `D1.csv` to `D8.csv`. Full rules are in `../docs/MANUAL.md`.

Header (exact, no extra spaces): `x,y,z,speed,action,arg,soc,depart_after`

A landmark is mapped only by a rig of at least 3 drones scanning within 10 s of each other, and a show pays only when every slot is filled on time. Every lost drone costs 500 points.

Memory card: each sortie (takeoff to landing) records at most 80 captures (photos and scans); later ones in that sortie are `card_full` and count for nothing, and the card empties when the drone lands.

Quiet zones (`data/noise.json`): each zone accepts at most `budget_s` drone-seconds per clock hour below 60 m; in an hour over the budget, photos and scans taken low in that zone score nothing (reason `noise`).

Each row is the next waypoint; the first leg starts at the drone's start pad. Every drone with at least one row takes off at t = 0 toward row 1. To start later, make row 1 a `pad` row at the drone's own start pad with the start time in `depart_after` (the 180 s minimum time on the pad does not apply to this start).

| action | arg | soc | on arrival |
|---|---|---|---|
| `fly` | ignored | ignored | continue (hovers until `depart_after` first if it is later) |
| `photo` | target id | ignored | hover until `depart_after`, hover 2 s more, take the photo from the waypoint, continue |
| `pad` | pad id | 0 to 1, default 1.0 | land at the pad position (within 1 m, z from 0 to 0.5), charge, take off for the next row when `depart_after` has passed, the battery is at least soc of full and at least 180 s have passed since the landing (`pads.min_turnaround_s`); on the last row the drone stays on the pad |
| `scan` | landmark id | ignored | hover until `depart_after`, hover 2 s more, scan the landmark from the waypoint, continue |
| `show` | `<show id>:<slot>`, e.g. `S2:3` (slots from 0) | ignored | the waypoint must be that slot's position (within 1 m in each of x, y, z); hold until the show's t0 + hold_s (or `depart_after` if later), continue |

`speed` is the airspeed for the leg into the row, 1 to 18 m/s (ends allowed). `depart_after` is in seconds from 06:00 (default 0). Fields are trimmed of whitespace, blank lines are skipped, `nan` and `inf` are rejected. At most 5000 rows. x and y must be inside the city bounding box. Numeric fields longer than 64 characters, and show slot numbers longer than 4 digits, are malformed. A malformed file gives that drone an empty plan (it stays on its pad). Waiting in the air burns battery, so a long wait belongs on a pad (in a `pad` row's `depart_after`). End each plan with a `pad` row: a drone still in the air at the end of the day is lost.

Example for D1 (starts on P1 at 1154.0, 134.3; the photo row's target and viewpoint are illustrative):

    x,y,z,speed,action,arg,soc,depart_after
    1154.0,134.3,60.0,5,fly,,,
    1000.0,300.0,80.0,12,fly,,,
    1000.0,320.0,75.0,10,photo,T0042,,
    1154.0,134.3,60.0,12,fly,,,
    1154.0,134.3,0.0,3,pad,P1,1.0,

The first row climbs straight up over the pad (a leg from the pad must reach 30 m before it is 30 m away); the last two rows come back over the pad and descend onto it.

Check: `python -m skyline check` (exit code 0 even when plans are bad; 2 for a usage error)
