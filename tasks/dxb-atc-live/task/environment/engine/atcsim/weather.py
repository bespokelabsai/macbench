"""Weather: the real METARs and TAFs of the day, played back in sim time, plus thunderstorm cells.

A METAR (or SPECI) holds until the next one, as at a real airport. Times are seconds since the start of the day's
window (t = 0 at the window start). TAFs are released at their real issue time, so the team sees only forecasts
that existed at that moment, including their misses.

Thunderstorm cells: METARs say when there is a thunderstorm at or near the airport, not where the cells are. The
scenario holds a list of cells (centre track, radius, start and end) built to agree with the METARs: a cell is over
or within 5 NM of the airport exactly when the METARs report TS there, and other cells drift across the approach
and departure paths. Cells show on the weather radar (query "weather").
"""
import math

from .geo import dist_nm, dest, wind_components

LIGHTNING_RADIUS_NM = 5.0       # ramp work stops with lightning within 5 statute miles (ACRP Report 8); we use 5 NM
LIGHTNING_CLEAR_SEC = 15 * 60   # and resumes 15 minutes after the last cell leaves that radius
TS_CODES = ("TS", "TSRA", "+TSRA", "-TSRA", "TSGR")


def _has(wx, *codes):
    return any(any(c in w for c in codes) for w in (wx or []))


WX_GRID_NM = 2.0        # weather radar display resolution
WX_RANGE_NM = 60.0      # how far out the display shows
WX_REFRESH_S = 300.0    # the weather radar picture refreshes this often


class Obs:
    """One METAR, with the values the simulator uses."""

    def __init__(self, d, t):
        self.t = t
        self.raw = d["raw"]
        self.wind_dir = d.get("wind_dir")           # None when variable
        self.wind_kt = d.get("wind_kt") or 0
        self.gust_kt = d.get("gust_kt")
        self.vis_m = d.get("vis_m") if d.get("vis_m") is not None else 10000
        rvrs = [r.get("m") or r.get("value_m") or r.get("rvr_m") for r in d.get("rvr") or [] if isinstance(r, dict)]
        rvrs = [r for r in rvrs if isinstance(r, (int, float))]
        self.rvr_m = min(rvrs) if rvrs else None
        self.ceiling_ft = d.get("ceiling_ft")
        self.wx = d.get("wx") or []
        self.temp_c = d.get("temp_c")
        self.qnh = d.get("qnh_hpa")
        self.windshear = bool(d.get("windshear"))
        self.cb = bool(d.get("cb"))
        self.ts = _has(self.wx, "TS") and not all(w.startswith("VC") for w in self.wx if "TS" in w)
        self.heavy_rain = _has(self.wx, "+TSRA", "+RA", "+SHRA")
        self.rain = _has(self.wx, "RA", "SHRA", "DZ")
        self.fog = _has(self.wx, "FG")
        self.dust = _has(self.wx, "DU", "SA", "SS", "DS")

    @property
    def rvr_or_vis(self):
        """Touchdown RVR when reported, else the visibility. RVR is never below what fog allows, so this is the
        value approach minima are checked against."""
        if self.rvr_m is not None:
            return self.rvr_m
        return self.vis_m


class Weather:
    def __init__(self, scenario):
        w = scenario["weather"]
        self.obs = [Obs(o, o["t"]) for o in w["metars"]]
        self.obs.sort(key=lambda o: o.t)
        self.tafs = sorted(w.get("tafs", []), key=lambda x: x["t"])
        self.alternates = {k: [Obs(o, o["t"]) for o in v] for k, v in w.get("alternates", {}).items()}
        self.cells = w.get("cells", [])
        self.aloft = w.get("aloft", {"veer_deg": 20, "factor_3000": 1.4, "fl100_kt": 30})
        self._i = 0
        self.last_lightning = -1e9

    def current(self, t):
        obs = self.obs
        if self._i >= len(obs) or obs[self._i].t > t:
            self._i = 0
        while self._i + 1 < len(obs) and obs[self._i + 1].t <= t:
            self._i += 1
        return obs[self._i]

    def history(self, t, since):
        return [o for o in self.obs if since <= o.t <= t]

    def tafs_issued(self, t):
        return [x for x in self.tafs if x["t"] <= t]

    def latest_taf(self, t):
        issued = self.tafs_issued(t)
        return issued[-1] if issued else None

    def alternate_obs(self, code, t):
        seq = self.alternates.get(code) or []
        cur = None
        for o in seq:
            if o.t <= t:
                cur = o
            else:
                break
        return cur

    # -- thunderstorm cells -----------------------------------------------------------------------------------
    def active_cells(self, t):
        out = []
        for c in self.cells:
            if c["t0"] <= t <= c["t1"]:
                f = (t - c["t0"]) / max(1.0, c["t1"] - c["t0"])
                lat = c["lat0"] + f * (c["lat1"] - c["lat0"])
                lon = c["lon0"] + f * (c["lon1"] - c["lon0"])
                out.append({"id": c["id"], "lat": lat, "lon": lon, "radius_nm": c["radius_nm"], "top_ft": c.get("top_ft", 40000)})
        return out

    def precip_grid(self, t):
        """The weather radar picture as a controller's display shows it: precipitation intensity, level 1 (light) to
        6 (extreme, a thunderstorm core), in squares of WX_GRID_NM, within WX_RANGE_NM of the airport. The weather radar
        refreshes every WX_REFRESH_S, so the picture is up to that old. Cores are levels 5 and 6; heavy rain spreads a
        few miles around them. Squares with no precipitation are left out."""
        from .geo import dist_nm as _d, dest as _dest
        from . import airport as _AP
        t0 = int(t // WX_REFRESH_S) * WX_REFRESH_S
        cache = getattr(self, "_grid", None)
        if cache is not None and cache[0] == t0:
            return cache[1]
        cells = self.active_cells(t0)
        sq = []
        if cells:
            n = int(WX_RANGE_NM // WX_GRID_NM)
            lat0, lon0 = _AP.ARP
            dlat = WX_GRID_NM / 60.0
            dlon = WX_GRID_NM / (60.0 * math.cos(math.radians(lat0)))
            for i in range(-n, n + 1):
                for j in range(-n, n + 1):
                    p = (lat0 + i * dlat, lon0 + j * dlon)
                    lvl = 0
                    for c in cells:
                        d = _d(p, (c["lat"], c["lon"]))
                        r = c["radius_nm"]
                        if d <= r * 0.6:
                            lvl = max(lvl, 6)
                        elif d <= r:
                            lvl = max(lvl, 5)
                        elif d <= r + 2.0:
                            lvl = max(lvl, 4)
                        elif d <= r + 4.0:
                            lvl = max(lvl, 3)
                        elif d <= r + 7.0:
                            lvl = max(lvl, 2)
                        elif d <= r + 10.0:
                            lvl = max(lvl, 1)
                    if lvl:
                        sq.append({"lat": round(p[0], 3), "lon": round(p[1], 3), "level": lvl})
        out = {"image_t": t0, "grid_nm": WX_GRID_NM, "squares": sq}
        self._grid = (t0, out)
        return out

    def in_cell(self, t, pos, alt_ft=None):
        for c in self.active_cells(t):
            if dist_nm(pos, (c["lat"], c["lon"])) <= c["radius_nm"]:
                return c
        return None

    def lightning_near(self, t, pos):
        near = any(dist_nm(pos, (c["lat"], c["lon"])) <= c["radius_nm"] + LIGHTNING_RADIUS_NM for c in self.active_cells(t))
        if near:
            self.last_lightning = t
        return near

    def ramp_closed(self, t, pos):
        """Ramp work (pushback, towing, marshalling onto stands) stops while lightning is near and for 15 minutes
        after it leaves."""
        self.lightning_near(t, pos)
        return t - self.last_lightning < LIGHTNING_CLEAR_SEC

    # -- winds ------------------------------------------------------------------------------------------------
    def surface_wind(self, t):
        o = self.current(t)
        d = o.wind_dir if o.wind_dir is not None else 0.0
        return d, o.wind_kt, o.gust_kt

    def wind_at(self, t, alt_ft):
        """Wind (from, kt) at an altitude: the surface wind veers and strengthens with height."""
        d, kt, _ = self.surface_wind(t)
        a = self.aloft
        if alt_ft <= 0:
            return d, kt
        f3 = a["factor_3000"]
        if alt_ft <= 3000:
            fr = alt_ft / 3000.0
            return (d + a["veer_deg"] * fr) % 360, kt * (1 + (f3 - 1) * fr)
        hi = max(kt * f3, a["fl100_kt"])
        fr = min(1.0, (alt_ft - 3000) / 7000.0)
        return (d + a["veer_deg"]) % 360, kt * f3 + (hi - kt * f3) * fr

    def runway_wind(self, t, heading):
        d, kt, g = self.surface_wind(t)
        head, cross = wind_components(d, kt, heading)
        if g:
            gh, gc = wind_components(d, g, heading)
            return head, cross, min(head, gh), max(cross, gc)
        return head, cross, head, cross

    def runway_state(self, t):
        """DRY, WET or CONTAMINATED (standing water), from rain in the last hour."""
        recent = self.history(t, t - 3600)
        if any(o.heavy_rain for o in recent):
            return "CONTAMINATED" if any(o.heavy_rain for o in self.history(t, t - 1800)) else "WET"
        if any(o.rain or o.ts for o in recent):
            return "WET"
        return "DRY"
