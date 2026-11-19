"""Ground under the vehicle, ground crossings and touchdown.

Methods of Mission, moved verbatim from sim.py. Mission inherits them from LandingMixin."""
from __future__ import annotations

import math
import os
import time
import zlib
from dataclasses import dataclass, field
import numpy as np
from ... import data as D
from ... import vibro
from ... import terrain
from ...environment import EARTH_SOI, MARS_SOI, MARS_REF_RADIUS, Dispersions, World
from ...core.frames import fixed_to_geodetic, geodetic_to_fixed
from ...core.common import (  # noqa: F401
    MILESTONES, MILESTONE_WEIGHTS, ATM_TOP, EARTH_R_EQ, PARTIAL_APPROACH_D0, SCENARIO_PHASES, STATE_AFTER, FlightFailure, interp, unit, rotate_towards, newtonian_cd, parachute_peak_load, great_circle_km, ElementState,
    SAMPLE_RANGE_KM,
)


def earth_ground_km(lat1, lon1, lat2, lon2):
    """Distance (km) over the WGS84 ellipsoid between two geodetic points: the chord between them on the
    ellipsoid, bent to the arc of a 6371 km sphere. Within 1 m of the true geodesic out to 100 km."""
    c = float(np.linalg.norm(geodetic_to_fixed(lat1, lon1, 0.0) - geodetic_to_fixed(lat2, lon2, 0.0)))
    R = 6371.0e3
    return 2.0 * R * math.asin(min(1.0, c / (2.0 * R))) / 1e3


class LandingMixin:
    # ------------------------------------------------------------------ terrain
    def terrain_body(self):
        """The landing site whose terrain map applies: Jezero whenever Mars is central, Utah on the way
        home. The launch pad at Kennedy has none (sea level)."""
        if self.central == "Mars":
            return "Mars"
        if self.central == "Earth" and "mars_departure" in self.milestones:
            return "Earth"
        return None

    def map_latlon(self, r, t):
        """Latitude and longitude as the sites, the terrain maps and the team's observations use them:
        areocentric at Mars (MOLA and the Mars 2020 products), geodetic on WGS84 at Earth (data.SITES, USGS 3DEP)."""
        if self.central == "Earth":
            lat, lon, _ = fixed_to_geodetic(self.body_fixed(r, t))
            return lat, lon
        lat, lon, _ = self.latlon(r, t)
        return lat, lon

    def _terrain_provider(self, body):
        """The terrain.mars provider (jezero_hirise by default, jezero_50m for today's map) at Mars; None at
        Earth, where terrain.py's Utah map stays."""
        if body != "Mars":
            return None
        return getattr(getattr(self, "world", None), "terrain_mars", None)

    def _t_height(self, body, lat, lon):
        p = self._terrain_provider(body)
        if p is not None:
            return p.elevation(lat, lon)
        return terrain.height(body, lat, lon) if terrain.get_map(body) is not None else None

    def _t_lowest(self, body):
        p = self._terrain_provider(body)
        return p.lowest() if p is not None else terrain.lowest(body)

    def _t_highest(self, body):
        p = self._terrain_provider(body)
        return p.highest() if p is not None else terrain.highest(body)

    def _t_touchdown(self, body, lat, lon, footprint_m):
        p = self._terrain_provider(body)
        return p.touchdown(lat, lon, footprint_m) if p is not None else terrain.touchdown(body, lat, lon, footprint_m)

    def ground_alt(self, r, t):
        """Ground height (m above the datum of altitude()) under the inertial position r: the terrain map
        where it covers, else the flat site altitude. At Earth both the Utah map and altitude() measure from
        the WGS84 ellipsoid."""
        body = self.terrain_body()
        if body is not None:
            h = self._t_height(body, *self.map_latlon(r, t))
            if h is not None:
                return h
        return self.surface_alt()

    def ground_floor(self):
        """The lowest ground the vehicle can meet here: Tudat stops an arc below it, and the crossing of
        the real ground under the path is found from the arc's samples (_ground_crossing)."""
        body = self.terrain_body()
        lo = self._t_lowest(body) if body is not None else None
        return self.surface_alt() if lo is None else min(lo, self.surface_alt())

    def _agl(self, x, t):
        return self.altitude(np.asarray(x[:3]), t) - self.ground_alt(np.asarray(x[:3]), t)

    def _ground_crossing(self, ts, sh, dvh):
        """The first two samples of an arc between which the path goes below the ground: (ta, fa, tb, fb),
        with fa > 0 >= fb the height above the ground. None when it stays above. Only needed where a map
        has ground above the floor that Tudat stops at."""
        body = self.terrain_body()
        top = self._t_highest(body) if body is not None else None
        if top is None or len(ts) < 2:
            return None
        top = max(top, self.surface_alt()) + 100.0
        prev = None
        for t in ts:
            if float(dvh[t][0]) > top:
                prev = None
                continue
            f = self._agl(sh[t], t)
            if f <= 0.0:
                if prev is None:
                    k = ts.index(t)
                    if k == 0:
                        return None
                    prev = (ts[k - 1], self._agl(sh[ts[k - 1]], ts[k - 1]))
                return (prev[0], prev[1], t, f) if prev[1] > 0.0 else None
            prev = (t, f)
        return None

    def touchdown(self):
        central = self.central
        r, v = self.x[:3], self.x[3:]
        vrel = v - self.omega_cross(r)
        up = unit(r)
        vv = float(np.dot(vrel, up))
        vh = float(np.linalg.norm(vrel - vv * up))
        lat, lon = self.map_latlon(r, self.t)   # geodetic at Earth, areocentric at Mars
        pres = self.present()
        lead = pres[0].el
        # The ground under the vehicle (terrain.py): its height, its slope at the vehicle's own scale and the
        # tallest rock under it. Off the maps the ground is flat at the site altitude, with no rocks.
        body = self.terrain_body()
        ground = (self._t_touchdown(body, *self.map_latlon(r, self.t), max(lead.diameter, 0.5)) if body else None) or {}
        slope, rock = ground.get("slope_deg", 0.0), ground.get("rock_m", 0.0)
        extra = {}
        if central == "Earth" and lead.kind == "capsule" and ground.get("surface"):
            extra["surface"] = ground["surface"]
            if ground["surface"] == "lakebed":
                # peak deceleration as the capsule sinks into the lakebed's clay (Kellas 2017, Eq. 2)
                extra["impact_g"] = round(D.SOIL_IMPACT_G_COEF * lead.diameter * float(np.dot(vrel, vrel))
                                          / max(self.total_mass(), 1e-6), 1)
        self.event("touchdown", body=central, vertical_mps=round(vv, 3), horizontal_mps=round(vh, 3), lat=lat, lon=lon,
                   ground_alt_m=round(self.ground_alt(r, self.t), 1), slope_deg=round(slope, 2), rock_m=round(rock, 3), **extra)
        if central == "Mars" and not self.mars_lifted and "mars_descent" in self.milestones:
            if not (lead.is_stage and lead.legs_touchdown_speed):
                # Say how fast it hit and whether a parachute opened: this is nearly always a crash
                # with the heat shield still on, not a separation fault.
                speed = float(np.linalg.norm(vrel))
                chutes = (f"parachutes opened: {', '.join(sorted(self.chute_deployed))}" if self.chute_deployed
                          else "no parachute was opened")
                self.fail(f"hit the ground at {speed:.0f} m/s on {lead.id} ({lead.kind}), which has no landing legs; "
                          f"{chutes}")
            lim = lead.legs_touchdown_speed * 1.25
            tilt = math.degrees(math.acos(float(np.clip(np.dot(unit(self.dir), up), -1, 1))))
            # A crash still earns some credit when it was close: half the milestone, less 1/30 per m/s
            # over the limits (a tilt counts 1 m/s per 5 degrees over 15, a slope 1 m/s per 5 degrees over
            # its limit, a rock 1 m/s per 10 cm over the legs' clearance).
            excess = (max(0.0, -vv - lim) + max(0.0, vh - 1.5) + max(0.0, tilt - 15.0) / 5.0
                      + max(0.0, slope - D.LANDING_SLOPE_MAX_DEG) / 5.0 + max(0.0, rock - D.LANDING_ROCK_MAX_M) * 10.0)
            if excess > 0:
                self._set_partial("mars_touchdown", 0.5 * (1.0 - excess / 30.0))
            if -vv > lim or vh > 1.5:
                self.fail(f"crashed on Mars: {-vv:.1f} m/s down, {vh:.1f} m/s sideways (legs rated {lead.legs_touchdown_speed:.1f} m/s)")
            if tilt > 15:
                self.fail(f"landed tilted {tilt:.0f} deg from vertical and tipped over")
            if slope > D.LANDING_SLOPE_MAX_DEG:
                self.fail(f"landed on ground sloping {slope:.0f} deg and tipped over (the legs stand on up to "
                          f"{D.LANDING_SLOPE_MAX_DEG:.0f} deg)")
            if rock > D.LANDING_ROCK_MAX_M:
                self.fail(f"landed on a {rock:.2f} m rock, taller than the {D.LANDING_ROCK_MAX_M:.2f} m the lander clears")
            self.mode = "landed"
            self.surface_site = (lat, lon, self.ground_alt(r, self.t))
            self.landed_t = self.t
            self.x = None
            self.milestone("mars_touchdown")
            s = D.SITES["mars_landing"]
            dist = great_circle_km(lat, lon, s["lat"], s["lon"], MARS_REF_RADIUS + s["alt"])
            self.event("landing_error_km", km=round(dist, 3))
            self.landing_km["Mars"] = dist
            if dist <= s["radius_km"]:
                self.milestone("mars_target")
            else:
                self._set_partial("mars_target", s["radius_km"] / dist)
            if dist > SAMPLE_RANGE_KM:
                self.fail(f"landed {dist:.1f} km from the sample cache at {s['name']}: beyond the {SAMPLE_RANGE_KM:.0f} km "
                          "the sample transfer can reach, so no samples can be loaded")
            return
        if central == "Earth" and "earth_arrival" in self.milestones:
            if lead.kind != "capsule" or len([p for p in pres if p.el.kind != "parachute"]) > 1:
                self.fail("something other than the bare return capsule hit the ground in Utah")
            speed = float(np.linalg.norm(vrel))
            lim = 10.0 if lead.landing == "parachute" else 45.0
            if speed > lim:
                self._set_partial("earth_touchdown", 0.5 * (1.0 - (speed - lim) / 30.0))
            if lead.landing == "parachute" and not self.chute_deployed:
                self.fail(f"capsule hit the ground at {speed:.0f} m/s without a parachute")
            if speed > lim:
                self.fail(f"capsule hit the ground at {speed:.1f} m/s (limit {lim:.0f} m/s for {lead.landing} landing)")
            if lead.landing == "crushable" and ground.get("surface") == "hard":
                self.fail(f"the crushable capsule hit hard ground off the dry lakebed at {speed:.1f} m/s (ground "
                          f"{ground['ground_alt_m']:.0f} m, slope {ground['resolved_slope_deg']:.1f} deg): it relies on "
                          "the lakebed's clay to take up the impact")
            if self.w:
                # samples' soak-back rise and containment at impact
                self._w_earth_touchdown(lead, extra.get("impact_g"), self.ground_alt(r, self.t))
            s = D.SITES["earth_landing"]
            dist = earth_ground_km(lat, lon, s["lat"], s["lon"])
            self.event("landing_error_km", km=round(dist, 3))
            self.landing_km["Earth"] = dist
            if dist > s["range_km"]:
                # Intact, but outside the range: the touchdown is physical and the flight ends here, but the score
                # counts it only in part (scoring.expected_progress). Replicas of flown capsules land where
                # they did, so this is not a flight failure.
                self.event("landed_outside_range", km=round(dist, 3), range_km=s["range_km"])
            self.milestone("earth_touchdown")
            if dist <= s["radius_km"]:
                self.milestone("earth_target")
            else:
                self._set_partial("earth_target", s["radius_km"] / dist)
            return
        self.fail(f"impact on {central} at {np.linalg.norm(vrel):.0f} m/s")
