"""Aircraft in the air: BlueSky flies them (OpenAP performance, real wind); this module steers them.

The guidance here plays the pilot and the flight management system: it follows the cleared route, flies a holding
pattern, intercepts and tracks the localizer and glide path, flies the published missed approach and the SID. It
only ever sets BlueSky's selected heading, altitude, vertical speed and speed, as a crew does on the autopilot.
"""
import math

import numpy as np

from .geo import bearing, dest, dist_nm, angdiff

FT = 0.3048
KT = 0.514444
GS_TAN = math.tan(math.radians(3.0))
FT_PER_NM_GS = 6076.12 * GS_TAN   # about 318 ft per NM on a 3 degree glide path

_bs = None


def bluesky():
    """Import and start BlueSky once per process, headless."""
    global _bs
    if _bs is None:
        import io
        import contextlib
        import os
        import tempfile
        from pathlib import Path
        # BlueSky keeps a work folder under the home directory. A user with no home, or an unwritable one, gets a
        # temporary home instead, so the engine starts for any user.
        try:
            writable = os.access(Path.home(), os.W_OK)
        except RuntimeError:
            writable = False
        if not writable:
            os.environ["HOME"] = tempfile.gettempdir()
        import bluesky as bs
        with contextlib.redirect_stdout(io.StringIO()):
            bs.init(mode="sim", detached=True)
            from bluesky import stack
            stack.stack("DT 1.0")
            stack.stack("ASAS OFF")
            bs.sim.step()
        _bs = bs
    return _bs


class AirState:
    """What the crew is doing, in the air."""
    __slots__ = ("mode", "route", "hold", "hold_phase", "hold_t", "hdg", "alt", "spd", "app_rwy", "established",
                 "on_gs", "fix", "gate", "exit_t", "deviating", "intercept_hdg", "missed_rwy", "hold_alt", "ra")

    def __init__(self):
        self.mode = "route"     # route, heading, hold, loc, sid, missed, divert, center
        self.route = []         # fixes still to fly
        self.hold = None        # hold dict when holding
        self.hold_phase = None
        self.hold_t = 0.0
        self.hdg = None         # assigned heading in heading mode
        self.alt = None         # cleared altitude, ft
        self.spd = None         # assigned speed, kt IAS (None: the crew's own speed)
        self.app_rwy = None     # cleared approach runway
        self.established = False
        self.on_gs = False
        self.fix = None
        self.gate = None
        self.exit_t = None
        self.deviating = None
        self.intercept_hdg = None
        self.missed_rwy = None
        self.hold_alt = None
        self.ra = None          # a TCAS resolution advisory being flown: (target alt ft, "climb"/"descend", until sim t)


WIND_EPOCH_S = 1800.0      # the wind aloft drifts to new offsets over this long


class Air:
    def __init__(self, fixes, runways, weather):
        self.bs = bluesky()
        self.traf = self.bs.traf
        # BlueSky is one simulator per process: a new day starts with no traffic and no wind. Run one World per
        # process at a time.
        while self.traf.ntraf > 0:
            self.traf.delete(self.traf.ntraf - 1)
        self.traf.wind.clear()
        self.fixes = fixes
        self.runways = runways
        self.weather = weather
        self.state = {}       # id -> AirState
        self.wind_set = None
        self._sel = {}        # id -> (hdg, alt, spd, vs) last set, to avoid redundant autopilot calls
        self.traits = {}      # id -> how this crew and aircraft fly (World.crew): reaction time, climb/descent rate...
        self._delay = {}      # id -> {what: (applied, pending, since)}: a crew acts a few seconds after it is told
        self._herr = {}       # id -> (assigned heading, the heading the crew actually flies)
        self.rngf = None      # World.rng, for the wind aloft and the crews' small errors

    # -- life cycle ---------------------------------------------------------------------------------------------
    def create(self, fid, actype, pos, hdg, alt_ft, cas_kt):
        self.traf.cre(fid, actype if actype else "B738", pos[0], pos[1], hdg, alt_ft * FT, cas_kt * KT)
        st = AirState()
        st.alt = alt_ft
        self.state[fid] = st
        self._sel[fid] = (None, None, None, None)
        return st

    def delete(self, fid):
        i = self.idx(fid)
        if i >= 0:
            self.traf.delete(i)
        self.state.pop(fid, None)
        self._sel.pop(fid, None)
        self._delay.pop(fid, None)
        self._herr.pop(fid, None)

    def set_traits(self, fid, c):
        """How this aircraft flies: its weight (BlueSky's performance model uses it) and its crew's habits."""
        self.traits[fid] = c
        i = self.idx(fid)
        if i >= 0 and hasattr(self.traf, "perf") and hasattr(self.traf.perf, "mass"):
            self.traf.perf.mass[i] = float(self.traf.perf.mass[i]) * c.get("mass", 1.0)

    def _late(self, fid, key, want, now, before=None):
        """A crew sets a new heading or altitude a few seconds after the readback, not at once. before: what the
        aircraft is doing now, when it was not flying this kind of selection a moment ago (a first vector)."""
        if want is None:
            return None
        react = self.traits.get(fid, {}).get("react_s", 0.0)
        d = self._delay.setdefault(fid, {})
        cur = d.get(key)
        if cur is None or now - cur[3] > 5.0:
            start = before if before is not None else want
            cur = (start, want, now, now)
        applied, pending, since, _ = cur
        if want != pending:
            pending, since = want, now
        if pending != applied and now - since >= react:
            applied = pending
        d[key] = (applied, pending, since, now)
        return applied

    def _flown_heading(self, fid, hdg):
        """Crews fly an assigned heading to within a few degrees, not exactly."""
        h = self._herr.get(fid)
        if h is None or h[0] != hdg:
            sd = self.traits.get(fid, {}).get("hdg_sd", 0.0)
            err = self.rngf("hdg", fid, round(hdg, 1)).gauss(0.0, sd) if (self.rngf and sd) else 0.0
            h = (hdg, (hdg + err) % 360.0)
            self._herr[fid] = h
        return h[1]

    def wind_here(self, fid):
        """The wind where the aircraft is (from, kt), as its crew would report it."""
        i = self.idx(fid)
        if i < 0:
            return None
        we, wn = float(self.traf.windeast[i]), float(self.traf.windnorth[i])
        kt = math.hypot(we, wn) / KT
        frm = (math.degrees(math.atan2(-we, -wn))) % 360.0
        return frm, kt

    def idx(self, fid):
        return self.traf.id2idx(fid)

    def exists(self, fid):
        return fid in self.state

    def snapshot(self, fid):
        i = self.idx(fid)
        if i < 0:
            return None
        t = self.traf
        return {"lat": float(t.lat[i]), "lon": float(t.lon[i]), "alt_ft": float(t.alt[i]) / FT, "hdg": float(t.hdg[i]),
                "trk": float(t.trk[i]), "cas_kt": float(t.cas[i]) / KT, "gs_kt": float(t.gs[i]) / KT,
                "vs_fpm": float(t.vs[i]) / FT * 60.0}

    def arrays(self):
        t = self.traf
        return list(t.id), np.array(t.lat), np.array(t.lon), np.array(t.alt) / FT, np.array(t.gs) / KT

    # -- wind ---------------------------------------------------------------------------------------------------
    def wind_error(self, now, alt_ft):
        """How the real wind aloft differs from the METAR-based profile: direction and speed offsets that drift
        slowly (new values every WIND_EPOCH_S, blended in between). Forecast winds at low levels are typically off by
        5 to 10 kt and 10 to 30 degrees; the surface wind (the METAR) is exact."""
        if alt_ft <= 0 or self.rngf is None:
            return 0.0, 0.0
        e = now / WIND_EPOCH_S
        k = int(e)
        fr = e - k
        scale = min(1.0, 0.4 + alt_ft / 10000.0)

        def at(epoch):
            r = self.rngf("wind", int(alt_ft), epoch)
            return r.gauss(0.0, 15.0) * scale, r.gauss(0.0, 7.0) * scale
        (d0, k0), (d1, k1) = at(k), at(k + 1)
        return d0 + (d1 - d0) * fr, k0 + (k1 - k0) * fr

    def set_wind(self, now):
        """Refresh BlueSky's wind field: the METAR's surface wind, veering and strengthening with height as the day's
        profile says, plus how the real wind aloft differs from that profile (wind_error)."""
        d0, k0 = self.weather.wind_at(now, 0)
        key = (round(d0), round(k0), int(now // 300))
        if key == self.wind_set:
            return
        self.wind_set = key
        w = self.traf.wind
        w.clear()
        lat, lon = 25.25, 55.36
        alts = [0, 1000, 3000, 6000, 10000, 20000, 30000]
        dirs, spds = [], []
        for a in alts:
            d, k = self.weather.wind_at(now, a)
            dd, dk = self.wind_error(now, a)
            dirs.append((d + dd) % 360.0)
            spds.append(max(0.0, k + dk) * KT)
        w.addpoint(lat, lon, np.array(dirs), np.array(spds), np.array(alts, dtype=float) * FT)

    # -- steering -----------------------------------------------------------------------------------------------
    def _set(self, fid, i, hdg=None, alt=None, spd=None, vs=None):
        last = self._sel[fid]
        ap = self.traf.ap
        h0, a0, s0, v0 = last
        if hdg is not None and (h0 is None or abs(angdiff(h0, hdg)) > 0.5):
            # hdg here is the track to make good: set the autopilot's selected track directly (what BlueSky's HDG
            # command does after its own wind correction), heading-select mode.
            ap.trk[i] = float(hdg) % 360.0
            self.traf.swlnav[i] = False
            h0 = hdg
        if alt is not None and vs is None and fid in self.traits:
            here = float(self.traf.alt[i]) / FT
            if abs(here - alt) > 300:
                tr = self.traits[fid]
                vs = tr.get("vs_clb", 2000.0) if alt > here else -tr.get("vs_desc", 1800.0)
        if alt is not None and (a0 is None or abs(a0 - alt) > 1 or vs != v0):
            if vs is not None:
                ap.selaltcmd(i, float(alt) * FT, float(vs) * FT / 60.0)
            else:
                ap.selaltcmd(i, float(alt) * FT)
            a0, v0 = alt, vs
        if spd is not None and (s0 is None or abs(s0 - spd) > 0.5):
            ap.selspdcmd(i, float(spd) * KT)
            s0 = spd
        self._sel[fid] = (h0, a0, s0, v0)

    def heading_for_track(self, i, trk):
        """Guidance works in tracks: the autopilot holds a selected track (see _set)."""
        return trk % 360.0

    def track_for_heading(self, i, hdg):
        """The track an aircraft makes good flying a heading in this wind."""
        tas = max(60.0, float(self.traf.tas[i]))
        we, wn = float(self.traf.windeast[i]), float(self.traf.windnorth[i])
        h = math.radians(hdg)
        e, n = tas * math.sin(h) + we, tas * math.cos(h) + wn
        return math.degrees(math.atan2(e, n)) % 360.0

    def steer(self, fid, f, now, default_spd):
        """One guidance update for an aircraft. Returns an event string or None (e.g. 'route_end', 'gate')."""
        st = self.state[fid]
        i = self.idx(fid)
        if i < 0:
            return None
        t = self.traf
        pos = (float(t.lat[i]), float(t.lon[i]))
        alt = float(t.alt[i]) / FT
        ev = None
        tr = self.traits.get(fid, {})
        spd = (st.spd + tr.get("spd_bias", 0.0)) if st.spd is not None else default_spd
        if st.mode == "loc" and st.app_rwy and st.spd is not None:
            # Assigned speeds end at 4 NM: the crew then slows to the final approach speed (AIP AD 2.22.7).
            if self.loc_coords(pos, self.runways[st.app_rwy])[0] < 4.0:
                spd = default_spd
        vs = None
        cmd_alt = self._late(fid, "alt", st.alt, now)
        if st.ra is not None and st.mode != "loc":
            cmd_alt = st.ra[0]   # the crew follows its TCAS resolution advisory, not its clearance, until clear of conflict
        if st.mode in ("route", "center", "divert", "sid") and st.route:
            fx = self.fixes[st.route[0]] if isinstance(st.route[0], str) else st.route[0]
            d = dist_nm(pos, fx)
            gs = max(100.0, float(t.gs[i]) / KT)
            turn_lead = min(2.0, gs / 3600.0 * 30.0)   # start the turn about 30 s early
            if d < max(0.6, turn_lead) and len(st.route) > 1:
                st.route.pop(0)
                fx = self.fixes[st.route[0]] if isinstance(st.route[0], str) else st.route[0]
            elif d < 1.0 and len(st.route) == 1:
                ev = "route_end"
            hdg = self.heading_for_track(i, bearing(pos, fx))
            if st.mode == "sid" and alt < 1500 and st.gate is not None:
                hdg = self.heading_for_track(i, self.runways[st.missed_rwy].heading) if st.missed_rwy else hdg
            self._set(fid, i, hdg=hdg, alt=cmd_alt, spd=spd)
        elif st.mode in ("route", "center", "divert", "sid"):
            self._set(fid, i, alt=cmd_alt, spd=spd)
        elif st.mode == "heading":
            want = self._flown_heading(fid, st.hdg) if st.hdg is not None else None
            h = self._late(fid, "hdg", want, now, before=float(t.hdg[i]))
            self._set(fid, i, hdg=self.track_for_heading(i, h if h is not None else want), alt=cmd_alt, spd=spd)
        elif st.mode == "hold":
            ev = self._hold(fid, i, pos, st, now, cmd_alt, spd)
        elif st.mode == "missed":
            rw = self.runways[st.missed_rwy]
            if alt < 2900:
                self._set(fid, i, hdg=self.heading_for_track(i, rw.heading), alt=cmd_alt or 3000, spd=max(spd, 200))
            else:
                st.mode = "route"
                st.route = [st.fix]
                ev = "missed_turn"
        # Approach arming and tracking.
        if st.app_rwy and st.mode != "loc":
            if self._try_capture(fid, i, pos, st):
                ev = "established"
        if st.mode == "loc":
            ev = self._track_loc(fid, i, pos, alt, st, spd) or ev
        return ev

    def _hold(self, fid, i, pos, st, now, alt, spd):
        h = st.hold
        fx = self.fixes[h["fix"]]
        inbound = h["inbound"] if h["inbound"] is not None else bearing(pos, fx)
        if st.hold_phase in (None, "to_fix", "inbound"):
            hdg = self.heading_for_track(i, bearing(pos, fx))
            if dist_nm(pos, fx) < 0.8:
                st.hold_phase, st.hold_t = "turn_out", now
            self._set(fid, i, hdg=hdg, alt=alt, spd=min(spd, 230 if (alt or 0) <= 14000 else 240))
        else:
            # The published turn direction: every turn in the hold goes that way. Each 180 degree turn is flown as two
            # 90 degree steps, so the autopilot never takes the short way round.
            d = -1.0 if h.get("turn") == "L" else 1.0
            out = (inbound + 180.0) % 360.0
            trk = float(self.traf.trk[i])
            if st.hold_phase == "turn_out":
                step = (inbound + d * 90.0) % 360.0
                if abs(angdiff(trk, step)) < 20.0:
                    st.hold_phase = "outbound"
                self._set(fid, i, hdg=self.heading_for_track(i, step), alt=alt, spd=min(spd, 230))
            elif st.hold_phase == "outbound":
                self._set(fid, i, hdg=self.heading_for_track(i, out), alt=alt, spd=min(spd, 230))
                if now - st.hold_t > 60.0 + h["leg_min"] * 60.0:
                    st.hold_phase = "turn_in"
            else:   # turn_in
                step = (out + d * 90.0) % 360.0
                if abs(angdiff(trk, step)) < 20.0:
                    st.hold_phase = "inbound"
                self._set(fid, i, hdg=self.heading_for_track(i, step), alt=alt, spd=min(spd, 230))
        return None

    def _try_capture(self, fid, i, pos, st):
        rw = self.runways[st.app_rwy]
        along, cross = self.loc_coords(pos, rw)
        trk = float(self.traf.trk[i])
        d_ang = abs(angdiff(trk, rw.heading))
        # Flying to a point on this final (IF or a final fix): turn onto the localizer on reaching it.
        if st.mode == "route" and len(st.route) == 1 and isinstance(st.route[0], str):
            from .airport import final_fix_along
            if final_fix_along(st.route[0]) is not None and (st.route[0][-2:] if st.route[0].startswith("IF") else st.route[0][:2]) == st.app_rwy[:2] and abs(cross) < 1.0 and 3.0 < along < 30.0:
                st.mode = "loc"
                st.established = abs(cross) < 0.25
                return True
        if 3.0 < along < 30.0 and abs(cross) < 3.0 and d_ang < 90.0:
            # Converging or already close to the centreline: capture.
            if abs(cross) < 0.25 or (d_ang > 10 and (cross > 0) == (angdiff(rw.heading, trk) < 0)):
                st.mode = "loc"
                st.established = abs(cross) < 0.25
                return True
        return False

    @staticmethod
    def loc_coords(pos, rw):
        """(distance to the threshold along the final, cross-track) in NM. cross > 0: right of the centreline as
        seen by the landing aircraft."""
        d = dist_nm(rw.thr, pos)
        b = bearing(rw.thr, pos)
        recip = (rw.heading + 180.0) % 360.0
        a = math.radians(angdiff(recip, b))
        return d * math.cos(a), -d * math.sin(a)

    def _track_loc(self, fid, i, pos, alt, st, spd):
        rw = self.runways[st.app_rwy]
        along, cross = self.loc_coords(pos, rw)
        corr = max(-30.0, min(30.0, cross * 60.0))
        trk = (rw.heading - corr) % 360.0
        if abs(cross) < 0.2:
            st.established = True
        gs_alt = along * FT_PER_NM_GS + 62
        gs_kt = max(100.0, float(self.traf.gs[i]) / KT)
        if st.established and gs_alt - 150 <= alt <= gs_alt + 150:
            st.on_gs = True
        if st.on_gs:
            vs = -gs_kt * 101.27 * GS_TAN * 1.0  # fpm for a 3 degree path at this ground speed
            err = alt - gs_alt
            vs -= max(-400.0, min(400.0, err * 2.0))
            self._set(fid, i, hdg=self.heading_for_track(i, trk), alt=max(0.0, gs_alt - 600), spd=spd, vs=vs)
        else:
            self._set(fid, i, hdg=self.heading_for_track(i, trk), alt=st.alt, spd=spd)
        if along <= 0.05:
            return "threshold"
        return None

    def step(self, n=1):
        for _ in range(n):
            self.bs.sim.step()
