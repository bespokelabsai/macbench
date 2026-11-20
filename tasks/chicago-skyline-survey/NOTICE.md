# Notices and data licenses

The world data ships twice, identical: in `task/environment/app/data/` (what the agents see) and in
`task/tests/data/` (the grader's pristine copy). The hidden wind days and their normalisation are in
`task/tests/hidden/`.

## OpenStreetMap (buildings, river and rail geometry)

`city.json` (buildings, river and rail) and the files built on its buildings and river (`targets.json`,
`landmarks.json`, `pads.json`, `nofly.json`, `noise.json`, `shows.json`) contain information from OpenStreetMap, © OpenStreetMap contributors, which is made available under the Open Database
License (ODbL) 1.0: https://opendatacommons.org/licenses/odbl/1-0/ (summary and attribution guidelines:
https://www.openstreetmap.org/copyright). These derived files are made available under the same license.

## Open-Meteo and ERA5 (wind forecast)

`wind_forecast.json` is weather data from Open-Meteo.com (https://open-meteo.com/en/license), licensed under
Creative Commons Attribution 4.0 (https://creativecommons.org/licenses/by/4.0/). It contains modified Copernicus
Climate Change Service information (ERA5 reanalysis); neither the European Commission nor ECMWF is responsible for
any use of it. The practice days in `data/practice_winds/` and the hidden days in `tests/hidden/` are synthetic:
the forecast plus a random hourly error, as `docs/MANUAL.md` (section 8) describes.

## Chicago Transit Authority (L train schedules)

The L train runs are derived from the Chicago Transit Authority's public GTFS feed
(https://www.transitchicago.com/developers/), used under the CTA's terms for its developer data. The data is
provided as is, and the CTA does not endorse this task.

## Sun positions

`sun.json` holds computed sun vectors for the survey date (NOAA solar position equations, refraction-corrected
elevation); it carries no third-party data.

## Fictional parts

The survey contractor, its client, the airspace rules, the no-fly and quiet zones, the light shows and the pads are
invented for this task. They do not describe real flight restrictions or permissions in Chicago.
