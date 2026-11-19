"""Geometry: building index, line of sight, clearance and photo validity.

Buildings are vertical prisms from z = 0 to ``height`` over their footprint.

* **Line of sight** (:func:`los_clear`) tests a segment against the raw prisms.
  A segment that touches a prism (boundary included) is blocked. The target's
  own building blocks too, except within the last ``OWN_BUILDING_TAIL_M``
  (1.0 m) of the segment at the target end: a sight
  line may not pass through the target's own building.
* **Building clearance** (:func:`segment_clear`) tests a segment against the
  prisms grown by ``building_clearance_m`` (c): every point whose horizontal
  distance to the footprint is <= c, from z = 0 up to height + c. The buffer is
  exact (round corners), not a polygonal approximation. Touching counts.

Both tests are exact, not sampled: the segment is first clipped to the
prism's z range (z is linear along the segment), and the clipped part is then
tested in 2D against the footprint with shapely/GEOS.

* **Photo validity** (:func:`viewpoint_ok`, :func:`viewpoint_reason`) applies
  every photo-validity rule except banking: range, static cone or moving
  elevation, and line of sight. Inclusive bounds are compared with a 1e-9
  tolerance so that values exactly on a bound pass despite rounding.

Candidate buildings come from a uniform grid (:class:`BuildingIndex`); each
building is registered in every cell its grown bounding box overlaps, and a
query visits exactly the cells the segment's xy projection passes through.
"""

from __future__ import annotations

import math

import numpy as np
import shapely

__all__ = [
    "BuildingIndex", "segment_clear", "los_clear", "blocking_buildings",
    "viewpoint_ok", "viewpoint_reason", "segment_polygon_entry", "los_clear_many", "EPS", "OWN_BUILDING_TAIL_M",
]

#: Tolerance for inclusive comparisons on distances (m) and angles (deg).
EPS = 1e-9

#: Length (m) of the sight segment's target end inside which the target's own
#: building is ignored by the line-of-sight test.
OWN_BUILDING_TAIL_M = 1.0

#: Reasons returned by :func:`viewpoint_reason`.
REASON_ABSENT = "absent"        # moving target not present at the photo time
REASON_RANGE = "range"          # |D - T| outside range_m
REASON_CONE = "cone"            # static target: angle to normal > static_cone_deg
REASON_ELEVATION = "elevation"  # moving target: elevation < moving_min_elevation_deg
REASON_LOS = "los"              # a building blocks D -> T (own building: all but the last 1.0 m)


class BuildingIndex:
    """Uniform-grid spatial index over building prisms.

    Parameters
    ----------
    buildings : sequence of objects with ``id``, ``footprint`` (n x 2),
        ``height`` and ``polygon`` (shapely Polygon).
    clearance : building clearance in metres; cells cover the grown boxes.
    cell_size : grid cell edge in metres.
    """

    def __init__(self, buildings, clearance: float, cell_size: float = 50.0):
        self.clearance = float(clearance)
        self.cell = float(cell_size)
        n = len(buildings)
        self.ids = np.array([b.id for b in buildings], dtype=np.int64)
        self.heights = np.array([b.height for b in buildings], dtype=np.float64)
        self.polys = np.empty(n, dtype=object)
        for i, b in enumerate(buildings):
            self.polys[i] = b.polygon
        if n:
            shapely.prepare(self.polys)
            self.bounds = shapely.bounds(self.polys).astype(np.float64)  # (n, 4) raw
        else:
            self.bounds = np.zeros((0, 4))
        self.pos_of_id = {int(bid): i for i, bid in enumerate(self.ids)}
        c = self.clearance
        if n:
            self.x0 = float(self.bounds[:, 0].min() - c)
            self.y0 = float(self.bounds[:, 1].min() - c)
            x1 = float(self.bounds[:, 2].max() + c)
            y1 = float(self.bounds[:, 3].max() + c)
        else:
            self.x0 = self.y0 = 0.0
            x1 = y1 = 1.0
        self.nx = max(1, int(math.floor((x1 - self.x0) / self.cell)) + 1)
        self.ny = max(1, int(math.floor((y1 - self.y0) / self.cell)) + 1)
        # CSR layout in row-major cell order: a row's cells are contiguous.
        buckets = [[] for _ in range(self.nx * self.ny)]
        for i in range(n):
            bx0, by0, bx1, by1 = self.bounds[i]
            i0, i1 = self._ix(bx0 - c), self._ix(bx1 + c)
            j0, j1 = self._iy(by0 - c), self._iy(by1 + c)
            for j in range(j0, j1 + 1):
                for k in range(i0, i1 + 1):
                    buckets[j * self.nx + k].append(i)
        self.cell_start = np.zeros(len(buckets) + 1, dtype=np.int64)
        np.cumsum([len(b) for b in buckets], out=self.cell_start[1:])
        flat = [i for b in buckets for i in b]
        self.cell_items = np.array(flat, dtype=np.int64)

    def _ix(self, x: float) -> int:
        return min(self.nx - 1, max(0, int(math.floor((x - self.x0) / self.cell))))

    def _iy(self, y: float) -> int:
        return min(self.ny - 1, max(0, int(math.floor((y - self.y0) / self.cell))))

    def candidates(self, x0: float, y0: float, x1: float, y1: float) -> np.ndarray:
        """Sorted indices of buildings registered in the cells the 2D segment crosses.

        Conservative: for each grid row in the segment's y range, it takes every
        cell between the segment's x extremes within that row band.
        """
        if len(self.ids) == 0:
            return np.zeros(0, dtype=np.int64)
        cs = self.cell
        gy0, gy1 = (y0 - self.y0) / cs, (y1 - self.y0) / cs
        gx0, gx1 = (x0 - self.x0) / cs, (x1 - self.x0) / cs
        jlo = int(math.floor(min(gy0, gy1)))
        jhi = int(math.floor(max(gy0, gy1)))
        if jhi < 0 or jlo >= self.ny:
            return np.zeros(0, dtype=np.int64)
        jlo, jhi = max(jlo, 0), min(jhi, self.ny - 1)
        dgy = gy1 - gy0
        slices = []
        start, items, nx = self.cell_start, self.cell_items, self.nx
        for j in range(jlo, jhi + 1):
            if dgy == 0.0:
                xa, xb = gx0, gx1
            else:
                sa = (j - gy0) / dgy
                sb = (j + 1 - gy0) / dgy
                if sa > sb:
                    sa, sb = sb, sa
                sa, sb = max(sa, 0.0), min(sb, 1.0)
                xa, xb = gx0 + (gx1 - gx0) * sa, gx0 + (gx1 - gx0) * sb
            ilo = int(math.floor(min(xa, xb)))
            ihi = int(math.floor(max(xa, xb)))
            if ihi < 0 or ilo >= nx:
                continue
            ilo, ihi = max(ilo, 0), min(ihi, nx - 1)
            a, b = start[j * nx + ilo], start[j * nx + ihi + 1]
            if b > a:
                slices.append(items[a:b])
        if not slices:
            return np.zeros(0, dtype=np.int64)
        return np.unique(np.concatenate(slices))

    def hits(self, p, q, grow: float, exclude_index: int = -1) -> np.ndarray:
        """Indices (sorted) of prisms touched by the 3D segment p -> q.

        ``grow`` = 0 tests the raw prisms; ``grow`` = c tests the prisms grown
        by c horizontally (exact distance) and c on top.
        """
        px, py, pz = float(p[0]), float(p[1]), float(p[2])
        qx, qy, qz = float(q[0]), float(q[1]), float(q[2])
        cand = self.candidates(px, py, qx, qy)
        if exclude_index >= 0 and len(cand):
            cand = cand[cand != exclude_index]
        return self.hits_among(cand, p, q, grow)

    def hits_among(self, cand: np.ndarray, p, q, grow: float) -> np.ndarray:
        """The subset of prisms ``cand`` (sorted indices) touched by the 3D segment p -> q."""
        px, py, pz = float(p[0]), float(p[1]), float(p[2])
        qx, qy, qz = float(q[0]), float(q[1]), float(q[2])
        if len(cand) == 0:
            return cand
        tops = self.heights[cand] + grow
        dz = qz - pz
        if dz == 0.0:
            if pz < 0.0:
                return cand[:0]
            keep = tops >= pz
            lo = np.zeros(int(keep.sum()))
            hi = np.ones(len(lo))
            cand = cand[keep]
        else:
            s0 = (0.0 - pz) / dz
            sh = (tops - pz) / dz
            lo = np.maximum(0.0, np.minimum(s0, sh))
            hi = np.minimum(1.0, np.maximum(s0, sh))
            keep = lo <= hi
            cand, lo, hi = cand[keep], lo[keep], hi[keep]
        if len(cand) == 0:
            return cand
        dx, dy = qx - px, qy - py
        ax, ay = px + dx * lo, py + dy * lo
        bx, by = px + dx * hi, py + dy * hi
        # Bounding-box prefilter (inclusive, with the grow margin).
        bb = self.bounds[cand]
        keep = ((np.minimum(ax, bx) <= bb[:, 2] + grow) & (np.maximum(ax, bx) >= bb[:, 0] - grow)
                & (np.minimum(ay, by) <= bb[:, 3] + grow) & (np.maximum(ay, by) >= bb[:, 1] - grow))
        if not keep.any():
            return cand[:0]
        cand, ax, ay, bx, by = cand[keep], ax[keep], ay[keep], bx[keep], by[keep]
        coords = np.stack([np.stack([ax, ay], axis=1), np.stack([bx, by], axis=1)], axis=1)
        lines = shapely.linestrings(coords)
        degenerate = (ax == bx) & (ay == by)
        if degenerate.any():
            # GEOS mishandles zero-length lines against prepared polygons: use points.
            lines[degenerate] = shapely.points(ax[degenerate], ay[degenerate])
        if grow > 0.0:
            hit = shapely.dwithin(self.polys[cand], lines, grow)
        else:
            hit = shapely.intersects(self.polys[cand], lines)
        return cand[hit]


def _xyz(p) -> tuple[float, float, float]:
    return float(p[0]), float(p[1]), float(p[2])


def blocking_buildings(world, p, q, grown: bool = True, exclude_building_id=None) -> list[int]:
    """Ids of buildings whose prism (grown if ``grown``) the segment p -> q touches.

    Sorted by position in ``city.json``. ``exclude_building_id`` (or None) is skipped.
    """
    idx = world.index
    ex = -1 if exclude_building_id is None else idx.pos_of_id.get(int(exclude_building_id), -1)
    grow = world.rules.building_clearance_m if grown else 0.0
    return [int(idx.ids[i]) for i in idx.hits(_xyz(p), _xyz(q), grow, ex)]


def segment_clear(world, p, q) -> bool:
    """True if segment p -> q keeps the building clearance.

    The segment must not touch any prism grown by ``building_clearance_m``
    (footprint buffered by c, height + c).
    """
    return len(world.index.hits(_xyz(p), _xyz(q), world.rules.building_clearance_m)) == 0


def los_clear(world, D, T, exclude_building_id=None) -> bool:
    """True if the sight segment D -> T is clear.

    The segment must touch no raw building prism. ``exclude_building_id`` is the
    target's own building (None for moving targets and bridges): it is ignored
    only within the last :data:`OWN_BUILDING_TAIL_M` metres of the segment at
    the T end, i.e. it is tested against D -> T' where T' is that far from T.
    """
    idx = world.index
    ex = -1 if exclude_building_id is None else idx.pos_of_id.get(int(exclude_building_id), -1)
    Dp, Tp = _xyz(D), _xyz(T)
    if len(idx.hits(Dp, Tp, 0.0, ex)):
        return False
    if ex < 0:
        return True
    vx, vy, vz = Tp[0] - Dp[0], Tp[1] - Dp[1], Tp[2] - Dp[2]
    L = math.sqrt(vx * vx + vy * vy + vz * vz)
    if L <= OWN_BUILDING_TAIL_M:
        return True
    f = (L - OWN_BUILDING_TAIL_M) / L
    Tq = (Dp[0] + vx * f, Dp[1] + vy * f, Dp[2] + vz * f)
    return len(idx.hits_among(np.array([ex], dtype=np.int64), Dp, Tq, 0.0)) == 0


def los_clear_many(world, D, T, exclude_building_id=None) -> np.ndarray:
    """Vectorised :func:`los_clear` from one point D to many points T (m x 3): a bool array.

    Gives exactly ``[los_clear(world, D, t, exclude_building_id) for t in T]``: the same clipping
    arithmetic and the same GEOS predicates, run over all (segment, building) pairs at once. Candidate
    buildings are those whose footprint bounding box overlaps the bounding box of all segments; the
    exact test then decides, so a larger candidate set cannot change the result.
    """
    idx = world.index
    T = np.asarray(T, dtype=np.float64).reshape(-1, 3)
    m = len(T)
    out = np.ones(m, dtype=bool)
    if m == 0 or len(idx.ids) == 0:
        return out
    px, py, pz = _xyz(D)
    ex = -1 if exclude_building_id is None else idx.pos_of_id.get(int(exclude_building_id), -1)
    qx, qy, qz = T[:, 0], T[:, 1], T[:, 2]
    # Own building: tested on D -> T' (1.0 m short of T), and not at all when |T - D| <= 1.0 m.
    if ex >= 0:
        vx, vy, vz = qx - px, qy - py, qz - pz
        L = np.sqrt(vx * vx + vy * vy + vz * vz)
        with np.errstate(divide="ignore", invalid="ignore"):
            f = (L - OWN_BUILDING_TAIL_M) / L
        own_on = L > OWN_BUILDING_TAIL_M
        ox, oy, oz = px + vx * f, py + vy * f, pz + vz * f
    bb = idx.bounds
    sel = ((bb[:, 0] <= max(px, float(qx.max()))) & (bb[:, 2] >= min(px, float(qx.min())))
           & (bb[:, 1] <= max(py, float(qy.max()))) & (bb[:, 3] >= min(py, float(qy.min())))
           & (idx.heights >= min(pz, float(qz.min()))))
    cand = np.nonzero(sel)[0]
    if ex >= 0:
        cand = cand[cand != ex]
    rows_i, cols_b, Ax, Ay, Bx, By = [], [], [], [], [], []

    def clip(cb, ex_, ey_, ez_, si):
        # cb: candidate building indices (k,); ex_, ey_, ez_: segment ends (s,); si: segment row ids (s,)
        if len(cb) == 0 or len(si) == 0:
            return
        tops = idx.heights[cb][None, :]
        dz = (ez_ - pz)[:, None]
        flat = dz == 0.0
        with np.errstate(divide="ignore", invalid="ignore"):
            s0 = (0.0 - pz) / dz
            sh = (tops - pz) / dz
        lo = np.maximum(0.0, np.minimum(s0, sh))
        hi = np.minimum(1.0, np.maximum(s0, sh))
        if pz < 0.0:
            keep_flat = np.zeros_like(flat & (tops >= pz))
        else:
            keep_flat = flat & (tops >= pz)
        lo = np.where(flat, 0.0, lo)
        hi = np.where(flat, 1.0, hi)
        keep = np.where(flat, keep_flat, lo <= hi)
        dx = (ex_ - px)[:, None]
        dy = (ey_ - py)[:, None]
        ax, ay = px + dx * lo, py + dy * lo
        bx, by = px + dx * hi, py + dy * hi
        b4 = idx.bounds[cb]
        keep &= ((np.minimum(ax, bx) <= b4[None, :, 2]) & (np.maximum(ax, bx) >= b4[None, :, 0])
                 & (np.minimum(ay, by) <= b4[None, :, 3]) & (np.maximum(ay, by) >= b4[None, :, 1]))
        ii, jj = np.nonzero(keep)
        if len(ii):
            rows_i.append(si[ii])
            cols_b.append(cb[jj])
            Ax.append(ax[ii, jj]); Ay.append(ay[ii, jj]); Bx.append(bx[ii, jj]); By.append(by[ii, jj])

    allrows = np.arange(m)
    clip(cand, qx, qy, qz, allrows)
    if ex >= 0 and own_on.any():
        r = allrows[own_on]
        clip(np.array([ex], dtype=np.int64), ox[own_on], oy[own_on], oz[own_on], r)
    if not rows_i:
        return out
    ri = np.concatenate(rows_i)
    cb = np.concatenate(cols_b)
    ax, ay, bx, by = np.concatenate(Ax), np.concatenate(Ay), np.concatenate(Bx), np.concatenate(By)
    coords = np.stack([np.stack([ax, ay], axis=1), np.stack([bx, by], axis=1)], axis=1)
    geoms = shapely.linestrings(coords)
    degenerate = (ax == bx) & (ay == by)
    if degenerate.any():
        geoms[degenerate] = shapely.points(ax[degenerate], ay[degenerate])
    hit = shapely.intersects(idx.polys[cb], geoms)
    out[ri[hit]] = False
    return out


def segment_polygon_entry(polygon, ax: float, ay: float, bx: float, by: float) -> float | None:
    """First point of the 2D segment A -> B inside ``polygon`` (boundary counts as inside).

    Returns the fraction s in [0, 1] (the point A + s (B - A)), 0.0 when A itself
    is inside, or None when the segment never touches the polygon. Exact GEOS
    predicates are used, so an end point that ``shapely.intersects_xy`` puts
    inside always gives a result. Used for the no-fly test of each flown step.
    """
    if shapely.intersects_xy(polygon, ax, ay):
        return 0.0
    if ax == bx and ay == by:
        return None
    line = shapely.LineString([(ax, ay), (bx, by)])
    if not shapely.intersects(polygon, line):
        return None
    coords = shapely.get_coordinates(shapely.intersection(line, polygon))
    if len(coords) == 0:  # touch too small for the overlay; the predicate said it touches
        return 1.0
    rx, ry = bx - ax, by - ay
    L2 = rx * rx + ry * ry
    s = min(((cx - ax) * rx + (cy - ay) * ry) / L2 for cx, cy in coords.tolist())
    return 0.0 if s < 0.0 else (1.0 if s > 1.0 else s)


def _resolve_target(world, target):
    if isinstance(target, str):
        return world.targets[target]
    if isinstance(target, dict):
        return world.targets[target["id"]]
    return target


def viewpoint_reason(world, target, D, t: float | None = None) -> str | None:
    """Why a photo of ``target`` from drone position D at time t is invalid, or None if valid.

    ``target`` is a target id, a target dict (with ``"id"``) or a :class:`~skyline.world.Target`.
    Static targets ignore ``t``; moving targets need it (positions are
    interpolated along the track; outside the track the reason is ``"absent"``).

    Reasons, checked in this order: ``"absent"``, ``"range"``, ``"cone"``
    (static), ``"elevation"`` (moving), ``"los"``.
    """
    tg = _resolve_target(world, target)
    T = tg.position_at(t)
    if T is None:
        if tg.moving and t is None:
            raise ValueError(f"target {tg.id} is moving: viewpoint_reason needs a time t")
        return REASON_ABSENT
    r = world.rules
    Dx, Dy, Dz = _xyz(D)
    vx, vy, vz = Dx - T[0], Dy - T[1], Dz - T[2]
    dist = math.sqrt(vx * vx + vy * vy + vz * vz)
    rmin, rmax = r.photo_range_m
    if dist < rmin - EPS or dist > rmax + EPS or dist == 0.0:
        return REASON_RANGE
    if tg.moving:
        elev = math.degrees(math.asin(max(-1.0, min(1.0, vz / dist))))
        if elev < r.moving_min_elevation_deg - EPS:
            return REASON_ELEVATION
    else:
        n = tg.normal
        nn = math.sqrt(n[0] * n[0] + n[1] * n[1] + n[2] * n[2])
        c = (vx * n[0] + vy * n[1] + vz * n[2]) / (dist * nn)
        ang = math.degrees(math.acos(max(-1.0, min(1.0, c))))
        if ang > r.static_cone_deg + EPS:
            return REASON_CONE
    if not los_clear(world, (Dx, Dy, Dz), T, tg.building_id):
        return REASON_LOS
    return None


def viewpoint_ok(world, target, D, t: float | None = None) -> bool:
    """True if a photo of ``target`` from D (at time t, for moving targets) would be valid.

    Applies range, cone/elevation and line of sight; see :func:`viewpoint_reason`.
    """
    return viewpoint_reason(world, target, D, t) is None
