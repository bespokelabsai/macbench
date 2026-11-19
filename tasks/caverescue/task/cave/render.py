"""Drone headlamp photos of a surveyed cave, rendered on CPU by sphere-tracing.

The passage volume is the union of elliptical tubes swept along each survey leg,
sized from the survey's LRUD cross-sections, with a little rock noise on the walls.
Scene objects (people, gear, chalk marks, water) are signed-distance primitives
placed by the scenario. Everything is deterministic for a given cave and scene.
"""
import math, numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H, FOV = 384, 288, 80.0
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# ---------- cave geometry ----------
class Cave:
    def __init__(self, g):
        S = g["stations"]
        self.station_pos = np.array([[s["x"], s["y"], s["z"]] for s in S], float)
        self.w = np.array([max(s["w"], 0.35) for s in S]); self.h = np.array([max(s["h"], 0.35) for s in S])
        segs = []
        for p in g["passages"]:
            st = p["stations"]
            for a, b in zip(st, st[1:]): segs.append((a, b))
        self.segs = np.array(segs, int)
        # passage centre at each station: shift by half the LRUD imbalance, across the mean leg direction
        dirs = np.zeros((len(S), 3))
        for a, b in self.segs:
            v = self.station_pos[b] - self.station_pos[a]; v[2] = 0
            n = np.linalg.norm(v)
            if n > 1e-6: dirs[a] += v / n; dirs[b] += v / n
        right = np.stack([dirs[:, 1], -dirs[:, 0], np.zeros(len(S))], 1)
        right /= np.maximum(np.linalg.norm(right, axis=1, keepdims=True), 1e-9)
        lr = np.array([[s.get("l", 0.5), s.get("r", 0.5), s.get("u", 0.5), s.get("d", 0.5)] for s in S])
        self.pos = self.station_pos + right * ((lr[:, 1] - lr[:, 0]) / 2)[:, None]
        self.pos[:, 2] += (lr[:, 2] - lr[:, 3]) / 2
        self.floor = self.station_pos[:, 2] - lr[:, 3]
        A, B = self.pos[self.segs[:, 0]], self.pos[self.segs[:, 1]]
        self.A, self.B = A, B
        self.ra = np.stack([self.w[self.segs[:, 0]] / 2, self.h[self.segs[:, 0]] / 2], 1)
        self.rb = np.stack([self.w[self.segs[:, 1]] / 2, self.h[self.segs[:, 1]] / 2], 1)
        self.mid = (A + B) / 2
        self.halflen = np.linalg.norm(B - A, axis=1) / 2

    def near(self, p, radius):
        d = np.linalg.norm(self.mid - p, axis=1) - self.halflen - np.maximum(self.ra.max(1), self.rb.max(1))
        return np.where(d < radius)[0]

def _hash3(ix, iy, iz, seed):
    h = (ix * 73856093) ^ (iy * 19349663) ^ (iz * 83492791) ^ (seed * 2654435761)
    h = (h ^ (h >> 13)) * 1274126177
    return ((h ^ (h >> 16)) & 0xFFFF).astype(np.float32) / 65535.0

def _value3(p, seed):
    f = np.floor(p); w = p - f; i = f.astype(np.int64)
    w = w * w * (3 - 2 * w)
    out = 0
    for dx in (0, 1):
        for dy in (0, 1):
            for dz in (0, 1):
                v = _hash3(i[..., 0] + dx, i[..., 1] + dy, i[..., 2] + dz, seed)
                wx = w[..., 0] if dx else 1 - w[..., 0]; wy = w[..., 1] if dy else 1 - w[..., 1]; wz = w[..., 2] if dz else 1 - w[..., 2]
                out = out + v * wx * wy * wz
    return out * 2 - 1

def _noise3(p, scale, seed=0):
    """Isotropic fractal value noise in [-1, 1], deterministic per seed."""
    out = np.zeros(p.shape[:-1]); amp = 1.0; tot = 0
    for k in range(3):
        out += amp * _value3(p * scale * (2.03 ** k) + 17.1 * k, seed + k); tot += amp; amp *= 0.5
    return out / tot

def cave_inside(cave, idx, P):
    """Positive inside the passage (approx metres to wall), negative in rock."""
    A, B = cave.A[idx], cave.B[idx]; ra, rb = cave.ra[idx], cave.rb[idx]
    AB = B - A; L2 = np.maximum((AB ** 2).sum(1), 1e-9)
    AP = P[:, None, :] - A[None]
    t = np.clip((AP * AB[None]).sum(2) / L2[None], 0, 1)
    C = A[None] + t[..., None] * AB[None]
    D = P[:, None, :] - C
    r = ra[None] + t[..., None] * (rb - ra)[None]          # (N,M,2) horizontal, vertical radii
    dz = D[..., 2]; dh = np.sqrt(np.maximum((D[..., :2] ** 2).sum(2), 0))
    q = np.sqrt((dh / r[..., 0]) ** 2 + (dz / r[..., 1]) ** 2)
    s = (1 - q) * np.minimum(r[..., 0], r[..., 1])
    return s.max(1)

# ---------- scene objects (SDF, positive outside the object) ----------
def sd_sphere(P, c, r): return np.linalg.norm(P - c, axis=1) - r
def sd_capsule(P, a, b, r):
    ab = b - a; t = np.clip(((P - a) @ ab) / max(ab @ ab, 1e-9), 0, 1)
    return np.linalg.norm(P - (a + t[:, None] * ab), axis=1) - r
def sd_box(P, c, half, yaw=0.0, rnd=0.0):
    cy, sy = math.cos(yaw), math.sin(yaw)
    Q = P - c; Q = np.stack([cy * Q[:, 0] + sy * Q[:, 1], -sy * Q[:, 0] + cy * Q[:, 1], Q[:, 2]], 1)
    d = np.abs(Q) - (np.asarray(half) - rnd)
    return np.linalg.norm(np.maximum(d, 0), axis=1) + np.minimum(d.max(1), 0) - rnd
def sd_torus(P, c, R, r):
    Q = P - c; qx = np.sqrt(Q[:, 0] ** 2 + Q[:, 1] ** 2) - R
    return np.sqrt(qx ** 2 + Q[:, 2] ** 2) - r

class Obj:
    """One renderable thing: a list of (sdf_fn, colour) parts."""
    def __init__(self, parts, glossy=0.0): self.parts, self.glossy = parts, glossy
    def sdf(self, P):
        ds = np.stack([f(P) for f, _ in self.parts], 1); return ds.min(1), ds.argmin(1)

# ---------- camera ----------
def look_basis(fwd):
    f = fwd / np.linalg.norm(fwd)
    up = np.array([0, 0, 1.0])
    if abs(f @ up) > 0.95: up = np.array([0, 1.0, 0])
    r = np.cross(f, up); r /= np.linalg.norm(r); u = np.cross(r, f)
    return f, r, u

def _cull(cave, idx, eye, axis, half_angle, max_dist):
    v = cave.mid[idx] - eye; dist = np.linalg.norm(v, axis=1)
    rad = cave.halflen[idx] + np.maximum(cave.ra[idx].max(1), cave.rb[idx].max(1)) + 0.3
    inside = dist <= rad
    cosang = (v @ axis) / np.maximum(dist, 1e-9)
    ang = np.arccos(np.clip(cosang, -1, 1)) - np.arcsin(np.clip(rad / np.maximum(dist, 1e-9), 0, 1))
    return idx[inside | ((ang < half_angle) & (dist - rad < max_dist))]

def _march(cave, idx, objs, eye, D, max_dist, steps, seed):
    N = len(D); t = np.zeros(N, np.float32); alive = np.ones(N, bool)
    hit_obj = np.full(N, -1); hit_part = np.zeros(N, int)
    for _ in range(steps):
        a = np.where(alive)[0]
        if not len(a): break
        P = eye + D[a] * t[a, None]
        d = cave_inside(cave, idx, P) + 0.09 * _noise3(P, 1.7, seed) + 0.025 * _noise3(P, 9.0, seed + 3)
        dobj = np.full(len(a), np.inf); which = np.full(len(a), -1); part = np.zeros(len(a), int)
        for k, o in enumerate(objs):
            do, pk = o.sdf(P)
            m = do < dobj; dobj[m] = do[m]; which[m] = k; part[m] = pk[m]
        step = np.minimum(d, dobj)
        done = (step < 0.004 + 0.001 * t[a]) | (t[a] > max_dist)
        oh = done & (dobj < d)
        hit_obj[a[oh]] = which[oh]; hit_part[a[oh]] = part[oh]
        alive[a[done]] = False
        t[a[~done]] += np.maximum(step[~done] * 0.7, 0.003)
    # refine wall hits onto the surface so the depth buffer has no step banding
    wall = (hit_obj < 0) & (t < max_dist)
    a = np.where(wall)[0]
    for _ in range(5):
        if not len(a): break
        P = eye + D[a] * t[a, None]
        d = cave_inside(cave, idx, P) + 0.09 * _noise3(P, 1.7, seed) + 0.025 * _noise3(P, 9.0, seed + 3)
        t[a] += 0.9 * d
    return np.minimum(t, max_dist), hit_obj, hit_part

def render(cave, eye, target, objs=(), water_z=None, decals=(), seed=0, max_dist=40.0, steps=110, tiles=6, stats=None):
    """Return a PIL image from eye looking at target.

    objs: list of Obj; water_z: flood surface height (metres, survey frame) or None;
    decals: list of dicts {center, normal, up, size, image(np.uint8 HxW alpha)} for chalk marks.
    """
    eye = np.asarray(eye, float)
    f, r, u = look_basis(np.asarray(target, float) - eye)
    tanh = math.tan(math.radians(FOV / 2)); aspect = H / W
    xs = (np.arange(W) + .5) / W * 2 - 1; ys = (np.arange(H) + .5) / H * 2 - 1
    X, Y = np.meshgrid(xs * tanh, -ys * tanh * aspect)
    D = f[None] + X.reshape(-1, 1) * r[None] + Y.reshape(-1, 1) * u[None]
    D /= np.linalg.norm(D, axis=1, keepdims=True)
    N = D.shape[0]
    near = cave.near(eye, max_dist)
    t = np.zeros(N); hit_obj = np.full(N, -1); hit_part = np.zeros(N, int)
    pix = np.arange(N).reshape(H, W)
    th, tw = math.ceil(H / tiles), math.ceil(W / tiles)
    for ty in range(tiles):
        for tx in range(tiles):
            ids = pix[ty * th:(ty + 1) * th, tx * tw:(tx + 1) * tw].ravel()
            if not len(ids): continue
            Dt = D[ids]; axis = Dt.mean(0); axis /= np.linalg.norm(axis)
            half = float(np.arccos(np.clip(Dt @ axis, -1, 1)).max())
            idx = _cull(cave, near, eye, axis, half, max_dist)
            if not len(idx): t[ids] = max_dist; continue
            t[ids], hit_obj[ids], hit_part[ids] = _march(cave, idx, objs, eye, Dt, max_dist, steps, seed)
    Phit = eye + D * t[:, None]
    wmask = np.zeros(N, bool)
    if water_z is not None:
        with np.errstate(divide="ignore", invalid="ignore"):
            tw_ = (water_z - eye[2]) / D[:, 2]
        wmask = (tw_ > 0) & (tw_ < t)
        t = np.where(wmask, tw_, t); Phit = eye + D * t[:, None]
    depth = t.reshape(H, W)
    P3 = Phit.reshape(H, W, 3)
    # smooth normals from a lightly blurred position buffer
    k = np.array([1, 2, 1], float) / 4
    Ps = P3.copy()
    for ax in (0, 1):
        Ps = (np.roll(Ps, 1, ax) * k[0] + Ps * k[1] + np.roll(Ps, -1, ax) * k[2])
    dx = np.gradient(Ps, axis=1); dy = np.gradient(Ps, axis=0)
    n = np.cross(dx, dy); n /= np.linalg.norm(n, axis=2, keepdims=True) + 1e-9
    Dv = D.reshape(H, W, 3)
    n = np.where((n * Dv).sum(2, keepdims=True) > 0, -n, n)
    lam = np.clip(-(n * Dv).sum(2), 0, 1)
    # cavity darkening: pixels deeper than their neighbourhood
    dblur = depth.copy()
    for _ in range(6):
        for ax in (0, 1): dblur = (np.roll(dblur, 2, ax) + dblur + np.roll(dblur, -2, ax)) / 3
    ao = np.clip(1 - np.maximum(depth - dblur, 0) / (1.2 + 0.15 * depth), 0.55, 1)
    tex = _noise3(Phit, 5.0, seed + 1).reshape(H, W)
    fine = _noise3(Phit, 23.0, seed + 2).reshape(H, W)
    band = _noise3(Phit * np.array([0.15, 0.15, 1.0]), 2.0, seed + 4).reshape(H, W)
    rock = np.stack([0.50 + 0.05 * band, 0.47 + 0.045 * band, 0.43 + 0.04 * band], 2) * (0.78 + 0.14 * tex[..., None] + 0.08 * fine[..., None])
    mud = np.array([0.40, 0.31, 0.22]) * (0.85 + 0.15 * fine[..., None])
    upness = np.clip((n[..., 2] - 0.35) / 0.65, 0, 1)[..., None]
    col = rock * (1 - upness) + mud * upness
    decal_px = []
    for dc in decals:
        c = np.asarray(dc["center"]); nn = np.asarray(dc["normal"], float); nn /= np.linalg.norm(nn)
        up = np.asarray(dc["up"], float); up -= nn * (up @ nn); up /= np.linalg.norm(up); rt = np.cross(up, nn)
        Q = P3 - c; dist_plane = np.abs((Q * nn).sum(2))
        uu = (Q * rt).sum(2) / dc["size"][0] + .5; vv = .5 - (Q * up).sum(2) / dc["size"][1]
        img = dc["image"]; ih, iw = img.shape
        m = (dist_plane < 0.7) & (uu >= 0) & (uu < 1) & (vv >= 0) & (vv < 1) & (hit_obj.reshape(H, W) < 0)
        a = np.zeros((H, W)); a[m] = img[(vv[m] * ih).astype(int), (uu[m] * iw).astype(int)] / 255.0
        a *= dc.get("alpha", 1.0) * (0.8 + 0.2 * fine)
        chalk = np.array(dc.get("colour", (0.93, 0.92, 0.88)))
        col = col * (1 - a[..., None]) + chalk * a[..., None]
        decal_px.append(int((a > 0.3).sum()))
    ho = hit_obj.reshape(H, W); hp = hit_part.reshape(H, W)
    for k_, o in enumerate(objs):
        for pi, (_, c) in enumerate(o.parts):
            m = (ho == k_) & (hp == pi)
            col[m] = np.asarray(c) * (0.85 + 0.15 * tex[m][:, None])
    wm = wmask.reshape(H, W)
    ripple = _noise3(Phit * np.array([1, 1, 0]), 3.0, seed + 5).reshape(H, W)
    col[wm] = np.array([0.08, 0.15, 0.16]) * (0.8 + 0.2 * ripple[wm][:, None])
    fall = 1.0 / (1 + (depth / 3.5) ** 2)
    light = (0.03 + lam * fall) * ao
    spec = np.zeros((H, W)); spec[wm] = 0.18 * fall[wm] * np.clip(ripple[wm] + 0.6, 0, 1.2)
    img = col * light[..., None] + spec[..., None]
    img[depth >= max_dist - 1e-3] = 0.0
    # auto exposure on the bright part of the frame, like a drone camera
    lum = img.mean(2); ref = np.percentile(lum, 92)
    img *= 0.75 / max(ref, 1e-4)
    rng = np.random.default_rng(seed + 7)
    img += rng.normal(0, 0.015, img.shape) * (1.2 - np.clip(img, 0, 1))
    yy, xx = np.mgrid[0:H, 0:W]; vig = 1 - 0.4 * (((xx - W / 2) / (W / 2)) ** 2 + ((yy - H / 2) / (H / 2)) ** 2)
    img = np.nan_to_num(img * vig[..., None])
    img = np.clip(np.clip(img, 0, None) ** (1 / 2.0), 0, 1)
    if stats is not None:
        vis = ho.copy(); vis[wm] = -1
        stats["obj_pixels"] = {k_: int((vis == k_).sum()) for k_ in range(len(objs))}
        stats["decal_pixels"] = decal_px
        if stats.get("want_geometry"):   # v5 trail placement: where each pixel hit rock, its normal, depth and light
            stats.update(P3=P3, normal=n, depth=depth, light=light, water=wm, obj=ho, right=r, fwd=f)
    return Image.fromarray((img * 255).astype(np.uint8))

def text_decal(lines, px=28, colour=255):
    font = ImageFont.truetype(FONT, px)
    w = max(int(font.getlength(s)) for s in lines) + 16; h = (px + 8) * len(lines) + 8
    im = Image.new("L", (w, h), 0); d = ImageDraw.Draw(im)
    for i, s in enumerate(lines): d.text((8, 4 + i * (px + 8)), s, fill=colour, font=font)
    return np.array(im)
