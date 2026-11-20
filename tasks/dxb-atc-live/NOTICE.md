# Notice

This task bundles data derived from the sources below. The task's own code is by Bespoke Labs.

- The flight schedule in `task/environment/hub/server/scenarios/live.json` is derived from the OpenSky Network flight
  lists (Zenodo record 7923702), under the Creative Commons Attribution 4.0 licence
  (https://creativecommons.org/licenses/by/4.0/). The flights of 4 January 2020 were moved seven days later, and one
  arrival in four was removed, with the departure its aircraft flies next.
- The airport layout in `task/environment/data/docs/airport.json`, `task/environment/data/docs/taxi_graph.json` and
  `task/environment/engine/atcsim/omdb_ground.json` is derived from OpenStreetMap data, © OpenStreetMap contributors,
  available under the Open Database Licence (https://opendatacommons.org/licenses/odbl/1-0/). These files are made
  available under the same licence.
- The weather reports in `live.json` are METAR and SPECI reports from the Iowa Environmental Mesonet ASOS archive
  (https://mesonet.agron.iastate.edu/) and TAFs from Ogimet (https://www.ogimet.com/). They are public aviation
  weather reports.
- BlueSky (GPL-3.0) and OpenAP (LGPL-3.0), both from TU Delft, are installed from PyPI when the image is built. They
  are not bundled in this folder.
