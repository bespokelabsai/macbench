"""Deterministic flight simulation of the whole fleet.

All drones are simulated jointly in time order, because pad slots and
separation couple them. A priority queue holds each drone's next update as
``(time, priority, drone index)``.

How the rules are applied (where the rules leave a choice, the most
literal reading was taken; these notes are part of the checker's spec):

* **Start.** Every drone starts landed on its start pad, full, at t = 0,
  occupying a slot. A drone with rows takes off at t = 0 toward row 1 (no
  ``depart_after`` applies to the start pad). To start later, make row 1 a
  ``pad`` row at the start pad: the drone takes off, lands again at once
  (zero-length leg) and then waits for that row's ``depart_after``.
* **Integration steps.** A leg is integrated from its start in steps of
  ``time_step_s``; a step ends early at the leg end, at the end of a wait, at
  ``day_length_s``, or at the instant the battery reaches 0. Wind and power are
  taken at the start of each step (explicit Euler); ground speed is constant
  within a step. Hovering (waiting, photo hover, pad queue, after the plan ends)
  is integrated the same way with power P(|w(z, t)|).
* **Ground speed.** ``no_progress`` is decided on the uncapped ground speed g
  (sqrt argument < 0 or g < 0.5 m/s) at each step start. If the vertical rate
  needed to stay on the line exceeds the climb or descent limit, the rate is
  capped and g lowered; a capped g below 0.5 m/s is not a loss. Pure vertical
  legs (horizontal length < 0.01 m) move at the vertical rate limit with
  V = |w|; their xy moves linearly with z. A leg of zero length takes no time.
* **Leg start.** At each leg start the drone is first lost if t >= day_length
  (``day_end``), then if the leg's segment breaks building clearance
  (``building``, lost at the leg start time), then the first step may be
  ``no_progress``.
* **Samples and checks.** Every step end is an integration sample. At each
  sample of a leg the altitude rule is checked. The no-fly rule is checked on
  the whole straight path of every airborne step (flying or hovering): for each
  active interval of a zone that overlaps the step, the part of the step flown
  during it is tested against the polygon (boundary counts as inside, active
  intervals are inclusive); a hit loses the drone at the end of that step (the
  sample). Within one step the battery crossing
  (exact time) comes first, then altitude, then no-fly at the step end. Pad
  zones are inclusive (horizontal distance <= radius). Pad rows keep the row's
  own coordinates.
* **Battery.** Lost (``battery``) at the exact time it reaches 0 while
  airborne, including when that is the moment of arrival.
* **Pads.** On arrival at a pad row the drone lands if a slot is free, else it
  hovers at the waypoint and joins the pad's FIFO queue (arrival time, then
  drone id). A departure frees its slot at the departure time and the queue
  head lands at that instant. Battery charges at battery_J / charge_time_full_s
  up to full; the drone leaves at the first t with t >= depart_after,
  battery >= soc * battery_J and t >= landing time + ``min_turnaround_s``
  The start of the day is not a landing: neither the
  t = 0 takeoff nor the takeoff after a row-1 pad row at the start pad (the
  zero-length "landing" at t = 0) is delayed by it. A departure that would happen at t >= day_length
  does not happen (the drone stays landed and is not lost).
* **Equal times.** Pad departures (slot releases) are processed first, then
  drone updates in drone id order, then the separation check for that integer
  second.
* **Separation.** At each integer second k the whole second (k - 1, k] is
  checked. Each drone's path is linear between its integration samples. A drone
  counts while it flies a leg or hovers (not landed, queued or lost) and is not
  inside a pad zone. A pair whose exact closest approach, over the moments of
  the second when both count, is < separation_m (3D) is in conflict; this
  includes the point check at k. All drones in conflicts are lost together at
  k (the first integer second at or after the violation). A drone lost earlier
  in that second for another reason keeps that loss but still counts up to its
  loss time, so its partner is lost at k.
* **Photos.** Photo time tp = max(arrival, depart_after) + hover_s, taken from
  the waypoint. Validity per MANUAL section 7 (``geometry.viewpoint_reason``). A valid
  photo is banked at the drone's next landing; on loss its unbanked photos are
  voided. Points, the light factor of static photos, the quality factor Q
  and the sync rule for moving photos are applied after the flight
  (``scoring.score_photos``), at the same tp.
* **Day end.** A drone airborne at t = day_length (flying, hovering, queued,
  or after its plan ends in the air) is lost with reason ``day_end``.
"""

from __future__ import annotations

import heapq
import math

from .energy import PowerModel
from .geometry import blocking_buildings, segment_polygon_entry, viewpoint_reason

PAD, LEG, HOVER, QUEUE, LOST = "pad", "leg", "hover", "queue", "lost"
INF = float("inf")


class _Drone:
    """Mutable per-drone simulation state."""

    __slots__ = (
        "idx", "id", "rows", "i", "phase", "t", "x", "y", "z", "E", "energy", "pad",
        "t_land", "E_land", "ver", "pend",
        # leg
        "p0", "q", "Lh", "dhx", "dhy", "dz", "s", "vertical", "speed",
        # hover
        "hover_end", "hover_kind",
        # bookkeeping
        "unbanked", "samples", "lost_reason", "lost_t", "photos_taken", "photos_banked",
        # mapping and shows: unbanked scans and show holds, scan counts, memory card, captures per sortie
        "unb_scans", "unb_holds", "scans_taken", "scans_banked", "card", "sorties",
        # separation: path points (t, x, y, z, counts) since the last check, and when it last stopped counting
        "trk", "air_end",
    )

    def __init__(self, idx, did, rows):
        self.idx, self.id, self.rows = idx, did, rows
        self.i = -1
        self.phase = PAD
        self.ver = 0
        self.pend = None
        self.unbanked = []
        self.samples = []
        self.lost_reason = None
        self.lost_t = None
        self.photos_taken = 0
        self.photos_banked = 0
        self.unb_scans = []
        self.unb_holds = []
        self.scans_taken = 0
        self.scans_banked = 0
        self.card = 0
        self.sorties = []  # per sortie (takeoff to landing): [captures recorded, captures card_full]
        self.energy = 0.0
        self.trk = []
        self.air_end = -INF


class Simulator:
    """Runs one day. Use :func:`simulate` rather than this class directly."""

    def __init__(self, world, plans, wind, log=True):
        self.w = world
        self.r = r = world.rules
        self.wind = wind
        self.log = log
        self.pm = PowerModel(r)
        self.B = r.battery_J
        self.rate = r.charge_rate_Jps
        self.day_end = r.day_length_s
        self.step = r.time_step_s
        self.drones = [_Drone(k, d, list(plans.get(d, []) or [])) for k, d in enumerate(r.fleet)]
        self.occupants = {p: [] for p in world.pads}
        self.queues = {p: [] for p in world.pads}
        self.heap = []
        self.events = []
        self.photos = []
        self.scans = []
        self.holds = []
        self.card_cap = r.card_capacity
        # noise usage U(zone, hour), counted per integer second while airborne (see _noise_span)
        self.noise_zones = list(world.noise_zones)
        self.noise_alt = world.noise_alt_m
        self.noise_usage = {nz.id: [0] * 12 for nz in self.noise_zones}
        self.next_k = 0
        for d in self.drones:
            pad = world.pads[world.start[d.id]]
            d.x, d.y, d.z = pad.pos
            d.E = self.B
            d.t = 0.0
            d.pad = pad.id
            d.t_land, d.E_land = 0.0, self.B
            d.trk.append((0.0, d.x, d.y, d.z, False))
            self.occupants[pad.id].append(d.idx)
            self._sample(d, 0.0, PAD)
            if d.rows:
                heapq.heappush(self.heap, (0.0, 0, d.idx, d.ver))
        for p, occ in self.occupants.items():
            if len(occ) > r.slots_per_pad:
                raise ValueError(f"pads.json: more than {r.slots_per_pad} drones start on pad {p}")

    # ------------------------------------------------------------------ utils
    def _event(self, t, d, typ, detail=""):
        self.events.append({"t": t, "drone": d.id, "type": typ, "detail": detail})

    def _sample(self, d, t, state):
        if self.log:
            d.samples.append((t, d.x, d.y, d.z, d.E, state))

    @staticmethod
    def _mark(d, t, counts):
        """Add a path point at time t; ``counts`` says whether the drone counts for separation from t on."""
        trk = d.trk
        if not counts and trk[-1][4]:
            d.air_end = t
        trk.append((t, d.x, d.y, d.z, counts))

    def _push(self, d, t, prio=1):
        heapq.heappush(self.heap, (t, prio, d.idx, d.ver))

    # ------------------------------------------------------------------- main
    def run(self):
        heap, drones, day_end = self.heap, self.drones, self.day_end
        while True:
            t_top = heap[0][0] if heap else INF
            k = self.next_k
            if k < t_top and k <= day_end:
                # Separation for the second (k - 1, k]: drones flying or hovering now, or that stopped
                # (landed, queued, lost) inside that second.
                lo = k - 1
                n = 0
                for d in drones:
                    if d.phase is LEG or d.phase is HOVER or d.air_end > lo:
                        n += 1
                if n >= 2:
                    self._separation(float(k))
                    self.next_k = k + 1
                else:
                    # Nothing changes before t_top, so at most one drone counts until then.
                    for d in drones:
                        if len(d.trk) > 1:
                            d.trk = d.trk[-1:]
                    if not heap:
                        break
                    self.next_k = max(k + 1, math.ceil(t_top))
                continue
            if not heap:
                break
            t, prio, idx, ver = heapq.heappop(heap)
            d = drones[idx]
            if ver != d.ver or d.phase is LOST:
                continue
            if prio == 0:
                self._depart(d, t)
            else:
                self._commit(d, t)
        for d in drones:
            if d.phase is PAD and self.log:
                t_end = self.day_end
                if d.samples[-1][0] < t_end:
                    E = min(self.B, d.E_land + self.rate * (t_end - d.t_land))
                    d.samples.append((t_end, d.x, d.y, d.z, E, PAD))
        return self

    # -------------------------------------------------------------- stepping
    def _plan_step(self, d):
        """Compute the drone's next integration step from its committed state and queue it."""
        t = d.t
        self._mark(d, t, d.phase is LEG or d.phase is HOVER)
        if t >= self.day_end:
            self._lose(d, t, "day_end")
            return
        t1 = t + self.step
        if t1 > self.day_end:
            t1 = self.day_end
        pm = self.pm
        if d.phase is LEG:
            wu, wv = self.wind.at(d.z, t)
            if d.vertical:
                up = d.dz > 0
                vz = self.r.climb_rate_max_mps if up else self.r.descent_rate_max_mps
                power = pm.power(math.hypot(wu, wv)) + (pm.climb_power(vz) if up else 0.0)
                t_end = t + max(0.0, abs(d.dz) - d.s) / vz
                rate = vz
            else:
                wd = wu * d.dhx + wv * d.dhy
                a = d.speed
                arg = a * a - (wu * wu + wv * wv) + wd * wd
                if arg < 0.0:
                    self._lose(d, t, "no_progress", f"wind {math.hypot(wu, wv):.2f} m/s")
                    return
                g = wd + math.sqrt(arg)
                if g < 0.5:
                    self._lose(d, t, "no_progress", f"ground speed {g:.3f} m/s")
                    return
                vz = 0.0
                if d.dz != 0.0:
                    vz = g * abs(d.dz) / d.Lh
                    lim = self.r.climb_rate_max_mps if d.dz > 0 else self.r.descent_rate_max_mps
                    if vz > lim:
                        vz = lim
                        g = vz * d.Lh / abs(d.dz)
                V = math.hypot(g * d.dhx - wu, g * d.dhy - wv)
                power = pm.power(V) + (pm.climb_power(vz) if d.dz > 0 else 0.0)
                t_end = t + max(0.0, d.Lh - d.s) / g
                rate = g
            done = t_end <= t1
            if done:
                t1 = t_end
        else:
            power = pm.power(self.wind.speed(d.z, t))
            rate = 0.0
            done = False
            if d.hover_end <= t1:
                t1 = d.hover_end
                done = True
        E1 = d.E - power * (t1 - t)
        loss = None
        if E1 <= 0.0:
            tx = t + d.E / power
            if tx < t1:
                t1, done = tx, False
            E1, loss = 0.0, "battery"
        d.pend = (t, t1, power, rate, done, loss)
        self._push(d, t1)

    def _position_at_progress(self, d, s):
        """Position along the current leg after progress s (horizontal metres, or |dz| metres if vertical)."""
        p0, q = d.p0, d.q
        total = abs(d.dz) if d.vertical else d.Lh
        f = s / total if total > 0.0 else 1.0
        return p0[0] + (q[0] - p0[0]) * f, p0[1] + (q[1] - p0[1]) * f, p0[2] + (q[2] - p0[2]) * f

    def _advance_to(self, d, t):
        """Move the drone's committed state along its pending step to time t (no checks)."""
        t0, t1, power, rate, done, loss = d.pend
        dt = t - t0
        if dt <= 0.0:
            return
        if self.noise_zones and math.ceil(t0) < t:
            self._noise_span(d, t0, t, power, rate, done, t1)
        if d.phase is LEG:
            if done and t >= t1:
                d.s = abs(d.dz) if d.vertical else d.Lh
                d.x, d.y, d.z = d.q
            else:
                d.s += rate * dt
                d.x, d.y, d.z = self._position_at_progress(d, d.s)
        d.E -= power * dt
        d.energy += power * dt
        d.t = t

    def _commit(self, d, t):
        """Apply the pending step ending at t, run the sample checks, then the next transition."""
        pend = d.pend
        if pend is None:  # leg start right after a takeoff
            self._run(d, t, "next_leg")
            return
        loss, done = pend[5], pend[4]
        t0, x0, y0 = d.t, d.x, d.y
        self._advance_to(d, t)
        if loss is not None:
            d.E = 0.0
            self._lose(d, t, loss)
            return
        d.pend = None
        self._sample(d, t, "air")
        r = self.r
        if d.phase is LEG:
            z = d.z
            if z > r.alt_max_m or (z < r.alt_min_m and not (z >= 0.0 and self.w.in_pad_zone(d.x, d.y))):
                self._lose(d, t, "altitude", f"z = {z:.3f} m")
                return
        if self.w.nofly:
            zone = self._nofly_path(t0, x0, y0, t, d.x, d.y)
            if zone is not None:
                self._lose(d, t, "nofly", f"zone {zone.id}")
                return
        if not done:
            self._plan_step(d)
        elif d.phase is LEG:
            self._run(d, t, "arrive")
        elif d.hover_kind == "photo":
            self._run(d, t, "photo")
        elif d.hover_kind == "scan":
            self._run(d, t, "scan")
        else:  # end of a fly-row wait
            self._run(d, t, "next_leg")

    # ----------------------------------------------------------- transitions
    def _run(self, d, t, action):
        """Process zero-duration transitions at time t until the drone needs time to pass."""
        rows = d.rows
        while True:
            if action == "next_leg":
                d.i += 1
                if d.i >= len(rows):
                    d.phase, d.hover_end, d.hover_kind = HOVER, INF, "hold"
                    self._plan_step(d)
                    return
                if t >= self.day_end:
                    self._lose(d, t, "day_end")
                    return
                row = rows[d.i]
                p0, q = (d.x, d.y, d.z), (row.x, row.y, row.z)
                hit = blocking_buildings(self.w, p0, q, grown=True)
                if hit:
                    self._lose(d, t, "building", f"leg to row {row.line or d.i + 1} hits building {hit[0]}")
                    return
                dx, dy, dz = q[0] - p0[0], q[1] - p0[1], q[2] - p0[2]
                Lh = math.hypot(dx, dy)
                d.phase, d.p0, d.q, d.dz, d.s, d.speed = LEG, p0, q, dz, 0.0, row.speed
                d.Lh = Lh
                d.vertical = Lh < 0.01
                if not d.vertical:
                    d.dhx, d.dhy = dx / Lh, dy / Lh
                elif dz == 0.0:
                    self._mark(d, t, True)  # keep the separation path exact across the (< 1 cm) jump
                    d.x, d.y, d.z = q
                    # A zero-length leg takes no time, but the drone is still at a new point: apply the same
                    # altitude and no-fly checks a sample would (otherwise chained tiny legs skip them).
                    r = self.r
                    if d.z > r.alt_max_m or (d.z < r.alt_min_m and not (d.z >= 0.0 and self.w.in_pad_zone(d.x, d.y))):
                        self._lose(d, t, "altitude", f"z = {d.z:.3f} m")
                        return
                    for zone in self.w.nofly:
                        if zone.active_at(t) and zone.contains_xy(d.x, d.y):
                            self._lose(d, t, "nofly", f"zone {zone.id}")
                            return
                    action = "arrive"
                    continue
                self._plan_step(d)
                return
            if action == "arrive":
                row = rows[d.i]
                if row.action == "fly":
                    if row.depart_after > t:
                        d.phase, d.hover_end, d.hover_kind = HOVER, row.depart_after, "fly"
                        self._plan_step(d)
                        return
                    action = "next_leg"
                    continue
                if row.action == "photo":
                    tp = max(t, row.depart_after) + self.r.photo_hover_s
                    if tp > t:
                        d.phase, d.hover_end, d.hover_kind = HOVER, tp, "photo"
                        self._plan_step(d)
                        return
                    action = "photo"
                    continue
                if row.action == "scan":
                    ts = max(t, row.depart_after) + self.r.scan_hover_s
                    if ts > t:
                        d.phase, d.hover_end, d.hover_kind = HOVER, ts, "scan"
                        self._plan_step(d)
                        return
                    action = "scan"
                    continue
                if row.action == "show":
                    t_end = self._arrive_show(d, t, row)
                    if t_end > t:
                        d.phase, d.hover_end, d.hover_kind = HOVER, t_end, "show"
                        self._plan_step(d)
                        return
                    action = "next_leg"
                    continue
                # pad row
                if len(self.occupants[row.arg]) < self.r.slots_per_pad:
                    self._land(d, t, row.arg)
                else:
                    self.queues[row.arg].append((t, d.idx))
                    self.queues[row.arg].sort()
                    d.phase, d.pad, d.hover_end, d.hover_kind = QUEUE, row.arg, INF, "queue"
                    self._event(t, d, "queue", row.arg)
                    self._plan_step(d)
                return
            if action == "photo":
                self._take_photo(d, t, rows[d.i])
                action = "next_leg"
                continue
            if action == "scan":
                self._take_scan(d, t, rows[d.i])
                action = "next_leg"
                continue
            raise AssertionError(action)

    def _card(self, d):
        """Count one capture (photo or scan) on the current sortie's card; False if the card is full."""
        if not d.sorties:  # cannot happen (a capture needs a takeoff), kept for safety
            d.sorties.append([0, 0])
        d.card += 1
        if d.card > self.card_cap:
            d.sorties[-1][1] += 1
            return False
        d.sorties[-1][0] += 1
        return True

    def _take_scan(self, d, t, row):
        pos = (d.x, d.y, d.z)
        rec = self._card(d)
        sc = {"t": t, "drone": d.id, "landmark": row.arg, "pos": list(pos), "banked": False,
              "reason": None if rec else "card_full"}
        self.scans.append(sc)
        d.scans_taken += 1
        if rec:
            d.unb_scans.append(sc)
            self._event(t, d, "scan", row.arg)
        else:
            self._event(t, d, "scan", f"{row.arg} card_full")

    def _arrive_show(self, d, t, row):
        """Record a show hold on arrival at a show row; return the time the drone may leave."""
        sid, _, slot = row.arg.rpartition(":")
        sh = self.w.shows[sid]
        t_hold_end = sh.t0 + sh.hold_s
        late = t > sh.t0 + 1e-9
        h = {"show": sid, "slot": int(slot), "drone": d.id, "t_arrive": t, "late": late, "t_hold_end": t_hold_end,
             "pos": [d.x, d.y, d.z], "held": True, "banked": False, "reason": None}
        self.holds.append(h)
        d.unb_holds.append(h)
        self._event(t, d, "show", row.arg + (" late" if late else ""))
        return max(t_hold_end, row.depart_after)

    def _noise_span(self, d, t0, t1, power, rate, done, t_step_end):
        """Add the integer seconds k with t0 <= k < t1 to the noise usage.

        The drone is airborne over the whole span (the caller advances only airborne drones). Its
        position at k is linear along the step. The sample at t1 itself is counted by the next span
        only if the drone is still airborne then, so a drone landing or lost at t1 does not count there.
        """
        x0, y0, z0 = d.x, d.y, d.z
        leg = d.phase is LEG
        alt = self.noise_alt
        U = self.noise_usage
        k = math.ceil(t0)
        while k < t1:
            if leg:
                if done and k >= t_step_end:
                    x, y, z = d.q
                else:
                    x, y, z = self._position_at_progress(d, d.s + rate * (k - t0))
            else:
                x, y, z = x0, y0, z0
            if z < alt:
                h = int(k // 3600)
                if h < 12:
                    for nz in self.noise_zones:
                        if nz.contains_xy(x, y):
                            U[nz.id][h] += 1
            k += 1

    def _take_photo(self, d, t, row):
        pos = (d.x, d.y, d.z)
        if not self._card(d):
            ph = {"t": t, "drone": d.id, "target": row.arg, "valid": False, "reason": "card_full",
                  "banked": False, "accepted": False, "value": 0.0, "pos": list(pos)}
            self.photos.append(ph)
            d.photos_taken += 1
            self._event(t, d, "photo", f"{row.arg} card_full")
            return
        reason = viewpoint_reason(self.w, row.arg, pos, t)
        ph = {"t": t, "drone": d.id, "target": row.arg, "valid": reason is None, "reason": reason,
              "banked": False, "accepted": False, "value": 0.0, "pos": list(pos)}
        self.photos.append(ph)
        d.photos_taken += 1
        if reason is None:
            d.unbanked.append(ph)
            self._event(t, d, "photo", row.arg)
        else:
            self._event(t, d, "photo", f"{row.arg} invalid: {reason}")

    def _land(self, d, t, pad_id):
        self.occupants[pad_id].append(d.idx)
        d.phase, d.pad, d.pend = PAD, pad_id, None
        d.ver += 1
        d.t = t
        d.t_land, d.E_land = t, d.E
        self._mark(d, t, False)
        for ph in d.unbanked:
            ph["banked"] = True
        d.photos_banked += len(d.unbanked)
        d.unbanked = []
        for sc in d.unb_scans:
            sc["banked"] = True
        d.scans_banked += len(d.unb_scans)
        d.unb_scans = []
        for h in d.unb_holds:
            h["banked"] = True
        d.unb_holds = []
        d.card = 0
        self._event(t, d, "land", pad_id)
        self._sample(d, t, PAD)
        row = d.rows[d.i]
        t_full = t + max(0.0, self.B - d.E) / self.rate
        if d.i + 1 < len(d.rows):
            need = row.soc * self.B
            # at least min_turnaround_s on the pad after every landing (charging meanwhile). The
            # t = 0 start is not a landing: a row-1 pad row at the start pad (zero-length leg, "land" at t = 0) is
            # exempt, so the first takeoff of the day is unaffected.
            turn = self.r.min_turnaround_s if t > 0.0 else 0.0
            t_leave = max(t + turn, row.depart_after, t + max(0.0, need - d.E) / self.rate)
        else:
            t_leave = INF
        if self.log and t < t_full < min(t_leave, self.day_end):
            d.samples.append((t_full, d.x, d.y, d.z, self.B, PAD))
        if t_leave < self.day_end:
            self._push(d, t_leave, prio=0)

    def _depart(self, d, t):
        """Take off from the current pad at time t; the slot is freed and the queue head lands."""
        d.E = min(self.B, d.E_land + self.rate * (t - d.t_land))
        d.t = t
        self._sample(d, t, PAD)
        pad_id = d.pad
        d.sorties.append([0, 0])
        self.occupants[pad_id].remove(d.idx)
        self._event(t, d, "takeoff", pad_id)
        d.phase, d.pad, d.pend = LEG, None, None
        d.ver += 1
        self._push(d, t, prio=1)  # the leg itself starts in the drone-update pass at time t
        self._grant(pad_id, t)

    def _grant(self, pad_id, t):
        """Give free slots on pad_id to queued drones, in queue order, at time t."""
        queue = self.queues[pad_id]
        while queue and len(self.occupants[pad_id]) < self.r.slots_per_pad:
            _, j = queue.pop(0)
            q = self.drones[j]
            if q.pend is not None:
                self._advance_to(q, t)
            if q.E <= 0.0:
                self._lose(q, t, "battery")
                continue
            self._land(q, t, pad_id)

    def _lose(self, d, t, reason, text=""):
        """Mark the drone lost at time t (interpolating along a pending step) and void its unbanked photos."""
        if d.pend is not None and t > d.t:
            self._advance_to(d, t)
        d.pend = None
        if d.phase is QUEUE:
            self.queues[d.pad] = [e for e in self.queues[d.pad] if e[1] != d.idx]
        d.phase = LOST
        d.ver += 1
        d.lost_reason, d.lost_t = reason, t
        self._mark(d, t, False)
        for ph in d.unbanked:
            ph["reason"] = "void"
        d.unbanked = []
        for sc in d.unb_scans:
            sc["reason"] = "void"
        d.unb_scans = []
        for h in d.unb_holds:
            h["held"] = t >= h["t_hold_end"]
        d.unb_holds = []
        self._event(t, d, "lost", f"{reason}: {text}" if text else reason)
        self._sample(d, t, LOST)

    def _position_at(self, d, t):
        """Position at time t, linear between the last committed sample and the pending one."""
        if d.pend is None or t <= d.t:
            return d.x, d.y, d.z
        t0, t1, power, rate, done, loss = d.pend
        if d.phase is LEG:
            if done and t >= t1:
                return d.q
            return self._position_at_progress(d, d.s + rate * (t - t0))
        return d.x, d.y, d.z

    def _nofly_path(self, t0, x0, y0, t1, x1, y1):
        """The zone the step's straight path (t0, x0, y0) -> (t1, x1, y1) touches while active, or None.

        Each active interval is intersected with [t0, t1] and the part of the path flown in it is tested
        against the polygon (boundary counts as inside). When several zones are touched, the one entered
        first wins (ties: file order).
        """
        best = None
        dt = t1 - t0
        lo_x, hi_x = (x0, x1) if x0 <= x1 else (x1, x0)
        lo_y, hi_y = (y0, y1) if y0 <= y1 else (y1, y0)
        for zone in self.w.nofly:
            b = zone.bounds
            if hi_x < b[0] or lo_x > b[2] or hi_y < b[1] or lo_y > b[3]:
                continue
            for a, c in zone.active:
                if c < t0 or a > t1:
                    continue
                if dt > 0.0:
                    ta, tb = (t0, t1) if a <= t0 else (a, t1)
                    if c < t1:
                        tb = c
                    fa, fb = (ta - t0) / dt, (tb - t0) / dt
                else:
                    ta = tb = t1
                    fa = fb = 1.0
                xa, ya = (x0, y0) if fa == 0.0 else (x0 + (x1 - x0) * fa, y0 + (y1 - y0) * fa)
                xb, yb = (x1, y1) if fb == 1.0 else (x0 + (x1 - x0) * fb, y0 + (y1 - y0) * fb)
                s = segment_polygon_entry(zone.polygon, xa, ya, xb, yb)
                if s is not None:
                    te = ta + (tb - ta) * s
                    if best is None or te < best[0]:
                        best = (te, zone)
        return None if best is None else best[1]

    def _separation(self, k):
        """Separation check for the second (k - 1, k]; every pair in conflict is lost at k.

        Each drone's path in the second is piecewise linear between its integration samples. A drone counts
        while flying a leg or hovering (not landed, queued or lost) and while outside every pad zone. Two
        drones conflict if, at some moment of the second when both count, their 3D distance is strictly less
        than separation_m (the exact closest approach of the linear pieces). The point check at k itself
        is included. A drone already lost earlier in the second keeps that loss.
        """
        sep = self.r.separation_m
        sep2 = sep * sep
        lo = k - 1.0
        cands = []
        for d in self.drones:
            cur = d.phase is LEG or d.phase is HOVER
            trk = d.trk
            if not cur and not d.air_end > lo:
                if len(trk) > 1:
                    d.trk = trk[-1:]
                continue
            i = len(trk) - 1
            while i > 0 and trk[i][0] > lo:
                i -= 1
            pts = trk[i:]
            if cur:
                p = self._position_at(d, k)
                end = (k, p[0], p[1], p[2], True)
                pts.append(end)
                d.trk = [end]
            else:
                end = pts[-1]
                p = (end[1], end[2], end[3])
                d.trk = [end]
            # Two cheap lower bounds on the pair distance, used before the exact test:
            # r = how far the drone gets from p (its position at k) during the second, and
            # dev = how far it gets from the straight constant-speed chord ps (at lo) -> p (at k).
            cx, cy, cz = p
            q0 = pts[0]
            if q0[0] < lo and len(pts) > 1:
                q1 = pts[1]
                f = (lo - q0[0]) / (q1[0] - q0[0]) if q1[0] > q0[0] else 1.0
                sx, sy, sz = q0[1] + (q1[1] - q0[1]) * f, q0[2] + (q1[2] - q0[2]) * f, q0[3] + (q1[3] - q0[3]) * f
            else:
                sx, sy, sz = q0[1], q0[2], q0[3]
            ux, uy, uz = cx - sx, cy - sy, cz - sz
            r2 = dev2 = 0.0
            for q in pts:
                dx, dy, dz = q[1] - cx, q[2] - cy, q[3] - cz
                v = dx * dx + dy * dy + dz * dz
                if v > r2:
                    r2 = v
                f = q[0] - lo
                if f > 0.0:
                    dx, dy, dz = q[1] - sx - ux * f, q[2] - sy - uy * f, q[3] - sz - uz * f
                    v = dx * dx + dy * dy + dz * dz
                    if v > dev2:
                        dev2 = v
            cands.append([d, cur, p, math.sqrt(r2), pts, None, (sx, sy, sz), math.sqrt(dev2)])
        if len(cands) < 2:
            return
        hit = {}
        for a in range(len(cands)):
            ca = cands[a]
            pa, ra = ca[2], ca[3]
            for b in range(a + 1, len(cands)):
                cb = cands[b]
                pb = cb[2]
                dx, dy, dz = pa[0] - pb[0], pa[1] - pb[1], pa[2] - pb[2]
                d2 = dx * dx + dy * dy + dz * dz
                lim = sep + ra + cb[3]
                if d2 >= lim * lim:
                    continue
                # closest approach of the two chords, less both deviations
                sa_, sb_ = ca[6], cb[6]
                rx, ry, rz = sa_[0] - sb_[0], sa_[1] - sb_[1], sa_[2] - sb_[2]
                ex, ey, ez = dx - rx, dy - ry, dz - rz
                ee = ex * ex + ey * ey + ez * ez
                f = -(rx * ex + ry * ey + rz * ez) / ee if ee > 0.0 else 0.0
                f = 0.0 if f < 0.0 else (1.0 if f > 1.0 else f)
                mx, my, mz = rx + ex * f, ry + ey * f, rz + ez * f
                lim = sep + ca[7] + cb[7] + 1e-6
                if mx * mx + my * my + mz * mz >= lim * lim:
                    continue
                if self._pair_conflict(ca, cb, d2, sep2, lo):
                    da, db = ca[0], cb[0]
                    hit.setdefault(da.idx, []).append(db.id)
                    hit.setdefault(db.idx, []).append(da.id)
        for idx in sorted(hit):
            d = self.drones[idx]
            if d.phase is LOST or d.phase is PAD:
                continue
            self._lose(d, k, "separation", "with " + ",".join(sorted(hit[idx])))

    def _pair_conflict(self, ca, cb, d2, sep2, lo):
        w = self.w
        if ca[1] and cb[1] and d2 < sep2:  # the instant k itself
            pa, pb = ca[2], cb[2]
            if not w.in_pad_zone(pa[0], pa[1]) and not w.in_pad_zone(pb[0], pb[1]):
                return True
        if ca[5] is None:
            ca[5] = _pieces(ca[4], lo)
        if cb[5] is None:
            cb[5] = _pieces(cb[4], lo)
        sa, sb = ca[5], cb[5]
        i = j = 0
        while i < len(sa) and j < len(sb):
            A, B = sa[i], sb[j]
            u = A[0] if A[0] > B[0] else B[0]
            v = A[1] if A[1] < B[1] else B[1]
            if u < v:
                a0, a1 = _at(A, u), _at(A, v)
                b0, b1 = _at(B, u), _at(B, v)
                if self._conflict_on(a0, a1, b0, b1, sep2):
                    return True
            if A[1] <= B[1]:
                i += 1
            else:
                j += 1
        return False

    def _conflict_on(self, a0, a1, b0, b1, sep2):
        """True if two drones moving linearly a0 -> a1 and b0 -> b1 over the same time span come strictly
        closer than the separation at a moment when neither is inside a pad zone."""
        rx, ry, rz = a0[0] - b0[0], a0[1] - b0[1], a0[2] - b0[2]
        ex, ey, ez = (a1[0] - b1[0]) - rx, (a1[1] - b1[1]) - ry, (a1[2] - b1[2]) - rz
        qa = ex * ex + ey * ey + ez * ez
        qb = 2.0 * (rx * ex + ry * ey + rz * ez)
        qc = rx * rx + ry * ry + rz * rz - sep2
        # closest approach over the span, compared strictly like the point check (exactly 15 m is fine)
        sm = 0.0
        if qa > 0.0:
            sm = -qb / (2.0 * qa)
            sm = 0.0 if sm < 0.0 else (1.0 if sm > 1.0 else sm)
        elif qb < 0.0:
            sm = 1.0
        mx, my, mz = rx + ex * sm, ry + ey * sm, rz + ez * sm
        if mx * mx + my * my + mz * mz >= sep2:
            return False
        # fraction s in [0, 1] of the span with qa s^2 + qb s + qc < 0 (distance < separation)
        s_lo = s_hi = sm
        if qa > 1e-12:
            disc = qb * qb - 4.0 * qa * qc
            if disc > 0.0:
                sq = math.sqrt(disc)
                s_lo, s_hi = (-qb - sq) / (2.0 * qa), (-qb + sq) / (2.0 * qa)
        elif qb != 0.0:
            root = -qc / qb
            s_lo, s_hi = (0.0, root) if qb > 0.0 else (root, 1.0)
        else:
            s_lo, s_hi = 0.0, 1.0
        if s_lo < 0.0:
            s_lo = 0.0
        if s_hi > 1.0:
            s_hi = 1.0
        if not s_lo < s_hi:  # rounding left only the closest point: test that instant
            s_lo = s_hi = sm
        # remove the parts where either drone is inside a pad zone (closed intervals)
        R = self.r.pad_zone_radius_m
        ex_iv = _pad_spans(self.w, R, a0, a1) + _pad_spans(self.w, R, b0, b1)
        if not ex_iv:
            return True
        if s_lo == s_hi:
            return not any(e0 <= s_lo <= e1 for e0, e1 in ex_iv)
        ex_iv.sort()
        cur = s_lo
        for e0, e1 in ex_iv:
            if e0 > cur:
                return True  # (cur, min(e0, s_hi)) is free and has positive length
            if e1 > cur:
                cur = e1
                if cur >= s_hi:
                    return False
        return cur < s_hi


def _pieces(pts, lo):
    """Linear pieces (t0, t1, x0, y0, z0, x1, y1, z1) of a path where the drone counts, clipped to t >= lo."""
    out = []
    for j in range(len(pts) - 1):
        t0, x0, y0, z0, counts = pts[j]
        if not counts:
            continue
        t1, x1, y1, z1, _ = pts[j + 1]
        if t1 <= t0 or t1 <= lo:
            continue
        if t0 < lo:
            f = (lo - t0) / (t1 - t0)
            x0, y0, z0, t0 = x0 + (x1 - x0) * f, y0 + (y1 - y0) * f, z0 + (z1 - z0) * f, lo
        out.append((t0, t1, x0, y0, z0, x1, y1, z1))
    return out


def _at(P, t):
    t0, t1 = P[0], P[1]
    if t <= t0:
        return P[2], P[3], P[4]
    if t >= t1:
        return P[5], P[6], P[7]
    f = (t - t0) / (t1 - t0)
    return P[2] + (P[5] - P[2]) * f, P[3] + (P[6] - P[3]) * f, P[4] + (P[7] - P[4]) * f


def _pad_spans(world, radius, p0, p1):
    """Closed fractions [s0, s1] of the horizontal path p0 -> p1 that lie within ``radius`` of a pad."""
    out = []
    r2 = radius * radius
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    qa = dx * dx + dy * dy
    for px, py in world._pad_xy:
        ox, oy = p0[0] - px, p0[1] - py
        qc = ox * ox + oy * oy - r2
        if qa <= 1e-12:
            if qc <= 0.0:
                out.append((0.0, 1.0))
            continue
        qb = 2.0 * (ox * dx + oy * dy)
        disc = qb * qb - 4.0 * qa * qc
        if disc < 0.0:
            continue
        sq = math.sqrt(disc)
        s0, s1 = (-qb - sq) / (2.0 * qa), (-qb + sq) / (2.0 * qa)
        if s1 < 0.0 or s0 > 1.0:
            continue
        out.append((s0, s1))
    return out
