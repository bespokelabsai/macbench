"""Points and score.

Points for one day:

1. take all banked photos (valid and banked);
2. **sync** (moving targets only, when the rules have ``moving_sync_window_s``):
   a banked moving photo p of target m by drone d at time tp is *synced* if
   some banked photo q of m by another drone d' != d has |tp - tq| <=
   ``moving_sync_window_s`` and an angle between the two drones' directions
   to the target (Dp - T(tm) and Dq - T(tm), both measured from the target's
   position at the midpoint time tm = (tp + tq) / 2) of at least
   ``moving_sync_min_angle_deg`` (1e-9 tolerance on both bounds). Sync is
   decided on all banked photos before acceptance, so a partner later
   rejected for ``repeat_limit`` or ``repeat_angle`` still counts. An
   unsynced moving photo gets reason ``"unsynced"`` and scores 0;
3. **acceptance**, over the static photos and the synced moving photos,
   sorted by (tp, drone id): for each target, a photo is accepted if it is
   the target's first accepted photo, or if its viewing direction (D - T) is
   at least ``repeat_min_angle_deg`` from every earlier accepted photo of
   that target (1e-9 degree tolerance on the bound); at most
   len(multipliers) photos are accepted per target (``repeat_limit`` is
   tested before ``repeat_angle``);
4. the n-th accepted photo is worth value * multipliers[n - 1] * L * Q, where
   the multipliers are ``repeat_multipliers`` for static targets and
   ``repeat_multipliers_moving`` for moving ones, L is :func:`light`
   (target, tp) for static targets and 1 for moving ones, and Q is
   :func:`quality` (the photo quality from distance and viewing angle);
5. the day's points are the sum.

Score: S = max(0, (P - P_greedy) / (P_ref - P_greedy)), uncapped above.
"""

from __future__ import annotations

import math

import numpy as np

from .geometry import EPS, _resolve_target, los_clear_many

#: Upward normal used for the light factor of roofs and bridges.
UP = (0.0, 0.0, 1.0)


def light(target, t: float, world=None) -> float:
    """Sun-light factor L of a target at time t.

    L = round(light_min + (light_max - light_min) * max(0, n . s(t)), 4), where
    s(t) is the interpolated unit vector toward the sun (``sun.json``, see
    :meth:`skyline.world.Sun.at`) and n is (0, 0, 1) for roofs and bridges and
    the target's ``normal`` for facades. Moving targets have L = 1.0, and so
    does every target when the rules have no ``photo.light``.

    ``target`` is a :class:`~skyline.world.Target` from :func:`skyline.load_world`
    (it carries the sun table), or, with ``world`` given, a target id or Target.
    """
    if world is not None:
        if isinstance(target, str):
            target = world.targets[target]
        elif isinstance(target, dict):
            target = world.targets[target["id"]]
        if target.moving or world.rules.light_min is None:
            return 1.0
        sun, lmin, lmax = world.sun, world.rules.light_min, world.rules.light_max
    else:
        if target.moving:
            return 1.0
        if target._light is None:
            return 1.0
        sun, lmin, lmax = target._light
    n = UP if target.kind in ("roof", "bridge") else target.normal
    s = sun.at(t)
    c = n[0] * s[0] + n[1] * s[1] + n[2] * s[2]
    if c < 0.0:
        c = 0.0
    return round(lmin + (lmax - lmin) * c, 4)


def quality(world, target, D, t: float | None = None) -> float | None:
    """Photo quality factor Q of a photo of ``target`` from drone position D at time t.

    Q = round(qd * qa, 4). With d = |D - T| (T the target position at t,
    :meth:`~skyline.world.Target.position_at`) and ``photo.range_m`` = [d0, d1]:

    * qd = 1 - (1 - far_factor) * (d - d0) / (d1 - d0), clamped to [far_factor, 1];
    * static targets: qa = 1 - (1 - edge_factor) * theta / static_cone_deg,
      clamped to [edge_factor, 1], where theta is the angle in degrees between
      (D - T) and the target's ``normal`` (the angle of the cone test in
      :func:`skyline.geometry.viewpoint_reason`); moving targets: qa = 1.

    far_factor and edge_factor are ``photo.quality`` in ``rules.json``. Q is 1.0
    when the rules have no ``photo.quality``. None when there is no direction
    from the target to the drone (the target is absent at t, or D = T).
    ``target`` is a target id, a target dict (with ``"id"``) or a Target.
    """
    r = world.rules
    if r.quality_far_factor is None:
        return 1.0
    tg = _resolve_target(world, target)
    T = tg.position_at(t)
    if T is None:
        return None
    vx, vy, vz = float(D[0]) - T[0], float(D[1]) - T[1], float(D[2]) - T[2]
    dist = math.sqrt(vx * vx + vy * vy + vz * vz)
    if dist == 0.0:
        return None
    d0, d1 = r.photo_range_m
    qf = r.quality_far_factor
    qd = 1.0 - (1.0 - qf) * (dist - d0) / (d1 - d0) if d1 > d0 else 1.0
    qd = min(1.0, max(qf, qd))
    qa = 1.0
    if not tg.moving:
        n = tg.normal
        nn = math.sqrt(n[0] * n[0] + n[1] * n[1] + n[2] * n[2])
        c = (vx * n[0] + vy * n[1] + vz * n[2]) / (dist * nn)
        theta = math.degrees(math.acos(max(-1.0, min(1.0, c))))
        qe = r.quality_edge_factor
        qa = 1.0 - (1.0 - qe) * theta / r.static_cone_deg if r.static_cone_deg > 0.0 else 1.0
        qa = min(1.0, max(qe, qa))
    return round(qd * qa, 4)


def _unit_view(tg, p):
    T = tg.position_at(p["t"])
    D = p["pos"]
    v = (D[0] - T[0], D[1] - T[1], D[2] - T[2])
    n = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    return (v[0] / n, v[1] / n, v[2] / n)


def _sync_angle_deg(tg, p, q):
    """Angle between the two drones' directions to the target at the midpoint time.

    Both directions are measured from T((tp + tq) / 2), so two photos from the
    same place give 0 degrees whatever the target's motion. None when a drone
    sits on the target's midpoint position (no direction).
    """
    T = tg.position_at(0.5 * (p["t"] + q["t"]))
    vs = []
    for D in (p["pos"], q["pos"]):
        v = (D[0] - T[0], D[1] - T[1], D[2] - T[2])
        n = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
        if n < 1e-9:
            return None
        vs.append((v[0] / n, v[1] / n, v[2] / n))
    return _angle_deg(vs[0], vs[1])


def _angle_deg(u, w):
    c = max(-1.0, min(1.0, u[0] * w[0] + u[1] * w[1] + u[2] * w[2]))
    return math.degrees(math.acos(c))


def score_photos(world, photos: list) -> float:
    """Mark photos in place and return the day's points.

    Each photo is a dict with at least ``t``, ``drone``, ``target``, ``valid``,
    ``banked`` and ``pos``. This sets, on every photo:

    * ``light``: L(target, t) for a static target, 1.0 for a moving one;
    * ``quality``: Q, :func:`quality` (target, pos, t) (None when the target
      has no position at t or D = T);
    * ``synced``: None for a static target; for a moving target True if the
      photo is valid, banked and synced, else False;
    * ``accepted`` and ``value`` (points credited);
    * ``reason`` of a banked photo that scores nothing: ``"unsynced"``,
      ``"repeat_limit"`` or ``"repeat_angle"``.

    A photo whose ``reason`` is already ``"noise"`` (see :func:`apply_noise`)
    is left out of sync and acceptance, exactly like an invalid photo.
    """
    r = world.rules
    order = {d: i for i, d in enumerate(r.fleet)}
    sync_on = r.moving_sync_window_s is not None
    for p in photos:
        tg = world.targets[p["target"]]
        p["light"] = light(tg, p["t"]) if not tg.moving else 1.0
        p["quality"] = quality(world, tg, p["pos"], p["t"])
        p["synced"] = False if tg.moving else None
    banked = sorted((p for p in photos if p["valid"] and p["banked"] and p.get("reason") != "noise"),
                    key=lambda p: (p["t"], order.get(p["drone"], len(order)), p["drone"]))
    views = [_unit_view(world.targets[p["target"]], p) for p in banked]

    # Sync (moving targets), decided on every banked photo before acceptance.
    by_target: dict = {}
    for i, p in enumerate(banked):
        if world.targets[p["target"]].moving:
            by_target.setdefault(p["target"], []).append(i)
    for idxs in by_target.values():
        for i in idxs:
            p = banked[i]
            if not sync_on:
                p["synced"] = True
                continue
            ok = False
            for j in idxs:
                q = banked[j]
                if q["drone"] == p["drone"]:
                    continue
                if abs(p["t"] - q["t"]) > r.moving_sync_window_s + EPS:
                    continue
                a = _sync_angle_deg(world.targets[p["target"]], p, q)
                if a is not None and a >= r.moving_sync_min_angle_deg - EPS:
                    ok = True
                    break
            p["synced"] = ok
            if not ok:
                p["reason"] = "unsynced"

    # Acceptance over static photos and synced moving photos.
    accepted: dict = {}
    total = 0.0
    for i, p in enumerate(banked):
        tg = world.targets[p["target"]]
        if tg.moving and not p["synced"]:
            continue
        mults = r.repeat_multipliers_moving if tg.moving else r.repeat_multipliers
        u = views[i]
        prev = accepted.setdefault(tg.id, [])
        if len(prev) >= len(mults):
            p["reason"] = "repeat_limit"
            continue
        ok = True
        for w in prev:
            if _angle_deg(u, w) < r.repeat_min_angle_deg - EPS:
                ok = False
                break
        if not ok:
            p["reason"] = "repeat_angle"
            continue
        p["accepted"] = True
        p["value"] = tg.value * mults[len(prev)] * p["light"] * p["quality"]
        prev.append(u)
        total += p["value"]
    return total


def points_to_score(P: float, norm: dict) -> float:
    """S = max(0, (P - P_greedy) / (P_ref - P_greedy)), uncapped above.

    ``norm`` is a dict with ``P_greedy`` and ``P_ref`` (``norm.json`` or
    ``norm_forecast.json``).
    """
    pg, pr = float(norm["P_greedy"]), float(norm["P_ref"])
    if pr == pg:
        raise ValueError("norm: P_ref equals P_greedy")
    return max(0.0, (float(P) - pg) / (pr - pg))


# --------------------------------------------------------------------------- v2b: landmark mapping
def _landmark(world, landmark):
    return world.landmarks[landmark] if isinstance(landmark, str) else landmark


def scan_mask(world, D, landmark) -> np.ndarray:
    """Bool array over the landmark's patches (file order): which patches a scan from D sees.

    A patch P of landmark L is seen from D when, with the 1e-9 tolerance
    on every bound:

    1. range: ``scan_range_m[0] <= |D - P.c| <= scan_range_m[1]``;
    2. incidence: the angle between P.n and (D - P.c) is at most ``max_incidence_deg``;
    3. field of view: the angle between (P.c - D) and (L.centroid - D) is at most
       ``scan_fov_half_deg``;
    4. line of sight: ``geometry.los_clear(world, D, P.c, exclude_building_id=L.building_id)``.
    """
    lm = _landmark(world, landmark)
    r = world.rules
    Dx, Dy, Dz = float(D[0]), float(D[1]), float(D[2])
    C = lm.C
    vx, vy, vz = C[:, 0] - Dx, C[:, 1] - Dy, C[:, 2] - Dz            # D -> P.c
    dist = np.sqrt(vx * vx + vy * vy + vz * vz)
    rmin, rmax = r.scan_range_m
    ok = (dist >= rmin - EPS) & (dist <= rmax + EPS) & (dist > 0.0)
    if not ok.any():
        return ok
    with np.errstate(divide="ignore", invalid="ignore"):
        N = lm.N
        ci = -(vx * N[:, 0] + vy * N[:, 1] + vz * N[:, 2]) / dist      # cos(n, D - P.c), |n| = 1
        inc = np.degrees(np.arccos(np.clip(ci, -1.0, 1.0)))
        ok &= inc <= r.max_incidence_deg + EPS
        if not ok.any():
            return ok
        cx, cy, cz = lm.centroid[0] - Dx, lm.centroid[1] - Dy, lm.centroid[2] - Dz
        cn = math.sqrt(cx * cx + cy * cy + cz * cz)
        if cn == 0.0:
            return np.zeros(len(C), dtype=bool)
        cf = (vx * cx + vy * cy + vz * cz) / (dist * cn)
        fov = np.degrees(np.arccos(np.clip(cf, -1.0, 1.0)))
        ok &= fov <= r.scan_fov_half_deg + EPS
    idx = np.nonzero(ok)[0]
    if len(idx):
        clear = los_clear_many(world, (Dx, Dy, Dz), C[idx], lm.building_id)
        ok[idx[~clear]] = False
    return ok


def scan_sees(world, D, landmark) -> list:
    """Patch ids (file order) of ``landmark`` (an id or a Landmark) seen by a scan from position D.

    See :func:`scan_mask` for the four rules. Vectorised; line of sight uses
    :func:`skyline.geometry.los_clear_many`, which equals ``los_clear`` patch by patch.
    """
    lm = _landmark(world, landmark)
    m = scan_mask(world, D, lm)
    return [lm.patch_ids[i] for i in np.nonzero(m)[0]]


def landmark_detail(world, landmark_id, scans) -> dict:
    """Mapping result of one landmark from the scans that count (banked, not card_full, not noise).

    ``scans`` is an iterable of scan dicts with ``pos``, ``t`` and ``drone`` (and optionally ``seen``,
    a list of patch ids, to skip recomputing), or of bare positions (x, y, z): a bare position is taken
    as a scan by a drone of its own at t = 0, so a list of positions is evaluated as one rig flown at
    the same moment. Every scan is taken as a scan of this landmark.

    A patch is **mapped** when some group of the scans that see it fits one
    time window (max t - min t <= ``mapping.sync_window_s``), comes from at least
    ``mapping.min_drones`` different drones, has at least ``mapping.min_views`` scans, and the largest
    angle between the view directions (D - P.c) of two scans *by different drones* in the group is at
    least ``mapping.min_spread_deg`` (1e-9 tolerance on every bound). All four conditions only get
    easier as the group grows, so the maximal groups (all scans in [t_a, t_a + window], a sliding
    window over the patch's scans sorted by time) are the ones checked.

    Returns ``{"c", "mapped", "total", "points", "mapped_patches", "views", "spread_deg",
    "rig_drones"}``: c = mapped area / total area; points = value * (c ** coverage_exponent + the sum
    of every ``mapping.bonus`` b with c >= t); per patch, ``views`` is the number of scans seeing it,
    ``rig_drones`` the most different drones among them inside one window and ``spread_deg`` the
    largest cross-drone angle inside one window (0 when no window has two drones).
    """
    lm = _landmark(world, landmark_id)
    r = world.rules
    n = len(lm.patch_ids)
    views: list = [[] for _ in range(n)]
    for k, sc in enumerate(scans):
        if isinstance(sc, dict):
            D = sc["pos"]
            t, drone = float(sc["t"]), sc["drone"]
            seen = sc.get("seen")
            ids = [lm.index_of[p] for p in seen] if seen is not None else np.nonzero(scan_mask(world, D, lm))[0]
        else:
            D = sc
            t, drone = 0.0, ("#", k)
            ids = np.nonzero(scan_mask(world, D, lm))[0]
        for i in ids:
            views[int(i)].append((t, drone, float(D[0]), float(D[1]), float(D[2])))
    mapped = np.zeros(n, dtype=bool)
    spreads = [0.0] * n
    rig = [0] * n
    need, need_d = r.mapping_min_views, r.mapping_min_drones
    win = r.mapping_sync_window_s + EPS
    cos_need = None
    for i, vs in enumerate(views):
        if not vs:
            continue
        vs.sort(key=lambda v: v[0])
        ts = [v[0] for v in vs]
        dr_ids: dict = {}
        dr = np.array([dr_ids.setdefault(v[1], len(dr_ids)) for v in vs])
        V = np.array([v[2:] for v in vs], dtype=np.float64) - lm.C[i]
        V /= np.sqrt((V * V).sum(axis=1))[:, None]
        m = len(vs)
        best_c, best_d, ok = 1.0, 0, False
        hi = -1
        for lo in range(m):
            if hi < lo:
                hi = lo
            prev = hi
            while hi + 1 < m and ts[hi + 1] - ts[lo] <= win:
                hi += 1
            if lo > 0 and hi == prev and ts[prev] - ts[lo - 1] <= win:
                continue  # same end as the window before: a subset of a group already checked
            g = slice(lo, hi + 1)
            nd = len(set(dr[g].tolist()))
            best_d = max(best_d, nd)
            if nd < 2:
                continue
            G = np.einsum("ij,kj->ik", V[g], V[g])
            G[dr[g][:, None] == dr[g][None, :]] = 2.0
            cmin = float(G.min())
            best_c = min(best_c, cmin)
            if (not ok and hi - lo + 1 >= need and nd >= need_d
                    and math.degrees(math.acos(max(-1.0, min(1.0, cmin)))) >= r.mapping_min_spread_deg - EPS):
                ok = True
        mapped[i] = ok
        rig[i] = best_d
        spreads[i] = math.degrees(math.acos(max(-1.0, min(1.0, best_c)))) if best_d >= 2 else 0.0
    c = float(lm.area[mapped].sum()) / lm.total_area
    bonus = sum(b for t, b in r.mapping_bonus if c >= t - EPS)
    return {"c": c, "mapped": int(mapped.sum()), "total": n,
            "points": lm.value * (c ** r.mapping_coverage_exponent + bonus),
            "mapped_patches": [lm.patch_ids[i] for i in np.nonzero(mapped)[0]],
            "views": [len(v) for v in views], "spread_deg": spreads, "rig_drones": rig}


def landmark_points(world, landmark_id, scans) -> float:
    """Points of one landmark from the counting scans; see :func:`landmark_detail`."""
    return landmark_detail(world, landmark_id, scans)["points"]


# --------------------------------------------------------------------------- v2b: noise budget
def noise_usage(world, samples) -> dict:
    """Noise usage U(z, h): zone id -> list of 12 hourly counts (h = 0..11).

    ``samples`` is either a :class:`~skyline.result.SimResult` (its usage from the simulation is
    returned as a copy), or an iterable of ``(t, x, y, z)`` airborne positions, one per drone per
    integer second, which are counted the checker's way: each counts 1 s in every zone whose polygon
    contains (x, y) (boundary included) when z < ``noise.alt_m``, in hour floor(t / 3600).
    """
    if hasattr(samples, "noise_usage"):
        return {k: list(v) for k, v in samples.noise_usage.items()}
    U = {nz.id: [0] * 12 for nz in world.noise_zones}
    for t, x, y, z in samples:
        h = int(math.floor(t / 3600.0))
        if not 0 <= h < 12:
            continue
        for nz in world.noise_zones_at(x, y, z):
            U[nz.id][h] += 1
    return U


def noise_over(world, usage) -> dict:
    """zone id -> set of hours h with U(z, h) > budget_s (strictly)."""
    out = {}
    for nz in world.noise_zones:
        u = usage.get(nz.id, [])
        out[nz.id] = {h for h, v in enumerate(u) if v > nz.budget_s}
    return out


def apply_noise(world, items, usage) -> int:
    """Give reason ``"noise"`` to every banked, valid photo or scan taken in an over-budget zone-hour.

    An item counts against zone z in hour h = floor(t / 3600) when its drone position ``pos`` was
    inside z (boundary included) and below ``noise.alt_m`` at its time ``t`` and U(z, h) > budget_s(z).
    Invalid and void items keep their reason. Returns the number of items marked.
    """
    over = noise_over(world, usage)
    if not any(over.values()):
        return 0
    n = 0
    for p in items:
        if not p.get("banked") or not p.get("valid", True) or p.get("reason") is not None:
            continue
        h = int(math.floor(p["t"] / 3600.0))
        x, y, z = p["pos"]
        if any(h in over[nz.id] for nz in world.noise_zones_at(x, y, z)):
            p["reason"] = "noise"
            n += 1
    return n


# --------------------------------------------------------------------------- v2b: light shows
def score_shows(world, holds) -> tuple:
    """Mark show holds in place and return (points, per-show dict).

    A hold (one ``show`` row flown) is a dict with ``show``, ``slot``, ``drone``, ``t_arrive``,
    ``late`` and ``banked``. Its ``reason`` becomes None when it fills its slot, else, in this order:
    ``"late"`` (arrived after t0), ``"void"`` (lost before landing), ``"same_drone"`` (the drone
    already filled another slot of this show, first by arrival time) or ``"slot_taken"`` (another
    drone filled that slot first). A show scores ``value`` only when every slot is filled.
    Per show: ``{"value", "slots", "filled", "points", "slot_drones", "failed"}`` with
    ``slot_drones`` the drone filling each slot (None if unfilled) and ``failed`` slot -> reason
    (``"empty"`` when no row targets that slot).
    """
    out = {}
    total = 0.0
    by_show: dict = {}
    for h in holds:
        by_show.setdefault(h["show"], []).append(h)
    order = {d: i for i, d in enumerate(world.rules.fleet)}
    for sid, sh in world.shows.items():
        hs = sorted(by_show.get(sid, []), key=lambda h: (h["t_arrive"], order.get(h["drone"], 99), h["slot"]))
        n = len(sh.slots)
        slot_drone = [None] * n
        fail = {}
        used = set()
        for h in hs:
            if h["late"]:
                h["reason"] = "late"
            elif not h["banked"]:
                h["reason"] = "void"
            elif h["drone"] in used:
                h["reason"] = "same_drone"
            elif slot_drone[h["slot"]] is not None:
                h["reason"] = "slot_taken"
            else:
                h["reason"] = None
                slot_drone[h["slot"]] = h["drone"]
                used.add(h["drone"])
        for i in range(n):
            if slot_drone[i] is None:
                rs = [h["reason"] for h in hs if h["slot"] == i]
                fail[i] = rs[0] if rs else "empty"
        filled = sum(1 for v in slot_drone if v is not None)
        pts = sh.value if filled == n else 0.0
        total += pts
        out[sid] = {"value": sh.value, "slots": n, "filled": filled, "points": pts,
                    "slot_drones": slot_drone, "failed": fail}
    return total, out


# --------------------------------------------------------------------------- v2b: the whole day
def score_day(world, photos, scans, holds, usage) -> dict:
    """Day points before the loss fine = photo + landmark + show points.

    The fine per lost drone (``scoring.loss_fine``, section 10.4) is applied by :func:`skyline.simulate`.

    Order: (1) the noise penalty marks photos and scans in over-budget zone-hours (reason
    ``"noise"``); (2) :func:`score_photos` over the rest; (3) landmark mapping over the banked
    scans without a reason; (4) shows. Marks photos, scans and holds in place. Returns
    ``{"points", "photo_points", "landmark_points", "show_points", "landmarks", "shows", "noise"}``.
    """
    apply_noise(world, photos, usage)
    apply_noise(world, scans, usage)
    photo_pts = score_photos(world, photos)
    lm_out, lm_pts = {}, 0.0
    by_lm: dict = {}
    for sc in scans:
        if sc["banked"] and sc["reason"] is None:
            by_lm.setdefault(sc["landmark"], []).append(sc)
    for lid in world.landmarks:
        det = landmark_detail(world, lid, by_lm.get(lid, []))
        det["scans"] = len(by_lm.get(lid, []))
        lm_out[lid] = det
        lm_pts += det["points"]
    show_pts, show_out = score_shows(world, holds)
    over = noise_over(world, usage)
    noise_out = {}
    for nz in world.noise_zones:
        u = list(usage.get(nz.id, [0] * 12))
        noise_out[nz.id] = {"budget_s": nz.budget_s, "usage": u, "hours_over": sorted(over[nz.id]),
                            "max_usage": max(u) if u else 0}
    return {"points": photo_pts + lm_pts + show_pts, "photo_points": photo_pts, "landmark_points": lm_pts,
            "show_points": show_pts, "landmarks": lm_out, "shows": show_out, "noise": noise_out}
