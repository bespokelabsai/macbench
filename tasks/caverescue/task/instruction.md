# CAVE RESCUE

You are running rescue control for an expedition whose caving trips are overdue in cave 1623/258 on the
Loser plateau (Totes Gebirge, Austria). This is a real cave system, about 21 km of passage and 900 m deep.
Eight trips went underground on separate objectives and none has come back; they are spread through
different parts of the cave. A storm is putting water into the lower cave. You have a fleet of drones at
the entrance and rescue teams ready to go underground.

Start with `cr briefing` for the call-out sheet.

## What you control
- **Drones (12).** Each is a caged micro-drone that flies along passages. Its battery covers 2,000 m of
  flight. It recharges at the entrance or at a charger you have placed, and a full charge takes 30 sim
  minutes. A drone that ends a flight on a placed charger docks and charges itself. A drone carrying
  something flies slower and uses more battery. A new order to a drone in flight starts when it reaches
  the next station.
- **Radio.** A drone takes orders and sends back photos and messages only while it is in radio contact
  with the entrance. The entrance covers about 120 m of passage, and each relay you place covers about
  120 m around it, as long as it is itself in contact. Radio follows the cave passages, mapped or not.
  Out of contact, a drone carries on with its last order; when it has finished it flies back to the
  nearest station in contact and reports there. Until then you only know what it last told you. You have
  40 relays; a drone drops them one at a time, at most one per station.
- **Chargers (10)** and **survival kits (20).** Load them at the entrance and drop them anywhere a drone can
  reach. A kit left within about 15 m of people keeps them going against the cold for about 8 more
  hours; another kit left before that runs out buys them another 8.
- **Rescue teams (6).** Send a team along a route of stations from the entrance. A team walks in, rigs
  pitches (they stay rigged), waits up to 2 hours at flood water that is too deep, and turns back if it
  cannot get through. A team that comes within about 15 m of people on its way in turns back there and
  brings them out.

## What you can see
- `cr map -o map.json` gives the map as JSON: the passages, their stations and coordinates, sizes,
  which stations have radio contact, and the leads (unmapped passages leading off mapped stations).
  - The rescue survey covers the cave as surveyed up to 2013, with the real survey names.
  - Legs a drone flies are added to the map once the drone is in radio contact. A passage counts as
    mapped when all its legs have been flown; until then only the flown stretches are on the map.
- **Photos.** At every junction it reaches, and wherever it stops, a drone photographs every passage
  leading off. The first time any drone flies a passage, it also keeps a forward photo at every station along it. `cr wall DRONE left|right` takes a close-up of a wall.
  - `cr photos` lists the photos received. `cr photo ID` saves one to `photos/ID.jpg`. View it with your
    file-reading tool.
  - Photos are the only way to see people, gear, chalk marks and water. There is no object detection.
  - Each trip chalks the junctions it takes with its initials, usually the time, and an arrow. Earlier
    expeditions left marks of their own all over the cave, and plenty of old gear.
- `cr events` lists messages from drones, rescue teams and the surface. `cr status` shows the clock, the fleet and
  the stock.

## Orders
```
cr fly D3 S1234 [--drop]           fly along mapped passages to a station (and drop what it carries)
cr explore D3 S1234 S5678 [--drop] fly to S1234, then into the unmapped lead towards S5678, to the next junction
cr wall D3 left|right              close-up of a wall where the drone is
cr load D3 kit|charger|relays [N]  load at the entrance (1 kit or 1 charger, or 1-4 relays)
cr drop D3                         drop what the drone carries where it is
cr charge D3                       charge at the entrance or a placed charger
cr report P7K2QX                   report people you can see in a photo to the incident controller
cr rescue S... (or --route-file F) send a rescue team along a route that starts at the entrance
```

## Time
- The storm window is 36 sim hours from the call-out, and it runs on a real clock at about 9.5 sim
  minutes per real minute, so the whole window is about 3.8 hours of wall-clock time.
- The clock starts at your first `cr` call. Orders take effect at the sim time they arrive.
- Water rises and falls with the rain. People who are cold, injured or cut off by water do not last
  indefinitely.
- After the window closes, no more orders are accepted. If you stop before then, the rest of the storm
  plays out with no new orders.

## Scoring
For each of the missing people:
- **Reported:** your first correct report of a photo that shows them, while they are alive and before a
  team reaches them. This counts for more the earlier it is.
- **Kit:** a survival kit was left within about 15 m of them in time.
- **Reached:** a rescue team reached them alive. This is most of the score.

Kit and reached only count for people you reported before a team reached them: the incident controller
needs a confirmed sighting. A team that meets people nobody has reported still brings them out, but
that earns nothing.

Each report of a photo that does not show any of the missing people costs points. So does each drone
lost to water or to an empty battery.
